"use strict";
// telerouter web UI: vanilla JS, no build step. DOM is built with el() (textContent only) - never innerHTML.

const S = { csrf: "", user: "", version: "", timer: null, page: "" };

// ---------------------------------------------------------------------------------------------- helpers
function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "value") e.value = v;
    else if (k === "checked") e.checked = !!v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat(5)) {
    if (k === null || k === undefined || k === false) continue;
    e.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return e;
}
const $ = (sel, root) => (root || document).querySelector(sel);

async function api(method, path, body) {
  const opt = { method, headers: {}, credentials: "same-origin" };
  if (method !== "GET") opt.headers["X-CSRF-Token"] = S.csrf;
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  if (r.status === 401 && !path.startsWith("/api/login") && path !== "/api/setup" && !path.startsWith("/api/account")) {
    await boot(); throw new Error("Please log in again");
  }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.blob();
  if (!r.ok) throw new Error((data && data.detail) || r.statusText);
  return data;
}

function toast(msg, bad) {
  let box = $("#toast");
  if (!box) { box = el("div", { id: "toast" }); document.body.append(box); }
  const t = el("div", { class: bad ? "bad" : "" }, msg);
  box.append(t);
  setTimeout(() => t.remove(), bad ? 7000 : 3500);
}
async function act(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg); return r; }
  catch (e) { toast(e.message, true); throw e; }
}
function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${Math.round(s)} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleString();
}
function when(ts) { return ts ? new Date(ts * 1000).toLocaleString() : ""; }
function stateDot(st) {
  const good = ["ready", "registered", "idle", "active", "connected", "speaking", "listening", "thinking", "call"];
  const warn = ["connecting", "starting", "registering", "ringing", "dialing"];
  return el("span", { class: "dot " + (good.includes(st) ? "ok" : warn.includes(st) ? "warn" : "bad") });
}
function field(label, input, hint) {
  return el("label", { class: "f" }, el("span", {}, label), input, hint ? el("div", { class: "hint" }, hint) : null);
}
function inp(name, value, attrs) { return el("input", { type: "text", name, value: value ?? "", ...(attrs || {}) }); }
function num(name, value, attrs) { return el("input", { type: "number", name, value: value ?? "", ...(attrs || {}) }); }
function pwd(name, placeholder) { return el("input", { type: "password", name, autocomplete: "new-password", placeholder: placeholder || "" }); }
function chk(name, checked, label) { return el("label", { class: "chk" }, el("input", { type: "checkbox", name, checked }), label); }
function sel(name, value, options) {
  return el("select", { name }, options.map(o => {
    const [v, t] = Array.isArray(o) ? o : [o, o];
    const opt = el("option", { value: v }, t);
    if (String(v) === String(value)) opt.selected = true;
    return opt;
  }));
}
function formData(root) {
  const out = {};
  root.querySelectorAll("input[name], select[name], textarea[name]").forEach(i => {
    if (i.type === "checkbox") out[i.name] = i.checked;
    else if (i.type === "number") out[i.name] = i.value === "" ? null : Number(i.value);
    else out[i.name] = i.value;
  });
  return out;
}
function copyBox(text) {
  const pre = el("pre", {}, text);
  const b = el("button", { class: "small", onclick: async () => {
    try { await navigator.clipboard.writeText(text); toast("Copied"); } catch { toast("Select and copy the text", true); }
  } }, "Copy");
  return el("div", { class: "copy" }, pre, b);
}
function modal(title, body, actions) {
  const bg = el("div", { class: "modal-bg" });
  const close = () => bg.remove();
  bg.append(el("div", { class: "modal" }, el("h2", {}, title), body,
    el("div", { class: "row end" }, ...(actions ? actions(close) : []), el("button", { onclick: close }, "Close"))));
  bg.addEventListener("click", e => { if (e.target === bg) close(); });
  document.body.append(bg);
  return close;
}

// ---------------------------------------------------------------------------------------------- boot / auth
async function boot() {
  clearInterval(S.timer);
  let s;
  try { s = await (await fetch("/api/session", { credentials: "same-origin" })).json(); }
  catch { $("#app").replaceChildren(el("div", { class: "boot" }, "Cannot reach telerouter.")); return; }
  S.csrf = s.csrf; S.user = s.username; S.version = s.version;
  if (s.setup_needed) return setupPage();
  if (s.totp_needed) return totpPage();
  if (!s.logged_in) return loginPage();
  shell();
}

function authCard(title, form, note) {
  $("#app").replaceChildren(el("div", { class: "auth" }, el("div", { class: "card" },
    el("h1", {}, el("img", { src: "/static/icon.svg", alt: "" }), title), note ? el("p", { class: "hint" }, note) : null, form)));
}

function setupPage() {
  const err = el("div", { class: "err" });
  const f = el("form", {},
    field("Setup code", inp("setup_code", "", { autocomplete: "off", placeholder: "XXXX-XXXX" }),
      "Printed in the server log on first start: docker logs telerouter 2>&1 | grep 'setup code'"),
    field("Admin user name", inp("username", "admin", { autocomplete: "username" })),
    field("Password (12+ characters)", pwd("password")),
    field("Repeat password", pwd("password2")), err,
    el("button", { class: "primary", type: "submit" }, "Create admin account"));
  f.addEventListener("submit", async e => {
    e.preventDefault(); err.textContent = "";
    const d = formData(f);
    if (d.password !== d.password2) { err.textContent = "The passwords differ."; return; }
    try { await api("POST", "/api/setup", { setup_code: d.setup_code, username: d.username, password: d.password }); boot(); }
    catch (x) { err.textContent = x.message; }
  });
  authCard("Welcome to telerouter", f, "Create the admin account. There is no default password.");
}

function loginPage() {
  const err = el("div", { class: "err" });
  const f = el("form", {},
    field("User name", inp("username", "", { autocomplete: "username" })),
    field("Password", el("input", { type: "password", name: "password", autocomplete: "current-password" })), err,
    el("button", { class: "primary", type: "submit" }, "Log in"));
  f.addEventListener("submit", async e => {
    e.preventDefault(); err.textContent = "";
    try { await api("POST", "/api/login", formData(f)); boot(); } catch (x) { err.textContent = x.message; }
  });
  authCard("telerouter", f);
}

function totpPage() {
  const err = el("div", { class: "err" });
  const f = el("form", {}, field("Code from your authenticator app", inp("code", "", { autocomplete: "one-time-code", inputmode: "numeric" })),
    err, el("button", { class: "primary", type: "submit" }, "Verify"));
  f.addEventListener("submit", async e => {
    e.preventDefault(); err.textContent = "";
    try { await api("POST", "/api/login/totp", formData(f)); boot(); } catch (x) { err.textContent = x.message; }
  });
  authCard("Two-factor check", f);
}

// ---------------------------------------------------------------------------------------------- shell
const PAGES = [
  ["dashboard", "Dashboard"], ["speakers", "Speakers"], ["agents", "Agents & machines"], ["connections", "Connections"],
  ["integrations", "Integrations"], ["voice", "Voice & calls"], ["mcp", "MCP access"], ["history", "History"],
  ["logs", "Logs"], ["account", "Account"],
];

function shell() {
  const nav = el("nav", { class: "side" },
    el("div", { class: "brand" }, el("img", { src: "/static/icon.svg", alt: "" }), "telerouter"),
    PAGES.map(([id, t]) => el("a", { href: "#/" + id, "data-p": id }, t)),
    el("div", { class: "foot" }, `v${S.version} · ${S.user}`));
  $("#app").replaceChildren(el("div", { class: "layout" }, nav, el("main", { id: "main" })));
  window.onhashchange = route;
  route();
}

