"""Machines: tmux sessions as connections, routing a speaker/call to one, leaving it."""

import json

from telerouter.agents.machine import MachineRuntime, Route, is_exit
from telerouter.agents.runtime import turn_end_without_stop
from telerouter.models import Agent, Config, Machine
from telerouter.store import migrate


def test_exit_phrases_only_short_and_explicit():
    for t in ("Back to Jarvis.", "back to the assistant please", "Disconnect.", "leave the session", "Exit tmux"):
        assert is_exit(t, "Jarvis"), t
    for t in ("go back to the login page and fix the button", "back to work on the parser", "deploy it",
              "Now go back to the previous commit and disconnect the old database from the app"):
        assert not is_exit(t, "Jarvis"), t
    assert is_exit("back to friday", "Friday")


def test_agents_move_onto_machines():
    cfg = Config(agents=[Agent(name="J", host="10.0.0.5", username="root", host_key="ssh-ed25519 AAAA", kind="claude")])
    assert migrate(cfg)
    m = cfg.machines[0]
    assert (m.host, m.username, m.host_key, m.agent_kind) == ("10.0.0.5", "root", "ssh-ed25519 AAAA", "claude")
    assert cfg.agents[0].machine_id == m.id and not migrate(cfg)


class _Store:
    def __init__(self):
        self.config = Config()

    def save(self):
        pass


class _Eng:
    def __init__(self):
        self.store = _Store()
        self.merged = None

    def merge_tmux_sessions(self, m, found):
        self.merged = found


async def test_tmux_listing_skips_telerouter_sessions():
    rt = MachineRuntime(_Eng(), Machine(name="lxc", host="h", username="u"))

    async def fake_sh(cmd, input=None, timeout=30):
        assert "|~|" in cmd
        return ("wa_logger|~|claude|~|/root\ntelerouter-0a82|~|sh|~|/root/jarvis\nbad name!|~|x|~|y\n"
                "pokerbalance|~|claude|~|/root/pokerbalance\ncalls_claude_/root/wa_logger\n")
    rt._sh = fake_sh
    found = await rt.list_sessions()
    assert [s["name"] for s in found] == ["wa_logger", "pokerbalance"]


