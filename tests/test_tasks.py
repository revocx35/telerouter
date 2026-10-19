"""Scheduled tasks: schedules, the three places a task runs, the gentle context reminder, the API."""

import asyncio
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from telerouter import tls
from telerouter.agents.runtime import AgentError, AgentRuntime, Pending, Source, context_size
from telerouter.cron import Cron, describe, next_run
from telerouter.models import Agent, Config, Machine, ScheduledTask
from telerouter.scheduler import Scheduler, reminder_answer

from .test_web import setup_admin

TZ = ZoneInfo("Europe/Istanbul")


def at(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=TZ)


def task(**kw) -> ScheduledTask:
    return ScheduledTask(**{"name": "Mail summary", "prompt": "Summarise my mail.", "timezone": "Europe/Istanbul", **kw})


# -- schedules ---------------------------------------------------------------------------------------------------
def test_cron_expressions():
    c = Cron("0 8 * * 1-5")                                        # weekdays at 08:00
    assert c.next_after(at("2026-10-16T09:00")) == at("2026-10-19T08:00")   # Friday 09:00 -> Monday
    assert Cron("*/30 * * * *").next_after(at("2026-10-19T10:10")) == at("2026-10-19T10:30")
    assert Cron("15 10 1 jan *").next_after(at("2026-10-19T00:00")) == at("2027-01-01T10:15")
    assert Cron("0 12 * * 0").next_after(at("2026-10-19T00:00")) == at("2026-10-25T12:00")   # 0 = Sunday
    assert Cron("0 12 * * 7").next_after(at("2026-10-19T00:00")) == at("2026-10-25T12:00")   # 7 = Sunday too
    # day-of-month and weekday both restricted: either matches (like cron)
    assert Cron("0 9 1 * mon").next_after(at("2026-10-19T10:00")) == at("2026-10-26T09:00")
    assert Cron("0 9 * * *").next_after(at("2026-10-19T09:00")) == at("2026-10-20T09:00")    # strictly after
    for bad in ("* * * *", "60 * * * *", "* 24 * * *", "*/0 * * * *", "a * * * *", "5-1 * * * *"):
        with pytest.raises(ValueError):
            Cron(bad)


def test_next_run_for_each_schedule():
    now = at("2026-10-19T10:00").timestamp()                       # a Monday
    assert next_run(task(times=["09:00", "18:00"]), now) == at("2026-10-19T18:00").timestamp()
    assert next_run(task(times=["09:00"], days=[2, 4]), now) == at("2026-10-21T09:00").timestamp()   # Wed
    assert next_run(task(schedule="interval", every_minutes=90), now) == now + 90 * 60
    assert next_run(task(schedule="once", at="2026-10-20T07:30"), now) == at("2026-10-20T07:30").timestamp()
    assert next_run(task(schedule="once", at="2026-10-18T07:30"), now) is None          # in the past
    assert next_run(task(schedule="cron", cron="0 8 * * 1-5"), now) == at("2026-10-20T08:00").timestamp()


def test_describe_and_validation():
    assert describe(task()) == "every day at 09:00"
    assert describe(task(days=[0, 1, 2, 3, 4], times=["18:00", "08:30"])) == "weekdays at 08:30, 18:00"
    assert describe(task(days=[0, 4])) == "Mon, Fri at 09:00"
    assert describe(task(schedule="interval", every_minutes=120)) == "every 2 hours"
    assert describe(task(schedule="once", at="2026-10-20T09:00")) == "once on 2026-10-20 09:00"
    for bad in ({"times": ["25:00"]}, {"times": []}, {"days": [7]}, {"schedule": "once"},
                {"schedule": "cron", "cron": "bad"}, {"schedule": "interval", "every_minutes": 1},
                {"timezone": "Mars/Base"}, {"prompt": " "}, {"session_name": "telerouter-x"},
                {"session_name": "a b"}, {"folder": "~/../etc"}, {"target": "shell"}):
        with pytest.raises(ValidationError):
            task(**bad)
    assert task(name="Morning mail!").tmux_name == "task-morning-mail"
    assert task(session_name="mailbot").tmux_name == "mailbot"


