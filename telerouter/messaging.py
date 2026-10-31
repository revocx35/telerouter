"""Messaging: written chat with the agents, from the web UI and from Telegram bots.

* A chat (the web page, or one paired Telegram chat) talks to the default agent. `/session` lists the tmux sessions
  of the owner's machines and switches the chat to one of them (`/session default` goes back); from then on its
  messages are typed into that session, like a speaker routed with "connect me to the X tmux".
* Every answer ends with a footer that names the tmux session it came from.
* Agents can also write on their own with the MCP tool `send_message`: it goes to every paired Telegram chat and
  shows up in the Messages page.

Telegram: long polling (`getUpdates`) with the bot's token, plain text only, private chats only. A chat has to be
paired first (the owner makes a one-time code in the UI and sends `/start <code>` to the bot); everybody else is
ignored without an answer. The token only travels in the URL of requests to api.telegram.org (TLS verified) and is
never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

import httpx

from .agents.runtime import AgentError, Source
from .models import TelegramChat
from .store import write_private

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger("telerouter.messaging")
logging.getLogger("httpx").setLevel(logging.WARNING)     # its INFO lines carry the request URL, i.e. the bot token

TELEGRAM_API = "https://api.telegram.org"
KEEP_MESSAGES = 500
MAX_IN = 4000                    # characters of one message from a person
MAX_OUT = 8000                   # stored per message; Telegram gets it in parts of CHUNK
CHUNK = 3800
PAIR_TTL = 600
PAIR_FAILS = 5
PAIR_BLOCK = 900
AGENT_MSGS_PER_MIN = 20

HELP = ("You are talking to {target}.\n"
        "/session - list the tmux sessions and switch to one\n"
        "/session default - back to the assistant\n"
        "/help - this text")


def split_text(text: str, limit: int = CHUNK) -> list[str]:
    """Cut at line ends where possible so no message passes Telegram's limit."""
    text = text.strip() or "(empty)"
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    parts.append(text)
    return parts


def footer_text(session: str, machine: str, agent: str = "") -> str:
    """The last line of an agent's message: which tmux session it came from."""
    where = f"tmux {session}" + (f" on {machine}" if machine else "")
    return f"— {agent} · {where}" if agent else f"— {where}"


Buttons = list[list[tuple[str, str]]]          # rows of (label, callback data)


@dataclass
class Channel:
    """Where a chat's answers go."""
    label: str                                               # "Web", "Telegram · Anna"
    send: Callable[[str, Buttons | None], Awaitable[None]]   # one message (already with its footer)
    typing: Callable[[], Awaitable[None]] | None = None      # "typing…" while the agent works


class TgError(Exception):
    def __init__(self, code: int, text: str):
        super().__init__(text)
        self.code = code


