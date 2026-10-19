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
    public, _ = create_app(settings, tls.ensure(settings.data_dir, []))
    with TestClient(public, base_url="https://testserver") as c:
        h = {"X-CSRF-Token": setup_admin(c)}
        r = c.post("/api/clients/enroll", json={"name": "Phone"}, headers=h).json()
        u = urlsplit(r["app_link"])
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        assert (u.scheme, u.netloc) == ("telerouter", "setup")
        assert q == {"v": "1", "server": "https://testserver", "ca": r["ca_sha256"], "code": r["code"],
                     "public": "https://jarvis.example.org"}
        got = c.post("/api/client/enroll", json={"code": q["code"], "platform": "android"}).json()
        assert got["token"].startswith("trc_")
