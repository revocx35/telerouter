"""Smart display: a full-screen page that shows the conversation on this speaker.

`telerouter-client run --display 8770` serves a small page on 127.0.0.1:8770 for a kiosk browser on the same
machine (e.g. a wall screen): a clock while idle, and during a turn what was heard ("You") and what the agent
answered, with the listening / thinking / speaking state - like the phone app's assistant sheet.

The data comes from the speaker's own WebSocket session (hello `"transcript": true`, the server's `state` and
`transcript` messages); nothing else is fetched. The listener is loopback-only and read-only:

* it binds only to a loopback address (transcripts are private: no plain-HTTP LAN listener),
* the Host header must name that loopback address (DNS rebinding: a web page elsewhere can't read it),
* GET/HEAD only, 8 KiB request head, 5 s to send it, at most 8 open connections / 4 event streams,
* strict CSP (no inline script or style), the page builds its DOM with textContent only.

Home screen (client 0.12, server 0.23): the server sends the screen's settings and layout (`display` message:
clock position/size/font, sections showing a website, a VNC server or a scheduled task's last summary). Website
sections are frames: only their origins are allowed (`frame-src`), sandboxed, no referrer. VNC and camera (RTSP)
sections are viewed by this client itself (vnc.py, camera.py) and handed to the page as PNG/JPEG pictures on
`/feed/<section id>`; their credentials never reach the page. The VNC
password never reaches the page. The page reports its size on `/size` so the editor in the web UI can match it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import shlex
import subprocess
import time
import re
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("telerouter_client")

MAX_HEAD = 8192
MAX_CONNS = 8
MAX_STREAMS = 4
KEEP_TURNS = 6
KEEPALIVE_S = 15.0
ON_LISTEN_MIN_GAP_S = 5.0
MAX_PANELS = 12
PANEL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]{1,300}$")


def frame_origin(url: str) -> str:
    """The origin of a website section's address for frame-src ("" if it is not a plain http(s) URL)."""
    try:
        u = urlsplit(str(url))
    except ValueError:
        return ""
    if u.scheme not in ("http", "https") or not u.hostname or "@" in u.netloc:
        return ""
    origin = f"{u.scheme}://{u.netloc}"
    return origin if ORIGIN_RE.match(origin) else ""


