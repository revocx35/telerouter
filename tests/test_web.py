"""Web API auth paths: setup code, login, lockout, CSRF, session expiry, 2FA, headers, secrets, tokens."""

import json

import pyotp
import pytest
from fastapi.testclient import TestClient

from telerouter import security, tls
from telerouter.web.app import create_app

PW = "correct horse battery"


@pytest.fixture()
def app(settings):
    files = tls.ensure(settings.data_dir, [])
    public, _internal = create_app(settings, files)
    return public


@pytest.fixture()
def c(app):
    with TestClient(app, base_url="https://testserver") as client:
        yield client


def use_cookie(c, value):
    c.cookies.clear()
    c.cookies.set(security.COOKIE, value)


def setup_admin(c):
    r = c.post("/api/setup", json={"setup_code": "ABCD-EFGH", "username": "admin", "password": PW})
    assert r.status_code == 200, r.text
    return c.get("/api/session").json()["csrf"]


def test_setup_needs_the_code_and_a_strong_password(c):
    assert c.get("/api/session").json()["setup_needed"] is True
    assert c.get("/api/status").status_code == 401
    r = c.post("/api/setup", json={"setup_code": "WRONG-CODE", "username": "admin", "password": PW})
    assert r.status_code == 401
    r = c.post("/api/setup", json={"setup_code": "ABCD-EFGH", "username": "admin", "password": "short"})
    assert r.status_code == 422
    setup_admin(c)
    r = c.post("/api/setup", json={"setup_code": "ABCD-EFGH", "username": "x", "password": PW})
    assert r.status_code == 409


def test_cookie_flags_and_security_headers(c):
    r = c.post("/api/setup", json={"setup_code": "ABCD-EFGH", "username": "admin", "password": PW})
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-telerouter=")
    for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
        assert flag.lower() in cookie.lower()
    h = c.get("/").headers
    assert "script-src 'self'" in h["content-security-policy"] and "unsafe-inline" not in h["content-security-policy"]
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "no-referrer" and "max-age" in h["strict-transport-security"]
    assert c.get("/api/status").headers["cache-control"] == "no-store"


def test_wrong_password_is_generic_and_locks_out(c):
    setup_admin(c)
    c.post("/api/logout", headers={"X-CSRF-Token": ""})
    r1 = c.post("/api/login", json={"username": "admin", "password": "wrong password 1"})
    r2 = c.post("/api/login", json={"username": "nobody", "password": PW})
    assert r1.status_code == r2.status_code == 401
    assert r1.json()["detail"] == r2.json()["detail"]                     # no user enumeration
    codes = [c.post("/api/login", json={"username": "admin", "password": f"wrong {i} password"}).status_code
             for i in range(8)]
    assert 429 in codes
    r = c.post("/api/login", json={"username": "admin", "password": PW})
    assert r.status_code == 429 and "retry-after" in r.headers             # even the right password waits


