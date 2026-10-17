"""One connected speaker client: paced audio out, microphone in, state shown in the UI.

Wire protocol (WebSocket, wss://.../ws/client, `Authorization: Bearer <client token>`):

  client -> server  text   {"type": "hello", "name", "version", "platform", "aec": bool}
                    binary 16 kHz mono PCM16 LE microphone audio (any size up to 64 KiB, usually 20 ms)
                    text   {"type": "pong"} / {"type": "button", "action": "talk"|"cancel"}
  server -> client  binary 16 kHz mono PCM16 LE audio to play
                    text   {"type": "state", "state": "idle|listening|thinking|speaking|ringing|call"}
                           {"type": "flush"}   drop everything queued for playback now
                           {"type": "ping"}    {"type": "config", "volume": 100}
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable

from ..media import pcm as pcmmod
from ..models import Client

log = logging.getLogger("telerouter.client")

LEAD_S = 0.25          # audio sent ahead of real time (client jitter buffer covers it)
CHUNK_BYTES = pcmmod.FRAME_BYTES * 5      # 100 ms per message


class ClientConn:
    def __init__(self, client: Client, send_bytes, send_text, aec: bool = False):
        self.client = client
        self.id = client.id
        self._send_bytes = send_bytes
        self._send_text = send_text
        self.aec = aec
        self.state = "idle"
        self.connected_at = time.time()
        self.on_mic: Callable[[bytes], None] | None = None
        self._out = bytearray()
        self._out_event = asyncio.Event()
        self._play_until = 0.0              # monotonic time when sent audio finishes playing
        self._drained = asyncio.Event()
        self._drained.set()
        self._task: asyncio.Task | None = None
        self.closed = asyncio.Event()
        self.mic_bytes = 0
        self.mic_level = -120.0
        self.last_text = ""                 # last thing heard (for the UI)
        self.last_reply = ""
        self.busy_task: asyncio.Task | None = None   # the conversation/call that owns this client

    @property
    def name(self) -> str:
        return self.client.name

    def start(self) -> None:
        self._task = asyncio.create_task(self._sender())

    async def close(self) -> None:
        self.closed.set()
        if self._task:
            self._task.cancel()
        if self.busy_task and not self.busy_task.done():
            self.busy_task.cancel()

    # -- state ----------------------------------------------------------------------------------
    def set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            asyncio.create_task(self.send_json({"type": "state", "state": state}))

    async def send_json(self, obj: dict) -> None:
        try:
            await self._send_text(json.dumps(obj))
        except Exception:
            pass

    # -- microphone ------------------------------------------------------------------------------
    def mic(self, data: bytes) -> None:
        self.mic_bytes += len(data)
        if self.mic_bytes % 16000 < len(data):          # cheap level meter, a few times a second
            self.mic_level = pcmmod.rms_dbfs(data)
        if self.on_mic:
            try:
                self.on_mic(data)
            except Exception:
                log.exception("mic handler failed")

    # -- speaker -------------------------------------------------------------------------------
    def play(self, audio: bytes) -> None:
        """Queue PCM16 audio for playback (after anything already queued)."""
        if not audio:
            return
        self._out += pcmmod.gain(audio, self.client.volume)
        self._drained.clear()
        self._out_event.set()

    def flush(self) -> None:
        """Stop playback now (queued here and buffered on the client)."""
        self._out.clear()
        self._play_until = time.monotonic()
        self._drained.set()
        asyncio.create_task(self.send_json({"type": "flush"}))

    @property
    def playing(self) -> bool:
        return bool(self._out) or time.monotonic() < self._play_until

    async def drained(self, extra: float = 0.0) -> None:
        """Wait until everything queued has been played on the client."""
        await self._drained.wait()
        wait = self._play_until - time.monotonic() + extra
        if wait > 0:
            await asyncio.sleep(wait)

    async def _sender(self) -> None:
        while True:
            if not self._out:
                if time.monotonic() >= self._play_until:
                    self._drained.set()
                self._out_event.clear()
                try:
                    await asyncio.wait_for(self._out_event.wait(), 0.1)
                except TimeoutError:
                    pass
                continue
            now = time.monotonic()
            if self._play_until < now:
                self._play_until = now
            ahead = self._play_until - now
            if ahead > LEAD_S:
                await asyncio.sleep(min(ahead - LEAD_S, 0.1))
                continue
            chunk = bytes(self._out[:CHUNK_BYTES])
            del self._out[:CHUNK_BYTES]
            self._play_until += len(chunk) / (2 * pcmmod.RATE)
            try:
                await self._send_bytes(chunk)
            except Exception:
                self.closed.set()
                return

    def info(self) -> dict:
        return {"id": self.id, "name": self.name, "state": self.state, "aec": self.aec,
                "connected_at": self.connected_at, "mic_level": round(self.mic_level, 1),
                "last_text": self.last_text, "last_reply": self.last_reply}
