"""Persistent configuration (pydantic). Secrets are write-only in the API (see web/app.py `redact`)."""

from __future__ import annotations

import re
import secrets
import time
from typing import Literal

from pydantic import BaseModel, Field, field_validator


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


ContactKind = Literal["pbx", "whatsapp", "camera"]
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
    inbound_client: str = ""          # client id when inbound == "client"
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
    """A speaker device (Linux/macOS client)."""
    id: str = Field(default_factory=new_id)
    name: str
    room: str = ""
    token_hash: str = ""
    created: float = Field(default_factory=time.time)
    last_seen: float = 0.0
    platform: str = ""
    client_version: str = ""
    enabled: bool = True
    agent_id: str = ""                # "" = default agent
    wake_words: list[str] = Field(default_factory=list)   # [] = global setting
    volume: int = Field(default=100, ge=0, le=200)
    visible_to_agents: bool = True

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str) -> str:
        return _name(v)

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


AgentKind = Literal["claude", "hermes"]


class Agent(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str = "Jarvis"              # how it introduces itself on calls
    kind: AgentKind = "claude"
    enabled: bool = True
    autostart: bool = True
    host: str
    port: int = Field(default=22, ge=1, le=65535)
    username: str
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
    clients: list[str] = Field(default_factory=lambda: ["*"])     # client ids or "*"
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
    max_utterance: float = Field(default=20.0, ge=3, le=120)
    end_silence_ms: int = Field(default=900, ge=300, le=4000)
    vad_threshold: float = Field(default=0.5, ge=0.1, le=0.95)
    follow_up: bool = False           # listen again (no wake word) right after a reply
    thinking_cue: bool = True         # soft tick while the agent works (every few seconds)

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


class SecuritySettings(BaseModel):
    session_idle_minutes: int = Field(default=30, ge=5, le=24 * 60)
    session_absolute_hours: int = Field(default=12, ge=1, le=24 * 30)


class Config(BaseModel):
    version: int = 1
    admin: Admin | None = None
    pbx: Pbx = Field(default_factory=Pbx)
    integrations: list[Integration] = Field(default_factory=list)
    contacts: list[Contact] = Field(default_factory=list)
    clients: list[Client] = Field(default_factory=list)
    enrollments: list[Enrollment] = Field(default_factory=list)
    agents: list[Agent] = Field(default_factory=list)
    default_agent_id: str = ""
    mcp_tokens: list[McpToken] = Field(default_factory=list)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    calls: CallSettings = Field(default_factory=CallSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    # Contact user of telerouter's direct SIP address (wa2sip rings sip:<this>@telerouter:5080)
    inbound_secret: str = Field(default_factory=lambda: "in-" + secrets.token_urlsafe(18).replace("-", "x").replace("_", "y"))
