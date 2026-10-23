"""MCP server: JSON-RPC handling, token scopes, contact/speaker allow-lists, origin check."""

import json
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from telerouter.mcp import McpServer
from telerouter.models import Config, Contact, McpToken, Speaker
from telerouter.security import new_token, token_hash


class FakeStore:
    def __init__(self):
        self.config = Config()

    def save(self):
        pass

    def agent(self, aid):
        return None

    def contact(self, cid):
        return next((c for c in self.config.contacts if c.id == cid), None)


class FakeCalls:
    """Records what connect_call did instead of dialling."""

    def __init__(self):
        self.ring, self.to_speaker, self.bridged = [], [], []

    async def ring_bridge(self, contact, me, token_id="", agent_id=""):
        self.ring.append((contact.id, me.id))
        return SimpleNamespace(id="rb")

    def call_to_speaker(self, contact, conn):
        self.to_speaker.append((contact.id, conn.id))
        return SimpleNamespace(id="sp")

    def get(self, sid):
        return None

    def bridge_to(self, inbound, contact):
        self.bridged.append(contact.id)
        return SimpleNamespace(id="br")


class FakeMic:                                  # a device (ClientConn) as far as platform goes
    def __init__(self, platform):
        self.cfg = SimpleNamespace(platform=platform)


class FakeSpeaker:
    def __init__(self, sid, name, platform):
        self.id, self.name = sid, name
        self.mic_dev = FakeMic(platform)


class FakeRt:
    def __init__(self, source):
        self.current_source = source


class FakeEngine:
    def __init__(self):
        self.store = FakeStore()
        self.speakers = {}
        self.agent_rts = {}
        self.machine_rts = {}
        self.calls = FakeCalls()
        self.rts = {}

    @property
    def agents(self):
        return self

    def get(self, aid):
        return self.rts.get(aid)

    def same_origin(self, origin, request):
        return origin == "https://telerouter.lan"

    class quiet:                       # quiet hours off
        @staticmethod
        async def calls_allowed():
            return True, ""


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
    cfg.speakers = [Speaker(id="k1", name="Kitchen", mic="0000000a"), Speaker(id="k2", name="Office")]
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


async def test_context_tools_only_for_agent_tokens(mcp):
    _, r = await call(mcp, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in r["result"]["tools"]}
    assert "reset_context" not in names and "context_status" not in names      # a normal user token
    agent_tok = new_token("trm_")
    mcp.engine.store.config.mcp_tokens.append(McpToken(name="agent", token_hash=token_hash(agent_tok), agent_id="a1"))
    _, r = await call(mcp, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, token=agent_tok)
    assert {"reset_context", "context_status"} <= {t["name"] for t in r["result"]["tools"]}
    _, r = await call(mcp, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "reset_context", "arguments": {"mode": "clear"}}}, token=agent_tok)
    assert r["result"]["isError"]                     # the fake engine runs no agent a1


async def test_garbled_spoken_names_find_the_connection(mcp):
    from telerouter.mcp import fuzzy_pick
    cs = [Contact(id=f"t{i}", kind="tmux", name=n) for i, n in enumerate(
        ["telerouter", "wa_logger", "pokerbalance", "truenas", "calls", "checker", "insta_logger"])]
    for said, want in (("Tether Outer T-Max", "telerouter"), ("Teller Outer", "telerouter"), ("WA logger", "wa_logger"),
                       ("poker balance session", "pokerbalance"), ("the true NAS tmux", "truenas")):
        got = fuzzy_pick(said, cs)
        assert got and got.name == want, (said, got and got.name)
    assert fuzzy_pick("kitchen lights", cs) is None
    mcp.engine.store.config.contacts = cs
    tok = mcp.engine.store.config.mcp_tokens[0]
    assert mcp._find_contact(tok, "Tether Outer T-Max").name == "telerouter"


def test_speakers_by_room_or_loose_name():
    from telerouter.mcp import pick_speaker
    cs = [Speaker(id="k1", name="Kitchen", room="kitchen"), Speaker(id="k2", name="Bedroom speaker", room="my room"),
          Speaker(id="k3", name="Office", room="office"), Speaker(id="k4", name="odam")]
    assert pick_speaker("the speaker in my room", cs).id == "k2"
    assert pick_speaker("bedroom", cs).id == "k2"
    assert pick_speaker("Kitchen speaker", cs).id == "k1"
    assert pick_speaker("k3", cs).id == "k3"
    assert pick_speaker("garage", cs) is None
    assert pick_speaker("Odam", cs).id == "k4"


