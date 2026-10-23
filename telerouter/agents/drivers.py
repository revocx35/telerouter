"""Per-agent-kind details: files to install on the agent host, the launch command, and reply events.

Both agents run interactively in tmux, so the owner can attach and watch or type. telerouter types the
voice request into the pane and learns the answer from the agent's own hook system:

* Claude Code: `--settings` adds UserPromptSubmit/Stop/Notification hooks; Stop carries
  `last_assistant_message`. Nothing in the owner's ~/.claude is changed.
* Hermes: shell hooks (`post_llm_call` carries `assistant_response`) and the MCP server are declared in
  ~/.hermes/config.yaml, which telerouter edits once (with a backup) when it sets the agent up.

The hook script only appends the event JSON (one line) to a file; telerouter tails it over SSH.
"""

from __future__ import annotations

import json
import shlex

from ..models import Agent

HOOK_SH = """#!/bin/sh
# telerouter agent hook: append the event (one JSON line) for telerouter to read over SSH.
# The turn's conversation_history is dropped: telerouter never reads it and it makes every event ~1 MB,
# big enough to hit the reader's line limit once a conversation has some history. A line that cannot be
# trimmed is appended as it came: the hook must never break the agent.
EV=$EVENTS$
LINE=$(printf '%s' "$(cat)" | tr -d '\n\r')
SMALL=""
if command -v python3 >/dev/null 2>&1; then
  SMALL=$(printf '%s' "$LINE" | python3 -c 'import json, sys
try:
    d = json.loads(sys.stdin.read())
except ValueError:
    raise SystemExit(1)
extra = d.get("extra")
if isinstance(extra, dict):
    extra.pop("conversation_history", None)
sys.stdout.write(json.dumps(d))' 2>/dev/null) || SMALL=""
fi
printf '%s\n' "${SMALL:-$LINE}" >> "$EV" 2>/dev/null
exit 0
"""

PATHS = 'export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.hermes/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"'


