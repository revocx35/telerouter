"""SIP message parsing and serialisation (RFC 3261 subset).

Only what a simple user agent needs: requests/responses, header lists with
compact-form support, name-addr / SIP-URI / Via helpers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

COMPACT = {
    "v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact",
    "l": "content-length", "c": "content-type", "k": "supported", "s": "subject",
    "e": "content-encoding", "o": "event", "u": "allow-events", "r": "refer-to",
    "b": "referred-by", "x": "session-expires",
}

CANONICAL = {
    "via": "Via", "from": "From", "to": "To", "call-id": "Call-ID", "cseq": "CSeq",
    "contact": "Contact", "content-length": "Content-Length", "content-type": "Content-Type",
    "www-authenticate": "WWW-Authenticate", "proxy-authenticate": "Proxy-Authenticate",
    "authorization": "Authorization", "proxy-authorization": "Proxy-Authorization",
    "max-forwards": "Max-Forwards", "record-route": "Record-Route", "route": "Route",
    "user-agent": "User-Agent", "allow": "Allow", "supported": "Supported",
    "expires": "Expires", "min-expires": "Min-Expires", "server": "Server",
    "session-expires": "Session-Expires", "rseq": "RSeq", "rack": "RAck",
    "p-asserted-identity": "P-Asserted-Identity", "remote-party-id": "Remote-Party-ID",
}

# Headers whose values may be comma-joined on one line.
LIST_HEADERS = {"via", "route", "record-route", "contact", "allow", "supported", "require"}


class SipParseError(ValueError):
    pass


def canonical_name(name: str) -> str:
    n = name.strip().lower()
    if len(n) == 1:
        n = COMPACT.get(n, n)
    return CANONICAL.get(n) or "-".join(p.capitalize() for p in n.split("-"))


def split_values(value: str) -> list[str]:
    """Split a comma separated header value, respecting quotes and <...>."""
    out, buf, quote, angle = [], [], False, 0
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and quote and i + 1 < len(value):
            buf.append(value[i:i + 2])
            i += 2
            continue
        if ch == '"':
            quote = not quote
        elif not quote and ch == "<":
            angle += 1
        elif not quote and ch == ">":
            angle = max(0, angle - 1)
        if ch == "," and not quote and angle == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    if buf and "".join(buf).strip():
        out.append("".join(buf).strip())
    return out


def parse_params(text: str) -> dict[str, str | None]:
    """Parse ';a=b;c' style parameters (leading ';' optional)."""
    params: dict[str, str | None] = {}
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            params[k.strip().lower()] = v.strip().strip('"')
        else:
            params[part.lower()] = None
    return params


def format_params(params: dict[str, str | None]) -> str:
    return "".join(f";{k}" if v is None else f";{k}={v}" for k, v in params.items())


@dataclass
class SipUri:
    scheme: str = "sip"
    user: str | None = None
    password: str | None = None
    host: str = ""
    port: int | None = None
    params: dict[str, str | None] = field(default_factory=dict)
    headers: str = ""

    _RE = re.compile(r"^(sips?|tel):(?:([^:@;?]*)(?::([^@;?]*))?@)?(\[[^\]]+\]|[^:;?]+)(?::(\d+))?([^?]*)(?:\?(.*))?$", re.I)

    @classmethod
    def parse(cls, text: str) -> "SipUri":
        m = cls._RE.match(text.strip())
        if not m:
            raise SipParseError(f"bad SIP URI: {text!r}")
        scheme, user, pw, host, port, params, headers = m.groups()
        return cls(scheme=scheme.lower(), user=user or None, password=pw, host=host,
                   port=int(port) if port else None, params=parse_params(params or ""),
                   headers=headers or "")

    def __str__(self) -> str:
        s = f"{self.scheme}:"
        if self.user is not None:
            s += self.user
            if self.password is not None:
                s += f":{self.password}"
            s += "@"
        s += self.host
        if self.port:
            s += f":{self.port}"
        s += format_params(self.params)
        if self.headers:
            s += f"?{self.headers}"
        return s

    @property
    def transport(self) -> str:
        return (self.params.get("transport") or "udp").lower()


@dataclass
class NameAddr:
    uri: str
    display: str = ""
    params: dict[str, str | None] = field(default_factory=dict)

    @classmethod
    def parse(cls, text: str) -> "NameAddr":
        text = text.strip()
        lt = _find_unquoted(text, "<")
        if lt >= 0:
            gt = text.index(">", lt)
            display = text[:lt].strip().strip('"')
            uri = text[lt + 1:gt].strip()
            params = parse_params(text[gt + 1:])
        else:
            display = ""
            if ";" in text:
                uri, rest = text.split(";", 1)
                params = parse_params(rest)
            else:
                uri, params = text, {}
        return cls(uri=uri.strip(), display=display, params=params)

    @property
    def tag(self) -> str | None:
        return self.params.get("tag")

    @property
    def sip_uri(self) -> SipUri:
        return SipUri.parse(self.uri)

    def __str__(self) -> str:
        d = f'"{self.display}" ' if self.display else ""
        return f"{d}<{self.uri}>{format_params(self.params)}"


def _find_unquoted(text: str, ch: str) -> int:
    quote = False
    for i, c in enumerate(text):
        if c == '"':
            quote = not quote
        elif c == ch and not quote:
            return i
    return -1


@dataclass
class Via:
    transport: str
    host: str
    port: int | None
    params: dict[str, str | None]

    @classmethod
    def parse(cls, text: str) -> "Via":
        m = re.match(r"\s*SIP\s*/\s*2\.0\s*/\s*(\w+)\s+([^;]+)(.*)$", text, re.I)
        if not m:
            raise SipParseError(f"bad Via: {text!r}")
        transport, hostport, params = m.groups()
        hostport = hostport.strip()
        if hostport.startswith("["):
            end = hostport.index("]")
            host, rest = hostport[: end + 1], hostport[end + 1:]
            port = int(rest[1:]) if rest.startswith(":") else None
        elif ":" in hostport:
            host, p = hostport.rsplit(":", 1)
            port = int(p)
        else:
            host, port = hostport, None
        return cls(transport.upper(), host, port, parse_params(params))

    @property
    def branch(self) -> str | None:
        return self.params.get("branch")

    def __str__(self) -> str:
        hp = self.host + (f":{self.port}" if self.port else "")
        return f"SIP/2.0/{self.transport} {hp}{format_params(self.params)}"


class SipMessage:
    def __init__(self, *, method: str | None = None, uri: str | None = None,
                 status: int | None = None, reason: str = "",
                 headers: list[tuple[str, str]] | None = None, body: bytes = b""):
        self.method = method.upper() if method else None
        self.uri = uri
        self.status = status
        self.reason = reason
        self.headers: list[list[str]] = [[canonical_name(k), v] for k, v in (headers or [])]
        self.body = body

    # -- construction helpers -------------------------------------------------
    @property
    def is_request(self) -> bool:
        return self.method is not None

    def get(self, name: str, default: str | None = None) -> str | None:
        key = canonical_name(name).lower()
        for k, v in self.headers:
            if k.lower() == key:
                return v
        return default

    def get_all(self, name: str) -> list[str]:
        key = canonical_name(name).lower()
        return [v for k, v in self.headers if k.lower() == key]

    def values(self, name: str) -> list[str]:
        """All values of a (possibly comma-joined) list header, in order."""
        out: list[str] = []
        for v in self.get_all(name):
            out.extend(split_values(v))
        return out

    def set(self, name: str, value: str) -> None:
        cname = canonical_name(name)
        key = cname.lower()
        for i, (k, _) in enumerate(self.headers):
            if k.lower() == key:
                self.headers[i][1] = value
                self.headers = [h for j, h in enumerate(self.headers) if j == i or h[0].lower() != key]
                return
        self.headers.append([cname, value])

    def add(self, name: str, value: str) -> None:
        self.headers.append([canonical_name(name), value])

    def prepend(self, name: str, value: str) -> None:
        self.headers.insert(0, [canonical_name(name), value])

    def remove(self, name: str) -> None:
        key = canonical_name(name).lower()
        self.headers = [h for h in self.headers if h[0].lower() != key]

    # -- common accessors -----------------------------------------------------
    @property
    def call_id(self) -> str:
        return self.get("Call-ID", "") or ""

    @property
    def cseq(self) -> tuple[int, str]:
        raw = (self.get("CSeq") or "0 UNKNOWN").split()
        return int(raw[0]), raw[1].upper() if len(raw) > 1 else ""

    @property
    def from_(self) -> NameAddr:
        return NameAddr.parse(self.get("From", "") or "")

    @property
    def to(self) -> NameAddr:
        return NameAddr.parse(self.get("To", "") or "")

    @property
    def top_via(self) -> Via | None:
        vias = self.values("Via")
        return Via.parse(vias[0]) if vias else None

    @property
    def branch(self) -> str | None:
        via = self.top_via
        return via.branch if via else None

    @property
    def content_type(self) -> str:
        return (self.get("Content-Type") or "").split(";")[0].strip().lower()

    # -- wire format ----------------------------------------------------------
    def to_bytes(self) -> bytes:
        if self.is_request:
            start = f"{self.method} {self.uri} SIP/2.0"
        else:
            start = f"SIP/2.0 {self.status} {self.reason}"
        lines = [start]
        for k, v in self.headers:
            if k.lower() == "content-length":
                continue
            lines.append(f"{k}: {v}")
        lines.append(f"Content-Length: {len(self.body)}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode() + self.body

    def __bytes__(self) -> bytes:
        return self.to_bytes()

    def summary(self) -> str:
        if self.is_request:
            return f"{self.method} {self.uri}"
        return f"{self.status} {self.reason} ({self.cseq[1]})"

    @classmethod
    def parse(cls, data: bytes) -> "SipMessage":
        head, sep, body = data.partition(b"\r\n\r\n")
        if not sep:
            head, sep, body = data.partition(b"\n\n")
        try:
            text = head.decode("utf-8")
        except UnicodeDecodeError:
            text = head.decode("latin-1")
        raw_lines = re.split(r"\r?\n", text)
        if not raw_lines or not raw_lines[0].strip():
            raise SipParseError("empty message")
        # unfold continuation lines
        lines: list[str] = []
        for line in raw_lines[1:]:
            if line[:1] in (" ", "\t") and lines:
                lines[-1] += " " + line.strip()
            elif line:
                lines.append(line)
        start = raw_lines[0].strip()
        if start.startswith("SIP/2.0"):
            parts = start.split(" ", 2)
            if len(parts) < 2 or not parts[1].isdigit():
                raise SipParseError(f"bad status line: {start!r}")
            msg = cls(status=int(parts[1]), reason=parts[2] if len(parts) > 2 else "")
        else:
            parts = start.split(" ")
            if len(parts) != 3 or parts[2] != "SIP/2.0":
                raise SipParseError(f"bad request line: {start!r}")
            msg = cls(method=parts[0], uri=parts[1])
        for line in lines:
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            msg.headers.append([canonical_name(k), v.strip()])
        length = msg.get("Content-Length")
        if length is not None and length.strip().isdigit():
            body = body[: int(length.strip())]
        msg.body = body
        return msg
