# telerouter

**An agentic smart-speaker manager and voice router.** Say *"Hey Jarvis, call my dad"* to a speaker in
your kitchen: telerouter hears the wake word, transcribes what you said, hands it to your AI agent
(Claude Code or Hermes, running in tmux on your machine), the agent finds "dad" in your contacts and asks
telerouter, through MCP, to call him on WhatsApp, and the call is connected to the kitchen speaker.

- **Speakers**: tiny clients for **Linux and macOS** stream their microphone and play what the server
  sends. Wake word (openWakeWord), speech-to-text (Whisper) and text-to-speech (Piper) run on the server.
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

Ports: `8443/tcp` (web UI, API, speakers, MCP), `5080/udp` (SIP), `18000-18199/udp` (RTP). The speech
services listen on `127.0.0.1` only.

## Set it up

1. **Agents & machines** → add the **machine** (SSH host, user, host key, the agent type you run there:
   Claude Code or Hermes); its tmux sessions appear under Connections → tmux. Then copy telerouter's SSH public key into `~/.ssh/authorized_keys` of the user that runs your
   agent (add `from="<telerouter IP>"` in front to pin it to telerouter). That host needs `tmux` and the
   agent installed and logged in (`claude` once by hand, or `hermes setup`). *Add agent*: host, user,
   *Fetch & confirm host key*, model (`claude-sonnet-5-5`), effort (`medium`), permission mode. telerouter
   starts it in tmux; attach with the command shown on the card.
2. **Speakers** → *Add speaker*, then run the shown command on the Linux/macOS machine (as the desktop
   user). It installs the client into `~/.local/share/telerouter-client` and starts it as a user service.
   Linux needs `python3-venv` and `libportaudio2`. Check audio with `telerouter-client test-audio`.
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
what you answered.

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
| `TELEROUTER_TRUSTED_PROXIES` | none | `private` or CIDRs whose X-Forwarded-For is trusted |
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
- macOS doesn't ask for the microphone: run `~/.local/share/telerouter-client/venv/bin/telerouter-client
  test-audio` in Terminal once and allow it.

## Development

See [AGENTS.md](AGENTS.md). Tests: `docker build -t telerouter-dev -f Dockerfile.dev . && docker run --rm
-v $PWD:/src -w /src telerouter-dev python -m pytest -q`.

License: MIT.
