"""SIP user agent: account registration and call (dialog) handling."""

from __future__ import annotations

import asyncio
import logging
import secrets
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..media.rtp import PortAllocator, RtpEndpoint
from . import sdp as sdpmod
from .auth import build_authorization, pick_challenge
from .message import NameAddr, SipMessage, SipUri
from .stack import T1, T2, SipStack, new_branch, new_call_id, new_tag

log = logging.getLogger("telerouter.sip")

ALLOW = "INVITE, ACK, CANCEL, BYE, OPTIONS, INFO, UPDATE, NOTIFY"
PREFERRED_CODECS = ["PCMA", "PCMU"]


@dataclass
class AccountConfig:
    id: str
    server: str
    port: int
    username: str
    password: str
    auth_username: str = ""
    domain: str = ""
    display_name: str = ""
    expires: int = 300
    contact_user: str = ""


class SipError(Exception):
    pass


class Account:
    def __init__(self, ua: "UserAgent", cfg: AccountConfig):
        self.ua = ua
        self.cfg = cfg
        self.state = "stopped"
        self.error: str | None = None
        self.registered_until: float | None = None
        self.last_register: float | None = None
        self.local_ip = "0.0.0.0"
        self._call_id = new_call_id()
        self._tag = new_tag()
        self._cseq = 0
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()

    # -- identity ----------------------------------------------------------------
    @property
    def domain(self) -> str:
        return self.cfg.domain or self.cfg.server

    @property
    def aor(self) -> str:
        return f"sip:{self.cfg.username}@{self.domain}"

    @property
    def contact_user(self) -> str:
        return self.cfg.contact_user or self.cfg.username

    def contact(self) -> str:
        return f"<sip:{self.contact_user}@{self.local_ip}:{self.ua.port}>"

    def from_header(self, tag: str, display: str | None = None) -> str:
        name = self.cfg.display_name if display is None else display
        name = name.replace('"', "").replace("\\", "").strip()
        d = f'"{name}" ' if name else ""
        return f"{d}<{self.aor}>;tag={tag}"

    async def dest(self) -> tuple[str, int]:
        addr = await self.ua.stack.resolve(self.cfg.server, self.cfg.port)
        self.local_ip = self.ua.advertise_ip or SipStack.local_ip_for(addr[0])
        return addr

    def status(self) -> dict:
        return {
            "state": self.state,
            "error": self.error,
            "registered_until": self.registered_until,
            "last_register": self.last_register,
            "contact": self.contact(),
        }

    # -- registration --------------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self.state == "registered":
            try:
                await asyncio.wait_for(self._register(0), 4)
            except Exception:
                pass
        self.state = "stopped"
        self.registered_until = None

    def refresh_now(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            try:
                if self.state != "registered":
                    self.state = "registering"
                granted = await self._register(self.cfg.expires)
                if self.state != "registered":
                    log.info("[%s] registered to %s:%d as %s (expires %ds)", self.cfg.username,
                             self.cfg.server, self.cfg.port, self.cfg.username, granted)
                self.state = "registered"
                self.error = None
                self.registered_until = time.time() + granted
                wait = max(10.0, granted * 0.75)
                backoff = 5.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                msg = str(e) or e.__class__.__name__
                if self.state != "failed" or msg != self.error:
                    log.warning("[%s] registration failed: %s", self.cfg.username, msg)
                self.state = "failed"
                self.error = msg
                self.registered_until = None
                wait = backoff
                backoff = min(backoff * 2, 120.0)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), wait)
            except TimeoutError:
                pass

    async def _register(self, expires: int) -> int:
        dest = await self.dest()
        uri = f"sip:{self.domain}"
        auth: tuple[str, str] | None = None
        challenged = False
        for _ in range(4):
            self._cseq += 1
            req = SipMessage(method="REGISTER", uri=uri)
            req.add("Via", f"SIP/2.0/UDP {self.local_ip}:{self.ua.port};branch={new_branch()};rport")
            req.add("Max-Forwards", "70")
            req.add("From", self.from_header(self._tag))
            req.add("To", f"<{self.aor}>")
            req.add("Call-ID", self._call_id)
            req.add("CSeq", f"{self._cseq} REGISTER")
            req.add("Contact", self.contact())
            req.add("Expires", str(expires))
            req.add("Allow", ALLOW)
            req.add("User-Agent", self.ua.user_agent)
            if auth:
                req.add(*auth)
            resp = await self.ua.stack.request(req, dest)
            self.last_register = time.time()
            if resp.status in (401, 407):
                hdr = "WWW-Authenticate" if resp.status == 401 else "Proxy-Authenticate"
                ch = pick_challenge(resp.get_all(hdr))
                if not ch:
                    raise SipError("unsupported authentication challenge")
                if challenged and ch.get("stale", "").lower() != "true":
                    raise SipError("authentication rejected - check username/password")
                challenged = True
                name = "Authorization" if resp.status == 401 else "Proxy-Authorization"
                auth = (name, build_authorization(ch, "REGISTER", uri,
                                                  self.cfg.auth_username or self.cfg.username,
                                                  self.cfg.password))
                continue
            if resp.status == 423:
                min_exp = int((resp.get("Min-Expires") or "0").strip() or 0)
                expires = max(expires, min_exp)
                continue
            if 200 <= resp.status < 300:
                if expires == 0:
                    return 0
                return self._granted_expires(resp, expires)
            raise SipError(f"{resp.status} {resp.reason}")
        raise SipError("registration did not complete")

    def _granted_expires(self, resp: SipMessage, requested: int) -> int:
        mine = f"sip:{self.contact_user}@{self.local_ip}:{self.ua.port}"
        for c in resp.values("Contact"):
            try:
                na = NameAddr.parse(c)
            except Exception:
                continue
            if na.uri.split(";")[0] == mine and na.params.get("expires"):
                return int(na.params["expires"] or requested)
        exp = resp.get("Expires")
        if exp and exp.strip().isdigit():
            return int(exp.strip())
        return requested

    # -- outgoing calls ----------------------------------------------------------------
    async def dial(self, target: str, display_name: str | None = None,
                   asserted_user: str | None = None) -> "Call":
        """Call `target` (an extension/number on this PBX, or a full SIP URI).

        `display_name` replaces the account's display name in From for this call only.
        `asserted_user` adds a P-Asserted-Identity with that user part (and the display name):
        PBXs that trust it (Asterisk `trust_id_inbound`) show it instead of the extension's own ID.
        """
        if "@" in target:
            uri = target if target.startswith("sip:") else f"sip:{target}"
        else:
            uri = f"sip:{target}@{self.domain}"
        dest = await self.dest()
        call = Call(self.ua, self, "outbound")
        call.from_display = display_name
        if asserted_user:
            name = (display_name or "").replace('"', "").replace("\\", "").strip()
            call.asserted_identity = (f'"{name}" ' if name else "") + f"<sip:{asserted_user}@{self.domain}>"
        call.call_id = new_call_id()
        call.local_tag = new_tag()
        call.local_cseq = 1
        call.local_uri = f"<{self.aor}>"
        call.remote_uri = f"<{uri}>"
        call.remote_target = uri
        call.remote_user = target
        call.peer_addr = dest
        call.local_ip = self.local_ip
        await call.alloc_rtp()
        self.ua.register_call(call)
        call._task = asyncio.create_task(call._run_uac(uri, dest))
        return call


