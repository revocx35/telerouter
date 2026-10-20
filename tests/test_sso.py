"""Single sign-on (OpenID Connect, e.g. Authentik) against a fake provider: the happy path, login CSRF, replay,
foreign tokens, people who aren't allowed, and the settings being password-protected and write-only."""

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from telerouter import security, tls
from telerouter.web.app import create_app

from .test_web import PW, setup_admin

ISSUER = "https://auth.example.test/application/o/telerouter/"


def b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")


class Provider:
    """A minimal OpenID provider behind httpx.MockTransport."""

    def __init__(self):
        self.nonce = ""
        self.challenge = ""
        self.claims: dict = {}
        self.userinfo: dict = {}
        self.token_calls = 0

    def id_token(self, **over) -> str:
        now = time.time()
        c = {"iss": ISSUER, "aud": "telerouter", "sub": "u1", "exp": now + 300, "iat": now, "nonce": self.nonce,
             "preferred_username": "salih", "email": "salih@example.test", "groups": ["family"], **self.claims, **over}
        return f"{b64({'alg': 'RS256'})}.{b64(c)}.sig"

    def handle(self, req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": "https://auth.example.test/application/o/authorize/",
                "token_endpoint": "https://auth.example.test/application/o/token/",
                "userinfo_endpoint": "https://auth.example.test/application/o/userinfo/",
                "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"]})
        if req.url.path.endswith("/token/"):
            self.token_calls += 1
            form = parse_qs(req.content.decode())
            verifier = form["code_verifier"][0]
            digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            basic = base64.b64encode(b"telerouter:s3cret").decode()
            if digest != self.challenge or req.headers.get("authorization") != f"Basic {basic}" or form["code"] != ["good"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "at", "token_type": "Bearer", "id_token": self.id_token()})
        if req.url.path.endswith("/userinfo/"):
            return httpx.Response(200, json={"sub": "u1", **self.userinfo})
        return httpx.Response(404)


@pytest.fixture()
def env(settings):
    files = tls.ensure(settings.data_dir, [])
    app, _ = create_app(settings, files)
    prov = Provider()
    app.state.sso._transport = httpx.MockTransport(prov.handle)
    with TestClient(app, base_url="https://testserver") as c:
        yield c, prov


def enable(c, csrf, **over):
    sso = {"enabled": True, "issuer": ISSUER, "client_id": "telerouter", "client_secret": "s3cret",
           "allowed_users": ["salih"], **over}
    return c.put("/api/account/sso", json={"password": PW, "sso": sso}, headers={"X-CSRF-Token": csrf})


def start(c, prov):
    """GET /api/oidc/start; the provider remembers the nonce and PKCE challenge. Returns the state."""
    r = c.get("/api/oidc/start", follow_redirects=False)
    assert r.status_code == 303, r.text
    loc = urlsplit(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(loc.query).items()}
    assert loc.netloc == "auth.example.test" and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == "https://testserver/api/oidc/callback" and q["client_id"] == "telerouter"
    assert "openid" in q["scope"].split()
    prov.nonce, prov.challenge = q["nonce"], q["code_challenge"]
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-telerouter-sso=") and "samesite=lax" in cookie.lower() and "httponly" in cookie.lower()
    return q["state"]


def logged_in(c) -> bool:
    return c.get("/api/session").json()["logged_in"]


def fresh(c, csrf):
    c.post("/api/logout", headers={"X-CSRF-Token": csrf})
    c.cookies.delete(security.COOKIE)


