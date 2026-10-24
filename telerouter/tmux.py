"""tmux sessions as text: the commands telerouter runs on a machine and the parsers for their output.

Pure functions only (no telerouter imports, no I/O), so the standalone tmux manager app ships the same
file: `tmux_manager/tmux.py` is a copy - keep the two identical when you change one.

Why text and not a pixel stream (VNC/noVNC): `capture-pane` hands back the pane's characters and
`send-keys`/`paste-buffer` type into it, so the browser draws the terminal itself. It stays sharp at any
zoom and on a phone, costs a few KB per refresh instead of a video stream, and a page of it can be
scrolled and copied. Nothing is recorded or transcoded.
"""

from __future__ import annotations

import re
import shlex

# `|~|` as the separator: without a terminal tmux turns control characters (a tab) into "_",
# which used to turn `list-sessions` output into "calls_claude_/root".
SEP = "|~|"
LIST_FORMAT = SEP.join([
    "#{session_name}", "#{session_windows}", "#{session_attached}", "#{session_created}",
    "#{session_activity}", "#{pane_current_command}", "#{pane_current_path}", "#{pane_pid}",
    "#{window_width}x#{window_height}",
])
LIST_FIELDS = 9

SESSION_RE = re.compile(r"^[A-Za-z0-9_.:@+-]{1,64}$")
INTERNAL_PREFIX = "telerouter-"
NEW_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
WORKDIR_RE = re.compile(r"^~?[A-Za-z0-9_./ -]{0,200}$")

# AI agents worth naming in the session list (the tmux pane's own command is often a shell or node)
AGENT_BINS = ("claude", "hermes", "codex", "opencode", "aider", "gemini", "cursor-agent")
# agents that hide behind an interpreter: Hermes starts as `python3 -I -c <launcher> chat ...` with its
# checkout on sys.path, so the binary name is not in the command line
AGENT_HINTS = (("hermes_cli", "hermes"), ("hermes-agent", "hermes"), ("claude-code", "claude"),
               ("@anthropic-ai/claude", "claude"))
AGENT_LABELS = {"claude": "Claude Code", "hermes": "Hermes", "codex": "Codex", "opencode": "OpenCode",
                "aider": "Aider", "gemini": "Gemini CLI", "cursor-agent": "Cursor agent"}