function route() {
  clearInterval(S.timer);
  const id = (location.hash.replace(/^#\//, "") || "dashboard").split("?")[0];
  S.page = id;
  document.querySelectorAll("nav.side a").forEach(a => a.classList.toggle("active", a.dataset.p === id));
  if (S.dirty && S.dirty() && !confirm("Leave without saving your changes?")) { history.back(); return; }
  S.dirty = null;
  const fn = { dashboard, speakers, agents, connections: connectionsPage, contacts: connectionsPage, integrations, voice, mcp,
    history: historyPage, logs, account }[id] || dashboard;
  const main = $("#main");
  main.replaceChildren(el("div", { class: "muted" }, "Loading…"));
  fn(main).catch(e => main.replaceChildren(el("div", { class: "err" }, e.message)));
}
function poll(fn, ms) { S.timer = setInterval(() => fn().catch(() => {}), ms); }

// ---------------------------------------------------------------------------------------------- dashboard
async function dashboard(main) {
  const box = el("div");
  main.replaceChildren(el("h1", {}, "Dashboard"), box);
  const [agentsCfg, clientsCfg] = await Promise.all([api("GET", "/api/agents"), api("GET", "/api/clients")]);
  let speech = null;
  api("GET", "/api/speech").then(s => { speech = s; draw(); }).catch(() => {});
  let st = await api("GET", "/api/status");
  function draw() {
    const online = Object.fromEntries(st.clients.map(c => [c.id, c]));
    const speakers = el("div", { class: "card" }, el("h2", {}, "Speakers"),
      clientsCfg.clients.length ? el("table", {}, el("tr", {}, el("th", {}, "Name"), el("th", {}, "State"), el("th", { class: "hide-sm" }, "Last heard"), el("th", {}, "")),
        clientsCfg.clients.map(c => {
          const o = online[c.id];
          return el("tr", {}, el("td", {}, c.name, c.room ? el("span", { class: "muted" }, ` · ${c.room}`) : ""),
            el("td", {}, stateDot(o ? o.state : "offline"), el("span", { class: "state" }, o ? o.state : "offline"),
              o && o.aec ? el("span", { class: "chip", title: "client echo cancellation" }, " AEC") : ""),
            el("td", { class: "hide-sm muted" }, o ? (o.last_text || "") : `seen ${ago(c.last_seen)}`),
            el("td", {}, o && o.state === "idle" ? el("button", { class: "small", onclick: () => act(() => api("POST", `/api/clients/${c.id}/talk`), "Listening…") }, "Talk") : ""));
        })) : el("p", { class: "hint" }, "No speakers yet. ", el("a", { href: "#/speakers" }, "Add one")));
    const agentsBox = el("div", { class: "card" }, el("h2", {}, "Agents"),
      agentsCfg.agents.length ? agentsCfg.agents.map(a => {
        const rt = st.agents[a.id];
        const s = rt ? rt.state : (a.host_key ? "disabled" : "host key not confirmed");
        return el("div", { class: "row" }, stateDot(s), el("b", {}, a.name), el("span", { class: "muted" }, `${a.kind} on ${a.username}@${a.host}`),
          el("span", { class: "chip" }, s), rt && rt.busy ? el("span", { class: "chip warn" }, "working") : "",
          rt && rt.error ? el("span", { class: "err" }, rt.error) : "");
      }) : el("p", { class: "hint" }, "No agent yet. ", el("a", { href: "#/agents" }, "Add Claude Code or Hermes")));
    const calls = el("div", { class: "card" }, el("h2", {}, "Calls"),
      st.calls.length ? el("table", {}, st.calls.map(c => el("tr", {},
        el("td", {}, c.direction === "in" ? "← " : "→ ", c.label), el("td", {}, c.mode), el("td", {}, stateDot(c.state), c.state),
        el("td", {}, el("button", { class: "small danger", onclick: () => act(() => api("POST", `/api/calls/${c.id}/hangup`), "Hung up") }, "Hang up")))))
        : el("p", { class: "hint" }, "No calls right now."));
    const sys = el("div", { class: "card" }, el("h2", {}, "System"),
      el("div", { class: "row" }, stateDot(st.pbx.state), "PBX: ", el("b", {}, st.pbx.state), st.pbx.error ? el("span", { class: "err" }, st.pbx.error) : ""),
      speech ? el("div", { class: "row" }, ["stt", "tts", "wake"].map(k => el("span", {}, stateDot(speech.health[k] ? "ready" : "down"),
        { stt: "Whisper", tts: "Piper", wake: "openWakeWord" }[k]))) : el("div", { class: "muted" }, "checking speech services…"),
      el("div", { class: "muted" }, `up ${Math.round(st.uptime / 60)} min`));
    box.replaceChildren(el("div", { class: "grid" }, speakers, agentsBox, calls, sys));
  }
  draw();
  poll(async () => { st = await api("GET", "/api/status"); draw(); }, 2000);
}

// ---------------------------------------------------------------------------------------------- speakers
async function speakers(main) {
  const [data, agentsCfg, settings] = await Promise.all([api("GET", "/api/clients"), api("GET", "/api/agents"), api("GET", "/api/settings")]);
  let wakeModels = [];
  try { wakeModels = (await api("GET", "/api/speech")).wake_models; } catch { }
  let st = await api("GET", "/api/status");
  const list = el("div");
  function draw() {
    const online = Object.fromEntries(st.clients.map(c => [c.id, c]));
    list.replaceChildren(...data.clients.map(c => {
      const o = online[c.id];
      const f = el("div", { class: "form2" },
        field("Name (what agents see)", inp("name", c.name)), field("Room", inp("room", c.room)),
        field("Agent", sel("agent_id", c.agent_id, [["", "default agent"], ...agentsCfg.agents.map(a => [a.id, a.name])])),
        field("Wake word", sel("wake_word", (c.wake_words || [])[0] || "", [["", `default (${settings.voice.wake_words.join(", ")})`], ...wakeModels.map(m => [m.name, m.phrase])])),
        field("Volume %", num("volume", c.volume, { min: 0, max: 200 })));
      const save = async () => {
        const d = formData(f);
        const body = { name: d.name, room: d.room, agent_id: d.agent_id, volume: d.volume,
          wake_words: d.wake_word ? [d.wake_word] : [], enabled: $("input[name=enabled]", card).checked,
          visible_to_agents: $("input[name=visible_to_agents]", card).checked };
        await act(() => api("PUT", `/api/clients/${c.id}`, body), "Saved");
      };
      const card = el("div", { class: "card" },
        el("div", { class: "row" }, stateDot(o ? o.state : "offline"), el("h2", {}, c.name), el("span", { class: "chip" }, o ? o.state : "offline"),
          el("span", { class: "muted" }, c.platform || ""), c.client_version ? el("span", { class: "muted" }, `client ${c.client_version}`) : "",
          el("span", { class: "spacer" }), el("span", { class: "muted" }, o ? `connected ${ago(o.connected_at)}` : `last seen ${ago(c.last_seen)}`)),
        f, el("div", { class: "row" }, chk("enabled", c.enabled, "Enabled"), chk("visible_to_agents", c.visible_to_agents, "Agents may use it (announce / calls)")),
        o ? el("div", { class: "row muted" }, `mic ${o.mic_level} dBFS`, o.last_text ? ` · heard: "${o.last_text}"` : "") : "",
        el("div", { class: "row" },
          el("button", { class: "primary", onclick: save }, "Save"),
          el("button", { disabled: !o, onclick: () => act(() => api("POST", `/api/clients/${c.id}/talk`), "Listening…") }, "Talk (like the wake word)"),
          el("button", { disabled: !o, onclick: () => { const t = prompt("Say on " + c.name + ":"); if (t) act(() => api("POST", `/api/clients/${c.id}/say`, { text: t })); } }, "Say…"),
          el("span", { class: "spacer" }),
          el("button", { class: "danger", onclick: async () => { if (confirm(`Remove ${c.name}? Its token stops working.`)) { await act(() => api("DELETE", `/api/clients/${c.id}`), "Removed"); route(); } } }, "Remove")));
      return card;
    }));
  }
  const addForm = el("form", { class: "row" }, inp("name", "", { placeholder: "Name, e.g. Kitchen", required: true }), inp("room", "", { placeholder: "Room (optional)" }),
    el("button", { class: "primary", type: "submit" }, "Add speaker"));
  addForm.addEventListener("submit", async e => {
    e.preventDefault();
    const r = await act(() => api("POST", "/api/clients/enroll", formData(addForm)));
    addForm.reset();
    modal("Install the speaker client", el("div", {},
      el("p", {}, "On the Linux or macOS machine with the microphone and speaker, run this as the user who should own the client (not root). It installs into ~/.local/share/telerouter-client and starts it as a user service. The code works once, for 15 minutes."),
      copyBox(r.command),
      el("h3", {}, "Needs"),
      el("p", { class: "hint" }, "Python 3.9+ with venv, curl; on Linux also PortAudio (Debian/Ubuntu: sudo apt install python3-venv libportaudio2). The command pins this server's certificate, so it is safe on an untrusted network."),
      el("h3", {}, "Manual install"),
      el("p", { class: "hint" }, `Code ${r.code} · server ${r.server} · CA sha256 ${r.ca_sha256}`)));
  });
  main.replaceChildren(el("h1", {}, "Speakers"),
    el("div", { class: "card" }, el("h2", {}, "Add a speaker"),
      el("p", { class: "hint" }, "A speaker is a small client on a Linux or macOS machine: it streams its microphone here and plays what telerouter sends. Wake words, speech recognition and the agent all run on this server."),
      addForm), list);
  draw();
  poll(async () => { st = await api("GET", "/api/status"); updateStates(); }, 3000);
  function updateStates() { /* keep forms untouched while typing; a full redraw happens on navigation */ }
}

// ---------------------------------------------------------------------------------------------- agents
const EFFORTS = ["low", "medium", "high", "xhigh", "max"];
const PERMS = [["auto", "auto (classifier decides)"], ["default", "default (asks in the terminal)"], ["acceptEdits", "acceptEdits"], ["dontAsk", "dontAsk"], ["bypassPermissions", "bypassPermissions (dangerous)"], ["plan", "plan"]];

async function agents(main) {
  const [data, mdata] = await Promise.all([api("GET", "/api/agents"), api("GET", "/api/machines")]);
  data.machines = mdata.machines;
  const kindName = k => k === "hermes" ? "Hermes" : "Claude Code";
  const machinesCard = el("div", { class: "card" }, el("div", { class: "row" }, el("h2", {}, "Machines"), el("span", { class: "spacer" }),
      el("button", { onclick: () => machineForm(null, mdata) }, "Add machine")),
    el("p", { class: "hint" }, "Hosts telerouter reaches over SSH. Agents (like Jarvis) run on a machine, and the machine's other tmux sessions show up under Connections → tmux, so you can say \"connect me to the wa_logger tmux\" and talk to the agent in it. Pick the agent type you run there."),
    mdata.machines.length ? el("table", {}, mdata.machines.map(m => {
      const rt = m.runtime;
      return el("tr", {}, el("td", {}, stateDot(rt ? rt.state : (m.host_key ? "disabled" : "error")), el("b", {}, m.name)),
        el("td", { class: "mono" }, `${m.username}@${m.host}${m.port !== 22 ? ":" + m.port : ""}`),
        el("td", {}, kindName(m.agent_kind)),
        el("td", { class: "muted" }, !m.host_key ? "host key not confirmed" : rt && rt.error ? rt.error :
          m.list_tmux ? `${rt ? rt.sessions.length : 0} tmux sessions` : "tmux not listed",
          m.agents.length ? ` · runs ${m.agents.join(", ")}` : ""),
        el("td", {}, el("button", { class: "small", onclick: () => machineForm(m, mdata) }, "Edit")));
    })) : el("p", { class: "hint" }, "No machine yet."));
  main.replaceChildren(el("h1", {}, "Agents & machines"),
    el("div", { class: "card" }, el("h2", {}, "How telerouter reaches an agent"),
      el("p", { class: "hint" }, "telerouter logs in over SSH with its own key, starts the agent (Claude Code or Hermes) inside a tmux session you can attach to, types each voice request into it and reads the answer through the agent's hooks. The agent gets telerouter's MCP tools through an SSH reverse tunnel (127.0.0.1 on the agent host). Add this public key to ~/.ssh/authorized_keys of the user on each machine:"),
      copyBox(data.ssh_public_key),
      el("p", { class: "hint" }, "The host needs tmux and the agent itself installed and logged in (claude: run it once yourself; hermes: hermes setup).")),
    machinesCard,
    el("div", { class: "row" }, el("button", { class: "primary", onclick: () => mdata.machines.length ? agentForm(null, data) : toast("Add the machine first", true) }, "Add agent"),
      data.agents.length > 1 ? el("label", { class: "chk" }, "Default agent:", sel("default", data.default_agent_id, data.agents.map(a => [a.id, a.name]))) : ""),
    el("div", { id: "agentlist" }));
  const def = $("select[name=default]", main);
  if (def) def.addEventListener("change", () => act(() => api("PUT", "/api/settings", { default_agent_id: def.value }), "Default agent set"));
  const list = $("#agentlist", main);
  for (const a of data.agents) {
    const rt = data.runtime[a.id];
    const screen = el("pre", { class: "screen", hidden: true });
    const askIn = inp("ask", "", { placeholder: "Type a test request…" });
    const askOut = el("div", { class: "muted" });
    const card = el("div", { class: "card" },
      el("div", { class: "row" }, stateDot(rt ? rt.state : "stopped"), el("h2", {}, a.name), el("span", { class: "chip" }, a.kind === "claude" ? "Claude Code" : "Hermes"),
        el("span", { class: "chip" }, rt ? rt.state : (a.enabled ? "host key not confirmed" : "disabled")), cfgId(a), el("span", { class: "spacer" }),
        el("button", { onclick: () => agentForm(a, data) }, "Edit")),
      rt && rt.error ? el("p", { class: "err" }, rt.error) : null,
      (a.mcp_servers || []).length ? el("p", { class: "hint" }, "Extra MCP: ", a.mcp_servers.map(s => s.name + (s.enabled ? "" : " (off)")).join(", ")) : null,
      el("p", { class: "hint" }, `${(data.machines.find(m => m.id === a.machine_id) || { name: a.host }).name} · ${a.workdir} · tmux ${a.tmux_session || "telerouter-" + a.id}`,
        a.kind === "claude" ? ` · model ${a.model || "default"} · effort ${a.effort || "default"} · permissions ${a.permission_mode}` : ` · model ${a.model || "default"}`),
      rt ? copyBox(rt.attach) : null,
      el("div", { class: "row" },
        el("button", { disabled: !rt, onclick: () => act(() => api("POST", `/api/agents/${a.id}/restart`), "Restarting the session…") }, "Start / restart session"),
        el("button", { disabled: !rt, onclick: () => act(() => api("POST", `/api/agents/${a.id}/stop`), "Stopped") }, "Stop session"),
        a.kind === "claude" ? el("button", { disabled: !rt, onclick: () => { if (confirm("Start a fresh conversation? The old one stays in Claude Code's history.")) act(() => api("POST", `/api/agents/${a.id}/new-conversation`), "New conversation"); } }, "New conversation") : "",
        el("button", { disabled: !rt, onclick: async () => { screen.hidden = false; screen.textContent = (await act(() => api("GET", `/api/agents/${a.id}/screen`))).screen || "(empty)"; } }, "Show terminal"),
        el("span", { class: "spacer" }),
        el("button", { class: "danger", onclick: async () => { if (confirm(`Remove ${a.name}? Its tmux session is closed.`)) { await act(() => api("DELETE", `/api/agents/${a.id}`), "Removed"); route(); } } }, "Remove")),
      screen,
      el("form", { class: "row", onsubmit: async e => {
        e.preventDefault(); if (!askIn.value.trim()) return;
        askOut.textContent = "Asking…";
        try { askOut.textContent = (await api("POST", `/api/agents/${a.id}/ask`, { text: askIn.value })).reply; } catch (x) { askOut.textContent = x.message; }
      } }, askIn, el("button", { type: "submit" }, "Ask")), askOut,
      rt && rt.log.length ? el("div", {}, el("h3", {}, "Recent voice requests"), rt.log.slice().reverse().map(l =>
        el("div", { class: "hint" }, el("b", {}, l.source), `: ${l.text}`, el("br"), "→ ", l.reply === null ? "…" : l.reply))) : null);
    list.append(card);
  }
}
function serverRow(s) {
  const tok = el("input", { type: "password", "data-k": "token", autocomplete: "new-password",
    placeholder: s.token_set ? "(unchanged)" : "token (e.g. HA long-lived access token)" });
  const row = el("div", { class: "card" },
    el("div", { class: "form2" },
      field("Name (tool prefix)", el("input", { type: "text", "data-k": "name", value: s.name, placeholder: "home-assistant" })),
      field("URL", el("input", { type: "text", "data-k": "url", value: s.url, placeholder: "https://ha.example.com/api/mcp" })),
      field("Token", tok),
      field("What it is for (told to the agent)", el("input", { type: "text", "data-k": "description", value: s.description }))),
    el("div", { class: "row" },
      el("label", { class: "chk" }, el("input", { type: "checkbox", "data-k": "enabled", checked: s.enabled }), "Enabled"),
      el("label", { class: "chk" }, el("input", { type: "checkbox", "data-k": "auto_approve", checked: s.auto_approve }), "Use its tools without permission prompts"),
      el("span", { class: "spacer" }),
      el("button", { class: "small danger", onclick: () => row.remove() }, "Remove")));
  return row;
}

function cfgId(a) { return el("span", { class: "muted mono" }, a.id); }

function machineForm(m, mdata) {
  const isNew = !m;
  m = m || { name: "", host: "", port: 22, username: "root", host_key: "", enabled: true, agent_kind: "claude", list_tmux: true, reply_timeout: 600 };
  let hostKey = m.host_key;
  const fp = el("div", { class: "hint" }, m.host_key ? `Host key confirmed: ${m.host_key_fingerprint}` : "Host key not confirmed yet.");
  const f = el("div", {},
    el("div", { class: "form2" },
      field("Name", inp("name", m.name, { placeholder: "e.g. claude LXC" })),
      field("SSH host", inp("host", m.host, { placeholder: "192.168.1.20" })),
      field("SSH port", num("port", m.port)),
      field("SSH user", inp("username", m.username)),
      field("Agent type in its tmux sessions", sel("agent_kind", m.agent_kind, [["claude", "Claude Code"], ["hermes", "Hermes Agent"]])),
      field("Wait for a session's answer (s)", num("reply_timeout", m.reply_timeout))),
    el("div", { class: "row" }, chk("enabled", m.enabled, "Enabled"), chk("list_tmux", m.list_tmux, "List its tmux sessions under Connections")),
    el("div", { class: "row" }, el("button", { onclick: async () => {
      const d = formData(f);
      const r = await act(() => api("POST", "/api/agents/hostkey", { host: d.host, port: d.port }));
      if (confirm(`Trust this SSH host key for ${d.host}?\n\n${r.fingerprint}\n\nCompare it with: ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub (on that host)`)) {
        hostKey = r.host_key; fp.textContent = `Host key confirmed: ${r.fingerprint} (saved when you press Save)`;
      }
    } }, "Fetch & confirm host key"), fp));
  modal(isNew ? "Add machine" : `Edit ${m.name}`, f, close => [
    isNew ? null : el("button", { class: "danger", onclick: async () => { if (confirm(`Remove ${m.name}?`)) { await act(() => api("DELETE", `/api/machines/${m.id}`), "Removed"); close(); route(); } } }, "Remove"),
    el("button", { class: "primary", onclick: async () => {
      const d = formData(f);
      d.host_key = hostKey;
      if (!d.host_key) { toast("Confirm the host key first", true); return; }
      await act(() => isNew ? api("POST", "/api/machines", d) : api("PUT", `/api/machines/${m.id}`, d), "Saved");
      close(); route();
    } }, "Save")].filter(Boolean));
}

function agentForm(a, data) {
  const isNew = !a;
  a = a || { name: "Jarvis", kind: (data.machines[0] || {}).agent_kind || "claude", machine_id: (data.machines[0] || {}).id || "", workdir: "~",
    tmux_session: "", model: "claude-sonnet-5-5", effort: "medium", permission_mode: "auto", hermes_provider: "", extra_instructions: "",
    mcp_remote_port: 47801, reply_timeout: 300, enabled: true, autostart: true };
  const servers = el("div", {}, (a.mcp_servers || []).map(serverRow));
  const f = el("div", {},
    el("div", { class: "form2" },
      field("Name (how it introduces itself)", inp("name", a.name)),
      field("Agent", sel("kind", a.kind, [["claude", "Claude Code"], ["hermes", "Hermes Agent"]])),
      field("Machine", sel("machine_id", a.machine_id, data.machines.map(m => [m.id, `${m.name} (${m.username}@${m.host})`]))),
      field("Working directory", inp("workdir", a.workdir)),
      field("Model", inp("model", a.model), "Claude Code: e.g. claude-sonnet-5-5, sonnet, opus. Hermes: provider model name."),
      field("Effort (Claude Code)", sel("effort", a.effort, [["", "default"], ...EFFORTS])),
      field("Permission mode", sel("permission_mode", a.permission_mode, PERMS), "Voice requests can't answer permission prompts: you'll hear 'needs you at the terminal'."),
      field("Hermes provider (optional)", inp("hermes_provider", a.hermes_provider)),
      field("tmux session name", inp("tmux_session", a.tmux_session, { placeholder: "telerouter-<id>" })),
      field("MCP tunnel port on the agent host", num("mcp_remote_port", a.mcp_remote_port)),
      field("Answer timeout (s)", num("reply_timeout", a.reply_timeout))),
    field("Extra instructions for voice requests", el("textarea", { name: "extra_instructions" }, a.extra_instructions)),
    el("h3", {}, "Extra MCP servers"),
    el("p", { class: "hint" }, "Other tools the agent gets, e.g. Home Assistant (Settings → Devices & services → add \"Model Context Protocol Server\", then a long-lived access token from your HA profile). HTTPS only; the token is sent as a Bearer header and is never shown again."),
    servers,
    el("div", { class: "row" },
      el("button", { onclick: () => servers.append(serverRow({ name: "home-assistant", url: "", description: "Home Assistant: lights, switches, covers, climate, media and sensors", enabled: true, auto_approve: true })) }, "Add Home Assistant"),
      el("button", { onclick: () => servers.append(serverRow({ name: "", url: "", description: "", enabled: true, auto_approve: true })) }, "Add other MCP server")),
    el("div", { class: "row" }, chk("enabled", a.enabled, "Enabled"), chk("autostart", a.autostart, "Start the tmux session automatically")));
  modal(isNew ? "Add agent" : `Edit ${a.name}`, f, close => [el("button", { class: "primary", onclick: async () => {
    const d = formData(f);
    for (const k of Object.keys(d)) if (k.startsWith("srv_")) delete d[k];
    d.mcp_servers = [...servers.children].map(r => ({
      name: $("[data-k=name]", r).value.trim(), url: $("[data-k=url]", r).value.trim(), token: $("[data-k=token]", r).value,
      description: $("[data-k=description]", r).value, enabled: $("[data-k=enabled]", r).checked,
      auto_approve: $("[data-k=auto_approve]", r).checked })).filter(s => s.name || s.url);
    await act(() => isNew ? api("POST", "/api/agents", d) : api("PUT", `/api/agents/${a.id}`, d), "Saved");
    close(); route();
  } }, "Save")]);
}

// ---------------------------------------------------------------------------------------------- connections
const GROUPS = [["pbx", "Phone extensions", "PBX"], ["whatsapp", "WhatsApp", "wa2sip"], ["camera", "Cameras", "cam2sip"],
  ["tmux", "tmux sessions", "machines"]];
S.open = S.open || new Set();

async function connectionsPage(main) {
  const [data, clientsCfg, agentsCfg] = await Promise.all([api("GET", "/api/contacts"), api("GET", "/api/clients"), api("GET", "/api/agents")]);
  const drafts = new Map();              // id -> changed fields
  const byId = new Map(data.contacts.map(c => [c.id, c]));
  const filter = inp("q", "", { placeholder: "Filter all connections…" });
  const groupsBox = el("div");
  const counter = el("span");
  const saveBtn = el("button", { class: "primary" }, "Save changes");
  const discardBtn = el("button", {}, "Discard");
  const bar = el("div", { class: "savebar hidden" }, counter, el("span", { class: "spacer" }), discardBtn, saveBtn);
  S.dirty = () => drafts.size > 0;
  const value = (c, k) => drafts.has(c.id) && k in drafts.get(c.id) ? drafts.get(c.id)[k] : c[k];
  function change(c, k, v) {
    const d = drafts.get(c.id) || {};
    if (v === c[k]) delete d[k]; else d[k] = v;
    if (Object.keys(d).length) drafts.set(c.id, d); else drafts.delete(c.id);
    bar.classList.toggle("hidden", !drafts.size);
    counter.textContent = `${drafts.size} connection${drafts.size === 1 ? "" : "s"} changed`;
  }
  const matches = c => { const q = filter.value.toLowerCase(); return !q || (c.alias + " " + c.name + " " + c.number + " " + c.description).toLowerCase().includes(q); };

  function row(c) {
    const text = (k, ph) => { const i = inp(k, value(c, k), { placeholder: ph }); i.addEventListener("input", () => change(c, k, i.value)); return i; };
    const vis = el("input", { type: "checkbox", checked: value(c, "enabled"), title: "visible to agents" });
    vis.addEventListener("change", () => change(c, "enabled", vis.checked));
    const canCall = !c.missing && clientsCfg.clients.length;
    return el("tr", { class: c.missing ? "gone" : "" },
      el("td", {}, vis),
      el("td", {}, el("div", {}, c.name), el("div", { class: "muted" }, c.source_name + (c.missing ? " · gone" : ""))),
      el("td", {}, text("alias", c.name)),
      el("td", { class: "hide-sm" }, text("description", c.kind === "tmux" ? "what is this session for?" : "e.g. my father")),
      el("td", { class: "hide-sm mono" }, c.number || ""),
      el("td", {}, ["pbx", "whatsapp"].includes(c.kind) ? inboundPicker(c, clientsCfg, agentsCfg, value, change) : ""),
      el("td", {}, el("div", { class: "row" },
        canCall ? el("button", { class: "small", onclick: () => callDialog(c, clientsCfg) }, c.kind === "tmux" ? "Connect" : "Call") : "",
        c.source === "manual" || c.missing ? el("button", { class: "small danger", onclick: async () => {
          if (confirm(`Delete ${c.alias || c.name}?`)) { await act(() => api("DELETE", `/api/contacts/${c.id}`)); drafts.delete(c.id); route(); } } }, "Delete") : "")));
  }

  function draw() {
    groupsBox.replaceChildren(...GROUPS.map(([kind, title, src]) => {
      const all = data.contacts.filter(c => c.kind === kind);
      const shown = all.filter(matches);
      const visible = all.filter(c => value(c, "enabled") && !c.missing).length;
      const open = S.open.has(kind) || (filter.value && shown.length > 0);
      const setAll = on => { for (const c of shown) change(c, "enabled", on); draw(); };
      const head = el("div", { class: "row group-head" },
        el("button", { class: "small", onclick: () => { S.open.has(kind) ? S.open.delete(kind) : S.open.add(kind); draw(); } }, open ? "▾ Collapse" : "▸ Expand"),
        el("h2", {}, title), el("span", { class: "chip" }, `${all.length}`), el("span", { class: "muted" }, `${visible} visible to agents · from ${src}`),
        el("span", { class: "spacer" }),
        el("button", { class: "small", disabled: !shown.length, onclick: () => setAll(true) }, "Select all"),
        el("button", { class: "small", disabled: !shown.length, onclick: () => setAll(false) }, "Deselect all"));
      const body = open ? (shown.length ? el("div", { class: "table-wrap" }, el("table", {},
        el("thead", {}, el("tr", {}, ["Visible", "Name", "Referred name", "Notes for the agent", "Number", "When they call", ""].map((t, i) =>
          el("th", { class: [3, 4].includes(i) ? "hide-sm" : "" }, t)))),
        el("tbody", {}, shown.map(row)))) : el("p", { class: "hint" }, all.length ? "Nothing matches the filter." : emptyHint(kind))) : null;
      return el("div", { class: "card" }, head, kind === "pbx" && open ? addExtension() : null, body);
    }));
  }
  function emptyHint(kind) {
    return { pbx: "Add the extensions telerouter may call.", whatsapp: "Link wa2sip under Integrations.",
      camera: "Link cam2sip under Integrations.", tmux: "Add a machine under Agents & machines; its tmux sessions appear here." }[kind];
  }
  function addExtension() {
    const add = el("form", { class: "row" }, inp("name", "", { placeholder: "Name", required: true }), inp("number", "", { placeholder: "Extension, e.g. 1005", required: true }),
      inp("alias", "", { placeholder: "Referred name (optional)" }), el("button", { type: "submit" }, "Add extension"));
    add.addEventListener("submit", async e => { e.preventDefault(); await act(() => api("POST", "/api/contacts", formData(add)), "Added"); route(); });
    return add;
  }
  saveBtn.addEventListener("click", async () => {
    const changes = [...drafts.entries()].map(([id, d]) => ({ id, ...d }));
    const r = await act(() => api("PUT", "/api/contacts", { changes }), "Saved");
    for (const ch of changes) Object.assign(byId.get(ch.id), ch);
    drafts.clear(); bar.classList.add("hidden"); draw();
    return r;
  });
  discardBtn.addEventListener("click", () => { drafts.clear(); bar.classList.add("hidden"); draw(); });
  filter.addEventListener("input", draw);
  main.replaceChildren(el("h1", {}, "Connections"),
    el("p", { class: "hint" }, "Everything telerouter can connect you to. The referred name is what the agent sees and what you say (\"call dad\", \"connect me to the wa_logger tmux\"). Only ticked connections are visible to agents. Changes are saved together with the button at the bottom."),
    el("div", { class: "row" }, filter), groupsBox, bar);
  draw();
}

function inboundPicker(c, clientsCfg, agentsCfg, value, change) {
  const policy = sel("inbound", value(c, "inbound"), [["default", "default"], ["agent", "talk to agent"], ["client", "ring speaker"], ["reject", "reject"]]);
  const speaker = sel("inbound_client", value(c, "inbound_client"), [["", "which speaker?"], ...clientsCfg.clients.map(x => [x.id, x.name])]);
  const agent = agentsCfg.agents.length > 1 ? sel("inbound_agent", value(c, "inbound_agent"), [["", "default agent"], ...agentsCfg.agents.map(x => [x.id, x.name])]) : null;
  const sync = () => { speaker.classList.toggle("hidden", policy.value !== "client"); if (agent) agent.classList.toggle("hidden", policy.value !== "agent"); };
  policy.addEventListener("change", () => { change(c, "inbound", policy.value); sync(); });
  speaker.addEventListener("change", () => change(c, "inbound_client", speaker.value));
  if (agent) agent.addEventListener("change", () => change(c, "inbound_agent", agent.value));
  sync();
  return el("div", {}, policy, speaker, agent);
}

function callDialog(c, clientsCfg) {
  const s = sel("client", "", clientsCfg.clients.map(x => [x.id, x.name]));
  const tmux = c.kind === "tmux";
  modal(`${tmux ? "Connect to" : "Call"} ${c.alias || c.name}`, el("div", {}, field("Speaker", s),
    tmux ? el("p", { class: "hint" }, "The speaker then talks to this session (after the wake word) until you say \"back to Jarvis\".") : null),
    close => [el("button", { class: "primary", onclick: async () => {
      await act(() => api("POST", `/api/contacts/${c.id}/call`, { client_id: s.value }), tmux ? "Connected" : "Calling…"); close();
    } }, tmux ? "Connect" : "Call")]);
}

// ---------------------------------------------------------------------------------------------- integrations
async function integrations(main) {
  const [pbx, integ, st, settings] = await Promise.all([api("GET", "/api/pbx"), api("GET", "/api/integrations"), api("GET", "/api/status"), api("GET", "/api/settings")]);
  const pf = el("div", { class: "form2" },
    field("PBX host", inp("host", pbx.host, { placeholder: "192.168.1.10" })), field("SIP port (UDP)", num("port", pbx.port)),
    field("Extension", inp("username", pbx.username)), field("Password", pwd("password", pbx.password_set ? "(unchanged)" : "")),
    field("Auth user (if different)", inp("auth_username", pbx.auth_username)), field("Domain (if different)", inp("domain", pbx.domain)),
    field("Caller name", inp("display_name", pbx.display_name)), field("Registration expiry (s)", num("expires", pbx.expires)));
  const pbxCard = el("div", { class: "card" },
    el("div", { class: "row" }, el("h2", {}, "PBX account"), stateDot(st.pbx.state), el("span", { class: "chip" }, st.pbx.state), st.pbx.error ? el("span", { class: "err" }, st.pbx.error) : ""),
    el("p", { class: "hint" }, `telerouter registers one extension (SIP over UDP, local port ${settings.sip_port}) to call extensions and to take calls. On FreePBX enable "Trust RPID/PAI" for nicer caller IDs. Incoming calls are accepted only from the PBX address.`),
    pf, chk("enabled", pbx.enabled, "Enabled"),
    el("div", { class: "row" }, el("button", { class: "primary", onclick: async () => {
      const d = formData(pbxCard); await act(() => api("PUT", "/api/pbx", d), "Saved"); setTimeout(route, 1500);
    } }, "Save"), el("button", { onclick: () => act(() => api("POST", "/api/pbx/register"), "Registering…") }, "Register now")));

  const list = el("div", {}, integ.integrations.map(i => el("div", { class: "card" },
    el("div", { class: "row" }, stateDot(i.last_error ? "error" : i.last_sync ? "ready" : "connecting"), el("h2", {}, i.name || i.kind), el("span", { class: "chip" }, i.kind),
      el("span", { class: "muted mono" }, i.url), el("span", { class: "spacer" }), el("span", { class: "muted" }, `synced ${ago(i.last_sync)}`)),
    i.last_error ? el("p", { class: "err" }, i.last_error) : null,
    el("p", { class: "hint" }, i.tls_fingerprint ? `Pinned certificate sha256 ${i.tls_fingerprint}` : "Certificate checked against public CAs."),
    el("div", { class: "row" },
      el("button", { onclick: async () => { const r = await act(() => api("POST", `/api/integrations/${i.id}/sync`)); toast(`${r.targets} targets, ${r.added} new`); route(); } }, "Sync now"),
      i.kind === "wa2sip" ? chk("inbound", i.inbound, "Ring telerouter for incoming WhatsApp calls") : "",
      el("button", { onclick: () => integrationForm(i) }, "Edit"),
      el("span", { class: "spacer" }),
      el("button", { class: "danger", onclick: async () => { if (confirm("Remove this connection? Its contacts are marked as gone.")) { await act(() => api("DELETE", `/api/integrations/${i.id}`)); route(); } } }, "Remove")))));
  list.querySelectorAll("input[name=inbound]").forEach((cb, n) => cb.addEventListener("change", async () => {
    const i = integ.integrations.filter(x => x.kind === "wa2sip")[n];
    await act(() => api("PUT", `/api/integrations/${i.id}`, { inbound: cb.checked }), "Saved");
  }));
  main.replaceChildren(el("h1", {}, "Integrations"), pbxCard,
    el("div", { class: "card" }, el("h2", {}, "wa2sip and cam2sip"),
      el("p", { class: "hint" }, "Link your wa2sip (WhatsApp calls) and cam2sip (camera intercom) instances. In their web UI open Settings → Integrations, create a telerouter token and paste it here. telerouter then lists their WhatsApp contacts and cameras, calls them directly over SIP (no PBX needed), and wa2sip can ring telerouter for incoming WhatsApp calls."),
      el("button", { class: "primary", onclick: () => integrationForm(null) }, "Add wa2sip / cam2sip")), list);
}

function integrationForm(i) {
  const isNew = !i;
  i = i || { kind: "wa2sip", name: "", url: "https://", token: "", tls_fingerprint: "", tls_cert: "", enabled: true, inbound: true };
  let cert = { pem: i.tls_cert, fingerprint: i.tls_fingerprint };
  const fp = el("div", { class: "hint" }, i.tls_fingerprint ? `Pinned: ${i.tls_fingerprint}` : "Not pinned: fetch and confirm the certificate (self-signed), or leave unpinned for a public certificate.");
  const f = el("div", {},
    el("div", { class: "form2" },
      field("Kind", isNew ? sel("kind", i.kind, [["wa2sip", "wa2sip (WhatsApp)"], ["cam2sip", "cam2sip (cameras)"]]) : el("div", {}, i.kind)),
      field("Name", inp("name", i.name, { placeholder: "e.g. Home WhatsApp" })),
      field("URL (https)", inp("url", i.url, { placeholder: "https://192.168.1.32:8493" })),
      field("Integration token", pwd("token", i.token_set ? "(unchanged)" : "from its Settings → Integrations"))),
    el("div", { class: "row" }, chk("enabled", i.enabled, "Enabled"),
      el("button", { onclick: async () => {
        const url = $("input[name=url]", f).value;
        const r = await act(() => api("POST", "/api/integrations/certificate", { url }));
        if (confirm(`Pin this certificate of ${url}?\n\nsha256 ${r.fingerprint}\n\nCompare it with the fingerprint shown in that app.`)) {
          cert = r; fp.textContent = `Pinned: ${r.fingerprint} (saved when you press Save)`;
        }
      } }, "Fetch & pin certificate")), fp);
  modal(isNew ? "Add connection" : `Edit ${i.name || i.kind}`, f, close => [el("button", { class: "primary", onclick: async () => {
    const d = formData(f);
    d.tls_cert = cert.pem || ""; d.tls_fingerprint = cert.fingerprint || "";
    if (!isNew) delete d.kind;
    await act(() => isNew ? api("POST", "/api/integrations", d) : api("PUT", `/api/integrations/${i.id}`, d), "Saved");
    close(); route();
  } }, "Save")]);
}

// ---------------------------------------------------------------------------------------------- voice & calls
async function voice(main) {
  const [settings, clientsCfg] = await Promise.all([api("GET", "/api/settings"), api("GET", "/api/clients")]);
  let sp = { health: {}, wake_models: [], voices: [] };
  try { sp = await api("GET", "/api/speech"); } catch { }
  const v = settings.voice, c = settings.calls;
  const wake = el("div", {}, (sp.wake_models.length ? sp.wake_models : v.wake_words.map(n => ({ name: n, phrase: n }))).map(m =>
    chk("ww_" + m.name, v.wake_words.includes(m.name), m.phrase)));
  const voices = sp.voices.length ? sel("tts_voice", v.tts_voice, sp.voices.map(x => [x.name, `${x.name}${x.installed ? "" : " (downloads on first use)"}`])) : inp("tts_voice", v.tts_voice);
  const vf = el("div", {},
    el("h3", {}, "Wake word"), wake, el("p", { class: "hint" }, "openWakeWord models. Custom .tflite models: put them in the wake-models volume and restart the openwakeword container."),
    el("div", { class: "form2" },
      field("Speech language", inp("language", v.language), "Whisper language code (en, tr, de…); empty = detect"),
      field("Voice", voices),
      field("Blip volume %", num("beep_volume", v.beep_volume, { min: 0, max: 100 })),
      field("Wait for speech after the blip (s)", num("listen_timeout", v.listen_timeout, { step: 0.5 })),
      field("Pause that ends a request (ms)", num("end_silence_ms", v.end_silence_ms, { step: 100 }), "Speakers. If more speech follows within 3 s, or the sentence sounds unfinished, it is joined."),
      field("…on phone calls (ms)", num("phone_end_silence_ms", v.phone_end_silence_ms, { step: 100 })),
      field("Longest request (s)", num("max_utterance", v.max_utterance)),
      field("Speech detection threshold", num("vad_threshold", v.vad_threshold, { step: 0.05, min: 0.1, max: 0.95 }))),
    el("p", { class: "hint" }, "Names Whisper often mishears can be listed in WHISPER_PROMPT in the server's .env (restart the whisper container)."),
    el("div", { class: "row" }, chk("follow_up", v.follow_up, "Keep listening after an answer (no wake word for a follow-up)"), chk("thinking_cue", v.thinking_cue, "Soft tick while the agent works")),
    el("div", { class: "row" }, el("button", { class: "primary", onclick: async () => {
      const d = formData(vf);
      const ww = Object.keys(d).filter(k => k.startsWith("ww_") && d[k]).map(k => k.slice(3));
      const body = { wake_words: ww }; for (const k of ["language", "tts_voice", "beep_volume", "listen_timeout", "end_silence_ms", "phone_end_silence_ms", "max_utterance", "vad_threshold", "follow_up", "thinking_cue"]) body[k] = d[k];
      await act(() => api("PUT", "/api/settings", { voice: body }), "Saved");
    } }, "Save"), el("button", { onclick: async () => {
      const blob = await act(() => api("POST", "/api/speech/preview", { text: "Hello! This is how I sound." }));
      new Audio(URL.createObjectURL(blob)).play();
    } }, "Preview voice (saved setting)")));
  const cf = el("div", {},
    el("div", { class: "form2" },
      field("Ring timeout (s)", num("ring_timeout", c.ring_timeout)),
      field("Known contacts calling in (policy 'default')", sel("inbound_default", c.inbound_default, [["reject", "reject"], ["agent", "talk to the agent"], ["client", "ring a speaker"]])),
      field("Speaker for 'ring a speaker'", sel("inbound_default_client", c.inbound_default_client, [["", "—"], ...clientsCfg.clients.map(x => [x.id, x.name])])),
      field("Unknown callers", sel("unknown_callers", c.unknown_callers, [["reject", "reject"], ["agent", "talk to the agent (not recommended)"]])),
      field("Agent greeting", inp("agent_greeting", c.agent_greeting)),
      field("PIN before talking to the agent (DTMF)", inp("agent_call_pin", c.agent_call_pin, { placeholder: "none", inputmode: "numeric" })),
      field("Longest call (min)", num("max_call_minutes", c.max_call_minutes))),
    chk("announce_calls", c.announce_calls, "Say \"Calling …\" on the speaker before ringing"),
    el("div", {}, el("button", { class: "primary", onclick: async () => { await act(() => api("PUT", "/api/settings", { calls: formData(cf) }), "Saved"); } }, "Save")));
  main.replaceChildren(el("h1", {}, "Voice & calls"),
    el("div", { class: "card" }, el("h2", {}, "Voice"), el("div", { class: "row" }, ["stt", "tts", "wake"].map(k => el("span", {}, stateDot(sp.health[k] ? "ready" : "down"), { stt: "Whisper", tts: "Piper", wake: "openWakeWord" }[k]))), vf),
    el("div", { class: "card" }, el("h2", {}, "Calls"), el("p", { class: "hint" }, "Who may reach your agent by phone: set it per contact on the Contacts page. Caller IDs can be spoofed on some networks, so use a PIN if the PBX is reachable from outside."), cf));
}

// ---------------------------------------------------------------------------------------------- MCP
async function mcp(main) {
  const [data, contactsD, clientsD] = await Promise.all([api("GET", "/api/mcp-tokens"), api("GET", "/api/contacts"), api("GET", "/api/clients")]);
  const SC = [["contacts", "list contacts"], ["calls", "make calls"], ["speak", "talk on speakers"], ["clients", "list speakers"]];
  const f = el("form", {}, el("div", { class: "form2" }, field("Name", inp("name", "", { placeholder: "e.g. claude on my laptop", required: true }))),
    el("div", {}, SC.map(([k, t]) => chk("s_" + k, true, t))),
    field("Contacts it may use", sel("contacts_mode", "*", [["*", "all visible contacts"], ["pick", "only these…"]])),
    el("div", { class: "row", id: "pick" }, contactsD.contacts.filter(c => c.enabled).map(c => chk("c_" + c.id, false, c.alias || c.name))),
    field("Speakers it may use", sel("clients_mode", "*", [["*", "all"], ["pick", "only these…"]])),
    el("div", { class: "row" }, clientsD.clients.map(c => chk("k_" + c.id, false, c.name))),
    el("button", { class: "primary", type: "submit" }, "Create token"));
  f.addEventListener("submit", async e => {
    e.preventDefault();
    const d = formData(f);
    const body = { name: d.name, scopes: SC.map(s => s[0]).filter(k => d["s_" + k]),
      contacts: d.contacts_mode === "*" ? ["*"] : Object.keys(d).filter(k => k.startsWith("c_") && d[k]).map(k => k.slice(2)),
      clients: d.clients_mode === "*" ? ["*"] : Object.keys(d).filter(k => k.startsWith("k_") && d[k]).map(k => k.slice(2)) };
    const r = await act(() => api("POST", "/api/mcp-tokens", body));
    const caFile = "~/.config/telerouter/ca.pem";
    modal("Token created", el("div", {}, el("p", {}, "Copy it now: it is shown only once."), copyBox(r.token),
      el("h3", {}, "Claude Code"),
      copyBox(`mkdir -p ~/.config/telerouter && curl -fsSk --pinnedpubkey '${data.spki_pin}' ${location.origin}/ca.pem -o ${caFile}\n` +
        `claude mcp add --transport http telerouter ${data.url} --header "Authorization: Bearer ${r.token}"\n` +
        `# start claude with: NODE_EXTRA_CA_CERTS=${caFile} claude`),
      el("h3", {}, "Any MCP client (Streamable HTTP)"),
      copyBox(JSON.stringify({ mcpServers: { telerouter: { type: "http", url: data.url, headers: { Authorization: `Bearer ${r.token}` } } } }, null, 1)),
      el("p", { class: "hint" }, "telerouter's certificate is self-signed: trust its CA (/ca.pem, sha256 shown above) or put telerouter behind a reverse proxy with a public certificate.")));
    route();
  });
  main.replaceChildren(el("h1", {}, "MCP access"),
    el("div", { class: "card" }, el("h2", {}, "telerouter as an MCP server"),
      el("p", { class: "hint" }, `Any agent can use telerouter's tools (contacts, calls, speakers) at ${data.url} with a token. Agents that telerouter runs itself get their own token automatically.`)),
    el("div", { class: "card" }, el("h2", {}, "Tokens"),
      el("table", {}, el("tr", {}, ["Name", "Scopes", "Contacts", "Speakers", "Last used", ""].map(t => el("th", {}, t))),
        data.tokens.map(t => el("tr", {}, el("td", {}, t.name, t.agent_id ? el("span", { class: "chip" }, "agent") : ""), el("td", {}, t.scopes.join(", ")),
          el("td", {}, t.contacts.includes("*") ? "all" : `${t.contacts.length}`), el("td", {}, t.clients.includes("*") ? "all" : `${t.clients.length}`),
          el("td", {}, ago(t.last_used)),
          el("td", {}, t.agent_id ? "" : el("button", { class: "small danger", onclick: async () => { if (confirm(`Revoke ${t.name}?`)) { await act(() => api("DELETE", `/api/mcp-tokens/${t.id}`), "Revoked"); route(); } } }, "Revoke")))))),
    el("div", { class: "card" }, el("h2", {}, "New token"), f));
}

// ---------------------------------------------------------------------------------------------- history / logs / account
async function historyPage(main) {
  const d = await api("GET", "/api/history");
  main.replaceChildren(el("h1", {}, "History"), el("div", { class: "card" },
    d.history.length ? el("table", {}, el("tr", {}, ["When", "Call", "Mode", "Result", "Duration"].map(t => el("th", {}, t))),
      d.history.map(h => el("tr", {}, el("td", {}, when(h.started || h.at)), el("td", {}, h.direction === "in" ? "← " : "→ ", h.label || ""),
        el("td", {}, h.mode || ""), el("td", {}, h.result || ""), el("td", {}, h.duration ? `${h.duration} s` : ""))))
      : el("p", { class: "hint" }, "Nothing yet.")));
}

async function logs(main) {
  const box = el("div", { class: "logs" });
  let after = 0;
  main.replaceChildren(el("h1", {}, "Logs"), el("div", { class: "card" }, box));
  async function more() {
    const d = await api("GET", `/api/logs?after=${after}`);
    const atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
    for (const r of d.records) {
      after = r.seq;
      box.append(el("div", { class: r.level }, `${new Date(r.ts * 1000).toLocaleTimeString()} ${r.level.padEnd(7)} ${r.logger}: ${r.msg}`));
    }
    while (box.childElementCount > 2000) box.firstChild.remove();
    if (atEnd) box.scrollTop = box.scrollHeight;
  }
  await more();
  poll(more, 2000);
}

async function account(main) {
  const acc = await api("GET", "/api/account");
  const pf = el("form", {}, el("div", { class: "form2" },
    field("Current password", el("input", { type: "password", name: "current", autocomplete: "current-password" })),
    field("New password (12+ characters)", pwd("new"))), el("button", { class: "primary", type: "submit" }, "Change password"));
  pf.addEventListener("submit", async e => { e.preventDefault(); await act(() => api("POST", "/api/account/password", formData(pf)), "Password changed; other sessions were logged out"); pf.reset(); boot(); });
  const tf = el("div");
  if (acc.totp_enabled) {
    const p = el("input", { type: "password", name: "password", autocomplete: "current-password" });
    tf.append(el("p", { class: "okc" }, "Two-factor login is on."), field("Password to turn it off", p),
      el("button", { class: "danger", onclick: async () => { await act(() => api("POST", "/api/account/totp/disable", { username: acc.username, password: p.value }), "2FA off"); route(); } }, "Turn off"));
  } else {
    tf.append(el("p", { class: "hint" }, "Protect the admin login with a code from an authenticator app (TOTP)."),
      el("button", { class: "primary", onclick: async () => {
        const r = await act(() => api("POST", "/api/account/totp/setup"));
        const code = inp("code", "", { inputmode: "numeric", placeholder: "123456" });
        tf.replaceChildren(r.qr ? el("img", { src: r.qr, alt: "QR code", width: 180, height: 180 }) : "", el("p", { class: "hint" }, "Scan it, or enter this key: "), copyBox(r.secret),
          field("Code from the app", code), el("button", { class: "primary", onclick: async () => { await act(() => api("POST", "/api/account/totp/enable", { code: code.value }), "2FA on"); route(); } }, "Turn on"));
      } }, "Set up 2FA"));
  }
  main.replaceChildren(el("h1", {}, "Account"),
    el("div", { class: "card" }, el("h2", {}, `Admin: ${acc.username}`), pf),
    el("div", { class: "card" }, el("h2", {}, "Two-factor login"), tf),
    el("div", { class: "card" }, el("button", { onclick: async () => { await api("POST", "/api/logout"); boot(); } }, "Log out")));
}

boot();
