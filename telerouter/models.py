"""Persistent configuration (pydantic). Secrets are write-only in the API (see web/app.py `redact`)."""

from __future__ import annotations

import os
import re
import secrets
import time
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def new_id() -> str:
    return secrets.token_hex(4)


NAME_RE = re.compile(r"^[^\x00-\x1f<>]{1,64}$")
TMUX_RE = re.compile(r"^[A-Za-z0-9_.-]{1,48}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.:\[\]_-]{1,253}$")
USER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/\[\]-]{0,80}$")
WAKE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

SECRET_FIELDS = {"password", "token", "password_hash", "totp_secret", "token_hash", "code_hash"}


def _name(v: str) -> str:
    v = " ".join(v.split())
    if not NAME_RE.match(v):
        raise ValueError("1-64 characters, no control characters or < >")
    return v


class Admin(BaseModel):
    username: str
    password_hash: str
    totp_secret: str = ""
    totp_enabled: bool = False
    created: float = Field(default_factory=time.time)


class Pbx(BaseModel):
    """The PBX account telerouter registers (one extension)."""
    enabled: bool = False
    host: str = ""
    port: int = Field(default=5060, ge=1, le=65535)
    username: str = ""                # the extension
    password: str = ""
    auth_username: str = ""
    domain: str = ""
    display_name: str = "Telerouter"
    expires: int = Field(default=300, ge=60, le=3600)
    contact_user: str = Field(default_factory=lambda: "tr-" + secrets.token_hex(4))

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        v = v.strip()
        if v and not HOST_RE.match(v):
            raise ValueError("invalid host")
        return v


class Integration(BaseModel):
    """A linked wa2sip or cam2sip instance (its integration API v1)."""
    id: str = Field(default_factory=new_id)
    kind: Literal["wa2sip", "cam2sip"]
    name: str = ""
    url: str                          # https://host:port of its web UI/API
    token: str = ""                   # its integration token (we are the client: stored, write-only in our API)
    tls_fingerprint: str = ""         # sha256 of its certificate (hex); "" = verify against system CAs
    tls_cert: str = ""                # the pinned certificate (PEM), the trust anchor for this link
    enabled: bool = True
    inbound: bool = True              # wa2sip: let it ring telerouter for incoming WhatsApp calls
    last_sync: float = 0.0
    last_error: str = ""

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not re.match(r"^https://[A-Za-z0-9.:\[\]_-]+(:\d+)?$", v):
            raise ValueError("must be https://host[:port] (no path)")
        return v


ContactKind = Literal["pbx", "whatsapp", "camera", "tmux"]
InboundPolicy = Literal["default", "agent", "client", "reject"]


class Contact(BaseModel):
    id: str = Field(default_factory=new_id)
    kind: ContactKind
    source: str = "manual"            # "manual" or an integration id
    source_ref: str = ""              # the target id at the source (wa2sip contact / cam2sip camera)
    name: str                         # name at the source
    alias: str = ""                   # "referred name": what agents see and what you say ("dad")
    description: str = ""             # extra hint for agents ("my father, lives in Izmir")
    number: str = ""                  # extension / phone number (pbx, whatsapp)
    enabled: bool = True              # visible to agents (MCP) and callable
    inbound: InboundPolicy = "default"
    inbound_client: str = ""          # speaker id when inbound == "client" (ring a speaker)
    inbound_agent: str = ""           # agent id when inbound == "agent" ("" = default agent)
    missing: bool = False             # gone at the source on the last sync

    @property
    def label(self) -> str:
        return self.alias or self.name

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str) -> str:
        return _name(v)

    @field_validator("alias", "description")
    @classmethod
    def _text(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v) > 200 or any(c in v for c in "<>"):
            raise ValueError("max 200 characters, no < >")
        return v

    @field_validator("number")
    @classmethod
    def _number(cls, v: str) -> str:
        v = v.strip()
        if v and not re.match(r"^\+?[0-9*#]{1,32}$", v):
            raise ValueError("digits only (optional leading +)")
        return v


