"""HTTP Digest authentication for SIP (RFC 2617 / RFC 8760)."""

from __future__ import annotations

import hashlib
import secrets

from .message import split_values

_HASHES = {
    "MD5": hashlib.md5,
    "SHA-256": hashlib.sha256,
    "SHA-512-256": lambda b=b"": hashlib.new("sha512_256", b),
}


def parse_challenge(value: str) -> dict[str, str]:
    """Parse 'Digest realm="x", nonce="y", ...' into a dict (keys lower-case)."""
    scheme, _, rest = value.strip().partition(" ")
    params: dict[str, str] = {"scheme": scheme}
    for part in split_values(rest):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        params[k.strip().lower()] = v.strip().strip('"')
    return params


def pick_challenge(values: list[str]) -> dict[str, str] | None:
    """Pick the first Digest challenge whose algorithm we support."""
    for v in values:
        ch = parse_challenge(v)
        if ch.get("scheme", "").lower() != "digest":
            continue
        algo = ch.get("algorithm", "MD5").upper().removesuffix("-SESS")
        if algo in _HASHES:
            return ch
    return None


def _h(algo: str, data: str) -> str:
    fn = _HASHES[algo]
    return fn(data.encode()).hexdigest()


def build_authorization(challenge: dict[str, str], method: str, uri: str,
                        username: str, password: str, nc: int = 1,
                        cnonce: str | None = None, body: bytes = b"") -> str:
    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    algo_raw = challenge.get("algorithm", "MD5")
    algo = algo_raw.upper()
    sess = algo.endswith("-SESS")
    base = algo.removesuffix("-SESS")
    qops = [q.strip() for q in challenge.get("qop", "").split(",") if q.strip()]
    qop = "auth" if "auth" in qops else ("auth-int" if "auth-int" in qops else None)
    cnonce = cnonce or secrets.token_hex(8)
    nc_s = f"{nc:08x}"

    ha1 = _h(base, f"{username}:{realm}:{password}")
    if sess:
        ha1 = _h(base, f"{ha1}:{nonce}:{cnonce}")
    if qop == "auth-int":
        ha2 = _h(base, f"{method}:{uri}:{_HASHES[base](body).hexdigest()}")
    else:
        ha2 = _h(base, f"{method}:{uri}")
    if qop:
        resp = _h(base, f"{ha1}:{nonce}:{nc_s}:{cnonce}:{qop}:{ha2}")
    else:
        resp = _h(base, f"{ha1}:{nonce}:{ha2}")

    parts = [
        f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"',
        f'uri="{uri}"', f'response="{resp}"', f"algorithm={algo_raw}",
    ]
    if "opaque" in challenge:
        parts.append(f'opaque="{challenge["opaque"]}"')
    if qop:
        parts += [f"qop={qop}", f"nc={nc_s}", f'cnonce="{cnonce}"']
    return "Digest " + ", ".join(parts)
