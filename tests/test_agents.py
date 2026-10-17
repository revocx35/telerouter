"""Agent drivers and reply matching (no SSH): launch scripts, hook files, event correlation, Hermes config."""

import asyncio
import json
import os
import subprocess
import sys

import pytest

from telerouter.agents.drivers import HERMES_CONFIGURE, ClaudeDriver, HermesDriver, make_driver
from telerouter.agents.runtime import AgentRuntime, Pending, Source
from telerouter.models import Agent


def agent(**kw):
    base = dict(name="Jarvis", host="10.0.0.5", username="me", host_key="ssh-ed25519 AAAA", session_uuid="u-1")
    base.update(kw)
    return Agent(**base)


def test_claude_launch_uses_model_effort_hooks_and_mcp():
    d = make_driver(agent(), "/home/me/.telerouter/x", "/home/me/proj", "http://127.0.0.1:47801/mcp")
    assert isinstance(d, ClaudeDriver)
    files = d.files("trm_secret")
    script = files["/home/me/.telerouter/x/start.sh"][0]
    assert "--model claude-sonnet-5-5" in script and "--effort medium" in script
    assert "--permission-mode auto" in script and "--session-id u-1" in script and "--resume u-1" in script
    settings = json.loads(files["/home/me/.telerouter/x/settings.json"][0])
    assert set(settings["hooks"]) == {"UserPromptSubmit", "Stop", "Notification", "SessionStart"}
    assert settings["permissions"]["allow"] == ["mcp__telerouter"]
    mcp = json.loads(files["/home/me/.telerouter/x/mcp.json"][0])
    assert mcp["mcpServers"]["telerouter"]["headers"]["Authorization"] == "Bearer trm_secret"
    assert files["/home/me/.telerouter/x/mcp.json"][1] == 0o600
    assert "trm_secret" not in script


def test_hook_script_appends_one_json_line(tmp_path):
    d = ClaudeDriver(agent(), str(tmp_path), str(tmp_path), "http://x")
    hook = tmp_path / "hook.sh"
    hook.write_text(d.hook_script())
    hook.chmod(0o700)
    for ev in ({"hook_event_name": "Stop", "last_assistant_message": "line1\nline2"}, {"a": 1}):
        subprocess.run([str(hook)], input=json.dumps(ev, indent=2).encode(), check=True)
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["last_assistant_message"] == "line1\nline2"


def test_hermes_launch_and_prompt():
    d = make_driver(agent(kind="hermes", model="anthropic/claude-sonnet-5-5", permission_mode="bypassPermissions"),
                    "/h/.telerouter/x", "/h", "http://127.0.0.1:47801/mcp")
    assert isinstance(d, HermesDriver)
    s = d.launch_script()
    assert "--create-if-missing" in s and "--accept-hooks" in s and "--yolo" in s
    assert "telerouter-" in s and "anthropic/claude-sonnet-5-5" in s
    assert d.prompt_text("hi", "telerouter #abc · Kitchen").startswith("[telerouter #abc · Kitchen] hi")


def test_hermes_configure_merges_config(tmp_path):
    yaml = pytest.importorskip("yaml")
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model: x\nmcp_servers:\n  github:\n    command: npx\n")
    tok = tmp_path / "tok"
    tok.write_text("trm_abc")
    script = tmp_path / "configure.py"
    script.write_text(HERMES_CONFIGURE)
    env = {**os.environ, "HERMES_HOME": str(home)}
    for _ in range(2):           # idempotent
        subprocess.run([sys.executable, str(script), "http://127.0.0.1:47801/mcp", str(tok), "/h/hook.sh"],
                       check=True, env=env, capture_output=True)
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    assert cfg["model"] == "x" and "github" in cfg["mcp_servers"]
    assert cfg["mcp_servers"]["telerouter"]["headers"]["Authorization"] == "Bearer trm_abc"
    assert [h["command"] for h in cfg["hooks"]["post_llm_call"]] == ["/h/hook.sh"]
    allow = json.loads((home / "shell-hooks-allowlist.json").read_text())
    assert {"event": "post_llm_call", "command": "/h/hook.sh"} in allow["approvals"]
    assert list(home.glob("config.yaml.bak-telerouter-*"))


class _Eng:
    pass


def runtime(kind="claude"):
    rt = AgentRuntime(_Engine2(), agent(kind=kind))
    return rt


async def test_claude_reply_matches_only_our_prompt():
    rt = runtime()
    fut = asyncio.get_running_loop().create_future()
    rt.pending = Pending("ab12", "hi", Source("speaker", "Kitchen"), fut)
    await rt._on_event({"hook_event_name": "UserPromptSubmit", "session_id": "S", "prompt": "typed by hand"})
    await rt._on_event({"hook_event_name": "Stop", "session_id": "S", "last_assistant_message": "not ours"})
    assert not fut.done()
    await rt._on_event({"hook_event_name": "UserPromptSubmit", "session_id": "S",
                        "prompt": "[telerouter #ab12 · Kitchen] hi"})
    await rt._on_event({"hook_event_name": "Stop", "session_id": "OTHER", "last_assistant_message": "wrong session"})
    assert not fut.done()
    await rt._on_event({"hook_event_name": "Stop", "session_id": "S", "stop_hook_active": False,
                        "last_assistant_message": "Paris."})
    assert fut.result() == "Paris."


