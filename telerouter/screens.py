"""Smart display screens (0.23): what telerouter sends a display client about its home screen.

A screen is a speaker device whose client serves the smart display page (client >= 0.12 says hello
`"display": true`). Its settings and home-screen layout are `models.Screen` (config.json, keyed by the device's
`Client.id`). telerouter sends them to the device as one `display` message when it connects, whenever the admin
saves them, and again when a scheduled task shown on it finishes a run:

  {"type": "display", "screen": {...Screen, VNC passwords included...}, "font_family": "<css stack>",
   "tasks": {"<task id>": {"name", "status", "ended", "text", "error", "running"}}}

VNC passwords go only to that device over its own authenticated TLS connection (the client keeps them in memory
and never hands them to the page). Everything else here is pure data shaping, no I/O.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .models import FONTS, Config, Screen

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger("telerouter.screens")

SUMMARY_MAX = 6000


def screen_for(cfg: Config, client_id: str) -> Screen:
    """The stored screen of a device, or the defaults (not stored)."""
    return next((s for s in cfg.screens if s.client_id == client_id), None) or Screen(client_id=client_id)


def font_family(sc: Screen) -> str:
    c = sc.clock
    if c.font == "custom":
        return f'"{c.font_name}", system-ui, sans-serif' if c.font_name else FONTS["system"]
    return FONTS.get(c.font) or FONTS["system"]


def task_summary(engine: "Engine", task_id: str) -> dict:
    """The task's newest summary for the screen: the last successful run's answer, plus the latest error."""
    sched = getattr(engine, "scheduler", None)
    t = sched.task(task_id) if sched else None
    if t is None:
        return {"name": "", "status": "missing", "ended": None, "text": "", "error": "this task was removed",
                "running": False}
    runs = list(sched.st(t.id)["runs"])
    ok = next((r for r in reversed(runs) if r.get("status") == "ok"), None)
    last = next((r for r in reversed(runs) if r.get("status") in ("ok", "error")), None)
    out = {"name": t.name, "status": "ok" if ok else (last or {}).get("status", "never"),
           "ended": (ok or {}).get("ended"), "text": ((ok or {}).get("reply") or "").strip()[:SUMMARY_MAX],
           "error": "", "running": t.id in sched.running}
    if last is not None and last.get("status") == "error" and last is not ok:
        out["error"] = str(last.get("error") or "the last run failed")[:300]
        out["error_at"] = last.get("ended")
    return out


def message(engine: "Engine", client_id: str) -> dict:
    sc = screen_for(engine.store.config, client_id)
    tasks = {p.task_id: task_summary(engine, p.task_id) for p in sc.panels if p.kind == "task"}
    return {"type": "display", "screen": sc.model_dump(), "font_family": font_family(sc), "tasks": tasks}


async def push(engine: "Engine", client_id: str) -> bool:
    """Send the screen to its device if it is connected and has a display."""
    conn = engine.clients.get(client_id)
    if conn is None or not getattr(conn, "display", False):
        return False
    await conn.send_json(message(engine, client_id))
    return True


def task_changed(engine: "Engine", task_id: str) -> None:
    """A scheduled task started or finished a run (or changed): refresh the screens that show it."""
    for sc in engine.store.config.screens:
        if any(p.kind == "task" and p.task_id == task_id for p in sc.panels):
            try:
                asyncio.get_running_loop().create_task(push(engine, sc.client_id))
            except RuntimeError:            # no loop (tests that call the scheduler synchronously)
                return


def merge_secrets(new: Screen, old: Screen | None) -> Screen:
    """An empty VNC password keeps the stored one of the same section, while it points at the same server."""
    if old is None:
        return new
    prev = {p.id: p for p in old.panels}
    for p in new.panels:
        o = prev.get(p.id)
        if p.kind == "vnc" and not p.password and o is not None and o.kind == "vnc" \
                and (o.host, o.port) == (p.host, p.port):
            p.password = o.password
    return new


def set_size(engine: "Engine", client_id: str, width, height) -> bool:
    """The page reported its size (CSS pixels): kept for the editor's aspect ratio. True if it changed."""
    try:
        w, h = int(width), int(height)
    except (TypeError, ValueError):
        return False
    if not (100 <= w <= 16384 and 100 <= h <= 16384):
        return False
    cfg = engine.store.config
    sc = next((s for s in cfg.screens if s.client_id == client_id), None)
    if sc is None:
        if len(cfg.screens) >= 50:
            return False
        sc = Screen(client_id=client_id)
        cfg.screens.append(sc)
    if (sc.width, sc.height) == (w, h):
        return False
    sc.width, sc.height = w, h
    engine.store.save()
    return True