class BotRuntime:
    """One Telegram bot: validates the token, polls for updates, answers paired chats."""

    def __init__(self, svc: "Messaging", bot_id: str, token: str):
        self.svc = svc
        self.bot_id = bot_id
        self.token = token
        self.state = "connecting"
        self.error = ""
        self.username = ""
        self._task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self.client = httpx.AsyncClient(transport=svc.transport, follow_redirects=False,
                                        timeout=httpx.Timeout(45, connect=10))

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        for t in [self._task, *self._tasks]:
            if t:
                t.cancel()
        for t in [self._task, *self._tasks]:
            if t:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        await self.client.aclose()
        self.state = "stopped"

    @property
    def bot(self):
        return next((b for b in self.svc.engine.store.config.telegram_bots if b.id == self.bot_id), None)

    async def api(self, method: str, params: dict | None = None, timeout: float = 15) -> object:
        try:
            r = await self.client.post(f"{self.svc.api_url}/bot{self.token}/{method}", json=params or {}, timeout=timeout)
        except httpx.HTTPError as e:
            # not str(e): some httpx errors carry the URL, which holds the token
            raise TgError(0, f"cannot reach Telegram ({type(e).__name__})") from None
        try:
            data = r.json()
        except ValueError:
            raise TgError(r.status_code, f"unexpected answer from Telegram (HTTP {r.status_code})") from None
        if not data.get("ok"):
            raise TgError(int(data.get("error_code") or r.status_code), str(data.get("description") or "error")[:200])
        return data.get("result")

    async def _run(self) -> None:
        backoff = 3.0
        while True:
            try:
                me = await self.api("getMe")
                self.username = str(me.get("username") or "")        # type: ignore[union-attr]
                self.state, self.error = "ok", ""
                log.info("Telegram bot @%s is up", self.username)
                offset, backoff = 0, 3.0
                while True:
                    updates = await self.api("getUpdates", {"offset": offset, "timeout": 25,
                                                            "allowed_updates": ["message", "callback_query"]}, timeout=45)
                    for u in updates:                                   # type: ignore[union-attr]
                        offset = max(offset, int(u["update_id"]) + 1)
                        t = asyncio.create_task(self._update(u))
                        self._tasks.add(t)
                        t.add_done_callback(self._tasks.discard)
            except asyncio.CancelledError:
                raise
            except TgError as e:
                self.state, self.error = "error", e.args[0]
                if e.code in (401, 404):
                    self.error = "Telegram rejected the token"
                    backoff = 300.0
                elif e.code == 409:
                    self.error = "another program is polling this bot (a bot can have only one)"
                    backoff = max(backoff, 30.0)
                log.warning("Telegram bot %s: %s", self.username or self.bot_id, self.error)
            except Exception as e:
                self.state, self.error = "error", type(e).__name__
                log.exception("Telegram bot %s failed", self.bot_id)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300.0)

    # -- sending -------------------------------------------------------------------------------------
    async def send(self, chat_id: int, text: str, buttons: Buttons | None = None) -> None:
        parts = split_text(text)
        for i, part in enumerate(parts):
            params: dict = {"chat_id": chat_id, "text": part, "link_preview_options": {"is_disabled": True}}
            if buttons and i == len(parts) - 1:
                params["reply_markup"] = {"inline_keyboard": [
                    [{"text": lbl[:60], "callback_data": data} for lbl, data in row] for row in buttons]}
            await self.api("sendMessage", params)

    async def _typing(self, chat_id: int) -> None:
        with contextlib.suppress(TgError):
            await self.api("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=8)

    # -- updates -------------------------------------------------------------------------------------
    async def _update(self, u: dict) -> None:
        try:
            if "callback_query" in u:
                await self._callback(u["callback_query"])
            elif "message" in u:
                await self._message(u["message"])
        except asyncio.CancelledError:
            raise
        except TgError as e:
            log.warning("Telegram bot %s: could not answer: %s", self.username or self.bot_id, e.args[0])
        except Exception:
            log.exception("Telegram update failed")

    def _allowed(self, chat_id: int) -> TelegramChat | None:
        bot = self.bot
        return next((c for c in bot.allowed_chats if c.id == chat_id), None) if bot else None

    def _channel(self, chat_id: int, who: str) -> Channel:
        return Channel(f"Telegram · {who}" if who else "Telegram",
                       lambda text, buttons=None: self.send(chat_id, text, buttons),
                       lambda: self._typing(chat_id))

    async def _message(self, m: dict) -> None:
        chat = m.get("chat") or {}
        text = m.get("text")
        if chat.get("type") != "private" or not isinstance(text, str) or not isinstance(chat.get("id"), int):
            return
        cid = chat["id"]
        frm = m.get("from") or {}
        who = " ".join(str(frm.get("first_name") or frm.get("username") or "").split())[:64]
        known = self._allowed(cid)
        if known is None:
            cmd, _, code = text.strip().partition(" ")
            if cmd.split("@")[0].lower() == "/start" and code.strip():
                await self.svc.pair(self, cid, who, code.strip())
            return                                           # everybody else: no answer at all
        await self.svc.incoming(f"tg:{self.bot_id}:{cid}", known.name or who, self._channel(cid, known.name or who), text)

    async def _callback(self, cb: dict) -> None:
        msg = cb.get("message") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if chat.get("type") != "private" or not isinstance(cid, int) or not self._allowed(cid):
            return
        with contextlib.suppress(TgError):
            await self.api("answerCallbackQuery", {"callback_query_id": cb.get("id")}, timeout=8)
        data = str(cb.get("data") or "")
        if data.startswith("s:"):
            key = f"tg:{self.bot_id}:{cid}"
            await self.svc.pick(key, data[2:], self._channel(cid, self._allowed(cid).name))      # type: ignore[union-attr]


class Messaging:
    def __init__(self, engine: "Engine", transport: httpx.AsyncBaseTransport | None = None,
                 api_url: str = TELEGRAM_API):
        self.engine = engine
        self.transport = transport
        self.api_url = api_url
        self.path = engine.store.dir / "messages.json"
        self.messages: list[dict] = []
        self.seq = 0
        self.targets: dict[str, dict] = {}          # chat -> {"machine_id", "session"}; absent = the default agent
        self.bots: dict[str, BotRuntime] = {}
        self._sigs: dict[str, tuple] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._lists: dict[str, list[dict]] = {}     # the choices each chat was last shown
        self._pair: dict[str, tuple[str, float]] = {}      # sha256(code) -> (bot id, expires)
        self._bad: dict[tuple[str, int], tuple[int, float]] = {}
        self._sent: dict[str, list[float]] = {}
        self._load()

    # -- storage -------------------------------------------------------------------------------------
    def _load(self) -> None:
        try:
            d = json.loads(self.path.read_text())
            self.messages = [m for m in d.get("messages", []) if isinstance(m, dict)][-KEEP_MESSAGES:]
            self.seq = max([int(d.get("seq", 0))] + [int(m.get("id", 0)) for m in self.messages])
            self.targets = {k: v for k, v in (d.get("targets") or {}).items() if isinstance(v, dict)}
        except (OSError, ValueError, TypeError):
            pass

    def _save(self) -> None:
        with contextlib.suppress(OSError):
            write_private(self.path, json.dumps({"seq": self.seq, "messages": self.messages, "targets": self.targets}))

    def add(self, chat: str, direction: str, text: str, who: str = "", channel: str = "", footer: str = "") -> dict:
        self.seq += 1
        m = {"id": self.seq, "at": time.time(), "chat": chat, "dir": direction, "who": who, "channel": channel,
             "text": text[:MAX_OUT], "footer": footer}
        self.messages.append(m)
        del self.messages[:-KEEP_MESSAGES]
        self._save()
        return m

    def since(self, after: int = 0) -> list[dict]:
        return [m for m in self.messages if m["id"] > after]

    # -- bots ----------------------------------------------------------------------------------------
    async def sync(self) -> None:
        """Start, restart or stop the bot runtimes to match the configuration."""
        wanted = {b.id: b for b in self.engine.store.config.telegram_bots if b.enabled and b.token}
        for bid in list(self.bots):
            b = wanted.get(bid)
            if b is None or self._sigs.get(bid) != (b.token,):
                rt = self.bots.pop(bid)
                self._sigs.pop(bid, None)
                await rt.stop()
        for bid, b in wanted.items():
            if bid not in self.bots:
                rt = BotRuntime(self, bid, b.token)
                self.bots[bid] = rt
                self._sigs[bid] = (b.token,)
                rt.start()

    async def stop(self) -> None:
        for rt in list(self.bots.values()):
            await rt.stop()
        self.bots.clear()

    def bot_state(self, bot_id: str) -> dict:
        rt = self.bots.get(bot_id)
        return {"state": rt.state if rt else "off", "error": rt.error if rt else "",
                "username": rt.username if rt else ""}

    # -- pairing -------------------------------------------------------------------------------------
    def new_pair_code(self, bot_id: str) -> tuple[str, int]:
        now = time.time()
        for h, (_, exp) in list(self._pair.items()):
            if exp < now:
                del self._pair[h]
        code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
        self._pair[hashlib.sha256(code.encode()).hexdigest()] = (bot_id, now + PAIR_TTL)
        return code, PAIR_TTL

    async def pair(self, rt: BotRuntime, chat_id: int, who: str, code: str) -> None:
        key = (rt.bot_id, chat_id)
        now = time.time()
        fails, until = self._bad.get(key, (0, 0.0))
        if until > now:
            return
        h = hashlib.sha256(code.upper().encode()).hexdigest()
        hit = next((k for k in self._pair if hmac.compare_digest(k, h)), None)
        entry = self._pair.get(hit) if hit else None
        bot = rt.bot
        if not entry or entry[0] != rt.bot_id or entry[1] < now or bot is None:
            fails += 1
            self._bad[key] = (fails, now + PAIR_BLOCK if fails >= PAIR_FAILS else 0.0)
            await rt.send(chat_id, "That code is not valid (or it ran out). Make a new one in telerouter's Messages page.")
            return
        del self._pair[hit]
        self._bad.pop(key, None)
        if not any(c.id == chat_id for c in bot.allowed_chats):
            bot.allowed_chats.append(TelegramChat(id=chat_id, name=who))
            self.engine.store.save()
        log.info("Telegram bot %s: paired a chat", rt.username or rt.bot_id)
        await rt.send(chat_id, "Paired. Write to me and it goes to your assistant; /session picks a tmux session.")

    # -- choices and targets ---------------------------------------------------------------------------
    def _default_origin(self) -> tuple[str, str, str]:
        """(agent name, machine name, tmux session) of the default agent."""
        a = self.engine.store.default_agent()
        if not a:
            return "", "", ""
        m = self.engine.store.machine(a.machine_id)
        return a.name, m.name if m else "", a.session_name

    async def choices(self, refresh: bool = False) -> list[dict]:
        name, machine, session = self._default_origin()
        out = [{"key": "default", "label": f"{name or 'Assistant'} (default agent)", "machine_id": "", "session": "",
                "tmux": session, "machine": machine}]
        for m in self.engine.store.config.machines:
            rt = self.engine.machine_rts.get(m.id)
            if not rt or not m.enabled:
                continue
            if refresh and rt.state == "ready":
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(rt.list_sessions(), 10)
            for s in rt.sessions:
                out.append({"key": f"{m.id}|{s['name']}", "label": f"{s['name']} on {m.name}", "machine_id": m.id,
                            "session": s["name"], "tmux": s["name"], "machine": m.name})
        return out

    def target_key(self, chat: str) -> str:
        t = self.targets.get(chat)
        return f"{t['machine_id']}|{t['session']}" if t else "default"

    async def _label(self, chat: str) -> str:
        t = self.targets.get(chat)
        if not t:
            name, machine, session = self._default_origin()
            return name or "your assistant"
        m = self.engine.store.machine(t["machine_id"])
        return f"the {t['session']} session" + (f" on {m.name}" if m else "")

    def set_target(self, chat: str, choice: dict) -> None:
        if choice["key"] == "default":
            self.targets.pop(chat, None)
        else:
            self.targets[chat] = {"machine_id": choice["machine_id"], "session": choice["session"]}
        self._save()

    async def pick(self, chat: str, wanted: str, ch: Channel) -> None:
        """Switch `chat` to a choice: its number in the last list, "default", or a session name."""
        wanted = wanted.strip()
        shown = self._lists.get(chat) or await self.choices(refresh=True)
        choice = None
        if wanted.lower() in ("default", "0", "back", "assistant"):
            choice = shown[0]
        elif wanted.isdigit():
            n = int(wanted)
            choice = shown[n] if 0 < n < len(shown) else None
        else:
            w = wanted.lower()
            fresh = await self.choices(refresh=True)
            for pred in (lambda c: c["session"].lower() == w or c["label"].lower() == w,
                         lambda c: c["session"] and w in c["label"].lower()):
                hits = [c for c in fresh[1:] if pred(c)]
                if len(hits) == 1:
                    choice = hits[0]
                    break
                if len(hits) > 1:
                    await self._say(chat, ch, "Several sessions match: " + ", ".join(c["label"] for c in hits)
                                    + ". Use the number from /session.")
                    return
        if choice is None:
            await self._say(chat, ch, "I don't know that session. /session shows the list.")
            return
        self.set_target(chat, choice)
        if choice["key"] == "default":
            await self._say(chat, ch, f"Back to {await self._label(chat)}.")
        else:
            await self._say(chat, ch, f"Now talking to {await self._label(chat)}. /session default goes back.")

    async def _session_list(self, chat: str, ch: Channel) -> None:
        opts = await self.choices(refresh=True)
        self._lists[chat] = opts
        cur = self.target_key(chat)
        lines = [f"You are talking to {await self._label(chat)}.", ""]
        buttons: Buttons = []
        for i, c in enumerate(opts):
            mark = "▶ " if c["key"] == cur else ""
            lines.append(f"{i}. {mark}{c['label']}")
            buttons.append([((("✓ " if c["key"] == cur else "") + c["label"]), f"s:{i}")])
        lines += ["", "Tap one, or send /session <number or name>."]
        await self._say(chat, ch, "\n".join(lines), buttons)

    async def _say(self, chat: str, ch: Channel, text: str, buttons: Buttons | None = None, footer: str = "") -> None:
        self.add(chat, "out", text, channel=ch.label, footer=footer)
        await ch.send(text + (f"\n\n{footer}" if footer else ""), buttons)

    # -- a message from a person ---------------------------------------------------------------------
    async def incoming(self, chat: str, who: str, ch: Channel, text: str) -> None:
        text = text.strip()[:MAX_IN]
        if not text:
            return
        self.add(chat, "in", text, who=who, channel=ch.label)
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            cmd = cmd.split("@")[0].lower()
            if cmd in ("/session", "/sessions"):
                if arg.strip():
                    await self.pick(chat, arg, ch)
                else:
                    await self._session_list(chat, ch)
            elif cmd == "/default":
                await self.pick(chat, "default", ch)
            elif cmd in ("/start", "/help"):
                await self._say(chat, ch, HELP.format(target=await self._label(chat)))
            else:
                await self._say(chat, ch, "I don't know that command. /help lists them.")
            return
        async with self._locks.setdefault(chat, asyncio.Lock()):
            await self._ask(chat, who, ch, text)

    async def _ask(self, chat: str, who: str, ch: Channel, text: str) -> None:
        e = self.engine
        t = self.targets.get(chat)
        origin = self._default_origin()
        keep = asyncio.create_task(self._typing_loop(ch)) if ch.typing else None
        try:
            if t:
                m = e.store.machine(t["machine_id"])
                origin = ("", m.name if m else "", t["session"])
                rt = e.machine_rts.get(t["machine_id"])
                if rt is None:
                    reply = "That machine is not connected."
                else:
                    header = f"text message from {who or 'the owner'} via {ch.label}: answer as normal written text, not for text-to-speech"
                    reply = await rt.ask(t["session"], text, ch.label, header=header)
            else:
                agent = e.store.default_agent()
                art = e.agent_rts.get(agent.id) if agent else None
                if not agent or not art:
                    reply = "No assistant is running yet."
                else:
                    label = (f"text message from {who or 'the owner'} via {ch.label}: "
                             "answer as normal written text, not for text-to-speech")
                    reply = await art.ask(text, Source("message", label))
        except AgentError as ex:
            reply = f"Sorry, {ex}."
        except Exception:
            log.exception("messaging: asking the agent failed")
            reply = "Sorry, that failed."
        finally:
            if keep:
                keep.cancel()
        await self._say(chat, ch, reply, footer=footer_text(origin[2], origin[1], origin[0]) if origin[2] else "")

    @staticmethod
    async def _typing_loop(ch: Channel) -> None:
        assert ch.typing
        while True:
            await ch.typing()
            await asyncio.sleep(4)

    # -- the web page --------------------------------------------------------------------------------
    def web_channel(self) -> Channel:
        async def nothing(text: str, buttons: Buttons | None = None) -> None:
            return None
        return Channel("Web", nothing)

    # -- an agent writes on its own ------------------------------------------------------------------
    def rate_ok(self, who: str) -> bool:
        now = time.time()
        recent = [t for t in self._sent.get(who, []) if now - t < 60]
        self._sent[who] = recent
        if len(recent) >= AGENT_MSGS_PER_MIN:
            return False
        recent.append(now)
        return True

    async def notify(self, text: str, footer: str) -> int:
        """Send `text` (+ footer) to every paired Telegram chat; returns how many received it."""
        text = text.strip()[:MAX_OUT]
        self.add("agent", "out", text, channel="Agent", footer=footer)
        sent = 0
        body = text + (f"\n\n{footer}" if footer else "")
        for bid, rt in list(self.bots.items()):
            bot = rt.bot
            if not bot or rt.state != "ok":
                continue
            for c in bot.allowed_chats:
                try:
                    await rt.send(c.id, body)
                    sent += 1
                except TgError as ex:
                    log.warning("Telegram bot %s: message not delivered: %s", rt.username or bid, ex.args[0])
        return sent