def test_reminder_answers():
    assert reminder_answer("Nothing is going on.\nTELEROUTER_CLEAR") == ("clear", "")
    assert reminder_answer("**TELEROUTER_COMPACT:** keep the open invoice list") == ("compact", "keep the open invoice list")
    assert reminder_answer("keeping it") == ("keep", "")
    assert reminder_answer("I won't say TELEROUTER_CLEAR here, keeping it") == ("keep", "")   # must be its own line


def test_context_size_from_a_transcript():
    lines = [{"type": "assistant", "message": {"usage": {"input_tokens": 5, "cache_read_input_tokens": 300000,
                                                         "cache_creation_input_tokens": 1000, "output_tokens": 200}}},
             {"type": "user", "message": {"content": "hi"}}]
    assert context_size("\n".join(json.dumps(x) for x in lines)) == 301205
    assert context_size('{"type": "user"}') is None


# -- the scheduler with fake agents and machines ---------------------------------------------------------------
class _Store:
    def __init__(self, path: Path):
        self.dir = path
        self.config = Config(agents=[Agent(name="Jarvis")], machines=[Machine(name="lxc", host="h", username="u")])
        self.saves = 0

    def save(self):
        self.saves += 1

    def agent(self, aid):
        return next((a for a in self.config.agents if a.id == aid), None)

    def default_agent(self):
        return self.config.agents[0]

    def machine(self, mid):
        return next((m for m in self.config.machines if m.id == mid), None)


class _Agent:
    def __init__(self):
        self.asked = []

    async def ask(self, text, source, on_note=None, timeout=None):
        self.asked.append((text, source, timeout))
        return "3 new mails, nothing urgent."


class _Machine:
    def __init__(self, machine):
        self.machine = machine
        self.state, self.error = "ready", ""
        self.sessions: list[str] = []
        self.asked: list[tuple] = []
        self.killed: list[str] = []
        self.resets: list[tuple] = []
        self.tokens = 1000
        self.answer = "keeping it"

    async def has_session(self, name):
        return name in self.sessions

    async def new_session(self, name, workdir="~", by_admin=False):
        assert by_admin
        self.sessions.append(name)
        return {"name": name}

    async def kill_session(self, name):
        self.killed.append(name)
        self.sessions.remove(name)

    async def ask(self, session, text, label, on_wait=None, header="", timeout=None):
        self.asked.append((session, text, header))
        return self.answer if "context reminder" in header else "Done: the report is in report.md."

    async def context_tokens(self, session):
        return self.tokens

    async def reset_context(self, session, mode, keep=""):
        self.resets.append((session, mode, keep))


class _Eng:
    def __init__(self, path):
        self.store = _Store(path)
        self.jarvis = _Agent()
        self.machine_rts = {self.store.config.machines[0].id: _Machine(self.store.config.machines[0])}

    @property
    def agents(self):
        return self

    def get(self, aid):
        return self.jarvis


async def _settle(sch):
    for _ in range(50):
        if not sch.running:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the run did not finish")


async def test_runs_in_the_agents_conversation(tmp_path):
    e = _Eng(tmp_path)
    t = task(timeout_minutes=30)
    e.store.config.tasks.append(t)
    sch = Scheduler(e)
    sch.plan(t, at("2026-10-19T08:00").timestamp())
    assert sch.st(t.id)["next"] == at("2026-10-19T09:00").timestamp()
    sch.tick(at("2026-10-19T08:59").timestamp())
    assert not sch.running                                        # not due yet
    sch.tick(at("2026-10-19T09:00:10").timestamp())
    await _settle(sch)
    text, src, timeout = e.jarvis.asked[0]
    assert text == "Summarise my mail." and src.kind == "schedule" and timeout == 30 * 60
    assert src.label == 'scheduled task "Mail summary"'
    run = sch.st(t.id)["runs"][-1]
    assert (run["status"], run["reply"], run["where"]) == ("ok", "3 new mails, nothing urgent.", "Jarvis's conversation")
    assert sch.st(t.id)["next"] == at("2026-10-20T09:00").timestamp()
    saved = json.loads((tmp_path / "tasks.json").read_text())       # state survives restarts
    assert saved[t.id]["runs"][-1]["status"] == "ok"


