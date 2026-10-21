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

## Where things live

`telerouter/` server (web/app.py API + UI routes, web/static/ the UI, voice/, agents/, router/ calls, sip/ + media/,
mcp.py, sso.py, scheduler.py, quiet.py), `client/` the Linux/macOS speaker client, `tests/` pytest, `tools/` test
helpers (SIP phone, fake agent, e2e). The full code map is in ARCHITECTURE.md §2. The Android app is a separate repo
(telerouter-android) that shows this web UI.

## Never

- Commit credentials, tokens, phone numbers, LAN addresses or hostnames of the owner's setup (they belong in the
  git-ignored CLAUDE.local.md / AGENTS.local.md).
- Store a secret in plain text in an API answer (use `SECRET_FIELDS` + `redact()`), log tokens, or put tokens in URLs.
- Add inline script/style to the UI or build DOM with `innerHTML` from data (CSP, XSS).
- Add a state-changing endpoint outside `/api/` or to `PUBLIC` without its own protection (throttle, one-time code).
- Trust a proxy's user header for login, or disable certificate verification on outbound calls.
- Poll the production API with a stored password, or test-call real WhatsApp contacts or cameras.

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
- **Claude Code 2.1.306 draws that dialog before it reads keys** (fixed in 0.10.1): an arrow sent the moment
  it appears is dropped, the Enter right after it isn't, so "No, exit" was chosen and the HomeLab task failed
  with "no Claude Code is running". `menu_keys()` now returns only the moves until the screen shows the cursor
  on the target, then Enter; the start-up loops resend keys when the screen didn't change for 3 s, and
  `_settle` stops at "the agent exited". Reproduce: start `claude` in `~` (home-folder trust is never saved)
  in tmux and send Down+Enter as soon as "Yes, I trust" is on screen. tmux `pane_current_command` shows
  `bash` for a running claude after an update; use `claude_transcript()` to tell if claude is alive.
- PBX numbers are dialed exactly as entered: feature codes keep `*`/`#` (`*43` is FreePBX's echo test, a safe
  live target). Stripping non-digits dialed "43".
- Home Assistant's MCP server (`/api/mcp`) answers 404 until the "Model Context Protocol Server"
  integration is added in HA; it accepts a long-lived access token as Bearer. Extra MCP servers are agent
  settings (`Agent.mcp_servers`), not telerouter tools.
- Context management (MCP `reset_context`): typing `/compact <keep>` or `/clear` with `tmux send-keys -l`
  then Enter works in Claude Code 2.1.302. `/compact` keeps the session id (SessionStart source "compact");
  `/clear` starts a new session id (SessionStart source "clear"), which `AgentRuntime._track` stores as
  `session_uuid` so restarts `--resume` the cleared conversation. The reset waits for the turn's Stop hook.
- **Claude Code ends some turns without the Stop hook**: on a subscription usage limit it prints "You've hit
  your session limit · resets …" and ends the turn (transcript: assistant text + `system`/`turn_duration`).
  `AgentRuntime._watch_turn_end` tails the transcript during a voice request and answers with that text,
  instead of leaving the caller waiting for `reply_timeout`.
- **Claude Code records the system prompt at a conversation's first request and reuses it on every
  `--resume`** (`--system-prompt-snapshot`, default on): changed voice instructions never reached Jarvis's
  existing conversation (verified: APPLE stayed APPLE after resuming with PEAR). start.sh passes
  `--system-prompt-snapshot off` when the installed claude knows the flag.
- Agents had Claude Code's own peer-session tools (ListAgents/SendMessage) and used them for "connect me to the
  X session"; the instructions now say voice goes through telerouter's connect_call. `mcp.fuzzy_pick` matches
  garbled spoken names ("Tether Outer T-Max" -> telerouter).
- **0.9 s of silence cut people off mid-sentence** ("…I usually close my…"). Endpointing now keeps a second
  listener running while Whisper transcribes; if the text looks unfinished (`vad.looks_unfinished`) or more
  speech arrived within 3 s, the parts are joined and re-transcribed (max 2 times). Defaults: 1.2 s on
  speakers, 1.5 s on phone calls, 45 s per request; old stored defaults are bumped by `store.migrate`.
- **Headless Linux speakers (no PulseAudio/PipeWire)**: Ubuntu's PortAudio includes the PulseAudio host API and
  fails to initialise at all without a sound server (`import sounddevice` raises). The client then falls back
  to `AlsaIO` (arecord/aplay, alsa-utils). Proxmox LXC: pass /dev/snd/controlC0, pcmC0D0p, pcmC0D0c, timer
  with `devN: …,gid=29,mode=0660`; set mixer levels in the service's ExecStartPre (no udev/alsa-restore).
- Foreign tmux sessions (not started by telerouter) have no hooks: `~/.claude/sessions/<pid>.json` (pid of
  the pane's claude, or a child of the pane's shell) gives the session id -> transcript. Read it from the
  line count noted before typing (`tail -n +N`), not a fixed tail: long turns push the prompt out.
