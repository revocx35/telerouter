"""Sessions API (/v1): other apps start agent sessions on a machine, give them a task and read the outcome.

An app holds an MCP token with the `sessions` scope (made on the MCP access page, hashed, revocable). It may
start sessions only on machines whose admin ticked *Allow API sessions*, and it sees, talks to and closes only
the sessions it started itself. A session is a normal tmux session (`MachineRuntime.new_session`), so the owner
can watch it on the tmux page; the task is typed in with a header that says which app sent it, and the answer is
read the same way as for any other tmux session (Claude Code transcript / Hermes hook event).

An app can hand the session's agent its own MCP servers (e.g. ADM gives each download job a server whose token
reaches only that job). Those go into a per-session folder on the machine (`~/.telerouter/api/<session>/`,
0700, files 0600: the server tokens never appear on a command line) and Claude Code is started with
`--settings` / `--mcp-config` from there. Hermes keeps MCP servers in its global config, so sessions with MCP
servers need a Claude Code machine.

Run state lives in `/data/api_sessions.json` (no secrets in it). A restart cannot keep waiting for a turn that
was in flight: such sessions come back as `lost` (the agent itself may well still be working; ask again).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, ValidationError, field_validator

from .agents import ssh
from .agents.runtime import AgentError, _short
from .models import ExtraMcpServer

if TYPE_CHECKING:
    from .engine import Engine
    from .models import McpToken

log = logging.getLogger("telerouter.apisessions")

MAX_ACTIVE_PER_TOKEN = 10            # sessions an app may have open at once
MAX_RECORDS = 500                    # closed sessions are forgotten oldest first beyond this
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
ID_RE = re.compile(r"^s_[0-9a-f]{12}$")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-Za-z0-9]")
URL_RE = re.compile(r"^(https://[A-Za-z0-9.\-\[\]:]+|http://(127\.0\.0\.1|localhost)(:\d+)?)(/[A-Za-z0-9._~/%-]*)?$")
ACTIVE = ("starting", "working", "idle", "lost")


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class SessionMcpServer(BaseModel):
    """An MCP server the session's agent gets (Claude Code only): name, https URL, bearer, pinned CA."""
    name: str
    url: str
    token: str = Field(default="", max_length=512)
    tls_ca: str = ""
    description: str = Field(default="", max_length=300)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.match(r"^[a-z0-9][a-z0-9_-]{0,31}$", v) or v == "telerouter":
            raise ValueError("lower-case letters, digits, - and _ (not 'telerouter')")
        return v

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip()
        if not URL_RE.match(v):
            raise ValueError("must be an https:// URL (plain http only for 127.0.0.1)")
        return v

    @field_validator("token")
    @classmethod
    def _token(cls, v: str) -> str:
        v = v.strip()
        if v and not re.match(r"^[A-Za-z0-9._~+/=-]+$", v):
            raise ValueError("the token must be printable without spaces")
        return v

    @field_validator("tls_ca")
    @classmethod
    def _ca(cls, v: str) -> str:
        return ExtraMcpServer.model_validate({"name": "x", "url": "https://x", "tls_ca": v}).tls_ca


class NewSession(BaseModel):
    machine: str = Field(default="", max_length=64)
    name: str = Field(default="", max_length=40)
    folder: str = Field(default="", max_length=200)
    prompt: str = Field(default="", max_length=60_000)
    timeout: int = Field(default=0, ge=0, le=86_400)          # seconds to wait for the task's turn; 0 = machine's
    model: str = Field(default="", max_length=80)               # "" = the machine's new-session model
    mcp_servers: list[SessionMcpServer] = Field(default_factory=list, max_length=5)


    @field_validator("model")
    @classmethod
    def _model(cls, v: str) -> str:
        from .models import MODEL_RE
        v = v.strip()
        if not MODEL_RE.match(v):
            raise ValueError("invalid model name")
        return v


class SendText(BaseModel):
    text: str = Field(min_length=1, max_length=60_000)
    timeout: int = Field(default=0, ge=0, le=86_400)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text or "")


