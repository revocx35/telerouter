# telerouter

**An agentic smart-speaker manager and voice router.** Say *"Hey Jarvis, call my dad"* to a speaker in
your kitchen: telerouter hears the wake word, transcribes what you said, hands it to your AI agent
(Claude Code or Hermes, running in tmux on your machine), the agent finds "dad" in your contacts and asks
telerouter, through MCP, to call him on WhatsApp, and the call is connected to the kitchen speaker.

- **Speakers**: tiny clients for **Linux and macOS** stream their microphone and play what the server
  sends. Wake word (openWakeWord), speech-to-text (Whisper) and text-to-speech (Piper) run on the server.
  A speaker is virtual: it takes its **microphone** from one device and its **output** from the same or
  another one, including **Google Cast speakers** (Nest, Mi Smart Speaker, any Chromecast built-in). Use a
  laptop's microphone with the good loudspeaker of a Cast speaker in the same room.
- **Agents**: telerouter SSHes into the machine where your agent lives, starts **Claude Code** (default
  model Sonnet 5.5, effort medium) or **Hermes** in a tmux session you can attach to, types each voice
  request into it and speaks the answer.
- **Router**: one SIP extension on your **PBX** (FreePBX/Asterisk), direct links to
  [wa2sip](https://github.com/revocx35/wa2sip) (WhatsApp calls) and
  [cam2sip](https://github.com/revocx35/cam-to-sip) (cameras). Calls can go to a speaker, to the agent
  (call telerouter from your phone and talk to your agent), or to another call.
- **MCP server**: your agent, or any other MCP client, gets tools to list contacts, call people, leave
  phone messages, talk on calls and announce things on speakers.
- **Connections**: your PBX extensions, WhatsApp contacts, cameras and **tmux sessions** (your own Claude
  Code sessions on your machines), grouped, with the names *you* use ("referred name": what the agent sees
  and what you say). *"Connect me to the wa_logger tmux"* switches your speaker or phone call to that session
  until you say *"back to Jarvis"*.

Secure by default: no default password (setup code), argon2id, lockout, optional TOTP, CSRF, strict CSP,
HTTPS everywhere with certificate pinning, tokens stored as hashes, scoped MCP tokens, hardened containers.
Details and the threat model: [ARCHITECTURE.md](ARCHITECTURE.md).

## Install the server

Needs Docker with Compose on a Linux host on your LAN (an LXC with nesting works). 4 CPU cores and 4 GB
RAM are comfortable for Whisper `small-int8`.

```bash
mkdir telerouter && cd telerouter
curl -fsSLO https://raw.githubusercontent.com/revocx35/telerouter/main/docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/revocx35/telerouter/main/.env.example -o .env
mkdir -p data whisper-data piper-data wake-models && sudo chown 1000:1000 data whisper-data piper-data
docker compose up -d
docker logs telerouter 2>&1 | grep 'setup code'
```

Open `https://<host>:8443`, accept the self-signed certificate (or use your own, see `.env`), enter the
setup code and create the admin account. The first start downloads the Whisper model and the default
voice (a few hundred MB).

Ports: `8443/tcp` (web UI, API, speakers, MCP), `5080/udp` (SIP), `18000-18199/udp` (RTP), and
`8480/tcp` only once you add a Google Cast speaker (plain HTTP: Cast speakers fetch their audio from it, see
below). The speech services listen on `127.0.0.1` only.

## Set it up

1. **Agents & machines** → add the **machine** (SSH host, user, host key, the agent type you run there:
   Claude Code or Hermes); its tmux sessions appear under Connections → tmux. Then copy telerouter's SSH public key into `~/.ssh/authorized_keys` of the user that runs your
   agent (add `from="<telerouter IP>"` in front to pin it to telerouter). That host needs `tmux` and the
   agent installed and logged in (`claude` once by hand, or `hermes setup`). *Add agent*: host, user,
   *Fetch & confirm host key*, model (`claude-sonnet-5-5`), effort (`medium`), permission mode. telerouter
   starts it in tmux; attach with the command shown on the card.
2. **Speakers** → *Devices* → *Add device*, then run the shown command on the Linux/macOS machine (as the
   desktop user). It installs the client into `~/.local/share/telerouter-client` and starts it as a user
   service, and the device gets its own speaker (its mic + its loudspeaker). Linux needs `python3-venv` and
   `libportaudio2` (or `alsa-utils` without a sound server). Check audio with `telerouter-client test-audio`.
   Optional: *Google Cast speakers* → *Find on the network* → *Add*, then *New speaker* with a device's
   microphone and the Cast speaker as output (see [Speakers from several devices](#speakers-from-several-devices)).
3. **Integrations** → your PBX extension (e.g. a new FreePBX extension for telerouter; enable *Trust
   RPID/PAI*). For WhatsApp and cameras: in wa2sip/cam2sip open *Settings → Integrations*, create a token,
   then here *Add wa2sip / cam2sip* with its HTTPS address, the token, and *Fetch & pin certificate*.
4. **Connections** → add the PBX extensions telerouter may call; WhatsApp contacts, cameras and tmux
   sessions appear by themselves, one collapsible group each. *Deselect all* and tick only what agents may
   see, give referred names ("dad", "front door"), decide what happens when a contact calls telerouter, then
   **Save changes** once.
5. **Voice & calls** → wake word (Hey Jarvis, Alexa, Okay Nabu, Hey Mycroft, Hey Rhasspy, or your own
   `.tflite` in `wake-models/`), language, voice, timeouts.

Then say *"Hey Jarvis"*: a blip means it listens, a second blip means it got you.

## Speakers from several devices

A **speaker** is what you and the agents talk to ("odam", "kitchen"). It has a **microphone** (an enrolled
device) and an **output** (an enrolled device or a Google Cast speaker). *Speakers → New speaker*: name it,
pick e.g. the laptop's microphone and the Mi Smart Speaker as output. A microphone feeds one speaker (it
moves there; the old speaker keeps its output and can still announce).

With a Cast speaker as output:
- answers, announcements and "Calling dad" play on the Cast speaker (each sentence starts in ~0.2 s; the
  first answer after 5 idle minutes ~1.5 s later, while the receiver app starts, which may chime);
- the beeps play on the microphone's device by default (instant feedback; *Beeps* → *on the output* to
  change it);
- live **calls are heard on the microphone's device**: Cast speakers buffer seconds of a live stream;
- no barge-in (the device's echo canceller can't hear the Cast speaker); the wake word pauses while it plays;
- telerouter takes the Cast speaker over when it speaks (whatever it was casting stops) and lets it go after
  5 idle minutes. If it is unreachable, the microphone's device plays instead for a minute.
- Cast speakers only fetch media over HTTPS with a public certificate, so their audio comes over **plain
  HTTP** (`TELEROUTER_CAST_PORT`, default 8480): each sentence gets a random single-use link that only that
  speaker's address may fetch, for at most 60 s. Keep it on your LAN.

## Android app (dashboard and assistant)

The [telerouter Android app](https://github.com/revocx35/telerouter-android) is telerouter on your phone:
the whole web UI as its dashboard (every page and setting, laid out for a phone), and Jarvis as the phone's
assistant instead of Google Assistant. *"Hey Jarvis"* or the assistant button/gesture opens a sheet over
whatever is on screen, and Jarvis answers as on any speaker: spoken and as text, or as text only (a setting in
the app). "Hey Jarvis" can also turn the screen on and show the conversation over the lock screen. Calls you ask
for ("call my dad") run through the phone too. The wake word is recognised on the phone (microWakeWord, as in
Home Assistant's companion app), so nothing is sent until you say it, and the phone connects only for the
conversation. Calls you ask for from the phone ring your own extension first (*Ring me first* in *Voice &
calls*), so "Hey Jarvis, call my mom" gives you an ordinary phone call with her.

Set up: install the app, enter the address you open telerouter with (it must have a publicly trusted
certificate, e.g. telerouter behind a reverse proxy), and sign in as in the browser: your proxy's login if it
has one (e.g. Authentik forward auth), then telerouter's login page or *Sign in with Authentik*. The phone then
adds itself as a speaker device; there is no code to copy. The voice connection goes to the same address, with
the same sign-in: the device token travels in `X-Telerouter-Token` (the proxy may use `Authorization` itself),
the proxy's cookie with it, and an expired proxy sign-in is renewed in the background while the identity
provider's session lasts.

Behind a reverse proxy, turn on its WebSocket support (`/ws/client`) and set `TELEROUTER_TRUSTED_PROXIES` to its
address, so throttling sees the real clients and the single sign-on redirect URI uses the name the browser
used (`X-Forwarded-Host`).

## Single sign-on (Authentik or another OpenID Connect provider)

*Account → Single sign-on* lets the people you list log in as the admin through your identity provider, with
the password login still working. In Authentik: *Applications → Providers → Create → OAuth2/OpenID Provider*
(confidential client; redirect URI as shown on the Account page, `https://<address>/api/oidc/callback`, one per
address you use), then an application for it. Copy the provider's *OpenID Configuration Issuer*
(`https://auth.example.com/application/o/<slug>/`), client ID and secret into telerouter, list the allowed
user names, e-mail addresses or groups, and save with your password. *The login page goes straight to the
provider* skips telerouter's login page (after *Log out* it shows it again). The provider's login and 2FA
replace telerouter's password and 2FA for that sign-in.

## Quiet hours (do not disturb)

*Voice & calls → Quiet hours*: pick a time range. Speakers still answer quick requests (lights, mail,
questions), but an answer that takes longer than 5 minutes (adjustable) is not spoken: the agent keeps
working silently, so asking it to build an app before going to sleep won't wake you. Calls are blocked:
incoming calls are rejected and agents can't call you or talk through a speaker on their own, unless one of
your Home Assistant desk sensors (motion/occupancy) says you're at your desk. Calls you ask for yourself still
work. Each speaker can also turn the thinking beeps off (*Speakers → Beeps while the agent works*).

## Scheduled tasks

*Scheduled tasks → New task*: instructions for an agent ("Check my mail and call me if anything is
urgent") and when to run them: at times of day (on chosen weekdays), every few minutes or hours, once, or a
cron expression, in a time zone. Each task runs in one of three places:

- **Jarvis's conversation**: typed into your assistant's ongoing conversation, so it remembers earlier runs
  and everything else you talked about. While a run is going on, voice requests to it hear that it is busy
  with the task.
- **A new session for every run**: a new tmux session with a fresh Claude Code (`<task>-MMDD-HHMM`) on a
  machine, in a folder you pick. The sessions of the last few runs stay open for you to look at.
- **Its own session**: one tmux session the task keeps between runs (`task-<name>`), started on its first
  run. Give it an **advised context size** (e.g. 300 = 300k tokens): when the context is above it after a
  run, the agent gets a gentle reminder that its context is high and that it may clear or compact it if it
  isn't working on something and doesn't need it. The agent decides. If it keeps its context, it is
  reminded again only after its context has grown another 10%.

The page keeps each run's answer, how long it took and the session it ran in, with *Run now* and *Stop run*.
Sessions use the machine's new-session model and permission mode. Nobody can answer a permission prompt
during a run, so use `auto` or another mode that doesn't ask. *Calls about a run* sets who decides whether
the agent phones you afterwards: its own rules (an agent told in its CLAUDE.md to call when a scheduled check is
done will call after every run), or only the task. Tasks run during quiet hours too, but quiet hours
still block their calls.

## Home Assistant (lights, switches, …)

Give your agent Home Assistant's own tools:

1. In Home Assistant: **Settings → Devices & services → Add integration → Model Context Protocol Server**,
   and expose the devices you want to control (**Settings → Voice assistants → Expose**).
2. Your HA profile → **Security → Long-lived access tokens → Create token** (an admin user, or turn off
   *Require an administrator account* in the MCP Server options).
3. In telerouter: **Agents → Edit → Extra MCP servers → Add Home Assistant**: URL
   `https://<your HA>/api/mcp`, the token. Save: the agent session restarts with HA's tools.

Then *"Hey Jarvis, turn off the kitchen lights"*. Any other MCP server (HTTPS + bearer token) works the same
way. Tokens are write-only in the UI; telerouter writes them only into the agent's own `mcp.json` (0600) on
the agent host.

A server with its own private certificate, like the **Zigbee IR Remote** app (room climate and IR buttons;
*Settings → AI agents* in the app makes the token and shows the certificate's fingerprint): *Add Zigbee IR
Remote*, fix the URL (`https://<HA host>:8098/mcp`), paste the token, press **Fetch & pin its certificate**,
compare the fingerprint, then save. Claude Code then trusts that certificate (only for this agent, through
`NODE_EXTRA_CA_CERTS`). Hermes agents only take servers with public certificates.

## Use telerouter from other agents (MCP)

*MCP access* → *New token* (choose scopes and which contacts/speakers it may use). For Claude Code:

```bash
mkdir -p ~/.config/telerouter
curl -fsSk --pinnedpubkey 'sha256//<pin from the UI>' https://<host>:8443/ca.pem -o ~/.config/telerouter/ca.pem
claude mcp add --transport http telerouter https://<host>:8443/mcp --header "Authorization: Bearer trm_..."
NODE_EXTRA_CA_CERTS=~/.config/telerouter/ca.pem claude
```

Tools: `list_contacts`, `list_speakers`, `connect_call`, `phone_message`, `start_voice_call`, `say`,
`listen`, `hang_up`, `list_calls`, `announce`, `ask_speaker`. Example: *"phone_message(contact='me',
message='The deploy finished')"* rings your extension and reads it to you; with `wait_for_reply` it returns
what you answered. Agents telerouter runs (Jarvis) also get `new_session`: *"start a new Claude session
called pokerbot in projects/pokerbot"* starts a fresh Claude Code (or Hermes) in a new tmux session on a
machine and connects you to it. Allow it per machine (*Agents & machines → edit → New sessions*; off by
default), with the model and permission mode new sessions get.

## Calls you ask for

*"Hey Jarvis, call my mom"* on a speaker plays the call on that speaker. *Voice & calls → Calls → Ring me
first* picks **your own extension**: a call you ask for then rings your phone first, and when you answer,
telerouter dials the contact and says "Connecting you to …" while it rings — a normal phone call, not audio
inside an app. Asking from the phone app always works this way (even with your phone in your pocket, it
rings); tick *only when I ask from the phone app* to keep home-speaker calls on the speaker, or name a
speaker ("call dad on the kitchen speaker") to keep a single call there.

## Calls into telerouter

Call telerouter's extension from your phone, or call the WhatsApp number linked in wa2sip: telerouter
looks the caller up in Contacts. With *talk to agent* you hear the greeting, a blip, and then talk to your
agent (say "goodbye" to end). Ask it to "connect me to dad" and it bridges you. Unknown callers are
rejected by default; set a DTMF PIN in *Voice & calls* if your PBX takes outside calls.

## Configuration (.env)

| Variable | Default | |
|---|---|---|
| `TELEROUTER_TLS_NAMES` | | extra names/IPs in the certificate (own IPs and hostname are automatic) |
| `TELEROUTER_TLS_CERT` / `_KEY` | | your own certificate instead |
| `TELEROUTER_HTTPS_PORT` | 8443 | |
| `TELEROUTER_SIP_PORT` | 5080 | UDP |
| `TELEROUTER_RTP_PORT_MIN/MAX` | 18000/18199 | |
| `TELEROUTER_ADVERTISE_IP` | auto | address put into SIP/SDP |
| `TELEROUTER_INTERNAL_PORT` | 47700 | loopback MCP port for agent tunnels |
| `TELEROUTER_TRUSTED_PROXIES` | none | `private` or CIDRs whose X-Forwarded-For / X-Forwarded-Host are trusted |
| `TELEROUTER_PUBLIC_URL` | | only for Android app 0.1.x setup links (0.2+ signs in at your address instead) |
| `TELEROUTER_SETUP_CODE` | random | fixed first-setup code |
| `WHISPER_MODEL` | small-int8 | tiny/base/small/medium-int8 |
| `WHISPER_PROMPT` | "Hey Jarvis. Call dad…" | names Whisper should expect, e.g. your tmux session names ("Connect me to the wa_logger tmux.") |
| `PIPER_VOICE` | en_US-lessac-medium | default voice |
| `WAKE_THRESHOLD` | 0.5 | openWakeWord sensitivity |

## Troubleshooting

- *"is not available (error)"* on a voice request: see the agent card (SSH, host key, tmux/agent missing,
  port of the MCP tunnel in use).
- *"needs you at the terminal"*: the agent asked for a permission; attach to the tmux session and answer,
  or pick a permission mode that fits voice use.
- Wake word too sensitive / deaf: `WAKE_THRESHOLD`, microphone level (Speakers page shows dBFS).
- Echo during calls: install the client with echo cancellation (default when available), or use a
  speakerphone with hardware echo cancelling.
- Headless Linux box without PulseAudio/PipeWire (e.g. a Proxmox LXC with /dev/snd passed through): the
  client uses ALSA directly (`arecord`/`aplay`, install `alsa-utils`); force it with
  `telerouter-client set --backend alsa`.
- macOS doesn't ask for the microphone: run `~/.local/share/telerouter-client/venv/bin/telerouter-client
  test-audio` in Terminal once and allow it.

## Development

See [AGENTS.md](AGENTS.md). Tests: `docker build -t telerouter-dev -f Dockerfile.dev . && docker run --rm
-v $PWD:/src -w /src telerouter-dev python -m pytest -q`.

License: MIT.
