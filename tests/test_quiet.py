"""Quiet hours: time window, desk sensors (Home Assistant), blocking calls, silent long tasks."""

import asyncio
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from telerouter.models import Agent, Config, ExtraMcpServer, QuietHours
from telerouter.quiet import Quiet, in_window


def test_window_crosses_midnight():
    t = lambda h, m=0: dt.datetime(2026, 10, 17, h, m)  # noqa: E731
    assert in_window(t(23, 30), "23:00", "08:00") and in_window(t(3), "23:00", "08:00")
    assert not in_window(t(8), "23:00", "08:00") and not in_window(t(14), "23:00", "08:00")
    assert in_window(t(14), "13:00", "15:00") and not in_window(t(16), "13:00", "15:00")
    assert not in_window(t(1), "00:00", "00:00")


def test_settings_validation():
    with pytest.raises(ValueError):
        QuietHours(start="25:00")
    with pytest.raises(ValueError):
        QuietHours(timezone="Mars/Olympus")
    with pytest.raises(ValueError):
        QuietHours(desk_sensors=["not an entity"])
    assert QuietHours(desk_sensors=["binary_sensor.desk_motion", " "]).desk_sensors == ["binary_sensor.desk_motion"]


class _Store:
    def __init__(self, cfg):
        self.config = cfg


class _Eng:
    def __init__(self, cfg):
        self.store = _Store(cfg)


def quiet(states, **q):
    cfg = Config(quiet=QuietHours(enabled=True, start="00:00", end="23:59", timezone="UTC",
                                  desk_sensors=["binary_sensor.desk"], **q),
                 agents=[Agent(name="J", mcp_servers=[ExtraMcpServer(name="home-assistant", url="https://ha.lan/api/mcp",
                                                                     token="llat")])])
    qq = Quiet(_Eng(cfg))

    async def fake_ha(path):
        assert path == "/api/states"
        return states
    qq._ha = fake_ha
    return qq


def ago(minutes):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)).isoformat()


async def test_calls_blocked_unless_at_the_desk():
    q = quiet([{"entity_id": "binary_sensor.desk", "state": "on", "last_changed": ago(1)}])
    assert q.ha_target() == ("https://ha.lan", "llat")             # from the agent's HA MCP server
    assert (await q.calls_allowed())[0]
    q = quiet([{"entity_id": "binary_sensor.desk", "state": "off", "last_changed": ago(4)}], desk_hold_minutes=10)
    assert (await q.calls_allowed())[0]                               # motion 4 min ago: still at the desk
    q = quiet([{"entity_id": "binary_sensor.desk", "state": "off", "last_changed": ago(30)}], desk_hold_minutes=10)
    ok, why = await q.calls_allowed()
    assert not ok and why.startswith("quiet hours")
    q = quiet([], block_calls=False)
    assert (await q.calls_allowed())[0]


async def test_mcp_refuses_agent_calls_in_quiet_hours():
    from telerouter.mcp import McpServer, ToolError

    class E:
        pass
    e = E()
    e.quiet = quiet([{"entity_id": "binary_sensor.desk", "state": "off", "last_changed": ago(300)}])
    m = McpServer.__new__(McpServer)
    m.engine = e
    with pytest.raises(ToolError, match="quiet hours"):
        await m._quiet_gate("calls")


async def test_long_task_is_not_spoken_in_quiet_hours():
    from telerouter.voice.assistant import VoiceAssistant

    class E:
        pass
    e = E()
    e.quiet = quiet([])
    e.quiet.cfg.long_task_minutes = 1
    va = VoiceAssistant.__new__(VoiceAssistant)
    va.engine = e

    class Conn:
        name = "Kitchen"
    import telerouter.voice.assistant as mod
    real = asyncio.wait_for

    async def short_wait(coro, timeout):
        return await real(coro, 0.05)                 # 1 minute -> 50 ms for the test
    mod.asyncio.wait_for = short_wait
    try:
        async def slow():
            await asyncio.sleep(1)
            return "app built"

        async def quick():
            return "lights on"
        assert await va._quiet_wait(Conn(), slow()) is None
        assert await va._quiet_wait(Conn(), quick()) == "lights on"
    finally:
        mod.asyncio.wait_for = real
