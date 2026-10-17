"""Calls: SIP legs to the PBX, wa2sip and cam2sip, bridged to a speaker, an agent, or another call.

Modes of a CallSession:
  speaker   a contact <-> a speaker client (live conversation)        "call my dad"
  agent     an inbound caller talks to an agent (listen/answer loop)   "call Jarvis from my phone"
  mcp       an agent leads the call itself via MCP (say/listen)        phone_message, start_voice_call
  bridge    two calls joined (the caller on the phone asked the agent to connect them)
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import re
import secrets
import time
from typing import TYPE_CHECKING

from ..media import pcm as pcmmod
from ..models import Contact
from ..sip.message import SipUri
from ..sip.ua import Account, AccountConfig, Call
from ..voice.vad import MAX_CONTINUATIONS, EndpointConfig, Endpointer, continuation, looks_unfinished
from .sipaudio import SipAudio

if TYPE_CHECKING:
    from ..engine import Engine
    from ..voice.client_conn import ClientConn

log = logging.getLogger("telerouter.calls")


class CallError(Exception):
    pass


def digits(s: str) -> str:
    return re.sub(r"[^0-9]", "", s or "")


def end_reason_text(reason: str, label: str) -> str:
    r = reason.lower()
    if "486" in r or "busy" in r or "600" in r:
        return f"{label} is busy."
    if "404" in r or "not found" in r or "604" in r:
        return f"I could not find {label}."
    if "480" in r or "unavailable" in r or "408" in r or "timeout" in r or "no response" in r:
        return f"{label} is not reachable right now."
    if "603" in r or "decline" in r or "403" in r:
        return f"{label} declined the call."
    if "487" in r or "cancel" in r:
        return f"{label} didn't answer."
    return f"The call to {label} didn't go through."


class CallSession:
    def __init__(self, mgr: "CallManager", direction: str, contact: Contact | None, label: str, mode: str):
        self.mgr = mgr
        self.id = secrets.token_hex(4)
        self.direction = direction
        self.contact = contact
        self.label = label
        self.mode = mode
        self.call: Call | None = None
        self.audio: SipAudio | None = None
        self.client_id = ""
        self.agent_id = ""
        self.token_id = ""                 # MCP token that started it
        self.state = "dialing"
        self.result = ""
        self.started = time.time()
        self.connected_at: float | None = None
        self.task: asyncio.Task | None = None
        self.ended = asyncio.Event()
        self.hangup_requested = asyncio.Event()
        self.endpointer: Endpointer | None = None
        self.ep_done = asyncio.Event()
        self.digits: asyncio.Queue[str] = asyncio.Queue()
        self.peer: CallSession | None = None    # bridge partner
        self.transcript: list[dict] = []

    def on_far_audio(self, pcm16: bytes) -> None:
        ep = self.endpointer
        if ep is not None and not ep.result:
            if ep.feed(pcm16):
                self.ep_done.set()

    def on_dtmf(self, d: str) -> None:
        self.digits.put_nowait(d)

    def info(self) -> dict:
        return {"id": self.id, "direction": self.direction, "mode": self.mode, "label": self.label,
                "contact_id": self.contact.id if self.contact else "", "state": self.state,
                "result": self.result, "client_id": self.client_id, "agent_id": self.agent_id,
                "started": self.started, "connected_at": self.connected_at,
                "codec": self.call.codec if self.call else None}


class CallManager:
    def __init__(self, engine: "Engine"):
        self.engine = engine
        self.sessions: dict[str, CallSession] = {}
        self.pin_fails: dict[str, list[float]] = {}

    # -- helpers -------------------------------------------------------------------------------------
    @property
    def cfg(self):
        return self.engine.store.config.calls

    def get(self, sid: str) -> CallSession | None:
        return self.sessions.get(sid)

    def active(self) -> list[CallSession]:
        return list(self.sessions.values())

    def _spawn(self, s: CallSession, coro) -> None:
        self.sessions[s.id] = s

        async def run():
            try:
                await coro
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("call %s failed", s.id)
            finally:
                await self._cleanup(s)
        s.task = asyncio.create_task(run())

    async def _cleanup(self, s: CallSession) -> None:
        if s.audio:
            s.audio.stop()
        if s.call and s.call.state != "ended":
            with contextlib.suppress(Exception):
                await s.call.hangup("telerouter hung up")
        s.state = "ended"
        s.ended.set()
        self.sessions.pop(s.id, None)
        dur = int(time.time() - s.connected_at) if s.connected_at else 0
        self.engine.store.add_history({
            "type": "call", "id": s.id, "direction": s.direction, "mode": s.mode, "label": s.label,
            "result": s.result or "ended", "duration": dur, "started": s.started,
            "client": s.client_id, "agent": s.agent_id})
        log.info("call %s (%s, %s) ended: %s", s.id, s.mode, s.label, s.result or "ended")

    def _endpoint_cfg(self, start_timeout: float | None = None) -> EndpointConfig:
        v = self.engine.store.config.voice
        return EndpointConfig(start_timeout_ms=int((start_timeout or v.listen_timeout) * 1000),
                              end_silence_ms=v.phone_end_silence_ms, max_ms=int(v.max_utterance * 1000),
                              threshold=v.vad_threshold)

    async def _tts(self, text: str) -> bytes:
        return await self.engine.speech.synthesize_all(text, self.engine.store.config.voice.tts_voice)

    async def _say(self, s: CallSession, text: str) -> None:
        if not s.audio or not s.call or s.call.state != "active" or not text.strip():
            return
        s.transcript.append({"who": "telerouter", "text": text})
        s.audio.play(await self._tts(text))
        await s.audio.drained()

    async def _listen(self, s: CallSession, timeout: float, cue: bool = True) -> str:
        """Beep, record one utterance from the far end, transcribe it ("" if nothing)."""
        assert s.audio and s.call
        vol = self.engine.store.config.voice.beep_volume
        if cue:
            s.audio.play(pcmmod.cue("start", vol))
            await s.audio.drained()
        cfg = self._endpoint_cfg(timeout)

        async def record(ep: Endpointer) -> Endpointer:
            s.ep_done.clear()
            s.endpointer = ep
            done = asyncio.create_task(s.ep_done.wait())
            gone = asyncio.create_task(s.call.ended.wait())
            try:
                await asyncio.wait({done, gone}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                done.cancel()
                gone.cancel()
            return ep

        ep = await record(Endpointer(cfg))
        if s.call.state != "active" or ep.result == "timeout" or not ep.started:
            s.endpointer = None
            return ""
        audio = bytes(ep.audio)
        lang, names = self.engine.store.config.voice.language, self.engine.contact_names()
        for rounds in range(MAX_CONTINUATIONS + 1):
            more = continuation(cfg)
            waiter = asyncio.create_task(record(more))       # keep recording while Whisper works
            text = await self.engine.speech.transcribe(audio, lang, names)
            if rounds == MAX_CONTINUATIONS or not (looks_unfinished(text) or more.started) or s.call.state != "active":
                waiter.cancel()
                break
            await waiter
            if not more.started:
                break
            log.info("[call %s] the sentence went on after a pause", s.id)
            audio += bytes(more.audio)
        s.endpointer = None
        if cue and s.call.state == "active":
            s.audio.play(pcmmod.cue("end", vol))
        if text:
            s.transcript.append({"who": s.label, "text": text})
        log.info("[call %s] heard from %s: %s", s.id, s.label, text or "(nothing)")
        return text

    # -- dialing ------------------------------------------------------------------------------------
    async def _dial(self, contact: Contact) -> Call:
        e = self.engine
        name = "Telerouter"
        if contact.kind == "pbx":
            acc = e.ua.accounts.get("pbx")
            if not acc:
                raise CallError("no PBX account is configured")
            if acc.state != "registered":
                raise CallError("the PBX account is not registered")
            # dial as entered: feature codes (*43) and # are part of the number
            return await acc.dial(contact.number, display_name=e.store.config.pbx.display_name or name)
        client = e.integrations.client(contact.source)
        if not client:
            raise CallError(f"the {contact.kind} connection for {contact.label} is not set up")
        if contact.source_ref:
            uri = await client.prepare_call(target=contact.source_ref)
        else:
            uri = await client.prepare_call(number=digits(contact.number))
        return await self.dial_direct(uri, name)

    async def dial_direct(self, uri: str, display: str = "Telerouter") -> Call:
        u = SipUri.parse(uri)
        host, port = u.host.strip("[]"), u.port or 5060
        acc = Account(self.engine.ua, AccountConfig(
            id=f"direct-{host}", server=host, port=port, username="telerouter", password="",
            display_name=display, contact_user="tr-" + secrets.token_hex(6)))
        return await acc.dial(uri)

    async def _wait_answer(self, s: CallSession, timeout: float) -> bool:
        call = s.call
        assert call
        ans = asyncio.create_task(call.answered.wait())
        end = asyncio.create_task(call.ended.wait())
        hup = asyncio.create_task(s.hangup_requested.wait())
        try:
            await asyncio.wait({ans, end, hup}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (ans, end, hup):
                t.cancel()
        if call.state == "active" and not s.hangup_requested.is_set():
            return True
        if call.state != "ended":
            await call.hangup("no answer" if not s.hangup_requested.is_set() else "cancelled")
            s.result = "no answer" if not s.hangup_requested.is_set() else "cancelled"
        else:
            s.result = call.end_reason
        return False

    async def _wait_end(self, s: CallSession, *extra: asyncio.Event) -> None:
        assert s.call
        waits = [asyncio.create_task(s.call.ended.wait()), asyncio.create_task(s.hangup_requested.wait())]
        waits += [asyncio.create_task(ev.wait()) for ev in extra]
        try:
            await asyncio.wait(waits, timeout=self.cfg.max_call_minutes * 60, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in waits:
                t.cancel()

    # -- speaker calls ------------------------------------------------------------------------------------
    def call_to_speaker(self, contact: Contact, conn: "ClientConn") -> CallSession:
        if conn.state in ("call", "ringing"):
            raise CallError(f"{conn.name} is already in a call")
        s = CallSession(self, "out", contact, contact.label, "speaker")
        s.client_id = conn.id
        conn.set_state("call")
        conn.call_session = s          # type: ignore[attr-defined]
        self._spawn(s, self._run_speaker_out(s, conn))
        return s

    async def _run_speaker_out(self, s: CallSession, conn: "ClientConn") -> None:
        e = self.engine
        try:
            if self.cfg.announce_calls:
                with contextlib.suppress(Exception):
                    conn.play(await self._tts(f"Calling {s.label}."))
            try:
                s.call = await self._dial(s.contact)          # type: ignore[arg-type]
            except Exception as ex:
                s.result = f"failed: {ex}"
                log.warning("call to %s failed: %s", s.label, ex)
                await conn.drained()
                conn.play(await self._tts(f"I couldn't call {s.label}."))
                return
            s.state = "ringing"
            await conn.drained()
            ring = asyncio.create_task(self._ringback(conn))
            try:
                answered = await self._wait_answer(s, self.cfg.ring_timeout)
            finally:
                ring.cancel()
                conn.flush()
            if not answered:
                if not s.hangup_requested.is_set():
                    conn.play(await self._tts(end_reason_text(s.result, s.label)))
                return
            await self._bridge_speaker(s, conn)
        finally:
            await self._release_speaker(s, conn)

    async def _bridge_speaker(self, s: CallSession, conn: "ClientConn") -> None:
        assert s.call
        s.state = "active"
        s.connected_at = time.time()
        s.result = "connected"
        s.audio = SipAudio(s.call, on_audio=conn.play, on_dtmf=s.on_dtmf)
        s.audio.start()
        log.info("speaker %s <-> %s connected (%s)", conn.name, s.label, s.call.codec)
        await self._wait_end(s, conn.closed)
        if s.call.state == "ended" and not s.hangup_requested.is_set():
            s.result = "far end hung up"

    async def _release_speaker(self, s: CallSession, conn: "ClientConn") -> None:
        if getattr(conn, "call_session", None) is s:
            conn.call_session = None   # type: ignore[attr-defined]
        if s.audio:
            s.audio.stop()
        if s.call and s.call.state != "ended":
            await s.call.hangup("hung up")
        if not conn.closed.is_set():
            conn.flush()
            conn.play(pcmmod.cue("end", self.engine.store.config.voice.beep_volume))
            await conn.drained()
            if conn.state in ("call", "ringing"):
                conn.set_state("idle")
            self.engine.reset_wake(conn)

    async def _ringback(self, conn: "ClientConn") -> None:
        tone = pcmmod.cue("ringback", self.engine.store.config.voice.beep_volume)
        while True:
            conn.play(tone)
            await conn.drained()

    def speaker_mic(self, conn: "ClientConn", pcm16: bytes) -> None:
        s: CallSession | None = getattr(conn, "call_session", None)
        if s and s.audio and s.state == "active":
            s.audio.send(pcm16)

    # -- inbound --------------------------------------------------------------------------------------------
    async def on_incoming(self, call: Call) -> None:
        e = self.engine
        acc_id = call.account.cfg.id
        src = call.peer_addr[0] if call.peer_addr else ""
        if acc_id == "pbx":
            if not await e.is_pbx_address(src):
                log.warning("refused INVITE for the PBX extension from %s (not the PBX)", src)
                call.reject(403, "Forbidden")
                return
            contact = e.find_contact_by_number(call.remote_user, prefer="pbx")
        elif acc_id == "inbound":
            integ = await e.integration_for_address(src)
            if not integ:
                log.warning("refused direct INVITE from %s (not a linked integration)", src)
                call.reject(403, "Forbidden")
                return
            ref = (call.invite.get("X-Telerouter-Target") or "").strip() if call.invite else ""
            contact = e.find_integration_contact(integ.id, ref, call.remote_user)
        else:
            call.reject(404, "Not Found")
            return
        label = contact.label if contact else (call.remote_display or call.remote_user or "unknown caller")
        cfg = self.cfg
        if contact:
            policy = contact.inbound if contact.inbound != "default" else cfg.inbound_default
        else:
            policy = cfg.unknown_callers
        log.info("incoming call from %s (%s) -> %s", label, call.remote_user, policy)
        if policy == "reject":
            call.reject(603, "Decline")
            e.store.add_history({"type": "call", "direction": "in", "label": label, "result": "rejected",
                                 "mode": "", "started": time.time(), "duration": 0})
            return
        if policy == "client":
            cid = (contact.inbound_client if contact and contact.inbound_client else cfg.inbound_default_client)
            conn = e.clients.get(cid) if cid else None
            if not conn or conn.state not in ("idle", "speaking"):
                call.reject(480, "Temporarily Unavailable")
                return
            s = CallSession(self, "in", contact, label, "speaker")
            s.call = s.call or call
            s.client_id = conn.id
            conn.call_session = s      # type: ignore[attr-defined]
            conn.flush()
            conn.set_state("ringing")
            call.ring()
            self._spawn(s, self._run_speaker_in(s, conn))
            return
        agent = None
        if contact and contact.inbound_agent:
            agent = e.store.agent(contact.inbound_agent)
        agent = agent or e.store.default_agent()
        if not agent:
            call.reject(480, "Temporarily Unavailable")
            return
        s = CallSession(self, "in", contact, label, "agent")
        s.call = call
        s.agent_id = agent.id
        self._spawn(s, self._run_agent_call(s))

    async def _run_speaker_in(self, s: CallSession, conn: "ClientConn") -> None:
        e = self.engine
        assert s.call
        s.state = "ringing"
        try:
            ring = asyncio.create_task(self._ring_speaker(s, conn))
            ans_ev = asyncio.Event()
            s.answer_event = ans_ev          # type: ignore[attr-defined]
            ended = asyncio.create_task(s.call.ended.wait())
            ans = asyncio.create_task(ans_ev.wait())
            hup = asyncio.create_task(s.hangup_requested.wait())
            try:
                await asyncio.wait({ended, ans, hup, asyncio.create_task(conn.closed.wait())},
                                   timeout=self.cfg.ring_timeout, return_when=asyncio.FIRST_COMPLETED)
            finally:
                ring.cancel()
                for t in (ended, ans, hup):
                    t.cancel()
                conn.flush()
            if not ans_ev.is_set() or s.call.state != "ringing":
                s.result = "missed" if s.call.state == "ringing" else "caller hung up"
                if s.call.state == "ringing":
                    s.call.reject(480, "Temporarily Unavailable")
                return
            conn.set_state("call")
            await s.call.answer()
            await asyncio.sleep(0.2)
            await self._bridge_speaker(s, conn)
        finally:
            await self._release_speaker(s, conn)

    async def _ring_speaker(self, s: CallSession, conn: "ClientConn") -> None:
        e = self.engine
        phrase = e.wake_phrase(conn)
        with contextlib.suppress(Exception):
            conn.play(await self._tts(f"Incoming call from {s.label}. Say {phrase} to answer."))
        tone = pcmmod.cue("ring", max(30, e.store.config.voice.beep_volume))
        while True:
            conn.play(tone)
            await conn.drained()

    def answer_on_speaker(self, conn: "ClientConn") -> bool:
        s: CallSession | None = getattr(conn, "call_session", None)
        ev = getattr(s, "answer_event", None) if s else None
        if s and ev and s.state == "ringing":
            ev.set()
            return True
        return False

    # -- an inbound caller talks to the agent -------------------------------------------------------------------
    async def _run_agent_call(self, s: CallSession) -> None:
        e = self.engine
        call = s.call
        assert call
        agent = e.store.agent(s.agent_id)
        await call.answer()
        s.state = "active"
        s.connected_at = time.time()
        s.result = "connected"
        s.audio = SipAudio(call, on_audio=s.on_far_audio, on_dtmf=s.on_dtmf)
        s.audio.start()
        await asyncio.sleep(0.3)
        if self.cfg.agent_call_pin and not await self._check_pin(s):
            s.result = "wrong PIN"
            await self._say(s, "Goodbye.")
            return
        rt = e.agents.get(s.agent_id)
        name = agent.name if agent else "your assistant"
        await self._say(s, self.cfg.agent_greeting.replace("{agent}", name))
        misses = 0
        while call.state == "active" and s.mode == "agent" and not s.hangup_requested.is_set():
            text = await self._listen(s, 8.0)
            if call.state != "active" or s.mode != "agent":
                break
            if not text:
                misses += 1
                if misses >= 2:
                    await self._say(s, "Goodbye.")
                    return
                await self._say(s, "Are you still there?")
                continue
            misses = 0
            if re.fullmatch(r"(good ?bye|bye|bye bye|hang up|that'?s all|thanks?,? bye)[.!]?", text.lower()):
                await self._say(s, "Goodbye.")
                return
            if not rt and getattr(s, "route", None) is None:
                await self._say(s, "The assistant is not available right now.")
                return
            from ..agents.machine import is_exit
            from ..agents.runtime import AgentError, Source
            route = getattr(s, "route", None)
            if route is not None and is_exit(text, name):
                s.route = None                         # type: ignore[attr-defined]
                await self._say(s, f"Back to {name}.")
                continue
            ticker = asyncio.create_task(self._think_ticks(s))
            try:
                if route is not None:
                    reply = await e.ask_route(route, text, f"phone call from {s.label}")
                else:
                    reply = await rt.ask(text, Source("phone", f"phone call from {s.label}", call_id=s.id))
            except AgentError as ex:
                reply = f"Sorry, {ex}."
            finally:
                ticker.cancel()
            if s.mode != "agent":          # the agent connected this caller to someone: the bridge owns the call
                break
            s.transcript.append({"who": name, "text": reply})
            await self._say(s, e.speakable(reply) or "Done.")
        if s.mode == "bridge":
            await s.ended_bridge.wait()     # type: ignore[attr-defined]

    async def _think_ticks(self, s: CallSession) -> None:
        if not self.engine.store.config.voice.thinking_cue:
            return
        tick = pcmmod.cue("think", self.engine.store.config.voice.beep_volume)
        while True:
            await asyncio.sleep(2.5)
            if s.audio:
                s.audio.play(tick)

    async def _check_pin(self, s: CallSession) -> bool:
        key = digits(s.call.remote_user if s.call else "") or "?"
        fails = [t for t in self.pin_fails.get(key, []) if t > time.time() - 3600]
        if len(fails) >= 5:
            log.warning("agent PIN locked for caller %s", key)
            return False
        for _ in range(3):
            await self._say(s, "Please enter your PIN, then press the hash key.")
            entered = ""
            while True:
                try:
                    d = await asyncio.wait_for(s.digits.get(), 10)
                except TimeoutError:
                    break
                if d == "#":
                    break
                if d.isdigit() and len(entered) < 16:
                    entered += d
            if entered and hmac.compare_digest(entered.encode(), self.cfg.agent_call_pin.encode()):
                self.pin_fails.pop(key, None)
                return True
            fails.append(time.time())
            self.pin_fails[key] = fails
            log.warning("wrong agent PIN from %s", key)
            if len(fails) >= 5:
                return False
            await self._say(s, "Wrong PIN.")
        return False

    # -- MCP-led calls --------------------------------------------------------------------------------------------
    async def start_voice_call(self, contact: Contact, token_id: str, agent_id: str = "") -> CallSession:
        s = CallSession(self, "out", contact, contact.label, "mcp")
        s.token_id = token_id
        s.agent_id = agent_id
        self.sessions[s.id] = s
        try:
            s.call = await self._dial(contact)
        except Exception as ex:
            self.sessions.pop(s.id, None)
            raise CallError(str(ex)) from ex
        s.state = "ringing"
        answered = await self._wait_answer(s, self.cfg.ring_timeout)
        if not answered:
            self.sessions.pop(s.id, None)
            await self._cleanup(s)
            raise CallError(end_reason_text(s.result, contact.label).rstrip("."))
        s.state = "active"
        s.connected_at = time.time()
        s.result = "connected"
        s.audio = SipAudio(s.call, on_audio=s.on_far_audio, on_dtmf=s.on_dtmf)
        s.audio.start()
        self._spawn(s, self._watch_mcp(s))
        await asyncio.sleep(0.5)
        return s

    async def _watch_mcp(self, s: CallSession) -> None:
        await self._wait_end(s)
        if s.call and s.call.state == "ended" and not s.hangup_requested.is_set():
            s.result = "far end hung up"

    async def say(self, s: CallSession, text: str, listen: bool, timeout: float) -> dict:
        if s.state != "active" or not s.call or s.call.state != "active":
            raise CallError("the call has ended")
        await self._say(s, text)
        heard = await self._listen(s, timeout) if listen else ""
        return {"heard": heard, "call_active": s.call.state == "active"}

    async def listen(self, s: CallSession, timeout: float) -> dict:
        if s.state != "active" or not s.call or s.call.state != "active":
            raise CallError("the call has ended")
        heard = await self._listen(s, timeout)
        return {"heard": heard, "call_active": s.call.state == "active"}

    async def hangup(self, s: CallSession, reason: str = "hung up") -> None:
        s.result = s.result if s.result and s.result != "connected" else reason
        s.hangup_requested.set()
        if s.peer:
            s.peer.hangup_requested.set()
        if s.call and s.call.state == "ringing" and s.direction == "in":
            s.call.reject(603, "Decline")
        if s.mode == "mcp" and s.task is None:
            await self._cleanup(s)

    async def phone_message(self, contact: Contact, message: str, wait_reply: bool, repeat: int,
                            token_id: str, agent_id: str, intro: str) -> dict:
        s = await self.start_voice_call(contact, token_id, agent_id)
        reply = ""
        try:
            for i in range(max(1, min(repeat, 3))):
                await self._say(s, (intro + " " if i == 0 and intro else "") + message)
                if s.call and s.call.state != "active":
                    break
                await asyncio.sleep(0.6)
            if wait_reply and s.call and s.call.state == "active":
                await self._say(s, "Please answer after the beep.")
                reply = await self._listen(s, 12.0)
                await self._say(s, "Thank you. Goodbye." if reply else "Goodbye.")
        finally:
            await self.hangup(s, "message delivered")
            if s.task:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(s.ended.wait(), 10)
        return {"answered": True, "reply": reply}

    # -- call-to-call bridge (a phone caller asked the agent to connect them) --------------------------------------
    def bridge_to(self, inbound: CallSession, contact: Contact) -> CallSession:
        if inbound.mode != "agent" or not inbound.call or inbound.call.state != "active":
            raise CallError("that call can't be connected")
        inbound.mode = "bridge"
        inbound.ended_bridge = asyncio.Event()     # type: ignore[attr-defined]
        if inbound.endpointer:
            inbound.endpointer.result = "timeout"
            inbound.ep_done.set()
        out = CallSession(self, "out", contact, contact.label, "bridge")
        out.peer, inbound.peer = inbound, out
        self._spawn(out, self._run_bridge(inbound, out))
        return out

    async def _run_bridge(self, a: CallSession, b: CallSession) -> None:
        assert a.audio and a.call
        try:
            a.audio.flush()
            await self._say(a, f"Connecting you to {b.label}.")
            try:
                b.call = await self._dial(b.contact)        # type: ignore[arg-type]
            except Exception as ex:
                b.result = f"failed: {ex}"
                await self._say(a, f"I couldn't call {b.label}. Goodbye.")
                return
            b.state = "ringing"
            a.audio.tone(pcmmod.cue("ringback", 60))
            answered = await self._wait_answer(b, self.cfg.ring_timeout)
            a.audio.tone(None)
            if not answered:
                await self._say(a, end_reason_text(b.result, b.label) + " Goodbye.")
                return
            b.state = "active"
            b.connected_at = time.time()
            b.result = "connected"
            b.audio = SipAudio(b.call)
            b.audio.start()
            a.audio.on_audio = None
            a.audio.on_raw = b.audio.send_g711
            b.audio.on_raw = a.audio.send_g711
            log.info("bridged %s <-> %s", a.label, b.label)
            await self._wait_end(b, a.call.ended, a.hangup_requested)
        finally:
            ev = getattr(a, "ended_bridge", None)
            if ev:
                ev.set()
            if a.call and a.call.state != "ended":
                await a.call.hangup("bridge ended")

    def info(self) -> list[dict]:
        return [s.info() for s in self.sessions.values()]