def _num(v, lo: float, hi: float, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if f != f else min(hi, max(lo, f))


def clean_screen(sc) -> dict | None:
    """What the page may see of the server's screen: no VNC/camera credentials; numbers clamped, kinds known."""
    if not isinstance(sc, dict):
        return None
    clock = sc.get("clock") if isinstance(sc.get("clock"), dict) else {}
    out = {
        "background": str(sc.get("background") or "#05080f")[:7], "accent": str(sc.get("accent") or "#5aa2ff")[:7],
        "header": sc.get("header") is not False, "conversation": sc.get("conversation") is not False,
        "idle_after_s": _num(sc.get("idle_after_s"), 5, 3600, 45),
        "clock": {"show": clock.get("show") is not False, "x": _num(clock.get("x"), 0, 100, 50),
                  "y": _num(clock.get("y"), 0, 100, 45), "size": _num(clock.get("size"), 2, 60, 22),
                  "weight": int(_num(clock.get("weight"), 100, 900, 200)),
                  "color": str(clock.get("color") or "#e8eef8")[:7], "seconds": clock.get("seconds") is True,
                  "hour12": clock.get("hour12") is True, "date": clock.get("date") is not False},
        "panels": [],
    }
    for p in (sc.get("panels") or [])[:MAX_PANELS]:
        if not isinstance(p, dict) or p.get("kind") not in ("web", "vnc", "rtsp", "task"):
            continue
        pid = str(p.get("id") or "")
        if not PANEL_ID_RE.match(pid):
            continue
        q = {"id": pid, "kind": p["kind"], "title": str(p.get("title") or "")[:64],
             "x": _num(p.get("x"), 0, 100, 0), "y": _num(p.get("y"), 0, 100, 0),
             "w": _num(p.get("w"), 3, 100, 30), "h": _num(p.get("h"), 3, 100, 30),
             "font_size": _num(p.get("font_size"), 0.8, 10, 2.4)}
        if p["kind"] == "web":
            if not frame_origin(p.get("url") or ""):
                continue
            q.update(url=str(p["url"])[:2000], refresh_minutes=_num(p.get("refresh_minutes"), 0, 1440, 0),
                     zoom=_num(p.get("zoom"), 25, 400, 100))
        elif p["kind"] in ("vnc", "rtsp"):
            q.update(fps=_num(p.get("fps"), 0.2, 10, 2), fit=p.get("fit") if p.get("fit") in ("contain", "cover", "fill")
                     else "contain")
        else:
            q.update(task_id=str(p.get("task_id") or "")[:32])
        out["panels"].append(q)
    return out


def loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_listen(spec: str) -> tuple[str, int]:
    """"8770" | "127.0.0.1:8770" | "[::1]:8770" -> (host, port). Only loopback hosts are accepted."""
    spec = str(spec).strip()
    host = "127.0.0.1"
    if spec.startswith("["):
        host, _, rest = spec[1:].partition("]")
        port = rest.lstrip(":")
    elif spec.count(":") == 1:
        host, port = spec.split(":")
    else:
        port = spec
    try:
        p = int(port)
    except ValueError:
        raise ValueError(f"display: bad port in {spec!r}") from None
    if not 1 <= p <= 65535:
        raise ValueError(f"display: bad port in {spec!r}")
    if not loopback(host):
        raise ValueError("display: only a loopback address is allowed (the page shows private transcripts)")
    return host, p


class Display:
    """Holds what the screen shows and pushes changes to the open pages (Server-Sent Events)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8770, on_listen: str = ""):
        self.host, self.port = host, port
        self.on_listen = shlex.split(on_listen) if on_listen else []
        self._last_on_listen = 0.0
        self.name = ""
        self.assistant = ""
        self.connected = False
        self.state = "offline"
        self.turns: list[dict] = []
        self.partial = ""                     # the words so far while the person speaks
        self._streams: set[asyncio.Queue] = set()
        self._conns = 0
        self._server: asyncio.AbstractServer | None = None
        self.screen: dict | None = None          # what the page shows on the home screen (no secrets)
        self.font_family = ""
        self.tasks: dict = {}
        self._feed_cfg: dict[str, tuple] = {}    # section id -> viewer key (with credentials): stays in this process
        self.feeds: dict = {}                    # section id -> VncViewer / RtspViewer (only while a page is open)
        self.size: tuple[int, int] | None = None
        self.on_size = None                      # callback(width, height): the session tells the server
        names = {host, "localhost", "127.0.0.1", "[::1]"} if loopback(host) else {host}
        self._hosts = {f"{n}:{port}" for n in names}

    # -- what the session reports -----------------------------------------------------------------
    def snapshot(self) -> dict:
        return {"type": "snapshot", "name": self.name, "assistant": self.assistant, "connected": self.connected,
                "state": self.state, "turns": self.turns, "partial": self.partial, "screen": self.screen, "font_family": self.font_family,
                "tasks": self.tasks, "frames": self.frame_origins()}

    def set_screen(self, msg: dict) -> None:
        """The server's `display` message: settings + home-screen layout (+ VNC/camera credentials, kept here)."""
        sc = msg.get("screen") if isinstance(msg, dict) else None
        self.screen = clean_screen(sc)
        ff = str(msg.get("font_family") or "")[:200]
        self.font_family = ff if re.match(r"^[A-Za-z0-9 ,\"'._-]*$", ff) else ""
        tasks = msg.get("tasks") if isinstance(msg.get("tasks"), dict) else {}
        self.tasks = {str(k)[:32]: {"name": str(v.get("name") or "")[:80], "status": str(v.get("status") or "")[:20],
                                    "ended": v.get("ended") if isinstance(v.get("ended"), (int, float)) else None,
                                    "text": str(v.get("text") or "")[:6000], "error": str(v.get("error") or "")[:300],
                                    "running": v.get("running") is True}
                      for k, v in list(tasks.items())[:MAX_PANELS] if isinstance(v, dict)}
        cfg = {}
        for p in (sc.get("panels") or [])[:MAX_PANELS] if isinstance(sc, dict) else []:
            if not isinstance(p, dict) or not PANEL_ID_RE.match(str(p.get("id") or "")):
                continue
            fps = _num(p.get("fps"), 0.2, 10, 2)
            if p.get("kind") == "vnc" and p.get("host"):
                cfg[str(p["id"])] = ("vnc", str(p["host"])[:253], int(_num(p.get("port"), 1, 65535, 5900)),
                                     str(p.get("password") or "")[:64], fps)
            elif p.get("kind") == "rtsp" and re.match(r"^rtsps?://", str(p.get("rtsp_url") or "")):
                cfg[str(p["id"])] = ("rtsp", str(p["rtsp_url"])[:1300], str(p.get("username") or "")[:128],
                                     str(p.get("password") or "")[:256], fps)
        self._feed_cfg = cfg
        self._sync_feeds()
        self._push({"type": "screen", "screen": self.screen, "font_family": self.font_family, "tasks": self.tasks,
                    "frames": self.frame_origins()})

    def _sync_feeds(self) -> None:
        """Run a viewer per VNC/camera section while a page is open; stop the others."""
        want = self._feed_cfg if self._streams else {}
        for pid in list(self.feeds):
            if pid not in want or self.feeds[pid].key() != want[pid]:
                self.feeds.pop(pid).stop()
        if not want:
            return
        from .camera import RtspViewer
        from .vnc import VncViewer
        for pid, k in want.items():
            if pid not in self.feeds:
                v = VncViewer(*k[1:]) if k[0] == "vnc" else RtspViewer(*k[1:])
                self.feeds[pid] = v
                try:
                    v.start()
                except RuntimeError:                 # no running loop (unit tests that set a screen synchronously)
                    pass

    def frame_origins(self) -> list[str]:
        out = []
        for p in (self.screen or {}).get("panels", []):
            o = frame_origin(p.get("url") or "") if p.get("kind") == "web" else ""
            if o and o not in out:
                out.append(o)
        return out

    def set_name(self, name: str, assistant: str = "") -> None:
        name, assistant = str(name or "")[:80], str(assistant or "")[:80]
        if (name, assistant) != (self.name, self.assistant):
            self.name, self.assistant = name, assistant
            self._push({"type": "name", "name": name, "assistant": assistant})

    def set_connected(self, up: bool) -> None:
        if up != self.connected:
            self.connected = up
            # the server sends `state` only when it changes, so a fresh connection starts out idle
            self.state = "idle" if up else "offline"
            self._push({"type": "connected", "connected": up, "state": self.state})

    def set_state(self, state: str) -> None:
        state = str(state or "")[:20]
        if state == self.state:
            return
        self.state = state
        if state == "listening":
            self._wake_screen()
        if state in ("idle", "offline"):
            self.partial = ""
        self._push({"type": "state", "state": state, "t": time.time()})

    def set_partial(self, text: str) -> None:
        """The words so far while the person speaks (replaced by the "user" transcript)."""
        if self.state != "listening":
            return
        text = str(text or "").strip()[:4000]
        if text != self.partial:
            self.partial = text
            self._push({"type": "partial", "text": text})

    def add_transcript(self, role: str, text: str) -> None:
        if role not in ("user", "assistant"):
            return
        text = str(text or "").strip()[:4000]
        if not text:
            return
        turn = {"role": role, "text": text, "t": time.time()}
        if role == "user":
            self.partial = ""
        self.turns = (self.turns + [turn])[-KEEP_TURNS:]
        self._push({"type": "transcript", **turn})

    def _wake_screen(self) -> None:
        """Run the --display-on-listen command (e.g. switch the panel on) when a turn starts listening."""
        if not self.on_listen:
            return
        now = time.monotonic()
        if now - self._last_on_listen < ON_LISTEN_MIN_GAP_S:
            return
        self._last_on_listen = now
        try:
            subprocess.Popen(self.on_listen, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            log.warning("display: --display-on-listen failed: %s", e)

    def _push(self, ev: dict) -> None:
        for q in list(self._streams):
            if q.full():                       # a stalled page: end its stream, it reconnects and gets a snapshot
                self._streams.discard(q)
                while not q.empty():
                    q.get_nowait()
                q.put_nowait(None)
                continue
            q.put_nowait(ev)

    # -- HTTP --------------------------------------------------------------------------------------
    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port, limit=MAX_HEAD)
        log.info("smart display on http://%s:%d/", self.host if ":" not in self.host else f"[{self.host}]",
                 self.port)

    async def stop(self) -> None:
        for v in list(self.feeds.values()):
            v.stop()
        self.feeds.clear()
        if self._server:
            self._server.close()
            for q in list(self._streams):
                self._streams.discard(q)
                while not q.empty():
                    q.get_nowait()
                q.put_nowait(None)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._conns >= MAX_CONNS:
            writer.close()
            return
        self._conns += 1
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5.0)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
                return
            lines = head.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ")
            if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
                return await self._send(writer, 400, b"bad request")
            method, target = parts[0], parts[1]
            headers = {}
            for ln in lines[1:]:
                k, sep, v = ln.partition(":")
                if sep:
                    headers[k.strip().lower()] = v.strip()
            if headers.get("host", "") not in self._hosts:
                return await self._send(writer, 403, b"forbidden")
            if method not in ("GET", "HEAD"):
                return await self._send(writer, 405, b"method not allowed", extra={"Allow": "GET, HEAD"})
            path, _, query = target.partition("?")
            if path == "/events" and method == "GET":
                return await self._events(writer)
            if path.startswith(("/feed/", "/vnc/")):
                return await self._feed(writer, path.split("/", 2)[2], query, method == "HEAD")
            if path == "/size":
                q = parse_qs(query[:200])
                self._report_size(q.get("w", [""])[0], q.get("h", [""])[0])
                return await self._send(writer, 204, b"")
            asset = ASSETS.get(path)
            if asset is None:
                return await self._send(writer, 404, b"not found")
            ctype, body = asset
            extra = None
            if path == "/" and self.frame_origins():
                extra = {"Content-Security-Policy": self._csp(" ".join(self.frame_origins()))}
            await self._send(writer, 200, body, ctype, head_only=method == "HEAD", extra=extra)
        finally:
            self._conns -= 1
            try:
                writer.close()
            except Exception:
                pass

    def _report_size(self, w: str, h: str) -> None:
        try:
            size = (int(w), int(h))
        except ValueError:
            return
        if not all(100 <= v <= 16384 for v in size) or size == self.size:
            return
        self.size = size
        if self.on_size:
            try:
                self.on_size(*size)
            except Exception:
                log.exception("display: could not report the size")

    async def _feed(self, writer, pid: str, query: str, head_only: bool) -> None:
        v = self.feeds.get(pid)
        if v is None:
            return await self._send(writer, 404, b"not found")
        st = {"X-Feed-Status": v.status}
        if v.fb is None or v.seq == 0:
            body = json.dumps({"status": v.status, "error": v.error}).encode()
            return await self._send(writer, 503, body, "application/json", head_only, st)
        since = parse_qs(query[:100]).get("since", [""])[0]
        if since == str(v.seq):
            return await self._send(writer, 204, b"", extra={**st, "X-Seq": str(v.seq)})
        seq, img, ctype = await v.image()
        await self._send(writer, 200, img, ctype, head_only, {**st, "X-Seq": str(seq)})

    @staticmethod
    def _csp(frames: str = "") -> str:
        return ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' blob:; "
                + (f"frame-src {frames}; " if frames else "")
                + "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

    @staticmethod
    def _headers(status: int, ctype: str, length: int | None, extra: dict | None = None) -> bytes:
        reason = {200: "OK", 204: "No Content", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
                  405: "Method Not Allowed", 503: "Service Unavailable"}.get(status, "OK")
        h = {
            "Content-Type": ctype,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": Display._csp(),
            "Connection": "close",
        }
        if length is not None:
            h["Content-Length"] = str(length)
        h.update(extra or {})
        return (f"HTTP/1.1 {status} {reason}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items())
                + "\r\n").encode()

    async def _send(self, writer, status: int, body: bytes, ctype: str = "text/plain; charset=utf-8",
                    head_only: bool = False, extra: dict | None = None) -> None:
        try:
            writer.write(self._headers(status, ctype, len(body), extra) + (b"" if head_only else body))
            await writer.drain()
        except (ConnectionError, RuntimeError):
            pass

    async def _events(self, writer) -> None:
        if len(self._streams) >= MAX_STREAMS:
            return await self._send(writer, 503, b"too many displays")
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._streams.add(q)
        self._sync_feeds()
        try:
            writer.write(self._headers(200, "text/event-stream; charset=utf-8", None))
            writer.write(b"retry: 2000\n\n" + _sse(self.snapshot()))
            await writer.drain()
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), KEEPALIVE_S)
                except asyncio.TimeoutError:
                    writer.write(b": keep-alive\n\n")
                    await writer.drain()
                    continue
                if ev is None:
                    return
                writer.write(_sse(ev))
                await writer.drain()
        except (ConnectionError, RuntimeError):
            return
        finally:
            self._streams.discard(q)
            self._sync_feeds()


