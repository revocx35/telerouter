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
