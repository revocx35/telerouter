"""SIP calls through the real engine with an in-process phone/registrar; speech and agent are stubbed."""

import asyncio
import wave

import pytest

from telerouter.engine import Engine
from telerouter.media import pcm
from telerouter.media.rtp import PortAllocator
from telerouter.models import Agent, Contact
from telerouter.router.sipaudio import SipAudio
from telerouter.sip.ua import Account, AccountConfig, UserAgent
from telerouter.store import Store

from .conftest import FIXTURES, free_port


class FakeSpeech:
    def __init__(self):
        self.heard = []

    async def transcribe(self, audio, language="", names=None):
        self.heard.append(len(audio))
        return "what time is it" if len(audio) > 8000 else ""

    async def synthesize_all(self, text, voice="", rate=16000):
        return pcm._tone([(500, 0.4)], 0.5)

    async def synthesize(self, text, voice="", rate=16000):
        yield pcm._tone([(500, 0.4)], 0.5)


class FakeAgent:
    def __init__(self):
        self.asked = []
        self.current_source = None

    async def ask(self, text, source, on_note=None):
        self.asked.append((text, source))
        self.current_source = source
        return "It is noon."

    def info(self):
        return {}

    async def stop(self):
        pass


class Rig:
    async def start(self, settings, tmp_path, pbx_host="127.0.0.1"):
        self.phone_port = free_port()
        store = Store(str(tmp_path))
        cfg = store.config
        cfg.pbx.enabled, cfg.pbx.host, cfg.pbx.port = True, pbx_host, self.phone_port
        cfg.pbx.username, cfg.pbx.password = "1011", "x"
        cfg.agents = [Agent(id="a1", name="Jarvis", host="10.0.0.9", username="me")]   # no host key: not started
        cfg.default_agent_id = "a1"
        cfg.contacts = [Contact(id="me", kind="pbx", name="Salih", number="2001", inbound="agent")]
        self.store = store
        self.ua = UserAgent(self.phone_port, PortAllocator(33200, 33239), advertise_ip="127.0.0.1")
        await self.ua.start()
        handler = self.ua.stack.request_handler

        async def handle(req, addr):
            if req.method == "REGISTER":
                self.ua.stack.respond(req, 200, "OK", [("Contact", f"{req.get('Contact')};expires=300")])
                return
            await handler(req, addr)
        self.ua.stack.request_handler = handle
        self.engine = Engine(settings, store)
        self.engine.speech = FakeSpeech()
        self.agent = FakeAgent()
        await self.engine.start()
        self.engine.agent_rts["a1"] = self.agent
        for _ in range(100):
            acc = self.engine.ua.accounts.get("pbx")
            if acc and acc.state == "registered":
                break
            await asyncio.sleep(0.02)
        assert self.engine.ua.accounts["pbx"].state == "registered"
        return self

    def phone(self, user="2001"):
        acc = Account(self.ua, AccountConfig(id=user, server="127.0.0.1", port=self.engine.settings.sip_port,
                                             username=user, password="", contact_user=user))
        self.ua.accounts[user] = acc
        return acc

    async def stop(self):
        await self.engine.stop()
        await self.ua.stop()


def speech_16k() -> bytes:
    with wave.open(str(FIXTURES / "speech.wav")) as w:
        return w.readframes(w.getnframes())


async def test_inbound_call_talks_to_the_agent(settings, tmp_path):
    rig = await Rig().start(settings, tmp_path)
    try:
        contact_user = rig.store.config.pbx.contact_user
        call = await rig.phone().dial(f"{contact_user}@127.0.0.1:{settings.sip_port}")
        await asyncio.wait_for(call.answered.wait(), 5)
        heard = bytearray()
        audio = SipAudio(call, on_audio=heard.extend)
        audio.start()
        await asyncio.sleep(2.0)                          # greeting + start blip
        assert len(heard) > 16000
        speech = speech_16k()
        for i in range(0, len(speech), 640):              # speak in real time
            audio.send(speech[i:i + 640])
            await asyncio.sleep(0.02)
        for _ in range(100):
            if rig.agent.asked:
                break
            await asyncio.sleep(0.05)
        assert rig.agent.asked and rig.agent.asked[0][0] == "what time is it"
        src = rig.agent.asked[0][1]
        assert src.kind == "phone" and "Salih" in src.label and src.call_id
        mark = len(heard)
        await asyncio.sleep(1.0)
        assert len(heard) > mark                          # the reply is played
        await call.hangup()
        for _ in range(50):
            if not rig.engine.calls.sessions:
                break
            await asyncio.sleep(0.05)
        assert not rig.engine.calls.sessions
        assert rig.store.history[-1]["mode"] == "agent"
    finally:
        await rig.stop()


