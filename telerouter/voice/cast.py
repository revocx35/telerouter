"""Google Cast speakers (Chromecast built-in) as speaker outputs.

Control: Cast v2 over TLS to <device>:8009. Frames are a 4-byte big-endian length + a protobuf CastMessage,
hand-encoded here (six fields, no protobuf dependency). telerouter launches the Default Media Receiver
(CC1AD845), keeps it for a few idle minutes (launching it again costs seconds and a chime), and plays each
burst of audio as one LOAD of a single-use URL on its media listener (see `CastMedia`).

Media: Cast receivers fetch media themselves and accept HTTPS only with a publicly trusted certificate,
so the media listener is plain HTTP on the LAN. Every burst gets a random 128-bit URL that only the
device's address may fetch, once, within 30 s (ARCHITECTURE.md, threat model).

Discovery: mDNS `_googlecast._tcp.local` (TXT fn = friendly name, md = model, id = device UUID). The UUID
finds the device again when DHCP gives it a new address.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import itertools
import json
import logging
import re
import secrets
import socket
import ssl
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..media import pcm as pcmmod
from ..models import CastDevice

log = logging.getLogger("telerouter.cast")

NS_CONN = "urn:x-cast:com.google.cast.tp.connection"
NS_HEARTBEAT = "urn:x-cast:com.google.cast.tp.heartbeat"
NS_RECEIVER = "urn:x-cast:com.google.cast.receiver"
NS_MEDIA = "urn:x-cast:com.google.cast.media"
MEDIA_RECEIVER = "CC1AD845"
SENDER = "sender-telerouter"
MAX_FRAME = 64 * 1024


# -- CastMessage (protobuf) ---------------------------------------------------------------------------------
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def encode_message(source: str, dest: str, namespace: str, payload: str) -> bytes:
    def s(field: int, v: str) -> bytes:
        b = v.encode()
        return bytes([field << 3 | 2]) + _varint(len(b)) + b
    body = (b"\x08\x00" + s(2, source) + s(3, dest) + s(4, namespace) + b"\x28\x00" + s(6, payload))
    return struct.pack(">I", len(body)) + body


def decode_message(body: bytes) -> dict:
    """CastMessage -> {"source", "dest", "namespace", "payload"} (string payloads only)."""
    names = {2: "source", 3: "dest", 4: "namespace", 6: "payload"}
    out: dict = {}
    i = 0
    while i < len(body):
        key, i = _read_varint(body, i)
        field, wt = key >> 3, key & 7
        if wt == 0:
            _, i = _read_varint(body, i)
        elif wt == 2:
            n, i = _read_varint(body, i)
            if field in names:
                out[names[field]] = body[i:i + n].decode("utf-8", "replace")
            i += n
        else:
            raise ValueError("unsupported protobuf wire type")
    return out


def _read_varint(b: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        if i >= len(b) or shift > 63:
            raise ValueError("bad varint")
        c = b[i]
        i += 1
        n |= (c & 0x7F) << shift
        shift += 7
        if not c & 0x80:
            return n, i


class CastError(Exception):
    pass


class CastChannel:
    """One TLS control connection to a Cast device.

    The device's certificate is self-signed and changes over time; it is not verified (Cast device
    authentication needs Google's Cast CA chain). What crosses this channel is control only (app launch,
    the single-use media URL, volume)."""

    def __init__(self, host: str, port: int = 8009):
        self.host, self.port = host, port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._tasks: list[asyncio.Task] = []
        self._connected: set[str] = set()
        self.closed = asyncio.Event()
        self.on_message: Callable[[str, str, dict], None] | None = None
        self.local_ip = ""
        self.peer_ip = ""

    async def open(self, timeout: float = 5.0) -> None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, ssl=ctx), timeout)
        self.local_ip = self._writer.get_extra_info("sockname")[0]      # our address as the device sees it
        self.peer_ip = self._writer.get_extra_info("peername")[0]
        self._tasks = [asyncio.create_task(self._read_loop()), asyncio.create_task(self._heartbeat())]
        self.connect_to("receiver-0")

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._writer:
            with contextlib.suppress(Exception):
                self._writer.close()
        self._fail(CastError("connection closed"))
        self.closed.set()

    def _fail(self, ex: Exception) -> None:
        for f in self._pending.values():
            if not f.done():
                f.set_exception(ex)
        self._pending.clear()

    def send(self, namespace: str, dest: str, payload: dict) -> None:
        if not self._writer or self.closed.is_set():
            raise CastError("not connected")
        self._writer.write(encode_message(SENDER, dest, namespace, json.dumps(payload)))

    def connect_to(self, dest: str) -> None:
        if dest not in self._connected:
            self.send(NS_CONN, dest, {"type": "CONNECT", "origin": {}})
            self._connected.add(dest)

    def notify(self, namespace: str, dest: str, payload: dict) -> None:
        """A request whose answer nobody waits for (it still needs a requestId)."""
        self.send(namespace, dest, {**payload, "requestId": next(self._ids)})

    async def request(self, namespace: str, dest: str, payload: dict, timeout: float = 10.0) -> dict:
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            self.send(namespace, dest, {**payload, "requestId": rid})
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError as e:
            raise CastError(f"{payload.get('type')}: no answer from the device") from e
        finally:
            self._pending.pop(rid, None)

    async def _read_loop(self) -> None:
        assert self._reader
        try:
            while True:
                head = await self._reader.readexactly(4)
                (n,) = struct.unpack(">I", head)
                if n > MAX_FRAME:
                    raise CastError("frame too large")
                msg = decode_message(await self._reader.readexactly(n))
                try:
                    payload = json.loads(msg.get("payload") or "{}")
                except ValueError:
                    continue
                if not isinstance(payload, dict):
                    continue
                ns, src = msg.get("namespace", ""), msg.get("source", "")
                if ns == NS_HEARTBEAT and payload.get("type") == "PING":
                    self.send(NS_HEARTBEAT, src, {"type": "PONG"})
                    continue
                if ns == NS_CONN and payload.get("type") == "CLOSE":
                    self._connected.discard(src)
                rid = payload.get("requestId")
                fut = self._pending.get(rid) if isinstance(rid, int) and rid else None
                if fut and not fut.done():
                    fut.set_result(payload)
                if self.on_message:
                    try:
                        self.on_message(ns, src, payload)
                    except Exception:
                        log.exception("cast message handler failed")
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ssl.SSLError, ValueError, CastError) as e:
            log.info("cast %s: connection closed (%s)", self.host, e or type(e).__name__)
        finally:
            self._fail(CastError("connection closed"))
            self.closed.set()

    async def _heartbeat(self) -> None:
        with contextlib.suppress(CastError, OSError):
            while True:
                await asyncio.sleep(5)
                self.send(NS_HEARTBEAT, "receiver-0", {"type": "PING"})

    # -- receiver -----------------------------------------------------------------------------------------
    async def receiver_status(self) -> dict:
        return (await self.request(NS_RECEIVER, "receiver-0", {"type": "GET_STATUS"})).get("status") or {}

    async def launch(self, app_id: str = MEDIA_RECEIVER) -> dict:
        """Launch (or find running) `app_id`; returns its application entry (transportId, sessionId)."""
        st = await self.receiver_status()
        app = next((a for a in st.get("applications") or [] if a.get("appId") == app_id), None)
        if app is None:
            res = await self.request(NS_RECEIVER, "receiver-0", {"type": "LAUNCH", "appId": app_id}, 20)
            if res.get("type") == "LAUNCH_ERROR":
                raise CastError(f"launch failed: {res.get('reason')}")
            app = next((a for a in (res.get("status") or {}).get("applications") or []
                        if a.get("appId") == app_id), None)
        if not app or not app.get("transportId"):
            raise CastError("the media receiver did not start")
        return app

    async def stop_app(self, session_id: str) -> None:
        with contextlib.suppress(CastError):
            await self.request(NS_RECEIVER, "receiver-0", {"type": "STOP", "sessionId": session_id}, 5)


# -- discovery ----------------------------------------------------------------------------------------------
@dataclass
class Found:
    uuid: str
    name: str
    model: str
    host: str
    port: int = 8009


def _mdns_query(service: str) -> bytes:
    q = b"\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    for part in service.split("."):
        q += bytes([len(part)]) + part.encode()
    return q + b"\x00\x00\x0c\x80\x01"         # PTR, class IN with the unicast-response bit


def _read_name(b: bytes, i: int, depth: int = 0) -> tuple[str, int]:
    parts = []
    while True:
        if i >= len(b) or depth > 10:
            raise ValueError("bad name")
        n = b[i]
        if n == 0:
            return ".".join(parts), i + 1
        if n & 0xC0 == 0xC0:
            ptr = ((n & 0x3F) << 8) | b[i + 1]
            rest, _ = _read_name(b, ptr, depth + 1)
            return ".".join(parts + [rest] if rest else parts), i + 2
        parts.append(b[i + 1:i + 1 + n].decode("utf-8", "replace"))
        i += 1 + n


def parse_mdns(b: bytes, sender: str) -> Found | None:
    """A Cast device's mDNS answer -> Found (TXT id/fn/md, SRV port, A address)."""
    try:
        qd, an, ns, ar = struct.unpack(">4H", b[4:12])
        i = 12
        for _ in range(qd):
            _, i = _read_name(b, i)
            i += 4
        txt: dict[str, str] = {}
        port, addr = 8009, ""
        for _ in range(an + ns + ar):
            _, i = _read_name(b, i)
            rtype, _, _, rdlen = struct.unpack(">HHIH", b[i:i + 10])
            i += 10
            rd = b[i:i + rdlen]
            if rtype == 16:                      # TXT
                j = 0
                while j < len(rd):
                    n = rd[j]
                    k, _, v = rd[j + 1:j + 1 + n].decode("utf-8", "replace").partition("=")
                    txt[k] = v
                    j += 1 + n
            elif rtype == 33 and rdlen >= 6:     # SRV
                port = struct.unpack(">H", rd[4:6])[0]
            elif rtype == 1 and rdlen == 4:      # A
                addr = socket.inet_ntoa(rd)
            i += rdlen
    except (ValueError, struct.error, IndexError):
        return None
    if not txt.get("id"):
        return None
    return Found(uuid=txt["id"], name=txt.get("fn", "")[:64], model=txt.get("md", "")[:64],
                 host=addr or sender, port=port)


