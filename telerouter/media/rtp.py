"""RTP over UDP: packet codec, port allocation, a socket endpoint and a pacer."""

from __future__ import annotations

import asyncio
import logging
import random
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger("telerouter.rtp")


@dataclass
class RtpPacket:
    pt: int
    seq: int
    ts: int
    ssrc: int
    marker: bool
    payload: bytes

    @classmethod
    def parse(cls, data: bytes) -> "RtpPacket | None":
        if len(data) < 12 or data[0] >> 6 != 2:
            return None
        b0, b1, seq, ts, ssrc = struct.unpack("!BBHII", data[:12])
        offset = 12 + 4 * (b0 & 0x0F)
        if b0 & 0x10:  # header extension
            if len(data) < offset + 4:
                return None
            ext_len = struct.unpack("!H", data[offset + 2:offset + 4])[0]
            offset += 4 + 4 * ext_len
        end = len(data)
        if b0 & 0x20 and end > offset:  # padding
            end -= data[-1]
        if offset > end:
            return None
        return cls(b1 & 0x7F, seq, ts, ssrc, bool(b1 & 0x80), data[offset:end])

    def to_bytes(self) -> bytes:
        return struct.pack("!BBHII", 0x80, (0x80 if self.marker else 0) | (self.pt & 0x7F),
                           self.seq & 0xFFFF, self.ts & 0xFFFFFFFF, self.ssrc) + self.payload


class RtpSender:
    """Keeps seq/timestamp/ssrc state for one outgoing RTP stream."""

    def __init__(self) -> None:
        self.ssrc = random.getrandbits(32)
        self.seq = random.getrandbits(16)
        self.ts = random.getrandbits(32)
        self.first = True

    def packet(self, pt: int, payload: bytes, samples: int, marker: bool = False) -> bytes:
        pkt = RtpPacket(pt, self.seq, self.ts, self.ssrc, marker or self.first, payload)
        self.first = False
        self.seq = (self.seq + 1) & 0xFFFF
        self.ts = (self.ts + samples) & 0xFFFFFFFF
        return pkt.to_bytes()


class PortAllocator:
    def __init__(self, lo: int, hi: int):
        self.lo = lo + (lo & 1)
        self.hi = hi
        self._next = self.lo
        self.in_use: set[int] = set()

    async def open(self, factory: Callable[[], asyncio.DatagramProtocol], host: str = "0.0.0.0"):
        loop = asyncio.get_running_loop()
        count = max(1, (self.hi - self.lo) // 2 + 1)
        for _ in range(count):
            port = self._next
            self._next = self.lo if self._next + 2 > self.hi else self._next + 2
            if port in self.in_use:
                continue
            try:
                transport, proto = await loop.create_datagram_endpoint(factory, local_addr=(host, port))
            except OSError:
                continue
            self.in_use.add(port)
            return transport, proto, port
        raise RuntimeError(f"no free RTP port in {self.lo}-{self.hi}")

    def release(self, port: int) -> None:
        self.in_use.discard(port)


class RtpEndpoint(asyncio.DatagramProtocol):
    """One RTP socket. Sends to `remote`, reports received packets via callback."""

    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None
        self.remote: tuple[str, int] | None = None
        self.on_packet: Callable[[RtpPacket], None] | None = None
        self.sender = RtpSender()
        self.port = 0
        self.rx_packets = 0
        self.tx_packets = 0
        self.rx_bytes = 0
        self.tx_bytes = 0
        self.last_rx = 0.0
        self._latched = False
        self.send_enabled = True

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) >= 2 and 200 <= data[1] <= 204:
            return  # RTCP (rtcp-mux) - ignored
        pkt = RtpPacket.parse(data)
        if pkt is None:
            return
        # Symmetric RTP: latch onto the real source once (NAT friendly).
        if self.remote is None or (not self._latched and addr[:2] != self.remote):
            self.remote = (addr[0], addr[1])
        self._latched = True
        self.rx_packets += 1
        self.rx_bytes += len(pkt.payload)
        self.last_rx = time.monotonic()
        if self.on_packet:
            try:
                self.on_packet(pkt)
            except Exception:  # never let a media callback kill the socket
                log.exception("RTP packet handler failed")

    def set_remote(self, host: str, port: int) -> None:
        if host in ("0.0.0.0", "") or port == 0:
            self.send_enabled = False
            return
        self.send_enabled = True
        if not self._latched or self.remote is None:
            self.remote = (host, port)

    def send(self, pt: int, payload: bytes, samples: int, marker: bool = False) -> None:
        if not self.transport or not self.remote or not self.send_enabled:
            return
        data = self.sender.packet(pt, payload, samples, marker)
        self.transport.sendto(data, self.remote)
        self.tx_packets += 1
        self.tx_bytes += len(payload)

    def send_event(self, pt: int, payload: bytes, ts: int, marker: bool = False) -> None:
        """Send a packet with an explicit timestamp (RFC 4733 events share one)."""
        if not self.transport or not self.remote or not self.send_enabled:
            return
        pkt = RtpPacket(pt, self.sender.seq, ts, self.sender.ssrc, marker, payload)
        self.sender.seq = (self.sender.seq + 1) & 0xFFFF
        self.transport.sendto(pkt.to_bytes(), self.remote)
        self.tx_packets += 1

    def close(self) -> None:
        if self.transport:
            self.transport.close()
            self.transport = None


