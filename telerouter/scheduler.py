"""Scheduled tasks: a prompt that runs on a schedule, in one of three places.

* agent:        typed into an agent telerouter runs (Jarvis), in its ongoing conversation.
* new_session:  a new tmux session with a fresh agent for every run (`<task>-MMDD-HHMM`); the sessions of
                the last `keep_sessions` runs stay open for the owner to look at, older ones are closed.
* task_session: one tmux session the task keeps (`task-<name>`), started on the first run. Above the
                task's advised context size the agent gets a gentle reminder after a run: it may answer
                TELEROUTER_CLEAR / TELEROUTER_COMPACT: <keep>, and telerouter types /clear or /compact.

Run state (next run, recent runs, the task's sessions) lives in /data/tasks.json, not in config.json.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import secrets
import time
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .agents.runtime import AgentError, Source
from .cron import describe, next_run
from .models import ScheduledTask, task_slug
from .store import write_private

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger("telerouter.tasks")

TICK_S = 15
CATCHUP_S = 15 * 60          # a run missed while telerouter was down runs late if it is this recent
RUNS_KEPT = 20
REPLY_MAX = 4000
REMIND_AGAIN = 0.1           # after "keep it", remind again once the context grew by 10% of the limit
REMINDER_TIMEOUT = 300

REMINDER = (
    "Just a gentle note from telerouter, nothing you have to do: your context is now about {now}k tokens, "
    "above the {limit}k advised for this scheduled task. If you are not in the middle of something and you "
    "don't think you need the earlier conversation any more, you can free it up: reply with a line that says "
    "only TELEROUTER_CLEAR to start fresh, or TELEROUTER_COMPACT: <what to keep> to keep a summary. Save "
    "anything worth remembering (for example in a notes file in this folder) before clearing. If you would "
    "rather keep your context, that's completely fine: just reply \"keeping it\". No need to tell anyone about "
    "this note.")
_CLEAR_RE = re.compile(r"^\W*TELEROUTER_CLEAR\W*$", re.M)
_COMPACT_RE = re.compile(r"^\W*TELEROUTER_COMPACT\b[:\s]*(.*)$", re.M)


def reminder_answer(reply: str) -> tuple[str, str]:
    """('clear' | 'compact' | 'keep', what to keep) from the agent's answer to the reminder."""
    if _CLEAR_RE.search(reply or ""):
        return "clear", ""
    m = _COMPACT_RE.search(reply or "")
    if m:
        return "compact", m.group(1).strip().strip("`*").strip()[:500]
    return "keep", ""