def voice_instructions(agent: Agent, tools: bool = True) -> str:
    text = f"""You are also "{agent.name}", the voice assistant of telerouter (a smart-speaker and phone router).
Messages that start with "[telerouter ..." were spoken by a person to a speaker in their home or on a phone
call, transcribed by Whisper (so expect transcription errors), and your final answer will be read aloud by
a text-to-speech voice.

For those messages:
- Answer in plain spoken language: one to three short sentences, no markdown, no lists, no code, no URLs,
  no emoji. Spell out symbols and units. Ask a short question back if the request is unclear.
- If the work takes longer, still finish with a short spoken summary of the result.
- If an earlier spoken request got no answer from you (e.g. you hit a usage limit), do not carry it out
  later on your own: only act on what the person asks now.
- Messages headed "[telerouter #... · scheduled task ...]" are not spoken: they are tasks the owner scheduled
  in telerouter, and nobody is waiting on a speaker or the phone. Do the work completely, then end with a
  short plain summary of the result (it is stored as the run's result).
- Messages without that prefix were typed in this terminal: answer them normally."""
    if tools:
        text += """

You have the "telerouter" MCP tools: contacts (people, phone extensions, WhatsApp contacts, cameras),
speakers, and calls. Contacts are named by their "referred name" (e.g. "dad").
- "call my dad" from a speaker: use connect_call with that contact; the speaker the request came from is
  used automatically. If they name a speaker ("call my dad on the speaker in my room"), pass it as `speaker`
  (also when the request came by phone). Then just say who you are calling, in a few words.
- Asked from the phone app, a call rings their own phone first (setting "ring me first") and connects them
  when they answer: it is the default there, so call connect_call as usual and say it in a few words, e.g.
  "Ringing you, then dad." Pass ring_first: true to ring them first from any speaker; ring_first: false
  keeps the whole call on the speaker.
- To deliver a message by phone yourself, use phone_message (it rings, speaks, can wait for a reply).
- For an interactive phone conversation you lead yourself, use start_voice_call, then say/listen, then hang_up.
- announce / ask_speaker talk through a speaker in the house.
- tmux contacts are the person's own agent sessions (e.g. "wa_logger"). "Connect me to the X tmux / session /
  project": use telerouter's connect_call with that tmux contact; from then on their voice goes to that
  session until they say "back to <your name>". This is how voice reaches another session: do NOT use Claude
  Code's own session tools (ListAgents, SendMessage, peer sessions) for it, and don't do that session's work
  yourself.
- "Start / open a new Claude (Code) session called X (in folder Y)": use new_session. It starts a new tmux
  session with a fresh agent on a machine and connects the person to it, like connect_call does for a tmux
  contact. Use a short name made of letters, digits, - and _. Then tell them in a few words that they are
  talking to the new session now and how to come back to you.
- Spoken names arrive garbled by speech recognition ("Tether Outer T-Max" = "telerouter tmux", "WA logger" =
  "wa_logger"). Check list_contacts (kind "tmux" for sessions) and pick the closest name; ask only if two
  are equally close.
Never call anyone the person didn't ask you to call.
- Your context size is shown in voice request headers ("context 112k tokens"); context_status tells it too.
  reset_context empties your conversation right after your current reply, like /compact or /clear: use it
  only when the user asked you to manage your context, and follow their rules. Prefer mode "compact" with
  `keep` naming everything still in progress (projects, open tasks, preferences that matter); use "clear"
  only when nothing is ongoing. Save anything you must remember long-term to memory before a clear."""
    extra = [s for s in agent.mcp_servers if s.enabled]
    if extra:
        text += "\n\nYou also have these MCP servers:"
        for s in extra:
            text += f"\n- \"{s.name}\"" + (f": {s.description}" if s.description else "")
        text += ("\nFor smart-home requests (lights, switches, covers, climate, \"is the door locked\") use "
                 "those tools right away instead of asking, then confirm in a few words (\"Done, the kitchen "
                 "light is on.\"). If the device name is unclear, ask which one.")
    if agent.extra_instructions.strip():
        text += "\n\n" + agent.extra_instructions.strip()
    return text


class Driver:
    kind = ""

    def __init__(self, agent: Agent, dir_: str, workdir: str, mcp_url: str):
        self.agent = agent
        self.dir = dir_
        self.workdir = workdir
        self.mcp_url = mcp_url

    @property
    def extra_servers(self):
        return [s for s in self.agent.mcp_servers if s.enabled]

    @property
    def events_path(self) -> str:
        return f"{self.dir}/events.jsonl"

    @property
    def hook_path(self) -> str:
        return f"{self.dir}/hook.sh"

    def hook_script(self) -> str:
        return HOOK_SH.replace("$EVENTS$", shlex.quote(self.events_path))

    def files(self, mcp_token: str) -> dict[str, tuple[str, int]]:
        """Remote files (path -> (content, mode)) written when the agent session is (re)started."""
        return {self.hook_path: (self.hook_script(), 0o700)}

    def launch_script(self) -> str:
        raise NotImplementedError

    def setup_script(self) -> str:
        """Shell run once per start, before launching (e.g. config edits). Empty: nothing."""
        return ""

    def prompt_text(self, text: str, header: str) -> str:
        return f"[{header}] {text}"

    def dialog_keys(self, screen: str) -> list[str] | None:
        """Keys to press for a start-up dialog visible on screen, or None."""
        return None

    def reset_keys(self, mode: str, keep: str) -> list[str]:
        """Text/keys that clear or compact the agent's context ([] = not supported)."""
        return []


