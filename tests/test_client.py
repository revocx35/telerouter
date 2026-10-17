"""Speaker client pieces that run without sound hardware."""

import hashlib
import ssl
import threading

import numpy as np
import pytest

from telerouter_client.audio import Player, Resampler


def test_resampler_keeps_pitch_and_length():
    for src, dst, f in ((48000, 16000, 440), (16000, 44100, 1000), (44100, 16000, 300)):
        r = Resampler(src, dst)
        t = np.arange(src) / src
        x = np.sin(2 * np.pi * f * t) * 10000
        block = src // 50
        out = np.concatenate([r(x[i:i + block]) for i in range(0, src, block)])
        assert abs(len(out) - dst) <= 2
        spec = np.abs(np.fft.rfft(out[dst // 10: dst - dst // 10]))
        peak = np.argmax(spec) * dst / (len(out) - 2 * (dst // 10))
        assert abs(peak - f) < 5


def test_player_buffer_flush_and_cap():
    p = Player()
    p.feed(b"\x01\x00" * 100)
    assert p.take(100) == b"\x01\x00" * 50
    p.flush()
    assert p.take(10) == bytes(10)
    p.feed(bytes(16000 * 2 * 10))                # 10 s: capped to 4 s
    assert p.queued_s <= 4.0

    def writer():
        for _ in range(1000):
            p.feed(b"\x00\x01" * 160)
    th = threading.Thread(target=writer)
    th.start()
    for _ in range(1000):
        p.take(320)
    th.join()


def test_enrollment_refuses_a_ca_with_the_wrong_fingerprint(monkeypatch, tmp_path):
    from telerouter import tls
    from telerouter_client import net
    files = tls.ensure(str(tmp_path), [])

    class Resp:
        def __init__(self, data):
            self.data = data

        def read(self, n):
            return self.data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(net.urllib.request, "urlopen", lambda *a, **k: Resp(files.ca_pem))
    good = hashlib.sha256(ssl.PEM_cert_to_DER_cert(files.ca_pem.decode())).hexdigest()
    assert net.fetch_ca("https://x:8443", good).startswith("-----BEGIN CERTIFICATE")
    with pytest.raises(SystemExit):
        net.fetch_ca("https://x:8443", "00" * 32)
