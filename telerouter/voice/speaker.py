"""A virtual speaker: what agents and you talk to ("odam"), built from devices.

Its microphone is one client device; its output is a client device or a Google Cast speaker. Both can be the
same client (the classic speaker) or different devices: the Raqqa laptop's microphone with the Mi speaker's
loudspeaker. The voice assistant and the call router only see this object:

  play(pcm)   speech (answers, announcements, "Calling dad")   -> the output
  cue(pcm)    beeps and ticks                                   -> the output, or the mic device (`cues`)
  live(pcm)   live call audio and ring-back                     -> the output if it can play live audio
                                                                   (a client), otherwise the mic device
A Cast speaker buffers seconds of a live stream, so calls on a speaker with a Cast output are heard on the mic
device. The speaker is online while its mic device is connected and its output is available.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from ..media import pcm as pcmmod
from ..models import Speaker
from .client_conn import ClientConn


class VirtualSpeaker:
    def __init__(self, cfg: Speaker, mic: ClientConn | None, out):
        self.cfg = cfg
        self.id = cfg.id
        self.mic_dev = mic
        self.out = out                      # ClientConn | CastOutput | None
        self.state = "idle"
        self.connected_at = time.time()
        self.on_mic: Callable[[bytes], None] | None = None
        self.closed = asyncio.Event()
        self.busy_task: asyncio.Task | None = None   # the conversation/call that owns this speaker
        self.last_text = ""                 # last thing heard (for the UI)
        self.last_reply = ""
        self.route = None                   # voice routed to a tmux session (Route)
        self.call_session = None
        self.wake_fed = False
        self.wake_stream = None             # its openWakeWord stream (voice assistant)

    @property
    def name(self) -> str:
        return self.cfg.name

    @property
    def has_mic(self) -> bool:
        return self.mic_dev is not None

    @property
    def aec(self) -> bool:
        """Barge-in needs the device's echo canceller, which only knows what that same device plays."""
        return self.mic_dev is not None and self.mic_dev is self.out and self.mic_dev.aec

    # -- outputs -------------------------------------------------------------------------------------------
    @property
    def cue_out(self):
        mode = self.cfg.cues
        if mode == "auto":
            mode = "mic" if getattr(self.out, "kind", "") == "cast" else "output"
        if mode == "mic" and self.mic_dev is not None:
            return self.mic_dev
        return self.out or self.mic_dev

    @property
    def live_out(self):
        if self.out is not None and self.out.live_ok:
            return self.out
        return self.mic_dev

    def _outputs(self) -> list:
        seen: list = []
        for o in (self.out, self.cue_out, self.live_out):
            if o is not None and all(o is not x for x in seen):
                seen.append(o)
        return seen

    def _send(self, o, audio: bytes) -> None:
        if o is None or not audio:
            return
        if o.kind == "cast":               # an unreachable Cast speaker hands the audio to the mic device
            o.spill = self.mic_dev.play if self.mic_dev is not None else None
        o.play(pcmmod.gain(audio, self.cfg.volume))

    def play(self, audio: bytes) -> None:
        self._send(self.out, audio)

    def cue(self, audio: bytes) -> None:
        self._send(self.cue_out, audio)

    def live(self, audio: bytes) -> None:
        self._send(self.live_out, audio)

    def flush(self) -> None:
        for o in self._outputs():
            o.flush()

    def prepare(self) -> None:
        if self.out is not None:
            self.out.prepare()

    @property
    def playing(self) -> bool:
        return any(o.playing for o in self._outputs())

    async def drained(self, extra: float = 0.0) -> None:
        await asyncio.gather(*(o.drained(extra) for o in self._outputs()))

    # -- microphone / state --------------------------------------------------------------------------------
    def mic(self, data: bytes) -> None:
        if self.on_mic:
            self.on_mic(data)

    def set_state(self, state: str) -> None:
        self.state = state
        for d in (self.mic_dev, self.out):
            if isinstance(d, ClientConn):
                d.set_state(state)

    async def close(self) -> None:
        self.closed.set()
        if self.busy_task and not self.busy_task.done():
            self.busy_task.cancel()
        if self.mic_dev is not None and self.mic_dev.on_mic == self.mic:
            self.mic_dev.on_mic = None

    def info(self) -> dict:
        return {"id": self.id, "name": self.name, "state": self.state, "aec": self.aec,
                "connected_at": self.connected_at,
                "mic_level": round(self.mic_dev.mic_level, 1) if self.mic_dev else None,
                "mic": self.mic_dev.name if self.mic_dev else "",
                "output": self.out.name if self.out is not None else "",
                "last_text": self.last_text, "last_reply": self.last_reply}