class ClaudeDriver(Driver):
    kind = "claude"

    def reset_keys(self, mode: str, keep: str) -> list[str]:
        return claude_reset_keys(mode, keep)

    def files(self, mcp_token: str) -> dict[str, tuple[str, int]]:
        hook = shlex.quote(self.hook_path)
        handler = [{"hooks": [{"type": "command", "command": hook, "timeout": 10}]}]
        settings = {
            "hooks": {"UserPromptSubmit": handler, "Stop": handler, "Notification": handler,
                      "SessionStart": handler},
            "permissions": {"allow": ["mcp__telerouter"] + [f"mcp__{s.name}" for s in self.extra_servers
                                                              if s.auto_approve]},
        }
        mcp = {"mcpServers": {"telerouter": {"type": "http", "url": self.mcp_url,
                                             "headers": {"Authorization": f"Bearer {mcp_token}"}}}}
        for s in self.extra_servers:
            entry: dict = {"type": "http", "url": s.url}
            if s.token:
                entry["headers"] = {"Authorization": f"Bearer {s.token}"}
            mcp["mcpServers"][s.name] = entry
        out = super().files(mcp_token)
        out[f"{self.dir}/settings.json"] = (json.dumps(settings, indent=1), 0o600)
        out[f"{self.dir}/mcp.json"] = (json.dumps(mcp, indent=1), 0o600)
        out[f"{self.dir}/instructions.md"] = (voice_instructions(self.agent), 0o600)
        out[f"{self.dir}/start.sh"] = (self.launch_script(), 0o700)
        # pinned CAs of extra MCP servers with private certificates: Claude Code (Node/Bun) trusts them
        # through NODE_EXTRA_CA_CERTS, in addition to the public CAs
        out[self.ca_path] = ("".join(s.tls_ca for s in self.extra_servers if s.tls_ca), 0o600)
        return out

    @property
    def ca_path(self) -> str:
        return f"{self.dir}/extra-ca.pem"

    def launch_script(self) -> str:
        a = self.agent
        uuid = a.session_uuid
        args = []
        if a.model:
            args += ["--model", a.model]
        if a.effort:
            args += ["--effort", a.effort]
        if a.permission_mode and a.permission_mode != "default":
            args += ["--permission-mode", a.permission_mode]
        d = shlex.quote(self.dir)
        return f"""#!/bin/sh
# started by telerouter in tmux session {a.session_name}; attach with: tmux attach -t {a.session_name}
{PATHS}
DIR={d}
[ -s "$DIR/extra-ca.pem" ] && export NODE_EXTRA_CA_CERTS="$DIR/extra-ca.pem"
cd {shlex.quote(self.workdir)} 2>/dev/null || cd "$HOME"
if ls "$HOME"/.claude/projects/*/{uuid}.jsonl >/dev/null 2>&1; then SESS="--resume {uuid}"; else SESS="--session-id {uuid}"; fi
# without this, a resumed conversation keeps the system prompt it started with (our instructions would go stale)
SNAP=""; claude --help 2>/dev/null | grep -q -- --system-prompt-snapshot && SNAP="--system-prompt-snapshot off"
claude $SESS $SNAP {" ".join(shlex.quote(x) for x in args)} \\
  --settings "$DIR/settings.json" --mcp-config "$DIR/mcp.json" \\
  --append-system-prompt "$(cat "$DIR/instructions.md")" -n telerouter
echo "agent exited with status $?"
sleep 3
"""

    def dialog_keys(self, screen: str) -> list[str] | None:
        return claude_dialog_keys(screen)


def claude_reset_keys(mode: str, keep: str) -> list[str]:
    """What to type to clear or compact a Claude Code conversation."""
    if mode == "clear":
        return ["/clear", "Enter"]
    if mode == "compact":
        keep = " ".join(keep.split())[:500]
        return [f"/compact {keep}".strip(), "Enter"]
    return []


def claude_dialog_keys(screen: str) -> list[str] | None:
    """Keys for a Claude Code start-up dialog (workspace trust, permission-mode confirmations), or None."""
    s = screen.lower()
    if "trust this folder" in s or "trust the files in this folder" in s:
        return menu_keys(screen, "yes, i trust") or menu_keys(screen, "yes, proceed") or menu_keys(screen, "yes")
    if "bypass permissions mode" in s and "yes, i accept" in s:
        return menu_keys(screen, "yes, i accept")
    if "auto mode" in s and "yes, enable" in s:
        return menu_keys(screen, "yes, enable")
    return None


