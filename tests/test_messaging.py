"""Messaging: the web chat, Telegram bots (pairing, /session, footers), agent-initiated messages, the API."""

import asyncio
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from telerouter import tls
from telerouter.agents.runtime import AgentError
from telerouter.engine import Engine
from telerouter.messaging import BotRuntime, Channel, Messaging, TgError, footer_text, split_text
from telerouter.models import Agent, Config, Machine, McpToken, TelegramBot, TelegramChat
from telerouter.store import Store
from telerouter.web.app import create_app

from .test_web import setup_admin

TOKEN = "123456789:AAFakeTokenFakeTokenFakeTokenFake_12"


class FakeTelegram:
    """api.telegram.org: records the calls; getUpdates answers with what the test queued."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []
        self.fail: tuple[int, str] | None = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        assert request.url.host == "api.telegram.org"
        assert request.url.path.startswith(f"/bot{TOKEN}/")
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        if self.fail:
            code, text = self.fail
            return httpx.Response(code, json={"ok": False, "error_code": code, "description": text})
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "my_test_bot"}})
        if method == "getUpdates":
            await asyncio.sleep(0.02)                    # a long poll waits; never spin the event loop
            ups, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": ups})
        return httpx.Response(200, json={"ok": True, "result": True})

    def sent(self) -> list[str]:
        return [b["text"] for m, b in self.calls if m == "sendMessage"]


class FakeAgentRt:
    def __init__(self, reply="It is done."):
        self.reply = reply
        self.asked: list[tuple[str, object]] = []

    async def ask(self, text, source, on_note=None, timeout=None):
        self.asked.append((text, source))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class FakeMachineRt:
    def __init__(self, sessions):
        self.state = "ready"
        self.sessions = [{"name": n, "command": "claude", "path": "/x"} for n in sessions]
        self.asked: list[tuple[str, str, str, str]] = []

    async def list_sessions(self):
        return self.sessions

    async def new_session(self, name, workdir="~", by_admin=False):
        if any(x["name"] == name for x in self.sessions):
            raise RuntimeError(f"a session called {name} already exists on laptop")
        self.sessions.append({"name": name, "command": "claude", "path": workdir})
        return self.sessions[-1]

    async def ask(self, session, text, label, on_wait=None, header="", timeout=None):
        self.asked.append((session, text, label, header))
        return f"answer from {session}"


@pytest.fixture()
def eng(settings):
    store = Store(settings.data_dir)
    m = Machine(id="m1", name="laptop", host="10.0.0.2", username="u", host_key="k")
    a = Agent(id="a1", name="Jarvis", machine_id="m1", host_key="k")
    store.config = Config(machines=[m], agents=[a], default_agent_id="a1",
                          telegram_bots=[TelegramBot(id="b1", name="Home", token=TOKEN,
                                                     allowed_chats=[TelegramChat(id=42, name="Anna")])])
    e = Engine(settings, store)
    e.agent_rts["a1"] = FakeAgentRt()
    e.machine_rts["m1"] = FakeMachineRt(["wa_logger", "calls"])
    return e


@pytest.fixture()
def tg(eng):
    fake = FakeTelegram()
    eng.messaging = Messaging(eng, transport=httpx.MockTransport(fake))
    return fake


def runtime(eng):
    rt = BotRuntime(eng.messaging, "b1", TOKEN)
    eng.messaging.bots["b1"] = rt
    rt.state = "ok"
    return rt


def private(text, chat=42, name="Anna"):
    return {"chat": {"id": chat, "type": "private"}, "from": {"id": chat, "first_name": name}, "text": text}


# -- helpers -----------------------------------------------------------------------------------------------------
def test_long_text_is_cut_below_telegrams_limit_at_line_ends():
    text = "\n".join(f"line {i} " + "x" * 90 for i in range(200))
    parts = split_text(text, 1000)
    assert all(len(p) <= 1000 for p in parts) and len(parts) > 10
    assert "\n".join(parts).replace("\n", " ").split() == text.replace("\n", " ").split()
    assert split_text("short") == ["short"] and split_text("  ") == ["(empty)"]


def test_the_footer_names_the_tmux_session():
    assert footer_text("telerouter-ab12", "laptop", "Jarvis") == "— Jarvis · tmux telerouter-ab12 on laptop"
    assert footer_text("wa_logger", "laptop") == "— tmux wa_logger on laptop"


# -- messages to the default agent and sessions ----------------------------------------------------------------------
async def test_a_telegram_message_goes_to_the_default_agent_and_the_answer_names_its_session(eng, tg):
    rt = runtime(eng)
    await rt._message(private("turn on the kitchen light"))
    agent = eng.agent_rts["a1"]
    text, source = agent.asked[0]
    assert text == "turn on the kitchen light"
    assert source.kind == "message" and "text message" in source.label and "Anna" in source.label
    sent = tg.sent()
    assert sent[-1] == "It is done.\n\n— Jarvis · tmux telerouter-a1 on laptop"
    assert [m["dir"] for m in eng.messaging.messages] == ["in", "out"]
    assert eng.messaging.messages[-1]["footer"] == "— Jarvis · tmux telerouter-a1 on laptop"


async def test_session_lists_the_sessions_and_switches(eng, tg):
    rt = runtime(eng)
    await rt._message(private("/session"))
    listing = [b for m, b in tg.calls if m == "sendMessage"][-1]
    assert "0. ▶ Jarvis (default agent)" in listing["text"]
    assert "1. wa_logger on laptop" in listing["text"] and "2. calls on laptop" in listing["text"]
    buttons = [b["callback_data"] for row in listing["reply_markup"]["inline_keyboard"] for b in row]
    assert buttons == ["s:0", "s:1", "s:2"]

    await rt._message(private("/session 1"))
    assert "Now talking to the wa_logger session on laptop" in tg.sent()[-1]
    await rt._message(private("run the tests"))
    machine = eng.machine_rts["m1"]
    session, text, label, header = machine.asked[0]
    assert (session, text) == ("wa_logger", "run the tests")
    assert "text message" in header and "not for text-to-speech" in header
    assert eng.agent_rts["a1"].asked == []                 # the default agent did not get it
    assert tg.sent()[-1] == "answer from wa_logger\n\n— tmux wa_logger on laptop"

    await rt._message(private("/session default"))
    await rt._message(private("hello"))
    assert eng.agent_rts["a1"].asked[0][0] == "hello"


async def test_the_session_buttons_switch_too_and_by_name(eng, tg):
    rt = runtime(eng)
    await rt._message(private("/session"))
    await rt._callback({"id": "cb1", "data": "s:2", "message": {"chat": {"id": 42, "type": "private"}}})
    assert any(m == "answerCallbackQuery" for m, _ in tg.calls)
    assert eng.messaging.target_key("tg:b1:42") == "m1|calls"
    await rt._message(private("/session wa_log"))
    assert eng.messaging.target_key("tg:b1:42") == "m1|wa_logger"
    await rt._message(private("/session nope"))
    assert "don't know that session" in tg.sent()[-1]
    await rt._message(private("/compact"))                    # a slash command is never typed into an agent
    assert "don't know that command" in tg.sent()[-1]
    assert eng.machine_rts["m1"].asked == []


async def test_an_agent_error_is_reported_with_the_session(eng, tg):
    eng.agent_rts["a1"].reply = AgentError("Jarvis did not answer within 300 s")
    rt = runtime(eng)
    await rt._message(private("hi"))
    assert tg.sent()[-1].startswith("Sorry, Jarvis did not answer within 300 s.")
    assert tg.sent()[-1].endswith("— Jarvis · tmux telerouter-a1 on laptop")


async def test_long_answers_are_sent_in_parts_with_the_footer_last(eng, tg):
    eng.agent_rts["a1"].reply = "\n".join("word " * 30 for _ in range(100))
    rt = runtime(eng)
    await rt._message(private("long please"))
    parts = tg.sent()
    assert len(parts) >= 2 and all(len(p) <= 4096 for p in parts)
    assert parts[-1].endswith("— Jarvis · tmux telerouter-a1 on laptop") and "tmux" not in parts[0]


# -- who may talk to the bot ---------------------------------------------------------------------------------------
async def test_strangers_and_groups_get_no_answer(eng, tg):
    rt = runtime(eng)
    await rt._message(private("hello", chat=999, name="Mallory"))
    await rt._message(private("/session", chat=999))
    await rt._message({"chat": {"id": 42, "type": "group"}, "from": {"id": 42}, "text": "hi"})
    await rt._message({"chat": {"id": 42, "type": "private"}, "photo": []})
    assert tg.sent() == [] and eng.agent_rts["a1"].asked == []
    assert eng.messaging.messages == []


async def test_pairing_with_a_one_time_code(eng, tg):
    rt = runtime(eng)
    code, ttl = eng.messaging.new_pair_code("b1")
    assert len(code) == 8 and ttl == 600
    await rt._message(private(f"/start {code}", chat=77, name="Bob"))
    assert [c.id for c in eng.store.config.telegram_bots[0].allowed_chats] == [42, 77]
    assert tg.sent()[-1].startswith("Paired.")
    await rt._message(private(f"/start {code}", chat=88))             # a code works once
    assert 88 not in [c.id for c in eng.store.config.telegram_bots[0].allowed_chats]
    await rt._message(private("hi there", chat=77, name="Bob"))        # the new chat is answered
    assert eng.agent_rts["a1"].asked[-1][0] == "hi there"


async def test_pairing_codes_expire_and_wrong_guesses_lock_the_chat_out(eng, tg):
    rt = runtime(eng)
    code, _ = eng.messaging.new_pair_code("b1")
    for h in list(eng.messaging._pair):
        bid, _exp = eng.messaging._pair[h]
        eng.messaging._pair[h] = (bid, 1.0)                              # long expired
    await rt._message(private(f"/start {code}", chat=77))
    assert 77 not in [c.id for c in eng.store.config.telegram_bots[0].allowed_chats]
    for i in range(5):
        await rt._message(private(f"/start WRONG{i:03d}", chat=77))
    fresh, _ = eng.messaging.new_pair_code("b1")
    before = len(tg.sent())
    await rt._message(private(f"/start {fresh}", chat=77))               # locked out, even with a valid code
    assert len(tg.sent()) == before
    assert 77 not in [c.id for c in eng.store.config.telegram_bots[0].allowed_chats]


async def test_a_code_made_for_another_bot_does_not_pair(eng, tg):
    rt = runtime(eng)
    code, _ = eng.messaging.new_pair_code("other-bot")
    await rt._message(private(f"/start {code}", chat=77))
    assert 77 not in [c.id for c in eng.store.config.telegram_bots[0].allowed_chats]


# -- the bot loop and its token --------------------------------------------------------------------------------------
async def test_the_loop_validates_the_token_and_answers_updates(eng, tg):
    tg.updates = [{"update_id": 5, "message": private("ping")}]
    await eng.messaging.sync()
    rt = eng.messaging.bots["b1"]
    for _ in range(100):
        if tg.sent():
            break
        await asyncio.sleep(0.05)
    assert rt.state == "ok" and rt.username == "my_test_bot"
    assert tg.sent()[0].startswith("It is done.")
    offsets = [b["offset"] for m, b in tg.calls if m == "getUpdates"]
    assert offsets[0] == 0 and 6 in offsets                               # the update was acknowledged
    await eng.messaging.stop()


async def test_a_rejected_token_is_reported_without_the_token(eng, tg, caplog):
    tg.fail = (401, "Unauthorized")
    caplog.set_level(logging.DEBUG)
    await eng.messaging.sync()
    rt = eng.messaging.bots["b1"]
    for _ in range(100):
        if rt.state == "error":
            break
        await asyncio.sleep(0.05)
    assert rt.state == "error" and "rejected the token" in rt.error
    assert TOKEN not in caplog.text and TOKEN.split(":")[1] not in caplog.text
    await eng.messaging.stop()


async def test_network_errors_never_carry_the_url(eng):
    def boom(request):
        raise httpx.ConnectError("failed for " + str(request.url))
    eng.messaging = Messaging(eng, transport=httpx.MockTransport(boom))
    rt = BotRuntime(eng.messaging, "b1", TOKEN)
    with pytest.raises(TgError) as ei:
        await rt.api("getMe")
    assert TOKEN not in str(ei.value) and "ConnectError" in str(ei.value)
    await rt.client.aclose()


async def test_bots_follow_the_configuration(eng, tg):
    await eng.messaging.sync()
    assert set(eng.messaging.bots) == {"b1"}
    eng.store.config.telegram_bots[0].enabled = False
    await eng.messaging.sync()
    assert eng.messaging.bots == {}
    await eng.messaging.stop()


# -- an agent writes on its own ------------------------------------------------------------------------------------
async def test_send_message_reaches_paired_chats_with_the_session_footer(eng, tg):
    runtime(eng)
    tok = McpToken(id="t1", name="jarvis", scopes=["contacts"], agent_id="a1")
    tool = eng.mcp.tools["send_message"]
    assert eng.mcp._allowed(tool, tok)
    out = await tool.fn(tok, text="The backup finished.")
    assert out["delivered_to_telegram_chats"] == 1
    assert tg.sent() == ["The backup finished.\n\n— Jarvis · tmux telerouter-a1 on laptop"]
    last = eng.messaging.messages[-1]
    assert last["channel"] == "Agent" and last["footer"].endswith("on laptop")
    for bad in ("", "x" * 4001):
        with pytest.raises(Exception, match="empty|4000"):
            await tool.fn(tok, text=bad)


async def test_agents_cannot_flood_the_chat(eng, tg):
    runtime(eng)
    tok = McpToken(id="t1", name="jarvis", agent_id="a1")
    tool = eng.mcp.tools["send_message"]
    for _ in range(20):
        await tool.fn(tok, text="ping")
    with pytest.raises(Exception, match="too many"):
        await tool.fn(tok, text="ping")


def test_agent_tokens_see_the_messaging_tool_without_the_scope(eng):
    tool = eng.mcp.tools["send_message"]
    agent_tok = McpToken(name="a", scopes=["contacts"], agent_id="a1")
    other_tok = McpToken(name="b", scopes=["contacts"])
    assert tool.scope == "messaging"
    assert eng.mcp._allowed(tool, agent_tok)              # telerouter's own agents may write to the owner
    assert not eng.mcp._allowed(tool, other_tok)          # other tokens need the "messaging" scope
    assert eng.mcp._allowed(tool, McpToken(name="c", scopes=["messaging"]))


# -- storage -------------------------------------------------------------------------------------------------------
async def test_messages_and_targets_survive_a_restart(eng, tg):
    runtime(eng)
    ms = eng.messaging
    ms.set_target("web", (await ms.choices())[1])
    ms.add("web", "in", "hello", who="you", channel="Web")
    again = Messaging(eng)
    assert again.targets == {"web": {"machine_id": "m1", "session": "wa_logger"}}
    assert [m["text"] for m in again.messages] == ["hello"]
    assert again.add("web", "in", "next")["id"] == again.seq == 2
    assert oct((eng.store.dir / "messages.json").stat().st_mode & 0o777) == "0o600"


# -- the web API -----------------------------------------------------------------------------------------------------
@pytest.fixture()
def web(settings):
    files = tls.ensure(settings.data_dir, [])
    public, _ = create_app(settings, files)
    with TestClient(public, base_url="https://testserver") as c:
        yield c


def test_bot_api_keeps_the_token_secret_and_checks_it(web):
    csrf = setup_admin(web)
    h = {"X-CSRF-Token": csrf}
    assert web.post("/api/messaging/bots", json={"name": "x", "token": TOKEN}).status_code == 403        # CSRF
    assert web.post("/api/messaging/bots", json={"name": "x", "token": "not a token"}, headers=h).status_code == 422
    assert web.post("/api/messaging/bots", json={"name": "x", "token": ""}, headers=h).status_code == 422
    r = web.post("/api/messaging/bots", json={"name": "Home", "token": TOKEN, "allowed_chats": [{"id": 1}]}, headers=h)
    assert r.status_code == 200, r.text
    bot = r.json()
    assert bot["token"] == "" and bot["token_set"] is True and bot["allowed_chats"] == []   # nothing smuggled in
    assert TOKEN not in web.get("/api/messaging").text
    assert web.post("/api/messaging/bots", json={"name": "again", "token": TOKEN}, headers=h).status_code == 409
    r = web.put(f"/api/messaging/bots/{bot['id']}", json={"name": "Renamed", "token": ""}, headers=h)
    assert r.status_code == 200 and r.json()["token_set"] is True and r.json()["name"] == "Renamed"
    assert web.post(f"/api/messaging/bots/{bot['id']}/pair", headers=h).status_code == 409          # not connected
    assert web.delete(f"/api/messaging/bots/{bot['id']}", headers=h).status_code == 200


def test_messaging_needs_a_login(web):
    for method, path in (("get", "/api/messaging"), ("get", "/api/messages"), ("post", "/api/messages"),
                         ("put", "/api/messages/target")):
        assert getattr(web, method)(path).status_code == 401


def test_the_web_chat_sends_and_lists(web):
    csrf = setup_admin(web)
    h = {"X-CSRF-Token": csrf}
    assert web.post("/api/messages", json={"text": "  "}, headers=h).status_code == 422
    assert web.post("/api/messages", json={"text": "x" * 4001}, headers=h).status_code == 422
    assert web.post("/api/messages", json={"text": "/help"}, headers=h).status_code == 200
    for _ in range(50):
        msgs = web.get("/api/messages").json()["messages"]
        if len(msgs) >= 2:
            break
    assert [m["dir"] for m in msgs[:2]] == ["in", "out"] and msgs[1]["text"].startswith("You are talking to")
    assert web.get(f"/api/messages?after={msgs[-1]['id']}").json()["messages"] == []
    assert web.put("/api/messages/target", json={"key": "nope|nope"}, headers=h).status_code == 404
    assert web.put("/api/messages/target", json={"key": "default"}, headers=h).json()["target"] == "default"


async def test_session_new_starts_a_session_and_switches_to_it(eng, tg):
    rt = runtime(eng)
    machine = eng.machine_rts["m1"]
    machine.machine = eng.store.config.machines[0]
    await rt._message(private("/session new pokerbot"))
    assert "No machine allows new sessions" in tg.sent()[-1]           # off by default
    eng.store.config.machines[0].allow_new_sessions = True
    await rt._message(private("/session new"))
    assert tg.sent()[-1].startswith("Usage: /session new")
    await rt._message(private("/session new pokerbot ~/bots on laptop"))
    assert "Started pokerbot on laptop and switched to it" in tg.sent()[-1]
    assert [s["name"] for s in machine.sessions][-1] == "pokerbot"
    await rt._message(private("hi"))
    assert machine.asked[-1][:2] == ("pokerbot", "hi")
    await rt._message(private("/session new pokerbot"))
    assert "Could not start it: a session called pokerbot already exists" in tg.sent()[-1]
