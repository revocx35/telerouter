"""Speaker client pieces that run without sound hardware."""

import hashlib
import ssl
import threading

import numpy as np
import pytest

from telerouter_client.audio import Player, Resampler


def test_resampler_keeps_pitch_and_length():
    for src, dst, f in ((48000, 16000, 440), (16000, 44100, 1000), (44100, 16000, 300)):
        r = Resampler(src, dst)
        t = np.arange(src) / src
        x = np.sin(2 * np.pi * f * t) * 10000
        block = src // 50
        out = np.concatenate([r(x[i:i + block]) for i in range(0, src, block)])
        assert abs(len(out) - dst) <= 2
        spec = np.abs(np.fft.rfft(out[dst // 10: dst - dst // 10]))
        peak = np.argmax(spec) * dst / (len(out) - 2 * (dst // 10))
        assert abs(peak - f) < 5


def test_player_buffer_flush_and_cap():
    p = Player()
    p.feed(b"\x01\x00" * 100)
    assert p.take(100) == b"\x01\x00" * 50
    p.flush()
    assert p.take(10) == bytes(10)
    p.feed(bytes(16000 * 2 * 10))                # 10 s: capped to 4 s
    assert p.queued_s <= 4.0

    def writer():
        for _ in range(1000):
            p.feed(b"\x00\x01" * 160)
    th = threading.Thread(target=writer)
    th.start()
    for _ in range(1000):
        p.take(320)
    th.join()


def test_enrollment_refuses_a_ca_with_the_wrong_fingerprint(monkeypatch, tmp_path):
    from telerouter import tls
    from telerouter_client import net
    files = tls.ensure(str(tmp_path), [])

    class Resp:
        def __init__(self, data):
            self.data = data

        def read(self, n):
            return self.data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(net.urllib.request, "urlopen", lambda *a, **k: Resp(files.ca_pem))
    good = hashlib.sha256(ssl.PEM_cert_to_DER_cert(files.ca_pem.decode())).hexdigest()
    assert net.fetch_ca("https://x:8443", good).startswith("-----BEGIN CERTIFICATE")
    with pytest.raises(SystemExit):
        net.fetch_ca("https://x:8443", "00" * 32)


def test_alsa_backend_streams_blocks_both_ways(tmp_path, monkeypatch):
    import time
    from telerouter_client.audio import AlsaIO, Player
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "arecord").write_text("#!/bin/sh\nwhile true; do head -c 640 /dev/zero; sleep 0.02; done\n")   # silence
    played = tmp_path / "played.raw"
    (bin_ / "aplay").write_text(f"#!/bin/sh\nexec cat > {played}\n")
    for f in bin_.iterdir():
        f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:" + __import__("os").environ["PATH"])
    assert AlsaIO.available()
    got = []
    player = Player()
    player.feed(b"\x01\x02" * 1600)
    io = AlsaIO(got.append, player)
    io._stop.clear()
    io.start()
    time.sleep(0.5)
    io._stop.set()
    io.stop()
    assert got and all(len(b) == 640 for b in got)
    assert played.exists() and played.stat().st_size >= 3200


# -- smart display (client 0.11) ------------------------------------------------------------------------------
def test_display_accepts_only_loopback_addresses():
    from telerouter_client.display import parse_listen
    assert parse_listen("8770") == ("127.0.0.1", 8770)
    assert parse_listen("127.0.0.1:9000") == ("127.0.0.1", 9000)
    assert parse_listen("[::1]:9000") == ("::1", 9000)
    assert parse_listen("localhost:9000") == ("localhost", 9000)
    for bad in ("0.0.0.0:8770", "192.168.1.5:8770", "[::]:8770", "example.com:80", "x", "70000", "0"):
        with pytest.raises(ValueError):
            parse_listen(bad)


async def _http(port: int, raw: bytes, read_until: bytes | None = None, timeout: float = 3.0) -> bytes:
    import asyncio
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(raw)
    await w.drain()
    out = b""
    try:
        while True:
            chunk = await asyncio.wait_for(r.read(65536), timeout)
            if not chunk:
                break
            out += chunk
            if read_until is not None and read_until in out:
                break
    finally:
        w.close()
    return out


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.mark.asyncio
async def test_display_serves_the_page_with_strict_headers_and_refuses_foreign_hosts():
    from telerouter_client.display import Display
    port = _free_port()
    d = Display("127.0.0.1", port)
    await d.start()
    try:
        page = await _http(port, f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        head, _, body = page.partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 200")
        assert b"script-src 'self'" in head and b"frame-ancestors 'none'" in head and b"no-store" in head
        assert b"<script src=\"/display.js\"" in body and b"<script>" not in body and b"style=" not in body
        js = await _http(port, f"GET /display.js HTTP/1.1\r\nHost: localhost:{port}\r\n\r\n".encode())
        assert js.startswith(b"HTTP/1.1 200") and b"innerHTML" not in js
        # DNS rebinding: a page on another name that resolves to 127.0.0.1 must not read transcripts
        evil = await _http(port, f"GET /events HTTP/1.1\r\nHost: evil.example:{port}\r\n\r\n".encode())
        assert evil.startswith(b"HTTP/1.1 403")
        nohost = await _http(port, b"GET / HTTP/1.1\r\n\r\n")
        assert nohost.startswith(b"HTTP/1.1 403")
        post = await _http(port, f"POST / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Length: 0\r\n\r\n".encode())
        assert post.startswith(b"HTTP/1.1 405")
        missing = await _http(port, f"GET /../etc/passwd HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        assert missing.startswith(b"HTTP/1.1 404")
        huge = await _http(port, b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nX: " + b"a" * 20000 + b"\r\n\r\n")
        assert not huge.startswith(b"HTTP/1.1 200")
    finally:
        await d.stop()


@pytest.mark.asyncio
async def test_display_streams_state_and_transcripts_and_wakes_the_screen(tmp_path):
    import asyncio
    import json as _json

    from telerouter_client.display import Display
    port = _free_port()
    marker = tmp_path / "woke"
    d = Display("127.0.0.1", port, on_listen=f"touch {marker}")
    d.set_name("iMac", "Jarvis")
    d.set_connected(True)
    d.add_transcript("user", "earlier question")
    await d.start()
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(f"GET /events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        await w.drain()

        async def event():
            while True:
                line = await asyncio.wait_for(r.readline(), 3)
                if line.startswith(b"data: "):
                    return _json.loads(line[6:])

        snap = await event()
        assert snap["type"] == "snapshot" and snap["name"] == "iMac" and snap["assistant"] == "Jarvis"
        assert snap["connected"] is True and snap["turns"][0]["text"] == "earlier question"
        assert snap["state"] == "idle"                       # connected = idle until the server says more
        d.set_state("listening")
        assert (await event()) ["state"] == "listening"
        d.add_transcript("user", "what's the weather")
        d.add_transcript("assistant", "Sunny, 21 degrees.")
        d.add_transcript("system", "ignored")              # unknown roles are dropped
        ev1, ev2 = await event(), await event()
        assert (ev1["role"], ev1["text"]) == ("user", "what's the weather")
        assert (ev2["role"], ev2["text"]) == ("assistant", "Sunny, 21 degrees.")
        for _ in range(20):
            d.add_transcript("assistant", "x")
        assert len(d.turns) == 6                             # only the last few turns are kept
        d.set_connected(False)
        assert d.state == "offline"
        w.close()
        for _ in range(50):
            if marker.exists():
                break
            await asyncio.sleep(0.05)
        assert marker.exists()                               # --display-on-listen ran once
    finally:
        await d.stop()


@pytest.mark.asyncio
async def test_display_limits_open_event_streams():
    import asyncio

    from telerouter_client import display as dmod
    port = _free_port()
    d = dmod.Display("127.0.0.1", port)
    await d.start()
    conns = []
    try:
        for _ in range(dmod.MAX_STREAMS):
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(f"GET /events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
            await w.drain()
            await asyncio.wait_for(r.readuntil(b"data: "), 3)
            conns.append(w)
        extra = await _http(port, f"GET /events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        assert extra.startswith(b"HTTP/1.1 503")
    finally:
        for w in conns:
            w.close()
        await d.stop()


def test_session_asks_for_transcripts_and_feeds_the_display():
    import asyncio
    import json as _json

    from telerouter_client.display import Display
    from telerouter_client.net import Session

    class WS:
        def __init__(self, frames):
            self.frames = frames
            self.sent = []

        async def send(self, m):
            self.sent.append(m)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.frames:
                raise StopAsyncIteration
            return self.frames.pop(0)

    d = Display("127.0.0.1", 1)
    s = Session({"server": "https://x:8443", "token": "t", "ca_pem": ""}, Player(), aec=False, display=d)
    ws = WS([_json.dumps({"type": "config", "name": "iMac", "assistant": "Jarvis", "volume": 100}),
             _json.dumps({"type": "state", "state": "listening"}),
             _json.dumps({"type": "transcript", "role": "user", "text": "hello"}),
             _json.dumps({"type": "transcript", "role": "assistant", "text": "Hi.", "say": "Hi."}),
             _json.dumps({"type": "state", "state": "idle"})])
    asyncio.run(s._session(ws))
    assert (d.name, d.assistant, d.state) == ("iMac", "Jarvis", "idle")
    assert [t["text"] for t in d.turns] == ["hello", "Hi."]


# -- smart display home screen + VNC viewer (client 0.12) ----------------------------------------------------------
def _cpix(rgb) -> bytes:
    return bytes((int(rgb[2]), int(rgb[1]), int(rgb[0])))            # CPIXEL of our pixel format: B, G, R


def _runlen(n: int) -> bytes:
    n -= 1
    return b"\xff" * (n // 255) + bytes([n % 255])


def _zrle_tile(tile: np.ndarray, kind: str) -> bytes:
    th, tw, _ = tile.shape
    flat = tile.reshape(-1, 3)
    if kind == "raw":
        return b"\x00" + b"".join(_cpix(p) for p in flat)
    if kind == "solid":
        return b"\x01" + _cpix(flat[0])
    pal = []
    for p in map(tuple, flat):
        if p not in pal:
            pal.append(p)
    if kind == "packed":
        bits = 1 if len(pal) == 2 else 2 if len(pal) <= 4 else 4
        out = bytes([len(pal)]) + b"".join(_cpix(p) for p in pal)
        for row in tile:
            acc, nb, rb = 0, 0, bytearray()
            for p in map(tuple, row):
                acc = (acc << bits) | pal.index(p)
                nb += bits
                if nb == 8:
                    rb.append(acc)
                    acc, nb = 0, 0
            if nb:
                rb.append(acc << (8 - nb))
            out += bytes(rb)
        return out
    runs, prev = [], None
    for p in map(tuple, flat):
        if runs and p == prev:
            runs[-1][1] += 1
        else:
            runs.append([p, 1])
        prev = p
    if kind == "rle":
        return b"\x80" + b"".join(_cpix(p) + _runlen(n) for p, n in runs)
    out = bytes([128 + len(pal)]) + b"".join(_cpix(p) for p in pal)        # palette RLE
    for p, n in runs:
        out += bytes([pal.index(p)]) if n == 1 else bytes([128 | pal.index(p)]) + _runlen(n)
    return out


class _FakeVnc:
    """A tiny RFB 3.8 server: VNC auth, then a ZRLE update, a CopyRect, a Raw rect, and a DesktopSize."""

    def __init__(self, password="secret", version=b"RFB 003.008\n"):
        import zlib
        self.password, self.version, self.z = password, version, zlib.compressobj()
        self.requests, self.shared, self.server = [], None, None
        rng = np.random.default_rng(1)
        self.img = np.zeros((70, 130, 3), np.uint8)
        self.img[:64, :64] = rng.integers(0, 255, (64, 64, 3), dtype=np.uint8)           # raw
        self.img[:64, 64:128] = (10, 200, 30)                                           # solid
        self.img[:64, 128:] = [[(255, 0, 0) if (x + y) % 2 else (0, 0, 255) for x in range(2)] for y in range(64)]
        self.img[64:, :64] = [[(x % 13 * 19, 7, 9) for x in range(64)] for _ in range(6)]  # packed (4 bpp)
        self.img[64:, 64:128] = [[(1, 2, 3) if x < 30 else (200, 100, 50) for x in range(64)] for _ in range(6)]
        self.img[64:, 128:] = [[(5, 5, 5)] * 2, [(9, 9, 9)] * 2] * 3

    def zrle(self, x, y, w, h) -> bytes:
        import zlib
        kinds = {(0, 0): "raw", (64, 0): "solid", (128, 0): "palrle", (0, 64): "packed", (64, 64): "rle",
                 (128, 64): "packed"}
        data = b""
        for ty in range(y, y + h, 64):
            for tx in range(x, x + w, 64):
                data += _zrle_tile(self.img[ty:min(ty + 64, y + h), tx:min(tx + 64, x + w)], kinds[(tx, ty)])
        comp = self.z.compress(data) + self.z.flush(zlib.Z_SYNC_FLUSH)
        return struct.pack(">HHHHiI", x, y, w, h, 16, len(comp)) + comp

    async def handle(self, r, w):
        import asyncio
        from telerouter_client.vnc import vnc_auth_response
        w.write(self.version)
        await r.readexactly(12)
        w.write(b"\x01\x02")                                  # one security type: VNC auth
        assert (await r.readexactly(1)) == b"\x02"
        ch = bytes(range(16))
        w.write(ch)
        ok = (await r.readexactly(16)) == vnc_auth_response(self.password, ch)
        if not ok:
            w.write(struct.pack(">II", 1, 14) + b"Authentication")
            await w.drain()
            w.close()
            return
        w.write(struct.pack(">I", 0))
        self.shared = await r.readexactly(1)
        w.write(struct.pack(">HH", 130, 70) + bytes(16) + struct.pack(">I", 4) + b"cams")
        assert (await r.readexactly(20))[0] == 0                      # SetPixelFormat
        hdr = await r.readexactly(4)
        await r.readexactly(4 * struct.unpack(">H", hdr[2:])[0])      # SetEncodings
        self.requests.append(await r.readexactly(10))
        w.write(struct.pack(">BxH", 0, 1) + self.zrle(0, 0, 130, 70))
        await w.drain()
        self.requests.append(await r.readexactly(10))
        raw = np.zeros((2, 3, 4), np.uint8)
        raw[..., 0], raw[..., 1], raw[..., 2] = 11, 22, 33               # B, G, R, X
        w.write(struct.pack(">BxH", 0, 2) + struct.pack(">HHHHiHH", 100, 66, 4, 4, 1, 0, 0)
                + struct.pack(">HHHHi", 0, 0, 3, 2, 0) + raw.tobytes())
        await w.drain()
        self.requests.append(await r.readexactly(10))
        w.write(struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 0, 0, 200, 100, -223))
        await w.drain()
        try:
            await asyncio.sleep(30)
        finally:
            w.close()

    async def start(self):
        import asyncio
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]


import struct  # noqa: E402  (used by the fake VNC server above)


def _png_decode(png: bytes) -> np.ndarray:
    import zlib
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    i, idat, w, h = 8, b"", 0, 0
    while i < len(png):
        n, kind = struct.unpack(">I4s", png[i:i + 8])
        data = png[i + 8:i + 8 + n]
        assert struct.unpack(">I", png[i + 8 + n:i + 12 + n])[0] == zlib.crc32(kind + data)
        if kind == b"IHDR":
            w, h = struct.unpack(">II", data[:8])
        elif kind == b"IDAT":
            idat += data
        i += 12 + n
    raw = np.frombuffer(zlib.decompress(idat), np.uint8).reshape(h, w * 3 + 1)
    out = np.zeros((h, w * 3), np.uint8)
    for y in range(h):
        f, row = raw[y, 0], raw[y, 1:]
        assert f in (0, 2)
        out[y] = row if f == 0 or y == 0 else row + out[y - 1]
    return out.reshape(h, w, 3)


def test_vnc_auth_and_png():
    from telerouter_client.vnc import des_encrypt, encode_png, vnc_auth_response
    assert des_encrypt(bytes.fromhex("133457799BBCDFF1"), bytes.fromhex("0123456789ABCDEF")).hex() == "85e813540f0ab405"
    # the RFB spec's key twist: each password byte is bit-reversed (checked against a real VNC server's answer)
    assert len(vnc_auth_response("pw", bytes(16))) == 16
    assert vnc_auth_response("pw", bytes(16)) != vnc_auth_response("px", bytes(16))
    img = np.random.default_rng(3).integers(0, 255, (37, 53, 3), dtype=np.uint8)
    assert (_png_decode(encode_png(img)) == img).all()


@pytest.mark.asyncio
async def test_vnc_viewer_decodes_zrle_raw_copyrect_and_resizes():
    import asyncio

    from telerouter_client.vnc import VncViewer
    srv = _FakeVnc()
    port = await srv.start()
    v = VncViewer("127.0.0.1", port, "secret", fps=10)
    v.start()
    try:
        for _ in range(100):
            if v.seq >= 1:
                break
            await asyncio.sleep(0.02)
        assert v.status == "connected" and v.name == "cams" and srv.shared == b"\x01"     # shared session
        assert srv.requests[0][1] == 0                                # first request: the whole screen
        assert (v.fb == srv.img).all()                                # every ZRLE tile type decoded
        seq, png = await v.png()
        assert (_png_decode(png) == srv.img).all()
        for _ in range(100):
            if v.seq >= 2:
                break
            await asyncio.sleep(0.02)
        assert srv.requests[1][1] == 1                                # then incremental updates
        assert (v.fb[66:70, 100:104] == srv.img[0:4, 0:4]).all()      # CopyRect
        assert (v.fb[0:2, 0:3] == (33, 22, 11)).all()                 # Raw (BGRX -> RGB)
        for _ in range(100):
            if v.seq >= 3:
                break
            await asyncio.sleep(0.02)
        assert v.fb.shape == (100, 200, 3)                            # DesktopSize
        assert (await v.png())[0] == v.seq
    finally:
        v.stop()
        srv.server.close()


@pytest.mark.asyncio
async def test_vnc_viewer_reports_a_wrong_password_and_bad_data():
    import asyncio
    import zlib

    from telerouter_client.vnc import VncError, VncViewer
    srv = _FakeVnc(password="right")
    port = await srv.start()
    v = VncViewer("127.0.0.1", port, "wrong")
    v.start()
    try:
        for _ in range(100):
            if v.status == "error":
                break
            await asyncio.sleep(0.02)
        assert v.status == "error" and "wrong password" in v.error
    finally:
        v.stop()
        srv.server.close()
    # malformed ZRLE: a run past its tile, an unknown tile type, a zlib bomb
    v = VncViewer("x", 1)
    v._resize(64, 64)
    for tile in (b"\x80" + b"\x01\x02\x03" + b"\xff" * 20 + b"\x00", b"\x80\x01", b"\x11"):
        v._z = zlib.decompressobj()
        z = zlib.compressobj()
        with pytest.raises(VncError):
            v.zrle(0, 0, 64, 64, z.compress(tile) + z.flush(zlib.Z_SYNC_FLUSH))
    v._z = zlib.decompressobj()
    with pytest.raises(VncError):
        v.zrle(0, 0, 4, 4, zlib.compress(b"\x00" * 10_000_000))


@pytest.mark.asyncio
async def test_display_home_screen_keeps_secrets_allows_only_its_frames_and_serves_vnc():
    import asyncio
    import json as _json

    from telerouter_client.display import Display, clean_screen, frame_origin
    assert frame_origin("https://ha.lan:8123/lovelace/0") == "https://ha.lan:8123"
    for bad in ("javascript:alert(1)", "https://user:pw@x/", "data:text/html,x", "https://a b/", "ftp://x/"):
        assert frame_origin(bad) == ""
    assert clean_screen({"panels": [{"id": "x", "kind": "web", "url": "javascript:1"},
                                    {"id": "<y>", "kind": "task", "task_id": "t"},
                                    {"id": "z", "kind": "shell"}]})["panels"] == []
    srv = _FakeVnc()
    vport = await srv.start()
    port = _free_port()
    d = Display("127.0.0.1", port)
    sizes = []
    d.on_size = lambda w, h: sizes.append((w, h))
    await d.start()
    msg = {"type": "display", "font_family": 'Georgia, "DejaVu Serif", serif',
           "screen": {"background": "#101010", "clock": {"x": 20, "y": 10, "size": 9},
                      "panels": [{"id": "cam", "kind": "vnc", "host": "127.0.0.1", "port": vport, "password": "secret",
                                  "fps": 10, "x": 50, "y": 0, "w": 50, "h": 50},
                                 {"id": "ha", "kind": "web", "url": "https://ha.lan:8123/lovelace/0"},
                                 {"id": "mail", "kind": "task", "task_id": "t1"}]},
           "tasks": {"t1": {"name": "Mail summary", "status": "ok", "ended": 1.0, "text": "- 3 new mails"}}}
    d.set_screen(msg)
    assert not d.vnc                                         # no page open: no VNC connection
    try:
        page = await _http(port, f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        assert b"frame-src https://ha.lan:8123;" in page.partition(b"\r\n\r\n")[0]
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(f"GET /events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        await w.drain()
        raw = await asyncio.wait_for(r.readuntil(b"\n\n"), 3)
        raw = await asyncio.wait_for(r.readuntil(b"\n\n"), 3) if b"data: " not in raw else raw
        snap = _json.loads(raw.split(b"data: ", 1)[1])
        assert b"secret" not in raw and snap["screen"]["panels"][0]["id"] == "cam"
        assert snap["tasks"]["t1"]["text"] == "- 3 new mails" and snap["screen"]["clock"]["x"] == 20
        assert "cam" in d.vnc                                # a page is open: the viewer runs
        got = b""
        for _ in range(100):
            got = await _http(port, f"GET /vnc/cam HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
            if got.startswith(b"HTTP/1.1 200"):
                break
            assert got.startswith(b"HTTP/1.1 503")
            await asyncio.sleep(0.03)
        head, _, png = got.partition(b"\r\n\r\n")
        assert b"Content-Type: image/png" in head and _png_decode(png).shape[2] == 3
        seq = head.split(b"X-Seq: ")[1].split(b"\r\n")[0].decode()
        same = await _http(port, f"GET /vnc/cam?since={seq} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        assert same.startswith(b"HTTP/1.1 204") or same.startswith(b"HTTP/1.1 200")
        nope = await _http(port, f"GET /vnc/other HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        assert nope.startswith(b"HTTP/1.1 404")
        foreign = await _http(port, f"GET /vnc/cam HTTP/1.1\r\nHost: evil.example:{port}\r\n\r\n".encode())
        assert foreign.startswith(b"HTTP/1.1 403")
        ok = await _http(port, f"GET /size?w=1920&h=1080 HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        await _http(port, f"GET /size?w=1920&h=1080 HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        await _http(port, f"GET /size?w=5&h=x HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        assert ok.startswith(b"HTTP/1.1 204") and sizes == [(1920, 1080)]
        # the section goes away: its viewer stops
        d.set_screen({"type": "display", "screen": {"panels": []}, "tasks": {}})
        assert not d.vnc
        w.close()
    finally:
        await d.stop()
        srv.server.close()


def test_session_asks_for_the_home_screen_and_passes_it_on():
    import asyncio
    import json as _json

    from telerouter_client.display import Display
    from telerouter_client.net import Session

    class WS:
        def __init__(self, frames):
            self.frames, self.sent = frames, []

        async def send(self, m):
            self.sent.append(m)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.frames:
                raise StopAsyncIteration
            return self.frames.pop(0)

    d = Display("127.0.0.1", 1)
    d.size = (1280, 800)
    s = Session({"server": "https://x:8443", "token": "t", "ca_pem": ""}, Player(), aec=False, display=d)
    ws = WS([_json.dumps({"type": "display", "screen": {"panels": [{"id": "c", "kind": "vnc", "host": "h",
                                                                     "password": "pw"}]}, "tasks": {}})])
    asyncio.run(s._session(ws))
    assert d.screen["panels"][0]["id"] == "c" and "password" not in d.screen["panels"][0]
    assert d._vnc_cfg["c"][2] == "pw"
    assert _json.loads(ws.sent[0]) == {"type": "display_size", "width": 1280, "height": 800}
