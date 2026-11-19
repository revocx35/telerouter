"""Gmail: connecting accounts with Google OAuth (fake Google), per-account token permissions, the gmail_* MCP
tools (search, read, drafts, send, labels, trash), refresh and revoked sign-ins, and the admin API."""

import base64
import email
import hashlib
import json
from email import policy
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from telerouter import tls
from telerouter.gmail import (COMPOSE, EMAIL_INSTRUCTIONS, MODIFY, READONLY, Gmail, GmailError, build_mime,
                              html_to_text)
from telerouter.mcp import McpServer
from telerouter.models import McpToken
from telerouter.security import new_token, token_hash
from telerouter.web.app import create_app

from .test_mcp import FakeEngine, req
from .test_web import setup_admin

CID = "123-abc.apps.googleusercontent.com"
SECRET = "GOCSPX-secret_value1"
REDIRECT = "https://telerouter.example.test/api/gmail/callback"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def part(mime, text, **kw):
    return {"mimeType": mime, "headers": [{"name": "Content-Type", "value": f"{mime}; charset=utf-8"}],
            "body": {"data": b64(text.encode()), "size": len(text)}, **kw}


class Google:
    """Google's token endpoint + a small Gmail API, behind httpx.MockTransport."""

    def __init__(self):
        self.email = "owner@gmail.com"
        self.scope = MODIFY
        self.challenge = ""
        self.refresh_ok = True
        self.token_calls: list[dict] = []
        self.revoked: list[str] = []
        self.sent: list[dict] = []
        self.drafts: dict[str, dict] = {}
        self.modified: list[dict] = []
        self.trashed: list[str] = []
        self.access = "at-1"
        self.n = 0
        self.seen_auth: set[str] = set()
        self.msgs = {
            "m1": {"id": "m1", "threadId": "t1", "labelIds": ["INBOX", "UNREAD"], "snippet": "Your invoice",
                   "payload": {"mimeType": "multipart/alternative",
                               "headers": [{"name": "From", "value": "Bank <bank@example.com>"},
                                           {"name": "To", "value": "owner@gmail.com"},
                                           {"name": "Subject", "value": "Invoice"},
                                           {"name": "Date", "value": "Wed, 18 Nov 2026 10:00:00 +0000"},
                                           {"name": "Message-ID", "value": "<abc@example.com>"}],
                               "parts": [part("text/html", "<html><style>p{}</style><p>Hello <b>Owner</b></p>"
                                                           "<p>Pay <a href='https://bank.example/x'>here</a></p></html>"),
                                         {"mimeType": "application/pdf", "filename": "invoice.pdf",
                                          "body": {"attachmentId": "att1", "size": 5}}]}},
        }

    def __call__(self, r: httpx.Request) -> httpx.Response:
        url = str(r.url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            form = {k: v[0] for k, v in parse_qs(r.content.decode()).items()}
            self.token_calls.append(form)
            if form.get("client_id") != CID or form.get("client_secret") != SECRET:
                return httpx.Response(401, json={"error": "invalid_client"})
            if form["grant_type"] == "authorization_code":
                digest = b64(hashlib.sha256(form["code_verifier"].encode()).digest())
                if digest != self.challenge or form["code"] != "good" or form["redirect_uri"] != REDIRECT:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json={"access_token": self.access, "expires_in": 3599,
                                                 "refresh_token": f"rt-{self.email}", "scope": self.scope})
            if not self.refresh_ok:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
            self.n += 1
            self.access = f"at-r{self.n}"
            return httpx.Response(200, json={"access_token": self.access, "expires_in": 3599, "scope": self.scope})
        if url.startswith("https://oauth2.googleapis.com/revoke"):
            self.revoked.append(parse_qs(r.content.decode())["token"][0])
            return httpx.Response(200)
        assert url.startswith("https://gmail.googleapis.com/gmail/v1/users/me/"), url
        self.seen_auth.add(r.headers.get("authorization", ""))
        if r.headers.get("authorization") != f"Bearer {self.access}":
            return httpx.Response(401, json={"error": {"message": "Invalid Credentials"}})
        path = r.url.path.split("/users/me")[1]
        body = json.loads(r.content) if r.content else {}
        if path == "/profile":
            return httpx.Response(200, json={"emailAddress": self.email, "messagesTotal": 42, "threadsTotal": 7})
        if path == "/labels":
            return httpx.Response(200, json={"labels": [{"id": "INBOX", "name": "INBOX", "type": "system"},
                                                        {"id": "Label_7", "name": "Bills", "type": "user"}]})
        if path == "/messages" and r.method == "GET":
            self.last_search = dict(r.url.params.multi_items())
            return httpx.Response(200, json={"messages": [{"id": "m1", "threadId": "t1"}], "resultSizeEstimate": 1})
        if path.startswith("/messages/") and path.count("/") == 2 and r.method == "GET":
            m = self.msgs.get(path.split("/")[2])
            return httpx.Response(200, json=m) if m else httpx.Response(404, json={"error": {"message": "Not Found"}})
        if path == "/messages/m1/attachments/att1":
            return httpx.Response(200, json={"data": b64(b"%PDF-"), "size": 5})
        if path == "/messages/send":
            self.sent.append(body)
            return httpx.Response(200, json={"id": "s1", "threadId": body.get("threadId", "tnew")})
        if path == "/drafts" and r.method == "POST":
            did = f"d{len(self.drafts) + 1}"
            self.drafts[did] = body
            return httpx.Response(200, json={"id": did, "message": {"id": "dm" + did}})
        if path == "/drafts/send":
            self.sent.append(self.drafts.pop(body["id"])["message"])
            return httpx.Response(200, json={"id": "s2", "threadId": "t9"})
        if path == "/messages/batchModify":
            self.modified.append(body)
            return httpx.Response(204)
        if path.endswith("/trash"):
            self.trashed.append(path.split("/")[2])
            return httpx.Response(200, json={"id": path.split("/")[2]})
        return httpx.Response(404, json={"error": {"message": "unknown " + path}})


