"""The phone app's side of the speaker protocol: a turn started by the device's own wake word, transcripts."""

import asyncio
import json
import wave
from types import SimpleNamespace

from telerouter.models import Client, Config, Speaker
from telerouter.voice.assistant import VoiceAssistant
from telerouter.voice.client_conn import ClientConn
from telerouter.voice.speaker import VirtualSpeaker

from .conftest import FIXTURES


def phone(wants_text=True):
    sent_text, sent_bytes = [], []

    async def st(t):
        sent_text.append(json.loads(t))

    async def sb(b):
        sent_bytes.append(b)
    dev = ClientConn(Client(id="0000000f", name="Phone"), sb, st)
    dev.wants_text = wants_text
    vs = VirtualSpeaker(Speaker(id="0000000f", name="Phone", mic="0000000f", output="client:0000000f"), dev, dev)
    return vs, dev, sent_text, sent_bytes


def assistant(heard="Turn on the light."):
    async def transcribe(audio, language, names):
        transcribe.audio = audio
        return heard
    eng = SimpleNamespace(store=SimpleNamespace(config=Config()), speech=SimpleNamespace(transcribe=transcribe),
                          calls=SimpleNamespace(active=lambda: []), contact_names=lambda: [])
    va = VoiceAssistant(eng)
    return va, transcribe


async def test_a_turn_started_on_the_phone_keeps_the_audio_sent_right_after_it():
    vs, dev, texts, played = phone()
    va, transcribe = assistant()
    spoken = []

    async def ask(conn, text):
        return "Done, the light is on."

    async def speak(conn, text):
        spoken.append(text)
    va._ask_agent, va.speak = ask, speak
    va.start_turn(vs, cue=False)
    assert vs.state == "listening"                       # at once: no server beep, no waiting for the task
    with wave.open(str(FIXTURES / "speech.wav")) as w:
        speech = w.readframes(w.getnframes())
    for i in range(0, len(speech) + 48000, 640):          # sent before the turn's task ever ran
        va.mic(vs, speech[i:i + 640] or bytes(640))
    await asyncio.wait_for(vs.busy_task, 10)
    assert len(transcribe.audio) > 32000                  # the whole utterance reached speech recognition
    assert spoken == ["Done, the light is on."]
    assert {"type": "transcript", "role": "user", "text": "Turn on the light."} in texts
    starts = [t for t in texts if t.get("type") == "state"]
    assert starts[0] == {"type": "state", "state": "listening"}
    # no start blip was played by the server (the phone played its own); the end blip still is
    assert len(played) == 0 or all(len(b) < 32000 for b in played)


async def test_transcripts_only_go_to_devices_that_asked_for_them():
    vs, dev, texts, _ = phone(wants_text=False)
    vs.transcript("assistant", "Hello")
    await asyncio.sleep(0)
    assert not [t for t in texts if t.get("type") == "transcript"]
    vs2, dev2, texts2, _ = phone(wants_text=True)
    vs2.transcript("assistant", "Hello")
    await asyncio.sleep(0)
    assert texts2 == [{"type": "transcript", "role": "assistant", "text": "Hello"}]


def test_device_enrollment_gives_the_android_app_a_setup_link(settings):
    from urllib.parse import parse_qs, urlsplit

    from fastapi.testclient import TestClient

    from telerouter import tls
    from telerouter.web.app import create_app

    from .test_web import setup_admin
    settings.public_url = "https://jarvis.example.org"
    t = tls.ensure(settings.data_dir, [])
    public, _ = create_app(settings, t)
    with TestClient(public, base_url="https://testserver") as c:
        h = {"X-CSRF-Token": setup_admin(c)}
        r = c.post("/api/clients/enroll", json={"name": "Phone"}, headers=h).json()
        u = urlsplit(r["app_link"])
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        assert (u.scheme, u.netloc) == ("telerouter", "setup")
        # the page came through a name telerouter's certificate doesn't cover (a proxy): "server" is that page's
        # address (a public certificate), "lan" an address the own certificate covers (for the pinned CA)
        lan = q.pop("lan")
        assert lan.startswith("https://") and lan.rsplit(":", 1)[1] == str(settings.https_port)
        assert lan[8:].rsplit(":", 1)[0] in t.names
        assert q == {"v": "1", "server": "https://testserver", "ca": r["ca_sha256"], "code": r["code"],
                     "public": "https://jarvis.example.org"}
        assert r["server"] == lan                              # the Linux installer pins too: own address
        # opened with a name the certificate covers: that name is the LAN address as well
        r2 = c.post("/api/clients/enroll", json={"name": "Phone 2"}, headers={**h, "host": "localhost:8443"}).json()
        assert "lan=https%3A%2F%2Flocalhost%3A8443" in r2["app_link"]
        got = c.post("/api/client/enroll", json={"code": q["code"], "platform": "android"}).json()
        assert got["token"].startswith("trc_")


