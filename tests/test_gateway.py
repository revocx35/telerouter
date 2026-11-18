"""MCP gateway: telerouter as an MCP client of external servers, re-serving their tools to its own tokens."""

import json

import httpx
import pytest

from telerouter.gateway import Gateway, McpClient, clean_tools, exposed_name
from telerouter.mcp import McpServer
from telerouter.models import ExternalMcpServer, McpToken
from telerouter.security import new_token, token_hash

from .test_mcp import FakeEngine, req

UP_TOKEN = "upstream-secret-123"


class Upstream:
    """A fake Streamable HTTP MCP server (JSON or SSE answers, sessions, pagination)."""

    def __init__(self, sse=False):
        self.sse = sse
        self.sessions: set[str] = set()
        self.calls: list[tuple[str, dict]] = []
        self.inits = 0
        self.seen_auth: set[str] = set()
        self.redirect = False
        self.huge = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen_auth.add(request.headers.get("authorization", ""))
        if self.redirect:
            return httpx.Response(307, headers={"Location": "https://evil.example/mcp"})
        if request.headers.get("authorization") != f"Bearer {UP_TOKEN}":
            return httpx.Response(401)
        if request.method == "DELETE":
            self.sessions.discard(request.headers.get("mcp-session-id", ""))
            return httpx.Response(204)
        msg = json.loads(request.content)
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            self.inits += 1
            sid = f"s{self.inits}"
            self.sessions.add(sid)
            return self._answer(mid, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                      "serverInfo": {"name": "fake-ha", "version": "1"}}, {"Mcp-Session-Id": sid})
        if request.headers.get("mcp-session-id") not in self.sessions:
            return httpx.Response(404)
        if mid is None:
            return httpx.Response(202)
        if method == "tools/list":
            cursor = (msg.get("params") or {}).get("cursor")
            if not cursor:
                return self._answer(mid, {"tools": [
                    {"name": "HassTurnOn", "description": "Turn on a device",
                     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}},
                    {"name": "bad name!", "description": "odd characters", "inputSchema": {"type": "object"}}],
                    "nextCursor": "p2"})
            return self._answer(mid, {"tools": [{"name": "HassTurnOff", "description": "Turn off",
                                                 "inputSchema": {"type": "string"}}]})
        if method == "tools/call":
            p = msg["params"]
            self.calls.append((p["name"], p["arguments"]))
            if self.huge:
                return self._answer(mid, {"content": [{"type": "text", "text": "x" * (5 * 1024 * 1024)}]})
            return self._answer(mid, {"content": [{"type": "text", "text": f"done {p['arguments'].get('name')}"}],
                                      "structuredContent": {"ok": True}, "isError": False})
        return self._answer(mid, None, error={"code": -32601, "message": "nope"})

    def _answer(self, mid, result, headers=None, error=None):
        body = {"jsonrpc": "2.0", "id": mid, **({"error": error} if error else {"result": result})}
        if self.sse:
            text = ('event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'
                    f"event: message\ndata: {json.dumps(body)}\n\n")
            return httpx.Response(200, headers={"Content-Type": "text/event-stream", **(headers or {})}, text=text)
        return httpx.Response(200, json=body, headers=headers or {})


def make(sse=False):
    up = Upstream(sse)
    e = FakeEngine()
    cfg = e.store.config
    a, b = new_token("trm_"), new_token("trm_")
    cfg.mcp_tokens = [McpToken(id="tA", name="jarvis", token_hash=token_hash(a)),
                      McpToken(id="tB", name="other", token_hash=token_hash(b), scopes=["contacts"])]
    cfg.external_mcp = [ExternalMcpServer(id="x1", name="ha", url="https://ha.lan/api/mcp", token=UP_TOKEN,
                                          description="lights")]
    e.gateway = Gateway(e, transport=httpx.MockTransport(up))
    server = McpServer(e)
    return e, server, up, a, b


async def rpc(server, token, method, params=None, mid=1):
    r = await server.handle(req({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}, token))
    return json.loads(r.body)


async def settle(e):
    await e.gateway.sync()
    for u in e.gateway.ups.values():
        if u.task:
            await u.task


@pytest.mark.parametrize("sse", [False, True])
async def test_external_tools_are_listed_and_called_for_every_token(sse):
    e, server, up, a, b = make(sse)
    await settle(e)
    info = e.gateway.info("x1")
    assert info["status"] == "connected" and info["server"]["name"] == "fake-ha"
    assert [t["exposed"] for t in info["tools"]] == ["ha__HassTurnOn", "ha__bad_name_", "ha__HassTurnOff"]
    for tok in (a, b):                                       # shared with all tokens by default, whatever their scopes
        names = [t["name"] for t in (await rpc(server, tok, "tools/list"))["result"]["tools"]]
        assert "ha__HassTurnOn" in names and "ha__HassTurnOff" in names
    tools = {t["name"]: t for t in (await rpc(server, a, "tools/list"))["result"]["tools"]}
    assert tools["ha__HassTurnOn"]["description"] == "[ha] Turn on a device"
    assert tools["ha__HassTurnOff"]["inputSchema"] == {"type": "object", "properties": {}}   # bad schema replaced
    assert "list_contacts" in tools                          # telerouter's own tools stay
    init = await rpc(server, a, "initialize", {"protocolVersion": "2025-06-18"})
    assert "ha__* tools come from ha (lights)" in init["result"]["instructions"]
    r = await rpc(server, a, "tools/call", {"name": "ha__HassTurnOn", "arguments": {"name": "kitchen"}})
    assert r["result"]["content"][0]["text"] == "done kitchen" and r["result"]["structuredContent"] == {"ok": True}
    r = await rpc(server, a, "tools/call", {"name": "ha__bad_name_", "arguments": {}})
    assert not r["result"]["isError"]
    assert up.calls == [("HassTurnOn", {"name": "kitchen"}), ("bad name!", {})]    # the upstream's own names
    await e.gateway.close()