def test_sign_in_with_authentik(env):
    c, prov = env
    csrf = setup_admin(c)
    assert enable(c, csrf, auto_redirect=True).status_code == 200
    fresh(c, csrf)
    assert c.get("/api/session").json()["sso"] == {"label": "Authentik", "auto": True}
    state = start(c, prov)
    r = c.get(f"/api/oidc/callback?state={state}&code=good", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert logged_in(c) and c.get("/api/status").status_code == 200
    # the state works once
    r = c.get(f"/api/oidc/callback?state={state}&code=good", follow_redirects=False)
    assert r.headers["location"] == "/?sso=failed"


def test_groups_from_userinfo_count(env):
    c, prov = env
    csrf = setup_admin(c)
    assert enable(c, csrf, allowed_users=[], allowed_groups=["Telerouter admins"]).status_code == 200
    fresh(c, csrf)
    prov.claims = {"groups": None, "preferred_username": "guest"}
    prov.userinfo = {"groups": ["telerouter admins"]}
    state = start(c, prov)
    c.get(f"/api/oidc/callback?state={state}&code=good", follow_redirects=False)
    assert logged_in(c)


@pytest.mark.parametrize("case", ["other_browser", "not_allowed", "wrong_nonce", "other_client", "other_issuer",
                                  "expired", "provider_error", "bad_code", "unverified_email"])
def test_sign_on_failures_log_nobody_in(env, case):
    c, prov = env
    csrf = setup_admin(c)
    assert enable(c, csrf, allowed_users=["salih@example.test"]).status_code == 200
    fresh(c, csrf)
    state = start(c, prov)
    code = "good"
    prov.claims = {"preferred_username": "someone"}
    if case == "other_browser":
        c.cookies.delete("__Host-telerouter-sso")                # login CSRF: the victim's browser never started it
    elif case == "not_allowed":
        prov.claims["email"] = "mallory@example.test"
    elif case == "wrong_nonce":
        prov.nonce = "replayed"
    elif case == "other_client":
        prov.claims["aud"] = "another-app"
    elif case == "other_issuer":
        prov.claims["iss"] = "https://evil.example.test/"
    elif case == "expired":
        prov.claims["exp"] = time.time() - 3600
    elif case == "bad_code":
        code = "stolen"
    elif case == "unverified_email":
        prov.claims["email_verified"] = False
    q = f"state={state}&code={code}" + ("&error=access_denied" if case == "provider_error" else "")
    r = c.get(f"/api/oidc/callback?{q}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?sso=failed"
    assert not logged_in(c)


def test_sso_settings_need_the_password_and_hide_the_secret(env):
    c, prov = env
    csrf = setup_admin(c)
    assert enable(c, csrf).status_code == 200
    r = c.put("/api/account/sso", json={"password": "wrong password!", "sso": {"enabled": False}},
              headers={"X-CSRF-Token": csrf})
    assert r.status_code == 401
    got = c.get("/api/account/sso").json()
    assert got["sso"]["client_secret"] == "" and got["sso"]["client_secret_set"] is True
    assert got["redirect_uri"] == "https://testserver/api/oidc/callback"
    assert "s3cret" not in json.dumps(got)
    # an empty secret keeps the stored one; no allowed people is refused
    assert enable(c, csrf, client_secret="").status_code == 200
    assert c.app.state.store.config.sso.client_secret == "s3cret"
    assert enable(c, csrf, allowed_users=[]).status_code == 422
    # a provider that isn't one is refused when saving
    assert enable(c, csrf, issuer="https://auth.example.test/application/o/other/").status_code == 422
    # needs a session + CSRF like every admin change
    assert c.put("/api/account/sso", json={"password": PW, "sso": {}}).status_code == 403


def test_the_redirect_uri_follows_a_trusted_proxy(settings):
    settings.trusted_proxies = "127.0.0.1/32"
    app, _ = create_app(settings, tls.ensure(settings.data_dir, []))
    with TestClient(app, base_url="https://testserver", client=("127.0.0.1", 50000)) as c:
        csrf = setup_admin(c)
        got = c.get("/api/account/sso", headers={"X-Forwarded-Host": "telerouter.example.org"}).json()
        assert got["redirect_uri"] == "https://telerouter.example.org/api/oidc/callback"
        assert csrf


def test_an_untrusted_forwarded_host_is_ignored(env):
    c, _ = env
    setup_admin(c)
    got = c.get("/api/account/sso", headers={"X-Forwarded-Host": "evil.example.org"}).json()
    assert got["redirect_uri"] == "https://testserver/api/oidc/callback"


def test_sso_off_means_no_endpoints(env):
    c, _ = env
    csrf = setup_admin(c)
    fresh(c, csrf)
    assert c.get("/api/session").json()["sso"] is None
    assert c.get("/api/oidc/start", follow_redirects=False).status_code == 404
    r = c.get("/api/oidc/callback?state=x&code=good", follow_redirects=False)
    assert r.headers["location"] == "/?sso=failed" and not logged_in(c)
