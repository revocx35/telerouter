#!/usr/bin/env python3
"""A tiny stand-in for the Claude Code TUI: trust dialog, prompt, hooks from --settings, MCP over HTTP."""
import json, os, subprocess, sys, urllib.request, uuid

args = sys.argv[1:]
def opt(name):
    return args[args.index(name) + 1] if name in args else None

settings = json.load(open(opt("--settings"))) if opt("--settings") else {}
mcp = json.load(open(opt("--mcp-config"))) if opt("--mcp-config") else {}
session = opt("--session-id") or opt("--resume") or str(uuid.uuid4())
model = opt("--model")

def hook(event, payload):
    for group in settings.get("hooks", {}).get(event, []):
        for h in group.get("hooks", []):
            data = json.dumps({"hook_event_name": event, "session_id": session, "cwd": os.getcwd(),
                               "transcript_path": "/tmp/none.jsonl", **payload})
            subprocess.run(h["command"], shell=True, input=data.encode())

def mcp_call(tool, arguments):
    srv = mcp["mcpServers"]["telerouter"]
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}}).encode()
    req = urllib.request.Request(srv["url"], data=body, headers={**srv["headers"], "Content-Type": "application/json",
                                                                  "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["result"]

print("Do you trust the files in this folder?\n 1. Yes, I trust this folder\n 2. No, exit", flush=True)
sys.stdin.readline()
sys.stdout.write("\033[2J\033[H")   # a TUI redraws: the dialog is gone
print(f"fake claude ({model}) ready. ? for shortcuts", flush=True)
while True:
    sys.stdout.write("> ")
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:
        break
    text = line.replace("\x1b[200~", "").replace("\x1b[201~", "").strip()
    if not text:
        continue
    hook("UserPromptSubmit", {"prompt": text})
    low = text.split("] ", 1)[-1].lower() if text.startswith("[telerouter") else text.lower()
    if "list contacts" in low:
        res = mcp_call("list_contacts", {})
        names = [c["name"] for c in json.loads(res["content"][0]["text"])["contacts"]]
        reply = "Your contacts are " + ", ".join(names) + "."
    elif "call " in low:
        words = [w.strip(".,?!") for w in low.split("call ", 1)[1].split()]
        words = [w for w in words if w not in ("my", "the", "to", "please")]
        who = words[0] if words else ""
        res = mcp_call("connect_call", {"contact": who})
        reply = ("Calling " + who + ".") if not res.get("isError") else ("I could not call " + who + ": " + res["content"][0]["text"])
    elif "permission" in low:
        hook("Notification", {"message": "Claude needs your permission to use Bash", "notification_type": "permission_prompt"})
        reply = "Done after permission."
    else:
        reply = "**Paris** is the capital of France."
    print(reply, flush=True)
    hook("Stop", {"stop_hook_active": False, "last_assistant_message": reply})
