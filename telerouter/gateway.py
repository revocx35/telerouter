"""MCP gateway: telerouter connects to external MCP servers and re-serves their tools on its own /mcp.

An admin adds a server (Streamable HTTP, https or loopback http) under MCP access. telerouter is the MCP
*client*: it initializes a session, lists the tools and keeps them cached. Every telerouter MCP token the
server is shared with (all by default) then sees those tools as `<name>__<tool>` next to telerouter's own,
so an agent that already has telerouter's MCP gets them without any change on its side. Calls are forwarded
as they are and the upstream result goes back unchanged.

The upstream credential stays in telerouter (write-only in the API); agents only ever hold their trm_ token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from . import __version__
from .models import ExternalMcpServer, McpToken

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger("telerouter.gateway")

PROTOCOL = "2025-06-18"
MAX_RESPONSE = 4 * 1024 * 1024       # one upstream answer (a tool result, a page of tools)
MAX_TOOLS = 300                      # per server
MAX_SCHEMA = 64 * 1024               # one tool's inputSchema as JSON
MAX_DESC = 4000
CALL_TIMEOUT = 230                   # telerouter's own tool timeout is 240 s
PARALLEL = 8                         # calls in flight per server
REFRESH_S = 300                      # re-list tools of connected servers
RETRY_S = 60                         # retry servers that failed
NAME_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class GatewayError(Exception):
    """A readable reason (shown to the admin and to agents); never contains the upstream token."""


class _Expired(Exception):
    """The upstream forgot our session (HTTP 404 with a session id): initialize again."""


def ssl_context(pem: str) -> ssl.SSLContext | bool:
    if not pem:
        return True                                    # public CAs
    ctx = ssl.create_default_context(cadata=pem)       # the pinned CA is the only trust anchor; hostname checked
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN  # a pinned self-signed leaf works too
    return ctx


def exposed_name(prefix: str, tool: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"{prefix}__{tool}")[:64]


class McpClient:
    """One Streamable HTTP connection to an external MCP server."""

    def __init__(self, srv: ExternalMcpServer, transport: httpx.AsyncBaseTransport | None = None):
        self.url = srv.url
        headers = {"User-Agent": f"telerouter/{__version__}"}
        if srv.token:
            headers["Authorization"] = f"Bearer {srv.token}"
        self._http = httpx.AsyncClient(verify=ssl_context(srv.tls_ca), follow_redirects=False, headers=headers,
                                       timeout=httpx.Timeout(20, read=CALL_TIMEOUT), transport=transport)
        self.session_id = ""
        self.protocol = ""
        self.server_info: dict = {}
        self.instructions = ""
        self._id = 0
        self._ready = False
        self._init_lock = asyncio.Lock()

    async def close(self) -> None:
        if self.session_id:
            try:
                await self._http.delete(self.url, headers=self._session_headers(), timeout=5)
            except httpx.HTTPError:
                pass
        await self._http.aclose()

    def _session_headers(self) -> dict:
        h = {}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.protocol:
            h["MCP-Protocol-Version"] = self.protocol
        return h

    async def _post(self, msg: dict) -> dict | None:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                   **self._session_headers()}
        try:
            async with self._http.stream("POST", self.url, content=json.dumps(msg).encode(), headers=headers) as r:
                code = r.status_code
                if code == 404 and self.session_id:
                    raise _Expired()
                if code in (401, 403):
                    raise GatewayError(f"the server refused telerouter's credentials (HTTP {code})")
                if 300 <= code < 400:
                    raise GatewayError(f"the server answered with a redirect (HTTP {code}); use the final URL")
                if code >= 400:
                    raise GatewayError(f"the server answered HTTP {code}")
                if sid := r.headers.get("mcp-session-id"):
                    if len(sid) > 512 or not sid.isprintable():
                        raise GatewayError("the server sent an invalid session id")
                    self.session_id = sid
                if "id" not in msg or code == 202:
                    return None
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype == "text/event-stream":
                    return await _from_sse(r, msg["id"])
                if ctype != "application/json":
                    raise GatewayError(f"unexpected answer type '{ctype[:40]}'")
                raw = b""
                async for chunk in r.aiter_bytes():
                    raw += chunk
                    if len(raw) > MAX_RESPONSE:
                        raise GatewayError("the answer is too large")
                data = json.loads(raw)
                if isinstance(data, list):                      # a batch answer to one request
                    data = next((x for x in data if isinstance(x, dict) and x.get("id") == msg["id"]), None)
                return data if isinstance(data, dict) else None
        except httpx.TimeoutException as e:
            raise GatewayError("the server did not answer in time") from e
        except httpx.HTTPError as e:
            raise GatewayError(f"cannot reach the server ({type(e).__name__})") from e
        except ValueError as e:
            raise GatewayError("the server sent invalid JSON") from e

    async def _request(self, method: str, params: dict | None = None) -> Any:
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        res = await self._post(msg)
        if not isinstance(res, dict):
            raise GatewayError(f"no answer to {method}")
        if "error" in res:
            err = res["error"] if isinstance(res["error"], dict) else {}
            raise GatewayError(str(err.get("message") or "error")[:300])
        return res.get("result")

    async def connect(self) -> None:
        async with self._init_lock:
            self.session_id, self.protocol, self._ready = "", "", False
            res = await self._request("initialize", {
                "protocolVersion": PROTOCOL, "capabilities": {},
                "clientInfo": {"name": "telerouter", "version": __version__}})
            if not isinstance(res, dict):
                raise GatewayError("invalid initialize answer")
            self.protocol = str(res.get("protocolVersion") or PROTOCOL)[:20]
            self.server_info = res.get("serverInfo") if isinstance(res.get("serverInfo"), dict) else {}
            self.instructions = str(res.get("instructions") or "")[:2000]
            await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self._ready = True

    async def request(self, method: str, params: dict | None = None) -> Any:
        if not self._ready:
            await self.connect()
        try:
            return await self._request(method, params)
        except _Expired:
            await self.connect()
            return await self._request(method, params)

    async def list_tools(self) -> list[dict]:
        tools, cursor = [], None
        for _ in range(20):
            res = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            if not isinstance(res, dict) or not isinstance(res.get("tools"), list):
                raise GatewayError("invalid tools/list answer")
            tools += [t for t in res["tools"] if isinstance(t, dict)]
            cursor = res.get("nextCursor")
            if not cursor or len(tools) >= MAX_TOOLS:
                break
        return tools[:MAX_TOOLS]

    async def call_tool(self, name: str, args: dict) -> dict:
        res = await self.request("tools/call", {"name": name, "arguments": args})
        if not isinstance(res, dict) or not isinstance(res.get("content", []), list):
            raise GatewayError("invalid tools/call answer")
        return res


async def _from_sse(r: httpx.Response, want_id: Any) -> dict | None:
    """The JSON-RPC answer with our id from a text/event-stream body (notifications on the way are skipped)."""
    size, data = 0, []
    async for line in r.aiter_lines():
        size += len(line) + 1
        if size > MAX_RESPONSE:
            raise GatewayError("the answer is too large")
        if line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
        elif line == "" and data:
            try:
                msg = json.loads("\n".join(data))
            except ValueError:
                msg = None
            data = []
            if isinstance(msg, dict) and msg.get("id") == want_id and ("result" in msg or "error" in msg):
                return msg
    return None


@dataclass
class Upstream:
    srv: ExternalMcpServer
    client: McpClient
    sig: str
    status: str = "connecting"                 # connecting | connected | error | off
    error: str = ""
    tools: list[dict] = field(default_factory=list)       # cleaned: name, exposed, description, inputSchema, ...
    refreshed: float = 0.0
    sem: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(PARALLEL))
    task: asyncio.Task | None = None


def _sig(s: ExternalMcpServer) -> str:
    """What needs a new connection when it changes (sharing, hidden tools and the description do not)."""
    return json.dumps([s.name, s.url, s.token, s.tls_ca, s.enabled])


def clean_tools(prefix: str, raw: list[dict]) -> list[dict]:
    out, seen = [], set()
    for t in raw:
        name = t.get("name")
        if not isinstance(name, str) or not name or len(name) > 128:
            continue
        exp = exposed_name(prefix, name)
        if exp in seen or not NAME_OK.match(exp):
            continue
        schema = t.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            schema = {"type": "object", "properties": {}}
        if len(json.dumps(schema)) > MAX_SCHEMA:
            log.warning("external MCP '%s': tool %s skipped (schema too large)", prefix, name[:64])
            continue
        seen.add(exp)
        item = {"name": name, "exposed": exp, "inputSchema": schema,
                "description": f"[{prefix}] " + str(t.get("description") or "")[:MAX_DESC]}
        if isinstance(t.get("title"), str):
            item["title"] = t["title"][:200]
        if isinstance(t.get("annotations"), dict):
            item["annotations"] = t["annotations"]
        if isinstance(t.get("outputSchema"), dict) and len(json.dumps(t["outputSchema"])) <= MAX_SCHEMA:
            item["outputSchema"] = t["outputSchema"]
        out.append(item)
    return out


class Gateway:
    def __init__(self, engine: "Engine", transport: httpx.AsyncBaseTransport | None = None):
        self.engine = engine
        self.transport = transport                    # tests
        self.ups: dict[str, Upstream] = {}

    @property
    def servers(self) -> list[ExternalMcpServer]:
        return self.engine.store.config.external_mcp

    # -- lifecycle -------------------------------------------------------------------------------------------
    async def sync(self) -> None:
        """Follow the config: new/changed servers (re)connect in the background, removed ones close."""
        want = {s.id: s for s in self.servers}
        for sid in list(self.ups):
            if sid not in want or self.ups[sid].sig != _sig(want[sid]):
                await self._drop(sid)
        for sid, s in want.items():
            up = self.ups.get(sid)
            if up:
                up.srv = s                            # name/description/sharing changes apply at once
                continue
            up = Upstream(s, McpClient(s, self.transport), _sig(s), status="connecting" if s.enabled else "off")
            self.ups[sid] = up
            if s.enabled:
                up.task = asyncio.create_task(self.refresh(sid))

    async def _drop(self, sid: str) -> None:
        up = self.ups.pop(sid, None)
        if up:
            if up.task and not up.task.done():
                up.task.cancel()
            await up.client.close()

    async def close(self) -> None:
        for sid in list(self.ups):
            await self._drop(sid)

    async def refresh(self, sid: str) -> None:
        up = self.ups.get(sid)
        if not up or not up.srv.enabled:
            return
        try:
            if up.status != "connected":
                await asyncio.wait_for(up.client.connect(), 30)
            raw = await asyncio.wait_for(up.client.list_tools(), 30)
            up.tools = clean_tools(up.srv.name, raw)
            if up.status != "connected":
                log.info("external MCP '%s' connected: %d tools", up.srv.name, len(up.tools))
            up.status, up.error, up.refreshed = "connected", "", time.time()
        except (GatewayError, TimeoutError) as e:
            msg = str(e) or "the server did not answer in time"
            if up.status != "error" or up.error != msg:
                log.warning("external MCP '%s': %s", up.srv.name, msg)
            up.status, up.error, up.refreshed = "error", msg, time.time()
            up.client._ready = False
        except Exception as e:                        # never let a background refresh die silently
            log.exception("external MCP '%s': refresh failed", up.srv.name)
            up.status, up.error, up.refreshed = "error", f"failed ({type(e).__name__})", time.time()
            up.client._ready = False

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(RETRY_S)
            now = time.time()
            for sid, up in list(self.ups.items()):
                due = REFRESH_S if up.status == "connected" else RETRY_S
                if up.srv.enabled and now - up.refreshed >= due and not (up.task and not up.task.done()):
                    up.task = asyncio.create_task(self.refresh(sid))

    # -- what a token sees -----------------------------------------------------------------------------------
    @staticmethod
    def shared_with(s: ExternalMcpServer, tok: McpToken) -> bool:
        return "*" in s.tokens or tok.id in s.tokens

    def _usable(self, tok: McpToken):
        for up in self.ups.values():
            if up.srv.enabled and up.status == "connected" and self.shared_with(up.srv, tok):
                yield up

    def tools_for(self, tok: McpToken) -> list[dict]:
        out = []
        for up in self._usable(tok):
            for t in up.tools:
                if t["name"] in up.srv.hidden_tools:
                    continue
                d = {"name": t["exposed"], "description": t["description"], "inputSchema": t["inputSchema"]}
                for k in ("title", "annotations", "outputSchema"):
                    if k in t:
                        d[k] = t[k]
                out.append(d)
        return out

    def find(self, tok: McpToken, exposed: str) -> tuple[Upstream, str] | None:
        for up in self._usable(tok):
            for t in up.tools:
                if t["exposed"] == exposed and t["name"] not in up.srv.hidden_tools:
                    return up, t["name"]
        return None

    def instructions(self, tok: McpToken) -> str:
        parts = []
        for up in self._usable(tok):
            what = up.srv.description or up.client.server_info.get("name") or "an external MCP server"
            parts.append(f"{up.srv.name}__* tools come from {up.srv.name} ({what})")
        if not parts:
            return ""
        return "telerouter also passes on tools of other MCP servers: " + "; ".join(parts) + "."

    async def call(self, up: Upstream, name: str, args: dict) -> dict:
        async with up.sem:
            try:
                res = await asyncio.wait_for(up.client.call_tool(name, args), CALL_TIMEOUT)
            except TimeoutError:
                return _error(f"{up.srv.name} did not answer in time")
            except GatewayError as e:
                if "cannot reach" in str(e):
                    up.status, up.error = "error", str(e)
                    up.client._ready = False
                return _error(f"{up.srv.name}: {e}")
            except Exception as e:
                log.exception("external MCP '%s': call %s failed", up.srv.name, name[:64])
                return _error(f"{up.srv.name}: failed ({type(e).__name__})")
        out = {"content": res.get("content") or [], "isError": bool(res.get("isError"))}
        if isinstance(res.get("structuredContent"), dict):
            out["structuredContent"] = res["structuredContent"]
        return out

    # -- admin view -----------------------------------------------------------------------------------------
    def info(self, sid: str) -> dict:
        up = self.ups.get(sid)
        if not up:
            return {"status": "off", "error": "", "tools": [], "refreshed": 0, "server": {}}
        return {"status": up.status if up.srv.enabled else "off", "error": up.error, "refreshed": up.refreshed,
                "server": {k: str(up.client.server_info.get(k, ""))[:80] for k in ("name", "version")},
                "tools": [{"name": t["name"], "exposed": t["exposed"], "description": t["description"][:300],
                           "hidden": t["name"] in up.srv.hidden_tools} for t in up.tools]}


def _error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}