def _two_devices():
    phone_vs, _, phone_text, _ = phone()
    spk_vs, _, _, spk_played = phone(wants_text=False)
    spk_vs.cfg = Speaker(id="0000000e", name="Raqqa laptop", mic="0000000e", output="client:0000000e")
    spk_vs.id = "0000000e"
    va, _ = assistant()
    va.engine.speakers = {phone_vs.id: phone_vs, spk_vs.id: spk_vs}
    return va, phone_vs, spk_vs


async def test_one_hey_jarvis_two_devices_the_phone_answers():
    import telerouter.voice.assistant as A
    # the speaker reacts first (server-side wake word), the phone's talk arrives a moment later
    va, phone_vs, spk = _two_devices()
    va._on_wake(spk, "hey_jarvis")
    await asyncio.sleep(0.05)
    assert spk.state == "listening"
    va.start_turn(phone_vs, cue=False)
    await asyncio.sleep(0.05)
    assert spk.busy_task.done() and spk.state == "idle"          # the speaker stood down
    assert phone_vs.state == "listening"
    # the other order: the phone first, the speaker's wake word right after is ignored
    va2, phone2, spk2 = _two_devices()
    va2.start_turn(phone2, cue=False)
    va2._on_wake(spk2, "hey_jarvis")
    assert spk2.busy_task is None and spk2.state == "idle"
    # a wake word on the speaker later is a new request: it answers
    va2._device_wake = (A.time.monotonic() - A.SAME_WAKE_S - 1, phone2.id)
    va2._on_wake(spk2, "hey_jarvis")
    await asyncio.sleep(0.05)
    assert spk2.state == "listening"
    for t in (phone_vs.busy_task, phone2.busy_task, spk2.busy_task):
        t.cancel()


async def test_a_phone_that_wants_text_only_gets_no_speech():
    async def synthesize(text, voice):
        yield b"\x01\x00" * 1600

    for speech, expect_audio in ((False, False), (True, True)):
        vs, dev, texts, played = phone()
        dev.speech = speech
        dev.start()
        va, _ = assistant()
        va.engine.speech.synthesize = synthesize
        await va.speak(vs, "The lights are on.")
        await asyncio.sleep(0.05)
        await dev.close()
        assert {"type": "transcript", "role": "assistant", "text": "The lights are on."} in texts
        assert bool(played) is expect_audio
        assert vs.state == "idle"


def test_phone_connects_with_the_token_header_behind_a_proxy(settings):
    from fastapi.testclient import TestClient

    from telerouter import tls
    from telerouter.web.app import create_app

    from .test_web import setup_admin
    public, _ = create_app(settings, tls.ensure(settings.data_dir, []))
    with TestClient(public, base_url="https://testserver") as c:
        h = {"X-CSRF-Token": setup_admin(c)}
        code = c.post("/api/clients/enroll", json={"name": "Zfold"}, headers=h).json()["code"]
        got = c.post("/api/client/enroll", json={"code": code, "platform": "android"}).json()
        # Authorization belongs to the proxy (e.g. Authentik); the device token travels in its own header
        with c.websocket_connect("/ws/client", headers={"X-Telerouter-Token": got["token"],
                                                        "Authorization": "Bearer proxy-session"}) as ws:
            cfg = json.loads(ws.receive_text())
            assert cfg["type"] == "config" and cfg["client_id"] == got["client_id"] and cfg["name"] == "Zfold"
            ws.send_text(json.dumps({"type": "hello", "version": "0.2.0", "platform": "android", "aec": False,
                                     "transcript": True, "speech": False}))
            ws.send_text(json.dumps({"type": "pong"}))
            conn = None
            for _ in range(50):
                conn = c.app.state.engine.clients.get(got["client_id"])
                if conn is not None and conn.wants_text:
                    break
                import time
                time.sleep(0.02)
            assert conn is not None and conn.wants_text and conn.speech is False
