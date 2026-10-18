"""telerouter as an MCP server: Streamable HTTP transport, JSON responses, tools only, stateless.

Served at POST /mcp on the HTTPS port (any MCP client with a token) and on the loopback listener that
the SSH reverse tunnels of telerouter's own agents reach. Every request needs `Authorization: Bearer
trm_...`; the token's scopes and contact/speaker lists limit what its tools see and do.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import re
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import __version__
from .models import Contact, McpToken
from .router.calls import CallError
from .security import bearer, token_matches

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger("telerouter.mcp")

PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
MAX_BODY = 256 * 1024

INSTRUCTIONS = (
    "telerouter routes voice: speakers in the home, phone extensions on a PBX, WhatsApp contacts (wa2sip) and "
    "cameras (cam2sip). Contacts have a referred name (e.g. 'dad'); use list_contacts to find them. "
    "connect_call links a contact to a speaker (or to the phone caller who asked you). phone_message and "
    "start_voice_call/say/listen/hang_up let you talk on the phone yourself. announce/ask_speaker use a speaker. "
    "Only call people the user asked you to call.")


class ToolError(Exception):
    pass


@dataclass
class Tool:
    name: str
    scope: str
    description: str
    schema: dict
    fn: Callable[..., Awaitable[Any]]


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


S = {"type": "string"}
KIND_WORDS = {"pbx": "extension phone desk", "whatsapp": "whatsapp wa mobile", "camera": "camera cameras cam intercom door",
              "tmux": "tmux session terminal project agent claude code"}


class McpServer:
    def __init__(self, engine: "Engine"):
        self.engine = engine
        self.tools: dict[str, Tool] = {}
        self._define()

    # -- auth / context -------------------------------------------------------------------------------
    def authenticate(self, request: Request) -> McpToken | None:
        tok = bearer(request.headers)
        if not tok.startswith("trm_"):
            return None
        for t in self.engine.store.config.mcp_tokens:
            if token_matches(tok, t.token_hash):
                if time.time() - t.last_used > 60:
                    t.last_used = time.time()
                    self.engine.store.save()
                return t
        return None

    def _contacts(self, tok: McpToken) -> list[Contact]:
        return [c for c in self.engine.store.config.contacts
                if c.enabled and not c.missing and tok.allows_contact(c.id)]

    def _find_contact(self, tok: McpToken, ref: str) -> Contact:
        ref_l = " ".join(ref.lower().split())
        cs = self._contacts(tok)
        for pred in (lambda c: c.id == ref,
                     lambda c: c.label.lower() == ref_l,
                     lambda c: c.alias.lower() == ref_l or c.name.lower() == ref_l,
                     lambda c: c.number and c.number.lstrip("+") == ref_l.lstrip("+"),
                     lambda c: ref_l in c.label.lower() or ref_l in c.name.lower()):
            hits = [c for c in cs if pred(c)]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise ToolError(f"'{ref}' matches several contacts: " + ", ".join(f"{c.label} ({c.id})" for c in hits)
                                + ". Use the id.")
        best = fuzzy_pick(ref, cs)
        if best is not None:
            return best
        raise ToolError(f"no contact '{ref}' (see list_contacts)")

    def _find_speaker(self, tok: McpToken, ref: str):
        e = self.engine
        cands = [c for c in e.store.config.speakers if c.visible_to_agents and tok.allows_client(c.id)]
        c = pick_speaker(ref, cands)
        if c is None:
            raise ToolError(f"no speaker '{ref}' (see list_speakers)")
        conn = e.speakers.get(c.id)
        if not conn:
            raise ToolError(f"speaker '{c.name}' is offline")
        return conn

    def _source(self, tok: McpToken):
        rt = self.engine.agents.get(tok.agent_id) if tok.agent_id else None
        return rt.current_source if rt else None

    # -- tools ------------------------------------------------------------------------------------------
    def _define(self) -> None:
        def tool(name, scope, desc, schema):
            def deco(fn):
                self.tools[name] = Tool(name, scope, desc, schema, fn)
                return fn
            return deco

        @tool("list_contacts", "contacts",
              "List the people/places you may call: referred name, kind (pbx = phone extension, whatsapp, camera), "
              "notes. Filter by kind and/or text (matches names, notes and the kind).",
              _obj({"query": {**S, "description": "optional filter text"},
                    "kind": {"type": "string", "enum": ["pbx", "whatsapp", "camera", "tmux"]}}))
        async def list_contacts(tok, query: str = "", kind: str = ""):
            q = query.lower().strip()
            out = []
            for c in self._contacts(tok):
                if kind and c.kind != kind:
                    continue
                hay = " ".join((c.label, c.name, c.description, c.kind, KIND_WORDS.get(c.kind, ""))).lower()
                if q and not all(w in hay for w in q.split()):
                    continue
                out.append({"id": c.id, "name": c.label, "kind": c.kind, "notes": c.description,
                            "number": c.number if c.kind != "camera" else ""})
            return {"contacts": out}

        @tool("list_speakers", "clients", "List the speakers and whether they are online (a speaker without a "
              "microphone can only announce).", _obj({}))
        async def list_speakers(tok):
            e = self.engine
            out = []
            for c in e.store.config.speakers:
                if c.visible_to_agents and tok.allows_client(c.id):
                    conn = e.speakers.get(c.id)
                    out.append({"id": c.id, "name": c.name, "room": c.room, "online": bool(conn),
                                "state": conn.state if conn else "offline", "microphone": bool(c.mic)})
            src = self._source(tok)
            return {"speakers": out, "current_request_from": src.label if src else None}

        @tool("connect_call", "calls",
              "Call a contact and connect them for a live conversation with a person: by default the speaker the "
              "current voice request came from (or the phone caller who asked). Returns when the call is dialing. "
              "For a tmux contact (an agent session on a machine) the person is switched to talk to that session "
              "until they say 'back to <your name>'.",
              _obj({"contact": {**S, "description": "referred name or id"},
                    "speaker": {**S, "description": "speaker name, room or id ('the speaker in my room'); omit to use the "
                                                  "requesting speaker. Ring a speaker even when the request came by phone."}},
                   ["contact"]))
        async def connect_call(tok, contact: str, speaker: str = ""):
            e = self.engine
            c = self._find_contact(tok, contact)
            src = self._source(tok)
            if c.kind == "tmux":
                return self._connect_tmux(tok, c, src, speaker)
            if not src:                                   # placed by an agent on its own, not asked by voice now
                await self._quiet_gate("calls")
            if speaker:
                conn = self._find_speaker(tok, speaker)
            elif src and src.kind == "speaker" and src.speaker_id:
                conn = e.speakers.get(src.speaker_id)
                if not conn:
                    raise ToolError("the requesting speaker went offline")
                if not tok.allows_client(conn.id):
                    raise ToolError("this token may not use that speaker")
            elif src and src.kind == "phone" and src.call_id:
                inbound = e.calls.get(src.call_id)
                if not inbound:
                    raise ToolError("the phone call has ended")
                s = e.calls.bridge_to(inbound, c)
                return {"call_id": s.id, "status": "connecting the phone caller", "to": c.label}
            else:
                raise ToolError("which speaker? (no voice request in progress; pass speaker)")
            s = e.calls.call_to_speaker(c, conn)
            return {"call_id": s.id, "status": "dialing", "to": c.label, "speaker": conn.name}

        @tool("phone_message", "calls",
              "Phone a contact and speak a message to them (text-to-speech), optionally wait for a spoken reply, "
              "then hang up. Blocks until done (up to ~2 minutes). Use for notifications and quick questions.",
              _obj({"contact": S, "message": S,
                    "wait_for_reply": {"type": "boolean", "default": False},
                    "repeat": {"type": "integer", "minimum": 1, "maximum": 3,
                               "description": "how often to read it; default 1 when waiting for a reply, else 2"}},
                   ["contact", "message"]))
        async def phone_message(tok, contact: str, message: str, wait_for_reply: bool = False, repeat: int = 0):
            await self._quiet_gate("calls")
            repeat = repeat or (1 if wait_for_reply else 2)
            e = self.engine
            c = self._find_contact(tok, contact)
            if c.kind == "camera":
                raise ToolError("cameras can't take phone messages; use start_voice_call")
            agent = e.store.agent(tok.agent_id) if tok.agent_id else None
            intro = f"Hello, this is {agent.name if agent else 'telerouter'} with a message."
            res = await e.calls.phone_message(c, message[:2000], wait_for_reply, repeat, tok.id, tok.agent_id, intro)
            return {"to": c.label, **res}

        @tool("start_voice_call", "calls",
              "Call a contact and keep the line open for you to talk: then use say / listen / hang_up. "
              "Returns once answered.", _obj({"contact": S}, ["contact"]))
        async def start_voice_call(tok, contact: str):
            await self._quiet_gate("calls")
            c = self._find_contact(tok, contact)
            s = await self.engine.calls.start_voice_call(c, tok.id, tok.agent_id)
            return {"call_id": s.id, "status": "answered", "to": c.label}

        @tool("say", "calls",
              "Speak text in a call you started; with listen (default) waits for the reply and returns the transcript.",
              _obj({"call_id": S, "text": S, "listen": {"type": "boolean", "default": True},
                    "listen_timeout": {"type": "number", "minimum": 2, "maximum": 60, "default": 12}},
                   ["call_id", "text"]))
        async def say(tok, call_id: str, text: str, listen: bool = True, listen_timeout: float = 12):
            s = self._own_call(tok, call_id)
            return await self.engine.calls.say(s, text[:2000], listen, listen_timeout)

        @tool("listen", "calls", "Wait for the other side to say something and return the transcript.",
              _obj({"call_id": S, "timeout": {"type": "number", "minimum": 2, "maximum": 60, "default": 12}},
                   ["call_id"]))
        async def listen(tok, call_id: str, timeout: float = 12):
            return await self.engine.calls.listen(self._own_call(tok, call_id), timeout)

        @tool("hang_up", "calls", "End a call (one you started, or a speaker call).", _obj({"call_id": S}, ["call_id"]))
        async def hang_up(tok, call_id: str):
            s = self.engine.calls.get(call_id)
            if not s or (s.token_id and s.token_id != tok.id) or \
                    (s.contact and not tok.allows_contact(s.contact.id)):
                raise ToolError("no such call")
            await self.engine.calls.hangup(s, "hung up by agent")
            return {"ok": True}

        @tool("list_calls", "calls", "Calls in progress.", _obj({}))
        async def list_calls(tok):
            out = []
            for s in self.engine.calls.active():
                if s.contact and not tok.allows_contact(s.contact.id):
                    continue
                out.append({"call_id": s.id, "with": s.label, "mode": s.mode, "state": s.state,
                            "direction": s.direction})
            return {"calls": out}

        @tool("new_session", "agent",
              "Start a new tmux session with a fresh agent (Claude Code, or Hermes on Hermes machines) on a machine "
              "and connect the person who asked to it: from their next sentence on they talk to that session until "
              "they say 'back to <your name>'. Only on machines where the owner allowed new sessions.",
              _obj({"name": {**S, "description": "session name: letters, digits, - _ . (e.g. pokerbot)"},
                    "folder": {**S, "description": "working directory, e.g. ~/projects/pokerbot (created if missing); "
                                                 "default the home folder"},
                    "machine": {**S, "description": "machine name; omit when only one machine allows new sessions"},
                    "connect": {"type": "boolean", "default": True, "description": "connect the person to it now"}},
                   ["name"]))
        async def new_session(tok, name: str, folder: str = "~", machine: str = "", connect: bool = True):
            from .agents.runtime import AgentError
            e = self.engine
            if "*" not in tok.contacts:
                raise ToolError("this token may only use the contacts it lists")
            rt = self._session_machine(machine)
            try:
                info = await rt.new_session(name, folder or "~")
            except AgentError as ex:
                raise ToolError(str(ex)) from ex
            started = {"started": info["name"], "machine": rt.machine.name, "folder": info.get("path", "")}
            c = next((c for c in e.store.config.contacts if c.kind == "tmux" and c.source == rt.machine.id
                      and c.source_ref == info["name"]), None)
            src = self._source(tok)
            if not connect or c is None or not src:
                return started
            return {**started, **self._connect_tmux(tok, c, src, "")}

        @tool("context_status", "agent",
              "Your own conversation size in tokens (measured after your last finished turn) and whether a "
              "context reset is already scheduled.", _obj({}))
        async def context_status(tok):
            rt = self._own_agent(tok)
            return {"context_tokens": rt.context_tokens, "reset_scheduled": bool(rt.pending_reset),
                    "note": "0 means not measured yet"}

        @tool("reset_context", "agent",
              "Empty your own conversation right after your current reply, like typing /compact or /clear in your "
              "terminal. mode 'compact' summarizes and keeps what `keep` names (projects in progress, open tasks); "
              "mode 'clear' drops everything. Only do this when the user asked you to manage your context.",
              _obj({"mode": {"type": "string", "enum": ["compact", "clear"]},
                    "keep": {**S, "description": "compact: what the summary must keep (e.g. 'the Proxmox backup work in progress')"}},
                   ["mode"]))
        async def reset_context(tok, mode: str, keep: str = ""):
            rt = self._own_agent(tok)
            from .agents.runtime import AgentError
            try:
                when = rt.request_reset(mode, keep)
            except AgentError as ex:
                raise ToolError(str(ex)) from ex
            return {"scheduled": mode, "when": when,
                    "note": "Finish your reply normally; the reset happens right after it."}

        @tool("announce", "speak", "Say something out loud on a speaker (e.g. a reminder or a notification).",
              _obj({"speaker": S, "text": S}, ["speaker", "text"]))
        async def announce(tok, speaker: str, text: str):
            await self._quiet_gate("speaker announcements")
            conn = self._find_speaker(tok, speaker)
            try:
                await self.engine.voice.announce(conn, text[:2000])
            except RuntimeError as ex:
                raise ToolError(str(ex)) from ex
            return {"ok": True, "speaker": conn.name}

        @tool("ask_speaker", "speak",
              "Ask a question out loud on a speaker and return what the person answers (after a beep).",
              _obj({"speaker": S, "question": S,
                    "timeout": {"type": "number", "minimum": 3, "maximum": 30, "default": 8}},
                   ["speaker", "question"]))
        async def ask_speaker(tok, speaker: str, question: str, timeout: float = 8):
            await self._quiet_gate("speaker questions")
            conn = self._find_speaker(tok, speaker)
            try:
                heard = await self.engine.voice.ask_speaker(conn, question[:1000], timeout)
            except RuntimeError as ex:
                raise ToolError(str(ex)) from ex
            return {"heard": heard}

    def _connect_tmux(self, tok: McpToken, c: Contact, src, speaker: str) -> dict:
        e = self.engine
        try:
            route = e.route_for(c)
        except ValueError as ex:
            raise ToolError(str(ex)) from ex
        agent = e.store.agent(tok.agent_id) if tok.agent_id else None
        back = f"back to {agent.name}" if agent else "back"
        if speaker or (src and src.kind == "speaker"):
            conn = self._find_speaker(tok, speaker) if speaker else e.speakers.get(src.speaker_id)
            if not conn:
                raise ToolError("the requesting speaker went offline")
            conn.route = route
            where = f"the {conn.name} speaker"
        elif src and src.kind == "phone" and src.call_id and e.calls.get(src.call_id):
            e.calls.get(src.call_id).route = route   # type: ignore[union-attr]
            where = "this phone call"
        else:
            raise ToolError("which speaker? (no voice request in progress; pass speaker)")
        return {"connected": c.label, "where": where,
                "note": f"From the next sentence on, the person talks to the {c.label} session directly. Tell them "
                        f"in a few words, and that saying '{back}' returns to you."}

    def _session_machine(self, ref: str):
        rts = [rt for rt in self.engine.machine_rts.values() if rt.machine.allow_new_sessions]
        if ref:
            r = " ".join(ref.lower().split())
            hits = [rt for rt in rts if r in (rt.machine.id, rt.machine.name.lower())] or \
                   [rt for rt in rts if r in rt.machine.name.lower() or r in rt.machine.host.lower()]
            if len(hits) == 1:
                return hits[0]
            if not hits:
                raise ToolError(f"no machine '{ref}' allows new sessions"
                                + (f" (these do: {', '.join(rt.machine.name for rt in rts)})" if rts else ""))
            rts = hits
        if not rts:
            raise ToolError("no machine allows new sessions; the owner can turn it on under Agents & machines")
        if len(rts) > 1:
            raise ToolError("which machine? " + ", ".join(rt.machine.name for rt in rts))
        return rts[0]

    async def _quiet_gate(self, what: str) -> None:
        allowed, why = await self.engine.quiet.calls_allowed()
        if not allowed:
            raise ToolError(f"quiet hours: no {what} now ({why}). Don't retry until the quiet hours end; "
                            "tell the person later instead.")

    def _own_agent(self, tok: McpToken):
        rt = self.engine.agents.get(tok.agent_id) if tok.agent_id else None
        if not rt:
            raise ToolError("only agents that telerouter runs can manage their context")
        return rt

    def _own_call(self, tok: McpToken, call_id: str):
        s = self.engine.calls.get(call_id)
        if not s or s.mode != "mcp" or s.token_id != tok.id:
            raise ToolError("no such call (or it has ended)")
        return s

    # -- JSON-RPC -------------------------------------------------------------------------------------------
    @staticmethod
    def _allowed(t: Tool, tok: McpToken) -> bool:
        return t.scope in tok.scopes or (t.scope == "agent" and bool(tok.agent_id))

    def _visible(self, tok: McpToken) -> list[Tool]:
        return [t for t in self.tools.values() if self._allowed(t, tok)]

    async def handle(self, request: Request) -> Response:
        if request.method != "POST":
            return Response(status_code=405, headers={"Allow": "POST"})
        origin = request.headers.get("origin")
        if origin and not self.engine.same_origin(origin, request):
            return JSONResponse({"error": "origin not allowed"}, status_code=403)
        tok = self.authenticate(request)
        if not tok:
            return JSONResponse({"error": "unauthorized"}, status_code=401,
                                headers={"WWW-Authenticate": 'Bearer realm="telerouter"'})
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({"error": "too large"}, status_code=413)
        try:
            msg = json.loads(body)
        except ValueError:
            return JSONResponse(_err(None, -32700, "parse error"), status_code=400)
        if isinstance(msg, list):
            results = [r for r in [await self._one(tok, m) for m in msg[:20]] if r is not None]
            return JSONResponse(results) if results else Response(status_code=202)
        res = await self._one(tok, msg)
        if res is None:
            return Response(status_code=202)
        return JSONResponse(res)

    async def _one(self, tok: McpToken, msg: Any) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _err(None, -32600, "invalid request")
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if mid is None:                   # notification (initialized, cancelled, ...)
            return None
        if method == "initialize":
            want = str(params.get("protocolVersion") or "")
            return _ok(mid, {"protocolVersion": want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                             "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": {"name": "telerouter", "version": __version__},
                             "instructions": INSTRUCTIONS})
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            return _ok(mid, {"tools": [{"name": t.name, "description": t.description, "inputSchema": t.schema}
                                       for t in self._visible(tok)]})
        if method == "tools/call":
            name = str(params.get("name") or "")
            args = params.get("arguments") or {}
            t = self.tools.get(name)
            if not t or not self._allowed(t, tok):
                return _err(mid, -32602, f"unknown tool: {name}")
            if not isinstance(args, dict) or set(args) - set(t.schema["properties"]):
                return _err(mid, -32602, "invalid arguments")
            for k in t.schema.get("required", []):
                if k not in args:
                    return _err(mid, -32602, f"missing argument: {k}")
            log.info("MCP %s by '%s': %s", name, tok.name, _brief(args))
            try:
                result = await asyncio.wait_for(t.fn(tok, **args), 240)
                return _ok(mid, {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                                 "structuredContent": result, "isError": False})
            except (ToolError, CallError) as ex:
                return _ok(mid, {"content": [{"type": "text", "text": str(ex)}], "isError": True})
            except TimeoutError:
                return _ok(mid, {"content": [{"type": "text", "text": "timed out"}], "isError": True})
            except TypeError as ex:
                return _err(mid, -32602, f"invalid arguments: {ex}")
            except Exception as ex:
                log.exception("MCP tool %s failed", name)
                return _ok(mid, {"content": [{"type": "text", "text": f"failed: {ex.__class__.__name__}"}],
                                 "isError": True})
        return _err(mid, -32601, f"method not found: {method}")


def _ok(mid, result) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _brief(args: dict) -> str:
    s = json.dumps(args, ensure_ascii=False)
    return s if len(s) < 200 else s[:200] + "..."


_FILLER = {"the", "my", "a", "tmux", "t", "max", "session", "sessions", "terminal", "project", "contact", "please",
           "to", "me", "connect", "call", "with"}


def _squash(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower().replace("_", " "))
    return "".join(w for w in words if w not in _FILLER)


def fuzzy_pick(ref: str, contacts: list[Contact]) -> Contact | None:
    """Speech recognition mangles names ("Tether Outer T-Max" for the telerouter tmux): the closest name
    wins when it is clearly closer than the next one."""
    target = _squash(ref)
    if len(target) < 3:
        return None
    scored = []
    for c in contacts:
        best = max(difflib.SequenceMatcher(None, target, _squash(n)).ratio() for n in {c.label, c.name} if _squash(n))\
            if _squash(c.label) or _squash(c.name) else 0.0
        scored.append((best, c))
    scored.sort(key=lambda x: -x[0])
    if not scored or scored[0][0] < 0.72:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.08:
        return None
    return scored[0][1]


_SPEAKER_FILLER = {"the", "a", "speaker", "speakers", "in", "on", "at", "of", "from", "please"}


def _speaker_words(text: str) -> str:
    return " ".join(w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in _SPEAKER_FILLER)


def pick_speaker(ref, clients):
    """'the speaker in my room' -> the speaker named/roomed 'my room' (or the one clearly closest)."""
    r = _speaker_words(ref)
    for c in clients:                                   # exact id, name or room
        if ref.strip().lower() in (c.id, c.name.lower(), c.room.lower()) or (r and r in (_speaker_words(c.name), _speaker_words(c.room))):
            return c
    hits = [c for c in clients if r and (r in _speaker_words(c.name + " " + c.room) or
                                         any(_speaker_words(x) and _speaker_words(x) in r for x in (c.name, c.room)))]
    if len(hits) == 1:
        return hits[0]
    scored = sorted(((max(difflib.SequenceMatcher(None, r, _speaker_words(x)).ratio() for x in (c.name, c.room)), c)
                     for c in clients), key=lambda x: -x[0])
    if scored and scored[0][0] >= 0.75 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.1):
        return scored[0][1]
    return None
