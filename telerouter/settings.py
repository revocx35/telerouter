"""TELEROUTER_* environment variables (deployment settings; everything else lives in /data/config.json)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import __version__


def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"TELEROUTER_{name}", default).strip()


def _int(name: str, default: int) -> int:
    v = _env(name)
    return int(v) if v else default


def _bool(name: str, default: bool) -> bool:
    v = _env(name).lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


@dataclass
class Settings:
    version: str = __version__
    data_dir: str = field(default_factory=lambda: _env("DATA_DIR", "/data"))
    bind: str = field(default_factory=lambda: _env("BIND", "0.0.0.0"))
    https_port: int = field(default_factory=lambda: _int("HTTPS_PORT", 8443))
    # loopback-only HTTP listener used by the SSH reverse tunnels to agents (MCP + nothing else)
    internal_port: int = field(default_factory=lambda: _int("INTERNAL_PORT", 47700))
    # extra DNS names / IPs for the self-signed certificate (comma separated)
    tls_names: list[str] = field(default_factory=lambda: [x.strip() for x in _env("TLS_NAMES").split(",") if x.strip()])
    tls_cert: str = field(default_factory=lambda: _env("TLS_CERT"))     # optional own certificate
    tls_key: str = field(default_factory=lambda: _env("TLS_KEY"))
    sip_port: int = field(default_factory=lambda: _int("SIP_PORT", 5080))
    sip_bind: str = field(default_factory=lambda: _env("SIP_BIND", "0.0.0.0"))
    rtp_port_min: int = field(default_factory=lambda: _int("RTP_PORT_MIN", 18000))
    rtp_port_max: int = field(default_factory=lambda: _int("RTP_PORT_MAX", 18199))
    advertise_ip: str = field(default_factory=lambda: _env("ADVERTISE_IP"))
    whisper_uri: str = field(default_factory=lambda: _env("WHISPER_URI", "tcp://127.0.0.1:10300"))
    piper_uri: str = field(default_factory=lambda: _env("PIPER_URI", "tcp://127.0.0.1:10200"))
    wake_uri: str = field(default_factory=lambda: _env("WAKE_URI", "tcp://127.0.0.1:10400"))
    # reverse proxies whose X-Forwarded-For is trusted ("" = none, "private" = RFC 1918 + loopback)
    trusted_proxies: str = field(default_factory=lambda: _env("TRUSTED_PROXIES", ""))
    setup_code: str = field(default_factory=lambda: _env("SETUP_CODE"))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    max_clients: int = field(default_factory=lambda: _int("MAX_CLIENTS", 32))
    # test hooks: run without the Wyoming services (fake STT/TTS/wake) - never in production
    fake_speech: bool = field(default_factory=lambda: _bool("FAKE_SPEECH", False))
