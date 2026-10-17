"""Media of one SIP call as 16 kHz PCM16: G.711/8 kHz conversion, paced RTP out, DTMF in."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from ..media import g711
from ..media import pcm as pcmmod
from ..media.rtp import RtpPacket
from ..sip.ua import Call

log = logging.getLogger("telerouter.sipaudio")

DTMF_EVENTS = "0123456789*#ABCD"
FRAME = 160                       # 20 ms of G.711
LIVE_MAX = FRAME * 15             # live (bridged) audio: drop beyond 300 ms of backlog


class SipAudio:
    def __init__(self, call: Call, on_audio: Callable[[bytes], None] | None = None,
                 on_dtmf: Callable[[str], None] | None = None):
        self.call = call
        self.on_audio = on_audio          # 16 kHz PCM16 from the far end
        self.on_dtmf = on_dtmf
        self.on_raw: Callable[[bytes, str], None] | None = None   # G.711 as received (call-to-call bridges)
        self.codec = call.codec or "PCMA"
        self._down = pcmmod.StreamResampler(16000, 8000)
        self._up = pcmmod.StreamResampler(8000, 16000)
        self._prompt = bytearray()        # G.711 bytes, plays before live audio
        self._live = bytearray()
        self._tone = b""
        self._tone_pos = 0
        self._drained = asyncio.Event()
        self._drained.set()
        self._task: asyncio.Task | None = None
        self._last_dtmf_ts: int | None = None
        self.silence = g711.SILENCE.get(self.codec, 0xD5)

    def start(self) -> None:
        self.codec = self.call.codec or "PCMA"
        self.silence = g711.SILENCE.get(self.codec, 0xD5)
        if self.call.rtp:
            self.call.rtp.on_packet = self._on_rtp
        self.call.on_dtmf = self._dtmf
        self._task = asyncio.create_task(self._pace())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        if self.call.rtp:
            self.call.rtp.on_packet = None
        self.call.on_dtmf = None
        self._drained.set()

    # -- out ----------------------------------------------------------------------------------------
    def _encode(self, pcm16: bytes) -> bytes:
        return pcmmod.g711_encode(self._down(pcm16), self.codec)

    def send(self, pcm16: bytes) -> None:
        """Live audio (a speaker's microphone, another call)."""
        self._live += self._encode(pcm16)
        if len(self._live) > LIVE_MAX:
            del self._live[: len(self._live) - FRAME * 4]

    def send_g711(self, payload: bytes, codec: str) -> None:
        self._live += payload if codec == self.codec else g711.convert(payload, codec, self.codec)
        if len(self._live) > LIVE_MAX:
            del self._live[: len(self._live) - FRAME * 4]

    def play(self, pcm16: bytes) -> None:
        """A prompt: queued after other prompts, wins over live audio."""
        # whole prompts are resampled in one go (the stream resampler belongs to the live audio)
        self._prompt += pcmmod.g711_encode(pcmmod.resample(pcm16 + pcmmod.silence(40), 16000, 8000), self.codec)
        self._drained.clear()

    def tone(self, pcm16: bytes | None) -> None:
        """Looping tone (ring-back) under prompts; None stops it."""
        self._tone = pcmmod.g711_encode(pcmmod.resample(pcm16, 16000, 8000), self.codec) if pcm16 else b""
        self._tone_pos = 0

    def flush(self) -> None:
        self._prompt.clear()
        self._live.clear()
        self._drained.set()

    async def drained(self) -> None:
        done = asyncio.create_task(self._drained.wait())
        gone = asyncio.create_task(self.call.ended.wait())
        try:
            await asyncio.wait({done, gone}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            done.cancel()
            gone.cancel()

    def _next_frame(self) -> bytes:
        if self._prompt:
            frame = bytes(self._prompt[:FRAME])
            del self._prompt[:FRAME]
            if not self._prompt:
                self._drained.set()
            return frame + bytes([self.silence]) * (FRAME - len(frame))
        if self._tone:
            if self._tone_pos >= len(self._tone):
                self._tone_pos = 0
            frame = self._tone[self._tone_pos:self._tone_pos + FRAME]
            self._tone_pos += FRAME
            return frame + bytes([self.silence]) * (FRAME - len(frame))
        if len(self._live) >= FRAME:
            frame = bytes(self._live[:FRAME])
            del self._live[:FRAME]
            return frame
        return bytes([self.silence]) * FRAME

    async def _pace(self) -> None:
        loop = asyncio.get_running_loop()
        next_t = loop.time()
        while True:
            next_t += 0.02
            delay = next_t - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -0.2:
                next_t = loop.time()
            call = self.call
            frame = self._next_frame()
            if call.state == "active" and call.rtp and call.negotiated:
                call.rtp.send(call.negotiated.remote_pt, frame, FRAME)

    # -- in -----------------------------------------------------------------------------------------
    def _dtmf(self, digit: str) -> None:
        if self.on_dtmf:
            self.on_dtmf(digit)

    def _on_rtp(self, pkt: RtpPacket) -> None:
        call = self.call
        neg = call.negotiated
        if not neg or call.state != "active":
            return
        if neg.dtmf_pt is not None and pkt.pt == neg.dtmf_pt:
            if len(pkt.payload) >= 4 and pkt.ts != self._last_dtmf_ts and pkt.payload[0] < len(DTMF_EVENTS):
                self._last_dtmf_ts = pkt.ts
                self._dtmf(DTMF_EVENTS[pkt.payload[0]])
            return
        if pkt.pt != neg.remote_pt:
            return
        if self.on_raw:
            self.on_raw(pkt.payload, self.codec)
        if not self.on_audio:
            return
        self.on_audio(self._up(pcmmod.g711_decode(pkt.payload, self.codec)))
