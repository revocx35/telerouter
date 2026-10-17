"""Self-signed CA/server certificate, the client's CA pinning, and pinned links to wa2sip/cam2sip."""

import asyncio
import json
import ssl

import pytest

from telerouter import tls
from telerouter.models import Integration
from telerouter.router.integrations import IntegrationClient, IntegrationError, fetch_certificate

from .conftest import free_port


def test_ca_and_server_cert(tmp_path):
    a = tls.ensure(str(tmp_path), ["telerouter.lan", "10.1.2.3"])
    assert "telerouter.lan" in a.names and "10.1.2.3" in a.names
    b = tls.ensure(str(tmp_path), ["telerouter.lan", "10.1.2.3"])
    assert a.ca_sha256 == b.ca_sha256 and a.server_spki == b.server_spki        # stable across restarts
    c = tls.ensure(str(tmp_path), ["other.lan"])
    assert c.ca_sha256 == a.ca_sha256 and c.server_spki != a.server_spki        # re-issued, CA kept
    assert (tmp_path / "tls" / "ca.key").stat().st_mode & 0o777 == 0o600


async def _serve(cert, key, body: dict, port: int):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)

    async def handle(reader, writer):
        req = await reader.read(65536)
        data = json.dumps(body).encode()
        auth_ok = b"authorization: bearer good" in req.lower()
        status = b"200 OK" if auth_ok else b"401 Unauthorized"
        writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\nContent-Length: "
                     + str(len(data)).encode() + b"\r\nConnection: close\r\n\r\n" + data)
        await writer.drain()
        writer.close()
    return await asyncio.start_server(handle, "127.0.0.1", port, ssl=ctx)


async def test_client_ca_pinning_with_strict_verification(tmp_path):
    files = tls.ensure(str(tmp_path), [])
    port = free_port(1)
    server = await _serve(files.cert, files.key, {"ok": True}, port)
    try:
        from telerouter_client.net import pinned_context
        ctx = pinned_context(files.ca_pem.decode())                 # VERIFY_X509_STRICT is on in 3.13
        r, w = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
        w.close()
        other = tls.ensure(str(tmp_path / "other"), [])
        with pytest.raises(ssl.SSLCertVerificationError):
            await asyncio.open_connection("127.0.0.1", port, ssl=pinned_context(other.ca_pem.decode()))
    finally:
        server.close()


async def test_integration_link_pins_the_certificate(tmp_path):
    files = tls.ensure(str(tmp_path), [])                       # plays the wa2sip server
    port = free_port(1)
    body = {"sip_uri": "sip:tr0123456789abcdef@203.0.113.9:5064", "targets": [
        {"id": "w:1@c.us", "name": "Alice", "kind": "whatsapp", "number": "+49 170"}, {"bad": 1}]}
    server = await _serve(files.cert, files.key, body, port)
    try:
        url = f"https://127.0.0.1:{port}"
        pem, fp = await fetch_certificate(url)
        assert len(fp) == 64
        good = IntegrationClient(Integration(kind="wa2sip", url=url, token="good"), pem)
        targets = await good.targets()
        assert targets == [{"id": "w:1@c.us", "name": "Alice", "kind": "whatsapp", "number": "+49170", "account": ""}]
        # the SIP address is rewritten to the host we talk to, never where the response points
        assert await good.prepare_call(target="w:1@c.us") == f"sip:tr0123456789abcdef@127.0.0.1:5064"
        await good.close()
        bad_token = IntegrationClient(Integration(kind="wa2sip", url=url, token="bad"), pem)
        with pytest.raises(IntegrationError, match="refused the token"):
            await bad_token.info()
        await bad_token.close()
        other = tls.ensure(str(tmp_path / "other"), [])
        wrong_pin = IntegrationClient(Integration(kind="wa2sip", url=url, token="good"),
                                      other.ca_pem.decode())
        with pytest.raises(IntegrationError):
            await wrong_pin.info()
        await wrong_pin.close()
        system_ca = IntegrationClient(Integration(kind="wa2sip", url=url, token="good"), "")
        with pytest.raises(IntegrationError):                    # self-signed: refused without a pin
            await system_ca.info()
        await system_ca.close()
    finally:
        server.close()


def test_integration_url_validation():
    with pytest.raises(ValueError):
        Integration(kind="wa2sip", url="http://10.0.0.1:8092")
    with pytest.raises(ValueError):
        Integration(kind="wa2sip", url="https://10.0.0.1:8493/path")
    assert Integration(kind="cam2sip", url="https://cams.lan:8443/").url == "https://cams.lan:8443"
