"""Sessions API (/v1): token scope, machine opt-in, ownership, per-session MCP files, turn states, throttling."""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from telerouter import tls
from telerouter.agents.machine import MachineRuntime
from telerouter.agents.runtime import AgentError
from telerouter.apisessions import ApiSessions, NewSession, SessionMcpServer, session_files, strip_ansi
from telerouter.models import Machine
from telerouter.web.app import create_app

PW = "correct horse battery"


class FakeRT:
    """A machine runtime without SSH: records what the Sessions API asks it to do."""

    def __init__(self, machine):
        self.machine = machine
        self.state = "ready"
        self.home = "/home/u"
        self.written: dict[str, str] = {}
        self.started: list[tuple] = []
        self.killed: list[str] = []
        self.shell: list[str] = []
        self.asked: list[tuple] = []
        self.reply = "found it, imported"
        self.gate: asyncio.Event | None = None

    async def _sh(self, cmd, input=None, timeout=30):
        self.shell.append(cmd)
        return ""

    async def _write_remote(self, path, content):
        self.written[path] = content
        return True

    async def new_session(self, name, workdir="~", by_admin=False, by_api=False, files_dir=""):
        assert by_api
        if not self.machine.allow_api_sessions:
            raise AgentError("not allowed")
        self.started.append((name, workdir, files_dir))
        return {"name": name, "path": "/home/u/" + workdir.lstrip("~/")}

    async def ask(self, session, text, label, on_wait=None, header="", timeout=None):
        self.asked.append((session, text, header, timeout))
        if self.gate:
            await self.gate.wait()
        return self.reply

    async def tmux_screen(self, session, history=0):
        return "\x1b[1mhello\x1b[0m from " + session

    async def kill_session(self, session):
        self.killed.append(session)

    def info(self):
        return {"id": self.machine.id, "state": self.state, "error": "", "sessions": []}

    async def stop(self):
        pass

    def start(self):
        pass


@pytest.fixture()
def env(settings):
    files = tls.ensure(settings.data_dir, [])
    public, _internal = create_app(settings, files)
    with TestClient(public, base_url="https://testserver") as c:
        eng = public.state.engine
        r = c.post("/api/setup", json={"setup_code": "ABCD-EFGH", "username": "admin", "password": PW})
        assert r.status_code == 200
        h = {"X-CSRF-Token": c.get("/api/session").json()["csrf"]}
        m = Machine(name="agents", host="h", username="u", allow_api_sessions=True)
        eng.store.config.machines.append(m)
        rt = FakeRT(m)
        eng.machine_rts = {m.id: rt}

        def token(scopes, name="adm"):
            out = c.post("/api/mcp-tokens", json={"name": name, "scopes": scopes}, headers=h).json()
            return out["token"], out["id"]

        yield c, eng, rt, token, h


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_needs_a_token_with_the_sessions_scope(env):
    c, eng, rt, token, h = env
    assert c.get("/v1/machines").status_code == 401
    plain, _ = token(["contacts"], "laptop")
    assert c.get("/v1/machines", headers=auth(plain)).status_code == 401
    good, _ = token(["sessions"])
    r = c.get("/v1/machines", headers=auth(good))
    assert r.status_code == 200 and r.json()["machines"][0]["name"] == "agents"
    assert r.headers["cache-control"] == "no-store"
    # the token is no admin login, and an admin cookie is no credential here
    c2 = TestClient(c.app, base_url="https://testserver")
    assert c2.get("/api/status", headers=auth(good)).status_code == 401
    assert c.get("/v1/sessions").status_code == 401


def test_bad_tokens_are_throttled(env):
    c, *_ = env
    codes = [c.get("/v1/sessions", headers=auth(f"trm_wrong{i}")).status_code for i in range(9)]
    assert 429 in codes


def test_machine_must_allow_api_sessions(env):
    c, eng, rt, token, h = env
    good, _ = token(["sessions"])
    rt.machine.allow_api_sessions = False
    r = c.post("/v1/sessions", json={"prompt": "hi"}, headers=auth(good))
    assert r.status_code == 409 and "allows API sessions" in r.json()["detail"]
    assert c.get("/v1/machines", headers=auth(good)).json()["machines"] == []


