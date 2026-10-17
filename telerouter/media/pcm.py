"""PCM helpers (numpy): resampling, G.711 <-> 16-bit, cue sounds, levels, WAV.

Internally all speech audio is 16 kHz mono signed 16-bit little endian ("PCM16"): clients send and
receive it, Whisper and openWakeWord take it. SIP calls are G.711 at 8 kHz.
"""

from __future__ import annotations

import io
import math
import wave
from functools import lru_cache

import numpy as np
import soxr

from . import g711

RATE = 16000
FRAME_MS = 20
FRAME_SAMPLES = RATE * FRAME_MS // 1000      # 320
FRAME_BYTES = FRAME_SAMPLES * 2              # 640


def to_array(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2")


def resample(pcm: bytes, src: int, dst: int) -> bytes:
    if src == dst or not pcm:
        return pcm
    out = soxr.resample(to_array(pcm).astype(np.float32) / 32768.0, src, dst, quality="HQ")
    return (np.clip(out, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


class StreamResampler:
    """Chunk-by-chunk resampling without clicks at chunk borders (keeps filter state)."""

    def __init__(self, src: int, dst: int):
        self.src, self.dst = src, dst
        self._rs = soxr.ResampleStream(src, dst, 1, dtype="int16", quality="HQ") if src != dst else None

    def __call__(self, pcm: bytes) -> bytes:
        if self._rs is None:
            return pcm
        return self._rs.resample_chunk(to_array(pcm)).astype("<i2").tobytes()


# -- G.711 ------------------------------------------------------------------------------------------
@lru_cache(maxsize=2)
def _decode_table(codec: str) -> np.ndarray:
    fn = g711.alaw_to_linear if codec == "PCMA" else g711.ulaw_to_linear
    return np.array([fn(b) for b in range(256)], dtype=np.int16)


@lru_cache(maxsize=2)
def _encode_table(codec: str) -> np.ndarray:
    fn = g711.linear_to_alaw if codec == "PCMA" else g711.linear_to_ulaw
    # index = sample >> 2 (14-bit) is plenty for G.711 (13/14-bit companded)
    return np.array([fn((i - 8192) << 2) for i in range(16384)], dtype=np.uint8)


def g711_decode(payload: bytes, codec: str) -> bytes:
    return _decode_table(codec)[np.frombuffer(payload, dtype=np.uint8)].astype("<i2").tobytes()


def g711_encode(pcm: bytes, codec: str) -> bytes:
    idx = (to_array(pcm).astype(np.int32) >> 2) + 8192
    return _encode_table(codec)[idx].tobytes()


# -- levels -----------------------------------------------------------------------------------------
def rms_dbfs(pcm: bytes) -> float:
    a = to_array(pcm).astype(np.float32)
    if not len(a):
        return -120.0
    rms = math.sqrt(float(np.mean(a * a))) / 32768.0
    return 20 * math.log10(rms) if rms > 0 else -120.0


def gain(pcm: bytes, percent: int) -> bytes:
    if percent == 100 or not pcm:
        return pcm
    a = to_array(pcm).astype(np.float32) * (percent / 100.0)
    return np.clip(a, -32768, 32767).astype("<i2").tobytes()


def silence(ms: int, rate: int = RATE) -> bytes:
    return bytes(2 * rate * ms // 1000)


# -- cue sounds ----------------------------------------------------------------------------------------
def _tone(freqs: list[tuple[float, float]], volume: float, rate: int = RATE) -> bytes:
    """Concatenated sine tones [(Hz, seconds)], soft attack/release, `volume` 0..1."""
    parts = []
    for f, secs in freqs:
        n = int(rate * secs)
        t = np.arange(n) / rate
        x = np.sin(2 * np.pi * f * t) if f > 0 else np.zeros(n)
        ramp = min(n // 4, int(rate * 0.008))
        if ramp:
            env = np.ones(n)
            env[:ramp] = np.linspace(0, 1, ramp)
            env[-ramp:] = np.linspace(1, 0, ramp)
            x = x * env
        parts.append(x)
    a = np.concatenate(parts) if parts else np.zeros(0)
    return (a * 32767 * 0.7 * volume).astype("<i2").tobytes()


@lru_cache(maxsize=32)
def cue(name: str, volume_pct: int = 40) -> bytes:
    v = max(0.0, min(1.0, volume_pct / 100.0))
    if name == "start":       # "listening": rising blip
        return _tone([(660, 0.07), (0, 0.02), (990, 0.09)], v)
    if name == "end":         # "got it": falling blip
        return _tone([(990, 0.07), (0, 0.02), (660, 0.09)], v)
    if name == "error":
        return _tone([(330, 0.18), (0, 0.05), (262, 0.25)], v)
    if name == "think":       # soft tick while the agent works
        return _tone([(1320, 0.03)], v * 0.35)
    if name == "ringback":    # 425 Hz, 1 s on 4 s off (one period)
        return _tone([(425, 1.0), (0, 4.0)], v * 0.6)
    if name == "ring":        # incoming call on a speaker: two short bursts, 3 s pause
        return _tone([(880, 0.35), (660, 0.35), (0, 0.25), (880, 0.35), (660, 0.35), (0, 2.5)], v)
    raise KeyError(name)


# -- WAV -----------------------------------------------------------------------------------------------
def to_wav(pcm: bytes, rate: int = RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def from_wav(data: bytes) -> tuple[bytes, int]:
    """Mono PCM16 + rate from a WAV file (stereo is mixed down)."""
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("only 16-bit WAV is supported")
        frames = w.readframes(w.getnframes())
        rate, ch = w.getframerate(), w.getnchannels()
    a = to_array(frames)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1).astype("<i2")
    return a.tobytes(), rate