def _sse(ev: dict) -> bytes:
    return b"data: " + json.dumps(ev, separators=(",", ":")).encode() + b"\n\n"


# -- the page (no build step, no inline script/style) ------------------------------------------------------
PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>telerouter display</title>
<link rel="stylesheet" href="/display.css">
<script src="/display.js" defer></script>
</head>
<body class="idle">
<header id="header"><span id="dot" class="dot"></span><span id="name">telerouter</span></header>
<main>
  <section id="home">
    <div id="panels"></div>
    <div id="clock" aria-live="off"><div id="time"></div><div id="date"></div></div>
  </section>
  <section id="talk" aria-live="polite">
    <div id="orb" class="orb"><span></span><span></span><span></span></div>
    <div id="status"></div>
    <div id="turns"></div>
  </section>
</main>
</body>
</html>
"""

CSS = """
:root { --bg:#05080f; --fg:#e8eef8; --dim:#7c8aa3; --you:#1d2a44; --bot:#0f1a2c; --accent:#5aa2ff;
        --listen:#5aa2ff; --think:#f4b740; --speak:#3ddc97; --off:#e5484d; }
* { box-sizing:border-box; margin:0; padding:0; }
html, body { height:100%; background:var(--bg); color:var(--fg); overflow:hidden; cursor:none;
  font-family:"Inter","Segoe UI",system-ui,-apple-system,sans-serif; }
header { position:fixed; top:2.2vh; left:2.5vw; display:flex; align-items:center; gap:.7em;
  font-size:2.1vh; color:var(--dim); letter-spacing:.04em; z-index:3; }
header[hidden] { display:none; }
.dot { width:1.1vh; height:1.1vh; border-radius:50%; background:var(--off); }
.dot.on { background:var(--speak); }
main { height:100%; position:relative; }
#home { position:absolute; inset:0; transition:opacity .8s ease; }
body.active #home { opacity:0; }
#clock { position:absolute; left:50%; top:45%; transform:translate(-50%,-50%); text-align:center; white-space:nowrap;
  z-index:2; }
