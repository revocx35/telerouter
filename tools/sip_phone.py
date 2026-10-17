#!/usr/bin/env python3
"""A scriptable SIP test phone (uses telerouter's SIP stack).

  python tools/sip_phone.py --server 127.0.0.1 --port 5160 --user 2001 --password Test-2001-pw \
      call 1009 --play in.wav --record out.wav --seconds 30
  python tools/sip_phone.py ... answer --play in.wav --record out.wav --seconds 30   # wait for a call

`--play` is a 16 kHz mono WAV (sent once, after `--play-delay` seconds); `--record` gets what the
other side sends (16 kHz). DTMF: --dtmf "1234#" sent after --dtmf-delay seconds.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telerouter.media import pcm as pcmmod          # noqa: E402
from telerouter.media.rtp import PortAllocator       # noqa: E402
from telerouter.router.sipaudio import SipAudio      # noqa: E402
from telerouter.sip.ua import AccountConfig, UserAgent   # noqa: E402


async def main(a) -> int:
    ua = UserAgent(a.local_port, PortAllocator(a.rtp_min, a.rtp_min + 40), user_agent="sip-phone")
    incoming: asyncio.Queue = asyncio.Queue()

    async def on_incoming(call):
        await incoming.put(call)

    ua.on_incoming = on_incoming
    await ua.start()
    await ua.set_accounts([AccountConfig(id="p", server=a.server, port=a.port, username=a.user, password=a.password,
                                         display_name=a.name)])
    acc = ua.accounts["p"]
    for _ in range(50):
        if acc.state == "registered":
            break
        await asyncio.sleep(0.1)
    print("registered" if acc.state == "registered" else f"registration: {acc.state} {acc.error}", flush=True)
    if a.cmd == "call":
        call = await acc.dial(a.target)
        try:
            await asyncio.wait_for(call.answered.wait(), a.ring)
        except TimeoutError:
            print("no answer:", call.end_reason or call.state)
            await call.hangup()
            await ua.stop()
            return 1
    else:
        call = await asyncio.wait_for(incoming.get(), a.ring)
        print("incoming from", call.remote_display, call.remote_user, flush=True)
        await asyncio.sleep(a.answer_after)
        await call.answer()
    print("connected", call.codec, flush=True)
    rec = bytearray()
    audio = SipAudio(call, on_audio=rec.extend)
    audio.start()

    async def player():
        if a.play:
            await asyncio.sleep(a.play_delay)
            w = wave.open(a.play)
            pcm = w.readframes(w.getnframes())
            for i in range(0, len(pcm), 640):
                audio.send(pcm[i:i + 640])
                await asyncio.sleep(0.02)

    async def dtmf():
        if a.dtmf:
            await asyncio.sleep(a.dtmf_delay)
            for d in a.dtmf:
                await call.send_dtmf(d)
                await asyncio.sleep(0.15)

    tasks = [asyncio.create_task(player()), asyncio.create_task(dtmf())]
    try:
        await asyncio.wait_for(call.ended.wait(), a.seconds)
        print("remote hung up:", call.end_reason, flush=True)
    except TimeoutError:
        await call.hangup()
        print("hung up after", a.seconds, "s", flush=True)
    for t in tasks:
        t.cancel()
    audio.stop()
    if a.record:
        Path(a.record).write_bytes(pcmmod.to_wav(bytes(rec)))
        print("recorded", round(len(rec) / 32000, 1), "s", flush=True)
    await ua.stop()
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--server", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5160)
    p.add_argument("--user", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--name", default="Test phone")
    p.add_argument("--local-port", type=int, default=5170)
    p.add_argument("--rtp-min", type=int, default=17700)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("call")
    c.add_argument("target")
    w = sub.add_parser("answer")
    w.add_argument("--answer-after", type=float, default=1.0)
    for sp in (c, w):
        sp.add_argument("--play")
        sp.add_argument("--play-delay", type=float, default=1.0)
        sp.add_argument("--record")
        sp.add_argument("--seconds", type=float, default=30)
        sp.add_argument("--ring", type=float, default=30)
        sp.add_argument("--dtmf", default="")
        sp.add_argument("--dtmf-delay", type=float, default=2.0)
    args = p.parse_args()
    logging.basicConfig(level=logging.WARNING)
    sys.exit(asyncio.run(main(args)))
