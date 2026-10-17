#!/bin/sh
# telerouter speaker client installer (Linux and macOS). Served by the telerouter server, which fills in
# its certificate pins. Usage (the web UI shows the full command):
#   curl -fsSk --pinnedpubkey 'sha256//...' https://SERVER:8443/client/install.sh | sh -s -- \
#        --server https://SERVER:8443 --ca-sha256 HEX --code XXXX-XXXX
# Installs into ~/.local/share/telerouter-client (a Python venv) and starts a per-user service.
set -eu

PIN='@SPKI_PIN@'
CA_SHA256='@CA_SHA256@'
SERVER=""
CODE=""
SERVICE=1
AEC=1

while [ $# -gt 0 ]; do
  case "$1" in
    --server) SERVER="$2"; shift 2 ;;
    --ca-sha256) CA_SHA256="$2"; shift 2 ;;
    --code) CODE="$2"; shift 2 ;;
    --no-service) SERVICE=0; shift ;;
    --no-aec) AEC=0; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ -n "$SERVER" ] || die "--server is required"
[ -n "$CODE" ] || die "--code is required"
[ "$(id -u)" != "0" ] || echo "warning: installing for root; a speaker client normally runs as the desktop user" >&2

OS="$(uname -s)"
DIR="${XDG_DATA_HOME:-$HOME/.local/share}/telerouter-client"

PY=""
for p in python3.13 python3.12 python3.11 python3.10 python3 python3.9; do
  if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys, venv; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY="$p"; break
  fi
done
if [ -z "$PY" ]; then
  if [ "$OS" = "Darwin" ]; then die "Python 3.9+ is needed: install it with 'xcode-select --install' or from python.org / Homebrew"; fi
  die "Python 3.9+ with venv is needed (Debian/Ubuntu: sudo apt install python3 python3-venv)"
fi
if [ "$OS" = "Linux" ] && ! ldconfig -p 2>/dev/null | grep -q libportaudio; then
  echo "note: PortAudio was not found. Debian/Ubuntu: sudo apt install libportaudio2   Fedora: sudo dnf install portaudio   Arch: sudo pacman -S portaudio" >&2
fi

say "Installing the telerouter client into $DIR"
mkdir -p "$DIR"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fsSk --pinnedpubkey "$PIN" "$SERVER/client/telerouter-client.tar.gz" -o "$TMP/client.tar.gz"
tar -xzf "$TMP/client.tar.gz" -C "$TMP"
"$PY" -m venv "$DIR/venv"
"$DIR/venv/bin/python" -m pip install -q --upgrade pip
if [ "$AEC" = "1" ] && "$DIR/venv/bin/python" -m pip install -q "$TMP/telerouter-client[aec]"; then
  :
else
  [ "$AEC" = "1" ] && echo "note: echo cancellation (livekit) is not available here; installing without it" >&2
  "$DIR/venv/bin/python" -m pip install -q "$TMP/telerouter-client"
fi
BIN="$DIR/venv/bin/telerouter-client"

say "Enrolling with $SERVER"
"$BIN" enroll --server "$SERVER" --ca-sha256 "$CA_SHA256" --code "$CODE"

if [ "$SERVICE" = "0" ]; then
  say "Done. Start it with: $BIN run"
  exit 0
fi

if [ "$OS" = "Darwin" ]; then
  PLIST="$HOME/Library/LaunchAgents/io.telerouter.client.plist"
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>io.telerouter.client</string>
  <key>ProgramArguments</key><array><string>$BIN</string><string>run</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/telerouter-client.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/telerouter-client.log</string>
</dict></plist>
EOF
  launchctl bootout "gui/$(id -u)/io.telerouter.client" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  say "Started (launchd agent io.telerouter.client, log ~/Library/Logs/telerouter-client.log)."
  echo "macOS asks once for microphone access. If no prompt appears, run '$BIN test-audio' in Terminal, allow it, then: launchctl kickstart -k gui/$(id -u)/io.telerouter.client"
else
  if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/telerouter-client.service"
    mkdir -p "$(dirname "$UNIT")"
    cat > "$UNIT" <<EOF
[Unit]
Description=telerouter speaker client
After=network-online.target pipewire.service pipewire-pulse.service pulseaudio.service

[Service]
ExecStart=$BIN run
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now telerouter-client.service
    say "Started (systemd user service telerouter-client; logs: journalctl --user -u telerouter-client -f)."
    echo "To run it without anyone logged in: sudo loginctl enable-linger $(id -un)"
  else
    say "No systemd user session here. Start it yourself: $BIN run"
  fi
fi
echo "Check your audio with: $BIN test-audio   (devices: $BIN devices)"
