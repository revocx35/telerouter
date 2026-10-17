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
    assert set(settings["hooks"]) == {"UserPromptSubmit", "Stop", "Notification"}
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
    rt = AgentRuntime(_Eng(), agent(kind=kind))
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