async def test_a_new_session_every_run_keeps_the_last_ones(tmp_path):
    e = _Eng(tmp_path)
    m = e.store.config.machines[0]
    t = task(target="new_session", machine_id=m.id, keep_sessions=2, folder="~/reports")
    e.store.config.tasks.append(t)
    sch = Scheduler(e)
    for i in range(3):
        sch.run_now(t)
        sch.st(t.id)["runs"][-1]["started"] = at(f"2026-10-1{i + 1}T09:00").timestamp()
        await _settle(sch)
    mrt = e.machine_rts[m.id]
    assert mrt.killed == ["mail-summary-1011-0900"]               # only the last two runs' sessions stay
    assert sch.st(t.id)["sessions"] == mrt.sessions == ["mail-summary-1012-0900", "mail-summary-1013-0900"]
    session, text, header = mrt.asked[-1]
    assert session == "mail-summary-1013-0900" and text == "Summarise my mail."
    assert header.startswith('scheduled task "Mail summary" (2026-10-13 09:00): an automatic run')
    assert sch.st(t.id)["runs"][-1]["session"] == "mail-summary-1013-0900"


async def test_own_session_gets_a_gentle_context_reminder(tmp_path):
    e = _Eng(tmp_path)
    m = e.store.config.machines[0]
    t = task(target="task_session", machine_id=m.id, context_limit_k=300)
    e.store.config.tasks.append(t)
    sch = Scheduler(e)
    mrt = e.machine_rts[m.id]

    async def run():
        sch.run_now(t)
        await _settle(sch)
        return sch.st(t.id)["runs"][-1]

    r = await run()                                                # below the limit: no reminder
    assert mrt.sessions == ["task-mail-summary"] and len(mrt.asked) == 1 and r["context_k"] == 1
    mrt.tokens = 312000
    r = await run()                                                # above: reminded, it keeps its context
    assert r["status"] == "ok" and r["reply"] == "Done: the report is in report.md."
    session, text, header = mrt.asked[-1]
    assert header == 'scheduled task "Mail summary": context reminder'
    assert "about 312k tokens, above the 300k advised" in text and "nothing you have to do" in text
    assert r["context_note"] == "reminded; the agent kept its context" and not mrt.resets
    n = len(mrt.asked)
    mrt.tokens = 330000
    await run()                                                    # grew less than 10% of the limit: no nagging
    assert len(mrt.asked) == n + 1
    mrt.tokens, mrt.answer = 345000, "Nothing is in progress.\nTELEROUTER_COMPACT: the open invoice list"
    r = await run()
    assert mrt.resets == [("task-mail-summary", "compact", "the open invoice list")]
    assert r["context_note"] == "reminded; the agent compacted its context"
    assert mrt.sessions == ["task-mail-summary"]                   # the same session every run


async def test_calls_about_a_run_only_when_the_task_asks(tmp_path):
    e = _Eng(tmp_path)
    m = e.store.config.machines[0]
    sch = Scheduler(e)
    for t in (task(calls="task"), task(name="s", target="new_session", machine_id=m.id, calls="task"),
              task(name="own", target="new_session", machine_id=m.id)):
        e.store.config.tasks.append(t)
        sch.run_now(t)
        await _settle(sch)
    assert e.jarvis.asked[0][0].endswith("(Don't phone, message or announce anything about this run unless the "
                                         "task above asks you to.)")
    quiet, own = e.machine_rts[m.id].asked
    assert quiet[2].endswith("Don't phone, message or announce anything about this run unless the task asks you to")
    assert "phone" not in own[2]                                   # the agent's own rules decide


async def test_overlap_once_and_restart(tmp_path):
    e = _Eng(tmp_path)
    gate = asyncio.Event()

    async def slow(text, source, on_note=None, timeout=None):
        await gate.wait()
        return "ok"
    e.jarvis.ask = slow
    t = task(schedule="interval", every_minutes=5)
    e.store.config.tasks.append(t)
    sch = Scheduler(e)
    sch.run_now(t)
    with pytest.raises(AgentError):
        sch.run_now(t)                                            # one run at a time
    sch.st(t.id)["next"] = 1.0
    sch.tick(1000.0)
    assert sch.st(t.id)["runs"][-1]["status"] == "skipped" and sch.st(t.id)["next"] == 1000.0 + 300
    gate.set()
    await _settle(sch)
    one = task(name="once", schedule="once", at="2026-10-19T09:00")
    e.store.config.tasks.append(one)
    sch.plan(one, at("2026-10-19T08:00").timestamp())
    sch.tick(at("2026-10-19T09:00").timestamp())
    await _settle(sch)
    assert not one.enabled and sch.st(one.id)["next"] is None     # a one-off task turns itself off
    # restart: a run cut off by the restart is closed, a long-missed run is skipped, a fresh one catches up
    sch.st(t.id)["runs"].append({"id": "x", "status": "running", "started": 1.0})
    sch.st(t.id)["next"] = 5.0
    one.enabled, one.schedule, one.every_minutes = True, "interval", 60
    import time
    sch.st(one.id)["next"] = time.time() - 60
    sch._save()
    sch2 = Scheduler(e)
    sch2.resume()
    assert sch2.st(t.id)["runs"][-1]["status"] == "error"
    assert sch2.st(t.id)["next"] > time.time()                    # missed long ago: planned anew
    assert sch2.st(one.id)["next"] < time.time()                   # missed a minute ago: runs on the next tick