- Over SSH (no terminal) tmux prints control characters in `-F` formats as `_`: tab-separated
  `list-sessions` output arrives as `calls_claude_/root`. Use a printable separator (`|~|`).
- **Speakers are virtual** (0.6): `Speaker` = mic device (client id) + output (`client:<id>` | `cast:<id>`);
  `VirtualSpeaker` is what voice/calls/MCP use (`engine.speakers`), `ClientConn` is only the device
  (`engine.clients`). Use `play()` for speech, `cue()` for beeps, `live()` for call audio. 0.5 configs are
  migrated in `store.migrate_raw` (each client -> a speaker with the same id, backup kept).
- **Google Cast**: a live WAV stream does not start before ~7 s is buffered (measured on a Mi Smart Speaker
  Lite), complete WAVs start in ~0.15 s and report FINISHED. Hence one LOAD per segment and no live calls on
  Cast outputs. Cast media is plain HTTP by necessity (receivers need publicly trusted HTTPS). When testing
  on a real Cast speaker at night, mute it (`SET_VOLUME {"muted": true}`) and unmute in a `finally`.
- **new_session** (0.7): `MachineRuntime.new_session` starts `claude -n <name>` in a new tmux session and
  answers the trust dialog with `drivers.claude_dialog_keys` (shared with the agents' start-up). Verified
  with a real Claude Code 2.1.302: trust dialog at 1 s, settled and `~/.claude/sessions/<pid>.json` present
  at 6 s. Off per machine until the admin ticks *New sessions*.
- **A call can trigger a speaker** (0.7.1): telerouter phoned the owner and said "Hey Jarvis" on the IP phone;
  the Raqqa laptop's mic heard it and started a turn. While any call that is not on that speaker is going on,
  the wake word no longer starts a turn there (`VoiceAssistant._other_call`; Voice setting `wake_during_calls`
  turns it back on). The wake word still answers/hangs up a call on the speaker itself.
- **Scheduled tasks** (0.8, `scheduler.py`): run state is in `/data/tasks.json`, not config.json. A
  `task_session` whose claude has exited (bare shell left in the tmux session) is killed and started again,
  but only if the task started it (0.10.1). Prompts
  carry a `scheduled task "X"` header (not "spoken request"), and a Jarvis run uses `Source("schedule", ...)`.
  MCP `_source()` returns None for it, so it counts as the agent acting on its own, and other voice requests
  to Jarvis fail fast with "busy with the scheduled task" instead of queueing behind the lock.
- **The owner's global CLAUDE.md on the agent host says to phone them when "a scheduled/cron check produced
  a result"**: a test run of a scheduled-task prompt in a scratch session phoned the owner twice (unanswered).
  That's why the task setting `calls` exists ("task" = told not to phone unless the task asks). Test
  scheduled-task prompts with `calls: "task"` wording, or on a machine without that CLAUDE.md.
- Real Claude Code 2.1.303 answered the context reminder with exactly `TELEROUTER_CLEAR` in a session with
  nothing in progress; after `/clear`, `~/.claude/sessions/<pid>.json` names the new session id at once.
- **Claude Code honours `NODE_EXTRA_CA_CERTS` for HTTP MCP servers** (2.1.303, a Bun binary): a server signed by a
  private CA is "✘ Failed to connect" without it and "✔ Connected" with it (`claude mcp get <name>` checks
  without spending tokens). Pinned CAs (`ExtraMcpServer.tls_ca`) use this. The Zigbee IR Remote app (1.3.0)
  serves MCP on :8098 with its own CA.
- **Android app** (0.9, repo telerouter-android): `talk` with `"cue": false` must install the endpointer in
  `start_turn` itself (synchronously). The audio the phone sends right after the message is handled before
  the turn's task runs, and with the old flow it went to the wake word stream and was lost. Transcripts only
  go to devices whose hello said `"transcript": true`. In 0.9.0 the setup link named the page's address
  for pinning; opened through the owner's reverse proxy (a public name), the page presented Let's Encrypt and setup
  failed ("does not match"). Since 0.9.1 the link has `lan` (`own_address`) and the app enrolls through the internet address first.
- **A phone and a speaker both woke on one "Hey Jarvis"** (0.9.2): the owner chose that the phone answers.
  `VoiceAssistant._phone_wins` cancels speaker turns their wake word started within `SAME_WAKE_S`, and `_on_wake`
  ignores speaker wake words within it after a phone's.
- **The phone app is the web UI** (0.10, app 0.2): it shows `/` in a WebView, so every web UI change reaches the
  phone. Keep pages usable at 360 px wide (the ≤760 px layout: top bar + drawer, `main > h1` hidden because the top
  bar shows the page name). In the app (`APP`, user agent `telerouter-android/`) the UI adds `telerouter-app://talk`
  and `telerouter-app://assistant` links; don't rename them without the app.
- **Behind a sign-in proxy (Authentik forward auth) `Authorization` may belong to the proxy**: phones send the device
  token in `X-Telerouter-Token`. The proxy's cookie expires (Authentik: the provider's token validity); the app
  renews it by following the proxy's redirects for `/api/health`.
