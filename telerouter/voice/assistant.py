"""The speaker conversation: wake word -> start blip -> listen -> end blip -> Whisper -> agent -> Piper.

Microphone audio of every connected speaker flows through `mic()`:
  idle / speaking*   -> openWakeWord (wake word starts a turn; *barge-in only with client echo cancelling)
  listening          -> endpointer (Silero VAD), then the utterance goes to Whisper
  ringing            -> openWakeWord (the wake word answers the call)
  call               -> the SIP call, plus openWakeWord (the wake word hangs up)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from typing import TYPE_CHECKING

from ..agents.runtime import AgentError, Source
from ..media import pcm as pcmmod
from .client_conn import ClientConn
from .speech import SpeechError, WakeStream
from .vad import MAX_CONTINUATIONS, EndpointConfig, Endpointer, continuation, looks_unfinished

if TYPE_CHECKING:
    from ..engine import Engine

log = logging.getLogger("telerouter.voice")


def speakable(text: str, limit: int = 900) -> str:
    """Agent output -> something a TTS voice can read (no markdown, code or URLs)."""
    t = text or ""
    t = re.sub(r"```.*?```", " (code omitted) ", t, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", "a link", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*\d+[.)]\s+", "", t, flags=re.M)
    t = re.sub(r"[*_~|>#]+", " ", t)
    t = re.sub(r"[\U0001F000-\U0001FFFF☀-➿]", "", t)
    t = " ".join(t.split())
    if len(t) > limit:
        cut = t[:limit]
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        t = (cut[: end + 1] if end > limit // 2 else cut) + " That's the short version."
    return t.strip()


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text)
    out, buf = [], ""
    for p in parts:
        buf = f"{buf} {p}".strip()
        if len(buf) > 40:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


class VoiceAssistant:
    def __init__(self, engine: "Engine"):
        self.engine = engine
        self.wake: dict[str, WakeStream] = {}
        self._listeners: dict[str, Endpointer] = {}
        self._listen_done: dict[str, asyncio.Event] = {}

    # -- client lifecycle --------------------------------------------------------------------------------
    def attach(self, conn: ClientConn) -> None:
        names = self.wake_words(conn)
        ws = WakeStream(self.engine.settings.wake_uri, names, lambda n: self._on_wake(conn, n), conn.name)
        self.wake[conn.id] = ws
        ws.start()
        conn.on_mic = lambda pcm: self.mic(conn, pcm)

    async def detach(self, conn: ClientConn) -> None:
        ws = self.wake.pop(conn.id, None)
        if ws:
            await ws.stop()
        self._listeners.pop(conn.id, None)

    def wake_words(self, conn: ClientConn) -> list[str]:
        return conn.client.wake_words or self.engine.store.config.voice.wake_words

    def refresh_wake_words(self) -> None:
        for cid, ws in self.wake.items():
            conn = self.engine.clients.get(cid)
            if conn:
                ws.set_names(self.wake_words(conn))

    def reset_wake(self, conn: ClientConn) -> None:
        ws = self.wake.get(conn.id)
        if ws:
            ws.reset()

    # -- audio routing ---------------------------------------------------------------------------------------
    def mic(self, conn: ClientConn, pcm: bytes) -> None:
        st = conn.state
        ep = self._listeners.get(conn.id)
        if st == "listening" and ep is not None:
            if ep.feed(pcm):
                ev = self._listen_done.get(conn.id)
                if ev:
                    ev.set()
            return
        if st == "call":
            self.engine.calls.speaker_mic(conn, pcm)
        wake_ok = st in ("idle", "ringing", "call") or (st == "speaking" and conn.aec)
        feed = wake_ok and not (st == "idle" and conn.playing and not conn.aec)
        ws = self.wake.get(conn.id)
        if not ws:
            return
        if feed:
            if not getattr(conn, "wake_fed", False):
                # feeding resumes after a pause: start a fresh detection, or the wake word that began
                # the last turn (still in the model's window) would fire again
                ws.reset()
            ws.feed(pcm)
        conn.wake_fed = feed      # type: ignore[attr-defined]

    def _on_wake(self, conn: ClientConn, name: str) -> None:
        st = conn.state
        log.info("wake word %s on %s (%s)", name, conn.name, st)
        if st == "ringing":
            self.engine.calls.answer_on_speaker(conn)
            return
        if st == "call":
            s = getattr(conn, "call_session", None)
            if s:
                asyncio.create_task(self.engine.calls.hangup(s, "hung up by voice"))
            return
        if st == "speaking" and conn.aec and conn.busy_task:
            conn.busy_task.cancel()
            conn.flush()
        elif st != "idle":
            return
        self.start_turn(conn)

    def start_turn(self, conn: ClientConn) -> None:
        if conn.busy_task and not conn.busy_task.done():
            return
        conn.busy_task = asyncio.create_task(self._turn(conn))

    # -- one conversation turn ------------------------------------------------------------------------------
    async def _turn(self, conn: ClientConn) -> None:
        e = self.engine
        try:
            follow = True
            first = True
            while follow:
                follow = False
                text = await self.listen(conn, None if first else 5.0)
                first = False
                if text is None:
                    return
                if not text:
                    conn.play(pcmmod.cue("error", e.store.config.voice.beep_volume))
                    await conn.drained()
                    return
                reply = await self._ask_agent(conn, text)
                if reply is None or conn.state in ("call", "ringing"):
                    return
                await self.speak(conn, reply)
                follow = e.store.config.voice.follow_up and conn.state == "idle"
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("voice turn on %s failed", conn.name)
        finally:
            self._listeners.pop(conn.id, None)
            if conn.state not in ("call", "ringing"):
                conn.set_state("idle")
            self.reset_wake(conn)

    async def listen(self, conn: ClientConn, start_timeout: float | None = None) -> str | None:
        """Blip, record one utterance, blip, transcribe. None: nothing was said."""
        e = self.engine
        v = e.store.config.voice
        conn.flush()
        conn.set_state("listening")
        conn.play(pcmmod.cue("start", v.beep_volume))
        await conn.drained(0.05)
        cfg = EndpointConfig(start_timeout_ms=int((start_timeout or v.listen_timeout) * 1000),
                             end_silence_ms=v.end_silence_ms, max_ms=int(v.max_utterance * 1000),
                             threshold=v.vad_threshold)

        async def record(ep: Endpointer, wait: float) -> Endpointer:
            ev = asyncio.Event()
            self._listen_done[conn.id] = ev
            self._listeners[conn.id] = ep
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(ev.wait(), wait)
            return ep

        try:
            ep = await record(Endpointer(cfg), v.max_utterance + v.listen_timeout + 5)
            if ep.result == "timeout" or not ep.started:
                conn.set_state("idle")
                return None
            audio = bytes(ep.audio)
            t0 = time.monotonic()
            for rounds in range(MAX_CONTINUATIONS + 1):
                # keep recording while Whisper works: a pause is not always the end of the sentence
                more = continuation(cfg)
                waiter = asyncio.create_task(record(more, v.max_utterance + 5))
                try:
                    text = await e.speech.transcribe(audio, v.language, e.contact_names())
                except SpeechError as ex:
                    waiter.cancel()
                    log.warning("speech-to-text failed: %s", ex)
                    await self.say_error(conn, "Speech recognition is not available.")
                    return ""
                if rounds == MAX_CONTINUATIONS or not (looks_unfinished(text) or more.started):
                    waiter.cancel()
                    break
                more = await waiter
                if not more.started:
                    break
                log.info("[%s] the sentence went on after a pause (%s)", conn.name, text[-40:])
                audio += bytes(more.audio)
        finally:
            self._listeners.pop(conn.id, None)
            self._listen_done.pop(conn.id, None)
        conn.set_state("thinking")
        conn.play(pcmmod.cue("end", v.beep_volume))
        log.info("[%s] heard (%.1f s audio, %.2f s stt): %s", conn.name, len(audio) / 32000,
                 time.monotonic() - t0, text)
        conn.last_text = text
        return text

    async def _ask_agent(self, conn: ClientConn, text: str) -> str | None:
        e = self.engine
        agent = e.store.agent_for_client(conn.client)
        route = getattr(conn, "route", None)
        if route is not None and route.expired():
            conn.route = route = None              # type: ignore[attr-defined]
        if route is not None:
            from ..agents.machine import is_exit
            if is_exit(text, agent.name if agent else ""):
                conn.route = None                  # type: ignore[attr-defined]
                return f"Back to {agent.name if agent else 'your assistant'}."
            conn.set_state("thinking")
            ticker = asyncio.create_task(self._think_ticks(conn))
            try:
                reply = await self._quiet_wait(conn, e.ask_route(route, text, f"{conn.name} speaker"))
            finally:
                ticker.cancel()
            if reply is not None:
                conn.last_reply = reply
            return reply
        rt = e.agents.get(agent.id) if agent else None
        if not agent or not rt:
            await self.say_error(conn, "No assistant is set up yet.")
            return None
        conn.set_state("thinking")
        ticker = asyncio.create_task(self._think_ticks(conn))

        async def note(msg: str) -> None:
            conn.play(await e.speech.synthesize_all(speakable(f"{agent.name} needs you at the terminal: {msg}"),
                                                    e.store.config.voice.tts_voice))
        try:
            src = Source("speaker", f"{conn.name} speaker" + (f" in {conn.client.room}" if conn.client.room else ""),
                         client_id=conn.id)
            reply = await self._quiet_wait(conn, rt.ask(text, src, on_note=note))
        except AgentError as ex:
            log.warning("[%s] agent: %s", conn.name, ex)
            reply = f"Sorry, {ex}."
        finally:
            ticker.cancel()
        if reply is not None:
            conn.last_reply = reply
        return reply

    async def _quiet_wait(self, conn: ClientConn, coro) -> str | None:
        """During quiet hours a long task is not answered on the speaker: stop waiting (and ticking) after
        long_task_minutes and stay silent; the agent keeps working. None = say nothing."""
        q = self.engine.quiet
        if not q.active():
            return await coro
        try:
            return await asyncio.wait_for(coro, q.cfg.long_task_minutes * 60)
        except TimeoutError:
            log.info("[%s] quiet hours: the answer takes longer than %d min - it won't be spoken",
                     conn.name, q.cfg.long_task_minutes)
            return None

    def ticks_on(self, conn: ClientConn) -> bool:
        pref = conn.client.thinking_cue
        return self.engine.store.config.voice.thinking_cue if pref == "default" else pref == "on"

    async def _think_ticks(self, conn: ClientConn) -> None:
        v = self.engine.store.config.voice
        if not self.ticks_on(conn):
            return
        tick = pcmmod.cue("think", v.beep_volume)
        while True:
            await asyncio.sleep(3.0)
            if conn.state == "thinking" and not conn.playing:
                conn.play(tick)

    async def speak(self, conn: ClientConn, text: str) -> None:
        """Say `text` on the speaker (sentence by sentence, so it starts quickly)."""
        e = self.engine
        text = speakable(text)
        if not text:
            return
        conn.set_state("speaking")
        try:
            for part in sentences(text):
                async for chunk in e.speech.synthesize(part, e.store.config.voice.tts_voice):
                    conn.play(chunk)
            await conn.drained(0.1)
        except SpeechError as ex:
            log.warning("text-to-speech failed: %s", ex)
            conn.play(pcmmod.cue("error", e.store.config.voice.beep_volume))
            await conn.drained()
        finally:
            if conn.state == "speaking":
                conn.set_state("idle")

    async def say_error(self, conn: ClientConn, text: str) -> None:
        with contextlib.suppress(Exception):
            await self.speak(conn, text)
            return
        conn.play(pcmmod.cue("error", self.engine.store.config.voice.beep_volume))

    # -- used by MCP tools -------------------------------------------------------------------------------------
    async def announce(self, conn: ClientConn, text: str) -> None:
        if conn.state in ("call", "ringing", "listening"):
            raise RuntimeError(f"{conn.name} is busy ({conn.state})")
        await self.speak(conn, text)

    async def ask_speaker(self, conn: ClientConn, question: str, timeout: float) -> str:
        if conn.state not in ("idle", "thinking", "speaking"):
            raise RuntimeError(f"{conn.name} is busy ({conn.state})")
        await self.speak(conn, question)
        try:
            text = await self.listen(conn, timeout)
        finally:
            if conn.state in ("thinking", "listening"):
                conn.set_state("idle")
        return text or ""