def test_state_changes_need_the_csrf_token(c):
    csrf = setup_admin(c)
    body = {"name": "Dad", "number": "1005"}
    assert c.post("/api/contacts", json=body).status_code == 403
    assert c.post("/api/contacts", json=body, headers={"X-CSRF-Token": "forged"}).status_code == 403
    assert c.post("/api/contacts", json=body, headers={"X-CSRF-Token": csrf,
                                                       "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.post("/api/contacts", json=body, headers={"X-CSRF-Token": csrf}).status_code == 200


def test_logout_and_expired_sessions_are_dead(app, c):
    csrf = setup_admin(c)
    assert c.get("/api/status").status_code == 200
    sessions = app.state.sessions
    real = sessions._clock
    sessions._clock = lambda: real() + sessions.idle_s + 5             # idle too long
    assert c.get("/api/status").status_code == 401
    sessions._clock = real
    c.post("/api/login", json={"username": "admin", "password": PW})
    csrf = c.get("/api/session").json()["csrf"]
    old = c.cookies.get(security.COOKIE)
    c.post("/api/logout", headers={"X-CSRF-Token": csrf})
    use_cookie(c, old)
    assert c.get("/api/status").status_code == 401                     # the old cookie is invalid server-side


def test_login_rotates_the_session(c):
    setup_admin(c)
    before = c.cookies.get(security.COOKIE)
    c.post("/api/login", json={"username": "admin", "password": PW})
    assert c.cookies.get(security.COOKIE) != before


def test_two_factor_login(app, c):
    csrf = setup_admin(c)
    h = {"X-CSRF-Token": csrf}
    secret = c.post("/api/account/totp/setup", headers=h).json()["secret"]
    assert c.post("/api/account/totp/enable", json={"code": "000000"}, headers=h).status_code == 401
    assert c.post("/api/account/totp/enable", json={"code": pyotp.TOTP(secret).now()}, headers=h).status_code == 200
    c.post("/api/logout", headers=h)
    r = c.post("/api/login", json={"username": "admin", "password": PW})
    assert r.json()["totp_needed"] is True
    assert c.get("/api/status").status_code == 401                     # password alone is not enough
    assert c.post("/api/login/totp", json={"code": "123456"}).status_code == 401
    # a fresh code (the enable code was used already in this 30 s step)
    import time
    code = pyotp.TOTP(secret).at(time.time() + 30)
    ok = c.post("/api/login/totp", json={"code": code})
    assert ok.status_code == 200
    assert c.get("/api/status").status_code == 200


def test_secrets_are_write_only(c):
    csrf = setup_admin(c)
    h = {"X-CSRF-Token": csrf}
    r = c.put("/api/pbx", json={"host": "127.0.0.1", "port": 9, "username": "1011", "password": "s3cret!"}, headers=h)
    assert r.json()["password"] == "" and r.json()["password_set"] is True
    c.put("/api/pbx", json={"display_name": "TR", "password": ""}, headers=h)       # empty keeps it
    assert c.app.state.store.config.pbx.password == "s3cret!"
    assert "s3cret!" not in json.dumps(c.get("/api/pbx").json())


def test_speaker_enrollment_code_is_single_use(c):
    csrf = setup_admin(c)
    r = c.post("/api/clients/enroll", json={"name": "Kitchen"}, headers={"X-CSRF-Token": csrf}).json()
    assert "--pinnedpubkey" in r["command"] and r["code"] in r["command"]
    assert c.post("/api/client/enroll", json={"code": "AAAA-BBBB"}).status_code == 401
    ok = c.post("/api/client/enroll", json={"code": r["code"].lower(), "platform": "Linux"})
    assert ok.status_code == 200 and ok.json()["token"].startswith("trc_")
    assert c.post("/api/client/enroll", json={"code": r["code"]}).status_code == 401
    stored = c.app.state.store.config.clients[0]
    assert stored.token_hash == security.token_hash(ok.json()["token"])


def test_speaker_websocket_refuses_bad_tokens(c):
    from starlette.websockets import WebSocketDisconnect
    setup_admin(c)
    for headers in ({}, {"Authorization": "Bearer trc_nope"}):
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws/client", headers=headers) as ws:
                ws.receive_text()


def test_mcp_tokens_shown_once_and_revocable(c):
    csrf = setup_admin(c)
    h = {"X-CSRF-Token": csrf}
    r = c.post("/api/mcp-tokens", json={"name": "laptop", "scopes": ["contacts"]}, headers=h).json()
    tok = r["token"]
    assert tok.startswith("trm_") and "token" not in c.get("/api/mcp-tokens").json()["tokens"][0]
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert c.post("/mcp", json=rpc).status_code == 401
    ok = c.post("/mcp", json=rpc, headers={"Authorization": f"Bearer {tok}"})
    assert [t["name"] for t in ok.json()["result"]["tools"]] == ["list_contacts"]
    c.delete(f"/api/mcp-tokens/{r['id']}", headers=h)
    assert c.post("/mcp", json=rpc, headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_password_change_logs_out_other_sessions(app, c):
    setup_admin(c)
    first = c.cookies.get(security.COOKIE)
    c.post("/api/login", json={"username": "admin", "password": PW})        # a second session
    csrf = c.get("/api/session").json()["csrf"]
    r = c.post("/api/account/password", json={"current": PW, "new": "another long password"},
               headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert c.get("/api/status").status_code == 200                        # this one got a fresh session
    mine = c.cookies.get(security.COOKIE)
    use_cookie(c, first)
    assert c.get("/api/status").status_code == 401                        # the other one is gone
    use_cookie(c, mine)
