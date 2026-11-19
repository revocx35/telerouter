"""A small view-only VNC (RFB 3.3/3.7/3.8) viewer for the smart display's VNC sections.

The speaker client connects to the VNC server itself (e.g. an NVR or a machine that shows a security camera),
keeps the framebuffer in memory and hands the display page PNG snapshots (`/vnc/<section id>` on the loopback
display listener). It never sends keyboard or mouse events and asks for a *shared* session, so whoever else is
connected stays connected.

Supported: security None and VNC Authentication (DES challenge), encodings ZRLE, Raw, CopyRect and the
DesktopSize pseudo-encoding. Pure Python + numpy + zlib (no new dependencies).

RFB is plain TCP: the VNC password is only a DES challenge/response, the picture itself is not encrypted. Use it
on a trusted LAN, ideally with a view-only password on the server.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
import zlib

import numpy as np

log = logging.getLogger("telerouter_client")

MAX_DIM = 8192
MAX_RECTS = 65535
MAX_COMPRESSED = 64 << 20
MAX_CUT_TEXT = 1 << 20
PNG_MAX_WIDTH = 1920
CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 60.0


class VncError(Exception):
    pass


# -- DES (only for the VNC Authentication challenge) ------------------------------------------------------------
_PC1 = [57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18, 10, 2, 59, 51, 43, 35, 27, 19, 11, 3, 60, 52, 44,
        36, 63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 28, 20, 12, 4]
_PC2 = [14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10, 23, 19, 12, 4, 26, 8, 16, 7, 27, 20, 13, 2, 41, 52, 31, 37,
        47, 55, 30, 40, 51, 45, 33, 48, 44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32]
_SHIFTS = [1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1]
_IP = [58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4, 62, 54, 46, 38, 30, 22, 14, 6, 64, 56, 48, 40,
       32, 24, 16, 8, 57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3, 61, 53, 45, 37, 29, 21, 13, 5,
       63, 55, 47, 39, 31, 23, 15, 7]
_FP = [40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31, 38, 6, 46, 14, 54, 22, 62, 30, 37, 5, 45, 13,
       53, 21, 61, 29, 36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27, 34, 2, 42, 10, 50, 18, 58, 26,
       33, 1, 41, 9, 49, 17, 57, 25]
_E = [32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11, 12, 13, 12, 13, 14, 15, 16, 17, 16, 17, 18, 19, 20, 21,
      20, 21, 22, 23, 24, 25, 24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1]
_P = [16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10, 2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22,
      11, 4, 25]
_S = [
    [14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7, 0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
     4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0, 15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13],
    [15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10, 3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
     0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15, 13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9],
    [10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8, 13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
     13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7, 1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12],
    [7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15, 13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
     10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4, 3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14],
    [2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9, 14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
     4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14, 11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3],
    [12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11, 10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
     9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6, 4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13],
    [4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1, 13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
     1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2, 6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12],
    [13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7, 1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
     7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8, 2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11],
]


def _permute(v: int, table: list[int], width: int) -> int:
    out = 0
    for t in table:
        out = (out << 1) | ((v >> (width - t)) & 1)
    return out


def des_encrypt(key: bytes, block: bytes) -> bytes:
    """Single-block DES (ECB). Only used for the 16-byte VNC challenge."""
    k = _permute(int.from_bytes(key, "big"), _PC1, 64)
    c, d = k >> 28, k & 0xFFFFFFF
    subkeys = []
    for s in _SHIFTS:
        c = ((c << s) | (c >> (28 - s))) & 0xFFFFFFF
        d = ((d << s) | (d >> (28 - s))) & 0xFFFFFFF
        subkeys.append(_permute((c << 28) | d, _PC2, 56))
    v = _permute(int.from_bytes(block, "big"), _IP, 64)
    left, right = v >> 32, v & 0xFFFFFFFF
    for sk in subkeys:
        x = _permute(right, _E, 32) ^ sk
        f = 0
        for i in range(8):
            six = (x >> (42 - 6 * i)) & 0x3F
            row = ((six & 0x20) >> 4) | (six & 1)
            f = (f << 4) | _S[i][row * 16 + ((six >> 1) & 0xF)]
        left, right = right, left ^ _permute(f, _P, 32)
    return _permute((right << 32) | left, _FP, 64).to_bytes(8, "big")


def vnc_auth_response(password: str, challenge: bytes) -> bytes:
    """VNC Authentication: DES of the challenge with the password (8 bytes, each byte's bits reversed)."""
    pw = password.encode("latin-1", "replace")[:8].ljust(8, b"\0")
    key = bytes(int(f"{b:08b}"[::-1], 2) for b in pw)
    return des_encrypt(key, challenge[:8]) + des_encrypt(key, challenge[8:16])


# -- PNG ----------------------------------------------------------------------------------------------------------
def encode_png(rgb: np.ndarray, level: int = 1) -> bytes:
    """An RGB (h, w, 3) uint8 array as PNG (filter: Up, which suits screens and camera pictures well)."""
    h, w, _ = rgb.shape
    rows = rgb.reshape(h, w * 3)
    up = rows.copy()
    up[1:] -= rows[:-1]                     # uint8 wrap-around is exactly PNG's modulo-256 arithmetic
    raw = np.empty((h, w * 3 + 1), np.uint8)
    raw[:, 0] = 2
    raw[0, 0] = 0
    raw[0, 1:] = rows[0]
    raw[1:, 1:] = up[1:]

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw.tobytes(), level)) + chunk(b"IEND", b""))