class Client(BaseModel):
    """An enrolled speaker device (Linux/macOS client): a microphone and a loudspeaker on one machine.
    What agents and you talk to is a `Speaker`, which takes its microphone and its output from devices."""
    id: str = Field(default_factory=new_id)
    name: str
    room: str = ""
    token_hash: str = ""
    created: float = Field(default_factory=time.time)
    last_seen: float = 0.0
    platform: str = ""
    client_version: str = ""
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str) -> str:
        return _name(v)


class CastDevice(BaseModel):
    """A Google Cast speaker (Chromecast built-in): an output only (it has no microphone telerouter can use)."""
    id: str = Field(default_factory=new_id)
    name: str
    host: str                         # its address (updated from mDNS when DHCP moves it)
    port: int = Field(default=8009, ge=1, le=65535)
    uuid: str = ""                    # mDNS id: finds the device again under a new address
    model: str = ""
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str) -> str:
        return _name(v)

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        v = v.strip()
        if not HOST_RE.match(v):
            raise ValueError("invalid host")
        return v

    @field_validator("uuid", "model")
    @classmethod
    def _text(cls, v: str) -> str:
        v = " ".join(v.split())[:64]
        if any(c in v for c in "<>"):
            raise ValueError("no < >")
        return v


class Speaker(BaseModel):
    """A speaker as agents and you see it ("odam"): a microphone from one device and an output from another
    (or the same) device. `mic` is a client id; `output` is "client:<id>" or "cast:<id>"."""
    id: str = Field(default_factory=new_id)
    name: str
    room: str = ""
    enabled: bool = True
    mic: str = ""                     # client id ("" = no microphone: announcements only)
    output: str = ""                  # "client:<id>" | "cast:<id>" ("" = none)
    cues: Literal["auto", "output", "mic"] = "auto"   # beeps: auto = on the mic device when the output is a Cast speaker
    agent_id: str = ""                # "" = default agent
    wake_words: list[str] = Field(default_factory=list)   # [] = global setting
    volume: int = Field(default=100, ge=0, le=200)
    visible_to_agents: bool = True
    thinking_cue: Literal["default", "on", "off"] = "default"   # ticks while the agent works ("default" = Voice setting)

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str) -> str:
        return _name(v)

    @field_validator("room")
    @classmethod
    def _room(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v) > 64 or any(c in v for c in "<>"):
            raise ValueError("max 64 characters, no < >")
        return v

    @field_validator("mic")
    @classmethod
    def _mic(cls, v: str) -> str:
        if v and not re.match(r"^[0-9a-f]{8}$", v):
            raise ValueError("invalid microphone device")
        return v

    @field_validator("output")
    @classmethod
    def _output(cls, v: str) -> str:
        if v and not re.match(r"^(client|cast):[0-9a-f]{8}$", v):
            raise ValueError("invalid output device")
        return v

    @field_validator("wake_words")
    @classmethod
    def _ww(cls, v: list[str]) -> list[str]:
        for w in v:
            if not WAKE_RE.match(w):
                raise ValueError("invalid wake word model name")
        return v[:4]


