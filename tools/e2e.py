#!/usr/bin/env python3
"""End-to-end check against a running stack (CI): setup, speaker enrollment, a spoken request.

  docker compose up -d   (with TELEROUTER_SETUP_CODE=E2E-CODE1)
  python tools/e2e.py --server https://127.0.0.1:8443 --setup-code E2E-CODE1

Creates the admin, synthesizes "Hey Jarvis." + a question with Piper (through telerouter), enrolls a
speaker with the client package from this repo, plays the WAV as its microphone and checks the server log:
wake word detected, utterance transcribed, an answer spoken (no agent: "No assistant is set up yet.").
Uses only the standard library plus the client package (installed from ./client).
"""

from __future__ import annotations

import argparse
import http.cookiejar
import io
import json
import os
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave

PW = "e2e correct horse battery"


class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()),
                                              urllib.request.HTTPCookieProcessor(self.cj))
        self.csrf = ""

    def call(self, method: str, path: str, body=None, raw: bool = False):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "X-CSRF-Token": self.csrf})
        with self.op.open(req, timeout=120) as r:
            data = r.read()
        return data if raw else json.loads(data)


def tts(api: Api, text: str) -> bytes:
    w = wave.open(io.BytesIO(api.call("POST", "/api/speech/preview", {"text": text}, raw=True)))
    return w.readframes(w.getnframes())


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--server", default="https://127.0.0.1:8443")
    p.add_argument("--setup-code", required=True)
    a = p.parse_args()
    api = Api(a.server)
    for _ in range(90):
        try:
            if api.call("GET", "/api/speech")["health"] == {"stt": True, "tts": True, "wake": True}:
                break
        except Exception:
            pass
        try:
            s = api.call("GET", "/api/session")
            if s["setup_needed"]:
                api.call("POST", "/api/setup", {"setup_code": a.setup_code, "username": "admin", "password": PW})
            api.csrf = api.call("GET", "/api/session")["csrf"]
        except Exception as e:
            print("waiting:", e)
        time.sleep(2)
    health = api.call("GET", "/api/speech")["health"]
    print("speech services:", health)
    assert all(health.values()), health

    silence = lambda s: bytes(int(32000 * s) // 2 * 2)          # noqa: E731
    pcm = silence(1) + tts(api, "Hey Jarvis.") + silence(1.2) + tts(api, "What is the capital of France?") + silence(5)
    tmp = tempfile.mkdtemp()
    wav = os.path.join(tmp, "in.wav")
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)

    enroll = api.call("POST", "/api/clients/enroll", {"name": "CI speaker"})
    env = {**os.environ, "XDG_CONFIG_HOME": tmp}
    cli = [sys.executable, "-m", "telerouter_client"]
    subprocess.run(cli + ["enroll", "--server", enroll["server"].replace("testserver", "127.0.0.1"),
                          "--ca-sha256", enroll["ca_sha256"], "--code", enroll["code"]], check=True, env=env)
    out = os.path.join(tmp, "out.wav")
    subprocess.run(cli + ["run", "--input-wav", wav, "--output-wav", out, "--duration", "25"], check=True, env=env)

    logs = "\n".join(r["msg"] for r in api.call("GET", "/api/logs")["records"])
    print(logs[-2500:])
    assert "wake word hey_jarvis" in logs, "wake word not detected"
    assert "heard" in logs and "capital" in logs.lower(), "utterance not transcribed"
    with wave.open(out) as w:
        played = w.readframes(w.getnframes())
    loud = sum(1 for i in range(0, len(played), 3200) if max(played[i:i + 3200] or b"\0") > 20)
    assert loud > 10, "nothing was played back"
    print("e2e OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