async def test_unknown_caller_is_rejected_by_default(settings, tmp_path):
    rig = await Rig().start(settings, tmp_path)
    try:
        call = await rig.phone("3333").dial(f"{rig.store.config.pbx.contact_user}@127.0.0.1:{settings.sip_port}")
        await asyncio.wait_for(call.ended.wait(), 5)
        assert "603" in call.end_reason
        assert not rig.agent.asked
    finally:
        await rig.stop()


async def test_pbx_extension_only_takes_calls_from_the_pbx(settings, tmp_path):
    rig = await Rig().start(settings, tmp_path, pbx_host="127.0.0.2")      # the "PBX" is another address
    try:
        call = await rig.phone().dial(f"{rig.store.config.pbx.contact_user}@127.0.0.1:{settings.sip_port}")
        await asyncio.wait_for(call.ended.wait(), 5)
        assert "403" in call.end_reason
    finally:
        await rig.stop()


async def test_direct_address_only_from_linked_integrations(settings, tmp_path):
    rig = await Rig().start(settings, tmp_path)
    try:
        secret = rig.store.config.inbound_secret
        call = await rig.phone().dial(f"{secret}@127.0.0.1:{settings.sip_port}")
        await asyncio.wait_for(call.ended.wait(), 5)
        assert "403" in call.end_reason                     # no wa2sip/cam2sip on 127.0.0.1
        wrong = await rig.phone().dial(f"in-guessed@127.0.0.1:{settings.sip_port}")
        await asyncio.wait_for(wrong.ended.wait(), 5)
        assert "404" in wrong.end_reason
    finally:
        await rig.stop()


async def test_pbx_numbers_are_dialed_as_entered(settings, tmp_path):
    rig = await Rig().start(settings, tmp_path)
    try:
        got = []

        async def on_incoming(call):
            got.append(call.invite.uri)
            call.reject(486, "Busy Here")
        rig.ua.on_incoming = on_incoming
        rig.phone("*43")                      # the "PBX" side takes calls for *43
        c = Contact(kind="pbx", name="Echo", number="*43")
        call = await rig.engine.calls._dial(c)
        await asyncio.wait_for(call.ended.wait(), 5)
        assert got and got[0].startswith("sip:*43@")
    finally:
        await rig.stop()


async def test_a_pause_mid_sentence_does_not_cut_it_off(settings, tmp_path):
    rig = await Rig().start(settings, tmp_path)
    try:
        call = await rig.phone().dial(f"{rig.store.config.pbx.contact_user}@127.0.0.1:{settings.sip_port}")
        await asyncio.wait_for(call.answered.wait(), 5)
        audio = SipAudio(call, on_audio=lambda b: None)
        audio.start()
        await asyncio.sleep(2.0)                             # greeting + start blip
        speech = speech_16k()
        part = speech[: len(speech) // 2]
        for chunk in (part, bytes(64000), speech):          # speech, a 2 s pause, more speech
            for i in range(0, len(chunk), 640):
                audio.send(chunk[i:i + 640])
                await asyncio.sleep(0.02)
        for _ in range(200):
            if rig.agent.asked:
                break
            await asyncio.sleep(0.05)
        assert len(rig.agent.asked) == 1                     # one request, not two halves
        heard = rig.engine.speech.heard
        assert len(heard) >= 2 and heard[-1] > heard[0]      # re-transcribed with the continuation
        await call.hangup()
    finally:
        await rig.stop()


async def test_incoming_calls_rejected_in_quiet_hours(settings, tmp_path):
    rig = await Rig().start(settings, tmp_path)
    try:
        q = rig.store.config.quiet
        q.enabled, q.start, q.end, q.timezone = True, "00:00", "23:59", "UTC"

        async def away():
            return False, "no motion at the desk"
        rig.engine.quiet.at_desk = away
        call = await rig.phone().dial(f"{rig.store.config.pbx.contact_user}@127.0.0.1:{settings.sip_port}")
        await asyncio.wait_for(call.ended.wait(), 5)
        assert "603" in call.end_reason and not rig.agent.asked

        async def here():
            return True, "binary_sensor.desk is on"
        rig.engine.quiet.at_desk = here
        call = await rig.phone().dial(f"{rig.store.config.pbx.contact_user}@127.0.0.1:{settings.sip_port}")
        await asyncio.wait_for(call.answered.wait(), 5)       # at the desk: the call goes through
        await call.hangup()
    finally:
        await rig.stop()
