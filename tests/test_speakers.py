"""Virtual speakers (mic + output from devices), config migration, Google Cast outputs against a fake device."""

import asyncio
import json
import socket
import ssl
import struct

import pytest
from fastapi.testclient import TestClient

from telerouter import tls
from telerouter.media import pcm
from telerouter.models import CastDevice, Speaker
from telerouter.store import Store, migrate_raw
from telerouter.voice import cast
from telerouter.voice.speaker import VirtualSpeaker

from .conftest import free_port
from .test_web import setup_admin


# -- migration -----------------------------------------------------------------------------------------------
def test_old_clients_become_speakers_with_the_same_id(tmp_path):
    old = {"clients": [{"id": "0a0b0c0d", "name": "Raqqa laptop", "room": "raqqa", "token_hash": "x" * 64,
                        "agent_id": "a1", "wake_words": ["hey_jarvis"], "volume": 80, "visible_to_agents": False,
                        "thinking_cue": "off"}],
           "mcp_tokens": [{"name": "t", "clients": ["0a0b0c0d"]}]}
    (tmp_path / "config.json").write_text(json.dumps(old))
    cfg = Store(str(tmp_path)).config
    sp = cfg.speakers[0]
    assert (sp.id, sp.name, sp.room, sp.mic, sp.output) == ("0a0b0c0d", "Raqqa laptop", "raqqa", "0a0b0c0d",
                                                             "client:0a0b0c0d")
    assert (sp.agent_id, sp.wake_words, sp.volume, sp.visible_to_agents, sp.thinking_cue) == \
        ("a1", ["hey_jarvis"], 80, False, "off")
    assert cfg.clients[0].token_hash == "x" * 64
    assert list(tmp_path.glob("config.json.bak-*"))                   # the old file is kept
    assert migrate_raw(json.loads((tmp_path / "config.json").read_text())) is False   # once only


# -- routing -------------------------------------------------------------------------------------------------
class FakeOut:
    def __init__(self, kind, name, live_ok=True, aec=False):
        self.kind, self.name, self.live_ok, self.aec = kind, name, live_ok, aec
        self.got: list[bytes] = []
        self.flushed = 0
        self.mic_level = -50.0
        self.on_mic = None
        self.spill = None

    def play(self, audio):
        self.got.append(audio)

    def flush(self):
        self.flushed += 1

    @property
    def playing(self):
        return False

    async def drained(self, extra=0.0):
        pass

    def prepare(self):
        pass


def test_split_speaker_routes_speech_beeps_and_calls():
    laptop, mi = FakeOut("client", "Raqqa laptop", aec=True), FakeOut("cast", "Mi", live_ok=False)
    vs = VirtualSpeaker(Speaker(name="odam", mic="0000000a", output="cast:0000000b"), laptop, mi)
    vs.play(b"\x10\x00" * 4)
    vs.cue(b"\x20\x00")
    vs.live(b"\x30\x00")
    assert mi.got == [b"\x10\x00" * 4] and laptop.got == [b"\x20\x00", b"\x30\x00"]
    assert mi.spill == laptop.play                       # if the Cast speaker is unreachable, the laptop plays
    assert not vs.aec                                    # the laptop's echo canceller doesn't hear the Mi speaker
    vs.cfg.cues = "output"
    vs.cue(b"\x40\x00")
    assert mi.got[-1] == b"\x40\x00"
    vs.flush()
    assert laptop.flushed == mi.flushed == 1
    vs.cfg.volume = 50
    vs.play(b"\x10\x00")
    assert mi.got[-1] == b"\x08\x00"


def test_classic_speaker_uses_one_device():
    dev = FakeOut("client", "Kitchen", aec=True)
    vs = VirtualSpeaker(Speaker(name="Kitchen", mic="0000000a", output="client:0000000a"), dev, dev)
    vs.play(b"\x01\x00")
    vs.cue(b"\x02\x00")
    vs.live(b"\x03\x00")
    assert len(dev.got) == 3 and vs.aec
    vs.flush()
    assert dev.flushed == 1


def test_output_only_speaker_cannot_listen():
    mi = FakeOut("cast", "Mi", live_ok=False)
    vs = VirtualSpeaker(Speaker(name="hall", output="cast:0000000b"), None, mi)
    assert not vs.has_mic and vs.live_out is None
    vs.cue(b"\x01\x00")
    assert mi.got == [b"\x01\x00"]