- **Caddy sends the upstream's address as Host towards an https upstream**: the SSO redirect URI came out as
  `https://127.0.0.1:8443/...` behind Caddy. `callback_url` uses `X-Forwarded-Host` from trusted proxies (NPM keeps
  Host anyway).
- SSO login CSRF: the start sets a `SameSite=Lax` binding cookie (Strict would not come back with the provider's
  cross-site redirect). Verified in the Android WebView against a fake provider behind Caddy.
- Exit phrases for a tmux route must be short: "go back to the login page" is meant for the session.
- Agents search contacts by words like "camera": `list_contacts` matches the kind and its synonyms too.

- **Quiet hours** (`quiet.py`): checked in the agent call path, `on_incoming` (603) and the MCP `_quiet_gate`
  (connect_call/phone_message/start_voice_call/announce/ask_speaker unless the user asked by voice). Blocked
  results start with "quiet hours" - phone-alert relies on that prefix (exit 2, no baresip fallback). Desk
  presence = any selected HA binary sensor `on` or changed within `desk_hold_minutes` (HA REST `/api/states`,
  using the URL+token of an agent's HA extra MCP server unless `quiet.ha_url/token` are set; cached 20 s).
- `phone_message` with `wait_for_reply`: message read once (repeat defaults to 1), then only the start blip,
  then listen. Reading it twice before the beep confused the user.
- A speaker connected to a tmux session stays connected across wake words: "Hey Jarvis" + a short exit phrase
  (`machine.is_exit`: "back to Jarvis", "disconnect", "leave the session") ends it; also 15 idle min or a restart.
  A phone call on a speaker ends with just the wake word.
- **Deploying while a speaker turn is in flight cuts the spoken answer**: when the request came by voice,
  schedule the deploy (`docker compose pull && up -d` in CT 118) in a background job that starts after replying.
- Never poll the prod API with a stored password in a loop: a stale password locked the admin account. The
  lockout is in memory (`docker compose restart telerouter` clears it). Check prod via `docker logs`.

## Status (v0.10.1, 2026-10-21)

Releases: tag `vX.Y.Z` -> CI builds multi-arch `ghcr.io/revocx35/telerouter:X.Y.Z` and `:X.Y`; prod follows
`TELEROUTER_VERSION=<major.minor>` (CLAUDE.local.md). Commit as `97547472+revocx35@users.noreply.github.com`, run the
leak check (gitleaks + git grep over history) before every push.

Verified: speaker turn end to end with real Whisper/Piper/openWakeWord (WAV-driven client); agent
plumbing over SSH/tmux/hooks/reverse-tunnel MCP with the fake agent; outbound and inbound calls through
Asterisk; wa2sip (simulated WhatsApp) both directions. In production (FreePBX 17, real Claude Code with
Sonnet 5.5): "Hey Jarvis, call the echo test" -> connect_call -> *43 bridged to a speaker; an inbound call
through the PBX talking to the agent; phone_message with a transcribed two-way reply; voice connection to a
foreign tmux Claude session; Home Assistant lights via Jarvis's HA MCP server; the Raqqa speaker LXC (ALSA
backend) in daily use; wa2sip/cam2sip linked and synced (no real WhatsApp contact or camera was called).
0.6 virtual speakers: CastOutput against the real Mi Smart Speaker Lite (muted: launch, LOAD, FINISHED
timing, flush, receiver stop), the rest with a fake Cast device in tests and the UI with Playwright.
Not yet heard on the real speaker: audible quality/latency of answers through Cast.
0.8 scheduled tasks: scheduler logic with fake agents/machines in tests, the UI with Playwright, and the
session side by hand against a real Claude Code 2.1.303 (scheduled-task header, transcript reply,
context size from the transcript, the reminder answered with TELEROUTER_CLEAR, /clear, the next run in the
new conversation). Not yet run end to end in prod through the scheduler.
0.10 (phone dashboard + SSO): tests for the OIDC flow (fake provider: happy path, login CSRF, replay, foreign
tokens, not allowed, password-protected settings), text-only phones and the token header. End to end on an
emulator with the app: Caddy + a fake Authentik outpost (forward auth) + a fake OpenID provider in front of a local
telerouter: proxy login and telerouter login in the WebView, the phone adding itself, voice through the proxy,
silent renewal of an expired proxy cookie, SSO in the WebView, sign-out. Not yet against the real Authentik.
Not verified: macOS client (code paths are cross-platform, untested on a Mac); echo cancellation on real
hardware; quiet hours with real desk sensors (off in prod until the user enables them under Voice & calls ->
Quiet hours). Hermes WAS run against a real install on 2026-10-21 (Hermes 0.22.4, our own CT 123): the CLI
moved `--create-if-missing` under the `chat` subcommand, which the old driver did not handle, so
`start.sh` now probes which form the installed CLI takes (and reports a launch that dies at once).
Open idea (not confirmed by the user, don't build unasked): a distinct listening blip / announcement while a
speaker is routed to a tmux session.
