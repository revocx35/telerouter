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
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import shlex
import subprocess
import time

log = logging.getLogger("telerouter_client")

MAX_HEAD = 8192
MAX_CONNS = 8
MAX_STREAMS = 4
KEEP_TURNS = 6
KEEPALIVE_S = 15.0
ON_LISTEN_MIN_GAP_S = 5.0


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
        self._streams: set[asyncio.Queue] = set()
        self._conns = 0
        self._server: asyncio.AbstractServer | None = None
        names = {host, "localhost", "127.0.0.1", "[::1]"} if loopback(host) else {host}
        self._hosts = {f"{n}:{port}" for n in names}

    # -- what the session reports -----------------------------------------------------------------
    def snapshot(self) -> dict:
        return {"type": "snapshot", "name": self.name, "assistant": self.assistant, "connected": self.connected,
                "state": self.state, "turns": self.turns}

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
        self._push({"type": "state", "state": state, "t": time.time()})

    def add_transcript(self, role: str, text: str) -> None:
        if role not in ("user", "assistant"):
            return
        text = str(text or "").strip()[:4000]
        if not text:
            return
        turn = {"role": role, "text": text, "t": time.time()}
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
            path = target.split("?", 1)[0]
            if path == "/events" and method == "GET":
                return await self._events(writer)
            asset = ASSETS.get(path)
            if asset is None:
                return await self._send(writer, 404, b"not found")
            ctype, body = asset
            await self._send(writer, 200, body, ctype, head_only=method == "HEAD")
        finally:
            self._conns -= 1
            try:
                writer.close()
            except Exception:
                pass

    @staticmethod
    def _headers(status: int, ctype: str, length: int | None, extra: dict | None = None) -> bytes:
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
                  503: "Service Unavailable"}.get(status, "OK")
        h = {
            "Content-Type": ctype,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                                       "connect-src 'self'; img-src 'self'; base-uri 'none'; "
                                       "form-action 'none'; frame-ancestors 'none'",
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
<header><span id="dot" class="dot"></span><span id="name">telerouter</span></header>
<main>
  <section id="clock" aria-live="off"><div id="time"></div><div id="date"></div></section>
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
  font-size:2.1vh; color:var(--dim); letter-spacing:.04em; z-index:2; }
.dot { width:1.1vh; height:1.1vh; border-radius:50%; background:var(--off); }
.dot.on { background:var(--speak); }
main { height:100%; display:flex; align-items:center; justify-content:center; }
#clock { text-align:center; transition:opacity .8s ease; }
#time { font-size:22vh; font-weight:200; letter-spacing:-.02em; line-height:1; font-variant-numeric:tabular-nums; }
#date { margin-top:2.5vh; font-size:4vh; color:var(--dim); font-weight:300; }
#talk { position:absolute; inset:0; padding:10vh 7vw 6vh; display:flex; flex-direction:column; align-items:center;
  opacity:0; pointer-events:none; transition:opacity .5s ease; }
body.active #talk { opacity:1; }
body.active #clock { opacity:0; }
#status { margin-top:1.6vh; font-size:2.6vh; color:var(--dim); min-height:3.2vh; letter-spacing:.05em; }
#turns { width:100%; max-width:120vh; margin-top:3vh; flex:1; overflow:hidden; display:flex; flex-direction:column;
  justify-content:flex-start; gap:2.2vh; }
.turn { max-width:85%; padding:2vh 2.6vh; border-radius:2.6vh; font-size:3.6vh; line-height:1.35;
  white-space:pre-wrap; overflow-wrap:anywhere; animation:rise .35s ease; }
.turn.user { align-self:flex-end; background:var(--you); border-bottom-right-radius:.8vh; }
.turn.assistant { align-self:flex-start; background:var(--bot); border:1px solid #1c2a44;
  border-bottom-left-radius:.8vh; }
.turn.old { opacity:.45; font-size:2.6vh; }
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
  var IDLE_AFTER_MS = 45000;          // back to the clock this long after the last answer
  var LABEL = { listening: "Listening…", thinking: "Thinking…", speaking: "", ringing: "Ringing…",
                call: "On a call", idle: "", offline: "Not connected" };
  var body = document.body, turnsEl = document.getElementById("turns"), statusEl = document.getElementById("status");
  var nameEl = document.getElementById("name"), dot = document.getElementById("dot");
  var state = "offline", turns = [], lastActivity = 0, assistant = "Assistant";

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function renderTurns() {
    while (turnsEl.firstChild) turnsEl.removeChild(turnsEl.firstChild);
    var lastUser = -1;
    for (var i = 0; i < turns.length; i++) if (turns[i].role === "user") lastUser = i;
    turns.forEach(function (t, i) {
      var cls = "turn " + t.role + (i < lastUser ? " old" : "") + (t.text.length > 280 ? " long" : "");
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

  function tick() {
    var now = new Date();
    document.getElementById("time").textContent =
      now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
    document.getElementById("date").textContent =
      now.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
    var busy = state !== "idle" && state !== "offline";
    var recent = Date.now() - lastActivity < IDLE_AFTER_MS;
    var active = busy || (recent && turns.length > 0);
    body.classList.toggle("active", active);
    body.classList.toggle("idle", !active);
  }

  function handle(ev) {
    if (ev.type === "snapshot") {
      nameEl.textContent = ev.name || "telerouter";
      assistant = ev.assistant || "Assistant";
      dot.classList.toggle("on", !!ev.connected);
      turns = ev.turns || [];
      renderTurns();
      setState(ev.connected ? ev.state : "offline");
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
      setState(ev.state);
    } else if (ev.type === "transcript") {
      turns.push({ role: ev.role, text: ev.text, t: ev.t });
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

  setInterval(tick, 1000);
  tick();
  connect();
})();
"""

ASSETS = {
    "/": ("text/html; charset=utf-8", PAGE.encode()),
    "/display.css": ("text/css; charset=utf-8", CSS.encode()),
    "/display.js": ("text/javascript; charset=utf-8", JS.encode()),
}
