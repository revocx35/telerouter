"""Single sign-on for the admin UI: OpenID Connect authorization code flow with PKCE (e.g. Authentik).

The browser is sent to the provider with a random `state`, a `nonce` and a PKCE challenge; the state is also tied
to a short-lived cookie in that browser (login CSRF). The callback trades the code for tokens directly with the
provider's token endpoint over verified TLS. The ID token is taken from that response, so its claims are checked
(issuer, audience, expiry, nonce) but its signature is not needed (OpenID Connect Core 3.1.3.7, TLS server
validation). Only the people listed in the settings (user names, e-mail addresses or groups) get in, as the admin.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

import httpx

from .models import SsoSettings

MAX_BODY = 256 * 1024


class SsoError(Exception):
    """Why a sign-on failed (logged on the server; the browser only learns that it failed)."""


@dataclass
class Pending:
    binding_hash: str
    nonce: str
    verifier: str
    redirect_uri: str
    created: float = field(default_factory=time.time)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def jwt_claims(token: str) -> dict:
    """The payload of a JWT (not verified here: see the module docstring)."""
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as e:
        raise SsoError("the ID token is not a JWT") from e
    if not isinstance(data, dict):
        raise SsoError("the ID token is not a JWT")
    return data


class Sso:
    TTL = 600                  # seconds a sign-on may take
    MAX_PENDING = 100
    DISCOVERY_TTL = 3600

    def __init__(self, get_cfg: Callable[[], SsoSettings], transport: httpx.AsyncBaseTransport | None = None,
                 clock=time.time):
        self._cfg = get_cfg
        self._transport = transport
        self._clock = clock
        self._pending: dict[str, Pending] = {}
        self._discovery: tuple[str, float, dict] | None = None

    def forget(self) -> None:
        """The settings changed: discover again, drop sign-ons in progress."""
        self._discovery = None
        self._pending.clear()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=10, follow_redirects=False, verify=True, transport=self._transport)

    async def _json(self, resp: httpx.Response, what: str) -> dict:
        body = resp.content[:MAX_BODY + 1]
        if len(body) > MAX_BODY:
            raise SsoError(f"{what}: the answer is too large")
        try:
            data = json.loads(body)
        except ValueError as e:
            raise SsoError(f"{what}: HTTP {resp.status_code}, not JSON") from e
        if not isinstance(data, dict):
            raise SsoError(f"{what}: unexpected answer")
        return data

    async def discovery(self, cfg: SsoSettings | None = None) -> dict:
        """The provider's /.well-known/openid-configuration (cached for an hour)."""
        cfg = cfg or self._cfg()
        now = self._clock()
        if self._discovery and self._discovery[0] == cfg.issuer and now - self._discovery[1] < self.DISCOVERY_TTL:
            return self._discovery[2]
        url = cfg.issuer.rstrip("/") + "/.well-known/openid-configuration"
        try:
            async with self._client() as c:
                r = await c.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError as e:
            raise SsoError(f"can't reach the provider ({type(e).__name__}: {e})") from e
        if r.status_code != 200:
            raise SsoError(f"the provider's discovery document answered HTTP {r.status_code} ({url})")
        doc = await self._json(r, "discovery")
        if str(doc.get("issuer", "")).rstrip("/") != cfg.issuer.rstrip("/"):
            raise SsoError(f"the provider calls itself {doc.get('issuer')!r}, not {cfg.issuer!r}")
        for key in ("authorization_endpoint", "token_endpoint"):
            if urlsplit(str(doc.get(key, ""))).scheme != "https":
                raise SsoError(f"the provider's {key} is not https")
        if doc.get("userinfo_endpoint") and urlsplit(str(doc["userinfo_endpoint"])).scheme != "https":
            doc.pop("userinfo_endpoint")
        self._discovery = (cfg.issuer, now, doc)
        return doc

    async def start(self, redirect_uri: str) -> tuple[str, str]:
        """The provider's login URL and the value for the browser's binding cookie."""
        cfg = self._cfg()
        if not cfg.enabled:
            raise SsoError("single sign-on is off")
        doc = await self.discovery(cfg)
        now = self._clock()
        self._pending = {k: p for k, p in self._pending.items() if now - p.created < self.TTL}
        while len(self._pending) >= self.MAX_PENDING:
            del self._pending[min(self._pending, key=lambda k: self._pending[k].created)]
        state, nonce, verifier, binding = (secrets.token_urlsafe(32) for _ in range(4))
        self._pending[state] = Pending(_sha(binding), nonce, verifier, redirect_uri, now)
        q = {"response_type": "code", "client_id": cfg.client_id, "redirect_uri": redirect_uri,
             "scope": cfg.scopes, "state": state, "nonce": nonce,
             "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"}
        auth = str(doc["authorization_endpoint"])
        return auth + ("&" if "?" in auth else "?") + urlencode(q), binding

    async def finish(self, state: str, code: str, binding: str, error: str = "") -> dict:
        """The callback: check state + binding cookie, trade the code, check the ID token. Returns its claims
        (merged with the userinfo endpoint's)."""
        cfg = self._cfg()
        p = self._pending.pop(state, None) if state else None
        if error:
            raise SsoError(f"the provider refused: {error[:100]}")
        if p is None or self._clock() - p.created > self.TTL:
            raise SsoError("unknown or expired sign-on (state)")
        if not binding or not hmac.compare_digest(_sha(binding), p.binding_hash):
            raise SsoError("the sign-on was started in another browser (binding cookie missing or wrong)")
        if not cfg.enabled or not code:
            raise SsoError("no code")
        doc = await self.discovery(cfg)
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": p.redirect_uri,
                "code_verifier": p.verifier, "client_id": cfg.client_id}
        auth = None
        methods = doc.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
        if cfg.client_secret:
            if "client_secret_basic" in methods:
                auth = httpx.BasicAuth(cfg.client_id, cfg.client_secret)
            else:
                data["client_secret"] = cfg.client_secret
        try:
            async with self._client() as c:
                r = await c.post(str(doc["token_endpoint"]), data=data, auth=auth, headers={"Accept": "application/json"})
                tokens = await self._json(r, "token endpoint")
                if r.status_code != 200:
                    raise SsoError(f"the token endpoint answered HTTP {r.status_code}: {str(tokens.get('error', ''))[:100]}")
                claims = self._check_id_token(str(tokens.get("id_token", "")), cfg, doc, p.nonce)
                access = tokens.get("access_token")
                if doc.get("userinfo_endpoint") and isinstance(access, str) and access:
                    u = await c.get(str(doc["userinfo_endpoint"]), headers={"Authorization": f"Bearer {access}",
                                                                           "Accept": "application/json"})
                    if u.status_code == 200:
                        info = await self._json(u, "userinfo")
                        if info.get("sub") == claims.get("sub"):
                            claims = {**info, **{k: v for k, v in claims.items() if v is not None}}
        except httpx.HTTPError as e:
            raise SsoError(f"can't reach the provider ({type(e).__name__})") from e
        return claims

    def _check_id_token(self, token: str, cfg: SsoSettings, doc: dict, nonce: str) -> dict:
        if not token:
            raise SsoError("the provider sent no ID token (is the 'openid' scope allowed?)")
        c = jwt_claims(token)
        now = self._clock()
        if str(c.get("iss", "")).rstrip("/") != str(doc["issuer"]).rstrip("/"):
            raise SsoError("ID token from another issuer")
        aud = c.get("aud")
        auds = aud if isinstance(aud, list) else [aud]
        if cfg.client_id not in auds or (len(auds) > 1 and c.get("azp") != cfg.client_id):
            raise SsoError("ID token for another client")
        try:
            exp, iat = float(c["exp"]), float(c.get("iat", now))
        except (KeyError, TypeError, ValueError) as e:
            raise SsoError("ID token without a valid expiry") from e
        if exp < now - 60 or iat > now + 300:
            raise SsoError("ID token expired or not yet valid (check the clocks)")
        if not isinstance(c.get("nonce"), str) or not hmac.compare_digest(c["nonce"], nonce):
            raise SsoError("ID token nonce mismatch")
        if not c.get("sub"):
            raise SsoError("ID token without a subject")
        return c

    def allowed(self, claims: dict) -> str | None:
        """Who signed in (for the log) if the settings let them in, else None."""
        cfg = self._cfg()
        users = {u.lower() for u in cfg.allowed_users}
        names = [claims.get("preferred_username"), claims.get("nickname")]
        if claims.get("email_verified") is not False:
            names.append(claims.get("email"))
        for n in names:
            if isinstance(n, str) and n and n.lower() in users:
                return n
        groups = claims.get("groups")
        wanted = {g.lower() for g in cfg.allowed_groups}
        if isinstance(groups, list):
            for g in groups:
                if isinstance(g, str) and g.lower() in wanted:
                    return f"{claims.get('preferred_username') or claims.get('sub')} (group {g})"
        return None

    @staticmethod
    def who(claims: dict) -> str:
        return str(claims.get("preferred_username") or claims.get("email") or claims.get("sub") or "?")[:120]
