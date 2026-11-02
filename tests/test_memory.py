"""Shared memory: notes in folders (versions, trash, moves), messages between agents, the MCP tools, the web API."""

import time

import pytest
from fastapi.testclient import TestClient

from telerouter import tls
from telerouter.engine import Engine
from telerouter.memory import (KEEP_REVISIONS, MemError, SharedMemory, Who, norm_path, norm_title)
from telerouter.models import Agent, Config, Machine, McpToken
from telerouter.security import new_token, token_hash
from telerouter.store import Store
from telerouter.web.app import create_app

from .test_web import setup_admin

CLAUDE = Who("token:t1", "claude CT 105")
HERMES = Who("agent:a2", "Hermes")


@pytest.fixture()
def mem(tmp_path):
    m = SharedMemory(tmp_path / "memory.db")
    yield m
    m.close()


# -- paths -------------------------------------------------------------------------------------------------------
def test_paths_are_normalised_and_unsafe_names_refused():
    assert norm_path(" servers / pve1//ct-118 telerouter/ ") == "servers/pve1/ct-118 telerouter"
    assert norm_path("") == "" and norm_path("/") == ""
    for bad in ("../etc", "a/../b", ".hidden", "a/.git", "x/<script>", "a\x00b", "a/" + "b" * 65):
        with pytest.raises(MemError):
            norm_path(bad)
    with pytest.raises(MemError):
        norm_path("/".join(["a"] * 13))
    assert norm_title("  architecture   notes ") == "architecture notes"
    for bad in ("", "a\x07b", "x" * 121):
        with pytest.raises(MemError):
            norm_title(bad)


# -- notes ---------------------------------------------------------------------------------------------------------
def test_a_note_creates_its_folders_and_lists_in_the_tree(mem):
    n = mem.write_note(CLAUDE, "servers/pve1/ct-118/telerouter", "architecture", "compose in /opt/telerouter")
    assert n["created"] and n["version"] == 1 and n["created_by"] == "claude CT 105"
    assert [f["path"] for f in mem.folders()] == ["servers", "servers/pve1", "servers/pve1/ct-118",
                                                   "servers/pve1/ct-118/telerouter"]
    top = mem.list_folder("", depth=2)
    assert top["folders"][0]["path"] == "servers"
    assert top["folders"][0]["folders"][0]["path"] == "servers/pve1"
    assert top["folders"][0]["folders"][0]["folders"][0]["notes_inside"] == 1
    leaf = mem.list_folder("servers/pve1/ct-118/telerouter")
    assert [x["title"] for x in leaf["notes"]] == ["architecture"] and "body" not in leaf["notes"][0]
    assert mem.read_note(folder="servers/pve1/ct-118/telerouter", title="ARCHITECTURE")["body"].startswith("compose")
    with pytest.raises(MemError, match="no folder"):
        mem.list_folder("servers/nope")


def test_replacing_needs_the_version_you_read_so_no_edit_is_lost(mem):
    n = mem.write_note(CLAUDE, "repos/telerouter", "todo", "a")
    with pytest.raises(MemError, match="exists already"):
        mem.write_note(HERMES, "repos/telerouter", "todo", "b")                      # blind overwrite refused
    mem.write_note(HERMES, "repos/telerouter", "todo", "b", version=1)
    with pytest.raises(MemError, match="changed since you read it.*Hermes"):
        mem.write_note(CLAUDE, "repos/telerouter", "todo", "c", version=1)           # Claude's copy is stale
    r = mem.read_note(n["id"])
    assert r["body"] == "b" and r["version"] == 2 and r["updated_by"] == "Hermes"
    revs = mem.revisions(n["id"])
    assert [(x["version"], x["by"]) for x in revs] == [(1, "claude CT 105")]
    assert mem.revision(n["id"], 1)["body"] == "a"


def test_append_adds_signed_dated_entries(mem):
    mem.append_note(CLAUDE, "servers/pve1/ct-122", "changelog", "upgraded wa_logger to 2.1")
    n = mem.append_note(HERMES, "servers/pve1/ct-122", "changelog", "restarted the gateway")
    body = mem.read_note(n["id"])["body"]
    assert body.index("claude CT 105") < body.index("Hermes")
    assert "### " in body and "UTC · Hermes\nrestarted the gateway" in body
    assert n["version"] == 2


def test_revisions_are_capped(mem):
    n = mem.write_note(CLAUDE, "x", "t", "0")
    for v in range(1, KEEP_REVISIONS + 6):
        mem._rate.clear()
        mem.write_note(CLAUDE, "x", "t", str(v), version=v)
    assert len(mem.revisions(n["id"])) == KEEP_REVISIONS