class Enrollment(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    code_hash: str
    expires: float


class ExtraMcpServer(BaseModel):
    """Another MCP server the agent gets (e.g. Home Assistant's /api/mcp), next to telerouter's own."""
    name: str                          # tool prefix for the agent, e.g. "home-assistant"
    url: str                           # https://... (plain http only on loopback)
    token: str = ""                    # sent as "Authorization: Bearer <token>"; write-only in the API
    description: str = ""              # what it is for, told to the agent ("lights, switches, sensors")
    enabled: bool = True
    auto_approve: bool = True          # allow its tools without permission prompts (voice can't answer them)
    # its own CA (PEM) for servers with a private certificate (e.g. the Zigbee IR Remote app); "" = public CAs
    tls_ca: str = ""

    @field_validator("tls_ca")
    @classmethod
    def _ca(cls, v: str) -> str:
        v = v.strip()
        if not v:
            return ""
        import ssl
        if len(v) > 8000 or v.count("-----BEGIN CERTIFICATE-----") != 1:
            raise ValueError("one PEM certificate")
        try:
            ssl.create_default_context(cadata=v)       # parses it for real (base64 alone is not enough)
        except (ValueError, ssl.SSLError) as e:
            raise ValueError("not a PEM certificate") from e
        return v + "\n"

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.match(r"^[a-z0-9][a-z0-9_-]{0,31}$", v) or v == "telerouter":
            raise ValueError("lower-case letters, digits, - and _ (not 'telerouter')")
        return v

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^(https://[A-Za-z0-9.\-\[\]:]+|http://(127\.0\.0\.1|localhost)(:\d+)?)(/[A-Za-z0-9._~/%-]*)?$", v):
            raise ValueError("must be an https:// URL (plain http only for 127.0.0.1)")
        return v

    @field_validator("description")
    @classmethod
    def _desc(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v) > 300:
            raise ValueError("max 300 characters")
        return v


AgentKind = Literal["claude", "hermes"]


class Machine(BaseModel):
    """A host telerouter reaches over SSH: runs telerouter's agents and/or your own agent tmux sessions."""
    id: str = Field(default_factory=new_id)
    name: str
    host: str
    port: int = Field(default=22, ge=1, le=65535)
    username: str
    host_key: str = ""                # pinned OpenSSH public host key
    enabled: bool = True
    agent_kind: Literal["claude", "hermes"] = "claude"   # what runs in its tmux sessions
    list_tmux: bool = True            # list its tmux sessions as connections
    reply_timeout: int = Field(default=600, ge=30, le=7200)   # tmux sessions can work for a while
    # agents telerouter runs may start new tmux sessions with a fresh agent here (MCP new_session)
    allow_new_sessions: bool = False
    new_session_model: str = ""        # "" = the agent's own default
    new_session_permission_mode: Literal["default", "acceptEdits", "auto", "bypassPermissions", "dontAsk", "plan"] = "auto"

    @field_validator("new_session_model")
    @classmethod
    def _model(cls, v: str) -> str:
        if not MODEL_RE.match(v):
            raise ValueError("invalid model name")
        return v

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str) -> str:
        return _name(v)

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        v = v.strip()
        if not HOST_RE.match(v):
            raise ValueError("invalid host")
        return v

    @field_validator("username")
    @classmethod
    def _user(cls, v: str) -> str:
        if not USER_RE.match(v):
            raise ValueError("invalid user name")
        return v


class Agent(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str = "Jarvis"              # how it introduces itself on calls
    kind: AgentKind = "claude"
    enabled: bool = True
    autostart: bool = True
    machine_id: str = ""              # the Machine it runs on (host/port/user/key are copied from it)
    host: str = ""
    port: int = Field(default=22, ge=1, le=65535)
    username: str = ""
    host_key: str = ""                # pinned OpenSSH public host key ("ssh-ed25519 AAAA...")
    workdir: str = "~"
    tmux_session: str = ""
    model: str = "claude-sonnet-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max", ""] = "medium"
    permission_mode: Literal["default", "acceptEdits", "auto", "bypassPermissions", "dontAsk", "plan"] = "auto"
    hermes_provider: str = ""
    extra_instructions: str = ""      # appended to the voice instructions
    mcp_remote_port: int = Field(default=47801, ge=1024, le=65535)
    session_uuid: str = ""            # Claude Code --session-id (kept across restarts with --resume)
    mcp_token_id: str = ""
    reply_timeout: int = Field(default=300, ge=10, le=3600)
    mcp_servers: list[ExtraMcpServer] = Field(default_factory=list, max_length=10)

    @field_validator("mcp_servers")
    @classmethod
    def _unique(cls, v: list[ExtraMcpServer]) -> list[ExtraMcpServer]:
        names = [s.name for s in v]
        if len(names) != len(set(names)):
            raise ValueError("MCP server names must be unique")
        return v

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str) -> str:
        return _name(v)

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        v = v.strip()
        if v and not HOST_RE.match(v):
            raise ValueError("invalid host")
        return v

    @field_validator("username")
    @classmethod
    def _user(cls, v: str) -> str:
        if v and not USER_RE.match(v):
            raise ValueError("invalid user name")
        return v

    @field_validator("tmux_session")
    @classmethod
    def _tmux(cls, v: str) -> str:
        if v and not TMUX_RE.match(v):
            raise ValueError("letters, digits, . _ - only")
        return v

    @field_validator("model", "hermes_provider")
    @classmethod
    def _model(cls, v: str) -> str:
        if not MODEL_RE.match(v):
            raise ValueError("invalid model/provider name")
        return v

    @field_validator("workdir")
    @classmethod
    def _workdir(cls, v: str) -> str:
        v = v.strip() or "~"
        if not re.match(r"^~?[A-Za-z0-9_./ -]{0,200}$", v) or ".." in v.split("/"):
            raise ValueError("plain path (letters, digits, _ . / - and spaces)")
        return v

    @field_validator("extra_instructions")
    @classmethod
    def _extra(cls, v: str) -> str:
        if len(v) > 4000:
            raise ValueError("max 4000 characters")
        return v

    @property
    def session_name(self) -> str:
        return self.tmux_session or f"telerouter-{self.id}"


