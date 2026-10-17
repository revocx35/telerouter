"""In-band DTMF detection (Goertzel) for calls without RFC 4733 telephone-events."""

from __future__ import annotations

import math

LOW = (697, 770, 852, 941)
HIGH = (1209, 1336, 1477, 1633)
KEYS = ("123A", "456B", "789C", "*0#D")
N = 205                       # ~25.6 ms at 8 kHz, the classic DTMF block size
RATE = 8000


class DtmfDetector:
    """Feed 8 kHz linear samples; returns a digit once per key press."""

    def __init__(self) -> None:
        self._coef = {f: 2 * math.cos(2 * math.pi * round(N * f / RATE) / N) for f in LOW + HIGH}
        self._buf: list[int] = []
        self._candidate: str | None = None
        self._hits = 0
        self._reported: str | None = None
        self._quiet = 0

    def _power(self, block: list[int], f: int) -> float:
        c = self._coef[f]
        s1 = s2 = 0.0
        for x in block:
            s0 = x + c * s1 - s2
            s2, s1 = s1, s0
        return s1 * s1 + s2 * s2 - c * s1 * s2

    def _block(self, block: list[int]) -> str | None:
        energy = sum(x * x for x in block)
        if energy < N * 300 * 300:          # too quiet to be a key tone
            return None
        low = [self._power(block, f) for f in LOW]
        high = [self._power(block, f) for f in HIGH]
        li = max(range(4), key=low.__getitem__)
        hi = max(range(4), key=high.__getitem__)
        lp, hp = low[li], high[hi]
        # both tones must dominate their group, have a sane twist, and hold most of the energy
        if any(v * 6 > lp for i, v in enumerate(low) if i != li):
            return None
        if any(v * 6 > hp for i, v in enumerate(high) if i != hi):
            return None
        if not (0.15 < hp / lp < 6.5):
            return None
        if (lp + hp) * 2 / N < energy * 0.5:
            return None
        return KEYS[li][hi]

    def feed(self, samples) -> str | None:
        self._buf.extend(samples)
        digit_out = None
        while len(self._buf) >= N:
            block, self._buf = self._buf[:N], self._buf[N:]
            d = self._block(block)
            if d is None:
                self._quiet += 1
                if self._quiet >= 2:             # released: allow the same key again
                    self._reported = None
                    self._candidate = None
                    self._hits = 0
                continue
            self._quiet = 0
            if d == self._candidate:
                self._hits += 1
            else:
                self._candidate, self._hits = d, 1
            if self._hits >= 2 and d != self._reported:   # >= ~50 ms of the same key
                self._reported = d
                digit_out = d
        return digit_out