class Scheduler:
    def __init__(self, engine: "Engine"):
        self.engine = engine
        self.path = engine.store.dir / "tasks.json"
        self.state: dict[str, dict] = self._load()
        self.running: dict[str, asyncio.Task] = {}

    # -- state ------------------------------------------------------------------------------------------
    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            write_private(self.path, json.dumps(self.state))
        except OSError as e:
            log.warning("could not write tasks.json: %s", e)

    def st(self, tid: str) -> dict:
        s = self.state.setdefault(tid, {})
        s.setdefault("runs", [])
        s.setdefault("sessions", [])
        s.setdefault("next", None)
        s.setdefault("reminded", 0)
        return s

    @property
    def tasks(self) -> list[ScheduledTask]:
        return self.engine.store.config.tasks

    def task(self, tid: str) -> ScheduledTask | None:
        return next((t for t in self.tasks if t.id == tid), None)

    # -- planning ---------------------------------------------------------------------------------------
    def plan(self, t: ScheduledTask, after: float | None = None) -> None:
        """Set the next run from `after` (now) on; called when a task is created or changed."""
        s = self.st(t.id)
        s["next"] = next_run(t, after or time.time()) if t.enabled else None
        self._save()

    def forget(self, tid: str) -> None:
        self.state.pop(tid, None)
        self._save()

    def resume(self) -> None:
        """At start-up: forget removed tasks, close runs a restart cut off, re-plan missed runs."""
        now = time.time()
        known = {t.id for t in self.tasks}
        for tid in list(self.state):
            if tid not in known:
                self.state.pop(tid)
        for t in self.tasks:
            s = self.st(t.id)
            for r in s["runs"]:
                if r.get("status") == "running":            # telerouter restarted during that run
                    r.update(status="error", error="telerouter restarted during the run", ended=now)
            nxt = s["next"]
            if not t.enabled:
                s["next"] = None
            elif nxt is None or now - nxt > CATCHUP_S:
                if nxt is not None:
                    log.info("task %s: the run due %s was missed", t.name, _local(nxt, t))
                s["next"] = next_run(t, now)
        self._save()

    async def loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(TICK_S)

    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        for t in list(self.tasks):
            s = self.st(t.id)
            if not t.enabled or s["next"] is None or s["next"] > now:
                continue
            due = s["next"]
            s["next"] = next_run(t, max(now, due))
            if t.schedule == "once":
                t.enabled = False
                self.engine.store.save()
            if t.id in self.running:
                self._record(t, {"trigger": "schedule", "status": "skipped", "started": now, "ended": now,
                                 "error": "the previous run was still going"})
                log.info("task %s: skipped, the previous run is still going", t.name)
                continue
            self.run_now(t, "schedule")
        self._save()

    # -- running ----------------------------------------------------------------------------------------
    def run_now(self, t: ScheduledTask, trigger: str = "manual") -> dict:
        if t.id in self.running:
            raise AgentError("it is running right now")
        run = {"id": secrets.token_hex(3), "trigger": trigger, "status": "running", "started": time.time(),
               "ended": None, "where": self.where(t), "reply": "", "error": ""}
        self._record(t, run)
        task = asyncio.create_task(self._run(t, run))
        self.running[t.id] = task
        task.add_done_callback(lambda _: self.running.pop(t.id, None))
        return run

    def _record(self, t: ScheduledTask, run: dict) -> None:
        s = self.st(t.id)
        run.setdefault("id", secrets.token_hex(3))
        s["runs"].append(run)
        del s["runs"][:-RUNS_KEPT]
        self._save()

    async def _run(self, t: ScheduledTask, run: dict) -> None:
        log.info("task %s: running (%s) in %s", t.name, run["trigger"], run["where"])
        try:
            reply = await self._execute(t, run)
            run.update(status="ok", reply=(reply or "")[:REPLY_MAX])
            log.info("task %s: done: %s", t.name, (reply or "")[:120].replace("\n", " "))
        except asyncio.CancelledError:
            run.update(status="error", error="stopped")
            raise
        except (AgentError, OSError, asyncio.TimeoutError) as e:
            run.update(status="error", error=str(e)[:500] or e.__class__.__name__)
            log.warning("task %s: failed: %s", t.name, run["error"])
        except Exception as e:
            run.update(status="error", error=str(e)[:500] or e.__class__.__name__)
            log.exception("task %s: failed", t.name)
        finally:
            run["ended"] = time.time()
            self._save()

    def where(self, t: ScheduledTask) -> str:
        store = self.engine.store
        if t.target == "agent":
            a = store.agent(t.agent_id) if t.agent_id else store.default_agent()
            return f"{a.name if a else 'the agent'}'s conversation"
        m = store.machine(t.machine_id)
        mname = m.name if m else "?"
        if t.target == "task_session":
            return f"tmux {t.tmux_name} on {mname}"
        return f"a new tmux session on {mname}"

    def _header(self, t: ScheduledTask, run: dict) -> str:
        at = dt.datetime.fromtimestamp(run["started"], ZoneInfo(t.timezone)).strftime("%Y-%m-%d %H:%M")
        text = (f"scheduled task \"{t.name}\" ({at}): an automatic run nobody is watching. Do the task, then "
                f"end with a short summary of the result (telerouter keeps it)")
        if t.calls == "task":
            text += ". Don't phone, message or announce anything about this run unless the task asks you to"
        return text

    async def _execute(self, t: ScheduledTask, run: dict) -> str:
        e = self.engine
        timeout = t.timeout_minutes * 60
        if t.target == "agent":
            a = e.store.agent(t.agent_id) if t.agent_id else e.store.default_agent()
            rt = e.agents.get(a.id) if a else None
            if not rt:
                raise AgentError("the agent is not running")
            text = t.prompt
            if t.calls == "task":
                text += ("\n\n(Don't phone, message or announce anything about this run unless the task above asks "
                         "you to.)")
            return await rt.ask(text, Source("schedule", f"scheduled task \"{t.name}\""), timeout=timeout)
        mrt = e.machine_rts.get(t.machine_id)
        if not mrt:
            raise AgentError("the machine is disabled or not set up")
        if mrt.state != "ready":
            raise AgentError(f"{mrt.machine.name} is not reachable ({mrt.state}{': ' + mrt.error if mrt.error else ''})")
        s = self.st(t.id)
        if t.target == "new_session":
            stamp = dt.datetime.fromtimestamp(run["started"], ZoneInfo(t.timezone)).strftime("%m%d-%H%M")
            name = f"{task_slug(t.name, 28)}-{stamp}"
            if await mrt.has_session(name):
                name += "-" + secrets.token_hex(2)
            await mrt.new_session(name, t.folder, by_admin=True)
            s["sessions"].append(name)
            self._save()
            await self._prune(t, mrt)
        else:
            name = t.tmux_name
            if (name in s["sessions"] and mrt.machine.agent_kind == "claude" and await mrt.has_session(name)
                    and not await mrt.claude_transcript(name)):
                # our session, but claude in it has exited (left at its shell): start it again
                log.info("task %s: no Claude Code is running in %s - starting it again", t.name, name)
                await mrt.kill_session(name)
            if not await mrt.has_session(name):
                await mrt.new_session(name, t.folder, by_admin=True)
                s["reminded"] = 0
                if name not in s["sessions"]:
                    s["sessions"] = [name]
                self._save()
        run["session"] = name
        reply = await mrt.ask(name, t.prompt, "", header=self._header(t, run), timeout=timeout)
        if t.target == "task_session" and t.context_limit_k and mrt.machine.agent_kind == "claude":
            await self._context_check(t, mrt, name, run)
        return reply

    async def _prune(self, t: ScheduledTask, mrt) -> None:
        """new_session: close the sessions of older runs, keeping the last keep_sessions."""
        s = self.st(t.id)
        while len(s["sessions"]) > t.keep_sessions:
            old = s["sessions"].pop(0)
            try:
                await mrt.kill_session(old)
            except AgentError as ex:
                log.warning("task %s: could not close %s: %s", t.name, old, ex)
        self._save()

    async def _context_check(self, t: ScheduledTask, mrt, session: str, run: dict) -> None:
        """After a run: above the advised context size, a gentle reminder; the agent decides."""
        s = self.st(t.id)
        tokens = await mrt.context_tokens(session)
        if tokens is None:
            return
        run["context_k"] = round(tokens / 1000)
        limit = t.context_limit_k * 1000
        if tokens < limit:
            s["reminded"] = 0
            return
        if s["reminded"] and tokens < s["reminded"] + limit * REMIND_AGAIN:
            return                                    # it chose to keep it; don't nag after every run
        log.info("task %s: context %dk is above the advised %dk - reminding the agent", t.name, tokens // 1000,
                 t.context_limit_k)
        header = f"scheduled task \"{t.name}\": context reminder"
        text = REMINDER.format(now=round(tokens / 1000), limit=t.context_limit_k)
        try:
            answer = await mrt.ask(session, text, "", header=header, timeout=REMINDER_TIMEOUT)
        except AgentError as ex:
            run["context_note"] = f"reminder not answered: {ex}"
            return
        choice, keep = reminder_answer(answer)
        if choice == "keep":
            s["reminded"] = tokens
            run["context_note"] = "reminded; the agent kept its context"
        else:
            await mrt.reset_context(session, choice, keep)
            s["reminded"] = 0
            run["context_note"] = "reminded; the agent " + ("cleared its context" if choice == "clear"
                                                            else "compacted its context")
        log.info("task %s: %s", t.name, run["context_note"])
        self._save()

    def stop(self) -> None:
        for task in list(self.running.values()):
            task.cancel()

    def stop_run(self, tid: str) -> bool:
        task = self.running.get(tid)
        if not task:
            return False
        task.cancel()
        return True

    # -- UI -----------------------------------------------------------------------------------------------
    def info(self, t: ScheduledTask) -> dict:
        s = self.st(t.id)
        return {"next": s["next"], "next_local": _local(s["next"], t) if s["next"] else "",
                "running": t.id in self.running, "schedule_text": describe(t),
                "where": self.where(t), "sessions": list(s["sessions"]), "runs": list(reversed(s["runs"][-10:]))}


def _local(ts: float, t: ScheduledTask) -> str:
    return dt.datetime.fromtimestamp(ts, ZoneInfo(t.timezone)).strftime("%a %Y-%m-%d %H:%M")
