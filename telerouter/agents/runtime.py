"""One agent: SSH connection, reverse tunnel for MCP, tmux session, prompts in, replies out."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import asyncssh

from ..models import Agent, McpToken
from ..security import new_token, token_hash
from . import ssh
from .drivers import Driver, make_driver

if TYPE_CHECKING:
    from ..engine import Engine

log = logging.getLogger("telerouter.agent")

MAX_EVENT_LINE = 1 << 20
EVENTS_MAX_BYTES = 4 << 20


class AgentError(Exception):
    pass


@dataclass
class Source:
    """Where a request to the agent came from (decides where replies and calls go)."""
    kind: str                       # "speaker" | "phone" | "mcp"
    label: str                      # "Kitchen speaker", "phone call from Dad"
    client_id: str = ""             # speaker
    call_id: str = ""               # phone


@dataclass
class Pending:
    nonce: str
    text: str
    source: Source
    future: asyncio.Future
    session_id: str = ""            # agent session that accepted the prompt (Claude UserPromptSubmit)
    started: float = field(default_factory=time.time)


class AgentRuntime:
    def __init__(self, engine: "Engine", agent: Agent):
        self.engine = engine
        self.agent = agent
        self.state = "stopped"          # stopped|connecting|starting|ready|error
        self.error = ""
        self.conn: asyncssh.SSHClientConnection | None = None
        self.home = ""
        self.driver: Driver | None = None
        self.session_alive = False
        self.pending: Pending | None = None
        self.current_source: Source | None = None
        self.log: deque[dict] = deque(maxlen=50)     # recent exchanges for the UI
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()                  # one voice request at a time
        self._listener = None
        self._ready = asyncio.Event()
        self._restart = asyncio.Event()

    # -- lifecycle --------------------------------------------------------------------------
    def start(self) -> None:
        if not self._task:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        await self._close()
        self.state = "stopped"
        if self.pending and not self.pending.future.done():
            self.pending.future.set_exception(AgentError("agent stopped"))

    async def _close(self) -> None:
        self._ready.clear()
        if self._listener:
            with contextlib.suppress(Exception):
                self._listener.close()
            self._listener = None
        if self.conn:
            self.conn.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.conn.wait_closed(), 5)
            self.conn = None

    @property
    def dir(self) -> str:
        return f"{self.home}/.telerouter/{self.agent.id}"

    async def _run(self) -> None:
        backoff = 3.0
        while True:
            try:
                self.state, self.error = "connecting", ""
                await self._connect()
                backoff = 3.0
                await self._serve()
            except asyncio.CancelledError:
                raise
            except PermissionError as e:
                self.state, self.error = "error", str(e)
            except (OSError, asyncssh.Error, AgentError, asyncio.TimeoutError) as e:
                self.state, self.error = "error", _short(e)
                log.warning("[%s] %s", self.agent.name, self.error)
            except Exception as e:
                self.state, self.error = "error", _short(e)
                log.exception("[%s] agent runtime failed", self.agent.name)
            await self._close()
            if self.pending and not self.pending.future.done():
                self.pending.future.set_exception(AgentError(f"agent connection lost: {self.error}"))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120.0)

    async def _connect(self) -> None:
        a = self.agent
        self.conn = await ssh.connect(a.host, a.port, a.username, self.engine.ssh_keys, a.host_key)
        self.home = (await self._sh('printf %s "$HOME"', check=True)).strip()
        if not self.home.startswith("/"):
            raise AgentError("could not read $HOME on the agent host")
        missing = (await self._sh(
            f'{_paths()}; for c in tmux {"claude" if a.kind == "claude" else "hermes"}; do '
            'command -v "$c" >/dev/null 2>&1 || printf "%s " "$c"; done')).strip()
        if missing:
            raise AgentError(f"not installed on {a.host}: {missing}")
        await self._sh(f"mkdir -p {ssh.q(self.dir)} && chmod 700 {ssh.q(self.home + '/.telerouter')} {ssh.q(self.dir)}",
                       check=True)
        # reverse tunnel: agent host 127.0.0.1:<mcp_remote_port> -> our loopback MCP listener
        try:
            self._listener = await self.conn.forward_remote_port(
                "127.0.0.1", a.mcp_remote_port, "127.0.0.1", self.engine.settings.internal_port)
        except asyncssh.Error as e:
            raise AgentError(f"could not open the MCP tunnel on {a.host}:127.0.0.1:{a.mcp_remote_port}: {e}") from e
        workdir = ssh.remote_path(self.home, a.workdir)
        self.driver = make_driver(a, self.dir, workdir, f"http://127.0.0.1:{a.mcp_remote_port}/mcp")
        log.info("[%s] connected to %s@%s", a.name, a.username, a.host)

    async def _serve(self) -> None:
        a = self.agent
        tail = asyncio.create_task(self._tail_events())
        try:
            self.session_alive = await self._has_session()
            if not self.session_alive and a.autostart:
                await self.start_session()
            elif self.session_alive:
                await self._handle_startup()      # it may sit in a dialog (e.g. we restarted mid-start)
                self.state = "ready"
                self._ready.set()
            else:
                self.state = "idle"
            while True:
                try:
                    await asyncio.wait_for(self._restart.wait(), 10)
                except TimeoutError:
                    pass
                if tail.done():
                    raise AgentError(f"event stream ended: {tail.exception() if not tail.cancelled() else ''}")
                alive = await self._has_session()
                if self._restart.is_set():
                    self._restart.clear()
                    if alive:
                        await self._kill_session()
                    alive = False
                    if a.enabled:
                        await self.start_session()
                        alive = True
                elif not alive and self.session_alive and a.autostart:
                    log.info("[%s] tmux session ended - starting it again", a.name)
                    await asyncio.sleep(2)
                    await self.start_session()
                    alive = True
                self.session_alive = alive
                if not alive:
                    self._ready.clear()
                    if self.state == "ready":
                        self.state = "idle"
        finally:
            tail.cancel()

    # -- shell helpers ---------------------------------------------------------------------------
    async def _sh(self, cmd: str, check: bool = False, input: str | None = None, timeout: float = 30) -> str:
        if not self.conn:
            raise AgentError("not connected")
        res = await asyncio.wait_for(self.conn.run(cmd, input=input, check=False), timeout)
        if check and res.exit_status != 0:
            raise AgentError(f"remote command failed ({res.exit_status}): {str(res.stderr or res.stdout)[:300]}")
        return str(res.stdout or "")

    @property
    def _pane(self) -> str:
        """tmux target of the agent's pane (exact session name, current window)."""
        return f"={self.agent.session_name}:"

    def _tmux(self, *args: str) -> str:
        return f"{_paths()}; tmux " + " ".join(ssh.q(x) for x in args)

    async def _has_session(self) -> bool:
        out = await self._sh(self._tmux("has-session", "-t", "=" + self.agent.session_name) + " 2>/dev/null && echo yes")
        return out.strip() == "yes"

    async def _kill_session(self) -> None:
        await self._sh(self._tmux("kill-session", "-t", "=" + self.agent.session_name) + " 2>/dev/null")

    async def _write_file(self, path: str, content: str, mode: int) -> None:
        tmp = f"{path}.tmp{secrets.token_hex(3)}"
        await self._sh(f"umask 077; cat > {ssh.q(tmp)} && chmod {mode:o} {ssh.q(tmp)} && mv -f {ssh.q(tmp)} {ssh.q(path)}",
                       check=True, input=content)

    # -- the tmux session ------------------------------------------------------------------------
    async def start_session(self) -> None:
        a = self.agent
        assert self.driver
        self.state = "starting"
        self._ready.clear()
        if a.kind == "claude" and not a.session_uuid:
            a.session_uuid = str(uuid.uuid4())
            self.engine.store.save()
        self.driver = make_driver(a, self.dir, self.driver.workdir, self.driver.mcp_url)
        token = self._issue_mcp_token()
        for path, (content, mode) in self.driver.files(token).items():
            await self._write_file(path, content, mode)
        await self._sh(f": >> {ssh.q(self.driver.events_path)}; "
                       f"[ $(wc -c < {ssh.q(self.driver.events_path)}) -gt {EVENTS_MAX_BYTES} ] && "
                       f": > {ssh.q(self.driver.events_path)}; true")
        setup = self.driver.setup_script()
        if setup:
            out = await self._sh(setup, check=True, timeout=60)
            log.info("[%s] agent host prepared: %s", a.name, out.strip()[:200])
        await self._sh(self._tmux("new-session", "-d", "-s", a.session_name, "-x", "160", "-y", "48",
                                  "sh " + ssh.q(f"{self.dir}/start.sh")), check=True)
        self.session_alive = True
        log.info("[%s] started %s in tmux session %s", a.name, a.kind, a.session_name)
        await self._handle_startup()
        self.state = "ready"
        self._ready.set()

    async def _handle_startup(self) -> None:
        """Click through first-run dialogs (workspace trust, ...) and wait until the UI settles."""
        assert self.driver
        stable, last, answered = 0, "", ""
        for _ in range(45):
            await asyncio.sleep(1)
            screen = await self.screen()
            tail = "\n".join([ln for ln in screen.splitlines() if ln.strip()][-14:])
            keys = self.driver.dialog_keys(tail) if screen != answered else None
            if keys:
                answered = screen
                log.info("[%s] answering a start-up dialog", self.agent.name)
                for k in keys:
                    await self._sh(self._tmux("send-keys", "-t", self._pane, k))
                    await asyncio.sleep(0.3)
                stable = 0
                continue
            stable = stable + 1 if screen == last and screen.strip() else 0
            last = screen
            if stable >= 3:
                return
        log.warning("[%s] agent UI did not settle within 45 s", self.agent.name)

    def _issue_mcp_token(self) -> str:
        """A fresh MCP token for this agent (only its hash is kept)."""
        cfg = self.engine.store.config
        tok = self.engine.store.mcp_token(self.agent.mcp_token_id)
        if not tok:
            tok = McpToken(name=f"agent {self.agent.name}", agent_id=self.agent.id)
            cfg.mcp_tokens.append(tok)
            self.agent.mcp_token_id = tok.id
        token = new_token("trm_")
        tok.token_hash = token_hash(token)
        self.engine.store.save()
        return token

    async def screen(self) -> str:
        try:
            return await self._sh(self._tmux("capture-pane", "-p", "-t", self._pane),
                                  timeout=10)
        except Exception:
            return ""

    def request_restart(self) -> None:
        self._restart.set()

    async def stop_session(self) -> None:
        self.agent.autostart = False
        await self._kill_session()
        self.session_alive = False
        self.state = "idle"

    # -- requests ----------------------------------------------------------------------------------
    async def ask(self, text: str, source: Source, on_note=None) -> str:
        """Type `text` into the agent and wait for its final answer."""
        a = self.agent
        if self.state not in ("ready", "starting"):
            raise AgentError(f"{a.name} is not available ({self.state}{': ' + self.error if self.error else ''})")
        async with self._lock:
            try:
                await asyncio.wait_for(self._ready.wait(), 60)
            except TimeoutError as e:
                raise AgentError(f"{a.name} did not start") from e
            assert self.driver
            nonce = secrets.token_hex(3)
            header = f"telerouter #{nonce} · {source.label}"
            prompt = self.driver.prompt_text(" ".join(text.split()), header)
            fut = asyncio.get_running_loop().create_future()
            self.pending = Pending(nonce, text, source, fut)
            self.pending.on_note = on_note  # type: ignore[attr-defined]
            self.current_source = source
            entry = {"at": time.time(), "source": source.label, "text": text, "reply": None}
            self.log.append(entry)
            try:
                await self._type(prompt)
                reply = await asyncio.wait_for(fut, a.reply_timeout)
                entry["reply"] = reply
                return reply
            except TimeoutError as e:
                entry["reply"] = "(no answer in time)"
                raise AgentError(f"{a.name} did not answer within {a.reply_timeout} s") from e
            finally:
                self.pending = None

    async def _type(self, text: str) -> None:
        s = self._pane
        buf = f"tr{secrets.token_hex(3)}"
        await self._sh(self._tmux("set-buffer", "-b", buf, "--", text) + " && " +
                       self._tmux("paste-buffer", "-d", "-p", "-b", buf, "-t", s), check=True)
        await asyncio.sleep(0.4)
        await self._sh(self._tmux("send-keys", "-t", s, "Enter"), check=True)

    async def interrupt(self) -> None:
        with contextlib.suppress(Exception):
            await self._sh(self._tmux("send-keys", "-t", self._pane, "Escape"))

    # -- events --------------------------------------------------------------------------------------
    async def _tail_events(self) -> None:
        assert self.conn and self.driver
        path = self.driver.events_path
        await self._sh(f": >> {ssh.q(path)}")
        async with self.conn.create_process(f"exec tail -n 0 -F {ssh.q(path)} 2>/dev/null",
                                            encoding="utf-8", errors="replace") as proc:
            async for line in proc.stdout:
                if len(line) > MAX_EVENT_LINE:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict):
                    try:
                        await self._on_event(ev)
                    except Exception:
                        log.exception("[%s] event handling failed", self.agent.name)

    async def _on_event(self, ev: dict) -> None:
        p = self.pending
        name = ev.get("hook_event_name") or ""
        if not p or p.future.done():
            return
        if self.agent.kind == "claude":
            if name == "UserPromptSubmit":
                if f"#{p.nonce}" in str(ev.get("prompt") or ""):
                    p.session_id = str(ev.get("session_id") or "")
            elif name == "Stop" and p.session_id and ev.get("session_id") == p.session_id:
                if ev.get("stop_hook_active"):
                    return
                reply = ev.get("last_assistant_message")
                if not isinstance(reply, str):
                    reply = await self._last_reply_from_transcript(str(ev.get("transcript_path") or ""))
                p.future.set_result(reply or "")
            elif name == "Notification" and p.session_id and ev.get("session_id") == p.session_id:
                note = getattr(p, "on_note", None)
                msg = str(ev.get("message") or "")
                if note and msg:
                    await note(msg)
        else:
            extra = ev.get("extra") or {}
            if name == "post_llm_call" and f"#{p.nonce}" in str(extra.get("user_message") or ""):
                p.session_id = str(ev.get("session_id") or "")
                p.future.set_result(str(extra.get("assistant_response") or ""))
            elif name == "on_session_end" and p.session_id and ev.get("session_id") == p.session_id \
                    and (extra.get("failed") or extra.get("interrupted")):
                p.future.set_exception(AgentError("the agent's turn failed"))

    async def _last_reply_from_transcript(self, path: str) -> str:
        """Fallback for Claude Code versions whose Stop hook has no last_assistant_message."""
        if not path or not re.match(r"^[A-Za-z0-9_./~ -]+\.jsonl$", path):
            return ""
        out = await self._sh(f"tail -n 60 {ssh.q(path)}", timeout=10)
        text = ""
        for line in out.splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "user" and isinstance(e.get("message", {}).get("content"), str):
                text = ""
            if e.get("type") == "assistant":
                for block in e.get("message", {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text") or text
        return text

    def info(self) -> dict:
        a = self.agent
        return {"id": a.id, "state": self.state, "error": self.error, "session_alive": self.session_alive,
                "busy": self.pending is not None,
                "attach": f"ssh -t {a.username}@{a.host}{' -p ' + str(a.port) if a.port != 22 else ''} "
                          f"tmux attach -t {a.session_name}",
                "log": list(self.log)[-10:]}


def _paths() -> str:
    return 'export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.hermes/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"'


def _short(e: BaseException) -> str:
    s = str(e) or e.__class__.__name__
    return s if len(s) < 300 else s[:300] + "..."