SHELLS = ("sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "nu")

# keys a browser may send (tmux keysyms); anything else is refused so the API can't be talked into
# `send-keys` with an arbitrary argument list
KEYS_RE = re.compile(r"^(?:C-[a-z]|M-[A-Za-z]|Enter|Tab|Escape|Space|BSpace|DC|IC|Up|Down|Left|Right"
                     r"|Home|End|PageUp|PageDown|F(?:[1-9]|1[0-2]))$")
KEYS = ["Enter", "Tab", "Escape", "Space", "BSpace", "Up", "Down", "Left", "Right", "Home", "End",
        "PageUp", "PageDown", "C-c", "C-d", "C-l", "C-u", "C-z", "C-a", "C-e", "C-r", "C-w", "F1", "F2"]
MAX_HISTORY = 2000


def q(s: str) -> str:
    return shlex.quote(s)


def target(session: str) -> str:
    """tmux target for pane commands: `=name:` matches this session's pane (a bare `=name` is a session)."""
    return "=" + session + ":"


# -- command lines --------------------------------------------------------------------------------------
def list_cmd() -> str:
    return "tmux list-sessions -F " + q(LIST_FORMAT) + " 2>/dev/null"


def processes_cmd() -> str:
    """The whole process table: the agent running in a pane is a descendant of the pane's own shell."""
    return "ps -eo pid=,ppid=,args= 2>/dev/null"


def capture_cmd(session: str, history: int = 0) -> str:
    """The pane as text with its colour escapes (`-e`); `history` adds scrollback lines above it."""
    args = ["tmux", "capture-pane", "-p", "-e", "-t", target(session)]
    if history:
        args += ["-S", "-%d" % clamp_history(history)]
    return " ".join(q(a) for a in args)


def send_key_cmd(session: str, key: str) -> str:
    return " ".join(q(a) for a in ("tmux", "send-keys", "-t", target(session), key))


def set_buffer_cmd(buf: str) -> str:
    return " ".join(q(a) for a in ("tmux", "set-buffer", "-b", buf))


def paste_cmd(session: str, buf: str) -> str:
    """Paste the buffer into the pane (bracketed paste: an agent takes it as one inserted text)."""
    return " ".join(q(a) for a in ("tmux", "paste-buffer", "-d", "-p", "-b", buf, "-t", target(session)))


def send_text_cmd(session: str, text: str, buf: str, enter: bool = True) -> str:
    """Two commands: put the text in a tmux buffer, then paste it (and press Enter unless `enter` is off)."""
    parts = [set_buffer_cmd(buf) + " -- " + q(text) , paste_cmd(session, buf)]
    if enter:
        parts.append(send_key_cmd(session, "Enter"))
    return " && ".join(parts)


def _home_path(folder: str) -> str:
    """A remote path for the shell: `~`/`~/x` against $HOME (never let the remote shell expand our input)."""
    if folder.startswith("~"):
        rest = folder[1:].lstrip("/")
        return '"$HOME"' + (f"/{q(rest)}" if rest else "")
    return q(folder)


def new_session_cmd(name: str, folder: str, inner: str = "") -> str:
    """`tmux new-session` in `folder` (`~` is the SSH user's home); `inner` is what runs in it."""
    cmd = " ".join(q(a) for a in ("tmux", "new-session", "-d", "-s", name, "-c")) + " " + _home_path(folder)
    if inner:
        cmd += " " + q(inner)
    return cmd


def kill_cmd(session: str) -> str:
    return " ".join(q(a) for a in ("tmux", "kill-session", "-t", "=" + session)) + " 2>/dev/null"


def mkdir_cmd(folder: str) -> str:
    return "mkdir -p " + _home_path(folder)


# -- parsing ---------------------------------------------------------------------------------------------
def _int(v: str) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


def parse_sessions(out: str) -> list[dict]:
    """`list-sessions -F LIST_FORMAT` -> one dict per session (unparseable lines are ignored)."""
    sessions = []
    for line in (out or "").splitlines():
        parts = line.split(SEP)
        if len(parts) != LIST_FIELDS:
            continue
        name = parts[0].strip()
        if not SESSION_RE.match(name):
            continue
        try:
            width, height = (int(x) for x in parts[8].strip().split("x"))
        except ValueError:
            width = height = 0
        sessions.append({
            "name": name,
            "windows": _int(parts[1]),
            "attached": _int(parts[2]) > 0,
            "created": _int(parts[3]),
            "activity": _int(parts[4]),
            "command": parts[5].strip(),
            "path": parts[6].strip(),
            "pid": _int(parts[7]),
            "width": width,
            "height": height,
        })
    return sessions


def parse_processes(out: str) -> dict[int, tuple[int, str]]:
    """`ps -eo pid=,ppid=,args=` -> {pid: (ppid, args)}."""
    procs: dict[int, tuple[int, str]] = {}
    for line in (out or "").splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        procs[pid] = (ppid, parts[2].strip())
    return procs


def agent_of(args: str) -> str:
    """The agent a process' command line names ("claude", "hermes", ...), or "" for anything else."""
    args = (args or "").strip()
    for tok in re.split(r"[\s=]+", args):
        base = tok.rsplit("/", 1)[-1]
        if base in AGENT_BINS:
            return base
    for hint, name in AGENT_HINTS:
        if hint in args:
            return name
    return ""


def agents_in_tree(pid: int, procs: dict[int, tuple[int, str]], depth: int = 6) -> list[str]:
    """Agents running inside a pane: the pane's own process and everything below it (a shell, then claude)."""
    if not pid or not procs:
        return []
    kids: dict[int, list[int]] = {}
    for p, (pp, _args) in procs.items():
        kids.setdefault(pp, []).append(p)
    found: list[str] = []
    stack = [(pid, 0)]
    while stack:
        cur, d = stack.pop()
        if d > depth:
            continue
        name = agent_of(procs.get(cur, (0, ""))[1])
        if name and name not in found:
            found.append(name)
        stack.extend((k, d + 1) for k in kids.get(cur, []))
    return found


def classify(session: dict, agents: list[str]) -> str:
    """What a session is for: a telerouter agent session, an AI agent, a plain shell or something else."""
    if session["name"].startswith(INTERNAL_PREFIX):
        return "agent-session"
    for a in agents:
        if a in AGENT_BINS:
            return a
    return "shell" if session.get("command", "") in SHELLS else "other"


def label(kind: str) -> str:
    return AGENT_LABELS.get(kind, "shell" if kind == "shell" else "other")


# -- validation ------------------------------------------------------------------------------------------
def clamp_history(n: int) -> int:
    return max(0, min(int(n or 0), MAX_HISTORY))


def session_ok(name: str) -> bool:
    return bool(SESSION_RE.match(name or ""))


def new_name_ok(name: str) -> bool:
    return bool(NEW_NAME_RE.match((name or "").strip())) and not (name or "").startswith(INTERNAL_PREFIX)


def folder_ok(folder: str) -> bool:
    f = " ".join((folder or "~").split()) or "~"
    return bool(WORKDIR_RE.match(f)) and ".." not in f.split("/")
