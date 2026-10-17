"""G.711 A-law / mu-law helpers built on 256-entry lookup tables.

Because every G.711 byte maps to one linear sample, transcoding between A-law
and mu-law *and* applying a gain can be done in a single ``bytes.translate``
call using a precomputed table - no per-sample Python loop.
"""

from __future__ import annotations

import io
import math
import wave
from functools import lru_cache

_SEG_AEND = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)
_SEG_UEND = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)
_BIAS = 0x84
_CLIP = 8159

SILENCE = {"PCMA": 0xD5, "PCMU": 0xFF}


def _search(val: int, table: tuple[int, ...]) -> int:
    for i, end in enumerate(table):
        if val <= end:
            return i
    return len(table)


def alaw_to_linear(a: int) -> int:
    a ^= 0x55
    t = (a & 0x0F) << 4
    seg = (a & 0x70) >> 4
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t += 0x108
        t <<= seg - 1
    return t if a & 0x80 else -t


def linear_to_alaw(pcm: int) -> int:
    pcm = max(-32768, min(32767, int(pcm))) >> 3
    if pcm >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        pcm = -pcm - 1
    seg = _search(pcm, _SEG_AEND)
    if seg >= 8:
        return 0x7F ^ mask
    aval = seg << 4
    aval |= (pcm >> 1) & 0x0F if seg < 2 else (pcm >> seg) & 0x0F
    return aval ^ mask


def ulaw_to_linear(u: int) -> int:
    u = ~u & 0xFF
    t = ((u & 0x0F) << 3) + _BIAS
    t <<= (u & 0x70) >> 4
    return (_BIAS - t) if u & 0x80 else (t - _BIAS)


def linear_to_ulaw(pcm: int) -> int:
    pcm = max(-32768, min(32767, int(pcm))) >> 2
    if pcm < 0:
        pcm = -pcm
        mask = 0x7F
    else:
        mask = 0xFF
    pcm = min(pcm, _CLIP) + (_BIAS >> 2)
    seg = _search(pcm, _SEG_UEND)
    if seg >= 8:
        return 0x7F ^ mask
    return ((seg << 4) | ((pcm >> (seg + 1)) & 0x0F)) ^ mask


DECODE = {
    "PCMA": [alaw_to_linear(i) for i in range(256)],
    "PCMU": [ulaw_to_linear(i) for i in range(256)],
}
ENCODE = {"PCMA": linear_to_alaw, "PCMU": linear_to_ulaw}
_SQUARES = {k: [v * v for v in table] for k, table in DECODE.items()}
# peak detection without a Python loop: translate each byte to the rank of its
# magnitude, take max() of that, look the magnitude back up
_MAGNITUDES = {k: sorted({abs(v) for v in table}) for k, table in DECODE.items()}
_MAG_RANK = {k: bytes(_MAGNITUDES[k].index(abs(v)) for v in table) for k, table in DECODE.items()}
_SIGN = {k: bytes(v < 0 for v in table) for k, table in DECODE.items()}


@lru_cache(maxsize=512)
def translate_table(src: str, dst: str, gain_db: float = 0.0) -> bytes | None:
    """Table converting `src` codec bytes to `dst` codec bytes with a gain.

    Returns None when the conversion is the identity (no work needed).
    """
    if src == dst and abs(gain_db) < 0.01:
        return None
    g = 10 ** (gain_db / 20.0)
    dec, enc = DECODE[src], ENCODE[dst]
    return bytes(enc(round(dec[i] * g)) for i in range(256))


def convert(payload: bytes, src: str, dst: str, gain_db: float = 0.0) -> bytes:
    table = translate_table(src, dst, round(gain_db, 1))
    return payload if table is None else payload.translate(table)


@lru_cache(maxsize=2)
def _linear_table(codec: str) -> bytes:
    enc = ENCODE[codec]
    return bytes(enc(i - 32768) for i in range(65536))


def encode_pcm(samples, codec: str) -> bytes:
    """Encode 16-bit linear samples (any iterable of ints) to G.711 bytes."""
    table = _linear_table(codec)
    return bytes(table[s + 32768] for s in samples)


def level_dbfs(payload: bytes, codec: str) -> float:
    """RMS level of a G.711 frame in dBFS (-96 for digital silence)."""
    if not payload:
        return -96.0
    mean = sum(map(_SQUARES[codec].__getitem__, payload)) / len(payload)
    if mean <= 0:
        return -96.0
    return max(-96.0, 10 * math.log10(mean / (32768.0 * 32768.0)))


def peak_dbfs(payload: bytes, codec: str) -> float:
    """Largest sample magnitude of a G.711 frame in dBFS (-96 for digital silence)."""
    if not payload:
        return -96.0
    peak = _MAGNITUDES[codec][max(payload.translate(_MAG_RANK[codec]))]
    return max(-96.0, 20 * math.log10(peak / 32768.0)) if peak else -96.0


def crossings(payload: bytes, codec: str) -> int:
    """Number of zero crossings (sign changes) in a G.711 frame, without a Python loop."""
    signs = payload.translate(_SIGN[codec])     # one 0/1 byte per sample
    return (int.from_bytes(signs[1:], "big") ^ int.from_bytes(signs[:-1], "big")).bit_count()


def silence(codec: str, nbytes: int = 160) -> bytes:
    return bytes([SILENCE[codec]]) * nbytes


def tone(codec: str, freq: float = 880.0, seconds: float = 1.0, level_db: float = -12.0,
         rate: int = 8000) -> bytes:
    amp = 32767 * 10 ** (level_db / 20.0)
    enc = ENCODE[codec]
    n = int(seconds * rate)
    fade = int(0.02 * rate)
    out = bytearray(n)
    for i in range(n):
        env = min(1.0, i / fade, (n - 1 - i) / fade) if fade else 1.0
        out[i] = enc(round(amp * env * math.sin(2 * math.pi * freq * i / rate)))
    return bytes(out)


def to_wav(payload: bytes, codec: str, rate: int = 8000) -> bytes:
    dec = DECODE[codec]
    pcm = bytearray()
    for b in payload:
        pcm += int(dec[b]).to_bytes(2, "little", signed=True)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(pcm))
    return buf.getvalue()
