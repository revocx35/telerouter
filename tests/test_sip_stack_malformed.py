"""Malformed headers that only fail when they are used (Via, CSeq) must be dropped by the stack."""

import asyncio
import logging

import pytest

from telerouter.sip.stack import SipStack


class _Tx:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))


BAD = [
    b"OPTIONS sip:x@h SIP/2.0\r\nVia: SIP/2.0/UDP h:notaport;branch=z9hG4bK1\r\nCSeq: 1 OPTIONS\r\n\r\n",
    b"OPTIONS sip:x@h SIP/2.0\r\nVia: SIP/2.0/UDP [::1;branch=z9hG4bK1\r\nCSeq: 1 OPTIONS\r\n\r\n",
    b"OPTIONS sip:x@h SIP/2.0\r\nVia: SIP/2.0/UDP h;branch=z9hG4bK1\r\nCSeq: abc OPTIONS\r\n\r\n",
    b"SIP/2.0 200 OK\r\nVia: SIP/2.0/UDP h:x;branch=z9hG4bK1\r\nCSeq: 1 INVITE\r\n\r\n",
    b"SIP/2.0 200 OK\r\nVia: SIP/2.0/UDP h;branch=z9hG4bK1\r\nCSeq: x INVITE\r\n\r\n",
]


@pytest.mark.parametrize("data", BAD)
def test_malformed_header_is_dropped(data, caplog):
    async def run():
        stack = SipStack(0)
        stack.transport = _Tx()
        calls = []

        async def handler(req, addr):
            calls.append(req)
        stack.request_handler = handler
        stack.on_2xx_retransmission = lambda r: calls.append(r)
        stack.datagram_received(data, ("192.0.2.1", 5060))      # must not raise
        await asyncio.sleep(0)
        return calls

    with caplog.at_level(logging.ERROR):
        assert asyncio.run(run()) == []
