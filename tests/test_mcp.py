"""MCP server: JSON-RPC handling, token scopes, contact/speaker allow-lists, origin check."""

import json

import pytest
from starlette.requests import Request

from telerouter.mcp import McpServer
from telerouter.models import Client, Config, Contact, McpToken
from telerouter.security import new_token, token_hash


class FakeStore:
    def __init__(self):
        self.config = Config()

    def save(self):
        pass

    def agent(self, aid):
        return None


class FakeEngine:
    def __init__(self):
        self.store = FakeStore()
        self.clients = {}
        self.agent_rts = {}

    @property
    def agents(self):
        return self

    def get(self, aid):
        return None

    def same_origin(self, origin, request):
        return origin == "https://telerouter.lan"


def req(body, token="", origin=None):
    headers = [(b"content-type", b"application/json")]
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    if origin:
        headers.append((b"origin", origin.encode()))
    data = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": data, "more_body": False}
    return Request({"type": "http", "method": "POST", "path": "/mcp", "headers": headers}, receive)


@pytest.fixture()
def mcp():
    e = FakeEngine()
    cfg = e.store.config
    cfg.contacts = [Contact(id="c1", kind="pbx", name="Desk", alias="dad", number="1005"),
                    Contact(id="c2", kind="whatsapp", name="Ayşe", number="905551112233"),
                    Contact(id="c3", kind="camera", name="Front door", enabled=False)]
    cfg.clients = [Client(id="k1", name="Kitchen"), Client(id="k2", name="Office")]
    full, limited = new_token("trm_"), new_token("trm_")
    cfg.mcp_tokens = [McpToken(name="full", token_hash=token_hash(full)),
                      McpToken(name="limited", token_hash=token_hash(limited), scopes=["contacts"], contacts=["c2"])]
    server = McpServer(e)
    server.full, server.limited = full, limited
    return server


async def call(mcp, body, token=None, origin=None):
    r = await mcp.handle(req(body, mcp.full if token is None else token, origin))
    return r.status_code, (json.loads(r.body) if r.body else None)


async def test_initialize_negotiates_the_protocol(mcp):
    st, r = await call(mcp, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
    assert st == 200 and r["result"]["protocolVersion"] == "2025-06-18"
    assert r["result"]["capabilities"] == {"tools": {"listChanged": False}}
    st, r = await call(mcp, {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "1999"}})
    assert r["result"]["protocolVersion"] == "2025-11-25"
    st, r = await call(mcp, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert st == 202 and r is None


async def test_auth_and_origin(mcp):
    st, _ = await call(mcp, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, token="")
    assert st == 401
    st, _ = await call(mcp, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, token="trm_wrong")
    assert st == 401
    st, _ = await call(mcp, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, origin="https://evil.example")
    assert st == 403
    st, r = await call(mcp, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, origin="https://telerouter.lan")
    assert st == 200 and r["result"] == {}


async def test_scopes_limit_tools_and_contacts(mcp):
    _, r = await call(mcp, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in r["result"]["tools"]}
    assert {"list_contacts", "connect_call", "phone_message", "say", "announce", "list_speakers"} <= names
    _, r = await call(mcp, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=mcp.limited)
    assert [t["name"] for t in r["result"]["tools"]] == ["list_contacts"]
    _, r = await call(mcp, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                            "params": {"name": "list_contacts", "arguments": {}}})
    got = r["result"]["structuredContent"]["contacts"]
    assert [c["name"] for c in got] == ["dad", "Ayşe"]             # hidden contacts are not listed
    _, r = await call(mcp, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "list_contacts", "arguments": {}}}, token=mcp.limited)
    assert [c["name"] for c in r["result"]["structuredContent"]["contacts"]] == ["Ayşe"]
    _, r = await call(mcp, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                            "params": {"name": "connect_call", "arguments": {"contact": "dad"}}}, token=mcp.limited)
    assert r["error"]["code"] == -32602                                 # not in its scopes


async def test_tool_argument_validation_and_errors(mcp):
    _, r = await call(mcp, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "list_contacts", "arguments": {"evil": 1}}})
    assert r["error"]["code"] == -32602
    _, r = await call(mcp, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                            "params": {"name": "connect_call", "arguments": {}}})
    assert "missing argument" in r["error"]["message"]
    # no voice request in progress and no speaker named: a readable tool error, not a crash
    _, r = await call(mcp, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "connect_call", "arguments": {"contact": "dad"}}})
    assert r["result"]["isError"] and "speaker" in r["result"]["content"][0]["text"]
    _, r = await call(mcp, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                            "params": {"name": "connect_call", "arguments": {"contact": "nobody"}}})
    assert r["result"]["isError"] and "no contact" in r["result"]["content"][0]["text"]
    _, r = await call(mcp, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                            "params": {"name": "announce", "arguments": {"speaker": "kitchen", "text": "hi"}}})
    assert r["result"]["isError"] and "offline" in r["result"]["content"][0]["text"]
    _, r = await call(mcp, {"jsonrpc": "2.0", "id": 6, "method": "nope"})
    assert r["error"]["code"] == -32601


async def test_contact_lookup_is_forgiving_but_not_ambiguous(mcp):
    tok = mcp.engine.store.config.mcp_tokens[0]
    assert mcp._find_contact(tok, "Dad").id == "c1"
    assert mcp._find_contact(tok, "1005").id == "c1"
    assert mcp._find_contact(tok, "ayş").id == "c2"
    mcp.engine.store.config.contacts.append(Contact(id="c4", kind="pbx", name="Dad mobile", number="1006"))
    from telerouter.mcp import ToolError
    assert mcp._find_contact(tok, "dad").id == "c1"            # exact referred name wins
    with pytest.raises(ToolError):
        mcp._find_contact(tok, "ad")                             # substring of two contacts


async def test_list_contacts_filters_by_kind_and_kind_words(mcp):
    mcp.engine.store.config.contacts.append(Contact(id="c5", kind="camera", name="C212"))
    for args in ({"query": "camera"}, {"query": "cameras"}, {"kind": "camera"}):
        _, r = await call(mcp, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "list_contacts", "arguments": args}})
        assert [c["name"] for c in r["result"]["structuredContent"]["contacts"]] == ["C212"], args