async def test_claude_session_reply_from_transcript():
    rt = MachineRuntime(_Eng(), Machine(name="lxc", host="h", username="u"))
    rt.state = "ready"
    calls = {"tail": 0}
    nonce_box = {}

    async def fake_sh(cmd, input=None, timeout=30):
        if "sessions/$c.json" in cmd:
            return json.dumps({"sessionId": "26e646fc-fdd6-4f2f-ab9d-ff9e40dbc292"})
        if cmd.startswith("ls -t"):
            return "/root/.claude/projects/-root/26e646fc-fdd6-4f2f-ab9d-ff9e40dbc292.jsonl\n"
        if cmd.startswith("wc -l"):
            return "120\n"
        if "set-buffer" in cmd:
            nonce_box["n"] = cmd.split("#", 1)[1][:6]
            return ""
        if cmd.startswith("tail -n +121"):
            calls["tail"] += 1
            n = nonce_box["n"]
            lines = [json.dumps({"type": "user", "message": {"content": f"[telerouter #{n} · Kitchen] how is it going"}}),
                     json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Tests pass."}]}})]
            if calls["tail"] > 1:
                lines.append(json.dumps({"type": "system", "subtype": "turn_duration"}))
            return "\n".join(lines)
        return ""
    rt._sh = fake_sh
    import asyncio
    orig = asyncio.sleep

    async def fast_sleep(s):
        await orig(0)
    asyncio.sleep = fast_sleep
    try:
        reply = await rt.ask("wa_logger", "how is it going", "Kitchen speaker")
    finally:
        asyncio.sleep = orig
    assert reply == "Tests pass." and calls["tail"] == 2


def test_route_expiry():
    r = Route("m1", "wa_logger", "wa_logger")
    assert not r.expired()
    r.last -= 16 * 60
    assert r.expired()


def test_claude_project_dir_naming():
    from telerouter.agents.machine import claude_project_dir
    assert claude_project_dir("/root/jarvis") == "-root-jarvis"
    assert claude_project_dir("/tmp/claude-0/-root-telerouter/x.y/cltest") == "-tmp-claude-0--root-telerouter-x-y-cltest"


def _fast_sleep():
    import asyncio
    orig = asyncio.sleep

    async def fast(s):
        await orig(0)
    return orig, fast


async def test_new_session_starts_claude_in_tmux_and_answers_the_trust_dialog():
    import asyncio
    rt = MachineRuntime(_Eng(), Machine(name="lxc", host="h", username="u", allow_new_sessions=True,
                                        new_session_model="claude-sonnet-5-5"))
    rt.state = "ready"
    no = "Do you trust the files in this folder?\n❯ 1. No, exit\n  2. Yes, I trust this folder\n"
    yes = "Do you trust the files in this folder?\n  1. No, exit\n❯ 2. Yes, I trust this folder\n"
    # the first Down is dropped (Claude Code draws the dialog before it reads keys): same screen 3 more times
    sent, screens = [], [no, no, no, no, yes, "│ > │", "│ > │", "│ > │", "│ > │"]
    made = {}

    async def fake_sh(cmd, input=None, timeout=30):
        if "list-sessions" in cmd:
            return "wa_logger|~|claude|~|/root\n" + ("pokerbot|~|claude|~|/root/projects/pokerbot\n" if made else "")
        if "new-session" in cmd:
            made["cmd"] = cmd
            return "ok /root/projects/pokerbot"
        if "capture-pane" in cmd:
            return screens.pop(0) if len(screens) > 1 else screens[0]
        if "send-keys" in cmd:
            sent.append(cmd.rsplit(" ", 1)[1])
            return ""
        if "sessions/$c.json" in cmd:
            return json.dumps({"sessionId": "26e646fc-fdd6-4f2f-ab9d-ff9e40dbc292", "cwd": "/root/projects/pokerbot"})
        if cmd.startswith("ls -t"):
            return ""
        return ""
    rt._sh = fake_sh
    rt.home = "/root"
    orig, fast = _fast_sleep()
    asyncio.sleep = fast
    try:
        info = await rt.new_session("pokerbot", "~/projects/pokerbot")
    finally:
        asyncio.sleep = orig
    assert info["name"] == "pokerbot" and info["path"] == "/root/projects/pokerbot"
    cmd = made["cmd"]
    assert "D=\"$HOME\"/projects/pokerbot" in cmd and "tmux new-session -d -s pokerbot -c \"$D\"" in cmd
    assert "claude -n pokerbot --model claude-sonnet-5-5 --permission-mode auto" in cmd
    assert sent == ["Down", "Down", "Enter"]          # moved off "No, exit"; Enter only once "Yes" is selected


async def test_new_session_stops_answering_once_claude_exited():
    import asyncio
    rt = MachineRuntime(_Eng(), Machine(name="lxc", host="h", username="u", allow_new_sessions=True))
    rt.state = "ready"
    sent = []
    dialog = "Do you trust the files in this folder?\n❯ 1. No, exit\n  2. Yes, I trust this folder\n"

    async def fake_sh(cmd, input=None, timeout=30):
        if "new-session" in cmd:
            return "ok /root"
        if "capture-pane" in cmd:                     # the dialog text stays above the shell prompt
            return dialog + "the agent exited\nroot@lxc:~#\n" + "root@lxc:~#\n" * len(sent)
        if "send-keys" in cmd:
            sent.append(cmd.rsplit(" ", 1)[1])
        return ""
    rt._sh = fake_sh
    orig, fast = _fast_sleep()
    asyncio.sleep = fast
    try:
        await rt.new_session("homelab", "~")
    finally:
        asyncio.sleep = orig
    assert sent == []                                 # no keys typed into the shell


async def test_new_session_refusals():
    from telerouter.agents.runtime import AgentError
    rt = MachineRuntime(_Eng(), Machine(name="lxc", host="h", username="u"))
    rt.state = "ready"

    async def fake_sh(cmd, input=None, timeout=30):
        assert "new-session" not in cmd
        return "wa_logger|~|claude|~|/root\n"
    rt._sh = fake_sh
    for name, folder, why in (("x", "~", "not allowed"), ):
        try:
            await rt.new_session(name, folder)
            raise AssertionError("expected a refusal")
        except AgentError as e:
            assert why in str(e)
    rt.machine.allow_new_sessions = True
    for name, folder, why in (("wa_logger", "~", "already exists"), ("telerouter-x", "~", "names"),
                              ("bad name", "~", "names"), ("ok", "~/../etc", "plain path"), ("ok", "$(rm -rf ~)", "plain path"),
                              ("ok", "~/a;b", "plain path")):
        try:
            await rt.new_session(name, folder)
            raise AssertionError(f"expected a refusal for {name} {folder}")
        except AgentError as e:
            assert why in str(e), (name, folder, str(e))


async def test_hermes_event_reader_takes_the_turn_with_our_nonce():
    import subprocess
    import sys
    from telerouter.agents.machine import HERMES_EVENT_READER
    other = {"hook_event_name": "post_llm_call",
             "extra": {"user_message": "[telerouter #aaaaaa · other] x", "assistant_response": "WRONG"}}
    mine = {"hook_event_name": "post_llm_call",
            "extra": {"user_message": "[telerouter #bbbbbb · scheduled task] do it",
                      "assistant_response": "All four apps are fine."}}
    end = {"hook_event_name": "on_session_end", "extra": {"completed": "true"}}
    out = subprocess.run([sys.executable, "-c", HERMES_EVENT_READER, "bbbbbb"],
                         input="\n".join(json.dumps(e) for e in (other, mine, end)),
                         text=True, capture_output=True, check=True)
    assert json.loads(out.stdout)["reply"] == "All four apps are fine."


async def test_hermes_reply_comes_from_its_hook_events_not_the_screen():
    """A Hermes turn's answer is its post_llm_call event: the pane holds reasoning, tool calls and TUI chrome."""
    rt = MachineRuntime(_Eng(), Machine(name="lxc", host="h", username="u", agent_kind="hermes"))
    rt.state, rt.home = "ready", "/root"
    box = {"nonce": "", "polls": 0}

    async def fake_sh(cmd, input=None, timeout=30):
        if cmd.startswith("export") and "ls -t" in cmd:
            return "/root/.telerouter/e9a5e65a/events.jsonl\n"
        if "cat >" in cmd and "hermes-event-reader.py" in cmd:
            return "ok\n"
        if cmd.startswith("wc -c"):
            return "120\n"
        if "set-buffer" in cmd:
            box["nonce"] = cmd.split("#", 1)[1][:6]
            return ""
        if cmd.startswith("F=120"):
            box["polls"] += 1
            return "" if box["polls"] < 2 else json.dumps({"reply": "Nothing new broke today."}) + "\n"
        assert "capture-pane" not in cmd, cmd          # the screen must not be read at all
        return ""

    rt._sh = fake_sh
    reply = await rt.ask("compatibility", "check the apps", "", header='scheduled task "compatibility"',
                         timeout=20)
    assert reply == "Nothing new broke today."


async def test_hermes_without_hook_events_still_answers_from_the_screen():
    rt = MachineRuntime(_Eng(), Machine(name="lxc", host="h", username="u", agent_kind="hermes"))
    rt.state = "ready"
    seen = {"pane": 0}

    async def fake_sh(cmd, input=None, timeout=30):
        if cmd.startswith("export") and "ls -t" in cmd:
            return ""                                   # no telerouter hooks on that host
        if "set-buffer" in cmd:
            return ""
        if "capture-pane" in cmd:
            seen["pane"] += 1
            return "old prompt\n" if seen["pane"] == 1 else "old prompt\nHere is the answer.\n"
        return ""

    rt._sh = fake_sh
    reply = await rt.ask("pokerbalance", "how is it going", "", timeout=20)
    assert "Here is the answer." in reply


async def test_a_new_hermes_session_is_settled_before_typing():
    rt = MachineRuntime(_Eng(), Machine(name="lxc", host="h", username="u", agent_kind="hermes"))
    rt.state = "ready"
    screens = ["booting", "booting", "menu", "menu", "menu", "menu", "menu"]

    async def fake_sh(cmd, input=None, timeout=30):
        if "capture-pane" in cmd:
            return screens.pop(0) if screens else "menu"
        return ""
    rt._sh = fake_sh
    await rt._settle_hermes("newtask")
    assert screens == ["menu"]                          # three identical screens in a row = settled


async def test_hermes_machine_reads_context_from_the_pane_and_clears_with_new():
    rt = MachineRuntime(_Eng(), Machine(name="lxc", host="h", username="u", agent_kind="hermes"))
    sent = []

    async def fake_sh(cmd, input=None, timeout=30):
        sent.append(cmd)
        if "capture-pane" in cmd:
            return "\u276f\n \u2624 deepseek-flash \u00b7 ~75.5K/1M \u00b7 [\u2588\u2591] ~7%\n"
        return ""
    rt._sh = fake_sh
    assert await rt.context_tokens("task-fdroid") == 75500
    assert not any("claude" in c for c in sent)                  # no Claude transcript lookup on Hermes
    await rt.reset_context("task-fdroid", "clear")
    typed = [c for c in sent if "send-keys" in c]
    assert "/new" in typed[0] and "/clear" not in typed[0] and typed[-1].endswith("Enter")


TEACH = ("   Teach auto mode about your environment?\n   Claude Code reads this project, your recent Claude sessions\n"
         "   ❯ Also scan shell history     true\n     Continue\n   ←/→ to change · Enter to continue · Esc to cancel\n")


async def test_claude_dialog_is_closed_before_the_prompt_and_a_slow_poll_is_not_fatal():
    """2026-10-31: Claude Code's "Teach auto mode" offer took a scheduled run's prompt, and one SSH poll that
    took over 30 s on a busy host ended the run with a bare TimeoutError."""
    import asyncio
    rt = MachineRuntime(_Eng(), Machine(name="lxc", host="h", username="u"))
    rt.state = "ready"
    sent, box = [], {"screen": TEACH, "tail": 0}

    async def fake_sh(cmd, input=None, timeout=30):
        sent.append(cmd)
        if "sessions/$c.json" in cmd:
            return json.dumps({"sessionId": "26e646fc-fdd6-4f2f-ab9d-ff9e40dbc292"})
        if cmd.startswith("ls -t"):
            return "/root/.claude/projects/-root/26e646fc-fdd6-4f2f-ab9d-ff9e40dbc292.jsonl\n"
        if cmd.startswith("wc -l"):
            return "120\n"
        if "capture-pane" in cmd:
            return box["screen"]
        if "send-keys" in cmd and "Escape" in cmd:
            box["screen"] = "❯ \n"
            return ""
        if "set-buffer" in cmd:
            assert box["screen"] != TEACH, "typed into the dialog"
            box["n"] = cmd.split("#", 1)[1][:6]
            return ""
        if cmd.startswith("tail -n +121"):
            box["tail"] += 1
            if box["tail"] == 1:
                raise asyncio.TimeoutError()                     # one hung poll
            return "\n".join([
                json.dumps({"type": "user", "message": {"content": f"[telerouter #{box['n']} · task] check"}}),
                json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "All fine."}]}}),
                json.dumps({"type": "system", "subtype": "turn_duration"})])
        return ""
    rt._sh = fake_sh
    orig, fast = _fast_sleep()
    asyncio.sleep = fast
    try:
        reply = await rt.ask("compatibility", "check", "", header='scheduled task "Compatibility"', timeout=60)
    finally:
        asyncio.sleep = orig
    assert reply == "All fine." and box["tail"] == 2
    assert any("send-keys" in c and "Escape" in c for c in sent)


