# Integration API v1 (wa2sip, cam2sip)

What telerouter expects from a voice app it links to. wa2sip ≥ 0.3.0 and cam2sip ≥ 1.7.0 implement it.

- HTTPS only; telerouter pins the app's certificate (SHA-256 shown in its Settings → Integrations).
- `Authorization: Bearer <integration token>`; the app stores only its SHA-256 and the token reaches
  nothing but these endpoints.

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/integration/v1/info` | | `{"app", "version", "sip_port", ...}` |
| GET | `/api/integration/v1/targets` | | `{"targets": [{"id", "name", "kind": "whatsapp"\|"camera", "number"?, "account"?}]}` |
| POST | `/api/integration/v1/calls` | `{"target": id}` or `{"number", "account"}` | `{"sip_uri": "sip:<one-time user>@<ip>:<port>", "expires_in": 30}` |
| PUT | `/api/integration/v1/inbound` (wa2sip) | `{"sip_uri", "enabled"}` | `{"ok": true}` |

**Calls out**: the one-time address is single-use, valid 30 s, and only accepted from the IP that asked for
it. telerouter sends a normal SIP INVITE (G.711) to it, but to the host it called the API on, never to a
host taken from the response. wa2sip answers like the WhatsApp call: 180 while it rings, 200 on answer, 486
busy, 404 not on WhatsApp, 603 declined, 480 otherwise. cam2sip answers at once.

**Calls in** (wa2sip): when enabled, an incoming WhatsApp call from a contact that no bridge lists
explicitly is sent as an INVITE to `sip_uri` (telerouter's secret direct address), with the caller in
P-Asserted-Identity (`+<number>`), display name `WA <name>` and `X-Telerouter-Target: <account>:<number>@c.us`.
If telerouter doesn't answer, a catch-all bridge of that WhatsApp account takes the call as before.