async def test_claude_permission_notification_reaches_the_speaker():
    rt = runtime()
    notes = []

    async def note(msg):
        notes.append(msg)
    fut = asyncio.get_running_loop().create_future()
    rt.pending = Pending("cd34", "x", Source("speaker", "Kitchen"), fut)
    rt.pending.on_note = note
    await rt._on_event({"hook_event_name": "UserPromptSubmit", "session_id": "S", "prompt": "[telerouter #cd34] x"})
    await rt._on_event({"hook_event_name": "Notification", "session_id": "S", "message": "Claude needs permission"})
    assert notes == ["Claude needs permission"]


async def test_hermes_reply_matches_by_nonce():
    rt = runtime("hermes")
    fut = asyncio.get_running_loop().create_future()
    rt.pending = Pending("ef56", "x", Source("phone", "call"), fut)
    await rt._on_event({"hook_event_name": "post_llm_call", "session_id": "T",
                        "extra": {"user_message": "something else", "assistant_response": "no"}})
    assert not fut.done()
    await rt._on_event({"hook_event_name": "post_llm_call", "session_id": "T",
                        "extra": {"user_message": "[telerouter #ef56 · call] x", "assistant_response": "Done."}})
    assert fut.result() == "Done."


def test_trust_dialog_selects_yes_even_when_no_is_preselected():
    screen = """ Accessing workspace:
 /root/jarvis
 Quick safety check: Is this a project you created or one you trust?
 ❯ No, exit
   Yes, I trust this folder
 Enter to confirm · Esc to cancel"""
    d = ClaudeDriver(agent(), "/d", "/w", "http://x")
    assert d.dialog_keys(screen) == ["Down", "Enter"]
    assert d.dialog_keys(screen.replace("❯ No, exit", "  No, exit").replace("   Yes, I trust", " ❯ Yes, I trust")) == ["Enter"]
    old_style = "Do you trust the files in this folder?\n ❯ 1. Yes, proceed\n   2. No, exit"
    assert d.dialog_keys(old_style) == ["Enter"]    # older wording: "Yes, proceed" is already selected


def test_extra_mcp_servers_reach_claude_with_permissions_and_instructions():
    from telerouter.models import ExtraMcpServer
    a = agent(mcp_servers=[ExtraMcpServer(name="home-assistant", url="https://ha.example.com/api/mcp", token="llat",
                                          description="lights and switches"),
                           ExtraMcpServer(name="off", url="https://x.example/mcp", enabled=False),
                           ExtraMcpServer(name="ask", url="https://y.example/mcp", auto_approve=False)])
    files = make_driver(a, "/d", "/w", "http://127.0.0.1:47801/mcp").files("trm_x")
    mcp = json.loads(files["/d/mcp.json"][0])["mcpServers"]
    assert mcp["home-assistant"] == {"type": "http", "url": "https://ha.example.com/api/mcp",
                                     "headers": {"Authorization": "Bearer llat"}}
    assert "off" not in mcp and "headers" not in mcp["ask"]
    allow = json.loads(files["/d/settings.json"][0])["permissions"]["allow"]
    assert allow == ["mcp__telerouter", "mcp__home-assistant"]
    instr = files["/d/instructions.md"][0]
    assert '"home-assistant": lights and switches' in instr and "smart-home" in instr and "llat" not in instr


def test_extra_mcp_server_validation():
    from telerouter.models import ExtraMcpServer
    for bad in ({"name": "telerouter", "url": "https://a.b/mcp"}, {"name": "ha", "url": "http://192.168.1.2:8123/api/mcp"},
                {"name": "Bad Name", "url": "https://a.b"}):
        with pytest.raises(ValueError):
            ExtraMcpServer(**bad)
    with pytest.raises(ValueError):
        agent(mcp_servers=[{"name": "ha", "url": "https://a.b"}, {"name": "ha", "url": "https://c.d"}])


def test_hermes_configure_adds_and_removes_extra_servers(tmp_path):
    yaml = pytest.importorskip("yaml")
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text("mcp_servers:\n  mine:\n    command: x\n")
    d = tmp_path / "tr"
    d.mkdir()
    (d / "tok").write_text("trm_abc")
    script = d / "configure.py"
    script.write_text(HERMES_CONFIGURE)
    env = {**os.environ, "HERMES_HOME": str(home)}

    def run(extra):
        (d / "extra.json").write_text(json.dumps(extra))
        subprocess.run([sys.executable, str(script), "http://127.0.0.1:47801/mcp", str(d / "tok"), "/h/hook.sh",
                        str(d / "extra.json")], check=True, env=env, capture_output=True)
        return yaml.safe_load((home / "config.yaml").read_text())["mcp_servers"]

    s = run({"home-assistant": {"url": "https://ha/api/mcp", "headers": {"Authorization": "Bearer t"}}})
    assert set(s) == {"mine", "telerouter", "home-assistant"}
    s = run({})
    assert set(s) == {"mine", "telerouter"}                  # ours removed, the owner's kept


