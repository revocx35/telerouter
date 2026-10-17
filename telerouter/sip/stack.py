"""SIP transport (UDP) and transaction layer (RFC 3261 section 17, simplified)."""

from __future__ import annotations

import asyncio
import logging
import secrets
import socket
import time
from collections.abc import Awaitable, Callable

from .message import SipMessage, SipParseError

log = logging.getLogger("telerouter.sip")
trace = logging.getLogger("telerouter.sip.trace")

T1 = 0.5
T2 = 4.0
TX_LINGER = 32.0


def new_branch() -> str:
    return "z9hG4bK" + secrets.token_hex(8)


def new_tag() -> str:
    return secrets.token_hex(6)


def new_call_id() -> str:
    return secrets.token_hex(12)


class ClientTransaction:
    def __init__(self, stack: "SipStack", request: SipMessage, dest: tuple[str, int],
                 on_provisional: Callable[[SipMessage], None] | None):
        self.stack = stack
        self.request = request
        self.dest = dest
        self.method = request.method or ""
        self.key = (request.branch or "", self.method)
        self.data = request.to_bytes()
        self.future: asyncio.Future[SipMessage] = asyncio.get_running_loop().create_future()
        self.on_provisional = on_provisional
        self.got_provisional = False
        self._timer: asyncio.Task | None = None
        self._linger: asyncio.TimerHandle | None = None

    def start(self) -> None:
        self.stack.transactions[self.key] = self
        self.stack.send_message(self.request, self.dest, self.data)
        self._timer = asyncio.create_task(self._timers())
        self.future.add_done_callback(self._completed)

    def _completed(self, _fut) -> None:
        # keep the transaction around a while to absorb retransmitted responses
        self._linger = asyncio.get_running_loop().call_later(TX_LINGER, self._remove)

    def _remove(self) -> None:
        if self.stack.transactions.get(self.key) is self:
            del self.stack.transactions[self.key]

    async def _timers(self) -> None:
        loop = asyncio.get_running_loop()
        interval = T1
        deadline = loop.time() + 64 * T1
        invite = self.method == "INVITE"
        try:
            while not self.future.done():
                await asyncio.sleep(interval)
                if self.future.done():
                    break
                if invite and self.got_provisional:
                    interval = 1.0  # wait for the final answer (UA decides when to give up)
                    continue
                if loop.time() >= deadline:
                    self.future.set_exception(TimeoutError(f"{self.method}: no response from {self.dest[0]}:{self.dest[1]}"))
                    break
                self.stack.send_bytes(self.data, self.dest)
                interval = interval * 2 if invite else min(interval * 2, T2)
                if not invite and self.got_provisional:
                    interval = T2
        except asyncio.CancelledError:
            if not self.future.done():
                self.future.cancel()
            raise

    def on_response(self, resp: SipMessage) -> None:
        if resp.status < 200:
            self.got_provisional = True
            if self.on_provisional and not self.future.done():
                try:
                    self.on_provisional(resp)
                except Exception:
                    log.exception("provisional handler failed")
            return
        if self.method == "INVITE" and resp.status >= 300:
            self.stack.send_bytes(_ack_for_non2xx(self.request, resp).to_bytes(), self.dest)
        if not self.future.done():
            self.future.set_result(resp)
        elif self.method == "INVITE" and resp.status < 300 and self.stack.on_2xx_retransmission:
            self.stack.on_2xx_retransmission(resp)

    def cancel(self) -> None:
        if self._timer:
            self._timer.cancel()
        if self._linger:
            self._linger.cancel()


def _ack_for_non2xx(invite: SipMessage, resp: SipMessage) -> SipMessage:
    ack = SipMessage(method="ACK", uri=invite.uri)
    ack.add("Via", invite.values("Via")[0])
    ack.add("Max-Forwards", "70")
    ack.add("From", invite.get("From") or "")
    ack.add("To", resp.get("To") or "")
    ack.add("Call-ID", invite.call_id)
    ack.add("CSeq", f"{invite.cseq[0]} ACK")
    for r in invite.get_all("Route"):
        ack.add("Route", r)
    return ack


class ServerTransaction:
    __slots__ = ("request", "addr", "last", "created", "final_status")

    def __init__(self, request: SipMessage, addr: tuple[str, int]):
        self.request = request
        self.addr = addr
        self.last: bytes | None = None
        self.created = time.monotonic()
        self.final_status = 0


RequestHandler = Callable[[SipMessage, tuple[str, int]], Awaitable[None]]


