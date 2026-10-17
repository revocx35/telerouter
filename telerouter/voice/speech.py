"""Wyoming clients: Whisper (speech-to-text), Piper (text-to-speech), openWakeWord (wake words)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator, Callable

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient
from wyoming.error import Error
from wyoming.info import Describe, Info
from wyoming.tts import Synthesize, SynthesizeVoice
from wyoming.wake import Detect, Detection

from ..media.pcm import FRAME_BYTES, RATE, StreamResampler

log = logging.getLogger("telerouter.speech")

STT_TIMEOUT = 60.0
TTS_TIMEOUT = 60.0


class SpeechError(Exception):
    pass


async def _client(uri: str, timeout: float) -> AsyncClient:
    client = AsyncClient.from_uri(uri, connect_timeout=5.0, read_timeout=timeout)
    await client.connect()
    return client


async def describe(uri: str) -> Info | None:
    try:
        client = await _client(uri, 5.0)
    except OSError:
        return None
    try:
        await client.write_event(Describe().event())
        while True:
            ev = await client.read_event()
            if ev is None:
                return None
            if Info.is_type(ev.type):
                return Info.from_event(ev)
    except (OSError, asyncio.TimeoutError):
        return None
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


class SpeechServices:
    """Stateless calls to Whisper and Piper; wake streams are per client (`WakeStream`)."""

    def __init__(self, whisper_uri: str, piper_uri: str, wake_uri: str):
        self.whisper_uri = whisper_uri
        self.piper_uri = piper_uri
        self.wake_uri = wake_uri
        self._stt_slots = asyncio.Semaphore(2)

    async def transcribe(self, pcm: bytes, language: str = "", names: list[str] | None = None) -> str:
        """16 kHz PCM16 -> text ("" when nothing was understood)."""
        async with self._stt_slots:
            try:
                client = await _client(self.whisper_uri, STT_TIMEOUT)
            except OSError as e:
                raise SpeechError(f"speech-to-text service unreachable: {e}") from e
            try:
                await client.write_event(Transcribe(language=language or None,
                                                    transcript_names=names or None).event())
                await client.write_event(AudioStart(rate=RATE, width=2, channels=1).event())
                step = FRAME_BYTES * 50                 # 1 s per chunk
                for i in range(0, len(pcm), step):
                    await client.write_event(AudioChunk(rate=RATE, width=2, channels=1, audio=pcm[i:i + step]).event())
                await client.write_event(AudioStop().event())
                while True:
                    ev = await client.read_event()
                    if ev is None:
                        raise SpeechError("speech-to-text closed the connection")
                    if Transcript.is_type(ev.type):
                        return clean_transcript(Transcript.from_event(ev).text)
                    if Error.is_type(ev.type):
                        raise SpeechError(Error.from_event(ev).text)
            except asyncio.TimeoutError as e:
                raise SpeechError("speech-to-text timed out") from e
            finally:
                with contextlib.suppress(Exception):
                    await client.disconnect()

    async def synthesize(self, text: str, voice: str = "", rate: int = RATE) -> AsyncIterator[bytes]:
        """Text -> PCM16 chunks at `rate` (streamed as Piper produces them)."""
        try:
            client = await _client(self.piper_uri, TTS_TIMEOUT)
        except OSError as e:
            raise SpeechError(f"text-to-speech service unreachable: {e}") from e
        try:
            await client.write_event(Synthesize(text=text, voice=SynthesizeVoice(name=voice) if voice else None).event())
            rs: StreamResampler | None = None
            while True:
                ev = await client.read_event()
                if ev is None:
                    raise SpeechError("text-to-speech closed the connection")
                if AudioStart.is_type(ev.type):
                    st = AudioStart.from_event(ev)
                    if st.width != 2 or st.channels != 1:
                        raise SpeechError("unexpected TTS audio format")
                    rs = StreamResampler(st.rate, rate)
                elif AudioChunk.is_type(ev.type):
                    ch = AudioChunk.from_event(ev)
                    if rs is None:
                        rs = StreamResampler(ch.rate, rate)
                    out = rs(ch.audio)
                    if out:
                        yield out
                elif AudioStop.is_type(ev.type):
                    return
                elif Error.is_type(ev.type):
                    raise SpeechError(Error.from_event(ev).text)
        except asyncio.TimeoutError as e:
            raise SpeechError("text-to-speech timed out") from e
        finally:
            with contextlib.suppress(Exception):
                await client.disconnect()

    async def synthesize_all(self, text: str, voice: str = "", rate: int = RATE) -> bytes:
        out = bytearray()
        async for chunk in self.synthesize(text, voice, rate):
            out += chunk
        return bytes(out)

    async def wake_models(self) -> list[dict]:
        info = await describe(self.wake_uri)
        if not info:
            return []
        return [{"name": m.name, "phrase": m.phrase or m.name} for p in info.wake for m in p.models]

    async def voices(self) -> list[dict]:
        info = await describe(self.piper_uri)
        if not info:
            return []
        out = []
        for p in info.tts:
            for v in p.voices:
                out.append({"name": v.name, "languages": v.languages, "installed": v.installed,
                            "description": v.description or v.name})
        return out

    async def health(self) -> dict:
        res = await asyncio.gather(describe(self.whisper_uri), describe(self.piper_uri), describe(self.wake_uri))
        return {"stt": res[0] is not None, "tts": res[1] is not None, "wake": res[2] is not None}


# Whisper sometimes "hears" these in silence or noise
_HALLUCINATIONS = {"", "you", "thank you.", "thanks for watching!", "thank you for watching.", ".", "bye.",
                   "[blank_audio]", "(silence)", "[music]", "subtitles by the amara.org community"}


def clean_transcript(text: str) -> str:
    t = " ".join((text or "").split())
    if t.lower() in _HALLUCINATIONS:
        return ""
    return re.sub(r"\[[^\]]*\]|\([^)]*\)", "", t).strip()


class WakeStream:
    """One client's microphone -> openWakeWord, kept connected; reconnects on its own."""

    def __init__(self, uri: str, names: list[str], on_detect: Callable[[str], None], label: str = ""):
        self.uri = uri
        self.names = list(names)
        self.on_detect = on_detect
        self.label = label
        self.connected = False
        self._client: AsyncClient | None = None
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=200)
        self._task: asyncio.Task | None = None
        self._reset = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None

    def feed(self, pcm: bytes) -> None:
        try:
            self._queue.put_nowait(pcm)
        except asyncio.QueueFull:          # service slow or down: drop audio, never block
            pass

    def reset(self) -> None:
        """Forget audio heard so far (new AudioStart), e.g. after a conversation ends."""
        self._reset = True
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)

    def set_names(self, names: list[str]) -> None:
        if names != self.names:
            self.names = list(names)
            self._reset = True
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(None)

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            try:
                client = await _client(self.uri, None)
            except OSError as e:
                if self.connected or backoff == 1.0:
                    log.warning("wake word service unreachable (%s): %s", self.label, e)
                self.connected = False
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            backoff = 1.0
            self.connected = True
            reader = asyncio.create_task(self._read(client))
            try:
                await self._session(client, reader)
            except (OSError, ConnectionError) as e:
                log.info("wake word stream (%s) dropped: %s", self.label, e)
            finally:
                reader.cancel()
                self.connected = False
                with contextlib.suppress(Exception):
                    await client.disconnect()
            await asyncio.sleep(1.0)

    async def _session(self, client: AsyncClient, reader: asyncio.Task) -> None:
        while True:
            self._reset = False
            await client.write_event(Detect(names=self.names).event())
            await client.write_event(AudioStart(rate=RATE, width=2, channels=1).event())
            while not self._reset:
                get = asyncio.create_task(self._queue.get())
                done, _ = await asyncio.wait({get, reader}, return_when=asyncio.FIRST_COMPLETED)
                if reader in done:
                    get.cancel()
                    raise ConnectionError("wake word service closed the connection")
                pcm = get.result()
                if pcm is None:
                    continue
                await client.write_event(AudioChunk(rate=RATE, width=2, channels=1, audio=pcm).event())
            await client.write_event(AudioStop().event())
            while not self._queue.empty():       # audio from before the reset doesn't count
                self._queue.get_nowait()

    async def _read(self, client: AsyncClient) -> None:
        while True:
            ev = await client.read_event()
            if ev is None:
                return
            if Detection.is_type(ev.type):
                name = Detection.from_event(ev).name or "?"
                try:
                    self.on_detect(name)
                except Exception:
                    log.exception("wake handler failed")