def test_reset_keys():
    c = make_driver(agent(), "/d", "/w", "http://x")
    assert c.reset_keys("clear", "") == ["/clear", "Enter"]
    assert c.reset_keys("compact", "the  Proxmox\nwork") == ["/compact the Proxmox work", "Enter"]
    h = make_driver(agent(kind="hermes"), "/d", "/w", "http://x")
    assert h.reset_keys("compact", "x") == ["/compress x", "Enter"] and h.reset_keys("clear", "") == []


class _Store:
    def __init__(self):
        self.saved = 0

    def save(self):
        self.saved += 1


class _Engine2:
    def __init__(self):
        self.store = _Store()


async def test_reset_runs_after_the_turn_and_session_id_follows_clear():
    rt = AgentRuntime(_Engine2(), agent())
    rt.driver = make_driver(rt.agent, "/d", "/w", "http://x")
    sent = []

    async def fake_sh(cmd, check=False, input=None, timeout=30):
        sent.append(cmd)
        if cmd.startswith("tail"):
            return json.dumps({"type": "assistant", "message": {"usage": {
                "input_tokens": 10, "cache_read_input_tokens": 100000, "cache_creation_input_tokens": 2000,
                "output_tokens": 300}}}) + "\n"
        return ""
    rt._sh = fake_sh
    rt.request_reset("clear", "")
    assert rt.pending_reset and not any("send-keys" in c for c in sent)      # nothing typed mid-turn
    await rt._on_event({"hook_event_name": "Stop", "session_id": "u-1", "transcript_path": "/r/.claude/x.jsonl",
                        "last_assistant_message": "ok"})
    await asyncio.sleep(2.2)
    typed = [c for c in sent if "send-keys" in c]
    assert any("/clear" in c for c in typed) and typed[-1].endswith("Enter")
    assert rt.pending_reset is None
    # after /clear the agent reports a new session id: resume that one from now on
    await rt._on_event({"hook_event_name": "UserPromptSubmit", "session_id": "u-2", "prompt": "hi"})
    assert rt.agent.session_uuid == "u-2" and rt.engine.store.saved == 1


async def test_context_size_is_measured_after_each_turn():
    rt = AgentRuntime(_Engine2(), agent())
    rt.driver = make_driver(rt.agent, "/d", "/w", "http://x")

    async def fake_sh(cmd, check=False, input=None, timeout=30):
        return json.dumps({"type": "assistant", "message": {"usage": {
            "input_tokens": 5, "cache_read_input_tokens": 111000, "output_tokens": 95}}}) + "\n"
    rt._sh = fake_sh
    await rt._on_event({"hook_event_name": "Stop", "session_id": "u-1", "transcript_path": "/r/.claude/x.jsonl"})
    await asyncio.sleep(0.05)
    assert rt.context_tokens == 111100


def test_turn_end_without_stop_hook_is_noticed():
    from telerouter.agents.runtime import turn_end_without_stop
    L = lambda **e: json.dumps(e)  # noqa: E731
    prompt = L(type="user", message={"content": "[telerouter #ab12 · phone] Turn on the lights."})
    limit = L(type="assistant", message={"content": [{"type": "text", "text": "You've hit your session limit · resets 7:10pm"}]})
    end = L(type="system", subtype="turn_duration", durationMs=705)
    assert turn_end_without_stop("\n".join([prompt, limit]), "ab12") is None            # still running
    assert turn_end_without_stop("\n".join([prompt, limit, end]), "ab12").startswith("You've hit")
    other = L(type="user", message={"content": "[telerouter #zz99] later"})
    assert turn_end_without_stop("\n".join([prompt, end, other, limit, end]), "ab12") == ""
    assert turn_end_without_stop("\n".join([limit, end]), "ab12") is None                # not our prompt


async def test_mid_turn_detection_from_events():
    rt = AgentRuntime(_Engine2(), agent())
    rt.driver = make_driver(rt.agent, "/d", "/w", "http://x")
    lines = {"v": ""}

    async def fake_sh(cmd, check=False, input=None, timeout=30):
        return lines["v"]
    rt._sh = fake_sh
    ev = lambda n: json.dumps({"hook_event_name": n})  # noqa: E731
    lines["v"] = "\n".join([ev("UserPromptSubmit"), ev("Stop"), ev("Notification")])
    assert not await rt._mid_turn()
    lines["v"] = "\n".join([ev("Stop"), ev("UserPromptSubmit")])
    assert await rt._mid_turn()