async def test_agent_busy_with_a_task_answers_voice_requests_at_once():
    class _E:
        store = None
    rt = AgentRuntime(_E(), Agent(name="Jarvis"))
    rt.state = "ready"
    loop = asyncio.get_running_loop()
    rt.pending = Pending("abc", "x", Source("schedule", 'scheduled task "Mail summary"'), loop.create_future())
    with pytest.raises(AgentError, match='busy with the scheduled task "Mail summary"'):
        await rt.ask("turn on the light", Source("speaker", "Kitchen speaker"))


def test_mcp_treats_a_scheduled_run_as_the_agent_on_its_own():
    from telerouter.mcp import McpServer

    class _Rt:
        current_source = Source("schedule", 'scheduled task "x"')

    class _E:
        agents = type("A", (), {"get": staticmethod(lambda aid: _Rt())})()
    srv = McpServer.__new__(McpServer)
    srv.engine = _E()
    tok = type("T", (), {"agent_id": "a1"})()
    assert srv._source(tok) is None                                # quiet hours apply, no "requesting speaker"
    _Rt.current_source = Source("speaker", "Kitchen speaker", speaker_id="0a0b0c0d")
    assert srv._source(tok).kind == "speaker"


# -- admin API -----------------------------------------------------------------------------------------------
@pytest.fixture()
def c(settings):
    from telerouter.web.app import create_app
    public, _ = create_app(settings, tls.ensure(settings.data_dir, []))
    with TestClient(public, base_url="https://testserver") as client:
        yield client


def test_tasks_via_the_api(c):
    h = {"X-CSRF-Token": setup_admin(c)}
    assert c.post("/api/tasks", json={"name": "x", "prompt": "y"}, headers=h).status_code == 422   # no agent yet
    m = c.post("/api/machines", json={"name": "lxc", "host": "10.0.0.5", "username": "root"}, headers=h).json()
    body = {"name": "Reports", "prompt": "Write the report.", "target": "task_session", "machine_id": m["id"],
            "context_limit_k": 300, "schedule": "cron", "cron": "0 8 * * 1-5", "timezone": "Europe/Istanbul"}
    t = c.post("/api/tasks", json=body, headers=h).json()
    assert t["tmux_name"] == "task-reports" and t["schedule_text"] == "cron 0 8 * * 1-5" and t["next"]
    assert t["where"] == "tmux task-reports on lxc"
    assert c.post("/api/tasks", json={**body, "name": "Reports"}, headers=h).status_code == 409   # same session
    assert c.post("/api/tasks", json={**body, "cron": "61 * * * *"}, headers=h).status_code == 422
    assert c.post("/api/tasks", json=body).status_code == 403                                  # CSRF
    off = c.put(f"/api/tasks/{t['id']}", json={"enabled": False}, headers=h).json()
    assert off["enabled"] is False and off["next"] is None
    assert c.delete(f"/api/machines/{m['id']}", headers=h).status_code == 409                  # a task uses it
    r = c.post(f"/api/tasks/{t['id']}/run", headers=h)
    assert r.status_code == 200 and r.json()["run"]["status"] == "running"
    listed = c.get("/api/tasks").json()
    assert listed["machines"][0]["name"] == "lxc" and listed["tasks"][0]["runs"]
    assert c.delete(f"/api/tasks/{t['id']}", headers=h).status_code == 200
    assert c.get("/api/tasks").json()["tasks"] == []