async def test_a_frozen_host_and_a_dropped_connection_do_not_fail_the_run():
    """2026-11-04: the 4-hourly Proxmox backup froze the agent host's LXC for 76 s (suspend mode). Polls hung, the SSH
    connection dropped, and the "was the prompt ever accepted?" look-again (a plain _sh) raised TimeoutError,
    failing the Compatibility run while Claude kept working. Now nothing seen during the freeze counts."""
    import asyncio
    import asyncssh
    from telerouter.agents import machine as mod
    rt = MachineRuntime(_Eng(), Machine(name="claude", host="h", username="u"))
    rt.state = "ready"
    clock = {"t": 1000.0}
    box = {"tail": 0, "looks": 0}
    path = "/root/.claude/projects/-root/26e646fc-fdd6-4f2f-ab9d-ff9e40dbc292.jsonl\n"

    async def fake_sh(cmd, input=None, timeout=30):
        frozen = 1 <= box["tail"] <= 25
        if "sessions/$c.json" in cmd:
            if frozen:
                box["looks"] += 1
                raise asyncio.TimeoutError()             # the look-again while frozen
            return json.dumps({"sessionId": "26e646fc-fdd6-4f2f-ab9d-ff9e40dbc292"})
        if cmd.startswith("ls -t"):
            return path
        if cmd.startswith("wc -l"):
            return "120\n"
        if "capture-pane" in cmd:
            if frozen:
                raise asyncio.TimeoutError()
            return "❯ \n"
        if "set-buffer" in cmd:
            box["n"] = cmd.split("#", 1)[1][:6]
            return ""
        if cmd.startswith("tail -n +121"):
            box["tail"] += 1
            clock["t"] += 3                                # each poll is a few seconds of wall time
            if box["tail"] <= 10:
                raise asyncio.TimeoutError()              # hung polls
            if box["tail"] <= 20:
                raise asyncssh.ConnectionLost("connection lost")   # keepalive gave up, reconnecting
            if box["tail"] <= 25:
                return ""                                 # answers, but the transcript lookup still hangs
            return "\n".join([
                json.dumps({"type": "user", "message": {"content": f"[telerouter #{box['n']} · task] check"}}),
                json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "All fine."}]}}),
                json.dumps({"type": "system", "subtype": "turn_duration"})])
        return ""
    rt._sh = fake_sh
    orig, fast = _fast_sleep()
    orig_mono = mod.time.monotonic
    asyncio.sleep = fast
    mod.time.monotonic = lambda: clock["t"]
    try:
        reply = await rt.ask("compatibility", "check", "", header='scheduled task "Compatibility"', timeout=3600)
    finally:
        asyncio.sleep = orig
        mod.time.monotonic = orig_mono
    assert reply == "All fine." and box["tail"] == 26 and box["looks"] >= 1


def test_ssh_keepalive_outlasts_a_backup_freeze():
    from telerouter.agents import ssh
    assert 20 * ssh.KEEPALIVE_COUNT_MAX >= 150


def test_teach_auto_mode_offer_is_dismissed():
    from telerouter.agents.drivers import claude_dialog_keys
    assert claude_dialog_keys(TEACH) == ["Escape"]
    assert claude_dialog_keys("❯ fix the tests\n") is None