def menu_keys(screen: str, target: str) -> list[str]:
    """Keys that move a TUI menu's cursor (the line with ❯) to the option containing `target`; Enter only
    once the screen shows the cursor on it.

    Claude Code's menus put one option per line; which option is preselected differs between versions
    (the workspace-trust dialog preselects "No, exit"), so never just press Enter. The moves and the Enter
    go out on separate looks at the screen: Claude Code 2.1.30x draws the trust dialog a moment before it
    reads keys, drops an arrow sent then but not the Enter after it, and "No, exit" quits the session.
    """
    lines = screen.splitlines()
    sel = next((i for i, ln in enumerate(lines) if "\u276f" in ln or ln.lstrip().startswith(">")), None)
    tgt = next((i for i, ln in enumerate(lines) if target in ln.lower()), None)
    if sel is None or tgt is None:
        return []
    d = tgt - sel
    return ["Enter"] if d == 0 else ["Down"] * d if d > 0 else ["Up"] * -d


class HermesDriver(Driver):
    kind = "hermes"

    def reset_keys(self, mode: str, keep: str) -> list[str]:
        if mode == "compact":                        # gateway CLI: /compress [<focus>]
            keep = " ".join(keep.split())[:500]
            return [f"/compress {keep}".strip(), "Enter"]
        return []

    def files(self, mcp_token: str) -> dict[str, tuple[str, int]]:
        out = super().files(mcp_token)
        out[f"{self.dir}/mcp-token"] = (mcp_token, 0o600)
        extra = {s.name: {"url": s.url, **({"headers": {"Authorization": f"Bearer {s.token}"}} if s.token else {}),
                          "timeout": 120} for s in self.extra_servers}
        out[f"{self.dir}/mcp-extra.json"] = (json.dumps(extra), 0o600)
        out[f"{self.dir}/configure.py"] = (HERMES_CONFIGURE, 0o600)
        out[f"{self.dir}/start.sh"] = (self.launch_script(), 0o700)
        return out

    def setup_script(self) -> str:
        d = shlex.quote(self.dir)
        return f"""{PATHS}
PY=""
for p in "$HOME/.hermes/hermes-agent/venv/bin/python" "$HOME/.hermes/hermes-agent/.venv/bin/python" python3; do
  if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import yaml' 2>/dev/null; then PY="$p"; break; fi
done
[ -n "$PY" ] || {{ echo "no python with PyYAML found to edit ~/.hermes/config.yaml" >&2; exit 3; }}
"$PY" {d}/configure.py {shlex.quote(self.mcp_url)} {d}/mcp-token {shlex.quote(self.hook_path)} {d}/mcp-extra.json
"""

    def launch_script(self) -> str:
        a = self.agent
        args = [f"telerouter-{a.id}", "--create-if-missing", "--accept-hooks"]
        if a.model:
            args += ["--model", a.model]
        if a.hermes_provider:
            args += ["--provider", a.hermes_provider]
        if a.permission_mode == "bypassPermissions":
            args += ["--yolo"]
        tail = " ".join(shlex.quote(x) for x in args)
        exit_file = shlex.quote(self.dir + "/last-exit")
        return f"""#!/bin/sh
# started by telerouter in tmux session {a.session_name}; attach with: tmux attach -t {a.session_name}
{PATHS}
export HERMES_ACCEPT_HOOKS=1
cd {shlex.quote(self.workdir)} 2>/dev/null || cd "$HOME"
rm -f {exit_file}
# Hermes 0.22 moved --create-if-missing under the `chat` subcommand; earlier CLIs take it at the top
# level, and one of them is all we need: probe instead of pinning a version. A launch that fails writes
# its exit status to last-exit so telerouter can report it instead of claiming the agent started.
if hermes chat --help 2>/dev/null | grep -q -- '--create-if-missing'; then
  hermes chat -c {tail}
elif hermes --help 2>/dev/null | grep -q -- '--create-if-missing'; then
  hermes -c {tail}
else
  echo "telerouter: this hermes CLI accepts --create-if-missing neither after 'chat' nor at the top level" >&2
  printf '%s\\n' 3 > {exit_file}
  sleep "${{TELEROUTER_EXIT_SLEEP:-3}}"
  exit 3
fi
status=$?
echo "agent exited with status $status"
printf '%s\\n' "$status" > {exit_file}
sleep "${{TELEROUTER_EXIT_SLEEP:-3}}"
"""

    def prompt_text(self, text: str, header: str) -> str:
        # Hermes gets no system-prompt flag from us: the voice rules travel with each request.
        return (f"[{header}] {text} -- (Spoken request: answer in one to three short plain sentences for "
                f"text-to-speech, no markdown. telerouter MCP tools are available for calls and speakers.)")


