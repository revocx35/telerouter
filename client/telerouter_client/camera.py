"""An RTSP camera viewer for the smart display's camera sections (client 0.13).

The speaker client runs `ffmpeg` (it must be installed on the machine: `sudo apt install ffmpeg`) to read the
camera's stream and turn it into a few JPEG pictures per second; the display page shows the newest one, exactly
like a VNC section (`/feed/<section id>`).

The camera's user name and password never appear on ffmpeg's command line (any local user could read them with
`ps`): the address with the credentials goes into a private (0600) ffconcat list in a private temporary folder
that is removed as soon as ffmpeg has opened the stream. ffmpeg's own error text contains that address, so it is
never logged or shown as is: errors become fixed, readable messages (`_why`) and anything that looks like
credentials is cut out (`redact`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from urllib.parse import quote, urlsplit

log = logging.getLogger("telerouter_client")

MAX_FRAME = 8 << 20                # a JPEG bigger than this is not a picture we want
STALL_S = 25.0                     # no picture for this long: start ffmpeg again
MAX_WIDTH = 1920
SOI, EOI = b"\xff\xd8", b"\xff\xd9"


def build_url(url: str, username: str = "", password: str = "") -> str:
    u = urlsplit(url)
    if u.scheme not in ("rtsp", "rtsps") or not u.hostname or "@" in u.netloc:
        raise ValueError("not an rtsp:// address")
    if not username and not password:
        return url
    cred = quote(username, safe="") + (":" + quote(password, safe="") if password else "")
    return f"{u.scheme}://{cred}@{u.netloc}{u.path}" + (f"?{u.query}" if u.query else "")


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    text = re.sub(r"(rtsps?|https?|tcp|tls)://[^\s/'\"]*@", r"\1://***@", text)
    for s in secrets:
        if s:
            text = text.replace(s, "***").replace(quote(s, safe=""), "***")
    return text


def _why(stderr: str) -> str:
    low = stderr.lower()
    if "401" in low or "unauthorized" in low or "authorization failed" in low:
        return "wrong user name or password"
    if "404" in low or "not found" in low:
        return "the camera has no such stream (check the path)"
    if "connection refused" in low:
        return "the camera refused the connection (check the address and port)"
    if "timed out" in low or "timeout" in low or "no route" in low or "unreachable" in low:
        return "the camera did not answer"
    if "invalid data" in low or "could not find codec" in low:
        return "the stream could not be decoded"
    return "the stream stopped"


class RtspViewer:
    """Keeps ffmpeg reading the camera and the newest JPEG in memory (restarts with a backoff)."""

    def __init__(self, url: str, username: str = "", password: str = "", fps: float = 2.0,
                 ffmpeg: str | None = None):
        self.url, self.username, self.password = url, username or "", password or ""
        self.fps = min(10.0, max(0.2, float(fps or 2.0)))
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg") or ""
        self.jpeg: bytes = b""
        self.seq = 0
        self.status = "connecting"
        self.error = ""
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None

    # the same face as vnc.VncViewer, for display.py
    @property
    def fb(self):
        return self.jpeg or None

    def key(self) -> tuple:
        return ("rtsp", self.url, self.username, self.password, self.fps)

    async def image(self) -> tuple[int, bytes, str]:
        return self.seq, self.jpeg, "image/jpeg"

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._kill()

    def _kill(self) -> None:
        p = self._proc
        if p is not None and p.returncode is None:
            try:
                p.kill()
            except ProcessLookupError:
                pass

    async def _run(self) -> None:
        backoff = 2.0
        while True:
            t0 = time.monotonic()
            try:
                await self._once()
            except asyncio.CancelledError:
                self._kill()
                raise
            except Exception as e:                       # never let a camera problem end the client
                self.status, self.error = "error", e.__class__.__name__
            log.warning("camera %s: %s", redact(self.url), self.error)
            if time.monotonic() - t0 > 60:
                backoff = 2.0
            await asyncio.sleep(300.0 if "password" in self.error else backoff)
            backoff = min(backoff * 2, 60.0)

    async def _once(self) -> None:
        if not self.ffmpeg:
            self.status, self.error = "error", "ffmpeg is not installed on this machine (sudo apt install ffmpeg)"
            return
        self.status = "connecting"
        full = build_url(self.url, self.username, self.password)
        tmp = tempfile.mkdtemp(prefix="trcam-")            # 0700
        listing = os.path.join(tmp, "in.ffconcat")
        try:
            fd = os.open(listing, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write("ffconcat version 1.0\n")
                f.write("file '" + full.replace("'", "%27") + "'\n")
                f.write("option rtsp_transport tcp\noption timeout 10000000\n")
            self._proc = await asyncio.create_subprocess_exec(
                self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-protocol_whitelist", "file,rtsp,rtsps,tcp,udp,rtp,tls",
                "-i", listing, "-an", "-sn", "-dn",
                "-vf", f"fps={self.fps:g},scale='min({MAX_WIDTH},iw)':-2",
                "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "5", "-",
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            err = asyncio.get_running_loop().create_task(self._proc.stderr.read(65536))
            buf = bytearray()
            while True:
                try:
                    chunk = await asyncio.wait_for(self._proc.stdout.read(262144), STALL_S)
                except asyncio.TimeoutError:
                    self._kill()
                    self.status, self.error = "error", "no picture from the camera for a while"
                    return
                if not chunk:
                    break
                buf += chunk
                while True:
                    end = buf.find(EOI)
                    if end < 0:
                        break
                    start = buf.find(SOI)
                    frame = bytes(buf[start:end + 2]) if 0 <= start < end else b""
                    del buf[:end + 2]
                    if frame:
                        if self.status != "connected":
                            log.info("camera %s connected", redact(self.url))
                            shutil.rmtree(tmp, ignore_errors=True)   # ffmpeg has the address: drop the file
                        self.jpeg, self.status, self.error = frame, "connected", ""
                        self.seq += 1
                if len(buf) > MAX_FRAME:
                    self._kill()
                    self.status, self.error = "error", "the camera's pictures are too large"
                    return
            await self._proc.wait()
            stderr = (await err).decode("utf-8", "replace")
            self.status, self.error = "error", _why(redact(stderr, (self.password, self.username)))
        finally:
            self._kill()
            shutil.rmtree(tmp, ignore_errors=True)