async def discover(timeout: float = 3.0) -> list[Found]:
    """Cast devices on the LAN (mDNS, needs host networking)."""
    loop = asyncio.get_running_loop()
    found: dict[str, Found] = {}

    class Proto(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            f = parse_mdns(data, addr[0])
            if f:
                found[f.uuid] = f

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.bind(("0.0.0.0", 0))
    transport, _ = await loop.create_datagram_endpoint(Proto, sock=sock)
    try:
        q = _mdns_query("_googlecast._tcp.local")
        for _ in range(2):
            transport.sendto(q, ("224.0.0.251", 5353))
            await asyncio.sleep(timeout / 2)
    finally:
        transport.close()
    return sorted(found.values(), key=lambda f: f.name.lower())


# -- media listener -------------------------------------------------------------------------------------------
MEDIA_TTL = 60.0
MAX_MEDIA_CONNS = 16
_MEDIA_PATH = re.compile(r"^/cast/([0-9a-f]{32})\.wav$")


def _plain_ip(addr: str) -> str:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return addr
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return str(ip.ipv4_mapped)
    return str(ip)


class CastMedia:
    """The plain-HTTP listener Cast devices fetch audio from. Started on first use. Only offered files exist:
    GET /cast/<128-bit random>.wav, only from the device it was offered to, for MEDIA_TTL seconds."""

    def __init__(self, bind: str, port: int):
        self.bind, self.port = bind, port
        self._items: dict[str, tuple[bytes, str, float]] = {}
        self._server: asyncio.base_events.Server | None = None
        self._lock = asyncio.Lock()
        self._conns = 0

    async def ensure(self) -> None:
        async with self._lock:
            if self._server is None:
                self._server = await asyncio.start_server(self._handle, self.bind, self.port, limit=8192)
                log.info("cast media listener on %s:%d (plain HTTP, single-use URLs)", self.bind, self.port)

    async def close(self) -> None:
        if self._server:
            self._server.close()
            self._server = None

    def offer(self, data: bytes, peer_ip: str) -> str:
        now = time.monotonic()
        self._items = {k: v for k, v in self._items.items() if v[2] > now}
        token = secrets.token_hex(16)
        self._items[token] = (data, _plain_ip(peer_ip), now + MEDIA_TTL)
        return f"/cast/{token}.wav"

    def withdraw(self, path: str) -> None:
        m = _MEDIA_PATH.match(path)
        if m:
            self._items.pop(m.group(1), None)

    def _lookup(self, method: str, path: str, peer: str) -> bytes | None:
        m = _MEDIA_PATH.match(path)
        item = self._items.get(m.group(1)) if m and method in ("GET", "HEAD") else None
        if not item or item[2] < time.monotonic() or item[1] != _plain_ip(peer):
            return None
        return item[0]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._conns += 1
        try:
            if self._conns > MAX_MEDIA_CONNS:
                return
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
                return
            parts = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ")
            peer = (writer.get_extra_info("peername") or ("",))[0]
            data = self._lookup(parts[0], parts[1], peer) if len(parts) == 3 else None
            if data is None:
                log.info("cast media: refused %s from %s", " ".join(parts[:2])[:80], peer)
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            else:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: audio/wav\r\nContent-Length: %d\r\n"
                             b"Cache-Control: no-store\r\nConnection: close\r\n\r\n" % len(data))
                if parts[0] == "GET":
                    writer.write(data)
            with contextlib.suppress(ConnectionError, TimeoutError):
                await asyncio.wait_for(writer.drain(), 30)
        finally:
            self._conns -= 1
            with contextlib.suppress(Exception):
                writer.close()


