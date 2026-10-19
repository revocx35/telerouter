"""Web UI + API (HTTPS), speaker WebSocket, client installer, MCP endpoint."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import io
import ipaddress
import json
import logging
import re
import secrets
import tarfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, ValidationError
from starlette.middleware.base import BaseHTTPMiddleware

from .. import security
from ..agents import ssh as sshmod
from ..engine import Engine
from ..logbuffer import LogBuffer
from ..models import (SECRET_FIELDS, Admin, Agent, CallSettings, CastDevice, Client, Contact, Enrollment, Integration,
                      Machine, McpToken, Pbx, QuietHours, ScheduledTask, SecuritySettings, Speaker, VoiceSettings)
from ..router.calls import CallError
from ..router.integrations import IntegrationError, fetch_certificate
from ..settings import Settings
from ..store import Store, write_private
from ..tls import TlsFiles
from ..voice import cast as castmod
from ..voice.client_conn import ClientConn
from ..voice.speaker import VirtualSpeaker

log = logging.getLogger("telerouter.web")

STATIC = Path(__file__).parent / "static"
CLIENT_SRC = Path(__file__).resolve().parents[2] / "client"
MAX_WS_BINARY = 64 * 1024
MAX_WS_TEXT = 4096

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


# -- request bodies ---------------------------------------------------------------------------------------
class SetupBody(BaseModel):
    setup_code: str = Field(max_length=64)
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._@-]+$")
    password: str = Field(max_length=256)


class LoginBody(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class CodeBody(BaseModel):
    code: str = Field(max_length=16)


class PasswordChange(BaseModel):
    current: str = Field(max_length=256)
    new: str = Field(max_length=256)


class EnrollStart(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    room: str = Field(default="", max_length=64)


class EnrollBody(BaseModel):
    code: str = Field(max_length=16)
    name: str = Field(default="", max_length=64)
    platform: str = Field(default="", max_length=64)


class TextBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class CallBody(BaseModel):
    speaker_id: str = Field(max_length=16)


class HostKeyBody(BaseModel):
    host: str = Field(max_length=253)
    port: int = Field(default=22, ge=1, le=65535)


class UrlBody(BaseModel):
    url: str = Field(max_length=300)


# -- helpers --------------------------------------------------------------------------------------------------
def redact(obj: Any) -> Any:
    """Secrets become "" plus "<field>_set": true. Never send a secret back to the browser."""
    if isinstance(obj, BaseModel):
        obj = obj.model_dump()
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in SECRET_FIELDS:
                out[k] = ""
                out[f"{k}_set"] = bool(v)
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


def merge(model_cls, existing: BaseModel | None, payload: dict, protected: set[str] = frozenset()):
    """Validate an update; an empty secret keeps the stored one; `protected` fields can't be set."""
    data = existing.model_dump() if existing else {}
    for k, v in payload.items():
        if k.endswith("_set") or k in protected:
            continue
        if k in SECRET_FIELDS and v in ("", None) and existing is not None:
            continue
        data[k] = v
    try:
        return model_cls.model_validate(data)
    except ValidationError as e:
        msg = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())
        raise HTTPException(422, msg) from e


def find(items: list, oid: str):
    for x in items:
        if x.id == oid:
            return x
    raise HTTPException(404, "not found")


def replace(items: list, item) -> None:
    for i, x in enumerate(items):
        if x.id == item.id:
            items[i] = item
            return


def _networks(spec: str) -> list:
    nets = []
    for part in spec.split(","):
        part = part.strip()
        if part == "private":
            nets += [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                                                       "127.0.0.0/8", "::1/128", "fc00::/7")]
        elif part:
            nets.append(ipaddress.ip_network(part, strict=False))
    return nets


def cross_site(headers) -> bool:
    """A browser request started by another site (Fetch Metadata)."""
    return headers.get("sec-fetch-site", "") in ("cross-site", "same-site")


