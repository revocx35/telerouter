# Architecture

telerouter is two things in one Docker Compose app:

1. **A smart-speaker manager.** Small clients on Linux/macOS machines stream their microphone to the
   server and play what it sends back. The server listens for a wake word (openWakeWord), records what
   you say (Silero VAD), turns it into text (Whisper), hands it to an **AI agent** (Claude Code or Hermes)
   running in tmux on another machine over SSH, and speaks the answer (Piper).
2. **A voice router.** It is a SIP endpoint: it registers an extension on your PBX, calls **wa2sip**
   (WhatsApp) and **cam2sip** (cameras) directly, takes incoming calls, and bridges any of them to a
   speaker, to the agent, or to another call. The agent drives all of this through telerouter's own
   **MCP server**, and so can any other MCP client ("call my WhatsApp and tell me the build is done").

## 1. Components

```mermaid
flowchart LR
  subgraph lxc["telerouter host (Docker, host networking for SIP/RTP)"]
    app["telerouter (Python/asyncio)<br/>HTTPS :8443 UI + API + /ws/client + /mcp<br/>SIP UDP :5080 · RTP 18000-18199<br/>loopback :47700 (MCP for agent tunnels)"]
    oww["openWakeWord<br/>127.0.0.1:10400"]
    whisper["Whisper (faster-whisper)<br/>127.0.0.1:10300"]
    piper["Piper<br/>127.0.0.1:10200"]
  end
  spk1["speaker client<br/>(Linux / macOS)"] -- "wss: mic PCM up / audio down" --> app
  app -- Wyoming --> oww & whisper & piper
  app -- "SSH (key, pinned host key)<br/>tmux · hooks · reverse tunnel" --> agenthost["agent host<br/>Claude Code / Hermes in tmux"]
  agenthost -. "MCP over the tunnel<br/>127.0.0.1:47801 -> :47700" .-> app
  app -- "SIP/RTP (REGISTER, calls)" --> pbx["PBX (FreePBX)"]
  app -- "HTTPS integration API (pinned cert)<br/>+ direct SIP" --> wa2sip & cam2sip
  other["any MCP client<br/>(token)"] -- "HTTPS /mcp" --> app
```

| Process | Image | Exposed | Notes |
|---|---|---|---|
| telerouter | built here | `0.0.0.0:8443/tcp`, `5080/udp`, `18000-18199/udp`, `127.0.0.1:47700` | host network; uid 1000, read-only root, `cap_drop: ALL` |
| whisper | `rhasspy/wyoming-whisper:3.8.1` | `127.0.0.1:10300` | `small-int8` on CPU by default, `--initial-prompt` from `WHISPER_PROMPT` |
| piper | `rhasspy/wyoming-piper:2.5.2` | `127.0.0.1:10200` | downloads voices on first use into `piper-data/` |
| openwakeword | `rhasspy/wyoming-openwakeword:2.1.0` | `127.0.0.1:10400` | built-in models + `wake-models/*.tflite` |

## 2. Code map

```
telerouter/
  __main__.py          two uvicorn servers: public HTTPS app + loopback MCP app
  settings.py          TELEROUTER_* environment
  models.py            pydantic config (Pbx, Integration, Contact, Client, Agent, McpToken, settings)
  store.py             /data/config.json (0600, atomic), history.json
  security.py          argon2id, tokens (stored as SHA-256), TOTP, LoginThrottle, server-side sessions
  tls.py               local CA + server certificate (re-issued when names change; CA stays)
  engine.py            wiring: SIP UA + accounts, agent runtimes, integration clients, contact lookups
  mcp.py               MCP server (Streamable HTTP, JSON responses, tools only)
  voice/
    client_conn.py     one speaker device connection (client): paced audio out, mic in, state
    speaker.py         VirtualSpeaker: a mic device + an output device; play/cue/live routing
    cast.py            Google Cast outputs: Cast v2 control (TLS :8009), mDNS discovery, plain-HTTP media listener
    assistant.py       wake -> listen -> STT -> agent -> TTS turn; mic routing; speakable()
    speech.py          Wyoming clients (Whisper, Piper, openWakeWord WakeStream)
    vad.py             Silero endpointing
  agents/
    ssh.py             telerouter's SSH key, host-key fetch/pin, connect
    drivers.py         Claude Code / Hermes: files, launch script, hook script, Hermes config edit
    runtime.py         AgentRuntime: SSH, reverse tunnel, tmux session, ask(), event tail
  router/
    calls.py           CallManager + CallSession (speaker / agent / mcp / bridge modes), inbound routing
    sipaudio.py        G.711 8 kHz <-> 16 kHz PCM, paced RTP, prompts, DTMF
    integrations.py    wa2sip/cam2sip integration API client (pinned TLS)
  sip/, media/         SIP stack + RTP/G.711/DTMF shared with wa2sip and cam2sip (MIT, same author)
  web/app.py           FastAPI: auth, admin API, /ws/client, client installer, /mcp
  web/static/          vanilla JS SPA (no build step, no inline script/style: strict CSP)
client/                telerouter-client (Python >= 3.9): enroll, run, devices, test-audio; install.sh
tests/                 pytest (auth paths, MCP, agents, SIP, TLS, voice, client)
tools/                 sip_phone.py (scriptable SIP phone), fake-agent/ (sshd + fake claude for tests)
```