# Runs on the agent host with Hermes' own Python: adds the telerouter MCP server and hook to
# ~/.hermes/config.yaml (backup first), and allow-lists the hook command.
HERMES_CONFIGURE = r'''
import json, os, shutil, sys, time
import yaml
url, token_file, hook = sys.argv[1], sys.argv[2], sys.argv[3]
home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
path = os.path.join(home, "config.yaml")
os.makedirs(home, exist_ok=True)
cfg = {}
if os.path.exists(path):
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
token = open(token_file).read().strip()
want_mcp = {"url": url, "headers": {"Authorization": "Bearer " + token}, "timeout": 300}
servers = cfg.setdefault("mcp_servers", {}) or {}
cfg["mcp_servers"] = servers
hooks = cfg.setdefault("hooks", {}) or {}
cfg["hooks"] = hooks
entries = hooks.setdefault("post_llm_call", []) or []
hooks["post_llm_call"] = entries
changed = servers.get("telerouter") != want_mcp
servers["telerouter"] = want_mcp
# extra servers telerouter manages (e.g. Home Assistant); names it added before but no longer has are removed
extra = json.load(open(sys.argv[4])) if len(sys.argv) > 4 and os.path.exists(sys.argv[4]) else {}
managed_file = os.path.join(os.path.dirname(token_file), "hermes-managed.json")
managed = set(json.load(open(managed_file))) if os.path.exists(managed_file) else set()
for name in managed - set(extra):
    if servers.pop(name, None) is not None:
        changed = True
for name, entry in extra.items():
    if servers.get(name) != entry:
        servers[name] = entry
        changed = True
with open(managed_file, "w") as f:
    json.dump(sorted(extra), f)
for ev in ("post_llm_call", "on_session_end"):
    lst = hooks.setdefault(ev, []) or []
    hooks[ev] = lst
    if not any(isinstance(e, dict) and e.get("command") == hook for e in lst):
        lst.append({"command": hook, "timeout": 10})
        changed = True
if changed:
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak-telerouter-" + time.strftime("%Y%m%d%H%M%S"))
    tmp = path + ".tmp-telerouter"
    with open(tmp, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
allow = os.path.join(home, "shell-hooks-allowlist.json")
data = {"approvals": []}
if os.path.exists(allow):
    try:
        data = json.load(open(allow)) or data
    except ValueError:
        pass
apps = data.setdefault("approvals", [])
for ev in ("post_llm_call", "on_session_end"):
    if not any(a.get("event") == ev and a.get("command") == hook for a in apps):
        apps.append({"event": ev, "command": hook})
with open(allow + ".tmp", "w") as f:
    json.dump(data, f, indent=1)
os.replace(allow + ".tmp", allow)
print("ok" + (" (config.yaml updated)" if changed else ""))
'''


def make_driver(agent: Agent, dir_: str, workdir: str, mcp_url: str) -> Driver:
    cls = ClaudeDriver if agent.kind == "claude" else HermesDriver
    return cls(agent, dir_, workdir, mcp_url)
