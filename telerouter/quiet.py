"""Quiet hours (do not disturb): is it quiet now, and is the owner at the desk (Home Assistant sensors)?"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import httpx

from .models import QuietHours

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger("telerouter.quiet")
CACHE_S = 20


def in_window(now: dt.datetime, start: str, end: str) -> bool:
    """`now` (local time) inside [start, end); windows may cross midnight (23:00-08:00)."""
    s = dt.time.fromisoformat(start)
    e = dt.time.fromisoformat(end)
    t = now.time()
    if s == e:
        return False
    return s <= t < e if s < e else (t >= s or t < e)


class Quiet:
    def __init__(self, engine: "Engine"):
        self.engine = engine
        self._desk: tuple[float, bool, str] = (0.0, False, "")

    @property
    def cfg(self) -> QuietHours:
        return self.engine.store.config.quiet

    def active(self, now: dt.datetime | None = None) -> bool:
        q = self.cfg
        if not q.enabled:
            return False
        now = now or dt.datetime.now(ZoneInfo(q.timezone))
        return in_window(now, q.start, q.end)

    def ha_target(self) -> tuple[str, str]:
        """(base URL, token): the explicit setting, else an agent's Home Assistant MCP server."""
        q = self.cfg
        if q.ha_url and q.token:
            return q.ha_url, q.token
        for a in self.engine.store.config.agents:
            for s in a.mcp_servers:
                if s.enabled and s.token and s.url.rstrip("/").endswith("/api/mcp"):
                    return s.url.rstrip("/")[: -len("/api/mcp")], s.token
        return "", ""

    async def _ha(self, path: str):
        base, token = self.ha_target()
        if not base:
            raise RuntimeError("no Home Assistant connection (add it to an agent's extra MCP servers)")
        async with httpx.AsyncClient(timeout=8, follow_redirects=False) as c:
            r = await c.get(base + path, headers={"Authorization": f"Bearer {token}"})
            r.raise_for_status()
            return r.json()

    async def at_desk(self) -> tuple[bool, str]:
        """Any desk sensor on, or on within desk_hold_minutes. (False, reason) when unknown."""
        q = self.cfg
        if not q.desk_sensors:
            return False, "no desk sensors chosen"
        now = time.monotonic()
        if now - self._desk[0] < CACHE_S:
            return self._desk[1], self._desk[2]
        try:
            states = await self._ha("/api/states")
        except Exception as e:
            log.warning("Home Assistant not reachable for the desk sensors: %s", e)
            self._desk = (now, False, "Home Assistant not reachable")
            return self._desk[1], self._desk[2]
        by_id = {s.get("entity_id"): s for s in states if isinstance(s, dict)}
        here, why = False, "no motion at the desk"
        for eid in q.desk_sensors:
            s = by_id.get(eid)
            if not s:
                continue
            if s.get("state") == "on":
                here, why = True, f"{eid} is on"
                break
            try:
                changed = dt.datetime.fromisoformat(s.get("last_changed", "").replace("Z", "+00:00"))
                ago = (dt.datetime.now(dt.timezone.utc) - changed).total_seconds()
            except ValueError:
                continue
            if s.get("state") == "off" and ago < q.desk_hold_minutes * 60:
                here, why = True, f"{eid} saw motion {int(ago // 60)} min ago"
                break
        self._desk = (now, here, why)
        return here, why

    async def calls_allowed(self) -> tuple[bool, str]:
        """Calls (incoming, or placed by agents) may ring now?"""
        if not self.active() or not self.cfg.block_calls:
            return True, ""
        here, why = await self.at_desk()
        return (True, f"quiet hours, but at the desk ({why})") if here else (False, f"quiet hours ({why})")

    async def sensors(self) -> list[dict]:
        """Motion/occupancy/presence sensors in Home Assistant, to pick the desk ones."""
        states = await self._ha("/api/states")
        out = []
        for s in states:
            eid = s.get("entity_id", "")
            attrs = s.get("attributes") or {}
            if eid.startswith("binary_sensor.") and attrs.get("device_class") in ("motion", "occupancy", "presence",
                                                                                     "moving"):
                out.append({"entity_id": eid, "name": attrs.get("friendly_name") or eid, "state": s.get("state"),
                            "device_class": attrs.get("device_class")})
        return sorted(out, key=lambda x: x["name"].lower())

    async def status(self) -> dict:
        here, why = await self.at_desk() if self.cfg.enabled else (False, "")
        allowed, reason = await self.calls_allowed()
        return {"active": self.active(), "at_desk": here, "desk_reason": why, "calls_allowed": allowed,
                "reason": reason, "ha": bool(self.ha_target()[0])}
