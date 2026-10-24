"""The tmux page: parsing a machine's sessions, reading a pane as text, typing into it, create/close.

The point of these tests is that the browser never gets a video stream: everything is `tmux
list-sessions`/`capture-pane` text in, `send-keys`/`paste-buffer` out.
"""

import json

import pytest
from fastapi.testclient import TestClient

from telerouter import security, tmux as tmuxmod, tls
from telerouter.agents.machine import MachineRuntime
from telerouter.agents.runtime import AgentError
from telerouter.models import Agent, Config, Machine
from telerouter.web.app import create_app

PW = "correct horse battery"


# -- pure helpers ---------------------------------------------------------------------------------------
def test_sessions_parse_with_the_pane_and_window_size():
    out = ("wa_logger|~|2|~|1|~|1792846803|~|1792877622|~|claude|~|/root/wa_logger|~|4242|~|120x40\n"
           "telerouter-0a82|~|1|~|0|~|1792837714|~|1792837714|~|sh|~|/root/jarvis|~|99|~|80x24\n"
           "bad name!|~|1|~|0|~|1|~|1|~|bash|~|/root|~|1|~|80x24\n"
           "truncated line without the fields\n")
    sessions = tmuxmod.parse_sessions(out)
    assert [s["name"] for s in sessions] == ["wa_logger", "telerouter-0a82"]
    s = sessions[0]
    assert (s["windows"], s["attached"], s["command"], s["path"], s["pid"]) == (2, True, "claude", "/root/wa_logger", 4242)
    assert (s["width"], s["height"]) == (120, 40)


def test_the_agent_in_a_pane_is_found_below_its_shell():
    procs = tmuxmod.parse_processes(
        "100 1 -bash\n200 100 claude\n300 200 node /usr/lib/x/cli.js\n400 1 /usr/bin/hermes chat\n"
        "500 1 python3 -m pytest tests/test_tmux.py\n")
    assert tmuxmod.agents_in_tree(100, procs) == ["claude"]
    assert tmuxmod.agents_in_tree(400, procs) == ["hermes"]
    assert tmuxmod.agents_in_tree(500, procs) == []
    assert tmuxmod.agent_of("/root/.local/bin/hermes chat -c x") == "hermes"
    assert tmuxmod.agent_of("python3 -m pytest tests/test_claude.py") == ""
    # Hermes hides behind an interpreter: its launcher script is in argv, not a "hermes" binary
    assert tmuxmod.agent_of("python3 -I -c import sys; sys.path.insert(0, '/root/.hermes/hermes-agent') chat -c x") == "hermes"


def test_what_a_session_is():
    assert tmuxmod.classify({"name": "telerouter-abc", "command": "sh"}, []) == "agent-session"
    assert tmuxmod.classify({"name": "wa_logger", "command": "bash"}, ["claude"]) == "claude"
    assert tmuxmod.classify({"name": "vps", "command": "bash"}, []) == "shell"
    assert tmuxmod.classify({"name": "build", "command": "make"}, []) == "other"


def test_pane_commands_use_an_exact_session_target_and_quote_what_they_send():
    assert tmuxmod.capture_cmd("vps").endswith("-t =vps:")
    assert "capture-pane" in tmuxmod.capture_cmd("vps") and "-e" in tmuxmod.capture_cmd("vps")
    assert "-S -300" in tmuxmod.capture_cmd("vps", 300)
    assert tmuxmod.clamp_history(10**9) == tmuxmod.MAX_HISTORY and tmuxmod.clamp_history(-5) == 0
    cmd = tmuxmod.send_text_cmd("vps", "rm -rf /tmp/it's here; echo $HOME", "trbuf", True)
    assert "set-buffer" in cmd and "paste-buffer" in cmd and "send-keys" in cmd
    assert "'rm -rf /tmp/it'\"'\"'s here; echo $HOME'" in cmd          # one quoted argument
    assert cmd.endswith("send-keys -t =vps: Enter")            # a key is a keysym, never a shell word
    assert "Enter" not in tmuxmod.send_text_cmd("vps", "hello", "b", False)
    assert tmuxmod.new_session_cmd("x", "~/my dir") == "tmux new-session -d -s x -c \"$HOME\"/'my dir'"
    assert tmuxmod.mkdir_cmd("~") == 'mkdir -p "$HOME"'


@pytest.mark.parametrize("key", ["Enter", "Tab", "Escape", "Up", "C-c", "C-d", "F5", "BSpace"])
def test_browser_keys_are_whitelisted(key):
    assert tmuxmod.KEYS_RE.match(key)