async def test_new_session_is_only_for_telerouters_agents(mcp):
    _, r = await call(mcp, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "new_session" not in {t["name"] for t in r["result"]["tools"]}       # a plain token doesn't see it
    from telerouter.models import Machine

    class Rt:
        def __init__(self, name, allow):
            self.machine = Machine(name=name, host=name.replace(" ", "-") + ".lan", username="u", allow_new_sessions=allow)
    mcp.engine.machine_rts = {"a": Rt("claude lxc", True), "b": Rt("laptop", False)}
    assert mcp._session_machine("").machine.name == "claude lxc"
    assert mcp._session_machine("Claude LXC").machine.name == "claude lxc"
    from telerouter.mcp import ToolError
    with pytest.raises(ToolError, match="allows new sessions"):
        mcp._session_machine("laptop")



async def test_connect_call_rings_you_first_when_asked_from_the_phone(mcp):
    """"Hey Jarvis, call my mom" on the phone: ring my extension, connect mom when I answer."""
    from telerouter.agents.runtime import Source
    cfg = mcp.engine.store.config
    cfg.contacts += [Contact(id="me", kind="pbx", name="Salih", number="1005"),
                     Contact(id="mom", kind="pbx", name="Anne", alias="mom", number="1006")]
    cfg.calls.ring_first_contact = "me"
    cfg.speakers += [Speaker(id="0badc0de", name="Zfold7", mic="0badc0de", output="client:0badc0de")]
    mcp.engine.speakers["0badc0de"] = FakeSpeaker("0badc0de", "Zfold7", "android")
    mcp.engine.speakers["k1"] = FakeSpeaker("k1", "Kitchen", "Linux x86_64")
    tok = new_token("trm_")
    cfg.mcp_tokens.append(McpToken(name="jarvis", token_hash=token_hash(tok), agent_id="a1"))

    async def ask_from(platform, contact="mom", **args):
        src = Source("speaker", f"{platform} speaker", speaker_id="0badc0de")
        mcp.engine.rts["a1"] = FakeRt(src)
        mcp.engine.speakers["0badc0de"].mic_dev = FakeMic(platform)
        _, r = await call(mcp, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "connect_call",
                                           "arguments": {"contact": contact, **args}}}, token=tok)
        return r["result"]

    r = await ask_from("android")
    assert r["isError"] is False and r["structuredContent"]["status"] == "ringing Salih first"
    assert mcp.engine.calls.ring == [("mom", "me")]           # my extension first, then mom
    assert not mcp.engine.calls.to_speaker

    # the same request from a home speaker stays on the speaker
    await ask_from("Linux x86_64")
    assert mcp.engine.calls.to_speaker == [("mom", "0badc0de")]
    assert len(mcp.engine.calls.ring) == 1

    # a named speaker wins over the phone rule, and ring_first: false keeps it on the speaker
    await ask_from("android", speaker="Kitchen")
    assert mcp.engine.calls.to_speaker[-1] == ("mom", "k1")
    assert len(mcp.engine.calls.ring) == 1
    await ask_from("android", ring_first=False)
    assert mcp.engine.calls.to_speaker[-1] == ("mom", "0badc0de")
    assert len(mcp.engine.calls.ring) == 1

    # ring_first: true works from any speaker; turning the setting off needs the flag
    await ask_from("Linux x86_64", ring_first=True)
    assert mcp.engine.calls.ring[-1] == ("mom", "me")
    cfg.calls.ring_first_on_phone = False
    await ask_from("android")
    assert mcp.engine.calls.to_speaker[-1] == ("mom", "0badc0de")


async def test_connect_call_ring_first_needs_a_configured_extension(mcp):
    from telerouter.agents.runtime import Source
    cfg = mcp.engine.store.config
    cfg.contacts.append(Contact(id="mom", kind="pbx", name="Anne", alias="mom", number="1006"))
    mcp.engine.speakers["0badc0de"] = FakeSpeaker("0badc0de", "Zfold7", "android")
    tok = new_token("trm_")
    cfg.mcp_tokens.append(McpToken(name="jarvis", token_hash=token_hash(tok), agent_id="a1"))
    mcp.engine.rts["a1"] = FakeRt(Source("speaker", "Zfold7 speaker", speaker_id="0badc0de"))
    _, r = await call(mcp, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "connect_call",
                                       "arguments": {"contact": "mom", "ring_first": True}}}, token=tok)
    assert r["result"]["isError"] and "ring me first" in r["result"]["content"][0]["text"]
    assert not mcp.engine.calls.ring
