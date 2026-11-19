"""Smart display screens (0.23): the admin API, what the device gets over its WebSocket, task summaries."""

import json
import time

import pytest

from telerouter.models import Screen, ScreenPanel


def _app(settings):
    from fastapi.testclient import TestClient

    from telerouter import tls
    from telerouter.web.app import create_app
    public, _ = create_app(settings, tls.ensure(settings.data_dir, []))
    return TestClient(public, base_url="https://testserver")


def _device(c, h, name="iMac"):
    code = c.post("/api/clients/enroll", json={"name": name}, headers=h).json()["code"]
    return c.post("/api/client/enroll", json={"code": code, "platform": "linux"}).json()


def _receive(ws, kind, n=10):
    for _ in range(n):
        m = json.loads(ws.receive_text())
        if m.get("type") == kind:
            return m
    raise AssertionError(f"no {kind} message")


def test_screen_models_validate_layouts():
    p = ScreenPanel(kind="web", url="https://ha.example/lovelace", x=80, w=40)
    assert p.w == 20                                       # clipped to the screen's right edge
    for bad in ({"kind": "web", "url": "javascript:alert(1)"}, {"kind": "web", "url": ""},
                {"kind": "vnc", "host": ""}, {"kind": "vnc", "host": "a b"}, {"kind": "task", "task_id": ""},
                {"kind": "web", "url": "https://x", "id": "<x>"}, {"kind": "web", "url": "https://x", "title": "<b>"}):
        with pytest.raises(ValueError):
            ScreenPanel(**bad)
    with pytest.raises(ValueError):
        Screen(client_id="a", background="red")
    with pytest.raises(ValueError):
        Screen(client_id="a", clock={"font": "comic"})
    with pytest.raises(ValueError):
        Screen(client_id="a", clock={"font": "custom", "font_name": "x;}body{"})
    with pytest.raises(ValueError):
        Screen(client_id="a", panels=[ScreenPanel(id="same", kind="vnc", host="h")] * 2)
    with pytest.raises(ValueError):
        Screen(client_id="a", panels=[ScreenPanel(kind="vnc", host="h") for _ in range(13)])