def setup_logging(settings: Settings, buffer: LogBuffer) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(type(h) is logging.StreamHandler for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(sh)
    buffer.setLevel(logging.INFO)
    root.addHandler(buffer)
    logging.getLogger("telerouter").setLevel(settings.log_level)
    for noisy in ("httpx", "uvicorn.access", "asyncssh"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def client_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(CLIENT_SRC.rglob("*")):
            rel = p.relative_to(CLIENT_SRC)
            if any(part in ("__pycache__", ".pytest_cache") or part.endswith(".egg-info") for part in rel.parts):
                continue
            if p.is_file() and p.name != "install.sh":
                tar.add(p, arcname=f"telerouter-client/{rel}")
    return buf.getvalue()


# =============================================================================================================
def create_app(settings: Settings, tls: TlsFiles, logbuf: LogBuffer | None = None,
               store: Store | None = None, engine: Engine | None = None) -> tuple[FastAPI, FastAPI]:
    """Returns (public HTTPS app, loopback MCP app)."""
    logbuf = logbuf or LogBuffer()
    store = store or Store(settings.data_dir)
    engine = engine or Engine(settings, store)
    cfg = store.config
    trusted = _networks(settings.trusted_proxies)
    throttle = security.LoginThrottle()
    totp_replay = security.TotpReplay()
    sec = cfg.security
    sessions = security.SessionStore(sec.session_idle_minutes * 60, sec.session_absolute_hours * 3600)
    hash_slots = asyncio.Semaphore(2)
    tarball = client_tarball() if CLIENT_SRC.exists() else b""
    setup_code_path = Path(settings.data_dir) / "setup-code"

    def setup_code() -> str:
        if settings.setup_code:
            return settings.setup_code
        if not setup_code_path.exists():
            write_private(setup_code_path, security.new_enrollment_code())
        return setup_code_path.read_text().strip()

    if cfg.admin is None:
        log.warning("first start: open the web UI and create the admin account with setup code %s", setup_code())

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log.info("telerouter %s starting (https :%d, sip udp :%d, rtp %d-%d)", settings.version,
                 settings.https_port, settings.sip_port, settings.rtp_port_min, settings.rtp_port_max)
        await engine.start()
        yield
        await engine.stop()

    app = FastAPI(title="telerouter", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.state.store = store
    app.state.sessions = sessions
    app.state.throttle = throttle

    # -- client IP / security headers / auth ---------------------------------------------------------------
    def client_ip(request: Request | WebSocket) -> str:
        peer = request.client.host if request.client else "unknown"
        try:
            ip = ipaddress.ip_address(peer)
        except ValueError:
            return peer
        if not any(ip in n for n in trusted):
            return peer
        for hop in reversed([h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]):
            try:
                if not any(ipaddress.ip_address(hop) in n for n in trusted):
                    return hop
            except ValueError:
                return peer
        return peer

    PUBLIC = {"/api/health", "/api/session", "/api/setup", "/api/login", "/api/login/totp", "/api/logout",
              "/api/client/enroll"}

    class Guard(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            path = request.url.path
            if path.startswith("/api/"):
                if path not in PUBLIC:
                    s = sessions.get(request.cookies.get(security.COOKIE))
                    if not s or s.pending_totp:
                        return JSONResponse({"detail": "login required"}, status_code=401)
                    request.state.session = s
                    if request.method not in ("GET", "HEAD", "OPTIONS"):
                        sent = request.headers.get("x-csrf-token", "")
                        if cross_site(request.headers) or not hmac.compare_digest(sent, s.csrf):
                            return JSONResponse({"detail": "CSRF check failed"}, status_code=403)
                elif request.method == "POST" and path != "/api/client/enroll" and cross_site(request.headers):
                    return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
            resp = await call_next(request)
            h = resp.headers
            h["Content-Security-Policy"] = CSP
            h["X-Frame-Options"] = "DENY"
            h["X-Content-Type-Options"] = "nosniff"
            h["Referrer-Policy"] = "no-referrer"
            h["Strict-Transport-Security"] = "max-age=31536000"
            h["Cross-Origin-Opener-Policy"] = "same-origin"
            h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
            if path.startswith("/api/") or path == "/":
                h["Cache-Control"] = "no-store"
            return resp

    app.add_middleware(Guard)

    async def check_password(password: str, stored: str | None) -> bool:
        async with hash_slots:
            return await asyncio.to_thread(security.verify_password, password, stored)

    def charge(*keys: str) -> None:
        try:
            throttle.begin(*keys)
        except security.Locked as e:
            raise HTTPException(429, "too many attempts, try again later",
                                headers={"Retry-After": str(int(e.retry_after) + 1)}) from e

    def start_session(request: Request, response: Response, username: str, pending: bool = False) -> security.Session:
        sessions.delete(request.cookies.get(security.COOKIE))        # rotate on login
        sid, s = sessions.create(username, pending_totp=pending)
        response.set_cookie(security.COOKIE, sid, httponly=True, secure=True, samesite="strict", path="/")
        return s

    # -- auth endpoints ------------------------------------------------------------------------------------------
    @app.get("/api/health")
    async def health():
        return {"ok": True}

    @app.get("/api/session")
    async def session(request: Request):
        s = sessions.get(request.cookies.get(security.COOKIE))
        return {"setup_needed": cfg.admin is None, "logged_in": bool(s and not s.pending_totp),
                "totp_needed": bool(s and s.pending_totp), "username": s.username if s else "",
                "csrf": s.csrf if s and not s.pending_totp else "", "version": settings.version}

    @app.post("/api/setup")
    async def setup(body: SetupBody, request: Request, response: Response):
        if cfg.admin is not None:
            raise HTTPException(409, "already set up")
        ip = client_ip(request)
        charge(f"ip:{ip}", "setup")
        if not hmac.compare_digest(body.setup_code.strip().upper().encode(), setup_code().upper().encode()):
            log.warning("wrong setup code from %s", ip)
            raise HTTPException(401, "wrong setup code")
        problem = security.password_problem(body.password, body.username)
        if problem:
            throttle.success(f"ip:{ip}", "setup")
            raise HTTPException(422, problem)
        throttle.success(f"ip:{ip}", "setup")
        cfg.admin = Admin(username=body.username, password_hash=await asyncio.to_thread(security.hash_password,
                                                                                        body.password))
        store.save()
        setup_code_path.unlink(missing_ok=True)
        log.info("admin account '%s' created from %s", body.username, ip)
        start_session(request, response, body.username)
        return {"ok": True}

    @app.post("/api/login")
    async def login(body: LoginBody, request: Request, response: Response):
        ip = client_ip(request)
        user = body.username.strip().lower()
        charge(f"ip:{ip}", f"user:{user}")
        admin = cfg.admin
        ok = await check_password(body.password, admin.password_hash if admin else None)
        if not ok or not admin or not hmac.compare_digest(user.encode(), admin.username.lower().encode()):
            log.warning("failed login for '%s' from %s", body.username[:64], ip)
            raise HTTPException(401, "wrong user name or password")
        throttle.success(f"ip:{ip}", f"user:{user}")
        pending = admin.totp_enabled
        start_session(request, response, admin.username, pending=pending)
        if not pending:
            log.info("admin login from %s", ip)
        return {"ok": True, "totp_needed": pending}

    @app.post("/api/login/totp")
    async def login_totp(body: CodeBody, request: Request, response: Response):
        s = sessions.get(request.cookies.get(security.COOKIE))
        if not s or not s.pending_totp or not cfg.admin:
            raise HTTPException(401, "log in first")
        ip = client_ip(request)
        charge(f"ip:{ip}", "totp")
        if not totp_replay.verify(cfg.admin.totp_secret, body.code):
            log.warning("wrong 2FA code from %s", ip)
            raise HTTPException(401, "wrong code")
        throttle.success(f"ip:{ip}", "totp")
        start_session(request, response, s.username)
        log.info("admin login (2FA) from %s", ip)
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(request: Request, response: Response):
        sessions.delete(request.cookies.get(security.COOKIE))
        response.delete_cookie(security.COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return {"ok": True}

    @app.post("/api/account/password")
    async def change_password(body: PasswordChange, request: Request, response: Response):
        admin = cfg.admin
        assert admin
        ip = client_ip(request)
        charge(f"ip:{ip}", f"user:{admin.username.lower()}")
        if not await check_password(body.current, admin.password_hash):
            raise HTTPException(401, "current password is wrong")
        throttle.success(f"ip:{ip}", f"user:{admin.username.lower()}")
        problem = security.password_problem(body.new, admin.username)
        if problem:
            raise HTTPException(422, problem)
        admin.password_hash = await asyncio.to_thread(security.hash_password, body.new)
        store.save()
        sessions.clear()                       # everyone else is logged out
        start_session(request, response, admin.username)
        log.info("admin password changed from %s", ip)
        return {"ok": True}

    @app.post("/api/account/totp/setup")
    async def totp_setup(request: Request):
        admin = cfg.admin
        assert admin
        if admin.totp_enabled:
            raise HTTPException(409, "2FA is already on")
        secret = security.new_totp_secret()
        request.state.session.totp_candidate = secret     # type: ignore[attr-defined]
        uri = security.totp_uri(secret, admin.username)
        return {"secret": secret, "uri": uri, "qr": _qr_data_url(uri)}

    @app.post("/api/account/totp/enable")
    async def totp_enable(body: CodeBody, request: Request):
        admin = cfg.admin
        assert admin
        secret = getattr(request.state.session, "totp_candidate", "")
        if not secret or not totp_replay.verify(secret, body.code):
            raise HTTPException(401, "wrong code - check the time on your phone")
        admin.totp_secret, admin.totp_enabled = secret, True
        store.save()
        log.info("2FA enabled for %s", admin.username)
        return {"ok": True}

    @app.post("/api/account/totp/disable")
    async def totp_disable(body: LoginBody, request: Request):
        admin = cfg.admin
        assert admin
        ip = client_ip(request)
        charge(f"ip:{ip}", f"user:{admin.username.lower()}")
        if not await check_password(body.password, admin.password_hash):
            raise HTTPException(401, "wrong password")
        throttle.success(f"ip:{ip}", f"user:{admin.username.lower()}")
        admin.totp_secret, admin.totp_enabled = "", False
        store.save()
        log.info("2FA disabled for %s", admin.username)
        return {"ok": True}

    @app.get("/api/account")
    async def account():
        return {"username": cfg.admin.username if cfg.admin else "", "totp_enabled": bool(cfg.admin and cfg.admin.totp_enabled)}

    # -- status / logs / settings ---------------------------------------------------------------------------
    @app.get("/api/status")
    async def status():
        return engine.status()

    @app.get("/api/logs")
    async def logs(after: int = 0):
        return {"records": logbuf.since(after)}

    @app.get("/api/history")
    async def history():
        return {"history": list(reversed(store.history[-200:]))}

    @app.get("/api/settings")
    async def get_settings():
        return {"voice": cfg.voice.model_dump(), "calls": cfg.calls.model_dump(),
                "security": cfg.security.model_dump(), "default_agent_id": cfg.default_agent_id,
                "quiet": redact(cfg.quiet),
                "sip_port": settings.sip_port}

    @app.put("/api/settings")
    async def put_settings(payload: dict):
        if "voice" in payload:
            cfg.voice = merge(VoiceSettings, cfg.voice, payload["voice"])
        if "calls" in payload:
            cfg.calls = merge(CallSettings, cfg.calls, payload["calls"])
        if "quiet" in payload:
            cfg.quiet = merge(QuietHours, cfg.quiet, payload["quiet"])
        if "security" in payload:
            cfg.security = merge(SecuritySettings, cfg.security, payload["security"])
            sessions.idle_s = cfg.security.session_idle_minutes * 60
            sessions.absolute_s = cfg.security.session_absolute_hours * 3600
        if "default_agent_id" in payload:
            aid = str(payload["default_agent_id"] or "")
            if aid and not store.agent(aid):
                raise HTTPException(422, "unknown agent")
            cfg.default_agent_id = aid
        store.save()
        await engine.apply_config()
        return await get_settings()

    @app.get("/api/quiet/status")
    async def quiet_status():
        return await engine.quiet.status()

    @app.get("/api/quiet/sensors")
    async def quiet_sensors():
        try:
            return {"sensors": await engine.quiet.sensors()}
        except Exception as e:
            raise HTTPException(502, f"Home Assistant: {e}") from e

    @app.get("/api/speech")
    async def speech_info():
        h, wake, voices = await asyncio.gather(engine.speech.health(), engine.speech.wake_models(),
                                               engine.speech.voices())
        return {"health": h, "wake_models": wake, "voices": voices}

    @app.post("/api/speech/preview")
    async def speech_preview(body: TextBody):
        from ..media import pcm as pcmmod
        try:
            audio = await engine.speech.synthesize_all(body.text, cfg.voice.tts_voice)
        except Exception as e:
            raise HTTPException(503, f"text-to-speech failed: {e}") from e
        return Response(pcmmod.to_wav(audio), media_type="audio/wav")

    # -- speaker devices (clients) ------------------------------------------------------------------------------
    def install_command(request: Request, code: str) -> dict:
        host = request.headers.get("host", f"localhost:{settings.https_port}")
        server = f"https://{host}"
        pin = f"sha256//{tls.server_spki}"
        cmd = (f"curl -fsSk --pinnedpubkey '{pin}' {server}/client/install.sh | "
               f"sh -s -- --server {server} --ca-sha256 {tls.ca_sha256} --code {code}")
        return {"server": server, "command": cmd, "ca_sha256": tls.ca_sha256, "spki_pin": pin}

    @app.get("/api/clients")
    async def list_clients():
        now = time.time()
        return {"clients": redact(cfg.clients),
                "enrollments": [{"id": e.id, "name": e.name, "expires": e.expires} for e in cfg.enrollments
                                if e.expires > now]}

    @app.post("/api/clients/enroll")
    async def start_enroll(body: EnrollStart, request: Request):
        now = time.time()
        cfg.enrollments = [e for e in cfg.enrollments if e.expires > now][-10:]
        code = security.new_enrollment_code()
        try:
            name = Client(name=body.name).name
        except ValidationError as e:
            raise HTTPException(422, "invalid name") from e
        cfg.enrollments.append(Enrollment(name=name + (f"|{body.room}" if body.room else ""),
                                          code_hash=security.token_hash(code.upper()), expires=now + 900))
        store.save()
        return {"code": code, "expires": now + 900, **install_command(request, code)}

    @app.post("/api/client/enroll")
    async def enroll(body: EnrollBody, request: Request):
        ip = client_ip(request)
        charge(f"ip:{ip}", "enroll")
        now = time.time()
        h = security.token_hash(body.code.strip().upper())
        match = next((e for e in cfg.enrollments if e.expires > now and hmac.compare_digest(e.code_hash, h)), None)
        if not match:
            log.warning("invalid speaker enrollment code from %s", ip)
            raise HTTPException(401, "invalid or expired code")
        throttle.success(f"ip:{ip}", "enroll")
        cfg.enrollments.remove(match)
        name, _, room = match.name.partition("|")
        if len(cfg.clients) >= 100:
            raise HTTPException(409, "too many speakers")
        token = security.new_token("trc_")
        c = Client(name=name, room=room, token_hash=security.token_hash(token), platform=body.platform[:64])
        cfg.clients.append(c)
        if not store.speaker(c.id) and len(cfg.speakers) < 100:     # its own speaker: its mic and loudspeaker
            cfg.speakers.append(Speaker(id=c.id, name=name, room=room, mic=c.id, output=f"client:{c.id}"))
        store.save()
        log.info("speaker '%s' enrolled from %s (%s)", c.name, ip, c.platform)
        return {"client_id": c.id, "name": c.name, "token": token}

    @app.put("/api/clients/{cid}")
    async def update_client(cid: str, payload: dict):
        c = find(cfg.clients, cid)
        new = merge(Client, c, payload, protected={"id", "token_hash", "created", "last_seen", "platform",
                                                    "client_version"})
        replace(cfg.clients, new)
        store.save()
        conn = engine.clients.get(cid)
        if conn:
            conn.client = new
            if not new.enabled:
                await engine.detach_client(conn)
        return redact(new)

    @app.delete("/api/clients/{cid}")
    async def delete_client(cid: str):
        c = find(cfg.clients, cid)
        cfg.clients.remove(c)
        drop_device_refs(mic=cid, output=f"client:{cid}")
        store.save()
        conn = engine.clients.get(cid)
        if conn:
            await engine.detach_client(conn)
        engine.sync_speakers()
        log.info("speaker device '%s' removed (its token is revoked)", c.name)
        return {"ok": True}

    def drop_device_refs(mic: str = "", output: str = "") -> None:
        """A device is gone: speakers lose it; a speaker left with neither mic nor output goes too."""
        for sp in list(cfg.speakers):
            if mic and sp.mic == mic:
                sp.mic = ""
            if output and sp.output == output:
                sp.output = ""
            if not sp.mic and not sp.output:
                cfg.speakers.remove(sp)

    # -- Google Cast speakers (outputs) ----------------------------------------------------------------------------
    @app.get("/api/casts")
    async def list_casts():
        return {"casts": [c.model_dump() for c in cfg.casts], "port": settings.cast_port,
                "runtime": {cid: c.info() for cid, c in engine.casts.items()}}

    @app.get("/api/casts/discover")
    async def discover_casts():
        try:
            found = await castmod.discover()
        except OSError as e:
            raise HTTPException(502, f"mDNS discovery failed: {e}") from e
        known = {c.uuid for c in cfg.casts if c.uuid}
        return {"found": [{**vars(f), "added": f.uuid in known} for f in found]}

    @app.post("/api/casts")
    async def add_cast(payload: dict):
        if len(cfg.casts) >= 20:
            raise HTTPException(409, "too many Cast speakers")
        c = merge(CastDevice, None, payload, protected={"id"})
        if c.uuid and any(x.uuid == c.uuid for x in cfg.casts):
            raise HTTPException(409, f"{c.name} is already added")
        cfg.casts.append(c)
        store.save()
        engine.sync_speakers()
        return c.model_dump()

    @app.put("/api/casts/{cid}")
    async def update_cast(cid: str, payload: dict):
        c = find(cfg.casts, cid)
        new = merge(CastDevice, c, payload, protected={"id", "uuid"})
        replace(cfg.casts, new)
        store.save()
        engine.sync_speakers()
        return new.model_dump()

    @app.delete("/api/casts/{cid}")
    async def delete_cast(cid: str):
        c = find(cfg.casts, cid)
        cfg.casts.remove(c)
        drop_device_refs(output=f"cast:{cid}")
        store.save()
        engine.sync_speakers()
        return {"ok": True}

    @app.post("/api/casts/{cid}/test")
    async def test_cast(cid: str):
        c = find(cfg.casts, cid)
        out = engine.casts.get(cid)
        if not out:
            raise HTTPException(409, "the Cast speaker is disabled")
        try:
            audio = await engine.speech.synthesize_all(f"This is {c.name}.", cfg.voice.tts_voice)
        except Exception as e:
            raise HTTPException(503, f"text-to-speech failed: {e}") from e
        out.spill = None
        out.play(audio)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(out.drained(), 30)
        if out.state == "unreachable":
            raise HTTPException(502, f"{c.name}: {out.error}")
        return {"ok": True}

    # -- speakers (virtual: a mic device + an output device) ---------------------------------------------------------
    def check_speaker(sp: Speaker) -> list[str]:
        """Device references must exist; a microphone feeds one speaker (it moves). Returns who lost it."""
        if sp.mic and not store.client(sp.mic):
            raise HTTPException(422, "unknown microphone device")
        kind, _, oid = sp.output.partition(":")
        if sp.output and not (store.client(oid) if kind == "client" else store.cast(oid)):
            raise HTTPException(422, "unknown output device")
        if not sp.mic and not sp.output:
            raise HTTPException(422, "pick a microphone, an output or both")
        moved = []
        for other in cfg.speakers:
            if other.id != sp.id and sp.mic and other.mic == sp.mic:
                other.mic = ""
                moved.append(other.name)
        return moved

    @app.get("/api/speakers")
    async def list_speakers():
        return {"speakers": [sp.model_dump() for sp in cfg.speakers]}

    @app.post("/api/speakers")
    async def create_speaker(payload: dict):
        if len(cfg.speakers) >= 100:
            raise HTTPException(409, "too many speakers")
        sp = merge(Speaker, None, payload, protected={"id"})
        moved = check_speaker(sp)
        cfg.speakers.append(sp)
        store.save()
        engine.sync_speakers()
        return {**sp.model_dump(), "mic_moved_from": moved}

    @app.put("/api/speakers/{sid}")
    async def update_speaker(sid: str, payload: dict):
        sp = find(cfg.speakers, sid)
        new = merge(Speaker, sp, payload, protected={"id"})
        moved = check_speaker(new)
        replace(cfg.speakers, new)
        store.save()
        engine.sync_speakers()
        return {**new.model_dump(), "mic_moved_from": moved}

    @app.delete("/api/speakers/{sid}")
    async def delete_speaker(sid: str):
        sp = find(cfg.speakers, sid)
        cfg.speakers.remove(sp)
        store.save()
        engine.sync_speakers()
        return {"ok": True}

    def online(sid: str) -> VirtualSpeaker:
        conn = engine.speakers.get(sid)
        if not conn:
            raise HTTPException(409, "the speaker is offline")
        return conn

    @app.post("/api/speakers/{sid}/say")
    async def speaker_say(sid: str, body: TextBody):
        conn = online(sid)
        try:
            await engine.voice.announce(conn, body.text)
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e
        return {"ok": True}

    @app.post("/api/speakers/{sid}/talk")
    async def speaker_talk(sid: str):
        conn = online(sid)
        if not conn.has_mic:
            raise HTTPException(409, "the speaker has no microphone")
        if conn.state != "idle":
            raise HTTPException(409, f"the speaker is {conn.state}")
        engine.voice.start_turn(conn)
        return {"ok": True}

    # -- agents ------------------------------------------------------------------------------------------------
    @app.get("/api/agents")
    async def list_agents():
        return {"agents": [redact({**a.model_dump(), "host_key_fingerprint": sshmod.fingerprint(a.host_key) if a.host_key else ""})
                           for a in cfg.agents],
                "ssh_public_key": engine.ssh_keys.public, "default_agent_id": cfg.default_agent_id,
                "runtime": {aid: rt.info() for aid, rt in engine.agent_rts.items()}}

    @app.post("/api/agents/hostkey")
    async def agent_hostkey(body: HostKeyBody):
        try:
            key = await sshmod.fetch_host_key(body.host.strip(), body.port)
        except Exception as e:
            raise HTTPException(502, f"could not reach {body.host}:{body.port}: {e}") from e
        return {"host_key": key, "fingerprint": sshmod.fingerprint(key)}

    # -- machines ------------------------------------------------------------------------------------------
    @app.get("/api/machines")
    async def list_machines():
        return {"machines": [{**m.model_dump(), "host_key_fingerprint": sshmod.fingerprint(m.host_key) if m.host_key else "",
                              "runtime": engine.machine_rts[m.id].info() if m.id in engine.machine_rts else None,
                              "agents": [a.name for a in cfg.agents if a.machine_id == m.id]}
                             for m in cfg.machines],
                "ssh_public_key": engine.ssh_keys.public}

    @app.post("/api/machines")
    async def create_machine(payload: dict):
        m = merge(Machine, None, payload, protected={"id"})
        cfg.machines.append(m)
        store.save()
        await engine.apply_config()
        return m.model_dump()

    @app.put("/api/machines/{mid}")
    async def update_machine(mid: str, payload: dict):
        m = find(cfg.machines, mid)
        if payload.get("host", m.host) != m.host or int(payload.get("port", m.port)) != m.port:
            if payload.get("host_key", m.host_key) == m.host_key:
                payload["host_key"] = ""            # a new host needs its key confirmed again
        new = merge(Machine, m, payload, protected={"id"})
        replace(cfg.machines, new)
        store.save()
        await engine.apply_config()
        return new.model_dump()

    @app.delete("/api/machines/{mid}")
    async def delete_machine(mid: str):
        m = find(cfg.machines, mid)
        users = [a.name for a in cfg.agents if a.machine_id == mid]
        if users:
            raise HTTPException(409, f"agents run on it: {', '.join(users)}")
        tasks = [t.name for t in cfg.tasks if t.target != "agent" and t.machine_id == mid]
        if tasks:
            raise HTTPException(409, f"scheduled tasks start sessions on it: {', '.join(tasks)}")
        cfg.machines.remove(m)
        store.save()
        await engine.apply_config()
        return {"ok": True}

    @app.post("/api/machines/{mid}/refresh")
    async def refresh_machine(mid: str):
        find(cfg.machines, mid)
        rt = engine.machine_rts.get(mid)
        if not rt:
            raise HTTPException(409, "the machine is disabled or its host key is not confirmed")
        try:
            sessions = await rt.list_sessions()
        except Exception as e:
            raise HTTPException(502, str(e)) from e
        return {"sessions": sessions}

    # -- scheduled tasks ----------------------------------------------------------------------------------
    def check_task(t: ScheduledTask) -> None:
        if t.target == "agent":
            if t.agent_id and not store.agent(t.agent_id) or not t.agent_id and not store.default_agent():
                raise HTTPException(422, "pick the agent")
            return
        if not store.machine(t.machine_id):
            raise HTTPException(422, "pick the machine its sessions start on")
        if t.target == "task_session":
            for x in cfg.tasks:
                if x.id != t.id and x.target == "task_session" and x.machine_id == t.machine_id \
                        and x.tmux_name == t.tmux_name:
                    raise HTTPException(409, f"the task {x.name} already uses the session {t.tmux_name}")

    def task_out(t: ScheduledTask) -> dict:
        return {**t.model_dump(), "tmux_name": t.tmux_name, **engine.scheduler.info(t)}

    @app.get("/api/tasks")
    async def list_tasks():
        return {"tasks": [task_out(t) for t in cfg.tasks],
                "agents": [{"id": a.id, "name": a.name} for a in cfg.agents],
                "default_agent_id": getattr(store.default_agent(), "id", ""),
                "machines": [{"id": m.id, "name": m.name, "agent_kind": m.agent_kind,
                              "new_session_model": m.new_session_model,
                              "new_session_permission_mode": m.new_session_permission_mode} for m in cfg.machines],
                "timezone": cfg.quiet.timezone}

    @app.post("/api/tasks")
    async def create_task(payload: dict):
        t = merge(ScheduledTask, None, payload, protected={"id"})
        check_task(t)
        cfg.tasks.append(t)
        store.save()
        engine.scheduler.plan(t)
        return task_out(t)

    @app.put("/api/tasks/{tid}")
    async def update_task(tid: str, payload: dict):
        t = find(cfg.tasks, tid)
        new = merge(ScheduledTask, t, payload, protected={"id"})
        check_task(new)
        replace(cfg.tasks, new)
        store.save()
        engine.scheduler.plan(new)
        return task_out(new)

    @app.delete("/api/tasks/{tid}")
    async def delete_task(tid: str):
        t = find(cfg.tasks, tid)
        engine.scheduler.stop_run(tid)
        cfg.tasks.remove(t)
        store.save()
        engine.scheduler.forget(tid)
        return {"ok": True}

    @app.post("/api/tasks/{tid}/run")
    async def run_task(tid: str):
        from ..agents.runtime import AgentError
        t = find(cfg.tasks, tid)
        try:
            run = engine.scheduler.run_now(t)
        except AgentError as e:
            raise HTTPException(409, str(e)) from e
        return {"run": run}

    @app.post("/api/tasks/{tid}/stop")
    async def stop_task(tid: str):
        find(cfg.tasks, tid)
        if not engine.scheduler.stop_run(tid):
            raise HTTPException(409, "it is not running")
        return {"ok": True}

    AGENT_PROTECTED = {"id", "session_uuid", "mcp_token_id", "host", "port", "username", "host_key"}

    def check_machine(a: Agent) -> None:
        if not store.machine(a.machine_id):
            raise HTTPException(422, "pick the machine the agent runs on")

    @app.post("/api/agents")
    async def create_agent(payload: dict):
        a = merge(Agent, None, {k: v for k, v in payload.items() if k not in AGENT_PROTECTED})
        check_machine(a)
        if any(x.mcp_remote_port == a.mcp_remote_port and x.machine_id == a.machine_id for x in cfg.agents):
            a.mcp_remote_port = max(x.mcp_remote_port for x in cfg.agents) + 1
        cfg.agents.append(a)
        if not cfg.default_agent_id:
            cfg.default_agent_id = a.id
        store.save()
        await engine.apply_config()
        return redact(a)

    @app.put("/api/agents/{aid}")
    async def update_agent(aid: str, payload: dict):
        a = find(cfg.agents, aid)
        if isinstance(payload.get("mcp_servers"), list):
            old = {s.name: s for s in a.mcp_servers}
            for s in payload["mcp_servers"]:
                if isinstance(s, dict) and not s.get("token"):          # empty = keep the stored token
                    prev = old.get(str(s.get("name", "")).strip().lower())
                    s["token"] = prev.token if prev else ""
                if isinstance(s, dict):
                    s.pop("token_set", None)
        new = merge(Agent, a, payload, protected=AGENT_PROTECTED)
        check_machine(new)
        replace(cfg.agents, new)
        store.save()
        await engine.apply_config()
        return redact(new)

    @app.delete("/api/agents/{aid}")
    async def delete_agent(aid: str):
        a = find(cfg.agents, aid)
        rt = engine.agent_rts.get(aid)
        if rt and rt.conn:
            try:
                await rt._kill_session()
            except Exception:
                pass
        cfg.agents.remove(a)
        cfg.mcp_tokens = [t for t in cfg.mcp_tokens if t.agent_id != aid]
        if cfg.default_agent_id == aid:
            cfg.default_agent_id = cfg.agents[0].id if cfg.agents else ""
        store.save()
        await engine.apply_config()
        return {"ok": True}

    def runtime(aid: str):
        rt = engine.agent_rts.get(aid)
        if not rt:
            raise HTTPException(409, "the agent is disabled or its host key is not confirmed")
        return rt

    @app.post("/api/agents/{aid}/restart")
    async def agent_restart(aid: str):
        a = find(cfg.agents, aid)
        a.autostart = True
        store.save()
        runtime(aid).request_restart()
        return {"ok": True}

    @app.post("/api/agents/{aid}/stop")
    async def agent_stop(aid: str):
        find(cfg.agents, aid)
        rt = runtime(aid)
        try:
            await rt.stop_session()
        except Exception as e:
            raise HTTPException(502, str(e)) from e
        store.save()
        return {"ok": True}

    @app.post("/api/agents/{aid}/new-conversation")
    async def agent_new_conversation(aid: str):
        a = find(cfg.agents, aid)
        a.session_uuid = str(uuid.uuid4()) if a.kind == "claude" else ""
        store.save()
        runtime(aid).request_restart()
        return {"ok": True}

    @app.get("/api/agents/{aid}/screen")
    async def agent_screen(aid: str):
        return {"screen": await runtime(aid).screen()}

    @app.post("/api/agents/{aid}/ask")
    async def agent_ask(aid: str, body: TextBody):
        from ..agents.runtime import AgentError, Source
        try:
            reply = await runtime(aid).ask(body.text, Source("mcp", "telerouter web UI"))
        except AgentError as e:
            raise HTTPException(502, str(e)) from e
        return {"reply": reply}

    # -- contacts -----------------------------------------------------------------------------------------------
    @app.get("/api/contacts")
    async def list_contacts():
        names = {i.id: i.name or i.kind for i in cfg.integrations}
        names.update({m.id: m.name for m in cfg.machines})
        return {"contacts": [{**c.model_dump(), "source_name": names.get(c.source, "manual")} for c in cfg.contacts]}

    @app.post("/api/contacts")
    async def create_contact(payload: dict):
        payload = {**payload, "source": "manual", "source_ref": "", "kind": "pbx"}
        c = merge(Contact, None, payload, protected={"id", "missing"})
        if not c.number:
            raise HTTPException(422, "an extension number is required")
        cfg.contacts.append(c)
        store.save()
        return c.model_dump()

    CONTACT_EDITABLE = {"alias", "description", "enabled", "inbound", "inbound_client", "inbound_agent"}

    @app.put("/api/contacts")
    async def update_contacts(payload: dict):
        """Save many contacts at once: {"changes": [{"id", ...fields}]} (all or nothing)."""
        changes = payload.get("changes")
        if not isinstance(changes, list) or len(changes) > 2000:
            raise HTTPException(422, "changes must be a list")
        updated = []
        for ch in changes:
            if not isinstance(ch, dict):
                raise HTTPException(422, "invalid change")
            c = find(cfg.contacts, str(ch.get("id", "")))
            fields = {k: v for k, v in ch.items() if k in CONTACT_EDITABLE}
            if c.source == "manual":
                fields.update({k: v for k, v in ch.items() if k in ("name", "number")})
            updated.append(merge(Contact, c, fields, protected={"id", "source", "source_ref", "kind", "missing"}))
        for new in updated:
            replace(cfg.contacts, new)
        store.save()
        return {"saved": len(updated)}

    @app.put("/api/contacts/{cid}")
    async def update_contact(cid: str, payload: dict):
        c = find(cfg.contacts, cid)
        locked = {"id", "source", "source_ref", "kind", "missing"}
        if c.source != "manual":
            locked |= {"name", "number"}
        new = merge(Contact, c, payload, protected=locked)
        replace(cfg.contacts, new)
        store.save()
        return new.model_dump()

    @app.delete("/api/contacts/{cid}")
    async def delete_contact(cid: str):
        c = find(cfg.contacts, cid)
        if c.kind == "tmux" and not c.missing:
            raise HTTPException(409, "running tmux sessions can be hidden (untick 'visible'), not deleted")
        if c.source != "manual" and not c.missing:
            raise HTTPException(409, "synced contacts can be hidden (untick 'visible'), not deleted")
        cfg.contacts.remove(c)
        store.save()
        return {"ok": True}

    @app.post("/api/contacts/{cid}/call")
    async def call_contact(cid: str, body: CallBody):
        c = find(cfg.contacts, cid)
        conn = online(body.speaker_id)
        if c.kind == "tmux":
            try:
                conn.route = engine.route_for(c)
            except ValueError as e:
                raise HTTPException(409, str(e)) from e
            agent = store.agent_for_speaker(conn.cfg)
            back = agent.name if agent else "your assistant"
            asyncio.create_task(engine.voice.announce(conn, f"Connected to {c.label}. Say back to {back} to return."))
            return {"connected": c.label}
        try:
            s = engine.calls.call_to_speaker(c, conn)
        except CallError as e:
            raise HTTPException(409, str(e)) from e
        return {"call_id": s.id}

    # -- PBX ---------------------------------------------------------------------------------------------------------
    @app.get("/api/pbx")
    async def get_pbx():
        return redact(cfg.pbx)

    @app.put("/api/pbx")
    async def put_pbx(payload: dict):
        cfg.pbx = merge(Pbx, cfg.pbx, payload, protected={"contact_user"})
        store.save()
        await engine.apply_config()
        return redact(cfg.pbx)

    @app.post("/api/pbx/register")
    async def pbx_register():
        acc = engine.ua.accounts.get("pbx")
        if acc:
            acc.refresh_now()
        return {"ok": bool(acc)}

    # -- integrations ---------------------------------------------------------------------------------------------
    @app.get("/api/integrations")
    async def list_integrations():
        return {"integrations": redact(cfg.integrations)}

    @app.post("/api/integrations/certificate")
    async def integration_cert(body: UrlBody):
        try:
            url = Integration(kind="wa2sip", url=body.url).url
        except ValidationError as e:
            raise HTTPException(422, "use https://host:port") from e
        try:
            pem, fp = await fetch_certificate(url)
        except Exception as e:
            raise HTTPException(502, f"could not fetch the certificate: {e}") from e
        return {"pem": pem, "fingerprint": fp}

    @app.post("/api/integrations")
    async def create_integration(payload: dict):
        i = merge(Integration, None, payload, protected={"id", "last_sync", "last_error"})
        cfg.integrations.append(i)
        store.save()
        await engine.apply_config()
        return redact(i)

    @app.put("/api/integrations/{iid}")
    async def update_integration(iid: str, payload: dict):
        i = find(cfg.integrations, iid)
        new = merge(Integration, i, payload, protected={"id", "kind", "last_sync", "last_error"})
        if new.url != i.url and new.tls_cert == i.tls_cert:
            new.tls_cert, new.tls_fingerprint = "", ""          # a new address needs its certificate confirmed
        replace(cfg.integrations, new)
        store.save()
        await engine.apply_config()
        return redact(new)

    @app.delete("/api/integrations/{iid}")
    async def delete_integration(iid: str):
        i = find(cfg.integrations, iid)
        client = engine.integ_clients.get(iid)
        if client and i.kind == "wa2sip":
            try:
                await client.set_inbound("", False)
            except IntegrationError:
                pass
        cfg.integrations.remove(i)
        for c in cfg.contacts:
            if c.source == iid:
                c.missing = True
        store.save()
        await engine.apply_config()
        return {"ok": True}

    @app.post("/api/integrations/{iid}/sync")
    async def sync_integration(iid: str):
        i = find(cfg.integrations, iid)
        try:
            return await engine.sync_integration(i)
        except IntegrationError as e:
            raise HTTPException(502, str(e)) from e

    # -- calls -----------------------------------------------------------------------------------------------------
    @app.get("/api/calls")
    async def list_calls():
        return {"calls": engine.calls.info()}

    @app.post("/api/calls/{sid}/hangup")
    async def hangup(sid: str):
        s = engine.calls.get(sid)
        if not s:
            raise HTTPException(404, "no such call")
        await engine.calls.hangup(s, "hung up from the web UI")
        return {"ok": True}

    # -- MCP tokens ------------------------------------------------------------------------------------------------
    @app.get("/api/mcp-tokens")
    async def list_tokens(request: Request):
        host = request.headers.get("host", "")
        return {"tokens": redact(cfg.mcp_tokens), "url": f"https://{host}/mcp", "ca_sha256": tls.ca_sha256,
                "spki_pin": f"sha256//{tls.server_spki}"}

    @app.post("/api/mcp-tokens")
    async def create_token(payload: dict):
        t = merge(McpToken, None, payload, protected={"id", "token_hash", "created", "last_used", "agent_id"})
        token = security.new_token("trm_")
        t.token_hash = security.token_hash(token)
        cfg.mcp_tokens.append(t)
        store.save()
        log.info("MCP token '%s' created", t.name)
        return {"token": token, **redact(t)}

    @app.put("/api/mcp-tokens/{tid}")
    async def update_token(tid: str, payload: dict):
        t = find(cfg.mcp_tokens, tid)
        new = merge(McpToken, t, payload, protected={"id", "token_hash", "created", "last_used", "agent_id"})
        replace(cfg.mcp_tokens, new)
        store.save()
        return redact(new)

    @app.delete("/api/mcp-tokens/{tid}")
    async def delete_token(tid: str):
        t = find(cfg.mcp_tokens, tid)
        if t.agent_id:
            raise HTTPException(409, "this token belongs to an agent; it is replaced whenever the agent restarts")
        cfg.mcp_tokens.remove(t)
        store.save()
        log.info("MCP token '%s' revoked", t.name)
        return {"ok": True}

    # -- MCP --------------------------------------------------------------------------------------------------------
    @app.api_route("/mcp", methods=["GET", "POST", "DELETE"])
    async def mcp_endpoint(request: Request):
        return await engine.mcp.handle(request)

    # -- client files ------------------------------------------------------------------------------------------------
    @app.get("/ca.pem")
    async def ca_pem():
        return Response(tls.ca_pem, media_type="application/x-pem-file")

    @app.get("/client/install.sh")
    async def install_sh():
        script = (CLIENT_SRC / "install.sh").read_text()
        script = script.replace("@SPKI_PIN@", f"sha256//{tls.server_spki}").replace("@CA_SHA256@", tls.ca_sha256)
        return PlainTextResponse(script, media_type="text/x-shellscript")

    @app.get("/client/telerouter-client.tar.gz")
    async def client_pkg():
        return Response(tarball, media_type="application/gzip")

    # -- speaker WebSocket ------------------------------------------------------------------------------------------
    @app.websocket("/ws/client")
    async def ws_client(ws: WebSocket):
        ip = client_ip(ws)
        token = security.bearer(ws.headers)
        try:
            throttle.check(f"ip:{ip}")
        except security.Locked:
            await ws.close(code=1008)
            return
        client = None
        if token.startswith("trc_"):
            client = next((c for c in cfg.clients if security.token_matches(token, c.token_hash)), None)
        if not client or not client.enabled:
            try:
                throttle.begin(f"ip:{ip}")
            except security.Locked:
                pass
            log.warning("speaker connection with an invalid token from %s", ip)
            await ws.close(code=1008)
            return
        if len(engine.clients) >= settings.max_clients and client.id not in engine.clients:
            await ws.close(code=1013)
            return
        await ws.accept()
        conn = ClientConn(client, ws.send_bytes, ws.send_text)
        client.last_seen = time.time()
        store.save()
        engine.attach_client(conn)
        log.info("speaker '%s' connected from %s", client.name, ip)
        await conn.send_json({"type": "config", "volume": 100, "server_version": settings.version})
        bucket, bucket_t = 0, time.monotonic()

        async def pinger():
            while True:
                await asyncio.sleep(20)
                await conn.send_json({"type": "ping"})

        ping = asyncio.create_task(pinger())
        try:
            while not conn.closed.is_set():
                try:
                    msg = await asyncio.wait_for(ws.receive(), 60)
                except TimeoutError:
                    log.info("speaker '%s' went quiet - closing", client.name)
                    break
                if msg["type"] == "websocket.disconnect":
                    break
                data = msg.get("bytes")
                if data is not None:
                    if len(data) > MAX_WS_BINARY:
                        break
                    now = time.monotonic()
                    if now - bucket_t > 1.0:
                        bucket, bucket_t = 0, now
                    bucket += len(data)
                    if bucket > 96_000:        # 3x the real-time rate of 16 kHz PCM16: drop the excess
                        continue
                    conn.mic(data)
                    continue
                text = msg.get("text") or ""
                if len(text) > MAX_WS_TEXT:
                    break
                try:
                    obj = json.loads(text)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                t = obj.get("type")
                if t == "hello":
                    conn.aec = bool(obj.get("aec"))
                    client.platform = str(obj.get("platform") or "")[:64]
                    client.client_version = str(obj.get("version") or "")[:32]
                    store.save()
                elif t == "button":               # the device's buttons act on the speaker using its mic
                    vs = engine.speaker_for_mic(client.id)
                    if vs and obj.get("action") == "talk" and vs.state == "idle":
                        engine.voice.start_turn(vs)
                    elif vs and obj.get("action") == "cancel" and vs.busy_task:
                        vs.busy_task.cancel()
                        vs.flush()
        except WebSocketDisconnect:
            pass
        finally:
            ping.cancel()
            client.last_seen = time.time()
            store.save()
            await engine.detach_client(conn)
            log.info("speaker '%s' disconnected", client.name)

    # -- static UI ---------------------------------------------------------------------------------------------------
    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/static/{name}")
    async def static(name: str):
        if not re.fullmatch(r"[a-z0-9_-]+\.(js|css|svg|png)", name):
            raise HTTPException(404)
        p = STATIC / name
        if not p.exists():
            raise HTTPException(404)
        return FileResponse(p)

    # -- loopback app for agent tunnels (MCP only) ---------------------------------------------------------------------
    internal = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @internal.api_route("/mcp", methods=["GET", "POST", "DELETE"])
    async def internal_mcp(request: Request):
        return await engine.mcp.handle(request)

    return app, internal


def _qr_data_url(text: str) -> str:
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return ""
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    buf = io.BytesIO()
    img.save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()