def test_deleted_notes_go_to_the_trash_and_can_be_restored(mem):
    n = mem.write_note(CLAUDE, "x", "t", "keep me")
    mem.delete_note(HERMES, n["id"])
    with pytest.raises(MemError):
        mem.read_note(n["id"])
    assert [(t["id"], t["deleted_by"]) for t in mem.trash()] == [(n["id"], "Hermes")]
    mem.write_note(CLAUDE, "x", "t", "new one with the same title")
    r = mem.restore_note(Who("owner", "owner"), n["id"])
    assert r["title"] == "t (restored)" and mem.read_note(n["id"])["body"] == "keep me"


def test_old_trash_is_purged(mem):
    n = mem.write_note(CLAUDE, "x", "t", "old")
    mem.delete_note(CLAUDE, n["id"])
    mem.db.execute("UPDATE notes SET deleted=? WHERE id=?", (time.time() - 40 * 86400, n["id"]))
    mem.purge()
    assert mem.trash() == []


def test_folders_move_with_their_notes_and_delete_only_when_empty(mem):
    n = mem.write_note(CLAUDE, "servers/pve/ct-1/app", "architecture", "x")
    with pytest.raises(MemError, match="into itself"):
        mem.move_folder(CLAUDE, "servers/pve", "servers/pve/sub")
    mem.move_folder(CLAUDE, "servers/pve", "servers/pve1")
    assert mem.read_note(n["id"])["folder"] == "servers/pve1/ct-1/app"
    assert "servers/pve" not in [f["path"] for f in mem.folders()]
    with pytest.raises(MemError, match="not empty"):
        mem.delete_folder(CLAUDE, "servers/pve1")
    mem.create_folder(CLAUDE, "servers/empty", "nothing yet")
    assert mem.delete_folder(CLAUDE, "servers/empty")["deleted"] == "servers/empty"
    out = mem.delete_folder(Who("owner", "owner"), "servers/pve1", recursive=True)   # the web UI may
    assert out["notes_to_trash"] == 1 and [t["id"] for t in mem.trash()] == [n["id"]]


def test_a_similar_folder_is_suggested(mem):
    mem.create_folder(CLAUDE, "servers/pve1/ct-118-telerouter")
    with pytest.raises(MemError, match="similar: servers/pve1/ct-118-telerouter"):
        mem.list_folder("servers/ct-118-telerouter")


def test_search_and_recent_changes(mem):
    mem.write_note(CLAUDE, "servers/pve1/ct-118", "architecture", "telerouter runs in Docker compose")
    mem.write_note(HERMES, "repos/wa_logger", "notes", "uses Chromium per user")
    assert [n["title"] for n in mem.search("docker TELEROUTER")] == ["architecture"]
    assert "Docker compose" in mem.search("docker")[0]["snippet"]
    assert mem.search("chromium", folder="servers") == []
    assert mem.search("100%") == []                                   # LIKE wildcards are literal
    assert {n["folder"] for n in mem.recent(since=time.time() - 60)} == {"servers/pve1/ct-118", "repos/wa_logger"}
    assert [n["folder"] for n in mem.recent("repos")] == ["repos/wa_logger"]


def test_size_and_rate_limits(mem):
    with pytest.raises(MemError, match="at most"):
        mem.write_note(CLAUDE, "x", "big", "x" * 100_001)
    with pytest.raises(MemError, match="too many"):
        for i in range(100):
            mem.create_folder(CLAUDE, f"f{i}")
    for i in range(100):                                              # the owner (web UI) is not throttled
        mem.create_folder(Who("owner", "owner"), f"g{i}")


def test_the_database_file_is_private(tmp_path):
    m = SharedMemory(tmp_path / "memory.db")
    m.close()
    assert oct((tmp_path / "memory.db").stat().st_mode & 0o777) == "0o600"


# -- messages ---------------------------------------------------------------------------------------------------
def test_messages_reach_the_recipient_and_everyone_once(mem):
    mem.touch(HERMES)
    mem.send(CLAUDE, HERMES, "I'm migrating CT 122, don't restart wa_logger", project="servers/pve1/ct-122")
    mem.send(CLAUDE, None, "deploying telerouter 0.14 at 15:00")
    mem.send(HERMES, Who("token:x", "other"), "not for Claude")
    assert mem.unread(HERMES) == 2 and mem.unread(CLAUDE) == 0              # own messages are not in the inbox
    got = mem.inbox(HERMES)
    assert [m["text"][:9] for m in got] == ["I'm migra", "deploying"] and all(m["new"] for m in got)
    assert got[0]["to"] == "Hermes" and got[1]["to"] == "everyone" and got[0]["project"] == "servers/pve1/ct-122"
    assert mem.unread(HERMES) == 0 and mem.inbox(HERMES) == []
    again = mem.inbox(HERMES, include_read=True)
    assert len(again) == 2 and not any(m["new"] for m in again)
    r = mem.send(HERMES, CLAUDE, "ok, waiting", reply_to=got[0]["id"])
    assert mem.inbox(CLAUDE)[0]["reply_to"] == got[0]["id"] and r["to"] == "claude CT 105"
    with pytest.raises(MemError, match="no message"):
        mem.send(HERMES, CLAUDE, "x", reply_to=999)
    owner_view = mem.all_messages()
    assert len(owner_view) == 4 and owner_view[0]["read_by"] == ["Hermes"]