## 3. Speakers

A **speaker** (`models.Speaker`, runtime `voice/speaker.py: VirtualSpeaker`) is what agents and the owner
talk to. It is built from devices: its **microphone** is an enrolled client (`Client`), its **output** an
enrolled client or a **Google Cast speaker** (`CastDevice`). The classic case is one client for both (every
enrolled client gets such a speaker, with the client's id); "odam" = the Raqqa laptop's mic + the Mi Smart
Speaker's loudspeaker. `Engine.sync_speakers()` turns config + connected devices into online
VirtualSpeakers (online = mic device connected and output there); a changed binding builds a new one, like
a reconnect. A microphone feeds one speaker (the API moves it). Device ids and speaker ids are separate
lists; MCP token allow-lists (`McpToken.clients`) and "ring a speaker" (`inbound_client`) hold speaker ids.

| VirtualSpeaker | audio | goes to |
|---|---|---|
| `play()` | answers, announcements, "Calling dad", incoming ring | the output |
| `cue()` | blips and thinking ticks | the output, or the mic device (`cues`; `auto` = mic device when the output is Cast) |
| `live()` | live call audio, ring-back | the output if `live_ok` (a client), otherwise the mic device |

Barge-in (AEC) only when mic and output are the same client. Wake detection pauses while any of the
speaker's outputs plays.

### 3.0 Google Cast outputs (`voice/cast.py`)

Control is Cast v2: TLS to the device's port 8009, length-prefixed protobuf `CastMessage` frames
(hand-encoded, six fields), JSON payloads; heartbeat PING/PONG. telerouter launches the Default Media
Receiver (`CC1AD845`) on first use (or ahead, `prepare()`, when a turn starts listening), keeps it, and stops
it after 5 idle minutes. Measured on a Mi Smart Speaker Lite (Cast firmware 1.52):

| what | result |
|---|---|
| launch the receiver | ~1.3-1.5 s |
| LOAD of a complete WAV -> PLAYING | ~0.15 s; `IDLE/FINISHED` arrives when playback ends |
| live WAV stream (unknown length) | starts only after ~7 s of 16 kHz audio is buffered, then stalls: unusable |

So `CastOutput` cuts audio into segments (what arrives within 120 ms: a sentence, a beep), LOADs each as a
complete WAV and waits for FINISHED (exact `drained()`); sentences that arrive while one plays become the
next segment. `flush()` sends media STOP. `live_ok = False`: calls play on the mic device. When the device
is unreachable the audio is handed to the speaker's mic device (`spill`) and the device is skipped for
60 s; its mDNS id finds it again after a DHCP move (`discover()`, `_googlecast._tcp.local`).

The receiver fetches the media itself, and Cast accepts HTTPS only with a publicly trusted certificate,
so `CastMedia` is a small plain-HTTP listener (`TELEROUTER_CAST_PORT`, 8480, started on first use): only
`GET /cast/<128-bit random>.wav`, only from the address of the device it was offered to, gone after
FINISHED or 60 s, 16 connections max, 8 KiB request head, no other paths.

### 3.1 Protocol

`wss://<server>:8443/ws/client` with `Authorization: Bearer trc_...` (the client's token).

| direction | frame | meaning |
|---|---|---|
| client -> server | binary | microphone, 16 kHz mono PCM16 LE, 20 ms blocks |
| client -> server | `{"type":"hello","version","platform","aec"}` | after connecting |
| client -> server | `{"type":"button","action":"talk"\|"cancel"}` | optional push-to-talk |
| server -> client | binary | audio to play, 16 kHz mono PCM16 LE, ~100 ms chunks |
| server -> client | `{"type":"state","state":...}` | idle, listening, thinking, speaking, ringing, call |
| server -> client | `{"type":"flush"}` | drop queued playback now (barge-in, call ended) |
| server -> client | `{"type":"ping"}` / `{"type":"config","volume"}` | keep-alive, settings |

The server paces playback (≤250 ms ahead of real time), so it always knows when the client has
finished playing (`ClientConn.drained()`): blips end before listening starts, and wake-word detection is
paused while the speaker talks (unless the client cancels echo, see 3.3).

### 3.2 One voice turn (`voice/assistant.py`)

```mermaid
sequenceDiagram
  participant C as speaker client
  participant T as telerouter
  participant W as openWakeWord
  participant S as Whisper
  participant A as agent (tmux)
  participant P as Piper
  C->>T: mic audio (continuous)
  T->>W: audio (idle state)
  W-->>T: detection hey_jarvis
  T->>C: start blip, state listening
  C->>T: "call my dad"
  Note over T: Silero VAD: speech, then 0.9 s pause
  T->>C: end blip, state thinking
  T->>S: utterance
  S-->>T: "Call my dad."
  T->>A: tmux paste "[telerouter #a1b2 · Kitchen speaker] Call my dad."
  A->>T: MCP connect_call(contact="dad")  (through the SSH tunnel)
  T-->>C: "Calling dad", ring-back, then the call (state call)
  A-->>T: Stop hook: last_assistant_message
  Note over T: speaker is in a call: the reply is not spoken
```

Mic routing by state: `idle` -> wake word; `listening` -> endpointer; `call` -> SIP call + wake word
(the wake word hangs up); `ringing` -> wake word (answers). While a call that is not on this speaker is in
progress, the wake word does not start a turn (a phone near the speaker saying "Hey Jarvis" must not trigger
it; Voice setting `wake_during_calls`). Whenever wake feeding resumes after a pause
the wake stream is reset (new AudioStart), otherwise the wake word that started the last turn, still in
openWakeWord's window, fires again.

### 3.3 Client

`client/telerouter_client`: PortAudio via `sounddevice` (Linux/macOS), opens the devices at 16 kHz or
resamples from their native rate; optional WebRTC echo cancellation + noise suppression (`livekit` APM,
the `aec` extra), fed with what it plays as the reverse stream. With AEC the server allows barge-in
(wake word while it speaks). `--input-wav/--output-wav` replace the devices for tests.

Enrollment: the admin creates a one-time code (15 min); the install command pins the server's public
key (`curl --pinnedpubkey`) and the CA fingerprint; the client downloads `/ca.pem`, checks its SHA-256,
then enrolls over TLS that trusts only that CA and gets a `trc_` token (only its hash is stored). The
installer creates a systemd user service (Linux) or a launchd agent (macOS).

## 4. Agents

`agents/runtime.py`, one `AgentRuntime` per agent:

1. SSH with telerouter's ed25519 key (`/data/ssh/id_ed25519`) to the configured user; the host key was
   fetched and confirmed by the admin (fingerprint shown) and is pinned.
2. Checks `tmux` and the agent binary, creates `~/.telerouter/<agent-id>/` (0700).
3. **Reverse tunnel**: `127.0.0.1:<mcp_remote_port>` on the agent host -> telerouter's loopback MCP
   listener. The agent reaches MCP without network access to telerouter and without trusting a
   self-signed certificate; the hop is inside SSH.
4. Writes the driver's files and starts `tmux new-session -d -s telerouter-<id> sh start.sh`. The owner
   can `ssh -t user@host tmux attach -t telerouter-<id>` and watch or type.
5. Tails `events.jsonl` over SSH (`tail -F`). The hook script only appends the hook's JSON as one line.

| | Claude Code | Hermes |
|---|---|---|
| launch | `claude --session-id/--resume <uuid> --model claude-sonnet-5-5 --effort medium --permission-mode auto --settings settings.json --mcp-config mcp.json --append-system-prompt "$(cat instructions.md)"` | `hermes -c telerouter-<id> --create-if-missing --accept-hooks [--model --provider --yolo]` |
| hooks | from `--settings` (nothing in `~/.claude` changes): UserPromptSubmit, Stop, Notification | `post_llm_call`, `on_session_end` in `~/.hermes/config.yaml` (edited once, with a backup; allow-listed) |
| reply | Stop `last_assistant_message` (fallback: transcript tail) | `post_llm_call` `extra.assistant_response` |
| MCP | `mcp.json` (0600) with a per-start `trm_` token; `permissions.allow: mcp__telerouter` | `mcp_servers.telerouter` in config.yaml |
| voice rules | `--append-system-prompt` | appended to each request |

**Context management**: after every turn telerouter reads the context size from the transcript's last
`usage` (input + cache + output tokens) and puts it in the next voice request's header ("context 112k
tokens"). The agent-only MCP tools `context_status` and `reset_context(mode, keep)` let the agent decide to
compact (keeping what it names) or clear; telerouter types `/compact <keep>` / `/clear` (Hermes:
`/compress`) into the pane right after the current turn's Stop hook, never during a voice request.

**Extra MCP servers** (`Agent.mcp_servers`, e.g. Home Assistant's `/api/mcp`): added to Claude's
`mcp.json` (with `permissions.allow: mcp__<name>` when auto-approve is on) or to Hermes' `mcp_servers`
(telerouter remembers which names it added in `hermes-managed.json` and removes them again). HTTPS only
(plain http only for 127.0.0.1), bearer token write-only in the API; the voice instructions list them and tell
the agent to use them directly for smart-home requests.

**Correlation**: each request carries a nonce (`[telerouter #a1b2 · Kitchen speaker] ...`). Claude: the
UserPromptSubmit event whose `prompt` contains the nonce names the session; the next Stop of that
session is the answer. Hermes: the `post_llm_call` whose `user_message` contains the nonce. Turns typed by
hand in tmux are ignored. Notification events during a voice request (permission prompts) are spoken
("Jarvis needs you at the terminal: ...").

Start-up dialogs (workspace trust, permission-mode confirmations) are recognised on the pane and
answered. Changing model/effort/permissions/instructions restarts the session (Claude resumes the same
conversation by its session id; "New conversation" starts a fresh one).

### 4.0 Quiet hours (`quiet.py`)

`Quiet.active()` checks the local time window (crossing midnight). During it, `VoiceAssistant._quiet_wait`
stops waiting for an agent answer after `long_task_minutes` and says nothing (ticks stop too; the agent keeps
working). `Quiet.calls_allowed()` blocks incoming calls (`CallManager.on_incoming` rejects them) and the MCP
tools that would ring or talk on their own (`phone_message`, `start_voice_call`, `announce`, `ask_speaker`,
`connect_call` without a live voice request), unless Home Assistant says the owner is at the desk: a chosen
motion/occupancy sensor is on, or was on within `desk_hold_minutes` (`/api/states`, cached 20 s, using an
agent's Home Assistant MCP server URL + token unless set explicitly).

### 4.1 Machines and tmux sessions

A **Machine** (`models.Machine`) is an SSH target with a pinned host key and the agent type its tmux
sessions run. Agents run on a machine (their SSH fields are copied from it; 0.3 configs are migrated, with a
backup of config.json). `agents/machine.py` keeps one SSH connection per machine, lists `tmux list-sessions`
every 60 s (except telerouter's own `telerouter-*`), and the engine merges them into connections of kind
`tmux` (missing ones are marked gone).

Talking to a session telerouter did not start (no hooks there): Claude Code writes
`~/.claude/sessions/<pid>.json` for the claude process in the pane, naming the live session id and so its
transcript. telerouter notes the transcript's length, pastes the request (with a nonce) into the pane and
waits for the `system`/`turn_duration` entry after it; the last assistant text of that turn is the answer.
Hermes sessions fall back to watching the pane until it is quiet.

**Routing**: MCP `connect_call` with a tmux contact (or the UI's *Connect*) puts a `Route` on the speaker or
the phone call. Their following requests go to the session instead of the agent; a short "back to <agent>",
"disconnect" or "leave the session" (≤ 7 words) ends it, and so do 15 idle minutes.

**New sessions** (MCP `new_session`, agent-only scope, machines with `allow_new_sessions`): telerouter runs
`mkdir -p <folder> && tmux new-session -d -s <name> -c <folder> '<agent>; exec $SHELL -l'` over the machine's
SSH connection (`claude -n <name> [--model] [--permission-mode]`, from the machine's new-session settings;
`hermes` on Hermes machines), answers Claude Code's start-up dialogs (workspace trust, auto mode) on the
pane, waits until the UI is still and `~/.claude/sessions/<pid>.json` exists (so the first routed request
finds the transcript), lists the sessions (the new one becomes a tmux contact) and routes the requester to
it. Names: `[A-Za-z0-9][A-Za-z0-9_.-]{0,39}`, not `telerouter-*`, not an existing session; folders: plain
paths without `..` (quoted; `~` = the SSH user's home).

## 5. Calls

`router/calls.py`. Every call is a `CallSession` with one SIP leg (`router/sipaudio.py`: G.711 <-> 16 kHz,
20 ms pacing, prompts above live audio, ring-back tone, RFC 4733/INFO DTMF) and a mode:

| mode | what | started by |
|---|---|---|
| speaker | contact <-> speaker client (live) | MCP `connect_call` from a speaker request, UI "Call", inbound "ring speaker" |
| agent | inbound caller talks to the agent: greeting, blip, listen, STT, agent, TTS, loop | inbound policy "talk to agent" |
| mcp | the agent talks itself: `start_voice_call` + `say`/`listen`/`hang_up`, `phone_message` | MCP |
| bridge | phone caller <-> another contact (the caller asked the agent to connect them) | MCP `connect_call` from a phone request |

### 5.1 Where calls go

| contact kind | how telerouter dials |
|---|---|
| pbx | its PBX extension (REGISTERed account `pbx`), digest auth |
| whatsapp | wa2sip `POST /api/integration/v1/calls {target}` -> one-time `sip:tr…@wa2sip:5064`; direct INVITE. wa2sip answers 180 while WhatsApp rings, 200 when answered, 486/404/603/480 otherwise |
| camera | cam2sip, same API; answered at once (camera intercom) |

### 5.2 Incoming

- **PBX**: INVITEs for the PBX account are accepted only from the PBX's address.
- **Direct** (wa2sip ringing us for a WhatsApp call): to `sip:<inbound_secret>@telerouter:5080`, only from
  the address of a linked integration. wa2sip sends `X-Telerouter-Target: <account>:<number>@c.us` and the
  caller's number in P-Asserted-Identity.
- The caller is matched to a contact; its **inbound policy** decides: talk to an agent (optionally after a
  DTMF PIN), ring a speaker (announce + ring; the wake word answers), or reject (603). Unknown callers
  are rejected by default.

## 6. MCP server

Streamable HTTP, JSON responses, stateless, tools only (`mcp.py`). Endpoints: `POST https://…:8443/mcp`
and the loopback listener used by agent tunnels. `Authorization: Bearer trm_…` (SHA-256 stored). Scopes:
`contacts`, `calls`, `speak`, `clients`; plus allow-lists of contacts and speakers.

| tool | scope | |
|---|---|---|
| list_contacts | contacts | referred names, kind, notes, number |
| list_speakers | clients | online state; tells the agent which speaker the current request came from |
| connect_call | calls | contact <-> requesting speaker / named speaker / bridge the phone caller |
| phone_message | calls | ring, speak a message (repeat), optionally record a spoken reply, hang up |
| start_voice_call, say, listen, hang_up, list_calls | calls | agent-led conversation |
| announce, ask_speaker | speak | talk/ask through a speaker |
| context_status, reset_context | (agents telerouter runs) | own context size; compact/clear after the current reply |
| new_session | (agents telerouter runs) | new tmux session with a fresh agent on a machine that allows it; connects the requester |

The SDK's FastMCP API changed in mcp 2.x; the protocol surface needed here (initialize, ping,
tools/list, tools/call, notifications) is small, so it is implemented directly and tested.

## 7. Security and threat model

**Assets**: the agent (it can run commands on its host), the PBX/WhatsApp/camera reach (calls cost
money, privacy), the microphones in the house, and the credentials telerouter holds (PBX secret,
integration tokens, SSH key).

| Threat | Mitigation |
|---|---|
| Someone on the LAN claims the first admin account | setup needs a one-time setup code printed in the container log (or `TELEROUTER_SETUP_CODE`) |
| Password guessing | argon2id; per-IP and per-account throttle with growing locks + global cap; generic errors; constant-time compares; optional TOTP (replay-protected) |
| Session theft / CSRF / clickjacking | server-side sessions (`__Host-` cookie, Secure, HttpOnly, SameSite=Strict), rotated on login, idle + absolute timeouts, logout and password change delete them; CSRF token on every state change + Fetch-Metadata refusal; `X-Frame-Options: DENY`, `frame-ancestors 'none'` |
| XSS | strict CSP (`script-src 'self'`, no inline script/style), DOM built with `textContent` only |
| Plain-text credentials on the wire | HTTPS only (self-signed CA; clients and the installer pin it); integrations over HTTPS with a pinned certificate; agent MCP inside SSH |
| Stolen/leaked tokens | random 256-bit tokens, only SHA-256 stored, sent in headers, revocable (speaker removal, MCP token revoke, integration token rotation); agent MCP token rotates on every session start; secrets write-only in the API |
| Rogue speaker client | needs an enrollment code (single use, 15 min, throttled); WebSocket frames size/rate capped; max clients |
| Eavesdropping on Cast speaker audio | Cast receivers fetch media only over plain HTTP (no publicly trusted certificate on a LAN): what a Cast output plays (answers, announcements; never live calls or microphone audio) crosses the LAN unencrypted. Single-use 128-bit URLs, served only to that device's address, withdrawn after playback / 60 s; the listener runs only once a Cast output is used. The Cast control channel is TLS but unauthenticated (device certificates are self-signed and rotate; Cast device auth is not implemented), so a LAN attacker who impersonates the device can get those URLs too. Don't use Cast outputs for private answers on an untrusted network |
| Rogue Cast device address | admin-only; telerouter only sends control messages and serves the offered WAV to it; mDNS updates the address only for the stored device id |
| Spoofed SIP INVITEs | PBX account only from the PBX's IP; direct address is a 128-bit secret and only from linked integrations' IPs; unknown callers rejected; optional DTMF PIN before the agent; wa2sip/cam2sip tickets are single-use, 30 s, bound to the requesting IP |
| Spoofed caller ID on the PBX | caller policy per contact; use the agent PIN if the PBX takes calls from outside |
| A voice request makes the agent do harm | the agent runs with the permission mode you choose (default `auto`); permission prompts are announced, never auto-approved; the agent instructions forbid calling anyone not asked for; MCP tokens can be limited to contacts/speakers |
| Voice reaches your coding sessions | tmux sessions are only reachable when visible (ticked) under Connections and allowed by the token; callers who may talk to the agent can also be routed there, so keep inbound "talk to agent" for trusted callers (+PIN) |
| Voice starts new agents on a machine | `new_session` only for telerouter's own agents (not for MCP tokens), only on machines where the admin enabled it (off by default), with the admin's model/permission mode; name and folder are validated and shell-quoted; the agent runs as that machine's SSH user, like the sessions you start yourself |
| MITM of the SSH connection | host key fetched once, shown as a fingerprint, pinned |
| Agent host compromise reaching telerouter | its MCP token only reaches MCP tools; telerouter never accepts commands from the agent host otherwise |
| SSRF via integration URLs | admin-only, HTTPS only, no redirects followed; the SIP address from the integration is rewritten to the integration's own host |
| Container escape / privilege | non-root uid 1000, read-only root fs, `cap_drop: ALL`, `no-new-privileges`, speech services bound to 127.0.0.1 |

Not covered: SIP over TLS/SRTP (the PBX side is UDP/RTP on the LAN like the rest of the user's setup);
the speech services trust anyone on the host's loopback.

## 8. Design decisions

| Decision | Why |
|---|---|
| Wyoming services (Rhasspy images) for Whisper/Piper/openWakeWord | maintained images, standard protocol, swap models without code changes; isolated processes |
| Wake word on the server, mic streamed | the user asked for streaming clients; one place to change wake words; clients stay tiny |
| Agent in tmux + hooks, not `claude -p` | the owner can attach and watch or take over; a persistent conversation; hooks give exact turn ends (no screen scraping) |
| Reverse SSH tunnel for MCP | no network path from the agent host to telerouter needed, no CA trust on the agent host, encrypted |
| Direct SIP to wa2sip/cam2sip with one-time addresses | no PBX extension needed per contact, no IVR digit games; the integration API gives names/ids |
| Own MCP implementation | small surface, no dependency on a moving SDK API |
| Shared SIP stack with wa2sip/cam2sip | proven against FreePBX 17; same author |
