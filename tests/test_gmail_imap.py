"""Gmail with an app password (IMAP + SMTP, gmail_imap.py) against a Gmail-like IMAP server over TCP: connecting,
the same gmail_* tools as the Google sign-in accounts, ids, labels with non-ASCII names, drafts, sending, trash,
refused passwords, and the admin API."""

import imaplib
import json

import pytest
from fastapi.testclient import TestClient

from telerouter import tls
from telerouter.gmail import Gmail, GmailError
from telerouter.gmail_imap import (_tokens, _tree, clean_app_password, mutf7_decode, mutf7_encode, parse_fetch,
                                   parse_list)
from telerouter.mcp import McpServer
from telerouter.models import McpToken
from telerouter.security import new_token, token_hash
from telerouter.web.app import create_app

from .fake_imap import FakeImap, FakeSmtp, Store
from .test_mcp import FakeEngine, req
from .test_web import setup_admin

PW = "abcd efgh ijkl mnop"

INVOICE = (b"From: Bank <bank@example.com>\nTo: owner@gmail.com\nSubject: Invoice\nDate: Wed, 18 Nov 2026 10:00:00 +0000\n"
           b"Message-ID: <inv@example.com>\nMIME-Version: 1.0\nContent-Type: multipart/mixed; boundary=XX\n\n--XX\n"
           b"Content-Type: text/html; charset=utf-8\n\n<p>Hello <b>Owner</b></p><script>x()</script>\n--XX\n"
           b"Content-Type: application/pdf\nContent-Disposition: attachment; filename=invoice.pdf\n"
           b"Content-Transfer-Encoding: base64\n\nJVBERi0=\n--XX--\n")
NOTE = b"From: Alice <alice@example.com>\nTo: owner@gmail.com\nSubject: =?utf-8?q?Gr=C3=BC=C3=9Fe?=\n\nhi there\n"


@pytest.fixture()
def env():
    st = Store()
    srv = FakeImap(st)
    st.inv = st.add(INVOICE, labels={"\\Inbox", "Bills"})
    st.note = st.add(NOTE, labels={"\\Inbox"}, flags={"\\Seen"}, thrid=st.inv["thrid"])
    e = FakeEngine()
    g = Gmail(e)
    g.imap_factory = lambda: imaplib.IMAP4("127.0.0.1", srv.port, timeout=10)
    FakeSmtp.sent = []
    g.smtp_factory = FakeSmtp
    e.gmail = g
    a = new_token("trm_")
    e.store.config.mcp_tokens = [McpToken(id="tA", name="jarvis", token_hash=token_hash(a))]
    yield e, st, McpServer(e), a
    srv.close()


