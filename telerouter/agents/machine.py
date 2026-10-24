"""A machine reached over SSH: lists its tmux sessions and lets a speaker or caller talk to the agent in one.

Sessions telerouter did not start have none of its own hooks, so replies come from the agent's own records:

* Claude Code: `~/.claude/sessions/<pid>.json` (pid of the claude process in the pane) names the live
  session id; its transcript `~/.claude/projects/*/<id>.jsonl` gets our prompt (with a nonce) and, when the
  turn is over, a `system`/`turn_duration` entry. The last assistant text of that turn is the answer.
* Hermes: its hooks (`post_llm_call`, installed by telerouter for the agent on that machine) append events
  to `~/.telerouter/<agent>/events.jsonl` for every session on the host, ours or not. `post_llm_call`
  carries the turn's final answer and the prompt that started it (our nonce); its reasoning, tool calls and
  TUI belong to the intermediate steps and must never be read as the answer.
* Anything else, and Hermes hosts without telerouter's hooks: the pane itself - wait until it stops
  changing, take the new lines. That cuts reasoning, tool calls and the TUI chrome into the answer, so it
  is the last resort.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import time
from typing import TYPE_CHECKING

import asyncssh

from ..models import Machine
from .. import tmux as tmuxmod
from . import ssh
from .drivers import claude_dialog_keys, claude_reset_keys
from .runtime import AgentError, _paths, _short, context_size, turn_end_without_stop

if TYPE_CHECKING:
    from ..engine import Engine

log = logging.getLogger("telerouter.machine")

LIST_INTERVAL = 60
SESSION_RE = tmuxmod.SESSION_RE
INTERNAL_PREFIX = tmuxmod.INTERNAL_PREFIX      # sessions of telerouter's own agents
NEW_NAME_RE = tmuxmod.NEW_NAME_RE
WORKDIR_RE = tmuxmod.WORKDIR_RE

# Runs on the machine with Hermes' hook events on stdin: prints the answer of the turn that carried our nonce.
# Kept on the machine so a turn's event (its conversation_history makes it ~1 MB) is filtered there.
HERMES_EVENT_READER = r'''import json, sys
nonce = "#" + sys.argv[1]
for line in sys.stdin:
    if nonce not in line:
        continue
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    extra = ev.get("extra") or {}
    if ev.get("hook_event_name") != "post_llm_call":
        continue
    if nonce not in str(extra.get("user_message") or ""):
        continue
    print(json.dumps({"reply": str(extra.get("assistant_response") or "")}))
    break
'''


class MachineRuntime:
    def __init__(self, engine: "Engine", machine: Machine):
        self.engine = engine
        self.machine = machine
        self.state = "stopped"
        self.error = ""
        self.sessions: list[dict] = []        # [{"name", "command", "path"}]
        self.conn: asyncssh.SSHClientConnection | None = None
        self.home = ""
        self._task: asyncio.Task | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._refresh = asyncio.Event()
        self._procs: dict[int, tuple[int, str]] = {}
        self._procs_at = 0.0
        self._bg: set[asyncio.Task] = set()

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

    async def _close(self) -> None:
        if self.conn:
            self.conn.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.conn.wait_closed(), 5)
            self.conn = None

    async def _run(self) -> None:
        backoff = 5.0
        while True:
            m = self.machine
            try:
                self.state, self.error = "connecting", ""
                self.conn = await ssh.connect(m.host, m.port, m.username, self.engine.ssh_keys, m.host_key)
                self.home = (await self._sh('printf %s "$HOME"')).strip()
                self.state = "ready"
                backoff = 5.0
                while True:
                    if m.list_tmux:
                        await self.list_sessions()
                    self._refresh.clear()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._refresh.wait(), LIST_INTERVAL)
            except asyncio.CancelledError:
                raise
            except PermissionError as e:
                self.state, self.error = "error", str(e)
            except Exception as e:
                self.state, self.error = "error", _short(e)
                log.warning("[%s] %s", m.name, self.error)
            await self._close()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300.0)

    def refresh(self) -> None:
        self._refresh.set()

    async def _sh(self, cmd: str, input: str | None = None, timeout: float = 30) -> str:
        if not self.conn:
            raise AgentError(f"{self.machine.name} is not connected")
        res = await asyncio.wait_for(self.conn.run(cmd, input=input, check=False), timeout)
        return str(res.stdout or "")

    def _tmux(self, *args: str) -> str:
        return f"{_paths()}; tmux " + " ".join(ssh.q(x) for x in args)

    async def list_sessions(self) -> list[dict]:
        # printable separator: without a terminal tmux turns control characters (tabs) into "_"
        out = await self._sh(self._tmux("list-sessions", "-F",
                                        "#{session_name}|~|#{pane_current_command}|~|#{pane_current_path}")
                             + " 2>/dev/null")
        found = []
        for line in out.splitlines():
            parts = line.split("|~|")
            if len(parts) != 3 or not SESSION_RE.match(parts[0]) or parts[0].startswith(INTERNAL_PREFIX):
                continue
            found.append({"name": parts[0], "command": parts[1], "path": parts[2]})
        self.sessions = found
        self.engine.merge_tmux_sessions(self.machine, found)
        return found

    # -- the tmux page: every session on the machine, as text -------------------------------------------
    def agent_sessions(self) -> dict[str, dict]:
        """tmux session name -> the agent telerouter runs in it (its own agents on this machine)."""
        cfg = getattr(getattr(self.engine, "store", None), "config", None)
        out: dict[str, dict] = {}
        for a in getattr(cfg, "agents", None) or []:
            if a.machine_id != self.machine.id:
                continue
            out[a.tmux_session or f"{INTERNAL_PREFIX}{a.id}"] = {"id": a.id, "name": a.name, "kind": a.kind}
        return out

    async def processes(self) -> dict[int, tuple[int, str]]:
        """The machine's process table, cached for a few seconds (a tmux listing is polled)."""
        now = time.monotonic()
        if self._procs and now - self._procs_at < 3:
            return self._procs
        self._procs = tmuxmod.parse_processes(await self._sh(tmuxmod.processes_cmd(), timeout=20))
        self._procs_at = now
        return self._procs

    async def tmux_list(self) -> list[dict]:
        """Every session on the machine (telerouter's own agent sessions too) with the agent inside it.

        Read-only: one `list-sessions` plus a cached `ps`, so the page can poll it.
        """
        out = await self._sh(f"{_paths()}; {tmuxmod.list_cmd()}", timeout=20)
        sessions = tmuxmod.parse_sessions(out)
        if not sessions:
            return []
        procs = await self.processes()
        mine = self.agent_sessions()
        for s in sessions:
            found = tmuxmod.agents_in_tree(s["pid"], procs)
            agent = mine.get(s["name"])
            s["kind"] = agent["kind"] if agent else tmuxmod.classify(s, found)
            s["agent"] = agent
            s["agents"] = [tmuxmod.AGENT_LABELS.get(a, a) for a in found] or (
                [tmuxmod.AGENT_LABELS.get(s["kind"], "")] if s["kind"] in tmuxmod.AGENT_BINS else [])
        sessions.sort(key=lambda x: (not x["agent"], x["name"].lower()))
        return sessions

    async def tmux_screen(self, session: str, history: int = 0) -> str:
        """The pane as text with its colour escapes; `history` scrollback lines above the visible screen."""
        if not tmuxmod.session_ok(session):
            raise AgentError("invalid session name")
        if self.state != "ready":
            raise AgentError(f"{self.machine.name} is not reachable ({self.state})")
        cmd = (f"{_paths()}; if tmux has-session -t {ssh.q('=' + session)} 2>/dev/null; then printf 'ok\\n'; "
               f"{tmuxmod.capture_cmd(session, history)}; else printf 'gone\\n'; fi")
        out = await self._sh(cmd, timeout=25)
        head, _, rest = out.partition("\n")
        if head.strip() != "ok":
            raise AgentError(f"the session {session} is not running any more")
        return rest

    async def tmux_send(self, session: str, text: str = "", key: str = "", enter: bool = False) -> None:
        """Type into a pane: `text` (pasted, so multi-line input arrives whole) and/or one keysym."""
        if not tmuxmod.session_ok(session):
            raise AgentError("invalid session name")
        if self.state != "ready":
            raise AgentError(f"{self.machine.name} is not reachable ({self.state})")
        if key:
            if not tmuxmod.KEYS_RE.match(key):
                raise AgentError("that key can't be sent")
            await self._sh(f"{_paths()}; {tmuxmod.send_key_cmd(session, key)}", timeout=15)
            return
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        if not text and not enter:
            raise AgentError("nothing to send")
        if not text:
            await self._sh(f"{_paths()}; {tmuxmod.send_key_cmd(session, 'Enter')}", timeout=15)
            return
        buf = f"tr{secrets.token_hex(3)}"
        await self._sh(f"{_paths()}; {tmuxmod.send_text_cmd(session, text, buf, enter)}", timeout=25)

    async def tmux_new(self, name: str, folder: str = "~", run: str = "agent") -> dict:
        """Start a session from the tmux page: `run` = agent (the machine's type), claude, hermes or shell."""
        m = self.machine
        name = (name or "").strip()
        if not tmuxmod.new_name_ok(name):
            raise AgentError("session names: letters, digits, - _ . (up to 40), not starting with telerouter-")
        folder = " ".join((folder or "~").split()) or "~"
        if not tmuxmod.folder_ok(folder):
            raise AgentError("the folder must be a plain path (letters, digits, _ . / - and spaces)")
        if self.state != "ready":
            raise AgentError(f"{m.name} is not reachable ({self.state}{': ' + self.error if self.error else ''})")
        if any(x["name"] == name for x in await self.tmux_list()):
            raise AgentError(f"a session called {name} already exists on {m.name}")
        kind = m.agent_kind if run == "agent" else run
        if kind == "shell":
            inner = ""
        else:
            launch = (self.launch_command(name) if kind == m.agent_kind
                      else ("hermes" if kind == "hermes" else "claude -n " + ssh.q(name)))
            inner = f'{_paths()}; {launch}; echo; echo "the agent exited"; exec "${{SHELL:-sh}}" -l'
        out = await self._sh(f"{tmuxmod.mkdir_cmd(folder)} && {tmuxmod.new_session_cmd(name, folder, inner)} "
                             f"&& printf ok 2>&1", timeout=30)
        if "ok" not in out:
            raise AgentError(f"could not start the session: {out.strip()[:200] or 'tmux failed'}")
        log.info("[%s] started the tmux session %s (%s) in %s", m.name, name, kind, folder)
        self._procs_at = 0.0                       # a new pane: the process table is stale now
        if kind in ("claude", "hermes"):
            # the start-up dialog (workspace trust, auto mode) is answered in the background while the
            # page shows the pane; it is the same routine telerouter's own agent sessions go through
            task = asyncio.create_task(self._settle_new(name, kind))
            self._bg.add(task)
            task.add_done_callback(self._bg.discard)
        found = await self.tmux_list()
        return next((x for x in found if x["name"] == name), {"name": name, "kind": kind, "path": folder})

    async def _settle_new(self, session: str, kind: str) -> None:
        try:
            if kind == "claude":
                await self._settle(session)
            else:
                await self._settle_hermes(session)
        except Exception as e:                      # the user is watching the pane anyway
            log.warning("[%s] %s: start-up handling stopped: %s", self.machine.name, session, _short(e))

    async def tmux_kill(self, session: str) -> None:
        """Close a session. An agent's own session is left to the Agents page (it would just come back)."""
        if not tmuxmod.session_ok(session):
            raise AgentError("invalid session name")
        if session in self.agent_sessions():
            raise AgentError(f"{session} is the session of an agent: stop or restart the agent under "
                             f"Agents & machines")
        cmd = (f"{_paths()}; if tmux has-session -t {ssh.q('=' + session)} 2>/dev/null; then "
               f"{tmuxmod.kill_cmd(session)} && printf ok; else printf gone; fi")
        out = await self._sh(cmd, timeout=20)
        if "ok" not in out and "gone" not in out:
            raise AgentError("could not close the session")
        log.info("[%s] closed the tmux session %s", self.machine.name, session)
        self._procs_at = 0.0
        self.refresh()

    # -- starting a session ---------------------------------------------------------------------------
    def launch_command(self, name: str) -> str:
        m = self.machine
        if m.agent_kind == "hermes":
            return "hermes"
        args = ["claude", "-n", name]
        if m.new_session_model:
            args += ["--model", m.new_session_model]
        if m.new_session_permission_mode != "default":
            args += ["--permission-mode", m.new_session_permission_mode]
        return " ".join(ssh.q(a) for a in args)

    async def new_session(self, name: str, workdir: str = "~", by_admin: bool = False) -> dict:
        """Start a tmux session `name` with a fresh agent in `workdir` (created if missing); returns its listing.
        Agents need the machine's allow_new_sessions; the admin's scheduled tasks (`by_admin`) do not."""
        m = self.machine
        if not m.allow_new_sessions and not by_admin:
            raise AgentError(f"starting new sessions on {m.name} is not allowed (Agents & machines -> edit the machine)")
        name = name.strip()
        if not NEW_NAME_RE.match(name) or name.startswith(INTERNAL_PREFIX):
            raise AgentError("session names: letters, digits, - _ . (up to 40), not starting with telerouter-")
        workdir = " ".join((workdir or "~").split()) or "~"
        if not WORKDIR_RE.match(workdir) or ".." in workdir.split("/"):
            raise AgentError("the folder must be a plain path (letters, digits, _ . / - and spaces)")
        if self.state != "ready":
            raise AgentError(f"{m.name} is not reachable ({self.state}{': ' + self.error if self.error else ''})")
        if any(x["name"] == name for x in await self.list_sessions()):
            raise AgentError(f"a session called {name} already exists on {m.name}")
        rest = workdir[1:].lstrip("/") if workdir.startswith("~") else ""
        target = ('"$HOME"' + (f"/{ssh.q(rest)}" if rest else "")) if workdir.startswith("~") else ssh.q(workdir)
        inner = f'{_paths()}; {self.launch_command(name)}; echo; echo "the agent exited"; exec "${{SHELL:-sh}}" -l'
        out = await self._sh(f'{_paths()}; D={target}; {{ mkdir -p "$D" && tmux new-session -d -s {ssh.q(name)} '
                             f'-c "$D" {ssh.q(inner)} && printf "ok %s" "$D"; }} 2>&1')
        if not out.startswith("ok "):
            raise AgentError(f"could not start the session: {out.strip()[:200] or 'tmux failed'}")
        path = out[3:].strip()
        log.info("[%s] started a new %s session %s in %s", m.name, m.agent_kind, name, path)
        if m.agent_kind == "claude":
            await self._settle(name)
        else:
            await self._settle_hermes(name)
        found = await self.list_sessions()
        return next((x for x in found if x["name"] == name), {"name": name, "command": m.agent_kind, "path": path})

    async def _settle_hermes(self, session: str, timeout: int = 45) -> None:
        """Wait until Hermes' TUI has drawn and stopped changing: a prompt typed into a booting screen is lost
        (and its banner is what a screen reader would call the answer)."""
        pane = f"={session}:"
        stable, last = 0, ""
        for _ in range(timeout):
            await asyncio.sleep(1)
            screen = await self._sh(self._tmux("capture-pane", "-p", "-t", pane), timeout=10)
            if "the agent exited" in screen:
                log.warning("[%s] %s: hermes exited during start-up", self.machine.name, session)
                return
            stable = stable + 1 if screen.strip() and screen == last else 0
            last = screen
            if stable >= 3:
                return
        log.warning("[%s] %s: the Hermes UI did not settle within %d s", self.machine.name, session, timeout)

    async def _settle(self, session: str, timeout: int = 45) -> None:
        """Answer Claude Code's start-up dialogs (workspace trust, auto mode) and wait until the UI is still."""
        pane = f"={session}:"
        stable, last, answered, waited = 0, "", "", 0
        for _ in range(timeout):
            await asyncio.sleep(1)
            screen = await self._sh(self._tmux("capture-pane", "-p", "-t", pane), timeout=10)
            tail = "\n".join([ln for ln in screen.splitlines() if ln.strip()][-14:])
            if "the agent exited" in tail:          # launch line in new_session; don't type into the shell
                log.warning("[%s] %s: claude exited during start-up", self.machine.name, session)
                return
            # the same screen again: the keys did nothing yet; send them again after a few seconds
            waited = waited + 1 if screen == answered else 0
            keys = claude_dialog_keys(tail) if screen != answered or waited >= 3 else None
            if keys:
                answered, waited = screen, 0
                log.info("[%s] %s: answering a start-up dialog", self.machine.name, session)
                for k in keys:
                    await self._sh(self._tmux("send-keys", "-t", pane, k))
                    await asyncio.sleep(0.3)
                stable = 0
                continue
            stable = stable + 1 if screen == last and screen.strip() else 0
            last = screen
            if stable >= 3 and await self.claude_transcript(session):
                return
        log.warning("[%s] %s: the new session did not settle within %d s", self.machine.name, session, timeout)

    # -- talking to a session ------------------------------------------------------------------------
    async def has_session(self, session: str) -> bool:
        return any(x["name"] == session for x in await self.list_sessions())

    async def kill_session(self, session: str) -> None:
        if not SESSION_RE.match(session) or session.startswith(INTERNAL_PREFIX):
            raise AgentError("invalid session name")
        await self._sh(self._tmux("kill-session", "-t", "=" + session) + " 2>/dev/null")
        log.info("[%s] closed the tmux session %s", self.machine.name, session)
        self.refresh()

    async def context_tokens(self, session: str) -> int | None:
        """Context size of the Claude Code conversation in `session` after its last turn (None: unknown)."""
        path = await self.claude_transcript(session)
        if not path:
            return None
        return context_size(await self._sh(f"tail -n 80 {ssh.q(path)} 2>/dev/null", timeout=15))

    async def reset_context(self, session: str, mode: str, keep: str = "") -> None:
        """Type /clear or /compact into an idle Claude Code session."""
        keys = claude_reset_keys(mode, keep)
        if not keys or not SESSION_RE.match(session):
            raise AgentError(f"can't {mode} that session")
        pane = f"={session}:"
        async with self._locks.setdefault(session, asyncio.Lock()):
            await asyncio.sleep(1.5)                     # let the TUI return to its prompt
            for k in keys:
                if k == "Enter":
                    await self._sh(self._tmux("send-keys", "-t", pane, "Enter"))
                else:
                    await self._sh(self._tmux("send-keys", "-t", pane, "-l", "--", k))
                    await asyncio.sleep(0.5)
            log.info("[%s] %s: context %s", self.machine.name, session, mode)
            await asyncio.sleep(3 if mode == "clear" else 30)   # /compact takes a while

    async def ask(self, session: str, text: str, label: str, on_wait=None, header: str = "",
                  timeout: float | None = None) -> str:
        """Type a spoken request into `session` and return the agent's answer when its turn is over.
        With `header` the text is not a spoken request (a scheduled task): "[telerouter #nonce · header] text"."""
        if not SESSION_RE.match(session):
            raise AgentError("invalid session name")
        if self.state != "ready":
            raise AgentError(f"{self.machine.name} is not reachable ({self.state}{': ' + self.error if self.error else ''})")
        lock = self._locks.setdefault(session, asyncio.Lock())
        async with lock:
            nonce = secrets.token_hex(3)
            if header:
                prompt = f"[telerouter #{nonce} · {header}] {text.strip()}"
            else:
                prompt = (f"[telerouter #{nonce} · {label}: spoken request, answer in a few plain sentences for "
                          f"text-to-speech, no markdown] {' '.join(text.split())}")
            timeout = timeout or self.machine.reply_timeout
            if self.machine.agent_kind == "claude":
                return await self._ask_claude(session, prompt, nonce, timeout)
            events = await self._hermes_events()
            if events:
                return await self._ask_hermes(session, prompt, nonce, timeout, events)
            log.warning("[%s] %s: no Hermes hook events on this machine - reading the screen instead",
                        self.machine.name, session)
            return await self._ask_screen(session, prompt, timeout)

    async def _type(self, session: str, text: str) -> None:
        pane = f"={session}:"
        buf = f"tr{secrets.token_hex(3)}"
        await self._sh(self._tmux("set-buffer", "-b", buf, "--", text) + " && " +
                       self._tmux("paste-buffer", "-d", "-p", "-b", buf, "-t", pane))
        await asyncio.sleep(0.4)
        await self._sh(self._tmux("send-keys", "-t", pane, "Enter"))

    async def claude_transcript(self, session: str) -> str:
        """Transcript file of the Claude Code process running in `session` ("" if none)."""
        script = (f"{_paths()}; p=$(tmux display -p -t {ssh.q('=' + session + ':')} '#{{pane_pid}}' 2>/dev/null) || exit 0; "
                  "for c in $p $(pgrep -P $p 2>/dev/null); do f=\"$HOME/.claude/sessions/$c.json\"; "
                  "[ -f \"$f\" ] && { cat \"$f\"; exit 0; }; done")
        info = await self._sh(script)
        try:
            d = json.loads(info)
        except ValueError:
            return ""
        sid, cwd = str(d.get("sessionId") or ""), str(d.get("cwd") or "")
        if not re.match(r"^[0-9a-f-]{36}$", sid):
            return ""
        path = (await self._sh(f'ls -t "$HOME"/.claude/projects/*/{sid}.jsonl 2>/dev/null | head -1')).strip()
        if not path and cwd.startswith("/") and self.home:
            # no message yet: the file appears with the first one, in the folder named after the cwd
            path = f"{self.home}/.claude/projects/{claude_project_dir(cwd)}/{sid}.jsonl"
        return path if re.match(r"^[A-Za-z0-9_./~ -]+\.jsonl$", path) else ""

    async def _ask_claude(self, session: str, prompt: str, nonce: str, timeout: float) -> str:
        path = await self.claude_transcript(session)
        if not path:
            raise AgentError(f"no Claude Code is running in the {session} session")
        start = int((await self._sh(f"wc -l < {ssh.q(path)} 2>/dev/null || echo 0")).strip() or 0)
        await self._type(session, prompt)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            out = await self._sh(f"tail -n +{start + 1} {ssh.q(path)} 2>/dev/null | head -c 4000000", timeout=30)
            reply = turn_end_without_stop(out, nonce)
            if reply is not None:
                return reply or "Done."
            if f"#{nonce}" not in out and time.monotonic() > deadline - timeout + 30:
                # never accepted (e.g. the session was cleared and the file changed): look again once
                newer = await self.claude_transcript(session)
                if newer and newer != path:
                    path, start = newer, 0
        raise AgentError(f"the {session} session is still working; I stopped waiting")

    async def _ask_hermes(self, session: str, prompt: str, nonce: str, timeout: float, path: str) -> str:
        """Type the prompt and wait for Hermes' own turn-end event carrying that nonce (never the screen)."""
        home = self.home or (await self._sh('printf %s "$HOME"')).strip()
        reader = f"{home}/.telerouter/hermes-event-reader.py"
        if not re.match(r"^/[A-Za-z0-9_./-]+\.py$", reader):
            raise AgentError(f"could not place the event reader on {self.machine.name} (home {home!r})")
        if not await self._write_remote(reader, HERMES_EVENT_READER):
            raise AgentError(f"could not write {reader} on {self.machine.name}")
        start = int((await self._sh(f"wc -c < {ssh.q(path)} 2>/dev/null || echo 0")).strip() or 0)
        await self._type(session, prompt)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            out = await self._sh(
                f"F={start}; S=$(wc -c < {ssh.q(path)} 2>/dev/null || echo 0); "
                f"[ \"${{S:-0}}\" -lt \"$F\" ] && F=0; "
                f"tail -c +$((F + 1)) {ssh.q(path)} 2>/dev/null | python3 {ssh.q(reader)} {ssh.q(nonce)}",
                timeout=30)
            line = out.strip().splitlines()[-1].strip() if out.strip() else ""
            if not line:
                continue
            try:
                reply = json.loads(line).get("reply")
            except ValueError:
                continue
            return (str(reply or "").strip() or "Done.")
        raise AgentError(f"the {session} session is still working; I stopped waiting")

    async def _write_remote(self, path: str, content: str) -> bool:
        tmp = f"{path}.tmp{secrets.token_hex(3)}"
        out = await self._sh(f"umask 077; mkdir -p {ssh.q(os.path.dirname(path))} && cat > {ssh.q(tmp)} && "
                             f"chmod 600 {ssh.q(tmp)} && mv -f {ssh.q(tmp)} {ssh.q(path)} && echo ok",
                             input=content)
        return "ok" in out

    async def _hermes_events(self) -> str:
        """The Hermes hook-event file on this machine ("" when telerouter's hooks are not installed there).

        Hermes hooks are global to the host's install, so any of telerouter's Hermes agents there records the
        events of every session - including task sessions and sessions other people started.
        """
        out = await self._sh(f"{_paths()}; ls -t \"$HOME\"/.telerouter/*/events.jsonl 2>/dev/null | head -1")
        path = out.strip().splitlines()[0].strip() if out.strip() else ""
        return path if re.match(r"^[A-Za-z0-9_./ -]+\.jsonl$", path) else ""

    async def _ask_screen(self, session: str, prompt: str, timeout: float) -> str:
        pane = f"={session}:"
        before = await self._sh(self._tmux("capture-pane", "-p", "-t", pane))
        await self._type(session, prompt)
        last, stable, changed = "", 0, False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            now = await self._sh(self._tmux("capture-pane", "-p", "-t", pane))
            changed = changed or now != before
            stable = stable + 1 if now == last else 0
            last = now
            if changed and stable >= 3:
                old = set(before.splitlines())
                new = [ln for ln in now.splitlines() if ln.strip() and ln not in old and "telerouter #" not in ln]
                return "\n".join(new[-15:]) or "Done."
        raise AgentError(f"the {session} session is still working; I stopped waiting")

    def info(self) -> dict:
        return {"id": self.machine.id, "state": self.state, "error": self.error, "sessions": self.sessions}


ROUTE_IDLE_S = 15 * 60
_EXIT = re.compile(r"\b(disconnect|(leave|exit|end|close|quit) (the |this )?(session|tmux|connection)|"
                   r"stop talking to (it|the session)|that'?s all for (the|this) session)\b", re.I)


class Route:
    """A speaker or phone call that talks to a tmux session instead of its agent."""

    def __init__(self, machine_id: str, session: str, label: str):
        self.machine_id = machine_id
        self.session = session
        self.label = label
        self.last = time.time()

    def expired(self) -> bool:
        return time.time() - self.last > ROUTE_IDLE_S


def is_exit(text: str, agent_name: str = "") -> bool:
    """A short sentence that leaves the session ("back to Jarvis", "disconnect", "leave the session")."""
    t = " ".join(re.sub(r"[^\w' ]", " ", (text or "").lower()).split())
    if not t or len(t.split()) > 7:                 # longer sentences are meant for the session
        return False
    names = {"jarvis", "the assistant", "assistant"} | ({agent_name.lower()} if agent_name else set())
    return any(f"back to {n}" in t for n in names) or bool(_EXIT.search(t))


def claude_project_dir(cwd: str) -> str:
    """Claude Code's folder name for a working directory: every non-alphanumeric character becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)