def test_session_runs_the_task_and_reports_the_answer(env):
    c, eng, rt, token, h = env
    good, _ = token(["sessions"])
    r = c.post("/v1/sessions", headers=auth(good), json={
        "name": "adm-job-1", "folder": "~/adm/job-1", "prompt": "find the book",
        "mcp_servers": [{"name": "adm", "url": "https://adm.lan:8450/mcp", "token": "adj_secret123"}]})
    assert r.status_code == 202, r.text
    sid = r.json()["id"]
    assert r.json()["state"] == "starting" and "token" not in json.dumps(r.json())
    assert wait_for(lambda: c.get(f"/v1/sessions/{sid}", headers=auth(good)).json()["state"] == "idle")
    s = c.get(f"/v1/sessions/{sid}", headers=auth(good)).json()
    assert s["reply"] == "found it, imported" and s["turns"] == 1 and s["mcp_servers"] == ["adm"]
    name, folder, files_dir = rt.started[0]
    assert name == "adm-job-1" and files_dir == "/home/u/.telerouter/api/adm-job-1"
    mcp = json.loads(rt.written[f"{files_dir}/mcp.json"])
    assert mcp["mcpServers"]["adm"]["headers"]["Authorization"] == "Bearer adj_secret123"
    assert json.loads(rt.written[f"{files_dir}/settings.json"])["permissions"]["allow"] == ["mcp__adm"]
    session, text, header, timeout = rt.asked[0]
    assert text == "find the book" and 'app "adm"' in header and timeout == rt.machine.reply_timeout
    # the record on disk has no secrets
    assert "adj_secret123" not in (eng.store.dir / "api_sessions.json").read_text()
    # screen as plain text
    assert c.get(f"/v1/sessions/{sid}/screen", headers=auth(good)).json()["screen"] == "hello from adm-job-1"
    # follow-up message
    r = c.post(f"/v1/sessions/{sid}/messages", json={"text": "and the sequel"}, headers=auth(good))
    assert r.status_code == 202
    assert wait_for(lambda: len(rt.asked) == 2 and
                    c.get(f"/v1/sessions/{sid}", headers=auth(good)).json()["turns"] == 2)
    # close: tmux session killed and the per-session folder removed
    r = c.delete(f"/v1/sessions/{sid}", headers=auth(good))
    assert r.status_code == 200 and r.json()["state"] == "closed"
    assert rt.killed == ["adm-job-1"] and any("rm -rf" in x and files_dir in x for x in rt.shell)
    assert c.post(f"/v1/sessions/{sid}/messages", json={"text": "x"}, headers=auth(good)).status_code == 409


def test_other_apps_cannot_see_or_touch_a_session(env):
    c, eng, rt, token, h = env
    a, _ = token(["sessions"], "adm")
    b, _ = token(["sessions"], "other")
    sid = c.post("/v1/sessions", json={"prompt": "x"}, headers=auth(a)).json()["id"]
    assert wait_for(lambda: c.get(f"/v1/sessions/{sid}", headers=auth(a)).json()["state"] == "idle")
    for call in (lambda: c.get(f"/v1/sessions/{sid}", headers=auth(b)),
                 lambda: c.get(f"/v1/sessions/{sid}/screen", headers=auth(b)),
                 lambda: c.post(f"/v1/sessions/{sid}/messages", json={"text": "x"}, headers=auth(b)),
                 lambda: c.delete(f"/v1/sessions/{sid}", headers=auth(b))):
        assert call().status_code == 404
    assert c.get("/v1/sessions", headers=auth(b)).json()["sessions"] == []
    assert rt.killed == []


def test_busy_session_refuses_a_second_message(env):
    c, eng, rt, token, h = env
    good, _ = token(["sessions"])
    rt.gate = asyncio.Event()
    sid = c.post("/v1/sessions", json={"prompt": "long job"}, headers=auth(good)).json()["id"]
    assert wait_for(lambda: c.get(f"/v1/sessions/{sid}", headers=auth(good)).json()["state"] == "working")
    r = c.post(f"/v1/sessions/{sid}/messages", json={"text": "hurry"}, headers=auth(good))
    assert r.status_code == 409
    c.delete(f"/v1/sessions/{sid}", headers=auth(good))