def consent(g: Gmail, google: Google, access="full", reconnect="") -> str:
    url = g.start(REDIRECT, access, reconnect)
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert q["access_type"] == "offline" and q["prompt"] == "consent" and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == REDIRECT and q["client_id"] == CID and "secret" not in url.lower()
    google.challenge = q["code_challenge"]
    return q["state"]


def make():
    e = FakeEngine()
    cfg = e.store.config
    cfg.gmail.client_id, cfg.gmail.client_secret = CID, SECRET
    google = Google()
    e.gmail = Gmail(e, transport=httpx.MockTransport(google))
    a = new_token("trm_")
    cfg.mcp_tokens = [McpToken(id="tA", name="jarvis", token_hash=token_hash(a))]
    return e, google, McpServer(e), a


async def connect(e, google, email="owner@gmail.com", access="full", scope=MODIFY):
    google.email, google.scope = email, scope
    state = consent(e.gmail, google, access)
    return await e.gmail.finish(state, "good")


async def rpc(server, token, method, params=None):
    r = await server.handle(req({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, token))
    return json.loads(r.body)


async def call(server, token, name, **args):
    res = (await rpc(server, token, "tools/call", {"name": name, "arguments": args}))
    if "error" in res:
        return res
    return res["result"]


async def names(server, token):
    return {t["name"] for t in (await rpc(server, token, "tools/list"))["result"]["tools"]}


# -- connecting --------------------------------------------------------------------------------------------------
async def test_connect_flow_stores_the_account_and_state_is_single_use():
    e, google, _, _ = make()
    state = consent(e.gmail, google)
    acc = await e.gmail.finish(state, "good")
    assert acc.email == "owner@gmail.com" and acc.refresh_token == "rt-owner@gmail.com" and acc.scopes == [MODIFY]
    assert e.store.config.gmail.accounts == [acc]
    assert e.gmail.info(acc)["perms"] == ["draft", "organize", "read", "send", "trash"]
    with pytest.raises(GmailError, match="unknown or expired"):
        await e.gmail.finish(state, "good")                          # replay
    with pytest.raises(GmailError, match="unknown or expired"):
        await e.gmail.finish("forged", "good")
    with pytest.raises(GmailError, match="cancelled"):
        await e.gmail.finish(consent(e.gmail, google), "", "access_denied")
    with pytest.raises(GmailError, match="refused"):
        await e.gmail.finish(consent(e.gmail, google), "stolen-code")   # Google refuses a code without our PKCE

    # the same address again replaces the sign-in but keeps the id (and so the tokens' permissions)
    google.email = "owner@gmail.com"
    again = await e.gmail.finish(consent(e.gmail, google, "read"), "good")
    assert again.id == acc.id and len(e.store.config.gmail.accounts) == 1


async def test_multiple_accounts_and_reconnect_must_be_the_same_address():
    e, google, _, _ = make()
    a = await connect(e, google, "owner@gmail.com")
    b = await connect(e, google, "work@company.com", "compose", f"{READONLY} {COMPOSE}")
    assert [x.email for x in e.store.config.gmail.accounts] == ["owner@gmail.com", "work@company.com"]
    assert e.gmail.info(b)["perms"] == ["draft", "read", "send"]
    google.email = "someone-else@gmail.com"
    state = consent(e.gmail, google, "full", reconnect=a.id)
    with pytest.raises(GmailError, match="not owner@gmail.com"):
        await e.gmail.finish(state, "good")
    assert "rt-someone-else@gmail.com" in google.revoked              # the stray grant is given back
    assert len(e.store.config.gmail.accounts) == 2


async def test_start_needs_the_client():
    e, google, _, _ = make()
    e.store.config.gmail.client_secret = ""
    with pytest.raises(GmailError, match="client ID and secret"):
        e.gmail.start(REDIRECT)


# -- tools and permissions ----------------------------------------------------------------------------------------
async def test_tools_follow_the_tokens_boxes_per_account():
    e, google, server, tok = make()
    acc = await connect(e, google)
    assert not any(n.startswith("gmail_") for n in await names(server, tok))       # nothing ticked
    init = (await rpc(server, tok, "initialize"))["result"]["instructions"]
    assert EMAIL_INSTRUCTIONS not in init

    e.store.config.mcp_tokens[0].gmail = {acc.id: ["read"]}
    got = await names(server, tok)
    assert {"gmail_list_accounts", "gmail_search", "gmail_read_message", "gmail_read_thread"} <= got
    assert not got & {"gmail_send", "gmail_create_draft", "gmail_trash", "gmail_modify"}
    assert EMAIL_INSTRUCTIONS in (await rpc(server, tok, "initialize"))["result"]["instructions"]
    r = await call(server, tok, "gmail_send", to="x@example.com", subject="hi", body="b")
    assert "unknown tool" in r["error"]["message"]

    r = await call(server, tok, "gmail_list_accounts")
    assert r["structuredContent"]["accounts"] == [{"email": "owner@gmail.com", "name": "", "you_may": ["read"],
                                                   "needs_reconnect": False}]


async def test_search_and_read_a_message():
    e, google, server, tok = make()
    acc = await connect(e, google)
    e.store.config.mcp_tokens[0].gmail = {acc.id: ["read"]}
    r = await call(server, tok, "gmail_search", query="is:unread", max_results=500)
    s = r["structuredContent"]
    assert google.last_search == {"maxResults": "50", "q": "is:unread"}
    assert s["messages"][0]["subject"] == "Invoice" and s["messages"][0]["unread"] is True
    r = await call(server, tok, "gmail_read_message", message_id="m1", mark_read=True)
    m = r["structuredContent"]
    assert m["body_format"] == "html" and "Hello Owner" in m["body"] and "<https://bank.example/x>" in m["body"]
    assert "p{}" not in m["body"]
    assert m["attachments"] == [{"attachment_id": "att1", "filename": "invoice.pdf", "mime_type": "application/pdf",
                                 "size": 5}]
    assert m["mark_read"].startswith("not allowed") and not google.modified       # needs "organize"
    r = await call(server, tok, "gmail_get_attachment", message_id="m1", attachment_id="att1")
    assert base64.b64decode(r["structuredContent"]["base64"]) == b"%PDF-"
    r = await call(server, tok, "gmail_read_message", message_id="../../drafts")
    assert r["isError"] and "invalid id" in r["content"][0]["text"]
    r = await call(server, tok, "gmail_read_message", message_id="nope")
    assert r["isError"] and "not found" in r["content"][0]["text"]


async def test_send_reply_and_drafts():
    e, google, server, tok = make()
    acc = await connect(e, google)
    e.store.config.mcp_tokens[0].gmail = {acc.id: ["read", "draft", "send"]}
    r = await call(server, tok, "gmail_send", to=["Alice <alice@example.com>", "bob@example.com"], subject="Hi",
                   body="Hello there", bcc="carol@example.com")
    assert r["structuredContent"]["sent"] is True
    raw = email.message_from_bytes(base64.urlsafe_b64decode(google.sent[0]["raw"] + "=="), policy=policy.default)
    assert raw["To"] == "Alice <alice@example.com>, bob@example.com" and raw["Bcc"] == "carol@example.com"
    assert raw["From"] == "owner@gmail.com" and raw["Subject"] == "Hi"
    assert raw.get_content().strip() == "Hello there"

    r = await call(server, tok, "gmail_send", reply_to_message_id="m1", body="Paid.")
    sent = google.sent[1]
    raw = email.message_from_bytes(base64.urlsafe_b64decode(sent["raw"] + "=="), policy=policy.default)
    assert sent["threadId"] == "t1" and raw["In-Reply-To"] == "<abc@example.com>"
    assert raw["Subject"] == "Re: Invoice" and raw["To"] == "Bank <bank@example.com>"

    r = await call(server, tok, "gmail_send", to="x@example.com", subject="a\r\nBcc: evil@example.com", body="b")
    assert r["isError"] and "one line" in r["content"][0]["text"] and len(google.sent) == 2
    r = await call(server, tok, "gmail_send", to="not an address", body="b")
    assert r["isError"] and len(google.sent) == 2
    r = await call(server, tok, "gmail_send", subject="no one", body="b")
    assert r["isError"] and "no recipient" in r["content"][0]["text"]

    r = await call(server, tok, "gmail_create_draft", to="alice@example.com", subject="Draft", body="later")
    did = r["structuredContent"]["draft_id"]
    assert did in google.drafts
    r = await call(server, tok, "gmail_send_draft", draft_id=did)
    assert r["structuredContent"]["sent"] and did not in google.drafts and len(google.sent) == 3


async def test_organize_and_trash_with_label_names():
    e, google, server, tok = make()
    acc = await connect(e, google)
    e.store.config.mcp_tokens[0].gmail = {acc.id: ["organize", "trash"]}
    r = await call(server, tok, "gmail_modify", message_ids=["m1"], add_labels=["bills"], archive=True, mark_read=True)
    assert google.modified == [{"ids": ["m1"], "addLabelIds": ["Label_7"], "removeLabelIds": ["INBOX", "UNREAD"]}]
    r = await call(server, tok, "gmail_modify", message_ids=["m1"], add_labels=["Nope"])
    assert r["isError"] and "no label" in r["content"][0]["text"]
    r = await call(server, tok, "gmail_trash", message_ids=["m1", "m2"])
    assert r["structuredContent"]["trashed"] == 2 and google.trashed == ["m1", "m2"]


async def test_accounts_are_separate_and_access_levels_count():
    e, google, server, tok = make()
    home = await connect(e, google, "owner@gmail.com")
    work = await connect(e, google, "work@company.com", "read", READONLY)
    e.store.config.gmail.accounts[1].name = "work"
    t = e.store.config.mcp_tokens[0]
    t.gmail = {home.id: ["read"], work.id: ["read", "send"]}
    r = await call(server, tok, "gmail_search")
    assert r["isError"] and "which account?" in r["content"][0]["text"]
    r = await call(server, tok, "gmail_search", account="work")
    assert not r["isError"] and r["structuredContent"]["account"] == "work@company.com"
    # the token may send from work, but the account was connected read-only
    r = await call(server, tok, "gmail_send", to="a@example.com", body="x")
    assert r["isError"] and "reconnect it with more access" in r["content"][0]["text"]
    t.gmail = {work.id: ["read"]}
    r = await call(server, tok, "gmail_search", account="owner@gmail.com")
    assert r["isError"] and "no Gmail account" in r["content"][0]["text"]


async def test_expired_access_is_refreshed_and_a_revoked_sign_in_is_reported():
    e, google, server, tok = make()
    acc = await connect(e, google)
    e.store.config.mcp_tokens[0].gmail = {acc.id: ["read"]}
    google.access = "at-rotated-by-google"                           # Google forgot our access token: 401
    r = await call(server, tok, "gmail_search")
    assert not r["isError"] and google.token_calls[-1]["grant_type"] == "refresh_token"
    assert google.token_calls[-1]["refresh_token"] == "rt-owner@gmail.com"

    google.refresh_ok = False
    google.access = "something-else"
    r = await call(server, tok, "gmail_search")
    text = r["content"][0]["text"]
    assert r["isError"] and "reconnect" in text and "rt-owner" not in text
    assert acc.error and e.gmail.info(acc)["connected"] is False
    assert all("rt-" not in a for a in google.seen_auth)             # the refresh token never goes to the API


def test_mime_and_html_helpers():
    raw = build_mime(sender="me@gmail.com", to="a@example.com", subject="Grüße", body="ça va", html="<p>ça va</p>")
    m = email.message_from_bytes(raw, policy=policy.default)
    assert m["Subject"] == "Grüße" and m.get_body(("plain",)).get_content().strip() == "ça va"
    assert m.get_body(("html",)).get_content().strip() == "<p>ça va</p>"
    with pytest.raises(GmailError):
        build_mime(sender="me@gmail.com", to="a@example.com\nBcc: x@example.com")
    assert html_to_text("<div>a</div><script>evil()</script><div>b &amp; c</div>") == "a\n\nb & c"


# -- admin API ---------------------------------------------------------------------------------------------------
@pytest.fixture()
def web(settings):
    files = tls.ensure(settings.data_dir, [])
    app, _ = create_app(settings, files)
    google = Google()
    app.state.engine.gmail.transport = httpx.MockTransport(google)
    with TestClient(app, base_url="https://testserver") as c:
        yield c, google, app


def test_admin_api_connects_and_grants(web):
    c, google, app = web
    assert c.get("/api/gmail").status_code == 401
    csrf = setup_admin(c)
    h = {"X-CSRF-Token": csrf}
    assert c.put("/api/gmail/client", json={"client_id": CID, "client_secret": SECRET}).status_code == 403   # CSRF
    assert c.put("/api/gmail/client", json={"client_id": "nope"}, headers=h).status_code == 422
    r = c.put("/api/gmail/client", json={"client_id": CID, "client_secret": SECRET, "redirect_uri": REDIRECT},
              headers=h).json()
    assert r["client_secret_set"] and SECRET not in json.dumps(r) and r["effective_redirect_uri"] == REDIRECT
    r = c.put("/api/gmail/client", json={"client_id": CID, "client_secret": "", "redirect_uri": ""}, headers=h).json()
    assert r["client_secret_set"] and r["effective_redirect_uri"] == "https://testserver/api/gmail/callback"
    c.put("/api/gmail/client", json={"redirect_uri": REDIRECT}, headers=h)

    url = c.post("/api/gmail/connect", json={"access": "full"}, headers=h).json()["url"]
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    google.challenge = q["code_challenge"]
    bad = c.get("/api/gmail/callback", params={"state": "forged", "code": "good"})        # public, no session needed
    assert bad.status_code == 400 and "expired" in bad.text
    ok = c.get("/api/gmail/callback", params={"state": q["state"], "code": "good"})
    assert ok.status_code == 200 and "owner@gmail.com is connected" in ok.text
    assert "<script" not in ok.text
    d = c.get("/api/gmail").json()
    acc = d["accounts"][0]
    assert acc["email"] == "owner@gmail.com" and acc["connected"]
    assert "rt-owner" not in json.dumps(d) and "rt-owner" not in json.dumps(c.get("/api/mcp-tokens").json())

    tok = c.post("/api/mcp-tokens", json={"name": "mail agent", "scopes": [],
                                          "gmail": {acc["id"]: ["read", "send", "bogus"], "gone": ["read"]}},
                 headers=h)
    assert tok.status_code == 422                                     # unknown permission
    tok = c.post("/api/mcp-tokens", json={"name": "mail agent", "scopes": [],
                                          "gmail": {acc["id"]: ["send", "read"], "gone": ["read"]}}, headers=h).json()
    assert tok["gmail"] == {acc["id"]: ["read", "send"]}             # unknown accounts dropped, order fixed
    r = c.post(f"/api/gmail/accounts/{acc['id']}/check", headers=h).json()
    assert r["messages_total"] == 42
    assert c.put(f"/api/gmail/accounts/{acc['id']}", json={"name": "home"}, headers=h).json()["name"] == "home"

    # the copy-the-address way
    google.email = "work@company.com"
    url = c.post("/api/gmail/connect", json={"access": "read"}, headers=h).json()["url"]
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    google.challenge = q["code_challenge"]
    r = c.post("/api/gmail/finish", json={"url": f"http://127.0.0.1:1/?state={q['state']}&code=good&scope=x"},
               headers=h)
    assert r.status_code == 200 and r.json()["email"] == "work@company.com"

    assert c.delete(f"/api/gmail/accounts/{acc['id']}", headers=h).status_code == 200
    assert google.revoked == ["rt-owner@gmail.com"]
    t = next(x for x in c.get("/api/mcp-tokens").json()["tokens"] if x["name"] == "mail agent")
    assert t["gmail"] == {}                                           # the grant went with the account


def test_new_client_id_drops_the_old_secret(web):
    c, _, app = web
    h = {"X-CSRF-Token": setup_admin(c)}
    c.put("/api/gmail/client", json={"client_id": CID, "client_secret": SECRET}, headers=h)
    r = c.put("/api/gmail/client", json={"client_id": "999-x.apps.googleusercontent.com"}, headers=h).json()
    assert r["client_secret_set"] is False
    assert c.post("/api/gmail/connect", json={}, headers=h).status_code == 422