# -- Cast protocol -------------------------------------------------------------------------------------------
def test_cast_message_roundtrip():
    frame = cast.encode_message("sender-1", "receiver-0", cast.NS_RECEIVER, '{"type":"GET_STATUS"}' + "é" * 200)
    (n,) = struct.unpack(">I", frame[:4])
    assert n == len(frame) - 4
    m = cast.decode_message(frame[4:])
    assert m == {"source": "sender-1", "dest": "receiver-0", "namespace": cast.NS_RECEIVER,
                 "payload": '{"type":"GET_STATUS"}' + "é" * 200}


def _name(n):
    return b"".join(bytes([len(p)]) + p.encode() for p in n.split(".")) + b"\x00"


def test_parse_mdns_answer():
    txt = b"".join(bytes([len(x)]) + x for x in (b"id=a1b2c3d4", b"fn=Odam", b"md=Mi Smart Speaker Lite"))
    rr = (_name("_googlecast._tcp.local") + struct.pack(">HHIH", 12, 1, 120, 2) + b"\xc0\x0c"
          + _name("Odam._googlecast._tcp.local") + struct.pack(">HHIH", 16, 1, 120, len(txt)) + txt
          + _name("Odam._googlecast._tcp.local") + struct.pack(">HHIH", 33, 1, 120, 8) + struct.pack(">HHH", 0, 0, 8009)
          + b"\xc0\x0c"
          + _name("odam.local") + struct.pack(">HHIH", 1, 1, 120, 4) + socket.inet_aton("192.168.1.38"))
    pkt = struct.pack(">6H", 0, 0x8400, 0, 1, 0, 3) + rr
    f = cast.parse_mdns(pkt, "192.168.1.99")
    assert (f.uuid, f.name, f.model, f.host, f.port) == ("a1b2c3d4", "Odam", "Mi Smart Speaker Lite",
                                                         "192.168.1.38", 8009)
    assert cast.parse_mdns(b"\x00" * 5, "x") is None


