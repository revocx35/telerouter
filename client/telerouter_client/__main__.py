"""telerouter-client: enroll | run | devices | test-audio"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from . import __version__

log = logging.getLogger("telerouter_client")


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "telerouter" / "client.json"


def load_config() -> dict:
    p = config_path()
    if not p.exists():
        raise SystemExit(f"not enrolled yet ({p} missing): run 'telerouter-client enroll ...' first")
    return json.loads(p.read_text())


def save_config(cfg: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=1)
    os.replace(tmp, p)


def cmd_enroll(a) -> None:
    from .net import enroll
    data = enroll(a.server, a.ca_sha256, a.code)
    old = {}
    try:
        old = json.loads(config_path().read_text())
    except (OSError, ValueError):
        pass
    for k in ("input_device", "output_device", "aec"):
        if k in old:
            data[k] = old[k]
    save_config(data)
    print(f"enrolled as '{data['name']}' (id {data['client_id']}); config in {config_path()}")


def _device(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except ValueError:
        return v


def cmd_run(a) -> None:
    from .audio import AlsaIO, FileIO, Player, SoundDeviceIO, make_echo
    from .net import Session
    cfg = load_config()
    player = Player()
    use_files = bool(a.input_wav or a.output_wav)
    aec_wanted = cfg.get("aec", True) and not a.no_aec and not use_files
    echo = make_echo(aec_wanted, 60)
    sess = Session(cfg, player, aec=echo is not None)
    backend = a.backend or cfg.get("backend") or "auto"
    ind, outd = _device(a.input_device or cfg.get("input_device")), _device(a.output_device or cfg.get("output_device"))
    if use_files:
        io = FileIO(sess.mic, player, a.input_wav, a.output_wav, a.start_delay)
    elif backend == "alsa":
        io = AlsaIO(sess.mic, player, ind, outd, echo)
    else:
        try:
            io = SoundDeviceIO(sess.mic, player, ind, outd, echo)
        except Exception as e:                   # e.g. PortAudio built with PulseAudio but no sound server
            if backend == "portaudio" or not AlsaIO.available():
                raise
            log.warning("PortAudio is not usable (%s); using ALSA directly", str(e).splitlines()[0][:120])
            io = AlsaIO(sess.mic, player, ind, outd, echo)

    async def main():
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        task = asyncio.create_task(sess.run())
        io.start()
        log.info("telerouter-client %s running as '%s'", __version__, cfg.get("name", ""))
        waiter = asyncio.create_task(stop.wait())
        timers = [waiter, task]
        if a.duration:
            timers.append(asyncio.create_task(asyncio.sleep(a.duration)))
        await asyncio.wait(timers, return_when=asyncio.FIRST_COMPLETED)
        task.cancel()
        io.stop()

    asyncio.run(main())


def cmd_devices(_a) -> None:
    import sounddevice as sd
    print(sd.query_devices())
    print("\ndefault (input, output):", sd.default.device)


def cmd_set(a) -> None:
    cfg = load_config()
    for k in ("input_device", "output_device"):
        v = getattr(a, k)
        if v is not None:
            cfg[k] = v
    if a.aec is not None:
        cfg["aec"] = a.aec == "on"
    if a.backend is not None:
        cfg["backend"] = a.backend
    save_config(cfg)
    print("saved; restart the client (systemctl --user restart telerouter-client, or launchctl kickstart)")


def cmd_test_audio(a) -> None:
    """Record a few seconds, then play them back (checks the microphone, the speaker and permissions)."""
    from .audio import BLOCK_BYTES, Player, SoundDeviceIO
    cfg = {}
    try:
        cfg = load_config()
    except SystemExit:
        pass
    rec = bytearray()
    player = Player()
    io = SoundDeviceIO(lambda b: rec.extend(b), player, _device(a.input_device or cfg.get("input_device")),
                       _device(a.output_device or cfg.get("output_device")))
    io.start()
    print(f"recording {a.seconds} s - say something...")
    time.sleep(a.seconds)
    import numpy as np
    x = np.frombuffer(bytes(rec), dtype="<i2").astype(np.float32)
    rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
    print(f"got {len(rec) // BLOCK_BYTES} blocks, level {20 * np.log10(max(rms, 1) / 32768):.1f} dBFS; playing it back")
    player.feed(bytes(rec))
    time.sleep(a.seconds + 0.5)
    io.stop()


def main() -> None:
    p = argparse.ArgumentParser(prog="telerouter-client", description="telerouter speaker client")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("enroll", help="register this machine with a one-time code from the web UI")
    e.add_argument("--server", required=True)
    e.add_argument("--ca-sha256", required=True)
    e.add_argument("--code", required=True)
    e.set_defaults(fn=cmd_enroll)
    r = sub.add_parser("run", help="run the speaker client")
    r.add_argument("--input-device")
    r.add_argument("--output-device")
    r.add_argument("--no-aec", action="store_true", help="no echo cancellation")
    r.add_argument("--backend", choices=["auto", "portaudio", "alsa"], help="audio backend (default auto: "
                   "PortAudio, else ALSA's arecord/aplay)")
    r.add_argument("--input-wav", help="test: use a 16 kHz mono WAV as the microphone")
    r.add_argument("--output-wav", help="test: record what would be played")
    r.add_argument("--start-delay", type=float, default=2.0, help="test: seconds before --input-wav starts")
    r.add_argument("--duration", type=float, default=0, help="stop after this many seconds (0 = run forever)")
    r.set_defaults(fn=cmd_run)
    d = sub.add_parser("devices", help="list audio devices")
    d.set_defaults(fn=cmd_devices)
    s = sub.add_parser("set", help="choose audio devices / echo cancellation")
    s.add_argument("--input-device")
    s.add_argument("--output-device")
    s.add_argument("--aec", choices=["on", "off"])
    s.add_argument("--backend", choices=["auto", "portaudio", "alsa"])
    s.set_defaults(fn=cmd_set)
    t = sub.add_parser("test-audio", help="record and play back a few seconds")
    t.add_argument("--seconds", type=float, default=4)
    t.add_argument("--input-device")
    t.add_argument("--output-device")
    t.set_defaults(fn=cmd_test_audio)
    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    a.fn(a)


if __name__ == "__main__":
    main()
