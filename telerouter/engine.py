"""The engine: configuration -> SIP account, speakers, agents, integrations; shared lookups."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time
from urllib.parse import urlsplit

from .agents.machine import MachineRuntime
from .agents.runtime import AgentRuntime
from .agents.ssh import SshKeys
from .media.rtp import PortAllocator
from .models import Agent, CastDevice, Contact, Integration, Machine
from .router.calls import CallManager, digits
from .router.integrations import IntegrationClient, IntegrationError
from .settings import Settings
from .sip.stack import SipStack
from .sip.ua import Account, AccountConfig, UserAgent
from .store import Store
from .voice.assistant import VoiceAssistant, speakable
from .voice.cast import CastMedia, CastOutput
from .voice.client_conn import ClientConn
from .voice.speaker import VirtualSpeaker
from .voice.speech import SpeechServices

log = logging.getLogger("telerouter.engine")

SYNC_INTERVAL = 600


def _conn_sig(a: Agent) -> tuple:
    return (a.enabled, a.host, a.port, a.username, a.host_key, a.kind, a.mcp_remote_port)


def _launch_sig(a: Agent) -> tuple:
    return (a.model, a.effort, a.permission_mode, a.workdir, a.extra_instructions, a.hermes_provider,
            a.tmux_session, a.name, tuple(s.model_dump_json() for s in a.mcp_servers))


class Engine:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.started = time.time()
        self.ports = PortAllocator(settings.rtp_port_min, settings.rtp_port_max)
        self.ua = UserAgent(settings.sip_port, self.ports, settings.advertise_ip,
                            user_agent=f"telerouter/{settings.version}", bind=settings.sip_bind)
        self.speech = SpeechServices(settings.whisper_uri, settings.piper_uri, settings.wake_uri)
        self.ssh_keys = SshKeys(settings.data_dir)
        self.clients: dict[str, ClientConn] = {}          # connected speaker devices
        self.casts: dict[str, CastOutput] = {}            # enabled Google Cast speakers
        self.speakers: dict[str, VirtualSpeaker] = {}     # online speakers (what agents talk to)
        self.cast_media = CastMedia(settings.bind, settings.cast_port)
        self.voice = VoiceAssistant(self)
        from .quiet import Quiet
        self.quiet = Quiet(self)
        self.calls = CallManager(self)
        self.ua.on_incoming = self.calls.on_incoming
        self.agent_rts: dict[str, AgentRuntime] = {}
        self.machine_rts: dict[str, MachineRuntime] = {}
        self._machine_sigs: dict[str, tuple] = {}
        self._agent_sigs: dict[str, tuple[tuple, tuple]] = {}
        self.integ_clients: dict[str, IntegrationClient] = {}
        self._addr_cache: dict[str, tuple[float, set[str]]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._sync_now = asyncio.Event()
        from .mcp import McpServer
        self.mcp = McpServer(self)
        from .scheduler import Scheduler
        self.scheduler = Scheduler(self)

    # small facades used by other modules
    @property
    def agents(self) -> "Engine":
        return self

    def get(self, agent_id: str) -> AgentRuntime | None:     # engine.agents.get(id)
        return self.agent_rts.get(agent_id)

    @property
    def integrations(self) -> "Engine":
        return self

    def client(self, integ_id: str) -> IntegrationClient | None:     # engine.integrations.client(id)
        return self.integ_clients.get(integ_id)

    speakable = staticmethod(speakable)

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    # -- lifecycle ------------------------------------------------------------------------------------
    async def start(self) -> None:
        await self.ua.start()
        await self.apply_config()
        self._spawn(self._sync_loop())
        self.scheduler.resume()
        self._spawn(self.scheduler.loop())

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        self.scheduler.stop()
        for vs in list(self.speakers.values()):
            await vs.close()
        for conn in list(self.clients.values()):
            await conn.close()
        await asyncio.gather(*(c.close() for c in self.casts.values()), return_exceptions=True)
        await self.cast_media.close()
        for s in self.calls.active():
            if s.task:
                s.task.cancel()
        await asyncio.gather(*(rt.stop() for rt in self.agent_rts.values()), return_exceptions=True)
        await asyncio.gather(*(rt.stop() for rt in self.machine_rts.values()), return_exceptions=True)
        for c in self.integ_clients.values():
            await c.close()
        await self.ua.stop()

    async def apply_config(self) -> None:
        cfg = self.store.config
        p = cfg.pbx
        accounts = []
        if p.enabled and p.host and p.username:
            accounts.append(AccountConfig(id="pbx", server=p.host, port=p.port, username=p.username,
                                          password=p.password, auth_username=p.auth_username, domain=p.domain,
                                          display_name=p.display_name, expires=p.expires,
                                          contact_user=p.contact_user))
        await self.ua.set_accounts(accounts)
        self.ua.direct_accounts["inbound"] = Account(self.ua, AccountConfig(
            id="inbound", server="0.0.0.0", port=5060, username="telerouter", password="",
            contact_user=cfg.inbound_secret))
        self._sync_agent_machines()
        await self._apply_machines()
        await self._apply_agents()
        await self._apply_integrations()
        self.sync_speakers()
        self._sync_now.set()

    def _sync_agent_machines(self) -> None:
        """An agent's SSH target comes from its machine."""
        for a in self.store.config.agents:
            m = self.store.machine(a.machine_id)
            if m:
                a.host, a.port, a.username = m.host, m.port, m.username
                a.host_key = m.host_key if m.enabled else ""

    async def _apply_machines(self) -> None:
        cfg = self.store.config
        wanted = {m.id: m for m in cfg.machines if m.enabled and m.host_key}
        for mid in list(self.machine_rts):
            m = wanted.get(mid)
            sig = (m.host, m.port, m.username, m.host_key) if m else None
            if not m or self._machine_sigs.get(mid) != sig:
                rt = self.machine_rts.pop(mid)
                self._machine_sigs.pop(mid, None)
                await rt.stop()
        for m in wanted.values():
            rt = self.machine_rts.get(m.id)
            if rt is None:
                rt = MachineRuntime(self, m)
                self.machine_rts[m.id] = rt
                self._machine_sigs[m.id] = (m.host, m.port, m.username, m.host_key)
                rt.start()
            else:
                rt.machine = m
                rt.refresh()
        for c in cfg.contacts:                   # sessions of removed/disabled machines are gone
            if c.kind == "tmux" and c.source not in wanted and not c.missing:
                c.missing = True

    def route_for(self, contact: Contact):
        from .agents.machine import Route
        if contact.kind != "tmux":
            raise ValueError("not a tmux session")
        if contact.source not in self.machine_rts:
            raise ValueError("that machine is not connected")
        return Route(contact.source, contact.source_ref, contact.label)

    async def ask_route(self, route, text: str, label: str) -> str:
        """Send what was said to the routed tmux session; its answer (or a spoken error)."""
        from .agents.runtime import AgentError
        rt = self.machine_rts.get(route.machine_id)
        route.last = time.time()
        if not rt:
            return f"The machine of {route.label} is not connected."
        try:
            reply = await rt.ask(route.session, text, label)
        except AgentError as e:
            return f"Sorry, {e}."
        route.last = time.time()
        return reply

    def task_sessions(self, machine_id: str) -> dict[str, dict]:
        """tmux session -> the scheduled task that uses it (a task's own session, or a per-run one).

        Used by the tmux page to mark a session as a scheduled task's; a `task_session` that never ran has no
        session yet, which costs nothing (nothing matches its name).
        """
        out: dict[str, dict] = {}
        for t in self.store.config.tasks:
            if t.target == "agent" or t.machine_id != machine_id:
                continue
            try:
                names = list(self.scheduler.info(t).get("sessions") or [])
            except Exception:
                names = []
            if t.target == "task_session" and not names:
                names = [t.tmux_name]
            for name in names:
                out[name] = {"id": t.id, "name": t.name, "target": t.target}
        return out

    def merge_tmux_sessions(self, m: Machine, sessions: list[dict]) -> None:
        cfg = self.store.config
        mine = {c.source_ref: c for c in cfg.contacts if c.kind == "tmux" and c.source == m.id}
        names = {s["name"] for s in sessions}
        changed = False
        for s in sessions:
            c = mine.get(s["name"])
            if c is None:
                cfg.contacts.append(Contact(kind="tmux", source=m.id, source_ref=s["name"], name=s["name"],
                                            description=f"{m.agent_kind} session in {s['path']}"[:200]))
                changed = True
            elif c.missing:
                c.missing = False
                changed = True
        for ref, c in mine.items():
            if ref not in names and not c.missing:
                c.missing = True
                changed = True
        if changed:
            self.store.save()

    async def _apply_agents(self) -> None:
        cfg = self.store.config
        wanted = {a.id: a for a in cfg.agents}
        for aid in list(self.agent_rts):
            a = wanted.get(aid)
            if not a or not a.enabled or not a.host_key or self._agent_sigs[aid][0] != _conn_sig(a):
                rt = self.agent_rts.pop(aid)
                self._agent_sigs.pop(aid, None)
                await rt.stop()
        for a in cfg.agents:
            if not a.enabled or not a.host_key:
                continue
            rt = self.agent_rts.get(a.id)
            if rt is None:
                rt = AgentRuntime(self, a)
                self.agent_rts[a.id] = rt
                self._agent_sigs[a.id] = (_conn_sig(a), _launch_sig(a))
                rt.start()
                continue
            rt.agent = a
            if self._agent_sigs[a.id][1] != _launch_sig(a):
                self._agent_sigs[a.id] = (_conn_sig(a), _launch_sig(a))
                if rt.session_alive:
                    log.info("[%s] launch settings changed - restarting its session", a.name)
                    rt.request_restart()

    async def _apply_integrations(self) -> None:
        cfg = self.store.config
        for iid, c in list(self.integ_clients.items()):
            await c.close()
            del self.integ_clients[iid]
        for i in cfg.integrations:
            if i.enabled and i.token:
                self.integ_clients[i.id] = IntegrationClient(i, i.tls_cert)

    # -- integration sync -------------------------------------------------------------------------------------
    async def _sync_loop(self) -> None:
        while True:
            self._sync_now.clear()
            for i in list(self.store.config.integrations):
                if i.enabled and i.id in self.integ_clients:
                    await self.sync_integration(i)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._sync_now.wait(), SYNC_INTERVAL)

    def our_sip_uri(self, host: str) -> str:
        ip = self.settings.advertise_ip or SipStack.local_ip_for(host)
        return f"sip:{self.store.config.inbound_secret}@{ip}:{self.settings.sip_port}"

    async def sync_integration(self, integ: Integration) -> dict:
        client = self.integ_clients.get(integ.id)
        if not client:
            raise IntegrationError("this connection is disabled or has no token")
        try:
            info = await client.info()
            targets = await client.targets()
            if integ.kind == "wa2sip":
                await client.set_inbound(self.our_sip_uri(client.host), integ.inbound)
        except IntegrationError as e:
            integ.last_error = str(e)
            self.store.save()
            log.warning("sync with %s (%s) failed: %s", integ.name or integ.kind, integ.url, e)
            raise
        added, updated = self._merge_targets(integ, targets)
        integ.last_sync = time.time()
        integ.last_error = ""
        self.store.save()
        if added or updated:
            log.info("synced %s: %d new, %d updated contacts", integ.name or integ.kind, added, updated)
        return {"info": info, "targets": len(targets), "added": added, "updated": updated}

    def _merge_targets(self, integ: Integration, targets: list[dict]) -> tuple[int, int]:
        cfg = self.store.config
        mine = {c.source_ref: c for c in cfg.contacts if c.source == integ.id}
        seen = set()
        added = updated = 0
        for t in targets:
            seen.add(t["id"])
            c = mine.get(t["id"])
            kind = "camera" if t["kind"] == "camera" else "whatsapp"
            if c is None:
                cfg.contacts.append(Contact(kind=kind, source=integ.id, source_ref=t["id"], name=t["name"],
                                            number=t.get("number", "")))
                added += 1
            elif c.name != t["name"] or c.number != t.get("number", "") or c.missing:
                c.name, c.number, c.missing = t["name"], t.get("number", ""), False
                updated += 1
        for ref, c in mine.items():
            if ref not in seen and not c.missing:
                c.missing = True
                updated += 1
        return added, updated

    # -- address checks for inbound SIP --------------------------------------------------------------------------
    async def _resolve(self, host: str) -> set[str]:
        hit = self._addr_cache.get(host)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None, family=socket.AF_INET)
            ips = {i[4][0] for i in infos}
        except OSError:
            ips = set()
        self._addr_cache[host] = (time.monotonic() + 60, ips)
        return ips

    async def is_pbx_address(self, ip: str) -> bool:
        p = self.store.config.pbx
        return bool(p.host) and ip in await self._resolve(p.host)

    async def integration_for_address(self, ip: str) -> Integration | None:
        for i in self.store.config.integrations:
            if i.enabled and ip in await self._resolve(urlsplit(i.url).hostname or ""):
                return i
        return None

    # -- contact lookups ---------------------------------------------------------------------------------------
    def find_contact_by_number(self, user: str, prefer: str = "") -> Contact | None:
        d = digits(user)
        if not d:
            return None
        hits = [c for c in self.store.config.contacts if digits(c.number) == d or
                (len(d) >= 7 and digits(c.number).endswith(d[-9:]) and len(digits(c.number)) >= 7)]
        hits.sort(key=lambda c: (c.kind != prefer, c.missing))
        return hits[0] if hits else None

    def find_integration_contact(self, integ_id: str, ref: str, user: str) -> Contact | None:
        cs = [c for c in self.store.config.contacts if c.source == integ_id]
        if ref:
            for c in cs:
                if c.source_ref == ref:
                    return c
        d = digits(user)
        if d:
            for c in cs:
                if digits(c.number) == d:
                    return c
        return None

    def contact_names(self) -> list[str]:
        return [c.label for c in self.store.config.contacts if c.enabled and not c.missing][:200]

    # -- speakers ------------------------------------------------------------------------------------------------
    def attach_client(self, conn: ClientConn) -> None:
        old = self.clients.get(conn.id)
        if old:
            self._spawn(self.detach_client(old))
        self.clients[conn.id] = conn
        conn.start()
        self.sync_speakers()

    async def detach_client(self, conn: ClientConn) -> None:
        if self.clients.get(conn.id) is conn:
            del self.clients[conn.id]
            self.sync_speakers()
        await conn.close()

    def _output(self, ref: str):
        kind, _, oid = ref.partition(":")
        if kind == "client":
            return self.clients.get(oid)
        if kind == "cast":
            return self.casts.get(oid)
        return None

    def sync_speakers(self) -> None:
        """Config + connected devices -> online VirtualSpeakers. A speaker is online while its mic device is
        connected and its output is there; a changed device binding makes a new one (like a reconnect)."""
        cfg = self.store.config
        devs = {d.id: d for d in cfg.casts if d.enabled}
        for cid in list(self.casts):
            if cid not in devs:
                self._spawn(self.casts.pop(cid).close())
        for d in devs.values():
            if d.id in self.casts:
                self.casts[d.id].dev = d
            else:
                self.casts[d.id] = CastOutput(d, self.cast_media, self._cast_moved)
        wanted = {}
        for sp in cfg.speakers:
            mic = self.clients.get(sp.mic) if sp.mic else None
            out = self._output(sp.output) if sp.output else None
            if not sp.enabled or (sp.mic and mic is None) or (sp.output and out is None) or (mic is None and out is None):
                continue
            wanted[sp.id] = (sp, mic, out)
        for sid, vs in list(self.speakers.items()):
            w = wanted.get(sid)
            if w is None or w[1] is not vs.mic_dev or w[2] is not vs.out:
                del self.speakers[sid]
                self.voice.detach(vs)
                self._spawn(vs.close())
                log.info("speaker '%s' offline", vs.name)
        for sid, (sp, mic, out) in wanted.items():
            vs = self.speakers.get(sid)
            if vs is not None:
                vs.cfg = sp
                continue
            vs = VirtualSpeaker(sp, mic, out)
            self.speakers[sid] = vs
            if mic is not None:
                mic.on_mic = vs.mic
                self.voice.attach(vs)
            log.info("speaker '%s' online (mic: %s, output: %s)", sp.name, mic.name if mic else "none",
                     out.name if out is not None else "none")
        self.voice.refresh_wake_words()

    def _cast_moved(self, dev: CastDevice, host: str, port: int) -> None:
        dev.host, dev.port = host, port
        d = self.store.cast(dev.id)
        if d is not None:
            d.host, d.port = host, port
        self.store.save()

    def speaker_for_mic(self, client_id: str) -> VirtualSpeaker | None:
        return next((vs for vs in self.speakers.values() if vs.mic_dev is not None and vs.mic_dev.id == client_id),
                    None)

    def reset_wake(self, conn: VirtualSpeaker) -> None:
        self.voice.reset_wake(conn)

    def wake_phrase(self, conn: VirtualSpeaker) -> str:
        words = self.voice.wake_words(conn)
        return (words[0] if words else "hey_jarvis").replace("_", " ")

    # -- misc ------------------------------------------------------------------------------------------------------
    def same_origin(self, origin: str, request) -> bool:
        host = request.headers.get("host", "")
        return origin.rstrip("/") in (f"https://{host}", f"http://{host}")

    def status(self) -> dict:
        cfg = self.store.config
        pbx = self.ua.accounts.get("pbx")
        return {
            "version": self.settings.version,
            "uptime": time.time() - self.started,
            "pbx": pbx.status() if pbx else {"state": "disabled"},
            "clients": [c.info() for c in self.clients.values()],
            "speakers": [vs.info() for vs in self.speakers.values()],
            "casts": {cid: c.info() for cid, c in self.casts.items()},
            "agents": {aid: rt.info() for aid, rt in self.agent_rts.items()},
            "machines": {mid: rt.info() for mid, rt in self.machine_rts.items()},
            "calls": self.calls.info(),
            "integrations": {i.id: {"last_sync": i.last_sync, "last_error": i.last_error}
                             for i in cfg.integrations},
        }