def test_message_limits(mem):
    with pytest.raises(MemError, match="empty"):
        mem.send(CLAUDE, HERMES, "  ")
    with pytest.raises(MemError, match="at most"):
        mem.send(CLAUDE, HERMES, "x" * 4001)
    with pytest.raises(MemError, match="too many"):
        for _ in range(30):
            mem.send(CLAUDE, HERMES, "spam")


def test_status_is_kept_per_agent(mem):
    mem.touch(CLAUDE)
    mem.set_status(HERMES, "upgrading Docker on CT 118")
    p = mem.peers()
    assert p["agent:a2"]["status"] == "upgrading Docker on CT 118" and p["token:t1"]["status"] == ""
    mem.set_status(HERMES, "")
    assert mem.peers()["agent:a2"]["status"] == ""


# -- MCP tools ---------------------------------------------------------------------------------------------------
@pytest.fixture()
def eng(settings):
    store = Store(settings.data_dir)
    m = Machine(id="m1", name="laptop", host="10.0.0.2", username="u", host_key="k")
    store.config = Config(machines=[m], agents=[Agent(id="a1", name="Jarvis", machine_id="m1", host_key="k")])
    e = Engine(settings, store)
    e.tokens = {}
    for name, scopes, agent in (("claude CT 105", ["calls", "memory"], ""), ("hermes box", ["memory"], ""),
                                ("calls only", ["calls"], ""), ("agent Jarvis", [], "a1")):
        plain = new_token("trm_")
        t = McpToken(name=name, token_hash=token_hash(plain), scopes=scopes, agent_id=agent)
        store.config.mcp_tokens.append(t)
        e.tokens[name] = (t, plain)
    return e