@pytest.mark.parametrize("key", ["Enter; rm -rf /", "-l", "C-;", "x", "", "S-Left"])
def test_anything_else_is_not_a_key(key):
    assert not tmuxmod.KEYS_RE.match(key)


def test_names_and_folders():
    assert tmuxmod.new_name_ok("pokerbot") and tmuxmod.new_name_ok("task-x.1")
    assert not tmuxmod.new_name_ok("telerouter-1234") and not tmuxmod.new_name_ok("-x") and not tmuxmod.new_name_ok("a/b")
    assert tmuxmod.folder_ok("~/work/app 2") and not tmuxmod.folder_ok("~/../etc") and not tmuxmod.folder_ok("a;reboot")


# -- the machine runtime ---------------------------------------------------------------------------------
class _Store:
    def __init__(self):
        self.config = Config()


class _Eng:
    def __init__(self):
        self.store = _Store()
        self.merged = None
        self.tasks = {}

    def merge_tmux_sessions(self, m, found):
        self.merged = found

    def task_sessions(self, machine_id):
        return self.tasks


def _machine():
    return Machine(name="lxc", host="h", username="root", agent_kind="hermes")


async def test_tmux_listing_includes_the_agents_own_sessions():
    eng = _Eng()
    eng.store.config.agents = [Agent(name="Jarvis", machine_id="m1", kind="hermes",
                                     tmux_session="telerouter-e9a5e65a")]
    rt = MachineRuntime(eng, Machine(id="m1", name="lxc", host="h", username="root"))
    rt.state = "ready"
    calls = []

    async def fake_sh(cmd, input=None, timeout=30):
        calls.append(cmd)
        if "list-sessions" in cmd:
            return ("telerouter-e9a5e65a|~|1|~|0|~|1792837714|~|1792837714|~|sh|~|/root|~|99|~|160x48\n"
                    "pokerbot|~|1|~|0|~|1792846803|~|1792877622|~|bash|~|/root/poker|~|100|~|120x40\n")
        if cmd.startswith("ps -eo"):
            return "99 1 sh\n100 1 -bash\n200 100 claude\n"
        return ""
    rt._sh = fake_sh
    sessions = await rt.tmux_list()
    assert [s["name"] for s in sessions] == ["telerouter-e9a5e65a", "pokerbot"]
    own, other = sessions
    assert own["agent"]["name"] == "Jarvis" and own["agent"]["kind"] == "hermes"
    assert own["kind"] == "hermes" and own["agents"] == ["Hermes"]  # from the config, not guessed from the pane
    assert other["kind"] == "claude" and other["agents"] == ["Claude Code"] and other["agent"] is None
    assert any("ps -eo" in c for c in calls)


async def test_a_sessions_used_by_a_scheduled_task_is_marked():
    eng = _Eng()
    eng.tasks = {"task-nightly": {"id": "t1", "name": "nightly", "target": "task_session"}}
    rt = MachineRuntime(eng, _machine())
    rt.state = "ready"

    async def fake_sh(cmd, input=None, timeout=30):
        if "list-sessions" in cmd:
            return ("task-nightly|~|1|~|0|~|1|~|1|~|bash|~|/root|~|10|~|80x24\n"
                    "pokerbot|~|1|~|0|~|1|~|1|~|bash|~|/root|~|11|~|80x24\n")
        return "10 1 -bash\n11 1 -bash\n"
    rt._sh = fake_sh
    sessions = await rt.tmux_list()
    marked = {s["name"]: s["task"] for s in sessions}
    assert marked["task-nightly"] == {"id": "t1", "name": "nightly", "target": "task_session"}
    assert marked["pokerbot"] is None


def test_the_scheduled_tasks_sessions_are_found_from_the_tasks(settings):
    from telerouter.engine import Engine
    from telerouter.models import Config, ScheduledTask
    from telerouter.store import Store
    store = Store(settings.data_dir)
    store.config = Config(tasks=[
        ScheduledTask(name="nightly", prompt="x", target="task_session", machine_id="m1", session_name="task-night"),
        ScheduledTask(name="mail", prompt="x", target="new_session", machine_id="m1"),
        ScheduledTask(name="elsewhere", prompt="x", target="new_session", machine_id="m2"),
        ScheduledTask(name="jarvis", prompt="x", target="agent"),
    ])
    eng = Engine(settings, store)
    eng.scheduler.info = lambda t: {"sessions": ["mail-1024-0900"] if t.name == "mail" else []}
    got = eng.task_sessions("m1")
    assert set(got) == {"task-night", "mail-1024-0900"}
    assert got["task-night"] == {"id": store.config.tasks[0].id, "name": "nightly", "target": "task_session"}
    assert got["mail-1024-0900"]["target"] == "new_session"
    assert eng.task_sessions("m3") == {}