McpScope = Literal["contacts", "calls", "speak", "clients"]


class McpToken(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    token_hash: str = ""
    created: float = Field(default_factory=time.time)
    last_used: float = 0.0
    scopes: list[McpScope] = Field(default_factory=lambda: ["contacts", "calls", "speak", "clients"])
    contacts: list[str] = Field(default_factory=lambda: ["*"])    # contact ids or "*"
    clients: list[str] = Field(default_factory=lambda: ["*"])     # speaker ids or "*"
    agent_id: str = ""               # set for the token telerouter made for one of its agents

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str) -> str:
        return _name(v)

    def allows_contact(self, cid: str) -> bool:
        return "*" in self.contacts or cid in self.contacts

    def allows_client(self, cid: str) -> bool:
        return "*" in self.clients or cid in self.clients


class VoiceSettings(BaseModel):
    wake_words: list[str] = Field(default_factory=lambda: ["hey_jarvis"])
    language: str = "en"             # Whisper language ("" = auto-detect)
    tts_voice: str = "en_US-lessac-medium"
    beep_volume: int = Field(default=40, ge=0, le=100)
    listen_timeout: float = Field(default=6.0, ge=2, le=30)     # wait for speech after the start beep
    max_utterance: float = Field(default=45.0, ge=3, le=120)
    end_silence_ms: int = Field(default=1200, ge=300, le=4000)        # speakers
    phone_end_silence_ms: int = Field(default=1500, ge=300, le=4000)  # phone calls (narrowband, people pause more)
    vad_threshold: float = Field(default=0.5, ge=0.1, le=0.95)
    follow_up: bool = False           # listen again (no wake word) right after a reply
    thinking_cue: bool = True         # soft tick while the agent works (every few seconds)
    # a phone call's audio (e.g. telerouter itself saying "Hey Jarvis" on your phone) must not start a turn
    # on a speaker that hears it: no new turns while a call that isn't on that speaker is going on
    wake_during_calls: bool = False

    @field_validator("wake_words")
    @classmethod
    def _ww(cls, v: list[str]) -> list[str]:
        for w in v:
            if not WAKE_RE.match(w):
                raise ValueError("invalid wake word model name")
        if not v:
            raise ValueError("pick at least one wake word")
        return v[:4]

    @field_validator("language")
    @classmethod
    def _lang(cls, v: str) -> str:
        if v and not re.match(r"^[a-z]{2,3}$", v):
            raise ValueError("two- or three-letter language code, or empty for auto")
        return v

    @field_validator("tts_voice")
    @classmethod
    def _voice(cls, v: str) -> str:
        if not re.match(r"^[A-Za-z0-9_.-]{1,80}$", v):
            raise ValueError("invalid voice name")
        return v