async def tool(eng, name, token_name, **args):
    tok = eng.tokens[token_name][0]
    r = await eng.mcp._one(tok, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": name, "arguments": args}})
    res = r["result"]
    return res["isError"], (res["content"][0]["text"] if res["isError"] else res["structuredContent"])


async def test_only_tokens_with_the_memory_scope_and_agents_see_the_tools(eng):
    async def names(tn):
        r = await eng.mcp._one(eng.tokens[tn][0], {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        return {t["name"] for t in r["result"]["tools"]}
    assert "write_note" in await names("claude CT 105") and "send_agent_message" in await names("agent Jarvis")
    assert not ({"write_note", "read_agent_messages"} & await names("calls only"))
    r = await eng.mcp._one(eng.tokens["calls only"][0], {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                         "params": {"name": "write_note", "arguments": {}}})
    assert r["error"]["message"] == "unknown tool: write_note"
    init = lambda tn: eng.mcp._one(eng.tokens[tn][0], {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert "Shared memory" in (await init("hermes box"))["result"]["instructions"]
    assert "Shared memory" not in (await init("calls only"))["result"]["instructions"]


async def test_two_agents_coordinate_through_messages_status_and_notes(eng):
    err, peers = await tool(eng, "list_peer_agents", "claude CT 105")
    assert not err and {p["name"] for p in peers["agents"]} == {"Jarvis", "claude CT 105", "hermes box", "owner"}
    assert peers["you"] == "claude CT 105"
    await tool(eng, "set_status", "claude CT 105", text="upgrading Docker on CT 118")
    err, out = await tool(eng, "send_agent_message", "claude CT 105", to="hermes", text="hands off CT 118 please",
                          project="servers/pve1/ct-118")
    assert not err and out["to"] == "hermes box"
    err, out = await tool(eng, "append_note", "claude CT 105", folder="servers/pve1/ct-118", title="changelog",
                          text="docker 27 -> 28")
    assert not err and out["created"]
    # Hermes' next memory call tells it there is mail
    err, out = await tool(eng, "list_folder", "hermes box", path="servers")
    assert out["unread_agent_messages"] == 1
    err, out = await tool(eng, "read_agent_messages", "hermes box")
    assert out["messages"][0]["from"] == "claude CT 105" and out["messages"][0]["text"] == "hands off CT 118 please"
    err, peers = await tool(eng, "list_peer_agents", "hermes box")
    claude = next(p for p in peers["agents"] if p["name"] == "claude CT 105")
    assert claude["status"] == "upgrading Docker on CT 118" and claude["last_seen"]
    err, out = await tool(eng, "recent_changes", "hermes box")
    assert out["notes"][0]["updated_by"] == "claude CT 105"
    # the agent telerouter runs answers under its own name
    err, out = await tool(eng, "send_agent_message", "agent Jarvis", to="claude", text="noted")
    assert not err
    err, out = await tool(eng, "read_agent_messages", "claude CT 105")
    assert out["messages"][0]["from"] == "Jarvis"


async def test_tool_errors_are_readable(eng):
    err, text = await tool(eng, "send_agent_message", "claude CT 105", to="nobody", text="x")
    assert err and "no agent 'nobody'" in text and "hermes box" in text
    err, text = await tool(eng, "read_note", "claude CT 105")
    assert err and "note_id" in text
    err, text = await tool(eng, "write_note", "claude CT 105", folder="../etc", title="x", body="y")
    assert err and "bad folder name" in text
    await tool(eng, "write_note", "claude CT 105", folder="a", title="t", body="1")
    err, text = await tool(eng, "write_note", "hermes box", folder="a", title="t", body="2")
    assert err and "exists already" in text and "append_note" in text


# -- web API ----------------------------------------------------------------------------------------------------
@pytest.fixture()
def web(settings):
    files = tls.ensure(settings.data_dir, [])
    public, _ = create_app(settings, files)
    with TestClient(public, base_url="https://testserver") as c:
        yield c


def test_the_memory_api_needs_a_login_and_csrf(web):
    for method, path in (("get", "/api/memory"), ("get", "/api/memory/folder"), ("get", "/api/memory/messages"),
                         ("post", "/api/memory/notes"), ("post", "/api/memory/messages"), ("get", "/api/memory/trash")):
        assert getattr(web, method)(path).status_code == 401
    setup_admin(web)
    assert web.post("/api/memory/notes", json={"folder": "x", "title": "t", "body": "b"}).status_code == 403
    assert web.post("/api/memory/folders/delete", json={"path": "x"}).status_code == 403


def test_the_owner_manages_notes_and_messages_from_the_web(web):
    h = {"X-CSRF-Token": setup_admin(web)}
    r = web.post("/api/memory/notes", json={"folder": "servers/pve1", "title": "architecture", "body": "v1"}, headers=h)
    assert r.status_code == 200, r.text
    nid = r.json()["id"]
    assert web.post("/api/memory/notes", json={"folder": "servers/pve1", "title": "architecture", "body": "x"},
                    headers=h).status_code == 409
    assert web.post("/api/memory/notes", json={"folder": "../x", "title": "t"}, headers=h).status_code == 422
    r = web.put(f"/api/memory/notes/{nid}", json={"body": "v2", "version": 1}, headers=h)
    assert r.status_code == 200 and r.json()["version"] == 2
    assert web.put(f"/api/memory/notes/{nid}", json={"body": "v3", "version": 1}, headers=h).status_code == 409
    note = web.get(f"/api/memory/notes/{nid}").json()
    assert note["body"] == "v2" and note["revisions"][0]["version"] == 1
    assert web.get(f"/api/memory/notes/{nid}/revisions/1").json()["body"] == "v1"
    assert web.get("/api/memory/notes/nope").status_code == 404
    st = web.get("/api/memory").json()
    assert [f["path"] for f in st["folders"]] == ["servers", "servers/pve1"] and st["stats"]["notes"] == 1
    assert web.get("/api/memory/search?q=v2").json()["notes"][0]["id"] == nid
    r = web.put("/api/memory/folders", json={"path": "servers/pve1", "new_path": "servers/pve1",
                                             "description": "the main Proxmox"}, headers=h)
    assert r.status_code == 200 and r.json()["description"] == "the main Proxmox"
    assert web.get("/api/memory/folder?path=servers/pve1").json()["notes"][0]["id"] == nid
    assert web.post("/api/memory/folders/delete", json={"path": "servers"}, headers=h).json()["notes_to_trash"] == 1
    assert web.get("/api/memory/trash").json()["notes"][0]["id"] == nid
    assert web.post(f"/api/memory/notes/{nid}/restore", headers=h).status_code == 200
    assert web.get(f"/api/memory/notes/{nid}").json()["folder"] == "servers/pve1"
    # messages: the owner writes to everyone; an unknown name is refused
    assert web.post("/api/memory/messages", json={"to": "everyone", "text": "hi all"}, headers=h).status_code == 200
    assert web.post("/api/memory/messages", json={"to": "ghost", "text": "x"}, headers=h).status_code == 404
    msgs = web.get("/api/memory/messages").json()
    assert msgs["messages"][0]["from"] == "owner" and msgs["messages"][0]["to"] == "everyone"
    assert any(a["name"] == "owner" for a in msgs["agents"])
