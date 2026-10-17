"""Client for the integration API v1 of wa2sip and cam2sip (see docs/integration-api.md).

  GET  /api/integration/v1/info      -> {"app", "version", "sip_port"}
  GET  /api/integration/v1/targets   -> {"targets": [{"id", "name", "kind", "number"?, "account"?}]}
  POST /api/integration/v1/calls     {"target": id} | {"number": "...", "account": id}
                                     -> {"sip_uri": "sip:<one-time user>@host:port", "expires_in": 30}
  PUT  /api/integration/v1/inbound   {"sip_uri": "...", "enabled": bool}   (wa2sip: ring us for WhatsApp calls)

Auth: `Authorization: Bearer <integration token>` made in that app's UI. TLS: the app's self-signed
certificate is pinned (the admin confirms its fingerprint once), or a public CA when none is pinned.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import ssl
from urllib.parse import urlsplit

import httpx

from ..models import Integration

log = logging.getLogger("telerouter.integration")

TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class IntegrationError(Exception):
    pass


def _ssl_context(pem: str) -> ssl.SSLContext | bool:
    if not pem:
        return True                                   # system CAs (e.g. behind a reverse proxy with a real cert)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False                       # the pinned certificate itself is the trust anchor
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    ctx.load_verify_locations(cadata=pem)
    return ctx


async def fetch_certificate(url: str) -> tuple[str, str]:
    """(PEM, sha256 hex) of the server's certificate, without verifying it (shown to the admin first)."""
    u = urlsplit(url)
    host, port = u.hostname or "", u.port or 443
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ctx, server_hostname=host or None), 8)
    try:
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    finally:
        writer.close()
    if not der:
        raise IntegrationError("no certificate received")
    return ssl.DER_cert_to_PEM_cert(der), hashlib.sha256(der).hexdigest()


class IntegrationClient:
    def __init__(self, integ: Integration, cert_pem: str):
        self.integ = integ
        self._http = httpx.AsyncClient(base_url=integ.url, verify=_ssl_context(cert_pem), timeout=TIMEOUT,
                                       follow_redirects=False,
                                       headers={"Authorization": f"Bearer {integ.token}",
                                                "User-Agent": "telerouter"})

    @property
    def host(self) -> str:
        return urlsplit(self.integ.url).hostname or ""

    async def close(self) -> None:
        await self._http.aclose()

    async def _req(self, method: str, path: str, json: dict | None = None) -> dict:
        try:
            r = await self._http.request(method, f"/api/integration/v1{path}", json=json)
        except httpx.HTTPError as e:
            raise IntegrationError(f"{self.integ.kind} unreachable: {e.__class__.__name__}: {e}") from e
        if r.status_code in (401, 403):
            raise IntegrationError(f"{self.integ.kind} refused the token ({r.status_code})")
        if r.status_code == 404:
            raise IntegrationError(f"{self.integ.kind} has no integration API (update it)")
        if r.status_code >= 400:
            detail = ""
            try:
                detail = r.json().get("detail", "")
            except ValueError:
                pass
            raise IntegrationError(f"{self.integ.kind}: {r.status_code} {detail}".strip())
        if len(r.content) > 4 << 20:
            raise IntegrationError("response too large")
        try:
            data = r.json()
        except ValueError as e:
            raise IntegrationError("invalid response") from e
        if not isinstance(data, dict):
            raise IntegrationError("invalid response")
        return data

    async def info(self) -> dict:
        return await self._req("GET", "/info")

    async def targets(self) -> list[dict]:
        data = await self._req("GET", "/targets")
        out = []
        for t in data.get("targets") or []:
            if not isinstance(t, dict) or not t.get("id") or not t.get("name"):
                continue
            out.append({"id": str(t["id"])[:64], "name": str(t["name"])[:64],
                        "kind": "camera" if t.get("kind") == "camera" else "whatsapp",
                        "number": re.sub(r"[^0-9+]", "", str(t.get("number") or ""))[:32],
                        "account": str(t.get("account") or "")[:64]})
        return out

    async def prepare_call(self, target: str = "", number: str = "", account: str = "") -> str:
        body = {"target": target} if target else {"number": number, "account": account}
        data = await self._req("POST", "/calls", body)
        uri = str(data.get("sip_uri") or "")
        m = re.match(r"^sip:([A-Za-z0-9._~-]{8,128})@([A-Za-z0-9.\-\[\]:]+?)(?::(\d+))?$", uri)
        if not m:
            raise IntegrationError("invalid SIP address from the integration")
        # the call goes to the host we already talk to (never somewhere the response points us)
        port = m.group(3) or "5060"
        return f"sip:{m.group(1)}@{self.host}:{port}"

    async def set_inbound(self, sip_uri: str, enabled: bool) -> None:
        await self._req("PUT", "/inbound", {"sip_uri": sip_uri, "enabled": enabled})
