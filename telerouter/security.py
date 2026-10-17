"""Passwords (argon2id), random tokens (stored as hashes), TOTP, login throttling, server-side sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 12
_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)   # argon2id
# Verifying against this keeps the timing of "no such user" equal to "wrong password".
_DUMMY_HASH = _hasher.hash(secrets.token_hex(16))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, stored: str | None) -> bool:
    try:
        return _hasher.verify(stored or _DUMMY_HASH, password) and bool(stored)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_problem(password: str, username: str = "") -> str | None:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"use at least {MIN_PASSWORD_LENGTH} characters"
    if len(password) > 256:
        return "too long"
    if username and username.lower() in password.lower():
        return "the password must not contain the user name"
    if len(set(password)) < 5:
        return "too repetitive"
    return None


# -- tokens -----------------------------------------------------------------------------------
def new_token(prefix: str) -> str:
    """256-bit random token with a readable prefix (e.g. "trc_" client, "trm_" MCP)."""
    return prefix + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_matches(token: str, stored_hash: str) -> bool:
    return bool(stored_hash) and hmac.compare_digest(token_hash(token), stored_hash)


def bearer(headers) -> str:
    h = headers.get("authorization") or ""
    return h[7:].strip() if h[:7].lower() == "bearer " else ""


def new_enrollment_code() -> str:
    """Short code typed/pasted once on the client (40 bits, 15 min, single use)."""
    raw = base64.b32encode(secrets.token_bytes(5)).decode().rstrip("=")
    return f"{raw[:4]}-{raw[4:]}"


# -- TOTP -------------------------------------------------------------------------------------
def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name="telerouter")


class TotpReplay:
    """Remembers used (secret, step) pairs so a code works once."""

    def __init__(self) -> None:
        self._used: dict[str, float] = {}

    def verify(self, secret: str, code: str, now: float | None = None) -> bool:
        code = "".join(code.split())
        if not secret or not code.isdigit() or len(code) != 6:
            return False
        now = now or time.time()
        totp = pyotp.TOTP(secret)
        for drift in (-1, 0, 1):
            t = now + drift * 30
            if hmac.compare_digest(totp.at(t), code):
                key = f"{token_hash(secret)}:{int(t // 30)}"
                if key in self._used:
                    return False
                self._used = {k: v for k, v in self._used.items() if v > now - 120}
                self._used[key] = now
                return True
        return False


# -- login throttle -----------------------------------------------------------------------------
class Locked(Exception):
    def __init__(self, retry_after: float):
        super().__init__(f"locked for {retry_after:.0f} s")
        self.retry_after = retry_after


class LoginThrottle:
    """Brute-force protection for logins, setup codes and enrollment codes (in memory).

    Per key (client IP, and separately the account name): after THRESHOLD failures every further
    attempt is locked for a time that doubles (30 s ... 1 h). Global: after GLOBAL_THRESHOLD
    failures from everywhere together, one attempt per lock period. `begin()` charges the attempt
    *before* the secret is checked; `success()` refunds it, so parallel requests can't race a lock.
    """

    THRESHOLD = 5
    BASE, CAP = 30.0, 3600.0
    GLOBAL_THRESHOLD = 50
    GLOBAL_BASE, GLOBAL_CAP = 30.0, 900.0
    IDLE_RESET = 24 * 3600.0

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._keys: dict[str, list[float]] = {}    # key -> [failures, locked_until, last_attempt]
        self._global_fails = 0
        self._global_locked_until = 0.0

    @staticmethod
    def _backoff(n: int, threshold: int, base: float, cap: float) -> float:
        return min(base * 2 ** max(0, n - threshold - 1), cap)

    def _state(self, key: str, now: float) -> list[float] | None:
        st = self._keys.get(key)
        if st and st[1] <= now and now - st[2] > self.IDLE_RESET:
            del self._keys[key]
            return None
        return st

    def check(self, *keys: str) -> None:
        now = self._clock()
        for key in keys:
            st = self._state(key, now)
            if st and st[1] > now:
                raise Locked(st[1] - now)
        if self._global_locked_until > now:
            raise Locked(self._global_locked_until - now)

    def begin(self, *keys: str) -> None:
        """Charge one attempt for every key. Raises Locked when any of them is locked."""
        now = self._clock()
        if len(self._keys) > 20_000:
            self._keys = {k: v for k, v in self._keys.items() if v[1] > now or now - v[2] < self.IDLE_RESET}
        self.check(*keys)
        self._global_fails += 1
        if self._global_fails > self.GLOBAL_THRESHOLD:
            self._global_locked_until = now + self._backoff(
                self._global_fails, self.GLOBAL_THRESHOLD, self.GLOBAL_BASE, self.GLOBAL_CAP)
        for key in keys:
            st = self._state(key, now) or [0, 0.0, now]
            st[0] += 1
            st[2] = now
            if st[0] > self.THRESHOLD:
                st[1] = now + self._backoff(int(st[0]), self.THRESHOLD, self.BASE, self.CAP)
            self._keys[key] = st

    def success(self, *keys: str) -> None:
        for key in keys:
            self._keys.pop(key, None)
        self._global_fails = 0
        self._global_locked_until = 0.0


# -- sessions -------------------------------------------------------------------------------------
COOKIE = "__Host-telerouter"


@dataclass
class Session:
    id_hash: str
    username: str
    csrf: str
    created: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    pending_totp: bool = False      # password OK, TOTP still to enter


class SessionStore:
    """Server-side sessions: the cookie holds a random id, we keep only its hash.

    Logout and password changes really invalidate (the entry is deleted). Idle and absolute timeouts
    apply. A restart logs everyone out, which is fine for an admin UI.
    """

    MAX = 200

    def __init__(self, idle_s: float, absolute_s: float, clock=time.time):
        self.idle_s = idle_s
        self.absolute_s = absolute_s
        self._clock = clock
        self._sessions: dict[str, Session] = {}

    def create(self, username: str, pending_totp: bool = False) -> tuple[str, Session]:
        self._sweep()
        sid = secrets.token_urlsafe(32)
        s = Session(token_hash(sid), username, secrets.token_urlsafe(24), pending_totp=pending_totp,
                    created=self._clock(), last_seen=self._clock())
        self._sessions[s.id_hash] = s
        while len(self._sessions) > self.MAX:            # oldest first
            oldest = min(self._sessions.values(), key=lambda x: x.last_seen)
            del self._sessions[oldest.id_hash]
        return sid, s

    def get(self, sid: str | None, touch: bool = True) -> Session | None:
        if not sid:
            return None
        s = self._sessions.get(token_hash(sid))
        if not s:
            return None
        now = self._clock()
        if now - s.last_seen > self.idle_s or now - s.created > self.absolute_s:
            del self._sessions[s.id_hash]
            return None
        if touch:
            s.last_seen = now
        return s

    def delete(self, sid: str | None) -> None:
        if sid:
            self._sessions.pop(token_hash(sid), None)

    def clear(self) -> None:
        self._sessions.clear()

    def _sweep(self) -> None:
        now = self._clock()
        for k, s in list(self._sessions.items()):
            if now - s.last_seen > self.idle_s or now - s.created > self.absolute_s:
                del self._sessions[k]
