"""Password hashing, tokens, TOTP, throttle and server-side sessions (pure logic, fake clocks)."""

import pyotp
import pytest

from telerouter import security


def test_argon2id_hash_and_verify():
    h = security.hash_password("correct horse battery")
    assert h.startswith("$argon2id$")
    assert security.verify_password("correct horse battery", h)
    assert not security.verify_password("wrong password!!", h)
    assert not security.verify_password("anything", None)          # no user: same code path, still False
    assert not security.verify_password("x", "garbage")


def test_password_rules():
    assert security.password_problem("short") is not None
    assert security.password_problem("aaaaaaaaaaaaaa") is not None              # too repetitive
    assert security.password_problem("admin-password-1", "admin") is not None   # contains the user name
    assert security.password_problem("correct horse battery", "admin") is None


def test_tokens_are_random_and_stored_as_hashes():
    a, b = security.new_token("trc_"), security.new_token("trc_")
    assert a != b and a.startswith("trc_") and len(a) > 40
    h = security.token_hash(a)
    assert a not in h and security.token_matches(a, h) and not security.token_matches(b, h)
    assert not security.token_matches(a, "")
    assert security.bearer({"authorization": "Bearer abc"}) == "abc"
    assert security.bearer({"authorization": "Basic abc"}) == ""


def test_totp_code_works_once():
    secret = security.new_totp_secret()
    r = security.TotpReplay()
    now = 1_800_000_000
    code = pyotp.TOTP(secret).at(now)
    assert r.verify(secret, code, now)
    assert not r.verify(secret, code, now + 1)          # replay refused
    assert not r.verify(secret, "000000", now)
    assert not r.verify(secret, "12345", now)


def test_throttle_locks_per_key_and_refunds_on_success():
    t = [0.0]
    th = security.LoginThrottle(clock=lambda: t[0])
    for _ in range(security.LoginThrottle.THRESHOLD):
        th.begin("ip:1", "user:admin")
    th.begin("ip:1", "user:admin")                    # the 6th attempt is charged and locks
    with pytest.raises(security.Locked) as e:
        th.begin("ip:1", "user:admin")
    assert e.value.retry_after > 0
    with pytest.raises(security.Locked):               # the account is locked from another IP too
        th.begin("ip:2", "user:admin")
    th.begin("ip:3", "user:other")                     # other accounts are not
    t[0] += 3600
    th.begin("ip:1", "user:admin")                     # lock expired
    th.success("ip:1", "user:admin")
    th.begin("ip:1", "user:admin")


def test_global_throttle_bounds_distributed_guessing():
    t = [0.0]
    th = security.LoginThrottle(clock=lambda: t[0])
    for i in range(security.LoginThrottle.GLOBAL_THRESHOLD + 1):
        th.begin(f"ip:{i}")
    with pytest.raises(security.Locked):
        th.begin("ip:new")


def test_sessions_idle_absolute_and_logout():
    t = [1000.0]
    store = security.SessionStore(idle_s=60, absolute_s=300, clock=lambda: t[0])
    sid, s = store.create("admin")
    assert store.get(sid) is s and len(s.csrf) > 20
    assert store.get("forged") is None
    t[0] += 59
    assert store.get(sid)                               # touched: idle timer restarts
    t[0] += 61
    assert store.get(sid) is None                       # idle timeout
    sid, _ = store.create("admin")
    for _ in range(6):
        t[0] += 55
        store.get(sid)
    assert store.get(sid) is None                       # absolute timeout despite activity
    sid, _ = store.create("admin")
    store.delete(sid)
    assert store.get(sid) is None                       # logout really invalidates