async def test_the_process_table_is_cached_between_listings():
    rt = MachineRuntime(_Eng(), _machine())
    rt.state = "ready"
    n = {"ps": 0}

    async def fake_sh(cmd, input=None, timeout=30):
        if "list-sessions" in cmd:
            return "a|~|1|~|0|~|1|~|1|~|bash|~|/root|~|10|~|80x24\n"
        if cmd.startswith("ps -eo"):
            n["ps"] += 1
            return "10 1 -bash\n"
        return ""
    rt._sh = fake_sh
    await rt.tmux_list()
    await rt.tmux_list()
    assert n["ps"] == 1


async def test_a_pane_is_read_as_text_and_a_dead_session_says_so():
    rt = MachineRuntime(_Eng(), _machine())
    rt.state = "ready"

    async def fake_sh(cmd, input=None, timeout=30):
        assert "capture-pane" in cmd
        return "ok\n\x1b[32mhello\x1b[0m\nworld\n" if "alive" in cmd else "gone\n"
    rt._sh = fake_sh
    assert await rt.tmux_screen("alive") == "\x1b[32mhello\x1b[0m\nworld\n"
    with pytest.raises(AgentError, match="not running"):
        await rt.tmux_screen("dead")


async def test_typing_pastes_the_text_then_presses_enter():
    rt = MachineRuntime(_Eng(), _machine())
    rt.state = "ready"
    seen = []

    async def fake_sh(cmd, input=None, timeout=30):
        seen.append(cmd)
        return ""
    rt._sh = fake_sh
    await rt.tmux_send("vps", text="hello\nworld\n", enter=True)
    blob = "\n".join(seen)
    assert "set-buffer" in blob and "paste-buffer" in blob
    assert "'hello\nworld'" in blob                     # the trailing newline is not a second Enter
    assert blob.endswith("send-keys -t =vps: Enter")
    with pytest.raises(AgentError, match="can't be sent"):
        await rt.tmux_send("vps", key="Enter; reboot")
    with pytest.raises(AgentError, match="nothing to send"):
        await rt.tmux_send("vps")


async def test_starting_a_session_runs_the_chosen_thing():
    rt = MachineRuntime(_Eng(), _machine())
    rt.state = "ready"
    seen = []
    started = {"yes": False}

    async def fake_sh(cmd, input=None, timeout=30):
        seen.append(cmd)
        if "list-sessions" in cmd:
            return "plain|~|1|~|0|~|1|~|1|~|bash|~|/root/work|~|77|~|80x24\n" if started["yes"] else ""
        if "new-session" in cmd:
            started["yes"] = True
        return "ok"
    rt._sh = fake_sh
    info = await rt.tmux_new("plain", "~/work", "shell")
    assert info["name"] == "plain"
    new = [c for c in seen if "new-session" in c][0]
    assert "mkdir -p \"$HOME\"/work" in new and "new-session -d -s plain -c \"$HOME\"/work" in new
    assert "SHELL" not in new                            # a bare shell session: tmux starts it itself
    with pytest.raises(AgentError, match="already exists"):
        await rt.tmux_new("plain", "~", "shell")


async def test_an_agents_session_is_not_killed_from_the_tmux_page():
    eng = _Eng()
    eng.store.config.agents = [Agent(name="Jarvis", machine_id="m1", kind="hermes",
                                     tmux_session="telerouter-e9a5e65a")]
    rt = MachineRuntime(eng, Machine(id="m1", name="lxc", host="h", username="root"))
    rt.state = "ready"
    seen = []

    async def fake_sh(cmd, input=None, timeout=30):
        seen.append(cmd)
        return "ok"
    rt._sh = fake_sh
    with pytest.raises(AgentError, match="Agents & machines"):
        await rt.tmux_kill("telerouter-e9a5e65a")
    await rt.tmux_kill("pokerbot")
    assert any("kill-session" in c for c in seen)