async def test_sharing_and_hidden_tools_limit_what_a_token_sees():
    e, server, up, a, b = make()
    await settle(e)
    srv = e.store.config.external_mcp[0]
    srv.tokens = ["tA"]
    srv.hidden_tools = ["HassTurnOff"]
    names = [t["name"] for t in (await rpc(server, b, "tools/list"))["result"]["tools"]]
    assert not any(n.startswith("ha__") for n in names)
    init = await rpc(server, b, "initialize", {})
    assert "ha__" not in init["result"]["instructions"]
    r = await rpc(server, b, "tools/call", {"name": "ha__HassTurnOn", "arguments": {}})
    assert r["error"]["message"] == "unknown tool: ha__HassTurnOn"
    names = [t["name"] for t in (await rpc(server, a, "tools/list"))["result"]["tools"]]
    assert "ha__HassTurnOn" in names and "ha__HassTurnOff" not in names
    r = await rpc(server, a, "tools/call", {"name": "ha__HassTurnOff", "arguments": {}})
    assert "error" in r and up.calls == []
    r = await rpc(server, a, "tools/call", {"name": "ha__HassTurnOn", "arguments": "nope"})
    assert r["error"]["message"] == "invalid arguments"
    srv.enabled = False                                       # turned off: gone at once, even before sync
    names = [t["name"] for t in (await rpc(server, a, "tools/list"))["result"]["tools"]]
    assert not any(n.startswith("ha__") for n in names)
    await e.gateway.close()


async def test_bad_upstream_credentials_redirects_and_expired_sessions():
    e, server, up, a, _ = make()
    e.store.config.external_mcp[0].token = "wrong"
    await settle(e)
    info = e.gateway.info("x1")
    assert info["status"] == "error" and "credentials" in info["error"] and "wrong" not in info["error"]
    names = [t["name"] for t in (await rpc(server, a, "tools/list"))["result"]["tools"]]
    assert not any(n.startswith("ha__") for n in names)

    e.store.config.external_mcp[0].token = UP_TOKEN           # new token -> new connection
    await settle(e)
    assert e.gateway.info("x1")["status"] == "connected"
    up.sessions.clear()                                       # the upstream restarted and forgot the session
    r = await rpc(server, a, "tools/call", {"name": "ha__HassTurnOn", "arguments": {"name": "x"}})
    assert r["result"]["content"][0]["text"] == "done x" and up.inits == 2    # re-initialized once

    up.redirect = True                                        # never followed (could leak the token elsewhere)
    r = await rpc(server, a, "tools/call", {"name": "ha__HassTurnOn", "arguments": {}})
    assert r["result"]["isError"] and "redirect" in r["result"]["content"][0]["text"]
    assert all("evil" not in x for x in up.seen_auth)
    await e.gateway.close()


async def test_oversized_answers_are_refused():
    e, server, up, a, _ = make()
    await settle(e)
    up.huge = True
    r = await rpc(server, a, "tools/call", {"name": "ha__HassTurnOn", "arguments": {}})
    assert r["result"]["isError"] and "too large" in r["result"]["content"][0]["text"]
    await e.gateway.close()


async def test_removed_servers_disconnect():
    e, server, up, a, _ = make()
    await settle(e)
    e.store.config.external_mcp = []
    await e.gateway.sync()
    assert e.gateway.ups == {} and up.sessions == set()      # the session was ended with DELETE
    names = [t["name"] for t in (await rpc(server, a, "tools/list"))["result"]["tools"]]
    assert not any(n.startswith("ha__") for n in names)


def test_names_and_cleaning():
    assert exposed_name("ha", "light.turn on") == "ha__light_turn_on"
    assert len(exposed_name("x" * 31, "y" * 100)) == 64
    got = clean_tools("ha", [{"name": "a"}, {"name": "a"}, {"name": ""}, {"nope": 1},
                             {"name": "b", "inputSchema": {"type": "object", "properties": {"p": {"description": "z" * 70000}}}}])
    assert [t["exposed"] for t in got] == ["ha__a"]          # duplicates, nameless and huge schemas dropped


async def test_client_speaks_plain_mcp_without_sessions():
    """Servers without Mcp-Session-Id (stateless) work too."""
    def handler(request):
        msg = json.loads(request.content)
        if "id" not in msg:
            return httpx.Response(202)
        result = {"protocolVersion": "2025-03-26", "serverInfo": {"name": "s"}} if msg["method"] == "initialize" \
            else {"tools": [{"name": "t", "inputSchema": {"type": "object"}}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": result})
    c = McpClient(ExternalMcpServer(name="s", url="http://127.0.0.1:9/mcp"), httpx.MockTransport(handler))
    assert [t["name"] for t in await c.list_tools()] == ["t"]
    assert c.protocol == "2025-03-26" and c.session_id == ""
    await c.close()
