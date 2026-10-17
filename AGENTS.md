# AGENTS.md

Notes for AI agents (and humans) working on this repository. Read [ARCHITECTURE.md](ARCHITECTURE.md) before
changing voice, agent or call code. Local test-environment details (real PBX, hosts, credentials) belong in
the git-ignored `AGENTS.local.md` / `CLAUDE.local.md`; never commit credentials, phone numbers or tokens.

## What this is

telerouter: Docker Compose app. Speaker clients (Linux/macOS) stream mics to the server; openWakeWord ->
Silero VAD -> Whisper -> agent (Claude Code or Hermes in tmux over SSH) -> Piper. It is also a SIP voice
router (PBX extension, wa2sip WhatsApp, cam2sip cameras) and an MCP server the agents use.

## Commands

```bash
# tests (the dev host has no pip/venv: use the dev image)
docker build -t telerouter-dev -f Dockerfile.dev .
docker run --rm -v $PWD:/src -w /src telerouter-dev python -m pytest -q          # ~15 s

# run the whole stack locally (host networking; needs ports 8443, 5080/udp, 18000-18199/udp, 10200/10300/10400)
mkdir -p data whisper-data piper-data wake-models && sudo chown 1000:1000 data whisper-data piper-data
cp .env.example .env && docker compose up -d --build
docker logs telerouter 2>&1 | grep 'setup code'

# speaker client against a running server, with a WAV as the microphone (no sound card needed)
telerouter-client run --input-wav utt.wav --output-wav out.wav --duration 30

# fake agent host (sshd + tmux + a fake `claude` that fires Claude Code-style hooks and calls MCP)
docker build -t telerouter-fake-agent tools/fake-agent
docker run -d --name trfake -p 127.0.0.1:2222:22 -e AUTHORIZED_KEY="<telerouter's public key>" telerouter-fake-agent

# scriptable SIP phone (register, call/answer, play/record WAV, send DTMF)
docker run --rm --network host -v $PWD:/src -w /src telerouter-dev python tools/sip_phone.py \
  --server 127.0.0.1 --port 5160 --user 2001 --password ... call 1009 --play in.wav --record out.wav

# disposable Asterisk for SIP tests: wa2sip's tools/test-pbx (UDP 5160, 1009/1010/2001/2002, 600 = echo)

# UI screenshots / smoke: mcr.microsoft.com/playwright/python:v1.55.0-noble (pip install playwright==1.55.0)
```

## Conventions

- One asyncio loop (uvicorn's). Never block it: argon2 runs in a thread, SSH and Wyoming are async.
  Media callbacks (`on_audio`, `mic`) run synchronously and must stay cheap (numpy, no per-sample Python).
- Internal audio format: 16 kHz mono PCM16. SIP side: G.711 8 kHz (`router/sipaudio.py` converts).
  `StreamResampler` instances keep filter state: never share one between two streams (prompts use the
  one-shot `pcm.resample`).
- Secrets: tokens are random (`security.new_token`), only `token_hash` is stored; config secrets
  (`password`, `token`) are write-only in the API (`redact()`/`merge()` in web/app.py: an empty value keeps
  the stored one). Add new secret field names to `models.SECRET_FIELDS`.
- Web UI: vanilla JS, no build step, **no inline script or style** (CSP). Build DOM with `el()`; never
  `innerHTML` with data.
- Every state-changing admin endpoint is under `/api/` and gets CSRF + session checks from the `Guard`
  middleware automatically; public endpoints are listed in `PUBLIC`.
- MCP tools: add them in `McpServer._define()` with a scope; check contact/speaker allow-lists through
  `_find_contact`/`_find_speaker`; raise `ToolError` for user-facing errors.
- New agent kinds: a `Driver` subclass in `agents/drivers.py` (files, launch script, prompt text,
  start-up dialogs) and event matching in `AgentRuntime._on_event`.
- When you change env vars, API or behaviour, update README.md, `.env.example`, `docker-compose.yml`,
  ARCHITECTURE.md and this file.
- Commits end with the Co-Authored-By trailer; commit as the GitHub noreply identity.

## Hard-won facts

- **openWakeWord keeps the wake word in its window.** When feeding resumes after a pause (after a turn,
  or when a call starts) send a new AudioStart (`WakeStream.reset()`), or the old "hey jarvis" fires again
  and, e.g., hangs up the call that just started.
- **wyoming-faster-whisper ignores per-request vocabulary** (`transcript_names`); biasing works only with
  `--initial-prompt` (compose `WHISPER_PROMPT`) or its Home Assistant name source. Piper's "Call echo"
  sounds like "Coleco" to Whisper: use distinct names in tests.
- **tmux targets**: `-t =name` matches a *session* exactly, but pane commands (`send-keys`, `capture-pane`,
  `paste-buffer`) need `=name:` (session:window), otherwise "can't find pane".
- A tmux pane keeps old dialog text in its history: detect dialogs only in the last lines and never answer
  the same screen twice (`_handle_startup`).
- `asyncssh.get_server_host_key()` has no timeout argument: wrap it in `asyncio.wait_for`.
- Claude Code's Stop hook carries `last_assistant_message`; UserPromptSubmit carries `prompt`. Hooks and
  MCP servers are passed per session (`--settings`, `--mcp-config`) so the owner's `~/.claude` is untouched.
- Hermes registers shell hooks only when approved: `--accept-hooks` / `HERMES_ACCEPT_HOOKS=1` plus the
  allow-list entry `configure.py` writes. Its TLS trust is the OS store (no CA env var), which is why MCP
  goes through the SSH tunnel as plain loopback HTTP.
- `Account.dial()` returns before the INVITE task runs: a fresh call's state can still be `init`.
- Python 3.13 enables `VERIFY_X509_STRICT`: telerouter's CA has SKI and the server cert AKI/SKI/KeyUsage,
  otherwise strict clients refuse them.
- The dev host's port 18443 is taken by another project; the loopback MCP port defaults to 47700.
- MCP SDK 2.x moved FastMCP; telerouter implements the small MCP surface itself (`mcp.py`).
- **Claude Code's workspace-trust dialog preselects "No, exit"** (v2.1.302): pressing Enter quits the agent.
  `drivers.menu_keys()` moves the `❯` cursor to the wanted option first.
- PBX numbers are dialed exactly as entered: feature codes keep `*`/`#` (`*43` is FreePBX's echo test, a safe
  live target). Stripping non-digits dialed "43".
- Agents search contacts by words like "camera": `list_contacts` matches the kind and its synonyms too.

## Status (v0.1.0, 2026-10-17)

Verified: speaker turn end to end with real Whisper/Piper/openWakeWord (WAV-driven client); agent
plumbing over SSH/tmux/hooks/reverse-tunnel MCP with the fake agent; outbound and inbound calls through
Asterisk; wa2sip (simulated WhatsApp) both directions. In production (FreePBX 17, real Claude Code with
Sonnet 5.5): "Hey Jarvis, call the echo test" -> connect_call -> *43 bridged to a speaker; an inbound call
through the PBX talking to the agent; phone_message with a transcribed reply; wa2sip/cam2sip linked and
synced (no real WhatsApp contact or camera was called during testing).
Not verified: macOS client (code paths are cross-platform, untested on a Mac); Hermes against a real
Hermes install; echo cancellation on real hardware.
