"""Transcribing an utterance while it is being spoken.

Driven by the endpointer's feeds (`Endpointer.on_feed`), two jobs:

* early final: as soon as the person pauses (`EARLY_SILENCE_MS`), the accurate Whisper model starts on the audio
  so far. When the pause turns into the end of the utterance with no speech since, that text is the answer:
  Whisper's fixed ~2 s (it always works on a 30 s window) ran during the end-of-speech wait instead of after it.
* partials (only with `on_partial`, for a device with a screen): the words so far from the fast live model
  (`SpeechServices.transcribe_live`), one request at a time, whenever there is new speech. Shown, never acted on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

from ..media.pcm import RATE
from .vad import Endpointer

log = logging.getLogger("telerouter.voice")

EARLY_SILENCE_MS = 350                     # a pause this long starts the accurate model on the audio so far
PARTIAL_MIN_NEW = 300 * 2 * RATE // 1000   # bytes of new speech before the words so far are transcribed again
PARTIAL_GAP_S = 0.4                        # rest between live passes (the host's CPU also runs the VAD + Whisper)


class LiveTranscriber:
    def __init__(self, speech, ep: Endpointer, language: str = "", names: list[str] | None = None,
                 prefix: bytes = b"", on_partial: Callable[[str], None] | None = None):
        self.speech = speech
        self.ep = ep
        self.language = language
        self.names = names
        self.prefix = prefix               # audio of earlier rounds (a sentence that went on after a pause)
        self.on_partial = on_partial if on_partial and getattr(speech, "live_available", False) else None
        self._early: asyncio.Task | None = None
        self._early_end = -1               # ep.speech_end the early task's audio covers
        self._partial: asyncio.Task | None = None
        self._partial_end = 0
        self._last_partial = ""
        self._partial_done_at = 0.0
        self._closed = False
        self.early_used = False
        ep.on_feed = self.poke
        self.poke()

    def _audio(self) -> bytes:
        return self.prefix + bytes(self.ep.audio)

    def poke(self) -> None:
        """After each feed (synchronous, cheap): start the work the new audio calls for."""
        ep = self.ep
        if self._closed or not ep.started:
            return
        if ep.silence_ms >= EARLY_SILENCE_MS and ep.speech_end != self._early_end:
            if self._early is not None:
                self._early.cancel()       # superseded: the person spoke again after the last pause
            self._early_end = ep.speech_end
            self._early = asyncio.create_task(self.speech.transcribe(self._audio(), self.language, self.names))
            self._early.add_done_callback(_quiet)
        if (self.on_partial is not None and (self._partial is None or self._partial.done())
                and (self._early is None or self._early.done())     # the accurate request goes first
                and ep.speech_end - self._partial_end >= PARTIAL_MIN_NEW
                and time.monotonic() - self._partial_done_at >= PARTIAL_GAP_S):
            self._partial_end = ep.speech_end
            self._partial = asyncio.create_task(self._run_partial(self._audio()))

    async def _run_partial(self, audio: bytes) -> None:
        try:
            text = await self.speech.transcribe_live(audio, self.language)
        except asyncio.CancelledError:
            raise
        except Exception as e:             # the live model is only a nicety: stop asking for this utterance
            log.info("live speech-to-text failed: %s", type(e).__name__)
            self.on_partial = None
            return
        finally:
            self._partial_done_at = time.monotonic()
        if not self._closed and text and text != self._last_partial and self.on_partial is not None:
            self._last_partial = text
            self.on_partial(text)

    async def final(self) -> str:
        """The accurate text of all the audio (call once the endpointer has ended)."""
        ep = self.ep
        t = self._early
        if t is not None and self._early_end == ep.speech_end:
            try:
                text = await t
                self.early_used = True
                return text
            except asyncio.CancelledError:
                me = asyncio.current_task()
                if me is not None and me.cancelling():
                    raise                  # we are being cancelled; otherwise only the early task was
            except Exception:
                pass                       # fall back to a fresh request, which reports the error
        return await self.speech.transcribe(self._audio(), self.language, self.names)

    def close(self) -> None:
        self._closed = True
        if self.ep.on_feed == self.poke:
            self.ep.on_feed = None
        for t in (self._early, self._partial):
            if t is not None and not t.done():
                t.cancel()


def _quiet(t: asyncio.Task) -> None:
    """Mark an early task's exception as retrieved (final() may never look at a superseded one)."""
    with contextlib.suppress(asyncio.CancelledError):
        t.exception()
