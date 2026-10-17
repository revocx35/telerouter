"""Utterance endpointing with Silero VAD: wait for speech, then stop after a pause."""

from __future__ import annotations

from dataclasses import dataclass

from pysilero_vad import SileroVoiceActivityDetector

from ..media.pcm import RATE

CHUNK_BYTES = SileroVoiceActivityDetector.chunk_bytes()      # 512 samples = 32 ms
CHUNK_MS = CHUNK_BYTES * 1000 // (2 * RATE)


@dataclass
class EndpointConfig:
    start_timeout_ms: int = 6000      # no speech by then: give up
    end_silence_ms: int = 900         # pause that ends the utterance
    max_ms: int = 20000
    threshold: float = 0.5
    min_speech_ms: int = 160          # shorter blips (a cough, a click) don't count as speech
    pre_roll_ms: int = 300            # audio kept from just before speech started


class Endpointer:
    """Feed 16 kHz PCM16; `feed()` returns None while listening, else "speech", "timeout" or "max"."""

    def __init__(self, cfg: EndpointConfig, vad: SileroVoiceActivityDetector | None = None):
        self.cfg = cfg
        self.vad = vad or SileroVoiceActivityDetector()
        self.vad.reset()
        self._buf = bytearray()
        self.audio = bytearray()
        self.elapsed_ms = 0
        self.speech_ms = 0
        self.silence_ms = 0
        self.started = False
        self.result: str | None = None

    def feed(self, pcm: bytes) -> str | None:
        if self.result:
            return self.result
        self._buf += pcm
        while len(self._buf) >= CHUNK_BYTES and not self.result:
            chunk = bytes(self._buf[:CHUNK_BYTES])
            del self._buf[:CHUNK_BYTES]
            self._step(chunk)
        return self.result

    def _step(self, chunk: bytes) -> None:
        c = self.cfg
        self.elapsed_ms += CHUNK_MS
        self.audio += chunk
        speech = self.vad.process_chunk(chunk) >= c.threshold
        if not self.started:
            self.speech_ms = self.speech_ms + CHUNK_MS if speech else 0
            if self.speech_ms >= c.min_speech_ms:
                self.started = True
                keep = (c.pre_roll_ms + self.speech_ms) * 2 * RATE // 1000
                del self.audio[: max(0, len(self.audio) - keep)]
            elif self.elapsed_ms >= c.start_timeout_ms:
                self.result = "timeout"
            else:
                keep = (c.pre_roll_ms + self.speech_ms) * 2 * RATE // 1000
                if len(self.audio) > keep * 4:
                    del self.audio[: len(self.audio) - keep]
            return
        self.silence_ms = 0 if speech else self.silence_ms + CHUNK_MS
        if self.silence_ms >= c.end_silence_ms:
            self.result = "speech"
        elif self.elapsed_ms >= c.max_ms:
            self.result = "max"