class SipStack(asyncio.DatagramProtocol):
    def __init__(self, port: int, bind: str = "0.0.0.0", user_agent: str = "telerouter"):
        self.port = port
        self.bind = bind
        self.user_agent = user_agent
        self.transport: asyncio.DatagramTransport | None = None
        self.transactions: dict[tuple[str, str], ClientTransaction] = {}
        self.server_tx: dict[tuple[str, str], ServerTransaction] = {}
        self.request_handler: RequestHandler | None = None
        self.cancel_handler: Callable[[SipMessage], None] | None = None
        self.ack_handler: Callable[[SipMessage], None] | None = None
        self.on_2xx_retransmission: Callable[[SipMessage], None] | None = None
        self._dns: dict[tuple[str, int], tuple[float, tuple[str, int]]] = {}
        self._sweeper: asyncio.Task | None = None

    # -- lifecycle -------------------------------------------------------------
    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: self, local_addr=(self.bind, self.port),
                                            family=socket.AF_INET)
        self._sweeper = asyncio.create_task(self._sweep())
        log.info("SIP listening on udp %s:%d", self.bind, self.port)

    def close(self) -> None:
        if self._sweeper:
            self._sweeper.cancel()
        for tx in list(self.transactions.values()):
            tx.cancel()
        self.transactions.clear()
        if self.transport:
            self.transport.close()

    def connection_made(self, transport) -> None:
        self.transport = transport

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(10)
            cutoff = time.monotonic() - TX_LINGER
            for k in [k for k, tx in self.server_tx.items() if tx.created < cutoff]:
                del self.server_tx[k]

    # -- helpers ---------------------------------------------------------------
    async def resolve(self, host: str, port: int) -> tuple[str, int]:
        key = (host, port)
        hit = self._dns.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, family=socket.AF_INET,
                                                             type=socket.SOCK_DGRAM)
        addr = infos[0][4][:2]
        self._dns[key] = (time.monotonic() + 60, addr)
        return addr

    @staticmethod
    def local_ip_for(dest_ip: str) -> str:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((dest_ip, 9))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            s.close()

    def send_bytes(self, data: bytes, addr: tuple[str, int]) -> None:
        if self.transport:
            self.transport.sendto(data, addr)

    def send_message(self, msg: SipMessage, addr: tuple[str, int], data: bytes | None = None) -> None:
        data = data or msg.to_bytes()
        if trace.isEnabledFor(logging.DEBUG):
            trace.debug("--> %s:%d\n%s", addr[0], addr[1], data.decode("utf-8", "replace"))
        self.send_bytes(data, addr)

    # -- client side -------------------------------------------------------------
    async def request(self, req: SipMessage, dest: tuple[str, int],
                      on_provisional: Callable[[SipMessage], None] | None = None) -> SipMessage:
        """Send a request and wait for its final response (retransmits over UDP)."""
        tx = ClientTransaction(self, req, dest, on_provisional)
        tx.start()
        return await tx.future

    # -- server side -------------------------------------------------------------
    def make_response(self, req: SipMessage, status: int, reason: str,
                      headers: list[tuple[str, str]] | None = None, body: bytes = b"",
                      to_tag: str | None = None) -> SipMessage:
        resp = SipMessage(status=status, reason=reason)
        for v in req.get_all("Via"):
            resp.add("Via", v)
        resp.add("From", req.get("From") or "")
        to = req.get("To") or ""
        if to_tag and ";tag=" not in to.replace(" ", "").lower() and status != 100:
            to += f";tag={to_tag}"
        resp.add("To", to)
        resp.add("Call-ID", req.call_id)
        resp.add("CSeq", req.get("CSeq") or "")
        for k, v in headers or []:
            resp.add(k, v)
        resp.add("Server", self.user_agent)
        resp.body = body
        return resp

    def respond(self, req: SipMessage, status: int, reason: str,
                headers: list[tuple[str, str]] | None = None, body: bytes = b"",
                to_tag: str | None = None) -> SipMessage:
        tx = self.server_tx.get((req.branch or "", req.method or ""))
        addr = tx.addr if tx else getattr(req, "source", None)
        resp = self.make_response(req, status, reason, headers, body, to_tag)
        data = resp.to_bytes()
        if tx:
            tx.last = data
            if status >= 200:
                tx.final_status = status
        if addr:
            self.send_message(resp, addr, data)
        return resp

    # -- incoming ----------------------------------------------------------------
    def datagram_received(self, data: bytes, addr) -> None:
        if not data.strip():
            return  # CRLF keep-alive
        try:
            msg = SipMessage.parse(data)
        except (SipParseError, ValueError, IndexError) as e:
            log.debug("dropping unparsable packet from %s: %s", addr, e)
            return
        addr = (addr[0], addr[1])
        msg.source = addr  # type: ignore[attr-defined]
        if trace.isEnabledFor(logging.DEBUG):
            trace.debug("<-- %s:%d\n%s", addr[0], addr[1], data.decode("utf-8", "replace"))
        if msg.is_request:
            self._on_request(msg, addr)
        else:
            self._on_response(msg)

    def _on_response(self, resp: SipMessage) -> None:
        key = (resp.branch or "", resp.cseq[1])
        tx = self.transactions.get(key)
        if tx:
            tx.on_response(resp)
        elif resp.cseq[1] == "INVITE" and 200 <= (resp.status or 0) < 300 and self.on_2xx_retransmission:
            self.on_2xx_retransmission(resp)

    def _on_request(self, req: SipMessage, addr: tuple[str, int]) -> None:
        method = req.method or ""
        branch = req.branch or ""
        if method == "ACK":
            tx = self.server_tx.get((branch, "INVITE"))
            if tx and tx.final_status >= 300:
                return  # ACK for a non-2xx final response: absorbed by the transaction
            if self.ack_handler:
                self.ack_handler(req)
            return
        key = (branch, method)
        existing = self.server_tx.get(key)
        if existing:
            if existing.last:
                self.send_bytes(existing.last, existing.addr)
            return
        self.server_tx[key] = ServerTransaction(req, addr)
        if method == "CANCEL":
            invite_tx = self.server_tx.get((branch, "INVITE"))
            if not invite_tx:
                self.respond(req, 481, "Call/Transaction Does Not Exist")
                return
            self.respond(req, 200, "OK")
            if invite_tx.final_status == 0 and self.cancel_handler:
                self.cancel_handler(invite_tx.request)
            return
        if self.request_handler:
            asyncio.create_task(self._dispatch(req, addr))

    async def _dispatch(self, req: SipMessage, addr: tuple[str, int]) -> None:
        try:
            await self.request_handler(req, addr)  # type: ignore[misc]
        except Exception:
            log.exception("error handling %s", req.summary())
            tx = self.server_tx.get((req.branch or "", req.method or ""))
            if tx and tx.final_status == 0 and req.method != "ACK":
                self.respond(req, 500, "Server Internal Error")