#clock[hidden] { display:none; }
#clock { font-size:22vh; }
#time { font-size:1em; font-weight:200; letter-spacing:-.02em; line-height:1; font-variant-numeric:tabular-nums; }
#date { margin-top:.11em; font-size:.18em; color:var(--dim); font-weight:300; letter-spacing:0; }
#panels { position:absolute; inset:0; }
.panel { position:absolute; display:flex; flex-direction:column; overflow:hidden; border-radius:1.4vh;
  background:rgba(255,255,255,.035); border:1px solid rgba(255,255,255,.07); }
.panel > h3 { flex:none; display:flex; gap:1em; justify-content:space-between; align-items:baseline;
  padding:1vh 1.6vh .6vh; font-size:1.8vh; font-weight:500; color:var(--dim); letter-spacing:.08em;
  text-transform:uppercase; white-space:nowrap; overflow:hidden; }
.panel > h3 small { font-size:1.5vh; letter-spacing:.02em; text-transform:none; font-weight:400; }
.panel .body { flex:1; position:relative; overflow:hidden; min-height:0; }
.panel iframe { position:absolute; left:0; top:0; width:100%; height:100%; border:0; transform-origin:0 0;
  background:#fff; }
.panel img.feed { position:absolute; inset:0; width:100%; height:100%; object-fit:contain; display:block; }
.panel img.fit-cover { object-fit:cover; }
.panel img.fit-fill { object-fit:fill; }
.note { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; text-align:center;
  padding:2vh; color:var(--dim); font-size:2vh; }
.note[hidden], .badge[hidden] { display:none; }
.badge { position:absolute; right:1vh; bottom:1vh; padding:.4vh 1vh; border-radius:.8vh; font-size:1.5vh;
  background:rgba(0,0,0,.6); color:var(--think); }
.task .text { position:absolute; inset:0; padding:.4vh 1.6vh 1.6vh; overflow:hidden; line-height:1.4;
  overflow-wrap:anywhere; }
.task .h { font-weight:600; margin:.7em 0 .25em; color:var(--fg); }
.task .h:first-child { margin-top:0; }
.task .p { margin:.2em 0; }
.task .li { margin:.15em 0; padding-left:1.1em; position:relative; }
.task .li::before { content:"•"; position:absolute; left:.2em; color:var(--accent); }
.task .li.num::before { content:none; }
.task .li.num { padding-left:0; }
.task .err { color:var(--off); margin-top:.5em; font-size:.85em; }
.task .empty { color:var(--dim); }
.running { color:var(--think); }
#talk { position:absolute; inset:0; padding:10vh 7vw 6vh; display:flex; flex-direction:column; align-items:center;
  opacity:0; pointer-events:none; transition:opacity .5s ease; z-index:2; }