class FakeCastDevice:
    """A Cast receiver: TLS :port, Default Media Receiver; fetches LOADed media over HTTP like a real one."""

    def __init__(self, tls_files, speed=4.0):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(tls_files.cert, tls_files.key)
        self.speed = speed                   # plays N times faster than real time
        self.app = None
        self.played: list[bytes] = []
        self.launches = 0
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self._conn, "127.0.0.1", 0, ssl=self.ctx)
        return self.server.sockets[0].getsockname()[1]

    async def _conn(self, r, w):
        sid = 0

        def send(ns, dest, payload):
            w.write(cast.encode_message("receiver-0" if "receiver" in ns else "t1", dest, ns, json.dumps(payload)))

        def status(rid=0):
            send(cast.NS_RECEIVER, "*", {"type": "RECEIVER_STATUS", "requestId": rid,
                                         "status": {"applications": [self.app] if self.app else []}})
        try:
            while True:
                (n,) = struct.unpack(">I", await r.readexactly(4))
                m = cast.decode_message(await r.readexactly(n))
                p = json.loads(m["payload"])
                t, rid = p.get("type"), p.get("requestId", 0)
                if t == "GET_STATUS" and m["namespace"] == cast.NS_RECEIVER:
                    status(rid)
                elif t == "LAUNCH":
                    self.launches += 1
                    self.app = {"appId": p["appId"], "transportId": "t1", "sessionId": "s1"}
                    status(rid)
                elif t == "STOP" and m["namespace"] == cast.NS_RECEIVER:
                    self.app = None
                    status(rid)
                elif t == "LOAD":
                    sid += 1
                    send(cast.NS_MEDIA, m["source"], {"type": "MEDIA_STATUS", "requestId": rid,
                                                      "status": [{"mediaSessionId": sid, "playerState": "IDLE"}]})
                    asyncio.create_task(self._play(send, p["media"]["contentId"], sid))
                elif t == "STOP":
                    send(cast.NS_MEDIA, "*", {"type": "MEDIA_STATUS", "status": [
                        {"mediaSessionId": p.get("mediaSessionId"), "playerState": "IDLE", "idleReason": "CANCELLED"}]})
        except (asyncio.IncompleteReadError, ConnectionError):
            pass

    async def _play(self, send, url, sid):
        host, _, rest = url.removeprefix("http://").partition(":")
        port, _, path = rest.partition("/")
        r, w = await asyncio.open_connection(host, int(port))
        w.write(f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nRange: bytes=0-\r\n\r\n".encode())
        data = await r.read()
        w.close()
        head, _, body = data.partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 200")
        audio, rate = pcm.from_wav(body)
        self.played.append(audio)
        send(cast.NS_MEDIA, "*", {"type": "MEDIA_STATUS", "status": [{"mediaSessionId": sid, "playerState": "PLAYING"}]})
        await asyncio.sleep(len(audio) / 32000 / self.speed)
        send(cast.NS_MEDIA, "*", {"type": "MEDIA_STATUS", "status": [
            {"mediaSessionId": sid, "playerState": "IDLE", "idleReason": "FINISHED"}]})


@pytest.fixture()
def tls_files(tmp_path):
    return tls.ensure(str(tmp_path), [])


async def test_cast_output_plays_each_burst_as_one_file(tls_files):
    dev = FakeCastDevice(tls_files)
    port = await dev.start()
    media = cast.CastMedia("127.0.0.1", free_port(socket.SOCK_STREAM))
    out = cast.CastOutput(CastDevice(name="Mi", host="127.0.0.1", port=port), media)
    try:
        one, two = b"\x01\x00" * 8000, b"\x02\x00" * 4000           # 0.5 s, 0.25 s
        out.play(one[:8000])
        out.play(one[8000:])                       # arrives together: one LOAD
        assert out.playing
        await asyncio.wait_for(out.drained(), 10)
        out.play(two)
        await asyncio.wait_for(out.drained(), 10)
        assert dev.played == [one, two] and dev.launches == 1 and out.state == "ready"
        assert not out.playing and not media._items        # URLs are withdrawn once played
    finally:
        await out.close()
        await media.close()
        dev.server.close()
    assert dev.app is None                         # closing stops the receiver app


async def test_cast_flush_stops_playback(tls_files):
    dev = FakeCastDevice(tls_files, speed=0.5)
    port = await dev.start()
    media = cast.CastMedia("127.0.0.1", free_port(socket.SOCK_STREAM))
    out = cast.CastOutput(CastDevice(name="Mi", host="127.0.0.1", port=port), media)
    try:
        out.play(b"\x01\x00" * 32000)              # 2 s, played at half speed
        while not dev.played:
            await asyncio.sleep(0.05)
        out.flush()
        await asyncio.wait_for(out.drained(), 2)   # long before it would have finished
    finally:
        await out.close()
        await media.close()
        dev.server.close()


async def test_unreachable_cast_hands_audio_to_the_mic_device():
    media = cast.CastMedia("127.0.0.1", free_port(socket.SOCK_STREAM))
    out = cast.CastOutput(CastDevice(name="Mi", host="127.0.0.1", port=free_port(socket.SOCK_STREAM)), media)
    spilled = []
    out.spill = spilled.append
    try:
        out.play(b"\x05\x00" * 100)
        await asyncio.wait_for(out.drained(), 10)
        assert spilled == [b"\x05\x00" * 100] and out.state == "unreachable" and not out.available
        out.play(b"\x06\x00")                      # during the retry pause: straight to the mic device
        assert spilled[-1] == b"\x06\x00"
    finally:
        await out.close()


async def test_cast_media_serves_only_offered_files_to_their_device():
    port = free_port(socket.SOCK_STREAM)
    media = cast.CastMedia("127.0.0.1", port)
    await media.ensure()

    async def get(path):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(f"GET {path} HTTP/1.1\r\n\r\n".encode())
        data = await r.read()
        w.close()
        return data

    try:
        mine = media.offer(b"RIFFdata", "127.0.0.1")
        other = media.offer(b"RIFFdata", "192.168.9.9")
        assert (await get(mine)).endswith(b"\r\n\r\nRIFFdata")
        assert (await get(other)).startswith(b"HTTP/1.1 404")          # offered to another device
        assert (await get("/cast/" + "0" * 32 + ".wav")).startswith(b"HTTP/1.1 404")
        assert (await get("/../etc/passwd")).startswith(b"HTTP/1.1 404")
        media.withdraw(mine)
        assert (await get(mine)).startswith(b"HTTP/1.1 404")
        assert len(mine) == len("/cast/") + 32 + len(".wav")           # 128-bit random path
    finally:
        await media.close()


# -- admin API -----------------------------------------------------------------------------------------------
@pytest.fixture()
def c(settings):
    from telerouter.web.app import create_app
    public, _ = create_app(settings, tls.ensure(settings.data_dir, []))
    with TestClient(public, base_url="https://testserver") as client:
        yield client


def enroll(c, h, name):
    code = c.post("/api/clients/enroll", json={"name": name}, headers=h).json()["code"]
    return c.post("/api/client/enroll", json={"code": code}).json()


def test_speakers_from_devices_via_the_api(c):
    h = {"X-CSRF-Token": setup_admin(c)}
    laptop = enroll(c, h, "Raqqa laptop")
    sps = c.get("/api/speakers").json()["speakers"]
    assert [(s["id"], s["mic"], s["output"]) for s in sps] == \
        [(laptop["client_id"], laptop["client_id"], f"client:{laptop['client_id']}")]   # its own speaker
    mi = c.post("/api/casts", json={"name": "Mi speaker", "host": "192.168.1.38", "uuid": "4123"}, headers=h).json()
    assert c.post("/api/casts", json={"name": "again", "host": "192.168.1.38", "uuid": "4123"},
                  headers=h).status_code == 409
    odam = c.post("/api/speakers", json={"name": "odam", "mic": laptop["client_id"], "output": f"cast:{mi['id']}"},
                  headers=h).json()
    assert odam["mic_moved_from"] == ["Raqqa laptop"]       # a microphone feeds one speaker
    by = {s["name"]: s for s in c.get("/api/speakers").json()["speakers"]}
    assert by["Raqqa laptop"]["mic"] == "" and by["odam"]["mic"] == laptop["client_id"]
    for bad in ({"name": "x", "mic": "deadbeef"}, {"name": "x", "output": "cast:deadbeef"}, {"name": "x"},
                {"name": "x", "output": "http://evil"}):
        assert c.post("/api/speakers", json=bad, headers=h).status_code == 422, bad
    assert c.post("/api/speakers", json={"name": "x", "output": f"cast:{mi['id']}"}).status_code == 403   # CSRF
    # the laptop connects: odam is online (laptop mic + Mi output), "Raqqa laptop" plays only
    with c.websocket_connect("/ws/client", headers={"Authorization": f"Bearer {laptop['token']}"}) as ws:
        ws.receive_text()
        st = {s["name"]: s for s in c.get("/api/status").json()["speakers"]}
        assert st["odam"]["mic"] == "Raqqa laptop" and st["odam"]["output"] == "Mi speaker"
        assert st["Raqqa laptop"]["mic"] == "" and st["Raqqa laptop"]["output"] == "Raqqa laptop"
        assert c.post(f"/api/speakers/{laptop['client_id']}/talk", headers=h).status_code == 409   # no mic
    # removing the Cast speaker leaves odam with the laptop mic only; removing the laptop removes both speakers
    assert c.delete(f"/api/casts/{mi['id']}", headers=h).status_code == 200
    assert {s["name"]: s["output"] for s in c.get("/api/speakers").json()["speakers"]}["odam"] == ""
    assert c.delete(f"/api/clients/{laptop['client_id']}", headers=h).status_code == 200
    assert c.get("/api/speakers").json()["speakers"] == []


async def test_a_call_saying_the_wake_word_does_not_start_a_turn_on_a_speaker():
    from types import SimpleNamespace

    from telerouter.models import Config
    from telerouter.voice.assistant import VoiceAssistant
    calls = []
    eng = SimpleNamespace(store=SimpleNamespace(config=Config()), calls=SimpleNamespace(active=lambda: calls))
    va = VoiceAssistant(eng)
    dev = FakeOut("client", "Raqqa laptop")
    vs = VirtualSpeaker(Speaker(id="0000000a", name="Raqqa", mic="0000000a", output="client:0000000a"), dev, dev)
    started = []
    va.start_turn = lambda conn: started.append(conn.name)
    calls.append(SimpleNamespace(state="active", client_id=""))       # telerouter phoning you (phone_message)
    va._on_wake(vs, "hey_jarvis")
    assert started == []
    calls[0].client_id = vs.id                                         # a call on this very speaker: unchanged
    va._on_wake(vs, "hey_jarvis")
    calls.clear()
    va._on_wake(vs, "hey_jarvis")
    assert started == ["Raqqa", "Raqqa"]
    calls.append(SimpleNamespace(state="active", client_id=""))
    eng.store.config.voice.wake_during_calls = True                    # the owner wants it anyway
    va._on_wake(vs, "hey_jarvis")
    assert len(started) == 3
