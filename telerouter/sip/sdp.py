"""Minimal SDP (RFC 4566) parsing/building for a single audio stream."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Codec:
    name: str        # "PCMA" / "PCMU"
    pt: int
    rate: int = 8000


PCMU = Codec("PCMU", 0)
PCMA = Codec("PCMA", 8)
SUPPORTED = {"PCMU": PCMU, "PCMA": PCMA}
STATIC_PT = {0: "PCMU", 8: "PCMA"}
DTMF_PT = 101


@dataclass
class SdpMedia:
    kind: str
    port: int
    proto: str
    fmts: list[str]
    rtpmap: dict[int, tuple[str, int]] = field(default_factory=dict)
    fmtp: dict[int, str] = field(default_factory=dict)
    connection: str | None = None
    direction: str | None = None
    attrs: list[str] = field(default_factory=list)

    def codec_name(self, pt: int) -> str | None:
        if pt in self.rtpmap:
            return self.rtpmap[pt][0].upper()
        return STATIC_PT.get(pt)


@dataclass
class Sdp:
    connection: str | None = None
    direction: str | None = None
    media: list[SdpMedia] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str | bytes) -> "Sdp":
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        sdp = cls()
        cur: SdpMedia | None = None
        for raw in text.replace("\r\n", "\n").split("\n"):
            line = raw.strip()
            if len(line) < 2 or line[1] != "=":
                continue
            t, v = line[0], line[2:]
            if t == "m":
                parts = v.split()
                if len(parts) < 3:
                    cur = None
                    continue
                port = int(parts[1].split("/")[0])
                cur = SdpMedia(kind=parts[0], port=port, proto=parts[2], fmts=parts[3:])
                sdp.media.append(cur)
            elif t == "c":
                addr = v.split()[-1].split("/")[0]
                if cur:
                    cur.connection = addr
                else:
                    sdp.connection = addr
            elif t == "a":
                if v in ("sendrecv", "sendonly", "recvonly", "inactive"):
                    if cur:
                        cur.direction = v
                    else:
                        sdp.direction = v
                elif cur and v.startswith("rtpmap:"):
                    pt_s, _, enc = v[7:].partition(" ")
                    name, _, rest = enc.partition("/")
                    rate = rest.split("/")[0]
                    if pt_s.isdigit():
                        cur.rtpmap[int(pt_s)] = (name, int(rate) if rate.isdigit() else 8000)
                elif cur and v.startswith("fmtp:"):
                    pt_s, _, params = v[5:].partition(" ")
                    if pt_s.isdigit():
                        cur.fmtp[int(pt_s)] = params
                elif cur:
                    cur.attrs.append(v)
        return sdp

    def audio(self) -> SdpMedia | None:
        for m in self.media:
            if m.kind == "audio" and m.port != 0:
                return m
        for m in self.media:
            if m.kind == "audio":
                return m
        return None

    def audio_address(self) -> tuple[str, int] | None:
        m = self.audio()
        if not m:
            return None
        host = m.connection or self.connection
        if not host:
            return None
        return host, m.port

    def audio_direction(self) -> str:
        m = self.audio()
        return (m.direction if m and m.direction else None) or self.direction or "sendrecv"


@dataclass
class Negotiated:
    codec: Codec
    remote_pt: int
    dtmf_pt: int | None


def negotiate(offer: Sdp, preferred: list[str]) -> Negotiated | None:
    """Pick a codec from an offer. Preference: our list order."""
    m = offer.audio()
    if not m or m.port == 0 or "SAVP" in m.proto.upper():
        return None
    offered: dict[str, int] = {}
    dtmf = None
    for f in m.fmts:
        if not f.isdigit():
            continue
        pt = int(f)
        name = m.codec_name(pt)
        if not name:
            continue
        if name == "TELEPHONE-EVENT" and m.rtpmap.get(pt, ("", 8000))[1] == 8000:
            dtmf = pt
        elif name in SUPPORTED and name not in offered:
            offered[name] = pt
    for name in preferred:
        if name in offered:
            return Negotiated(SUPPORTED[name], offered[name], dtmf)
    return None


def negotiate_answer(answer: Sdp) -> Negotiated | None:
    """Interpret an SDP answer to our offer (first supported format wins)."""
    m = answer.audio()
    if not m or m.port == 0:
        return None
    dtmf = None
    chosen = None
    for f in m.fmts:
        if not f.isdigit():
            continue
        pt = int(f)
        name = m.codec_name(pt)
        if name == "TELEPHONE-EVENT":
            dtmf = pt
        elif name in SUPPORTED and chosen is None:
            chosen = (SUPPORTED[name], pt)
    if not chosen:
        return None
    return Negotiated(chosen[0], chosen[1], dtmf)


def build(ip: str, port: int, codecs: list[tuple[Codec, int]], dtmf_pt: int | None,
          session_id: int, version: int, direction: str = "sendrecv") -> bytes:
    """Build an audio-only SDP body. `codecs` is a list of (codec, payload type)."""
    fmts = [str(pt) for _, pt in codecs] + ([str(dtmf_pt)] if dtmf_pt is not None else [])
    lines = [
        "v=0",
        f"o=telerouter {session_id} {version} IN IP4 {ip}",
        "s=telerouter",
        f"c=IN IP4 {ip}",
        "t=0 0",
        f"m=audio {port} RTP/AVP {' '.join(fmts)}",
    ]
    for codec, pt in codecs:
        lines.append(f"a=rtpmap:{pt} {codec.name}/{codec.rate}")
    if dtmf_pt is not None:
        lines.append(f"a=rtpmap:{dtmf_pt} telephone-event/8000")
        lines.append(f"a=fmtp:{dtmf_pt} 0-16")
    lines += ["a=ptime:20", "a=maxptime:40", f"a={direction}"]
    return ("\r\n".join(lines) + "\r\n").encode()


def new_session_id() -> int:
    return int(time.time() * 1000) & 0x7FFFFFFF