async def call(server, token, name, **args):
    r = await server.handle(req({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": name, "arguments": args}}, token))
    return json.loads(r.body)["result"]


def text(r):
    return r["content"][0]["text"]


async def connected(e, st, perms=("read", "draft", "send", "organize", "trash"), access="full"):
    acc = await e.gmail.connect_app_password("Owner@Gmail.com", PW, "home", access)
    e.store.config.mcp_tokens[0].gmail = {acc.id: list(perms)}
    return acc


# -- unit ------------------------------------------------------------------------------------------------------
def test_helpers():
    assert mutf7_encode("Çözel & co") == "&AMcA9g-zel &- co" and mutf7_decode("&AMcA9g-zel &- co") == "Çözel & co"
    assert mutf7_decode("[Gmail]/T&APw-m Postalar") == "[Gmail]/Tüm Postalar"
    assert clean_app_password(" ABCD efgh ijkl mnop ") == "abcdefghijklmnop"
    with pytest.raises(GmailError):
        clean_app_password("hunter2!")
    got = parse_fetch([(b'1 (UID 7 X-GM-MSGID 255 X-GM-LABELS (\\Inbox "a \\"b\\"") FLAGS (\\Seen) '
                        b'BODY[HEADER.FIELDS (FROM SUBJECT)] {5}', b"ab\r\n\r"), b")",
                       b"2 (UID 9 X-GM-MSGID 16 FLAGS ())"])
    assert got[0]["UID"] == 7 and got[0]["X-GM-LABELS"] == ["\\Inbox", 'a "b"'] and \
        got[0]["BODY[HEADER.FIELDS (FROM SUBJECT)]"] == b"ab\r\n\r"
    assert got[1] == {"UID": 9, "X-GM-MSGID": "16", "FLAGS": []}
    assert parse_list([b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"', (b'(\\HasNoChildren) "/" {4}', b"Odd)")]) == \
        [({"\\all", "\\hasnochildren"}, "[Gmail]/All Mail"),
         ({"\\hasnochildren"}, "Odd)")]
    assert _tree(_tokens([b"NIL (a)"])) == [None, ["a"]]


# -- connecting --------------------------------------------------------------------------------------------------
async def test_connect_checks_imap_and_smtp_and_keeps_the_password_secret(env):
    e, st, _, _ = env
    with pytest.raises(GmailError, match="refused the address or app password"):
        await e.gmail.connect_app_password("owner@gmail.com", "zzzz zzzz zzzz zzzz")
    assert e.store.config.gmail.accounts == []
    acc = await e.gmail.connect_app_password("owner@gmail.com", PW, "home")
    assert acc.kind == "app_password" and acc.app_password == "abcdefghijklmnop" and acc.email == "owner@gmail.com"
    info = e.gmail.info(acc)
    assert info["connected"] and info["perms"] == ["draft", "organize", "read", "send", "trash"]
    assert "abcdefgh" not in json.dumps(info)
    # read-only: the boxes for writing can't be used, whatever the token says
    again = await e.gmail.connect_app_password("owner@gmail.com", PW, "", "read")
    assert again.id == acc.id and e.gmail.info(again)["perms"] == ["read"] and again.name == "home"


# -- the tools -------------------------------------------------------------------------------------------------------
async def test_search_read_thread_and_attachment(env):
    e, st, server, tok = env
    await connected(e, st, ("read",))
    r = await call(server, tok, "gmail_search", query="is:unread", label="Bills")
    s = r["structuredContent"]
    assert st.searches[-1] == 'is:unread label:"Bills"'
    assert [m["subject"] for m in s["messages"]] == ["Invoice"]
    m = s["messages"][0]
    assert m["id"] == format(st.inv["msgid"], "x") and m["unread"] and "INBOX" in m["labels"] and "Bills" in m["labels"]
    r = await call(server, tok, "gmail_search")
    assert [x["subject"] for x in r["structuredContent"]["messages"]] == ["Grüße", "Invoice"]   # newest first

    r = await call(server, tok, "gmail_read_message", message_id=m["id"])
    msg = r["structuredContent"]
    assert msg["body_format"] == "html" and msg["body"] == "Hello Owner" and msg["thread_id"] == m["thread_id"]
    assert msg["attachments"] == [{"attachment_id": "p2", "filename": "invoice.pdf", "mime_type": "application/pdf",
                                   "size": 5}]
    r = await call(server, tok, "gmail_get_attachment", message_id=m["id"], attachment_id="p2")
    assert r["structuredContent"]["base64"] == "JVBERi0="
    r = await call(server, tok, "gmail_read_thread", thread_id=m["thread_id"])
    assert [x["subject"] for x in r["structuredContent"]["messages"]] == ["Invoice", "Grüße"]
    r = await call(server, tok, "gmail_read_message", message_id="abc123")
    assert r["isError"] and "not found" in text(r)
    r = await call(server, tok, "gmail_list_labels")
    names = [x["name"] for x in r["structuredContent"]["labels"]]
    assert "Bills" in names and "Çözel" in names and "INBOX" in names and not any("[Gmail]" in n for n in names)


async def test_organize_and_trash(env):
    e, st, server, tok = env
    await connected(e, st)
    mid = format(st.inv["msgid"], "x")
    r = await call(server, tok, "gmail_modify", message_ids=[mid], add_labels=["çözel"], archive=True,
                   mark_read=True, star=True)
    assert not r["isError"], text(r)
    assert st.inv["labels"] == {"Bills", "&AMcA9g-zel", "\\Starred"} and "\\Seen" in st.inv["flags"]
    r = await call(server, tok, "gmail_modify", message_ids=[mid], mark_unread=True, remove_labels=["Bills"])
    assert "\\Seen" not in st.inv["flags"] and "Bills" not in st.inv["labels"]
    r = await call(server, tok, "gmail_modify", message_ids=[mid], add_labels=["Nope"])
    assert r["isError"] and "no label" in text(r)
    r = await call(server, tok, "gmail_trash", message_ids=[mid])
    assert r["structuredContent"]["trashed"] == 1 and st.inv["trashed"]
    assert st.moves == ["UID MOVE"]                    # MOVE is seen although Gmail lists it only after login
    r = await call(server, tok, "gmail_read_message", message_id=mid)       # still readable in the trash
    assert not r["isError"]
    r = await call(server, tok, "gmail_trash", message_ids=[mid], restore=True)
    assert not st.inv["trashed"] and "\\Inbox" in st.inv["labels"]


async def test_send_reply_and_drafts(env):
    e, st, server, tok = env
    await connected(e, st)
    r = await call(server, tok, "gmail_send", to="Alice <alice@example.com>", bcc="carol@example.com",
                   subject="Hi", body="Hello")
    assert r["structuredContent"]["sent"] is True
    frm, rcpts, data = FakeSmtp.sent[-1]
    assert frm == "owner@gmail.com" and rcpts == ["alice@example.com", "carol@example.com"]
    assert b"carol" not in data and b"Subject: Hi" in data                   # Bcc only on the envelope

    r = await call(server, tok, "gmail_send", reply_to_message_id=format(st.inv["msgid"], "x"), body="Paid.")
    _, rcpts, data = FakeSmtp.sent[-1]
    assert rcpts == ["bank@example.com"] and b"In-Reply-To: <inv@example.com>" in data and b"Subject: Re: Invoice" in data

    r = await call(server, tok, "gmail_create_draft", to="bob@example.com", subject="Later", body="draft text")
    did = r["structuredContent"]["draft_id"]
    draft = next(m for m in st.msgs if format(m["msgid"], "x") == did)
    assert "\\Draft" in draft["labels"]
    r = await call(server, tok, "gmail_list_drafts")
    assert [d["subject"] for d in r["structuredContent"]["drafts"]] == ["Later"]
    r = await call(server, tok, "gmail_update_draft", draft_id=did, to="bob@example.com", subject="Later 2", body="x")
    did2 = r["structuredContent"]["draft_id"]
    assert did2 != did and draft["trashed"]
    r = await call(server, tok, "gmail_send_draft", draft_id=did2)
    assert r["structuredContent"]["sent"] and FakeSmtp.sent[-1][1] == ["bob@example.com"]
    assert (await call(server, tok, "gmail_list_drafts"))["structuredContent"]["drafts"] == []
    r = await call(server, tok, "gmail_delete_draft", draft_id=did2)
    assert r["isError"] and "no such draft" in text(r)


async def test_revoked_app_password_is_reported(env):
    e, st, server, tok = env
    acc = await connected(e, st, ("read", "send"))
    st.password = "qqqqqqqqqqqqqqqq"
    r = await call(server, tok, "gmail_search")
    assert r["isError"] and "app password" in text(r) and acc.error and not e.gmail.info(acc)["connected"]
    FakeSmtp.password = "qqqqqqqqqqqqqqqq"
    try:
        r = await call(server, tok, "gmail_send", to="a@example.com", body="x")
        assert r["isError"] and "app password" in text(r)
    finally:
        FakeSmtp.password = "abcdefghijklmnop"
    st.password = "abcdefghijklmnop"
    r = await call(server, tok, "gmail_search")
    assert not r["isError"] and acc.error == ""                             # works again, error cleared


# -- admin API ---------------------------------------------------------------------------------------------------
def test_admin_api_app_password(settings, env):
    _, st, _, _ = env
    files = tls.ensure(settings.data_dir, [])
    app, _ = create_app(settings, files)
    srv_port = None
    with TestClient(app, base_url="https://testserver") as c:
        g = app.state.engine.gmail
        from .fake_imap import FakeImap as _F
        srv = _F(st)
        srv_port = srv.port
        g.imap_factory = lambda: imaplib.IMAP4("127.0.0.1", srv_port, timeout=10)
        g.smtp_factory = FakeSmtp
        try:
            body = {"email": "owner@gmail.com", "app_password": PW, "name": "home"}
            assert c.post("/api/gmail/app-password", json=body).status_code == 401
            h = {"X-CSRF-Token": setup_admin(c)}
            assert c.post("/api/gmail/app-password", json=body).status_code == 403             # CSRF
            bad = c.post("/api/gmail/app-password", json={**body, "app_password": "wrong pass word!"}, headers=h)
            assert bad.status_code == 422 and "16 letters" in bad.json()["detail"]
            r = c.post("/api/gmail/app-password", json=body, headers=h)
            assert r.status_code == 200, r.text
            acc = r.json()
            assert acc["kind"] == "app_password" and acc["connected"]
            d = c.get("/api/gmail").json()
            assert "abcdefgh" not in json.dumps(d) and d["accounts"][0]["email"] == "owner@gmail.com"
            chk = c.post(f"/api/gmail/accounts/{acc['id']}/check", headers=h).json()
            assert chk["messages_total"] == 2
            assert c.delete(f"/api/gmail/accounts/{acc['id']}", headers=h).status_code == 200
            assert c.get("/api/gmail").json()["accounts"] == []
        finally:
            srv.close()
