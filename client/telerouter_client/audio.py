"""Audio I/O for the speaker client: 16 kHz mono 16-bit, 20 ms blocks.

* SoundDeviceIO   real microphone/speaker through PortAudio (sounddevice). Opens the devices at 16 kHz,
                  or at their native rate with resampling when 16 kHz isn't supported.
* FileIO          plays a WAV file as the "microphone" and records what the server sends (tests).

Optional echo cancellation (WebRTC APM from the `livekit` package): what we play is fed to the
canceller as the reverse stream, so the server doesn't hear its own voice back (barge-in, calls).
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
import wave
from collections.abc import Callable

import numpy as np

log = logging.getLogger("telerouter_client.audio")

RATE = 16000
BLOCK = 320                 # 20 ms
BLOCK_BYTES = BLOCK * 2
MAX_PLAY_S = 4.0            # playback buffer cap: drop the oldest beyond this


class Resampler:
    """Streaming resampler (windowed-sinc low-pass, then linear interpolation); fine for speech."""

    def __init__(self, src: int, dst: int):
        self.src, self.dst = src, dst
        self.step = src / dst
        cutoff = 0.45 * min(src, dst) / src          # normalised to the input rate
        n = 63
        t = np.arange(n) - (n - 1) / 2
        h = np.sinc(2 * cutoff * t) * np.hamming(n)
        self.h = (h / h.sum()).astype(np.float32)
        self.hist = np.zeros(n - 1, dtype=np.float32)
        self.prev: float | None = None
        self.t = 0.0                                  # next output position, relative to `prev`

    def __call__(self, x: np.ndarray) -> np.ndarray:
        if self.src == self.dst or not len(x):
            return x.astype(np.float32)
        buf = np.concatenate([self.hist, x.astype(np.float32)])
        y = np.convolve(buf, self.h, mode="valid")    # one filtered sample per input sample
        self.hist = buf[-(len(self.h) - 1):]
        if self.prev is not None:
            y = np.concatenate([[self.prev], y])
        idx = np.arange(self.t, len(y) - 1 + 1e-9, self.step)
        out = np.interp(idx, np.arange(len(y)), y) if len(idx) else np.zeros(0, np.float32)
        self.t = (idx[-1] + self.step if len(idx) else self.t) - (len(y) - 1)
        self.prev = float(y[-1])
        return out.astype(np.float32)


class Player:
    """Thread-safe playback buffer (bytes of PCM16 at 16 kHz)."""

    def __init__(self):
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.volume = 1.0
        self.underruns = 0
        self._was_playing = False

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._buf += data
            cap = int(MAX_PLAY_S * RATE * 2)
            if len(self._buf) > cap:
                del self._buf[: len(self._buf) - cap]

    def flush(self) -> None:
        with self._lock:
            self._buf.clear()

    def take(self, nbytes: int) -> bytes:
        with self._lock:
            chunk = bytes(self._buf[:nbytes])
            del self._buf[:nbytes]
        if len(chunk) < nbytes:
            if chunk or self._was_playing:
                self.underruns += 1 if chunk else 0
            self._was_playing = False
            chunk += bytes(nbytes - len(chunk))
        else:
            self._was_playing = True
        if self.volume != 1.0:
            a = np.frombuffer(chunk, dtype="<i2").astype(np.float32) * self.volume
            chunk = np.clip(a, -32768, 32767).astype("<i2").tobytes()
        return chunk

    @property
    def queued_s(self) -> float:
        return len(self._buf) / (2 * RATE)


class Echo:
    """WebRTC echo canceller + noise suppression (10 ms frames), if the `livekit` package is installed."""

    def __init__(self, delay_ms: int = 60):
        from livekit import rtc          # optional dependency
        self.rtc = rtc
        self.apm = rtc.AudioProcessingModule(echo_cancellation=True, noise_suppression=True,
                                             high_pass_filter=True, auto_gain_control=False)
        self.apm.set_stream_delay_ms(int(delay_ms))
        self._lock = threading.Lock()

    def _frames(self, pcm: bytes):
        for i in range(0, len(pcm) - 319, 320):          # 160 samples = 10 ms
            yield self.rtc.AudioFrame(bytearray(pcm[i:i + 320]), RATE, 1, 160)

    def reverse(self, pcm: bytes) -> None:
        with self._lock:
            for f in self._frames(pcm):
                self.apm.process_reverse_stream(f)

    def capture(self, pcm: bytes) -> bytes:
        out = bytearray()
        with self._lock:
            for f in self._frames(pcm):
                self.apm.process_stream(f)
                out += bytes(f.data.cast("B")) if hasattr(f.data, "cast") else bytes(f.data)
        return bytes(out)


def make_echo(enabled: bool, delay_ms: int) -> Echo | None:
    if not enabled:
        return None
    try:
        e = Echo(delay_ms)
        log.info("echo cancellation on (WebRTC APM)")
        return e
    except Exception as ex:          # ImportError, missing native lib, ...
        log.info("echo cancellation not available (%s); install the 'aec' extra for it", ex.__class__.__name__)
        return None


class SoundDeviceIO:
    def __init__(self, on_mic: Callable[[bytes], None], player: Player, input_device=None, output_device=None,
                 echo: Echo | None = None):
        import sounddevice as sd
        self.sd = sd
        self.on_mic = on_mic
        self.player = player
        self.echo = echo
        self.input_device = input_device
        self.output_device = output_device
        self._in = None
        self._out = None
        self._mic_rs: Resampler | None = None
        self._spk_rs: Resampler | None = None
        self._in_acc = bytearray()

    def _open_rate(self, kind: str, device) -> int:
        sd = self.sd
        check = sd.check_input_settings if kind == "input" else sd.check_output_settings
        try:
            check(device=device, samplerate=RATE, channels=1, dtype="int16")
            return RATE
        except Exception:
            info = sd.query_devices(device, kind)
            return int(info["default_samplerate"])

    def start(self) -> None:
        sd = self.sd
        in_rate = self._open_rate("input", self.input_device)
        out_rate = self._open_rate("output", self.output_device)
        if in_rate != RATE:
            self._mic_rs = Resampler(in_rate, RATE)
            log.info("microphone runs at %d Hz, resampling to 16 kHz", in_rate)
        if out_rate != RATE:
            self._spk_rs = Resampler(RATE, out_rate)
            log.info("speaker runs at %d Hz, resampling from 16 kHz", out_rate)
        self._out_rate = out_rate
        self._out_pending = np.zeros(0, dtype=np.float32)

        def in_cb(indata, frames, t, status):
            data = bytes(indata)
            if self._mic_rs is not None:
                a = np.frombuffer(data, dtype="<i2")
                data = np.clip(self._mic_rs(a), -32768, 32767).astype("<i2").tobytes()
            self._in_acc.extend(data)
            while len(self._in_acc) >= BLOCK_BYTES:
                block = bytes(self._in_acc[:BLOCK_BYTES])
                del self._in_acc[:BLOCK_BYTES]
                if self.echo:
                    block = self.echo.capture(block)
                self.on_mic(block)

        def out_cb(outdata, frames, t, status):
            if self._spk_rs is None:
                chunk = self.player.take(frames * 2)
                if self.echo:
                    self.echo.reverse(chunk)
                outdata[:] = chunk
                return
            need = frames
            while len(self._out_pending) < need:
                pcm = self.player.take(BLOCK_BYTES)
                if self.echo:
                    self.echo.reverse(pcm)
                self._out_pending = np.concatenate([self._out_pending,
                                                    self._spk_rs(np.frombuffer(pcm, dtype="<i2"))])
            out = self._out_pending[:need]
            self._out_pending = self._out_pending[need:]
            outdata[:] = np.clip(out, -32768, 32767).astype("<i2").tobytes()

        in_block = BLOCK if in_rate == RATE else int(in_rate * 0.02)
        out_block = BLOCK if out_rate == RATE else int(out_rate * 0.02)
        self._in = sd.RawInputStream(samplerate=in_rate, blocksize=in_block, channels=1, dtype="int16",
                                     device=self.input_device, callback=in_cb, latency="low")
        self._out = sd.RawOutputStream(samplerate=out_rate, blocksize=out_block, channels=1, dtype="int16",
                                       device=self.output_device, callback=out_cb, latency="low")
        self._in.start()
        self._out.start()
        if self.echo:
            delay = (self._in.latency + self._out.latency) * 1000 + 20
            self.echo.apm.set_stream_delay_ms(int(delay))

    def stop(self) -> None:
        for s in (self._in, self._out):
            if s is not None:
                try:
                    s.stop()
                    s.close()
                except Exception:
                    pass


class FileIO:
    """Test I/O: a WAV file is the microphone (real time, then silence); playback is recorded."""

    def __init__(self, on_mic: Callable[[bytes], None], player: Player, input_wav: str | None,
                 output_wav: str | None, start_delay: float = 2.0):
        self.on_mic = on_mic
        self.player = player
        self.input_wav = input_wav
        self.output_wav = output_wav
        self.start_delay = start_delay
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._out = None

    def start(self) -> None:
        pcm = b""
        if self.input_wav:
            with wave.open(self.input_wav, "rb") as w:
                if w.getframerate() != RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
                    raise SystemExit("--input-wav must be 16 kHz mono 16-bit")
                pcm = w.readframes(w.getnframes())
        if self.output_wav:
            self._out = wave.open(self.output_wav, "wb")
            self._out.setnchannels(1)
            self._out.setsampwidth(2)
            self._out.setframerate(RATE)
        self._thread = threading.Thread(target=self._run, args=(pcm,), daemon=True)
        self._thread.start()

    def _run(self, pcm: bytes) -> None:
        t0 = time.monotonic()
        n = 0
        pos = -int(self.start_delay * RATE * 2)
        while not self._stop.is_set():
            n += 1
            target = t0 + n * 0.02
            d = target - time.monotonic()
            if d > 0:
                time.sleep(d)
            if 0 <= pos < len(pcm):
                block = pcm[pos:pos + BLOCK_BYTES]
                block += bytes(BLOCK_BYTES - len(block))
            else:
                block = bytes(BLOCK_BYTES)
            pos += BLOCK_BYTES
            self.on_mic(block)
            out = self.player.take(BLOCK_BYTES)
            if self._out:
                self._out.writeframes(out)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(2)
        if self._out:
            self._out.close()


class AlsaIO:
    """Plain ALSA through `arecord` / `aplay` (alsa-utils): for headless Linux without PulseAudio/PipeWire,
    where PortAudio builds that include the PulseAudio host API refuse to start at all."""

    def __init__(self, on_mic: Callable[[bytes], None], player: Player, input_device=None, output_device=None,
                 echo: Echo | None = None):
        self.on_mic = on_mic
        self.player = player
        self.echo = echo
        self.input_device = str(input_device) if input_device not in (None, "") else "default"
        self.output_device = str(output_device) if output_device not in (None, "") else "default"
        self._rec = self._play = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    @staticmethod
    def available() -> bool:
        import shutil
        return bool(shutil.which("arecord") and shutil.which("aplay"))

    def _cmd(self, tool: str, device: str) -> list[str]:
        return [tool, "-q", "-D", device, "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw",
                "--buffer-time=100000", "--period-time=20000"]

    def start(self) -> None:
        import subprocess
        self._rec = subprocess.Popen(self._cmd("arecord", self.input_device), stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        self._play = subprocess.Popen(self._cmd("aplay", self.output_device), stdin=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL)
        for fn in (self._read_loop, self._write_loop):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self._threads.append(t)
        log.info("audio through ALSA (arecord/aplay) on %s / %s", self.input_device, self.output_device)

    def _read_loop(self) -> None:
        f = self._rec.stdout
        while not self._stop.is_set():
            block = f.read(BLOCK_BYTES)
            if not block or len(block) < BLOCK_BYTES:
                if self._stop.is_set():
                    return
                log.error("arecord stopped")
                os._exit(3)                       # let the service manager restart us
            if self.echo:
                block = self.echo.capture(block)
            self.on_mic(block)

    def _write_loop(self) -> None:
        f = self._play.stdin
        while not self._stop.is_set():
            chunk = self.player.take(BLOCK_BYTES)    # aplay blocks when its buffer is full: that paces us
            if self.echo:
                self.echo.reverse(chunk)
            try:
                f.write(chunk)
                f.flush()
            except (BrokenPipeError, OSError):
                if self._stop.is_set():
                    return
                log.error("aplay stopped")
                os._exit(3)

    def stop(self) -> None:
        self._stop.set()
        for p in (self._rec, self._play):
            if p is not None:
                with contextlib.suppress(Exception):
                    p.terminate()