# -- output ---------------------------------------------------------------------------------------------------
SETTLE_S = 0.12          # audio arriving closer together than this goes into one LOAD
IDLE_STOP_S = 300.0      # stop the receiver app after this long unused (relaunching costs ~1.5 s)
RETRY_S = 60.0           # after a failure, don't try the device again for this long (the mic device plays)
DONE = ("FINISHED", "CANCELLED", "INTERRUPTED", "ERROR")


class CastOutput:
    """A Cast speaker as a speaker output: play()/flush()/drained() like a client device.

    Audio is cut into segments (a sentence, a beep) and each is LOADed as a complete WAV: the receiver starts
    a file in ~0.15 s and reports FINISHED when it is done, so drained() is exact. Streams are not used: the
    receiver buffers ~7 s of a live WAV before it plays. Hence `live_ok = False` (no live calls here).
    If the device can't be reached, the audio goes to `spill` (the mic device of the speaker that played)."""

    kind = "cast"
    live_ok = False

    def __init__(self, dev: CastDevice, media: CastMedia,
                 on_moved: Callable[[CastDevice, str, int], None] | None = None):
        self.dev = dev
        self.id = dev.id
        self.media = media
        self.on_moved = on_moved
        self.spill: Callable[[bytes], None] | None = None
        self.state = "idle"
        self.error = ""
        self._retry_at = 0.0
        self._buf = bytearray()
        self._last_add = 0.0
        self._wake = asyncio.Event()
        self._drained = asyncio.Event()
        self._drained.set()
        self._busy = False
        self._stop = asyncio.Event()
        self._changed = asyncio.Event()
        self._lock = asyncio.Lock()
        self._ch: CastChannel | None = None
        self._app: dict | None = None
        self._media: dict[int, tuple[str, str]] = {}      # mediaSessionId -> (playerState, idleReason)
        self._last_used = 0.0
        self._prep: asyncio.Task | None = None
        self._task = asyncio.create_task(self._run())

    @property
    def name(self) -> str:
        return self.dev.name

    @property
    def available(self) -> bool:
        return self.dev.enabled and time.monotonic() >= self._retry_at

    # -- output interface ---------------------------------------------------------------------------------
    def play(self, audio: bytes) -> None:
        if not audio:
            return
        if not self.available and self.spill:
            self.spill(audio)
            return
        self._buf += audio
        self._last_add = time.monotonic()
        self._drained.clear()
        self._wake.set()

    def flush(self) -> None:
        self._buf.clear()
        if self._busy:
            self._stop.set()
        else:
            self._drained.set()

    @property
    def playing(self) -> bool:
        return bool(self._buf) or self._busy

    async def drained(self, extra: float = 0.0) -> None:
        await self._drained.wait()
        if extra > 0:
            await asyncio.sleep(extra)

    def prepare(self) -> None:
        """A turn started: launch the receiver now, so the answer doesn't wait for it."""
        if self.available and (self._prep is None or self._prep.done()):
            self._prep = asyncio.create_task(self._prepare())

    async def _prepare(self) -> None:
        try:
            await self._ensure_app()
        except (CastError, OSError, TimeoutError) as e:
            self._failed(e)

    async def close(self) -> None:
        for t in (self._task, self._prep):
            if t:
                t.cancel()
        ch, app = self._ch, self._app
        if ch and app and not ch.closed.is_set():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ch.stop_app(app.get("sessionId", "")), 3)
        if ch:
            await ch.close()
        self._ch = self._app = None
        self._drained.set()

    def info(self) -> dict:
        return {"id": self.id, "name": self.name, "state": self.state, "error": self.error,
                "host": self.dev.host, "playing": self.playing}

    # -- device -------------------------------------------------------------------------------------------
    def _failed(self, e: Exception) -> None:
        self.error = str(e) or type(e).__name__
        self.state = "unreachable"
        self._retry_at = time.monotonic() + RETRY_S
        self._app = None
        log.warning("cast '%s' (%s): %s - the speaker's mic device plays for %d s", self.name, self.dev.host,
                    self.error, RETRY_S)

    async def _connect(self) -> CastChannel:
        d = self.dev
        try:
            ch = CastChannel(d.host, d.port)
            await ch.open()
            return ch
        except (OSError, TimeoutError) as e:
            first = e
        if d.uuid:                                    # DHCP may have moved it: look for its mDNS id
            f = next((f for f in await discover(2.0) if f.uuid == d.uuid), None)
            if f and (f.host, f.port) != (d.host, d.port):
                log.info("cast '%s' moved %s -> %s", d.name, d.host, f.host)
                if self.on_moved:
                    self.on_moved(d, f.host, f.port)
                ch = CastChannel(f.host, f.port)
                await ch.open()
                return ch
        raise CastError(f"unreachable ({first or type(first).__name__})")

    async def _ensure_app(self) -> tuple[CastChannel, dict]:
        async with self._lock:
            self._last_used = time.monotonic()
            if self._ch is None or self._ch.closed.is_set():
                self.state = "connecting"
                self._app = None
                self._ch = await self._connect()
                self._ch.on_message = self._on_message
            if self._app is None:
                app = await self._ch.launch()
                self._ch.connect_to(app["transportId"])
                self._app = app
            self.state, self.error = "ready", ""
            return self._ch, self._app

    def _on_message(self, ns: str, src: str, p: dict) -> None:
        t = p.get("type")
        if ns == NS_RECEIVER and t == "RECEIVER_STATUS" and self._app:
            apps = (p.get("status") or {}).get("applications") or []
            if not any(a.get("sessionId") == self._app.get("sessionId") for a in apps):
                log.info("cast '%s': the receiver app was closed or replaced", self.name)
                self._app = None
        elif ns == NS_MEDIA and t == "MEDIA_STATUS":
            for st in p.get("status") or []:
                sid = st.get("mediaSessionId")
                if isinstance(sid, int):
                    self._media[sid] = (str(st.get("playerState") or ""), str(st.get("idleReason") or ""))
            self._changed.set()

    # -- playback loop ------------------------------------------------------------------------------------
    async def _run(self) -> None:
        while True:
            if not self._buf:
                if not self._busy:
                    self._drained.set()
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), 30)
                except TimeoutError:
                    await self._idle_stop()
                continue
            wait = SETTLE_S - (time.monotonic() - self._last_add)
            if wait > 0:
                await asyncio.sleep(wait)
                continue
            seg = bytes(self._buf)
            self._buf.clear()
            self._stop.clear()
            self._busy = True
            try:
                await self._play_segment(seg)
            except (CastError, OSError, TimeoutError) as e:
                self._failed(e)
                rest = seg + bytes(self._buf)
                self._buf.clear()
                if self.spill and not self._stop.is_set():
                    self.spill(rest)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("cast '%s' playback failed", self.name)
            finally:
                self._busy = False
                self._last_used = time.monotonic()

    async def _play_segment(self, pcm: bytes) -> None:
        ch, app = await self._ensure_app()
        if self._stop.is_set():
            return
        await self.media.ensure()
        path = self.media.offer(pcmmod.to_wav(pcm), ch.peer_ip)
        try:
            res = await ch.request(NS_MEDIA, app["transportId"], {
                "type": "LOAD", "autoplay": True, "currentTime": 0,
                "media": {"contentId": f"http://{ch.local_ip}:{self.media.port}{path}", "contentType": "audio/wav",
                          "streamType": "BUFFERED", "metadata": {"metadataType": 0, "title": "telerouter"}}}, 10)
            if res.get("type") != "MEDIA_STATUS":
                self._app = None                     # LOAD_FAILED / INVALID_REQUEST: relaunch next time
                raise CastError(f"the device refused the audio ({res.get('type')})")
            sid = next((st.get("mediaSessionId") for st in res.get("status") or []), None)
            deadline = time.monotonic() + len(pcm) / (2 * pcmmod.RATE) + 8
            while True:
                state, reason = self._media.get(sid, ("", "")) if sid is not None else ("", "")
                if state == "IDLE" and reason in DONE:
                    if reason == "ERROR":
                        log.warning("cast '%s': the device could not play the audio", self.name)
                    break
                if self._stop.is_set():
                    with contextlib.suppress(CastError, OSError):
                        ch.notify(NS_MEDIA, app["transportId"], {"type": "STOP", "mediaSessionId": sid})
                    break
                left = deadline - time.monotonic()
                if left <= 0:
                    log.info("cast '%s': no FINISHED from the device - going on", self.name)
                    break
                self._changed.clear()
                stop = asyncio.create_task(self._stop.wait())
                changed = asyncio.create_task(self._changed.wait())
                try:
                    await asyncio.wait({stop, changed}, timeout=min(left, 1.0), return_when=asyncio.FIRST_COMPLETED)
                finally:
                    stop.cancel()
                    changed.cancel()
                if ch.closed.is_set():
                    raise CastError("connection lost")
        finally:
            self.media.withdraw(path)
            self._media = {k: v for k, v in self._media.items() if k == sid}

    async def _idle_stop(self) -> None:
        ch, app = self._ch, self._app
        if not ch or self._busy or time.monotonic() - self._last_used < IDLE_STOP_S:
            return
        async with self._lock:
            if app and not ch.closed.is_set():
                await ch.stop_app(app.get("sessionId", ""))
            await ch.close()
            self._ch = self._app = None
            self.state = "idle"
            log.info("cast '%s': idle - receiver stopped", self.name)