# -- the API ---------------------------------------------------------------------------------------------
class FakeRT:
    def __init__(self):
        self.state = "ready"
        self.error = ""
        self.sent = []
        self.killed = []

    async def stop(self):
        pass

    def info(self):
        return {"id": "m", "state": self.state, "error": self.error, "sessions": []}

    async def tmux_list(self):
        return [{"name": "vps", "windows": 1, "attached": False, "created": 1, "activity": 2, "command": "bash",
                 "path": "/root", "pid": 10, "width": 80, "height": 24, "kind": "shell", "agents": [], "agent": None},
                {"name": "telerouter-e9a5e65a", "windows": 1, "attached": False, "created": 1, "activity": 2,
                 "command": "sh", "path": "/root", "pid": 11, "width": 80, "height": 24, "kind": "hermes",
                 "agents": [], "agent": {"id": "a1", "name": "Hermes", "kind": "hermes"}}]

    async def tmux_screen(self, session, history=0):
        if session == "dead":
            raise AgentError("the session dead is not running any more")
        return "hello\n\x1b[1mbold\x1b[0m\n"

    async def tmux_send(self, session, text="", key="", enter=False):
        if key == "Nope":
            raise AgentError("that key can't be sent")
        self.sent.append({"session": session, "text": text, "key": key, "enter": enter})

    async def tmux_new(self, name, folder="~", run="agent"):
        return {"name": name, "kind": "shell" if run == "shell" else run, "path": folder}

    async def tmux_kill(self, session):
        if session.startswith("telerouter-"):
            raise AgentError("telerouter-e9a5e65a is the session of an agent")
        self.killed.append(session)


@pytest.fixture()
def tmux_app(settings):
    files = tls.ensure(settings.data_dir, [])
    public, _internal = create_app(settings, files)
    store = public.state.store
    machine = Machine(name="claude LXC", host="10.0.0.5", username="root")
    store.config.machines.append(machine)
    with TestClient(public, base_url="https://testserver") as client:
        rt = FakeRT()                                    # after start-up: the engine prunes unknown runtimes
        public.state.engine.machine_rts[machine.id] = rt
        yield client, machine.id, rt


def _login(c):
    r = c.post("/api/setup", json={"setup_code": "ABCD-EFGH", "username": "admin", "password": PW})
    assert r.status_code == 200, r.text
    return c.get("/api/session").json()["csrf"]


def test_the_apis_are_admin_only(tmux_app):
    c, mid, _rt = tmux_app
    assert c.get("/api/tmux").status_code == 401
    assert c.get(f"/api/tmux/{mid}/vps").status_code == 401


def test_the_page_lists_sessions_per_machine(tmux_app):
    c, mid, _rt = tmux_app
    _login(c)
    d = c.get("/api/tmux").json()
    m = d["machines"][0]
    assert m["name"] == "claude LXC" and m["state"] == "ready"
    names = [s["name"] for s in m["sessions"]]
    assert names == ["vps", "telerouter-e9a5e65a"]
    assert m["sessions"][1]["agent"]["name"] == "Hermes"


def test_a_screen_is_text_and_an_unchanged_one_costs_nothing(tmux_app):
    c, mid, _rt = tmux_app
    _login(c)
    d = c.get(f"/api/tmux/{mid}/vps?history=0").json()
    assert d["same"] is False and "bold" in d["screen"]
    again = c.get(f"/api/tmux/{mid}/vps?history=0&hash={d['hash']}").json()
    assert again["same"] is True and "screen" not in again
    assert c.get(f"/api/tmux/{mid}/dead").status_code == 502


def test_typing_needs_csrf_and_a_known_key(tmux_app):
    c, mid, rt = tmux_app
    csrf = _login(c)
    body = {"text": "ls -la", "enter": True}
    assert c.post(f"/api/tmux/{mid}/vps/send", json=body).status_code == 403
    r = c.post(f"/api/tmux/{mid}/vps/send", json=body, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    assert rt.sent[-1] == {"session": "vps", "text": "ls -la", "key": "", "enter": True}
    r = c.post(f"/api/tmux/{mid}/vps/send", json={"key": "Nope"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 502
    r = c.post(f"/api/tmux/{mid}/vps/send", json={"key": "Enter"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and rt.sent[-1]["key"] == "Enter"


def test_new_and_delete(tmux_app):
    c, mid, rt = tmux_app
    csrf = _login(c)
    r = c.post(f"/api/tmux/{mid}", json={"name": "pokerbot", "folder": "~/poker", "run": "shell"},
               headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and r.json()["session"]["kind"] == "shell"
    assert c.post(f"/api/tmux/{mid}", json={"name": "x", "run": "vnc"}, headers={"X-CSRF-Token": csrf}).status_code == 422
    assert c.delete(f"/api/tmux/{mid}/vps", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert rt.killed == ["vps"]
    r = c.delete(f"/api/tmux/{mid}/telerouter-e9a5e65a", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 502 and "session of an agent" in r.json()["detail"]


def test_an_unknown_machine_is_a_404(tmux_app):
    c, _mid, _rt = tmux_app
    _login(c)
    assert c.get("/api/tmux/nope/vps").status_code == 404