class Pacer:
    """Turn bursty audio into a steady 20 ms frame clock (adaptive jitter buffer).

    Sources can deliver audio in irregular chunks (e.g. 128 ms at a time).
    Bytes are buffered and emitted as fixed frames. Playback starts once the
    buffer holds the largest chunk seen plus a small margin; every underrun
    grows that margin a little, and excess backlog is dropped so latency stays
    bounded.
    """

    def __init__(self, emit: Callable[[bytes], None], silence_byte: int,
                 frame_bytes: int = 160, frame_ms: int = 20, max_extra_ms: int = 240):
        self.emit = emit
        self.silence = bytes([silence_byte]) * frame_bytes
        self.frame_bytes = frame_bytes
        self.frame_s = frame_ms / 1000.0
        self.buf = bytearray()
        self.chunk = frame_bytes                 # largest input chunk seen
        self.margin = frame_bytes * 2            # grows on underruns
        self.max_margin = frame_bytes * 15       # 300 ms
        self.max_extra = frame_bytes * (max_extra_ms // frame_ms)
        self.primed = False
        self.underruns = 0
        self.dropped = 0
        self._task: asyncio.Task | None = None

    @property
    def target(self) -> int:
        return self.chunk + self.margin

    def push(self, data: bytes) -> None:
        self.chunk = max(self.chunk, min(len(data), 8 * 1024))
        self.buf += data
        limit = self.target + self.max_extra
        if len(self.buf) > limit:
            self._trim(self.target)

    def _trim(self, size: int) -> None:
        drop = len(self.buf) - size
        if drop > 0:
            del self.buf[:drop]
            self.dropped += drop

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def next_frame(self) -> bytes:
        if not self.primed and len(self.buf) >= self.target:
            self.primed = True
            self._trim(self.target)
        if self.primed and len(self.buf) >= self.frame_bytes:
            frame = bytes(self.buf[: self.frame_bytes])
            del self.buf[: self.frame_bytes]
            return frame
        if self.primed:
            self.underruns += 1
            self.primed = False
            self.margin = min(self.margin + self.frame_bytes, self.max_margin)
        return self.silence

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        next_t = loop.time()
        while True:
            next_t += self.frame_s
            delay = next_t - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -0.2:  # we fell far behind (suspend?) - resync clock
                next_t = loop.time()
            try:
                self.emit(self.next_frame())
            except Exception:
                log.exception("pacer emit failed")

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