class ApiSessions:
    def __init__(self, engine: "Engine", path):
        self.engine = engine
        self.path = path
        self.records: dict[str, dict] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._load()

    # -- persistence ------------------------------------------------------------------------------------
    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        for r in data.get("sessions", []) if isinstance(data, dict) else []:
            if isinstance(r, dict) and ID_RE.match(str(r.get("id", ""))):
                if r.get("state") in ("starting", "working"):
                    r["state"] = "lost"
                    r["error"] = "telerouter restarted while this turn was running; the agent may still be working"
                self.records[r["id"]] = r

    def _save(self) -> None:
        recs = sorted(self.records.values(), key=lambda r: r.get("created", 0))
        closed = [r for r in recs if r.get("state") == "closed"]
        while len(recs) > MAX_RECORDS and closed:
            gone = closed.pop(0)
            recs.remove(gone)
            self.records.pop(gone["id"], None)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"sessions": recs}, f, indent=1)
        os.replace(tmp, self.path)

    # -- lookups ----------------------------------------------------------------------------------------
    def machines(self) -> list[dict]:
        out = []
        for m in self.engine.store.config.machines:
            if not m.allow_api_sessions or not m.enabled:
                continue
            rt = self.engine.machine_rts.get(m.id)
            out.append({"id": m.id, "name": m.name, "agent_kind": m.agent_kind,
                        "state": rt.state if rt else "stopped", "reply_timeout": m.reply_timeout})
        return out

    def _machine(self, ref: str):
        rts = [rt for rt in self.engine.machine_rts.values()
               if rt.machine.allow_api_sessions and rt.machine.enabled]
        if ref:
            r = " ".join(ref.lower().split())
            rts = [rt for rt in rts if r in (rt.machine.id, rt.machine.name.lower())]
            if not rts:
                raise ApiError(404, f"no machine '{ref}' allows API sessions")
        if not rts:
            raise ApiError(409, "no machine allows API sessions (Agents & machines -> edit a machine)")
        if len(rts) > 1:
            raise ApiError(400, "which machine? " + ", ".join(rt.machine.name for rt in rts))
        rt = rts[0]
        if rt.state != "ready":
            raise ApiError(502, f"{rt.machine.name} is not reachable ({rt.state})")
        return rt

    def _own(self, tok: "McpToken", sid: str) -> dict:
        r = self.records.get(sid) if ID_RE.match(sid or "") else None
        if not r or r.get("token_id") != tok.id:
            raise ApiError(404, "no such session")         # someone else's session looks like a missing one
        return r

    @staticmethod
    def public(r: dict) -> dict:
        return {k: r.get(k) for k in ("id", "name", "machine", "machine_id", "folder", "state", "reply", "error",
                                      "turns", "created", "updated", "mcp_servers", "model")}

    def list(self, tok: "McpToken") -> list[dict]:
        return [self.public(r) for r in sorted(self.records.values(), key=lambda r: r.get("created", 0),
                                               reverse=True) if r.get("token_id") == tok.id]

    def get(self, tok: "McpToken", sid: str) -> dict:
        return self.public(self._own(tok, sid))

    # -- actions ----------------------------------------------------------------------------------------
    def _set(self, r: dict, **kw) -> None:
        r.update(kw)
        r["updated"] = time.time()
        self._save()

    async def create(self, tok: "McpToken", body: NewSession) -> dict:
        active = [r for r in self.records.values() if r.get("token_id") == tok.id and r.get("state") in ACTIVE]
        if len(active) >= MAX_ACTIVE_PER_TOKEN:
            raise ApiError(429, f"this app already has {len(active)} open sessions; close some first")
        rt = self._machine(body.machine)
        m = rt.machine
        if body.mcp_servers and m.agent_kind != "claude":
            raise ApiError(400, f"{m.name} runs Hermes: MCP servers per session need a Claude Code machine")
        names = [s.name for s in body.mcp_servers]
        if len(set(names)) != len(names):
            raise ApiError(422, "MCP server names must differ")
        name = body.name.strip() or f"api-{secrets.token_hex(3)}"
        if not NAME_RE.match(name) or name.startswith("telerouter-"):
            raise ApiError(422, "session names: letters, digits, - _ . (up to 40), not starting with telerouter-")
        if any(r.get("machine_id") == m.id and r.get("name") == name and r.get("state") in ACTIVE
               for r in self.records.values()):
            raise ApiError(409, f"a session called {name} is already open on {m.name}")
        folder = " ".join(body.folder.split()) or f"~/telerouter-api/{name}"
        sid = "s_" + secrets.token_hex(6)
        r = {"id": sid, "token_id": tok.id, "app": tok.name, "machine_id": m.id, "machine": m.name, "name": name,
             "folder": folder, "state": "starting", "reply": "", "error": "", "turns": 0, "created": time.time(),
             "updated": time.time(), "mcp_servers": names, "files_dir": "", "model": body.model}
        self.records[sid] = r
        self._save()
        self._spawn(sid, self._start(r, rt, body, tok.name))
        log.info("API session %s (%s) requested by '%s' on %s", name, sid, tok.name, m.name)
        return self.public(r)

    async def _start(self, r: dict, rt, body: NewSession, app: str) -> None:
        try:
            files_dir = ""
            if body.mcp_servers:
                home = rt.home or (await rt._sh('printf %s "$HOME"')).strip()
                files_dir = f"{home}/.telerouter/api/{r['name']}"
                if not re.match(r"^/[A-Za-z0-9_./-]+$", files_dir):
                    raise AgentError(f"unusable home folder on {rt.machine.name}")
                r["files_dir"] = files_dir
                for path, content in session_files(body.mcp_servers).items():
                    if not await rt._write_remote(f"{files_dir}/{path}", content):
                        raise AgentError(f"could not write the session's MCP settings on {rt.machine.name}")
            info = await rt.new_session(r["name"], r["folder"], by_api=True, files_dir=files_dir, model=body.model)
            self._set(r, folder=info.get("path") or r["folder"], state="idle")
            if body.prompt.strip():
                await self._turn(r, rt, body.prompt, body.timeout, app)
        except asyncio.CancelledError:
            raise
        except AgentError as e:
            self._set(r, state="failed", error=str(e))
            await self._remove_files(r, rt)
        except Exception as e:
            log.warning("API session %s failed to start: %s", r["name"], _short(e))
            self._set(r, state="failed", error=_short(e))
            await self._remove_files(r, rt)

    async def _turn(self, r: dict, rt, text: str, timeout: int, app: str) -> None:
        self._set(r, state="working", error="")
        header = (f'task from the app "{app}" (telerouter Sessions API): nobody is watching this session live. '
                  "Do the task, then end with a short summary of the result")
        try:
            reply = await rt.ask(r["name"], text, label="", header=header,
                                 timeout=float(timeout or rt.machine.reply_timeout))
            self._set(r, state="idle", reply=reply[:20_000], turns=int(r.get("turns") or 0) + 1)
        except AgentError as e:
            still = "still working" in str(e)
            self._set(r, state="lost" if still else "failed", error=str(e))

    async def send(self, tok: "McpToken", sid: str, body: SendText) -> dict:
        r = self._own(tok, sid)
        if r["state"] in ("starting", "working"):
            raise ApiError(409, f"the session is {r['state']}; wait for its answer")
        if r["state"] in ("closed", "failed"):
            raise ApiError(409, f"the session is {r['state']}")
        rt = self.engine.machine_rts.get(r["machine_id"])
        if not rt or rt.state != "ready":
            raise ApiError(502, f"{r['machine']} is not reachable")
        self._set(r, state="working")
        self._spawn(sid, self._turn(r, rt, body.text, body.timeout, tok.name))
        return self.public(r)

    async def screen(self, tok: "McpToken", sid: str, history: int = 0) -> str:
        r = self._own(tok, sid)
        rt = self.engine.machine_rts.get(r["machine_id"])
        if not rt:
            raise ApiError(502, f"{r['machine']} is not connected")
        try:
            return strip_ansi(await rt.tmux_screen(r["name"], max(0, min(int(history), 2000))))
        except AgentError as e:
            raise ApiError(502, str(e)) from e

    async def close(self, tok: "McpToken", sid: str) -> dict:
        r = self._own(tok, sid)
        task = self._tasks.pop(sid, None)
        if task:
            task.cancel()
        rt = self.engine.machine_rts.get(r["machine_id"])
        if rt and rt.state == "ready" and r["state"] != "closed":
            try:
                await rt.kill_session(r["name"])
            except Exception as e:
                raise ApiError(502, f"could not close the session: {_short(e)}") from e
            await self._remove_files(r, rt)
        self._set(r, state="closed")
        log.info("API session %s (%s) closed by '%s'", r["name"], sid, tok.name)
        return self.public(r)

    async def _remove_files(self, r: dict, rt) -> None:
        d = r.get("files_dir") or ""
        if d and re.match(r"^/[A-Za-z0-9_./-]+/\.telerouter/api/[A-Za-z0-9_.-]+$", d):
            try:
                await rt._sh(f"rm -rf -- {ssh.q(d)}", timeout=20)
            except Exception as e:
                log.info("could not remove %s: %s", d, _short(e))

    def revoke_token(self, token_id: str) -> None:
        """A revoked app's sessions stay open on the machine (the owner closes them on the tmux page)."""
        for r in self.records.values():
            if r.get("token_id") == token_id and r.get("state") in ACTIVE:
                r["token_id"] = ""
        self._save()

    def _spawn(self, sid: str, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks[sid] = t
        t.add_done_callback(lambda _t, s=sid: self._tasks.pop(s, None) if self._tasks.get(s) is _t else None)


def session_files(servers: list[SessionMcpServer]) -> dict[str, str]:
    """settings.json (allow the servers' tools: nobody answers a permission prompt), mcp.json, extra-ca.pem."""
    mcp: dict = {"mcpServers": {}}
    for s in servers:
        entry: dict = {"type": "http", "url": s.url}
        if s.token:
            entry["headers"] = {"Authorization": f"Bearer {s.token}"}
        mcp["mcpServers"][s.name] = entry
    settings = {"permissions": {"allow": [f"mcp__{s.name}" for s in servers]}}
    return {"settings.json": json.dumps(settings, indent=1), "mcp.json": json.dumps(mcp, indent=1),
            "extra-ca.pem": "".join(s.tls_ca for s in servers if s.tls_ca)}


def parse(model, data) -> BaseModel:
    try:
        return model.model_validate(data)
    except ValidationError as e:
        msg = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())
        raise ApiError(422, msg) from e