def test_validation(env):
    c, eng, rt, token, h = env
    good, _ = token(["sessions"])
    bad = [{"name": "telerouter-x"}, {"name": "a b"}, {"name": "../x"},
           {"mcp_servers": [{"name": "adm", "url": "http://evil.example/mcp"}]},
           {"mcp_servers": [{"name": "telerouter", "url": "https://x/mcp"}]},
           {"mcp_servers": [{"name": "a", "url": "https://x/mcp"}, {"name": "a", "url": "https://y/mcp"}]},
           {"mcp_servers": [{"name": "a", "url": "https://x/mcp", "token": "has space"}]},
           {"mcp_servers": [{"name": "a", "url": "https://x/mcp", "tls_ca": "not a cert"}]}]
    for body in bad:
        r = c.post("/v1/sessions", json=body, headers=auth(good))
        assert r.status_code == 422, (body, r.text)
    assert c.post("/v1/sessions", content=b"[1]", headers=auth(good)).status_code == 400
    assert c.post("/v1/sessions", content=b"x" * 400_000, headers=auth(good)).status_code == 413
    rt.machine.agent_kind = "hermes"
    r = c.post("/v1/sessions", json={"mcp_servers": [{"name": "adm", "url": "https://x/mcp"}]}, headers=auth(good))
    assert r.status_code == 400 and "Claude Code" in r.json()["detail"]


def test_open_session_cap_per_app(env):
    c, eng, rt, token, h = env
    good, _ = token(["sessions"])
    codes = [c.post("/v1/sessions", json={}, headers=auth(good)).status_code for _ in range(11)]
    assert codes[:10] == [202] * 10 and codes[10] == 429


def test_revoking_the_token_detaches_its_sessions(env):
    c, eng, rt, token, h = env
    good, tid = token(["sessions"])
    sid = c.post("/v1/sessions", json={}, headers=auth(good)).json()["id"]
    assert c.delete(f"/api/mcp-tokens/{tid}", headers=h).status_code == 200
    assert c.get(f"/v1/sessions/{sid}", headers=auth(good)).status_code == 401
    assert eng.api_sessions.records[sid]["token_id"] == ""


def test_restart_marks_running_turns_lost(tmp_path):
    p = tmp_path / "api_sessions.json"
    p.write_text(json.dumps({"sessions": [
        {"id": "s_0123456789ab", "token_id": "t", "state": "working", "name": "x", "created": 1},
        {"id": "s_0123456789ac", "token_id": "t", "state": "idle", "name": "y", "created": 2},
        {"id": "bad", "state": "idle"}]}))
    s = ApiSessions(object(), p)
    assert s.records["s_0123456789ab"]["state"] == "lost" and "restarted" in s.records["s_0123456789ab"]["error"]
    assert s.records["s_0123456789ac"]["state"] == "idle" and "bad" not in s.records


def test_session_files_and_ansi():
    servers = [SessionMcpServer(name="ADM", url="https://adm.lan/mcp", token="adj_x")]
    files = session_files(servers)
    assert json.loads(files["mcp.json"])["mcpServers"]["adm"]["url"] == "https://adm.lan/mcp"
    assert files["extra-ca.pem"] == ""
    assert strip_ansi("\x1b[31mred\x1b[0m \x1b]0;title\x07ok") == "red ok"
    assert NewSession().timeout == 0


def test_launch_command_points_claude_at_the_session_files():
    rt = MachineRuntime(object(), Machine(name="m", host="h", username="u", new_session_model="claude-sonnet-5-5"))
    cmd = rt.launch_command("adm-1", "/home/u/.telerouter/api/adm-1")
    assert "--mcp-config /home/u/.telerouter/api/adm-1/mcp.json" in cmd
    assert "--settings /home/u/.telerouter/api/adm-1/settings.json" in cmd
    assert "NODE_EXTRA_CA_CERTS=/home/u/.telerouter/api/adm-1/extra-ca.pem" in cmd
    assert "--mcp-config" not in rt.launch_command("plain")


async def test_new_session_by_api_needs_the_machine_switch():
    class _E:
        store = None
    rt = MachineRuntime(_E(), Machine(name="m", host="h", username="u", allow_new_sessions=True))
    rt.state = "ready"
    with pytest.raises(AgentError, match="API sessions are not allowed"):
        await rt.new_session("x", "~", by_api=True)