class Call:
    """One SIP dialog with a single audio stream."""

    def __init__(self, ua: "UserAgent", account: Account, direction: str):
        self.ua = ua
        self.account = account
        self.direction = direction
        self.id = secrets.token_hex(4)
        self.state = "init"           # ringing -> active -> ended
        self.call_id = ""
        self.local_tag = ""
        self.remote_tag = ""
        self.local_uri = ""
        self.remote_uri = ""
        self.remote_target = ""
        self.route_set: list[str] = []
        self.local_cseq = secrets.randbelow(10000) + 1
        self.remote_cseq = 0
        self.peer_addr: tuple[str, int] | None = None
        self.local_ip = account.local_ip
        self.remote_user = ""
        self.remote_display = ""
        self.invite: SipMessage | None = None
        self.rtp: RtpEndpoint | None = None
        self.rtp_port = 0
        self.negotiated: sdpmod.Negotiated | None = None
        self.remote_direction = "sendrecv"
        self._sdp_session = sdpmod.new_session_id()
        self._sdp_version = 1
        self.created_at = time.time()
        self.answered_at: float | None = None
        self.ended_at: float | None = None
        self.end_reason = ""
        self.ended = asyncio.Event()
        self.answered = asyncio.Event()
        self.on_dtmf: Callable[[str], None] | None = None
        self.on_end: Callable[["Call"], None] | None = None
        self._acked = asyncio.Event()
        self._ack_bytes: bytes | None = None
        self._invite_req: SipMessage | None = None
        self._task: asyncio.Task | None = None
        self._cancel_requested = False
        self._pending_key: str | None = None
        self.from_display: str | None = None     # outbound: per-call display name in From
        self.asserted_identity: str | None = None  # outbound: P-Asserted-Identity header value

    # -- info ------------------------------------------------------------------------
    @property
    def codec(self) -> str | None:
        return self.negotiated.codec.name if self.negotiated else None

    def info(self) -> dict:
        return {
            "id": self.id,
            "direction": self.direction,
            "state": self.state,
            "remote": self.remote_user,
            "remote_display": self.remote_display,
            "codec": self.codec,
            "created_at": self.created_at,
            "answered_at": self.answered_at,
            "ended_at": self.ended_at,
            "end_reason": self.end_reason,
            "rtp": {
                "local_port": self.rtp_port,
                "remote": f"{self.rtp.remote[0]}:{self.rtp.remote[1]}" if self.rtp and self.rtp.remote else None,
                "rx_packets": self.rtp.rx_packets if self.rtp else 0,
                "tx_packets": self.rtp.tx_packets if self.rtp else 0,
            },
        }

    # -- media ---------------------------------------------------------------------------
    async def alloc_rtp(self) -> None:
        transport, proto, port = await self.ua.ports.open(RtpEndpoint)
        self.rtp = proto
        self.rtp.port = port
        self.rtp_port = port

    def _local_sdp(self, offer: bool = False) -> bytes:
        if offer:
            codecs = [(sdpmod.SUPPORTED[n], sdpmod.SUPPORTED[n].pt) for n in PREFERRED_CODECS]
            dtmf = sdpmod.DTMF_PT
        else:
            n = self.negotiated
            codecs = [(n.codec, n.remote_pt)]
            dtmf = n.dtmf_pt
        body = sdpmod.build(self.local_ip, self.rtp_port, codecs, dtmf, self._sdp_session, self._sdp_version)
        return body

    def _apply_remote_sdp(self, body: bytes, is_offer: bool) -> bool:
        parsed = sdpmod.Sdp.parse(body)
        neg = sdpmod.negotiate(parsed, PREFERRED_CODECS) if is_offer else sdpmod.negotiate_answer(parsed)
        if not neg:
            return False
        if self.negotiated and is_offer:
            # keep payload type mapping stable on re-INVITE if the codec is still offered
            if self.negotiated.codec.name != neg.codec.name:
                self._sdp_version += 1
        self.negotiated = neg
        addr = parsed.audio_address()
        self.remote_direction = parsed.audio_direction()
        if self.rtp and addr:
            if self.remote_direction in ("inactive", "sendonly"):
                self.rtp.set_remote(addr[0], 0)
            else:
                self.rtp.set_remote(addr[0], addr[1])
        return True

    async def send_dtmf(self, digit: str, duration_ms: int = 120) -> None:
        """Send one key press as RFC 4733 telephone-events."""
        if not self.rtp or not self.negotiated or self.negotiated.dtmf_pt is None:
            raise RuntimeError("telephone-event was not negotiated")
        event = "0123456789*#ABCD".index(digit.upper())
        pt = self.negotiated.dtmf_pt
        ts = self.rtp.sender.ts
        steps = max(1, duration_ms // 20)
        for i in range(1, steps + 1):
            self.rtp.send_event(pt, struct.pack("!BBH", event, 10, i * 160), ts, marker=(i == 1))
            await asyncio.sleep(0.02)
        for _ in range(3):   # end packets are sent three times
            self.rtp.send_event(pt, struct.pack("!BBH", event, 0x80 | 10, steps * 160), ts)
        self.rtp.sender.ts = (ts + steps * 160) & 0xFFFFFFFF

    # -- UAS operations --------------------------------------------------------------------
    def _contact(self) -> str:
        return f"<sip:{self.account.contact_user}@{self.local_ip}:{self.ua.port}>"

    def ring(self) -> None:
        if self.state != "ringing" or not self.invite:
            return
        self.ua.stack.respond(self.invite, 180, "Ringing", [("Contact", self._contact())],
                              to_tag=self.local_tag)

    async def answer(self) -> bool:
        if self.state != "ringing" or not self.invite:
            return False
        body = self._local_sdp(offer=self.negotiated is None)
        resp = self.ua.stack.respond(self.invite, 200, "OK", [
            ("Contact", self._contact()), ("Allow", ALLOW), ("Supported", "replaces"),
            ("Content-Type", "application/sdp"),
        ], body=body, to_tag=self.local_tag)
        self.state = "active"
        self.answered_at = time.time()
        self.answered.set()
        self._drop_pending()
        asyncio.create_task(self._retransmit_2xx(resp.to_bytes()))
        return True

    def _drop_pending(self) -> None:
        if self._pending_key is not None:
            self.ua._pending.pop(self._pending_key, None)
            self._pending_key = None

    async def _retransmit_2xx(self, data: bytes) -> None:
        interval = T1
        waited = 0.0
        while waited < 64 * T1 and self.state == "active":
            try:
                await asyncio.wait_for(self._acked.wait(), interval)
                return
            except TimeoutError:
                waited += interval
                if self.peer_addr:
                    self.ua.stack.send_bytes(data, self.peer_addr)
                interval = min(interval * 2, T2)
        if self.state == "active" and not self._acked.is_set():
            log.warning("call %s: no ACK received for 200 OK", self.id)
            await self.hangup("no ACK")

    def reject(self, status: int = 486, reason: str = "Busy Here") -> None:
        if self.state == "ringing" and self.invite:
            self.ua.stack.respond(self.invite, status, reason, to_tag=self.local_tag)
        self._finish(f"rejected {status} {reason}")

    def _cancelled(self) -> None:
        if self.state == "ringing" and self.invite:
            self.ua.stack.respond(self.invite, 487, "Request Terminated", to_tag=self.local_tag)
            self._finish("caller cancelled")

    # -- UAC ------------------------------------------------------------------------------
    def _build_invite(self, uri: str, auth: tuple[str, str] | None) -> SipMessage:
        req = SipMessage(method="INVITE", uri=uri)
        req.add("Via", f"SIP/2.0/UDP {self.local_ip}:{self.ua.port};branch={new_branch()};rport")
        req.add("Max-Forwards", "70")
        req.add("From", self.account.from_header(self.local_tag, self.from_display))
        req.add("To", self.remote_uri)
        req.add("Call-ID", self.call_id)
        req.add("CSeq", f"{self.local_cseq} INVITE")
        req.add("Contact", self._contact())
        req.add("Allow", ALLOW)
        req.add("User-Agent", self.ua.user_agent)
        if self.asserted_identity:
            req.add("P-Asserted-Identity", self.asserted_identity)
        req.add("Content-Type", "application/sdp")
        if auth:
            req.add(*auth)
        req.body = self._local_sdp(offer=True)
        return req

    async def _run_uac(self, uri: str, dest: tuple[str, int]) -> None:
        self.state = "ringing"
        auth = None
        try:
            for attempt in range(3):
                req = self._build_invite(uri, auth)
                self._invite_req = req
                resp = await self.ua.stack.request(req, dest, on_provisional=self._on_provisional)
                if resp.status in (401, 407) and attempt == 0 and not self._cancel_requested:
                    hdr = "WWW-Authenticate" if resp.status == 401 else "Proxy-Authenticate"
                    ch = pick_challenge(resp.get_all(hdr))
                    if not ch:
                        break
                    name = "Authorization" if resp.status == 401 else "Proxy-Authorization"
                    auth = (name, build_authorization(ch, "INVITE", uri,
                                                      self.account.cfg.auth_username or self.account.cfg.username,
                                                      self.account.cfg.password))
                    self.local_cseq += 1
                    continue
                if 200 <= resp.status < 300:
                    self._on_uac_2xx(resp, dest)
                    return
                self._finish(f"{resp.status} {resp.reason}")
                return
            self._finish("authentication failed")
        except asyncio.CancelledError:
            self._finish("cancelled")
        except Exception as e:
            self._finish(str(e) or e.__class__.__name__)

    def _on_provisional(self, resp: SipMessage) -> None:
        if resp.status > 100 and resp.to.tag:
            self.remote_tag = resp.to.tag
        if resp.body and resp.content_type == "application/sdp":
            self._apply_remote_sdp(resp.body, is_offer=False)

    def _on_uac_2xx(self, resp: SipMessage, dest: tuple[str, int]) -> None:
        self.remote_tag = resp.to.tag or ""
        self.remote_uri = resp.get("To") or self.remote_uri
        contacts = resp.values("Contact")
        if contacts:
            self.remote_target = NameAddr.parse(contacts[0]).uri
        self.route_set = list(reversed(resp.values("Record-Route")))
        ok = bool(resp.body) and self._apply_remote_sdp(resp.body, is_offer=False)
        self._send_ack(resp.cseq[0])
        if self._cancel_requested or not ok:
            self.state = "active"  # dialog is confirmed: it must be closed with BYE
            asyncio.create_task(self.hangup("cancelled" if self._cancel_requested else "no compatible codec"))
            return
        self.state = "active"
        self.answered_at = time.time()
        self.answered.set()
        log.info("call %s answered by %s (%s)", self.id, self.remote_user, self.codec)

    def _send_ack(self, cseq: int) -> None:
        ack = SipMessage(method="ACK", uri=self.remote_target)
        ack.add("Via", f"SIP/2.0/UDP {self.local_ip}:{self.ua.port};branch={new_branch()};rport")
        ack.add("Max-Forwards", "70")
        ack.add("From", self.account.from_header(self.local_tag, self.from_display))
        ack.add("To", self.remote_uri)
        ack.add("Call-ID", self.call_id)
        ack.add("CSeq", f"{cseq} ACK")
        for r in self.route_set:
            ack.add("Route", r)
        self._ack_bytes = ack.to_bytes()
        asyncio.create_task(self._send_in_dialog_raw(self._ack_bytes))

    async def _send_in_dialog_raw(self, data: bytes) -> None:
        dest = await self._dialog_dest()
        self.ua.stack.send_bytes(data, dest)

    async def _dialog_dest(self) -> tuple[str, int]:
        target = self.route_set[0] if self.route_set else self.remote_target
        if target.startswith("<") or "<" in target:
            target = NameAddr.parse(target).uri
        try:
            u = SipUri.parse(target)
            return await self.ua.stack.resolve(u.host.strip("[]"), u.port or 5060)
        except Exception:
            if self.peer_addr:
                return self.peer_addr
            raise

    def resend_ack(self) -> None:
        if self._ack_bytes:
            asyncio.create_task(self._send_in_dialog_raw(self._ack_bytes))

    # -- teardown --------------------------------------------------------------------------
    async def hangup(self, reason: str = "local hangup") -> None:
        if self.state == "ended":
            return
        if self.state == "ringing":
            if self.direction == "inbound":
                self.reject(603, "Decline")
                return
            if not self._cancel_requested:        # one CANCEL per call: a second one gets no answer
                self._cancel_requested = True
                if self._invite_req:
                    await self._send_cancel()
            # wait briefly for the 487 so the transaction completes cleanly
            try:
                await asyncio.wait_for(asyncio.shield(self.ended.wait()), 4)
            except TimeoutError:
                self._finish(reason)
            return
        if self.state == "active":
            self.local_cseq += 1
            bye = self._in_dialog_request("BYE")
            self._finish(reason)
            try:
                dest = await self._dialog_dest()
                await asyncio.wait_for(self.ua.stack.request(bye, dest), 8)
            except Exception:
                pass

    async def _send_cancel(self) -> None:
        inv = self._invite_req
        assert inv is not None
        cancel = SipMessage(method="CANCEL", uri=inv.uri)
        cancel.add("Via", inv.values("Via")[0])
        cancel.add("Max-Forwards", "70")
        cancel.add("From", inv.get("From") or "")
        cancel.add("To", inv.get("To") or "")
        cancel.add("Call-ID", inv.call_id)
        cancel.add("CSeq", f"{inv.cseq[0]} CANCEL")
        try:
            dest = self.peer_addr or await self._dialog_dest()
            await asyncio.wait_for(self.ua.stack.request(cancel, dest), 5)
        except Exception:
            pass

    def _in_dialog_request(self, method: str) -> SipMessage:
        req = SipMessage(method=method, uri=self.remote_target)
        req.add("Via", f"SIP/2.0/UDP {self.local_ip}:{self.ua.port};branch={new_branch()};rport")
        req.add("Max-Forwards", "70")
        if self.direction == "outbound":
            req.add("From", self.account.from_header(self.local_tag, self.from_display))
        else:
            req.add("From", _with_tag(self.local_uri, self.local_tag))
        req.add("To", _with_tag(self.remote_uri, self.remote_tag))
        req.add("Call-ID", self.call_id)
        req.add("CSeq", f"{self.local_cseq} {method}")
        for r in self.route_set:
            req.add("Route", r)
        req.add("User-Agent", self.ua.user_agent)
        return req

    def _finish(self, reason: str) -> None:
        if self.state == "ended":
            return
        self.state = "ended"
        self.end_reason = reason
        self.ended_at = time.time()
        self._drop_pending()
        if self.rtp:
            self.rtp.close()
            self.ua.ports.release(self.rtp_port)
        self.ua.unregister_call(self)
        self.ended.set()
        log.info("call %s ended: %s", self.id, reason)
        if self.on_end:
            try:
                self.on_end(self)
            except Exception:
                log.exception("on_end callback failed")

    # -- in-dialog requests --------------------------------------------------------------------
    async def handle_request(self, req: SipMessage) -> None:
        stack = self.ua.stack
        method = req.method
        if req.cseq[0] < self.remote_cseq and method not in ("ACK", "CANCEL"):
            stack.respond(req, 500, "Server Internal Error", [("Retry-After", "1")])
            return
        if method not in ("ACK", "CANCEL"):
            self.remote_cseq = req.cseq[0]
        if method == "BYE":
            stack.respond(req, 200, "OK")
            self._finish("remote hangup")
        elif method in ("INVITE", "UPDATE"):
            if req.body and req.content_type == "application/sdp":
                if not self._apply_remote_sdp(req.body, is_offer=True):
                    stack.respond(req, 488, "Not Acceptable Here")
                    return
                self._sdp_version += 1
            contacts = req.values("Contact")
            if contacts:
                self.remote_target = NameAddr.parse(contacts[0]).uri
            resp = stack.respond(req, 200, "OK", [
                ("Contact", self._contact()), ("Allow", ALLOW), ("Content-Type", "application/sdp"),
            ], body=self._local_sdp())
            if method == "INVITE":
                self._acked.clear()
                asyncio.create_task(self._retransmit_2xx(resp.to_bytes()))
        elif method == "INFO":
            stack.respond(req, 200, "OK")
            if b"Signal" in req.body and self.on_dtmf:
                for line in req.body.decode("utf-8", "replace").splitlines():
                    if line.lower().startswith("signal"):
                        digit = line.split("=", 1)[-1].strip()
                        if digit:
                            self.on_dtmf(digit[0])
        elif method in ("OPTIONS", "NOTIFY"):
            stack.respond(req, 200, "OK", [("Allow", ALLOW)])
        else:
            stack.respond(req, 405, "Method Not Allowed", [("Allow", ALLOW)])

    def handle_ack(self, req: SipMessage) -> None:
        self._acked.set()
        if req.body and req.content_type == "application/sdp" and self.negotiated is None:
            self._apply_remote_sdp(req.body, is_offer=False)


def _with_tag(header: str, tag: str) -> str:
    if not tag or ";tag=" in header.replace(" ", "").lower():
        return header
    return f"{header};tag={tag}"


IncomingHandler = Callable[[Call], Awaitable[None]]


class UserAgent:
    def __init__(self, port: int, rtp_ports: PortAllocator, advertise_ip: str = "",
                 user_agent: str = "telerouter", bind: str = "0.0.0.0"):
        self.port = port
        self.ports = rtp_ports
        self.advertise_ip = advertise_ip
        self.user_agent = user_agent
        self.stack = SipStack(port, bind, user_agent)
        self.accounts: dict[str, Account] = {}
        self.direct_accounts: dict[str, Account] = {}   # never REGISTER; matched by Contact user only
        self.calls: dict[tuple[str, str], Call] = {}
        self.on_incoming: IncomingHandler | None = None
        self._pending: dict[str, Call] = {}   # INVITE branch -> ringing inbound call
        self.stack.request_handler = self._on_request
        self.stack.cancel_handler = self._on_cancel
        self.stack.ack_handler = self._on_ack
        self.stack.on_2xx_retransmission = self._on_2xx_retransmission

    async def start(self) -> None:
        await self.stack.start()

    async def stop(self) -> None:
        for call in list(self.calls.values()):
            await call.hangup("shutting down")
        await asyncio.gather(*(a.stop() for a in self.accounts.values()), return_exceptions=True)
        self.stack.close()

    # -- accounts -------------------------------------------------------------------------------
    async def set_accounts(self, configs: list[AccountConfig]) -> None:
        wanted = {c.id: c for c in configs}
        for aid in list(self.accounts):
            acc = self.accounts[aid]
            if aid not in wanted or wanted[aid] != acc.cfg:
                await acc.stop()
                del self.accounts[aid]
        for aid, cfg in wanted.items():
            if aid not in self.accounts:
                acc = Account(self, cfg)
                self.accounts[aid] = acc
                acc.start()

    def _account_for(self, req: SipMessage) -> Account | None:
        try:
            ruser = SipUri.parse(req.uri or "").user
        except Exception:
            ruser = None
        for acc in [*self.accounts.values(), *self.direct_accounts.values()]:
            if ruser and ruser == acc.contact_user:
                return acc
        try:
            to_user = req.to.sip_uri.user
        except Exception:
            to_user = None
        matches = [a for a in self.accounts.values() if to_user and a.cfg.username == to_user]
        return matches[0] if len(matches) == 1 else None

    # -- calls ------------------------------------------------------------------------------------
    def register_call(self, call: Call) -> None:
        self.calls[(call.call_id, call.local_tag)] = call

    def unregister_call(self, call: Call) -> None:
        self.calls.pop((call.call_id, call.local_tag), None)

    def _find_dialog(self, req: SipMessage) -> Call | None:
        return self.calls.get((req.call_id, req.to.tag or ""))

    async def _on_request(self, req: SipMessage, addr: tuple[str, int]) -> None:
        call = self._find_dialog(req) if req.to.tag else None
        if call:
            await call.handle_request(req)
            return
        if req.to.tag and req.method != "OPTIONS":
            self.stack.respond(req, 481, "Call/Transaction Does Not Exist")
            return
        if req.method == "INVITE":
            await self._incoming_invite(req, addr)
        elif req.method == "OPTIONS":
            self.stack.respond(req, 200, "OK", [("Allow", ALLOW), ("Accept", "application/sdp")])
        elif req.method in ("NOTIFY",):
            self.stack.respond(req, 200, "OK")
        else:
            self.stack.respond(req, 405, "Method Not Allowed", [("Allow", ALLOW)])

    async def _incoming_invite(self, req: SipMessage, addr: tuple[str, int]) -> None:
        account = self._account_for(req)
        if not account:
            self.stack.respond(req, 404, "Not Found")
            return
        self.stack.respond(req, 100, "Trying")
        call = Call(self, account, "inbound")
        call.invite = req
        call.call_id = req.call_id
        call.local_tag = new_tag()
        call.remote_tag = req.from_.tag or ""
        call.remote_cseq = req.cseq[0]
        call.local_uri = req.get("To") or ""
        call.remote_uri = req.get("From") or ""
        call.peer_addr = addr
        call.local_ip = self.advertise_ip or SipStack.local_ip_for(addr[0])
        contacts = req.values("Contact")
        call.remote_target = NameAddr.parse(contacts[0]).uri if contacts else str(req.from_.uri)
        call.route_set = req.values("Record-Route")
        ident = req.get("P-Asserted-Identity") or req.get("From") or ""
        try:
            na = NameAddr.parse(ident)
            call.remote_user = na.sip_uri.user or ""
            call.remote_display = na.display
        except Exception:
            call.remote_user = req.from_.uri
        if req.body and req.content_type == "application/sdp":
            parsed = sdpmod.Sdp.parse(req.body)
            if not sdpmod.negotiate(parsed, PREFERRED_CODECS):
                self.stack.respond(req, 488, "Not Acceptable Here", to_tag=call.local_tag)
                return
        try:
            await call.alloc_rtp()
        except RuntimeError:
            self.stack.respond(req, 503, "Service Unavailable", to_tag=call.local_tag)
            return
        if req.body and req.content_type == "application/sdp":
            call._apply_remote_sdp(req.body, is_offer=True)
        call.state = "ringing"
        self.register_call(call)
        call._pending_key = req.branch or ""
        self._pending[call._pending_key] = call
        log.info("incoming call from %s to %s", call.remote_user or "?", account.cfg.username)
        if not self.on_incoming:
            call.reject(480, "Temporarily Unavailable")
            return
        await self.on_incoming(call)

    def _on_cancel(self, invite: SipMessage) -> None:
        call = self._pending.get(invite.branch or "")
        if call:
            call._cancelled()

    def _on_ack(self, req: SipMessage) -> None:
        call = self._find_dialog(req)
        if call:
            call.handle_ack(req)

    def _on_2xx_retransmission(self, resp: SipMessage) -> None:
        call = self.calls.get((resp.call_id, resp.from_.tag or ""))
        if call:
            call.resend_ack()