# -- the viewer -----------------------------------------------------------------------------------------------------
class VncViewer:
    """Keeps a shared, view-only RFB session open and the picture up to date (reconnects on errors)."""

    def __init__(self, host: str, port: int = 5900, password: str = "", fps: float = 2.0):
        self.host, self.port, self.password = host, int(port), password or ""
        self.fps = min(10.0, max(0.2, float(fps or 2.0)))
        self.fb: np.ndarray | None = None
        self.seq = 0                         # bumps with every finished update
        self.status = "connecting"           # connecting | connected | error
        self.error = ""
        self.name = ""
        self._z = zlib.decompressobj()
        self._task: asyncio.Task | None = None
        self._png: tuple[int, bytes] = (-1, b"")
        self._png_lock = asyncio.Lock()

    def key(self) -> tuple:
        return ("vnc", self.host, self.port, self.password, self.fps)

    async def image(self) -> tuple[int, bytes, str]:
        seq, png = await self.png()
        return seq, png, "image/png"

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        backoff = 2.0
        while True:
            t0 = time.monotonic()
            writer = None
            try:
                self.status, self._z = "connecting", zlib.decompressobj()
                reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port),
                                                        CONNECT_TIMEOUT_S)
                await self.session(reader, writer)
            except asyncio.CancelledError:
                raise
            except (VncError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError,
                    IndexError, zlib.error) as e:
                self.status = "error"
                self.error = str(e) if isinstance(e, VncError) else (
                    "the VNC server did not answer" if isinstance(e, asyncio.TimeoutError)
                    else "the connection ended" if isinstance(e, asyncio.IncompleteReadError)
                    else str(e) or e.__class__.__name__)
                log.warning("vnc %s:%d: %s", self.host, self.port, self.error)
            finally:
                if writer is not None:
                    writer.close()
            if time.monotonic() - t0 > 60:
                backoff = 2.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
            if "password" in self.error:
                backoff = 300.0               # don't hammer a server that refuses us (it may lock us out)

    # -- protocol ---------------------------------------------------------------------------------------------
    async def _read(self, reader, n: int) -> bytes:
        return await asyncio.wait_for(reader.readexactly(n), READ_TIMEOUT_S)

    async def _reason(self, reader) -> str:
        (n,) = struct.unpack(">I", await self._read(reader, 4))
        return (await self._read(reader, min(n, 4096))).decode("utf-8", "replace")

    async def session(self, reader, writer) -> None:
        ver = await self._read(reader, 12)
        if not ver.startswith(b"RFB ") or len(ver) != 12:
            raise VncError("not a VNC server")
        try:
            major, minor = int(ver[4:7]), int(ver[8:11])
        except ValueError:
            raise VncError("not a VNC server") from None
        minor = 8 if (major, minor) >= (3, 8) else 7 if (major, minor) >= (3, 7) else 3
        writer.write(b"RFB 003.%03d\n" % minor)
        await writer.drain()
        if minor == 3:
            (sec,) = struct.unpack(">I", await self._read(reader, 4))
            if sec == 0:
                raise VncError("refused: " + await self._reason(reader))
        else:
            (n,) = struct.unpack(">B", await self._read(reader, 1))
            if n == 0:
                raise VncError("refused: " + await self._reason(reader))
            types = await self._read(reader, n)
            sec = 1 if 1 in types and not self.password else 2 if 2 in types else 1 if 1 in types else 0
            if not sec:
                raise VncError("the server wants a security type this viewer doesn't support "
                               f"({', '.join(str(t) for t in types)}); use VNC password or none")
            writer.write(bytes([sec]))
            await writer.drain()
        if sec == 2:
            if not self.password:
                raise VncError("the server wants a password")
            challenge = await self._read(reader, 16)
            writer.write(vnc_auth_response(self.password, challenge))
            await writer.drain()
        elif sec != 1:
            raise VncError(f"unsupported security type {sec}")
        if sec == 2 or minor == 8:
            (res,) = struct.unpack(">I", await self._read(reader, 4))
            if res != 0:
                why = await self._reason(reader) if minor == 8 else ""
                raise VncError("wrong password" + (f" ({why})" if why else "") if sec == 2
                               else "refused" + (f": {why}" if why else ""))
        writer.write(b"\x01")                          # ClientInit: shared session, others stay connected
        await writer.drain()
        w, h = struct.unpack(">HH", await self._read(reader, 4))
        await self._read(reader, 16)                   # the server's pixel format: we set our own below
        self.name = (await self._reason(reader))[:200]
        self._resize(w, h)
        # 32 bpp, depth 24, little-endian true colour, 8 bits per channel: R<<16 | G<<8 | B
        writer.write(struct.pack(">BxxxBBBBHHHBBBxxx", 0, 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
        encs = [16, 1, 0, -223]                        # ZRLE, CopyRect, Raw, DesktopSize
        writer.write(struct.pack(">BxH", 2, len(encs)) + struct.pack(f">{len(encs)}i", *encs))
        writer.write(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))   # first update: the whole screen
        await writer.drain()
        self.status, self.error = "connected", ""
        log.info("vnc %s:%d connected (%dx%d, %s)", self.host, self.port, w, h, self.name or "unnamed")
        last_req = time.monotonic()
        while True:
            (kind,) = await self._read(reader, 1)
            if kind == 0:
                await self._update(reader)
                self.seq += 1
                wait = 1.0 / self.fps - (time.monotonic() - last_req)
                if wait > 0:
                    await asyncio.sleep(wait)
                fb = self.fb
                writer.write(struct.pack(">BBHHHH", 3, 1, 0, 0, fb.shape[1], fb.shape[0]))
                await writer.drain()
                last_req = time.monotonic()
            elif kind == 1:                            # SetColourMapEntries (not used with true colour)
                _, _, n = struct.unpack(">BHH", await self._read(reader, 5))
                await self._read(reader, 6 * n)
            elif kind == 2:                            # Bell
                continue
            elif kind == 3:                            # ServerCutText
                await self._read(reader, 3)
                (n,) = struct.unpack(">I", await self._read(reader, 4))
                if n > MAX_CUT_TEXT:
                    raise VncError("clipboard text too large")
                await self._read(reader, n)
            else:
                raise VncError(f"unknown server message {kind}")

    def _resize(self, w: int, h: int) -> None:
        if not (1 <= w <= MAX_DIM and 1 <= h <= MAX_DIM):
            raise VncError(f"unsupported screen size {w}x{h}")
        self.fb = np.zeros((h, w, 3), np.uint8)

    async def _update(self, reader) -> None:
        _, n = struct.unpack(">BH", await self._read(reader, 3))
        for _ in range(n):
            x, y, w, h, enc = struct.unpack(">HHHHi", await self._read(reader, 12))
            if enc == -223:                            # DesktopSize
                self._resize(w, h)
                continue
            fb = self.fb
            if x + w > fb.shape[1] or y + h > fb.shape[0]:
                raise VncError("a rectangle outside the screen")
            if enc == 0:
                px = np.frombuffer(await self._read(reader, w * h * 4), np.uint8).reshape(h, w, 4)
                fb[y:y + h, x:x + w] = px[:, :, 2::-1]
            elif enc == 1:
                sx, sy = struct.unpack(">HH", await self._read(reader, 4))
                if sx + w > fb.shape[1] or sy + h > fb.shape[0]:
                    raise VncError("a copy from outside the screen")
                fb[y:y + h, x:x + w] = fb[sy:sy + h, sx:sx + w].copy()
            elif enc == 16:
                (ln,) = struct.unpack(">I", await self._read(reader, 4))
                if ln > MAX_COMPRESSED:
                    raise VncError("update too large")
                self.zrle(x, y, w, h, await self._read(reader, ln))
            else:
                raise VncError(f"the server sent encoding {enc}, which was not asked for")

    def zrle(self, x: int, y: int, w: int, h: int, data: bytes) -> None:
        try:
            self._zrle(x, y, w, h, data)
        except (IndexError, ValueError, zlib.error) as e:
            raise VncError("a broken ZRLE update") from e

    def _zrle(self, x: int, y: int, w: int, h: int, data: bytes) -> None:
        tiles = ((w + 63) // 64) * ((h + 63) // 64)
        buf = self._z.decompress(data, w * h * 5 + tiles * 64 + 1024)   # cap: no zlib bomb
        if self._z.unconsumed_tail:
            raise VncError("a ZRLE rectangle larger than its size")
        fb, i = self.fb, 0
        for ty in range(y, y + h, 64):
            th = min(64, y + h - ty)
            for tx in range(x, x + w, 64):
                tw = min(64, x + w - tx)
                n = tw * th
                sub = buf[i]
                i += 1
                if sub == 0:                           # raw CPIXELs (B, G, R)
                    tile = np.frombuffer(buf, np.uint8, n * 3, i).reshape(th, tw, 3)[:, :, ::-1]
                    i += n * 3
                elif sub == 1:                         # solid
                    tile = (buf[i + 2], buf[i + 1], buf[i])
                    i += 3
                elif 2 <= sub <= 16:                   # packed palette
                    pal = np.frombuffer(buf, np.uint8, sub * 3, i).reshape(sub, 3)[:, ::-1]
                    i += sub * 3
                    bits = 1 if sub == 2 else 2 if sub <= 4 else 4
                    rb = (tw * bits + 7) // 8
                    raw = np.frombuffer(buf, np.uint8, rb * th, i).reshape(th, rb)
                    i += rb * th
                    shifts = np.arange(8 - bits, -1, -bits, dtype=np.uint8)
                    idx = ((raw[:, :, None] >> shifts) & ((1 << bits) - 1)).reshape(th, -1)[:, :tw]
                    tile = pal[idx]
                elif sub == 128 or sub >= 130:         # plain RLE / palette RLE
                    out = np.empty((n, 3), np.uint8)
                    pal = None
                    if sub >= 130:
                        k = sub - 128
                        pal = np.frombuffer(buf, np.uint8, k * 3, i).reshape(k, 3)[:, ::-1]
                        i += k * 3
                    p = 0
                    while p < n:
                        if pal is None:
                            color = (buf[i + 2], buf[i + 1], buf[i])
                            i += 3
                            long = True
                        else:
                            b = buf[i]
                            i += 1
                            color, long = pal[b & 127], b >= 128
                        run = 1
                        if long:
                            while True:
                                b = buf[i]
                                i += 1
                                run += b
                                if b != 255:
                                    break
                        if p + run > n:
                            raise VncError("a ZRLE run past its tile")
                        out[p:p + run] = color
                        p += run
                    tile = out.reshape(th, tw, 3)
                else:
                    raise VncError(f"bad ZRLE tile type {sub}")
                fb[ty:ty + th, tx:tx + tw] = tile

    # -- the picture for the page -----------------------------------------------------------------------------
    async def png(self) -> tuple[int, bytes]:
        """(seq, PNG) of the current picture; encoded at most once per update, in a thread."""
        async with self._png_lock:
            if self._png[0] == self.seq or self.fb is None:
                return self._png
            seq, fb = self.seq, self.fb
            step = -(-fb.shape[1] // PNG_MAX_WIDTH)
            snap = np.ascontiguousarray(fb[::step, ::step]) if step > 1 else fb.copy()
            self._png = (seq, await asyncio.to_thread(encode_png, snap))
            return self._png