class CallSettings(BaseModel):
    ring_timeout: int = Field(default=45, ge=5, le=180)
    inbound_default: Literal["agent", "client", "reject"] = "reject"   # known contacts with policy "default"
    inbound_default_client: str = ""
    unknown_callers: Literal["reject", "agent"] = "reject"
    agent_greeting: str = "Hello, this is {agent}. How can I help?"
    agent_call_pin: str = ""          # DTMF PIN asked before an inbound caller talks to an agent
    max_call_minutes: int = Field(default=60, ge=1, le=600)
    announce_calls: bool = True       # say "Calling Dad" on the speaker before ringing

    @field_validator("agent_call_pin")
    @classmethod
    def _pin(cls, v: str) -> str:
        if v and not re.match(r"^[0-9]{4,12}$", v):
            raise ValueError("4-12 digits")
        return v


HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


class QuietHours(BaseModel):
    """Do-not-disturb: quiet speakers for long tasks and no calls, unless you're at your desk."""
    enabled: bool = False
    start: str = "23:00"
    end: str = "08:00"
    timezone: str = Field(default_factory=lambda: os.environ.get("TZ") or "UTC")
    long_task_minutes: int = Field(default=5, ge=1, le=240)   # longer answers are not spoken on speakers
    block_calls: bool = True
    desk_sensors: list[str] = Field(default_factory=list)     # Home Assistant entity ids ("binary_sensor.desk_motion")
    desk_hold_minutes: int = Field(default=10, ge=0, le=240)  # motion seen this recently = still at the desk
    ha_url: str = ""          # Home Assistant base URL; "" = taken from an agent's Home Assistant MCP server
    token: str = ""           # its long-lived token (write-only); "" = from that MCP server

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not HHMM_RE.match(v.strip()):
            raise ValueError("HH:MM (24 h)")
        return v.strip()

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(v)
        except Exception as e:
            raise ValueError("unknown time zone (e.g. Europe/Istanbul)") from e
        return v

    @field_validator("desk_sensors")
    @classmethod
    def _sensors(cls, v: list[str]) -> list[str]:
        v = [x.strip() for x in v if x.strip()]
        for x in v:
            if not ENTITY_RE.match(x):
                raise ValueError(f"invalid entity id: {x}")
        return v[:20]

    @field_validator("ha_url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if v and not re.match(r"^https://[A-Za-z0-9.\-\[\]:]+(:\d+)?$", v):
            raise ValueError("https://host[:port] (no path)")
        return v


TASK_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")


class ScheduledTask(BaseModel):
    """A prompt that runs on a schedule: in an agent's conversation (Jarvis), in a new tmux session each run,
    or in one tmux session the task keeps (with an advised context size)."""
    id: str = Field(default_factory=new_id)
    name: str
    enabled: bool = True
    prompt: str
    target: Literal["agent", "new_session", "task_session"] = "agent"
    agent_id: str = ""                # agent: "" = the default agent
    machine_id: str = ""              # sessions: where they start (as the machine's SSH user)
    folder: str = "~"                 # sessions: working folder (created if missing)
    session_name: str = ""            # task_session: its tmux session ("" = task-<name>)
    keep_sessions: int = Field(default=3, ge=1, le=20)        # new_session: the last runs' sessions kept
    context_limit_k: int = Field(default=0, ge=0, le=2000)    # task_session: gentle reminder above this (k tokens)
    schedule: Literal["daily", "interval", "once", "cron"] = "daily"
    times: list[str] = Field(default_factory=lambda: ["09:00"], max_length=24)   # daily: HH:MM
    days: list[int] = Field(default_factory=list)            # daily: 0 = Monday; empty = every day
    every_minutes: int = Field(default=60, ge=5, le=60 * 24 * 31)
    at: str = ""                      # once: local date and time ("2026-10-20T09:00")
    cron: str = ""
    timezone: str = Field(default_factory=lambda: os.environ.get("TZ") or "UTC")
    timeout_minutes: int = Field(default=60, ge=1, le=24 * 60)
    # "agent": the agent's own rules decide whether it phones you about a run (e.g. your CLAUDE.md);
    # "task": it is told not to phone, message or announce anything unless the task asks for it
    calls: Literal["agent", "task"] = "agent"

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str) -> str:
        return _name(v)

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 8000:
            raise ValueError("1-8000 characters")
        return v

    @field_validator("folder")
    @classmethod
    def _folder(cls, v: str) -> str:
        v = " ".join(v.split()) or "~"
        if not re.match(r"^~?[A-Za-z0-9_./ -]{0,200}$", v) or ".." in v.split("/"):
            raise ValueError("plain path (letters, digits, _ . / - and spaces)")
        return v

    @field_validator("session_name")
    @classmethod
    def _session(cls, v: str) -> str:
        v = v.strip()
        if v and (not TASK_SESSION_RE.match(v) or v.startswith("telerouter-")):
            raise ValueError("letters, digits, - _ . (up to 40), not starting with telerouter-")
        return v

    @field_validator("times")
    @classmethod
    def _times(cls, v: list[str]) -> list[str]:
        v = [t.strip() for t in v if t.strip()]
        for t in v:
            if not HHMM_RE.match(t):
                raise ValueError("times as HH:MM (24 h)")
        return sorted(set(v))

    @field_validator("days")
    @classmethod
    def _days(cls, v: list[int]) -> list[int]:
        if any(d < 0 or d > 6 for d in v):
            raise ValueError("weekdays are 0 (Monday) to 6 (Sunday)")
        return sorted(set(v))

    @field_validator("at")
    @classmethod
    def _at(cls, v: str) -> str:
        v = v.strip()
        if v:
            import datetime as dt
            try:
                v = dt.datetime.fromisoformat(v).replace(tzinfo=None, second=0, microsecond=0).isoformat(timespec="minutes")
            except ValueError as e:
                raise ValueError("date and time like 2026-10-20T09:00") from e
        return v

    @field_validator("cron")
    @classmethod
    def _cron(cls, v: str) -> str:
        v = " ".join(v.split())
        if v:
            from .cron import Cron
            Cron(v)
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        return _check_tz(v)

    @model_validator(mode="after")
    def _schedule(self):
        if self.schedule == "daily" and not self.times:
            raise ValueError("pick at least one time of day")
        if self.schedule == "once" and not self.at:
            raise ValueError("pick the date and time")
        if self.schedule == "cron" and not self.cron:
            raise ValueError("enter the cron expression")
        return self

    @property
    def tmux_name(self) -> str:
        """task_session: the session it keeps."""
        return self.session_name or task_slug("task-" + self.name)