body.active #talk { opacity:1; background:var(--bg); }
#status { margin-top:1.6vh; font-size:2.6vh; color:var(--dim); min-height:3.2vh; letter-spacing:.05em; }
#turns { width:100%; max-width:120vh; margin-top:3vh; flex:1; overflow:hidden; display:flex; flex-direction:column;
  justify-content:flex-start; gap:2.2vh; }
.turn { max-width:85%; padding:2vh 2.6vh; border-radius:2.6vh; font-size:3.6vh; line-height:1.35;
  white-space:pre-wrap; overflow-wrap:anywhere; animation:rise .35s ease; }
.turn.user { align-self:flex-end; background:var(--you); border-bottom-right-radius:.8vh; }
.turn.assistant { align-self:flex-start; background:var(--bot); border:1px solid #1c2a44;
  border-bottom-left-radius:.8vh; }
.turn.old { opacity:.45; font-size:2.6vh; }
.turn.live, .turn.still { animation:none; }
.turn.live { opacity:.8; }
.turn.long { font-size:2.8vh; }
.who { display:block; font-size:1.7vh; color:var(--dim); margin-bottom:.6vh; letter-spacing:.08em;
  text-transform:uppercase; }
.orb { position:relative; width:12vh; height:12vh; flex:none; }
.orb span { position:absolute; inset:0; border-radius:50%; border:.5vh solid var(--accent); opacity:.0; }
body.s-listening .orb span { border-color:var(--listen); animation:pulse 1.6s ease-out infinite; }
body.s-listening .orb span:nth-child(2) { animation-delay:.53s; }
body.s-listening .orb span:nth-child(3) { animation-delay:1.06s; }
body.s-thinking .orb span:first-child { opacity:1; border-color:var(--think); border-right-color:transparent;
  border-bottom-color:transparent; animation:spin 1s linear infinite; }
body.s-thinking .orb span:nth-child(2) { opacity:.25; border-color:var(--think); }
body.s-speaking .orb span { border-color:var(--speak); animation:wave 1.2s ease-in-out infinite; }
body.s-speaking .orb span:nth-child(2) { animation-delay:.2s; }
body.s-speaking .orb span:nth-child(3) { animation-delay:.4s; }
body.s-call .orb span:first-child, body.s-ringing .orb span:first-child { opacity:1; border-color:var(--speak);
  animation:pulse 2s ease-out infinite; }