def test_screens_api_device_message_and_task_summaries(settings):
    from telerouter import screens
    from telerouter.models import ScheduledTask

    from .test_web import setup_admin
    with _app(settings) as c:
        h = {"X-CSRF-Token": setup_admin(c)}
        eng = c.app.state.engine
        cfg = eng.store.config
        dev = _device(c, h)
        cid = next(x.id for x in cfg.clients if x.name == "iMac")
        _device(c, h, "Kitchen")                           # a plain speaker: no screen
        mail = ScheduledTask(name="Mail summary", prompt="summarize my mail")
        cfg.tasks.append(mail)
        assert c.get("/api/screens").json()["screens"] == []

        with c.websocket_connect("/ws/client", headers={"X-Telerouter-Token": dev["token"]}) as ws:
            _receive(ws, "config")
            ws.send_text(json.dumps({"type": "hello", "version": "0.12.0", "platform": "Linux x86_64",
                                     "transcript": True, "display": True}))
            first = _receive(ws, "display")
            assert first["screen"]["clock"]["show"] is True and first["screen"]["panels"] == []
            ws.send_text(json.dumps({"type": "display_size", "width": 1920, "height": 1080}))
            ws.send_text(json.dumps({"type": "display_size", "width": 10**9, "height": 5}))   # ignored
            for _ in range(50):
                if any(s.width for s in cfg.screens):
                    break
                time.sleep(0.02)
            lst = c.get("/api/screens").json()
            assert [s["name"] for s in lst["screens"]] == ["iMac"] and lst["screens"][0]["online"]
            assert lst["screens"][0]["screen"]["width"] == 1920 and lst["screens"][0]["screen"]["height"] == 1080
            assert {"id": mail.id, "name": "Mail summary"} in lst["tasks"]
            assert [d["name"] for d in lst["other_devices"]] == ["Kitchen"]

            body = {"background": "#101010", "conversation": False,
                    "clock": {"x": 20, "y": 15, "size": 12, "font": "serif", "color": "#ffffff", "seconds": True},
                    "panels": [{"id": "cam1", "kind": "vnc", "title": "Front door", "host": "nvr.lan", "port": 5901,
                                "password": "s3cret", "x": 50, "y": 5, "w": 45, "h": 45},
                               {"id": "mail", "kind": "task", "task_id": mail.id, "x": 50, "y": 52, "w": 45, "h": 44},
                               {"id": "ha", "kind": "web", "url": "https://ha.example/lovelace/0", "x": 2, "y": 40,
                                "w": 45, "h": 55, "refresh_minutes": 30}]}
            # CSRF: a state change without the token is refused
            assert c.put(f"/api/screens/{cid}", json=body).status_code == 403
            r = c.put(f"/api/screens/{cid}", json=body, headers=h)
            assert r.status_code == 200, r.text
            out = r.json()
            assert out["sent"] is True
            cam = out["screen"]["panels"][0]
            assert cam["password"] == "" and cam["password_set"] is True     # write-only in the API
            msg = _receive(ws, "display")
            assert msg["screen"]["panels"][0]["password"] == "s3cret"        # the device itself gets it
            assert msg["font_family"].startswith("Georgia")
            assert msg["tasks"][mail.id]["status"] == "never" and msg["tasks"][mail.id]["name"] == "Mail summary"

            # an empty password keeps the stored one; a different server needs it again
            body["panels"][0]["password"] = ""
            c.put(f"/api/screens/{cid}", json=body, headers=h)
            assert screens.screen_for(cfg, cid).panels[0].password == "s3cret"
            _receive(ws, "display")
            body["panels"][0]["host"] = "other.lan"
            c.put(f"/api/screens/{cid}", json=body, headers=h)
            assert screens.screen_for(cfg, cid).panels[0].password == ""
            _receive(ws, "display")

            # a task run updates the screens that show it: the last good summary plus a newer failure
            st = eng.scheduler.st(mail.id)
            st["runs"] += [{"status": "ok", "reply": "## Inbox\n- 3 new mails\n- **bill** due", "ended": 100.0},
                           {"status": "error", "error": "the machine did not answer", "ended": 200.0}]
            summ = screens.task_summary(eng, mail.id)
            assert summ["text"].startswith("## Inbox") and summ["ended"] == 100.0
            assert summ["error"] == "the machine did not answer" and summ["status"] == "ok"
            mail.name = "Mails"

            async def run_ended():                         # what the scheduler calls when a run starts/ends
                eng.scheduler._screens(mail.id)
            c.portal.call(run_ended)
            msg = _receive(ws, "display")
            assert msg["tasks"][mail.id]["name"] == "Mails" and "3 new mails" in msg["tasks"][mail.id]["text"]

            # validation
            bad = dict(body, panels=[{"kind": "task", "task_id": "nope"}])
            assert c.put(f"/api/screens/{cid}", json=bad, headers=h).status_code == 422
            bad = dict(body, panels=[{"kind": "web", "url": "file:///etc/passwd"}])
            assert c.put(f"/api/screens/{cid}", json=bad, headers=h).status_code == 422
            assert c.put("/api/screens/nosuchdev", json=body, headers=h).status_code == 404

            # reset
            r = c.delete(f"/api/screens/{cid}", headers=h)
            assert r.status_code == 200 and r.json()["screen"]["panels"] == []
            assert _receive(ws, "display")["screen"]["panels"] == []
        # deleting the device forgets its screen
        c.put(f"/api/screens/{cid}", json={"panels": []}, headers=h)
        assert any(s.client_id == cid for s in cfg.screens)
        c.delete(f"/api/clients/{cid}", headers=h)
        assert not any(s.client_id == cid for s in cfg.screens)


def test_screens_need_a_login(settings):
    with _app(settings) as c:
        assert c.get("/api/screens").status_code == 401


def test_a_removed_task_shows_as_missing(settings):
    from types import SimpleNamespace

    from telerouter import screens
    from telerouter.models import Config
    eng = SimpleNamespace(store=SimpleNamespace(config=Config()), scheduler=SimpleNamespace(task=lambda tid: None))
    assert screens.task_summary(eng, "gone")["status"] == "missing"
    assert screens.font_family(Screen(client_id="x", clock={"font": "custom", "font_name": "Ubuntu"})) \
        .startswith('"Ubuntu"')
