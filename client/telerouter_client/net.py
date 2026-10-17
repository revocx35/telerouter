"""Enrollment and the WebSocket session with the telerouter server."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import platform
import ssl
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from . import __version__
from .audio import Player

log = logging.getLogger("telerouter_client")


def _unverified() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def pinned_context(ca_pem: str) -> ssl.SSLContext:
    """Trust exactly telerouter's own CA (no system CAs); the CA is pinned, so the host name doesn't matter."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cadata=ca_pem)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def fetch_ca(server: str, ca_sha256: str) -> str:
    """Download the server's CA and check it against the fingerprint from the install command."""
    with urllib.request.urlopen(server.rstrip("/") + "/ca.pem", context=_unverified(), timeout=15) as r:
        pem = r.read(65536).decode()
    der = ssl.PEM_cert_to_DER_cert(pem)
    got = hashlib.sha256(der).hexdigest()
    if got.lower() != ca_sha256.lower().replace(":", ""):
        raise SystemExit(f"certificate fingerprint mismatch: expected {ca_sha256}, got {got}. Not enrolling.")
    return pem


def enroll(server: str, ca_sha256: str, code: str) -> dict:
    server = server.rstrip("/")
    u = urlsplit(server)
    if u.scheme != "https" or not u.hostname:
        raise SystemExit("--server must be https://host:port")
    ca = fetch_ca(server, ca_sha256)
    body = json.dumps({"code": code, "platform": f"{platform.system()} {platform.machine()}"}).encode()
    req = urllib.request.Request(server + "/api/client/enroll", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, context=pinned_context(ca), timeout=15) as r:
            data = json.loads(r.read(65536))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read()).get("detail", "")
        except Exception:
            pass
        raise SystemExit(f"enrollment failed: {e.code} {detail}") from e
    return {"server": server, "ca_pem": ca, "token": data["token"], "client_id": data["client_id"],
            "name": data.get("name", "")}


def _connect(url: str, token: str, ctx: ssl.SSLContext):
    headers = {"Authorization": f"Bearer {token}"}
    try:
        from websockets.asyncio.client import connect        # websockets >= 13
        return connect(url, ssl=ctx, additional_headers=headers, max_size=256 * 1024, ping_interval=20,
                       ping_timeout=20, open_timeout=15, compression=None)
    except ImportError:                                      # pragma: no cover - old websockets
        import websockets
        return websockets.connect(url, ssl=ctx, extra_headers=headers, max_size=256 * 1024, ping_interval=20,
                                  ping_timeout=20, open_timeout=15, compression=None)


class Session:
    """Keeps the connection up; mic blocks go up, playback audio comes down."""

    def __init__(self, cfg: dict, player: Player, aec: bool):
        self.cfg = cfg
        self.player = player
        self.aec = aec
        self.loop: asyncio.AbstractEventLoop | None = None
        self.mic_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        self.connected = False
        self.state = "offline"

    def mic(self, block: bytes) -> None:
        """Called from the audio thread."""
        loop = self.loop
        if loop is None or not self.connected:
            return
        loop.call_soon_threadsafe(self._put, block)

    def _put(self, block: bytes) -> None:
        if self.mic_q.full():
            try:
                self.mic_q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self.mic_q.put_nowait(block)

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        server = self.cfg["server"].rstrip("/")
        url = "wss://" + server.split("://", 1)[1] + "/ws/client"
        ctx = pinned_context(self.cfg["ca_pem"])
        backoff = 1.0
        while True:
            t0 = time.monotonic()
            try:
                async with _connect(url, self.cfg["token"], ctx) as ws:
                    log.info("connected to %s", server)
                    await ws.send(json.dumps({"type": "hello", "version": __version__, "aec": self.aec,
                                              "platform": f"{platform.system()} {platform.machine()}"}))
                    while not self.mic_q.empty():
                        self.mic_q.get_nowait()
                    self.connected = True
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                msg = str(e) or e.__class__.__name__
                if "1008" in msg or "403" in msg or "401" in msg:
                    log.error("the server refused this client (removed or token revoked?): %s", msg)
                    backoff = 60.0
                else:
                    log.warning("connection: %s", msg)
            finally:
                self.connected = False
                self.player.flush()
            if time.monotonic() - t0 > 30:
                backoff = 1.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _session(self, ws) -> None:
        async def up():
            while True:
                block = await self.mic_q.get()
                await ws.send(block)

        sender = asyncio.create_task(up())
        try:
            async for msg in ws:
                if isinstance(msg, (bytes, bytearray)):
                    self.player.feed(bytes(msg))
                    continue
                try:
                    obj = json.loads(msg)
                except ValueError:
                    continue
                t = obj.get("type")
                if t == "flush":
                    self.player.flush()
                elif t == "state":
                    self.state = str(obj.get("state"))
                    log.info("state: %s", self.state)
                elif t == "config":
                    vol = obj.get("volume")
                    if isinstance(vol, (int, float)):
                        self.player.volume = 1.0       # the server applies the volume; keep local at 100 %
                elif t == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
        finally:
            sender.cancel()