@keyframes pulse { 0% { transform:scale(.55); opacity:.9; } 100% { transform:scale(1.25); opacity:0; } }
@keyframes spin { to { transform:rotate(360deg); } }
@keyframes wave { 0%,100% { transform:scale(.7); opacity:.25; } 50% { transform:scale(1); opacity:.9; } }
@keyframes rise { from { transform:translateY(1.5vh); opacity:0; } to { transform:none; opacity:1; } }
"""

JS = r"""
"use strict";
(function () {
  var LABEL = { listening: "Listening…", thinking: "Thinking…", speaking: "", ringing: "Ringing…",
                call: "On a call", idle: "", offline: "Not connected" };
  var DEFAULT = { background: "#05080f", accent: "#5aa2ff", header: true, conversation: true, idle_after_s: 45,
                  clock: { show: true, x: 50, y: 45, size: 22, weight: 200, color: "#e8eef8", seconds: false,
                           hour12: false, date: true }, panels: [] };
  var body = document.body, turnsEl = document.getElementById("turns"), statusEl = document.getElementById("status");
  var nameEl = document.getElementById("name"), dot = document.getElementById("dot");
  var clockEl = document.getElementById("clock"), panelsEl = document.getElementById("panels");
  var state = "offline", turns = [], lastActivity = 0, assistant = "Assistant", partial = "";
  var screen = DEFAULT, tasks = {}, built = {};          // built: section id -> { key, root, update, stop }
  var frames = null;           // the websites' origins this page's CSP (frame-src) was served with

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  // ---- conversation ----------------------------------------------------------------------------------------
  function renderTurns() {
    while (turnsEl.firstChild) turnsEl.removeChild(turnsEl.firstChild);
    var lastUser = -1;
    for (var i = 0; i < turns.length; i++) if (turns[i].role === "user") lastUser = i;
    var shown = turns.slice();
    if (partial) {
      // the words so far: a "You" bubble that grows in place (no entry animation on each update)
      shown.push({ role: "user", text: partial, live: true });
      lastUser = shown.length - 1;
    }
    shown.forEach(function (t, i) {
      var cls = "turn " + t.role + (i < lastUser ? " old" : "") + (t.text.length > 280 ? " long" : "") +
        (t.live ? " live" : "") + (t.still || t.shown ? " still" : "");
      t.shown = true;                  // animate a bubble only the first time it appears
      var b = el("div", cls);
      b.appendChild(el("span", "who", t.role === "user" ? "You" : assistant));
      b.appendChild(document.createTextNode(t.text));
      turnsEl.appendChild(b);
    });
    // keep the newest bubble visible: drop the oldest ones that don't fit (measured by layout height, not
    // scrollHeight: the entry animation's transform would count as overflow)
    function used() {
      var h = 0, kids = turnsEl.children, gap = parseFloat(getComputedStyle(turnsEl).rowGap) || 0;
      for (var k = 0; k < kids.length; k++) h += kids[k].offsetHeight + (k ? gap : 0);
      return h;
    }
    while (turnsEl.children.length > 1 && used() > turnsEl.clientHeight) {
      turnsEl.removeChild(turnsEl.firstChild);
    }
  }

  function setState(s) {
    state = s || "idle";
    body.className = body.className.replace(/\bs-\S+/g, "").trim() + " s-" + state;
    statusEl.textContent = LABEL[state] !== undefined ? LABEL[state] : "";
    if (state !== "idle" && state !== "offline") lastActivity = Date.now();
    tick();
  }

  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function tick() {
    var now = new Date(), c = screen.clock;
    var opt = { hour: "2-digit", minute: "2-digit", hour12: !!c.hour12 };
    if (c.seconds) opt.second = "2-digit";
    document.getElementById("time").textContent = now.toLocaleTimeString([], opt);
    document.getElementById("date").textContent =
      now.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
    var busy = state !== "idle" && state !== "offline";
    var recent = Date.now() - lastActivity < screen.idle_after_s * 1000;
    var active = screen.conversation && (busy || (recent && turns.length > 0));
    body.classList.toggle("active", active);
    body.classList.toggle("idle", !active);
  }

  // ---- home screen -------------------------------------------------------------------------------------------
  function applyScreen(fontFamily) {
    var root = document.documentElement.style, c = screen.clock;
    root.setProperty("--bg", screen.background);
    root.setProperty("--accent", screen.accent);
    root.setProperty("--listen", screen.accent);
    document.getElementById("header").hidden = !screen.header;
    clockEl.hidden = !c.show;
    clockEl.style.left = c.x + "%";
    clockEl.style.top = c.y + "%";
    clockEl.style.color = c.color;
    clockEl.style.fontFamily = fontFamily || "";
    clockEl.style.fontSize = c.size + "vh";
    document.getElementById("time").style.fontWeight = String(c.weight);
    document.getElementById("date").hidden = !c.date;
    renderPanels();
    tick();
  }

  function place(node, p) {
    node.style.left = p.x + "%";
    node.style.top = p.y + "%";
    node.style.width = p.w + "%";
    node.style.height = p.h + "%";
  }

  function renderPanels() {
    var keep = {};
    screen.panels.forEach(function (p) {
      var key = JSON.stringify(p), b = built[p.id];
      if (b && b.key !== key) { b.stop(); panelsEl.removeChild(b.root); b = null; }
      if (!b) {
        b = build(p);
        b.key = key;
        built[p.id] = b;
        panelsEl.appendChild(b.root);
      }
      if (b.update) b.update();
      keep[p.id] = true;
    });
    Object.keys(built).forEach(function (id) {
      if (!keep[id]) { built[id].stop(); panelsEl.removeChild(built[id].root); delete built[id]; }
    });
  }

  function build(p) {
    var root = el("div", "panel " + p.kind), head = null, small = null;
    place(root, p);
    if (p.title || p.kind === "task") {
      head = el("h3", "", "");
      head.appendChild(el("span", "", p.title));
      small = el("small", "");
      head.appendChild(small);
      root.appendChild(head);
    }
    var bodyEl = el("div", "body");
    root.appendChild(bodyEl);
    if (p.kind === "web") return web(p, root, bodyEl);
    if (p.kind === "vnc" || p.kind === "rtsp") return feed(p, root, bodyEl);
    return task(p, root, bodyEl, head, small);
  }

  function web(p, root, box) {
    var f = el("iframe");
    f.setAttribute("sandbox", "allow-scripts allow-same-origin allow-forms");
    f.setAttribute("referrerpolicy", "no-referrer");
    f.setAttribute("title", p.title || "website");
    var z = (p.zoom || 100) / 100;
    if (z !== 1) {
      f.style.width = (100 / z) + "%";
      f.style.height = (100 / z) + "%";
      f.style.transform = "scale(" + z + ")";
    }
    if (/^https?:\/\//i.test(p.url)) f.src = p.url;
    box.appendChild(f);
    var timer = p.refresh_minutes > 0 ? setInterval(function () { f.src = p.url; }, p.refresh_minutes * 60000) : 0;
    return { root: root, stop: function () { clearInterval(timer); f.src = "about:blank"; } };
  }

  function feed(p, root, box) {
    var what = p.kind === "vnc" ? "VNC" : "the camera", label = p.kind === "vnc" ? "VNC: " : "Camera: ";
    var img = el("img", "feed fit-" + (p.fit || "contain")), note = el("div", "note", "Connecting to " + what + "…");
    var badge = el("div", "badge", "reconnecting…"), seq = "", url = null, alive = true, timer = 0;
    img.alt = "";
    badge.hidden = true;
    box.appendChild(img); box.appendChild(note); box.appendChild(badge);
    var every = Math.max(100, 1000 / (p.fps || 2));
    function later(ms) { if (alive) timer = setTimeout(step, ms); }
    function step() {
      fetch("/feed/" + encodeURIComponent(p.id) + "?since=" + encodeURIComponent(seq), { cache: "no-store" })
        .then(function (r) {
          badge.hidden = (r.headers.get("X-Feed-Status") || "connected") === "connected" || r.status === 503;
          if (r.status === 200) {
            seq = r.headers.get("X-Seq") || "";
            return r.blob().then(function (b) {
              var u = URL.createObjectURL(b);
              img.onload = function () { if (url) URL.revokeObjectURL(url); url = u; };
              img.src = u;
              note.hidden = true;
              later(every);
            });
          }
          if (r.status === 204) { later(every); return; }
          if (r.status === 503) {
            return r.json().then(function (j) {
              note.textContent = j.error ? label + j.error : "Connecting to " + what + "…";
              note.hidden = false;
              later(2000);
            });
          }
          later(3000);
        })
        .catch(function () { later(3000); });
    }
    step();
    return { root: root, stop: function () { alive = false; clearTimeout(timer); if (url) URL.revokeObjectURL(url); } };
  }

  function inline(text, parent) {
    // **bold** becomes <b>; links [text](url) keep their text; `code` and __x__ lose their marks
    text = text.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1").replace(/`([^`]+)`/g, "$1").replace(/__(.+?)__/g, "$1");
    text.split("**").forEach(function (part, i) {
      if (!part) return;
      parent.appendChild(i % 2 ? el("b", "", part) : document.createTextNode(part));
    });
    return parent;
  }

  function markdown(text, box) {
    text.split(/\r?\n/).forEach(function (ln) {
      var t = ln.trim(), m;
      if (!t || /^(-{3,}|\*{3,}|_{3,})$/.test(t)) return;
      if ((m = t.match(/^#{1,6}\s+(.*)$/))) box.appendChild(inline(m[1], el("div", "h")));
      else if ((m = t.match(/^[-*•+]\s+(.*)$/))) box.appendChild(inline(m[1], el("div", "li")));
      else if ((m = t.match(/^(\d+[.)])\s+(.*)$/))) box.appendChild(inline(m[1] + " " + m[2], el("div", "li num")));
      else box.appendChild(inline(t, el("div", "p")));
    });
  }

  function whenText(ts) {
    if (!ts) return "";
    var d = new Date(ts * 1000), now = new Date();
    var hm = pad(d.getHours()) + ":" + pad(d.getMinutes());
    if (d.toDateString() === now.toDateString()) return hm;
    return d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" }) + " " + hm;
  }

  function task(p, root, box, head, small) {
    var text = el("div", "text");
    text.style.fontSize = p.font_size + "vh";
    box.appendChild(text);
    var last = "";
    function update() {
      var t = tasks[p.task_id] || { name: "", status: "missing", text: "", error: "this task is not available" };
      if (!p.title && head) head.firstChild.textContent = t.name || "Scheduled task";
      small.textContent = t.running ? "running…" : t.ended ? "updated " + whenText(t.ended) : "";
      small.className = t.running ? "running" : "";
      var sig = t.text + "\u0000" + t.error + "\u0000" + t.status;
      if (sig === last) return;
      last = sig;
      while (text.firstChild) text.removeChild(text.firstChild);
      text.scrollTop = 0;
      if (t.text) markdown(t.text, text);
      else text.appendChild(el("div", "empty", t.status === "never" ? "No summary yet: the task has not run." :
                                                 t.status === "missing" ? "" : "No summary yet."));
      if (t.error) text.appendChild(el("div", "err", "Last run failed: " + t.error));
    }
    return { root: root, update: update, stop: function () {} };
  }

  // long summaries scroll slowly, pause at the end, and start over
  var scrollWait = new WeakMap();
  setInterval(function () {
    if (body.classList.contains("active")) return;
    var boxes = panelsEl.querySelectorAll(".task .text"), now = Date.now();
    for (var i = 0; i < boxes.length; i++) {
      var b = boxes[i], max = b.scrollHeight - b.clientHeight;
      if (max <= 2) { b.scrollTop = 0; continue; }
      var w = scrollWait.get(b);
      if (w === undefined) { scrollWait.set(b, now + 6000); continue; }
      if (now < w) continue;
      if (b.scrollTop >= max - 1) {
        if (w === 0) { scrollWait.set(b, now + 6000); continue; }
        b.scrollTop = 0;
        scrollWait.set(b, now + 6000);
      } else {
        b.scrollTop += 1;
        if (b.scrollTop >= max - 1) scrollWait.set(b, 0);
      }
    }
  }, 50);

  function setScreen(sc, fontFamily, tk) {
    screen = sc || DEFAULT;
    tasks = tk || {};
    applyScreen(fontFamily);
  }

  // ---- events ------------------------------------------------------------------------------------------------
  function handle(ev) {
    if (ev.type === "snapshot") {
      nameEl.textContent = ev.name || "telerouter";
      assistant = ev.assistant || "Assistant";
      dot.classList.toggle("on", !!ev.connected);
      turns = ev.turns || [];
      partial = ev.partial || "";
      if (frames === null) frames = (ev.frames || []).join(" ");
      else if ((ev.frames || []).join(" ") !== frames) { location.reload(); return; }
      setScreen(ev.screen, ev.font_family, ev.tasks);
      renderTurns();
      setState(ev.connected ? ev.state : "offline");
    } else if (ev.type === "screen") {
      // a website from a new origin needs a new CSP: load the page again (it is served with the new one)
      if (frames !== null && (ev.frames || []).join(" ") !== frames) { location.reload(); return; }
      setScreen(ev.screen, ev.font_family, ev.tasks);
    } else if (ev.type === "name") {
      nameEl.textContent = ev.name || "telerouter";
      assistant = ev.assistant || "Assistant";
      renderTurns();
    } else if (ev.type === "connected") {
      dot.classList.toggle("on", !!ev.connected);
      setState(ev.connected ? "idle" : "offline");
    } else if (ev.type === "state") {
      if (ev.state === "listening" && turns.length) {
        // a new turn starts: keep only the last exchange as faded context
        turns = turns.slice(-2);
        renderTurns();
      }
      if ((ev.state === "idle" || ev.state === "offline") && partial) {
        partial = "";
        renderTurns();
      }
      setState(ev.state);
    } else if (ev.type === "partial") {
      partial = ev.text || "";
      lastActivity = Date.now();
      renderTurns();
      tick();
    } else if (ev.type === "transcript") {
      // the final words replace the live bubble in place (no second entry animation)
      var still = ev.role === "user" && !!partial;
      if (ev.role === "user") partial = "";
      turns.push({ role: ev.role, text: ev.text, t: ev.t, still: still });
      if (turns.length > 6) turns = turns.slice(-6);
      lastActivity = Date.now();
      renderTurns();
      tick();
    }
  }

  function connect() {
    var es = new EventSource("/events");
    es.onmessage = function (m) {
      try { handle(JSON.parse(m.data)); } catch (e) { /* ignore a bad frame */ }
    };
    es.onerror = function () {
      dot.classList.remove("on");
    };
  }

  // tell the client (and so the layout editor) how big this screen is
  var sizeTimer = 0;
  function reportSize() {
    clearTimeout(sizeTimer);
    sizeTimer = setTimeout(function () {
      fetch("/size?w=" + Math.round(window.innerWidth) + "&h=" + Math.round(window.innerHeight),
            { cache: "no-store" }).catch(function () {});
    }, 500);
  }
  window.addEventListener("resize", reportSize);

  setInterval(tick, 1000);
  applyScreen("");
  connect();
  reportSize();
})();
"""

ASSETS = {
    "/": ("text/html; charset=utf-8", PAGE.encode()),
    "/display.css": ("text/css; charset=utf-8", CSS.encode()),
    "/display.js": ("text/javascript; charset=utf-8", JS.encode()),
}