def task_slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n].strip("-")
    return s or "task"


def _check_tz(v: str) -> str:
    from zoneinfo import ZoneInfo
    try:
        ZoneInfo(v)
    except Exception as e:
        raise ValueError("unknown time zone (e.g. Europe/Istanbul)") from e
    return v


class SecuritySettings(BaseModel):
    session_idle_minutes: int = Field(default=30, ge=5, le=24 * 60)
    session_absolute_hours: int = Field(default=12, ge=1, le=24 * 30)


class Config(BaseModel):
    version: int = 1
    admin: Admin | None = None
    pbx: Pbx = Field(default_factory=Pbx)
    integrations: list[Integration] = Field(default_factory=list)
    machines: list[Machine] = Field(default_factory=list)
    contacts: list[Contact] = Field(default_factory=list)
    clients: list[Client] = Field(default_factory=list)          # speaker devices (enrolled clients)
    casts: list[CastDevice] = Field(default_factory=list)        # Google Cast speakers (outputs)
    speakers: list[Speaker] = Field(default_factory=list)        # virtual speakers built from devices
    enrollments: list[Enrollment] = Field(default_factory=list)
    agents: list[Agent] = Field(default_factory=list)
    default_agent_id: str = ""
    mcp_tokens: list[McpToken] = Field(default_factory=list)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    calls: CallSettings = Field(default_factory=CallSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    quiet: QuietHours = Field(default_factory=QuietHours)
    tasks: list[ScheduledTask] = Field(default_factory=list)
    # Contact user of telerouter's direct SIP address (wa2sip rings sip:<this>@telerouter:5080)
    inbound_secret: str = Field(default_factory=lambda: "in-" + secrets.token_urlsafe(18).replace("-", "x").replace("_", "y"))
