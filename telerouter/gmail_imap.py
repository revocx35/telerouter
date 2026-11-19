"""Gmail over IMAP + SMTP with a Google app password: the second way to connect an account (no Google Cloud project).

The owner turns on 2-Step Verification, makes an app password at myaccount.google.com/apppasswords and gives
telerouter the address + that password. `ImapMail` offers the same operations as the REST backend (`RestMail` in
gmail.py), so the gmail_* tools, ids and permission boxes are the same for both kinds of account:

* ids: Gmail's REST message/thread ids are the hex form of IMAP's X-GM-MSGID / X-GM-THRID, so ids look and work the
  same. A draft's id is its message id.
* search: Gmail's own search syntax through X-GM-RAW in the "All Mail" folder (found by its special-use flag, the
  name is localized); labels through X-GM-LABELS; unread = no \\Seen flag.
* drafts: APPEND to the Drafts folder with \\Draft; sending one = SMTP + moving the draft to the trash.
* sending: smtp.gmail.com (implicit TLS, the app password); Gmail files a copy in Sent itself. Bcc is taken out of
  the message and only used as an envelope recipient.
* trash: MOVE to the Trash folder (Gmail empties it after 30 days); restore = MOVE back to INBOX.

imaplib/smtplib are blocking: every operation runs in a thread with its own short IMAP session (login, work, logout),
at most 3 at a time per account (Gmail allows 15 connections). Certificates are verified (default SSL context).
"""

from __future__ import annotations

import asyncio
import base64
import email
import imaplib
import re
import smtplib
import ssl
import time
from email.policy import SMTP, default
from email.utils import getaddresses
from typing import TYPE_CHECKING, Any

from .gmail import MAX_API_BODY, MAX_ATTACHMENT, GmailError, html_to_text

if TYPE_CHECKING:
    from .gmail import Gmail
    from .models import GmailAccount

IMAP_HOST, SMTP_HOST = "imap.gmail.com", "smtp.gmail.com"
HEADERS = "BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE)]"
META = "UID X-GM-MSGID X-GM-THRID X-GM-LABELS FLAGS"
FOLDER_TTL = 600
SPECIAL = {"\\all": "all", "\\drafts": "drafts", "\\sent": "sent", "\\trash": "trash", "\\junk": "junk",
           "\\flagged": "flagged", "\\important": "important"}
# REST-style system label ids <-> Gmail IMAP labels
SYS_TO_IMAP = {"INBOX": "\\Inbox", "STARRED": "\\Starred", "IMPORTANT": "\\Important", "SENT": "\\Sent",
               "DRAFT": "\\Draft"}
IMAP_TO_SYS = {v.lower(): k for k, v in SYS_TO_IMAP.items()}
SYS_SEARCH = {"INBOX": "in:inbox", "SENT": "in:sent", "DRAFT": "in:drafts", "TRASH": "in:trash", "SPAM": "in:spam",
              "STARRED": "is:starred", "UNREAD": "is:unread", "IMPORTANT": "is:important"}


class AuthFailed(GmailError):
    """Gmail refused the address / app password."""


def imap_connect() -> imaplib.IMAP4:
    return imaplib.IMAP4_SSL(IMAP_HOST, 993, ssl_context=ssl.create_default_context(), timeout=30)


def smtp_connect() -> smtplib.SMTP:
    return smtplib.SMTP_SSL(SMTP_HOST, 465, context=ssl.create_default_context(), timeout=30)


def clean_app_password(v: str) -> str:
    """Google shows app passwords as 'abcd efgh ijkl mnop'."""
    v = re.sub(r"\s+", "", v or "")
    if not re.fullmatch(r"[A-Za-z]{16}", v):
        raise GmailError("an app password is 16 letters (Google shows it as four groups of four)")
    return v.lower()


# -- IMAP modified UTF-7 (RFC 3501 5.1.3) for folder/label names ---------------------------------------------------
def mutf7_encode(s: str) -> str:
    out, buf = [], []

    def flush():
        if buf:
            b = "".join(buf).encode("utf-16-be")
            out.append("&" + base64.b64encode(b).decode().rstrip("=").replace("/", ",") + "-")
            buf.clear()
    for ch in s:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def mutf7_decode(s: str) -> str:
    def dec(m):
        t = m.group(1)
        if not t:
            return "&"
        try:
            return base64.b64decode(t.replace(",", "/") + "=" * (-len(t) % 4)).decode("utf-16-be")
        except (ValueError, UnicodeDecodeError):
            return m.group(0)
    return re.sub(r"&([A-Za-z0-9+,]*)-", dec, s)


def q(name: str) -> str:
    """A mailbox or label name as an IMAP quoted string."""
    enc = mutf7_encode(name)
    return '"' + enc.replace("\\", "\\\\").replace('"', '\\"') + '"'


# -- parsing imaplib's answers -----------------------------------------------------------------------------------
class Lit:
    __slots__ = ("data",)

    def __init__(self, data: bytes):
        self.data = data


_OPEN, _CLOSE = object(), object()
_TOK = re.compile(rb'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|(NIL)(?=[\s()]|$)|'
                  rb'([^\s()"\[]+(?:\[[^\]]*\])?(?:<[0-9.]+>)?|\[[^\]]*\][^\s()"]*))')


def _segments(data: list) -> list:
    """imaplib's list of bytes and (head, literal) tuples -> text pieces and Lit objects in wire order."""
    out = []
    for item in data or []:
        if isinstance(item, tuple):
            head, lit = item[0], item[1] if len(item) > 1 else b""
            out += [re.sub(rb"\{\d+\+?\}\s*$", b"", head), Lit(lit)]
        elif isinstance(item, bytes):
            out.append(item)
    return out


def _tokens(parts: list) -> list:
    toks: list = []
    for p in parts:
        if isinstance(p, Lit):
            toks.append(p)
            continue
        pos = 0
        while pos < len(p):
            m = _TOK.match(p, pos)
            if not m or m.end() == pos:
                if not p[pos:].strip():
                    break
                raise GmailError("unexpected answer from Gmail's IMAP server")
            pos = m.end()
            if m.group(1):
                toks.append(_OPEN)
            elif m.group(2):
                toks.append(_CLOSE)
            elif m.group(3) is not None:
                toks.append(re.sub(rb"\\(.)", rb"\1", m.group(3)).decode("utf-8", "replace"))
            elif m.group(4):
                toks.append(None)
            else:
                toks.append(m.group(5).decode("utf-8", "replace"))
    return toks


def _tree(toks: list) -> list:
    stack: list[list] = [[]]
    for t in toks:
        if t is _OPEN:
            stack.append([])
        elif t is _CLOSE:
            if len(stack) > 1:
                done = stack.pop()
                stack[-1].append(done)
        else:
            stack[-1].append(t.data if isinstance(t, Lit) else t)
    while len(stack) > 1:
        done = stack.pop()
        stack[-1].append(done)
    return stack[0]


def parse_fetch(data: list) -> list[dict]:
    """FETCH answers -> [{"UID": 5, "X-GM-MSGID": ..., "X-GM-LABELS": [...], "BODY[]": b"..."}, ...]."""
    out = []
    for item in _tree(_tokens(_segments(data))):
        if not isinstance(item, list):
            continue                              # the message sequence number
        d: dict[str, Any] = {}
        for i in range(0, len(item) - 1, 2):
            k = item[i]
            if isinstance(k, str):
                d[k.upper().replace(".PEEK", "")] = item[i + 1]
        if "UID" in d:
            try:
                d["UID"] = int(d["UID"])
            except (TypeError, ValueError):
                continue
            out.append(d)
    return out


def parse_list(data: list) -> list[tuple[set[str], str]]:
    out = []
    for item in data or []:
        if item is None:
            continue
        parts = _segments([item])
        tree = _tree(_tokens(parts))
        if len(tree) >= 3 and isinstance(tree[0], list):
            name = tree[2]
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            if isinstance(name, str):
                out.append(({str(f).lower() for f in tree[0]}, mutf7_decode(name)))
    return out


def _uids(data: list) -> list[int]:
    out = []
    for item in data or []:
        if isinstance(item, bytes):
            out += [int(x) for x in item.split() if x.isdigit()]
    return out


def _hex(v: Any) -> str:
    try:
        return format(int(v), "x")
    except (TypeError, ValueError):
        return ""


def _dec(hex_id: str) -> str:
    try:
        return str(int(hex_id, 16))
    except ValueError as e:
        raise GmailError("invalid id") from e


def _labels(d: dict) -> list[str]:
    out = []
    for lab in d.get("X-GM-LABELS") or []:
        if not isinstance(lab, str):
            continue
        out.append(IMAP_TO_SYS.get(lab.lower(), mutf7_decode(lab)))
    flags = {str(f).lower() for f in d.get("FLAGS") or []}
    if "\\seen" not in flags:
        out.append("UNREAD")
    if "\\flagged" in flags:
        out.append("STARRED")
    return list(dict.fromkeys(out))


def _headers_of(raw: bytes) -> dict[str, str]:
    msg = email.message_from_bytes(raw or b"", policy=default)
    return {k.lower(): str(v) for k, v in msg.items()}


def _summary(d: dict) -> dict:
    h = _headers_of(d.get("BODY[HEADER.FIELDS (FROM TO CC SUBJECT DATE)]") or b"")
    labels = _labels(d)
    return {"id": _hex(d.get("X-GM-MSGID")), "thread_id": _hex(d.get("X-GM-THRID")), "from": h.get("from", ""),
            "to": h.get("to", ""), "cc": h.get("cc", ""), "subject": h.get("subject", ""), "date": h.get("date", ""),
            "snippet": "", "labels": labels, "unread": "UNREAD" in labels}


def _text(part) -> str:
    try:
        return part.get_content()
    except (LookupError, KeyError, ValueError, AssertionError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload)


def _parts(msg):
    """(index, part, is_attachment) over every leaf part; the index is the attachment id ("p<index>")."""
    for i, p in enumerate(msg.walk()):
        if p.is_multipart():
            continue
        yield i, p, bool(p.get_filename()) or p.get_content_disposition() == "attachment"


def full_from_mime(d: dict, raw: bytes, max_chars: int) -> dict:
    msg = email.message_from_bytes(raw, policy=default)
    body, kind = "", ""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
    except (KeyError, ValueError):
        part = None
    if part is not None:
        body = _text(part)
        kind = "plain" if part.get_content_type() == "text/plain" else "html"
        if kind == "html":
            body = html_to_text(body)
    atts = []
    for i, p, is_att in _parts(msg):
        if is_att:
            payload = p.get_payload(decode=True) or b""
            atts.append({"attachment_id": f"p{i}", "filename": str(p.get_filename() or "attachment")[:200],
                         "mime_type": p.get_content_type()[:100], "size": len(payload)})
    labels = _labels(d)
    h = {k.lower(): str(v) for k, v in msg.items()}
    return {"id": _hex(d.get("X-GM-MSGID")), "thread_id": _hex(d.get("X-GM-THRID")), "from": h.get("from", ""),
            "to": h.get("to", ""), "cc": h.get("cc", ""), "subject": h.get("subject", ""), "date": h.get("date", ""),
            "labels": labels, "unread": "UNREAD" in labels, "reply_to": h.get("reply-to", ""), "bcc": h.get("bcc", ""),
            "body": body[:max_chars], "body_format": kind, "truncated": len(body) > max_chars, "attachments": atts}


# =============================================================================================================
class ImapMail:
    """The operations of the gmail_* tools for an app-password account."""

    def __init__(self, g: "Gmail", acc: "GmailAccount"):
        self.g, self.acc = g, acc

    # -- sessions ---------------------------------------------------------------------------------------------
    async def _run(self, fn, *args):
        sem = self.g._imap_slots.setdefault(self.acc.id, asyncio.Semaphore(3))
        async with sem:
            try:
                res = await asyncio.to_thread(self._session, fn, *args)
            except AuthFailed as e:
                self._failed(str(e))
                raise
        self.acc.last_used = time.time()
        if self.acc.error:
            self.acc.error = ""
            self.g.engine.store.save()
        return res

    def _failed(self, why: str) -> None:
        self.acc.error = why
        self.g.engine.store.save()

    def _session(self, fn, *args):
        try:
            M = self.g.imap_factory()
        except (OSError, imaplib.IMAP4.error) as e:
            raise GmailError(f"cannot reach Gmail's IMAP server ({type(e).__name__})") from e
        try:
            try:
                M.login(self.acc.email, self.acc.app_password)
            except imaplib.IMAP4.error as e:
                raise AuthFailed("Gmail refused the address or app password (revoked, or the Google password "
                                 "was changed: make a new app password)") from e
            # Gmail announces MOVE, UIDPLUS... only after login; imaplib keeps the list from before it
            typ, data = M.capability()
            if typ == "OK" and data and isinstance(data[-1], bytes):
                M.capabilities = tuple(data[-1].decode("ascii", "replace").upper().split())
            return fn(M, *args)
        except GmailError:
            raise
        except (imaplib.IMAP4.error, OSError) as e:
            raise GmailError(f"Gmail's IMAP server failed: {type(e).__name__}: {str(e)[:160]}") from e
        finally:
            try:
                M.logout()
            except Exception:
                pass

    def _folders(self, M) -> dict:
        have = self.g._imap_folders.get(self.acc.id)
        if have and time.time() - have[0] < FOLDER_TTL:
            return have[1]
        typ, data = M.list('""', '"*"')
        if typ != "OK":
            raise GmailError("Gmail did not list its folders")
        special: dict[str, str] = {"inbox": "INBOX"}
        user: list[str] = []
        for flags, name in parse_list(data):
            kind = next((SPECIAL[f] for f in flags if f in SPECIAL), "")
            if kind:
                special.setdefault(kind, name)
            elif "\\noselect" not in flags and name.upper() != "INBOX" and not name.startswith("[Gmail]"):
                user.append(name)
        if "all" not in special:
            raise GmailError("Gmail's IMAP has no 'All Mail' folder (is it hidden from IMAP in Gmail's settings?)")
        res = {"special": special, "user": user}
        self.g._imap_folders[self.acc.id] = (time.time(), res)
        return res

    def _folder(self, M, kind: str) -> str:
        f = self._folders(M)["special"].get(kind)
        if not f:
            raise GmailError(f"Gmail's IMAP has no {kind} folder (shown in IMAP? Gmail settings → Labels)")
        return f

    @staticmethod
    def _select(M, name: str, write: bool = False) -> None:
        typ, _ = M.select(q(name), readonly=not write)
        if typ != "OK":
            raise GmailError(f"cannot open the folder {name}")

    @staticmethod
    def _find(M, hex_id: str, what: str = "X-GM-MSGID") -> list[int]:
        typ, data = M.uid("SEARCH", what, _dec(hex_id))
        return _uids(data) if typ == "OK" else []

    def _locate(self, M, hex_id: str, write: bool = False, kinds=("all", "trash", "junk")) -> tuple[str, int]:
        for kind in kinds:
            folder = self._folders(M)["special"].get(kind)
            if not folder:
                continue
            self._select(M, folder, write)
            uids = self._find(M, hex_id)
            if uids:
                return folder, uids[0]
        raise GmailError("not found (no such message)")

    @staticmethod
    def _fetch(M, uids: list[int], items: str) -> list[dict]:
        if not uids:
            return []
        typ, data = M.uid("FETCH", ",".join(map(str, uids)), f"({items})")
        if typ != "OK":
            raise GmailError("Gmail did not send the messages")
        return parse_fetch(data)

    def _raw(self, M, uid: int) -> tuple[dict, bytes]:
        meta = self._fetch(M, [uid], META + " RFC822.SIZE")
        if not meta:
            raise GmailError("not found (no such message)")
        try:
            size = int(meta[0].get("RFC822.SIZE") or 0)
        except (TypeError, ValueError):
            size = 0
        if size > MAX_API_BODY:
            raise GmailError("the message is too large")
        got = self._fetch(M, [uid], "UID BODY.PEEK[]")
        raw = got[0].get("BODY[]") if got else None
        if not isinstance(raw, bytes):
            raise GmailError("Gmail sent no message body")
        return meta[0], raw

    # -- operations (each runs in one IMAP session) ------------------------------------------------------------
    async def check(self) -> dict:
        def fn(M):
            folder = self._folder(M, "all")
            typ, data = M.status(q(folder), "(MESSAGES)")
            m = re.search(rb"MESSAGES (\d+)", b" ".join(x for x in data if isinstance(x, bytes))) if typ == "OK" else None
            return {"messages_total": int(m.group(1)) if m else None, "threads_total": None}
        return await self._run(fn)

    async def profile(self) -> dict:
        return await self.check()

    async def search(self, query: str, label: str, n: int, page_token: str, spam_trash: bool) -> dict:
        def fn(M):
            parts = [query.strip()]
            if label:
                up = label.upper()
                if up in SYS_SEARCH:
                    parts.append(SYS_SEARCH[up])
                else:
                    name = self._user_label(M, label)
                    parts.append('label:"' + name.replace('"', "") + '"')
                if up in ("TRASH", "SPAM"):
                    spam_trash_ = True
                else:
                    spam_trash_ = spam_trash
            else:
                spam_trash_ = spam_trash
            if spam_trash_:
                parts.append("in:anywhere")
            raw = " ".join(p for p in parts if p)
            self._select(M, self._folder(M, "all"))
            if raw:
                M.literal = raw.encode("utf-8")
                typ, data = M.uid("SEARCH", "CHARSET", "UTF-8", "X-GM-RAW")
            else:
                typ, data = M.uid("SEARCH", "ALL")
            if typ != "OK":
                raise GmailError("Gmail refused the search")
            uids = sorted(_uids(data), reverse=True)
            start = int(page_token) if page_token.isdigit() else 0
            page = uids[start:start + n]
            got = {d["UID"]: d for d in self._fetch(M, page, f"{META} {HEADERS}")}
            msgs = [_summary(got[u]) for u in page if u in got]
            return {"messages": msgs, "next_page_token": str(start + n) if start + n < len(uids) else "",
                    "result_size_estimate": len(uids)}
        return await self._run(fn)

    def _user_label(self, M, ref: str) -> str:
        for name in self._folders(M)["user"]:
            if name.lower() == ref.strip().lower():
                return name
        raise GmailError(f"no label '{ref[:80]}' (see gmail_list_labels)")

    async def message(self, mid: str, max_chars: int) -> dict:
        def fn(M):
            _, uid = self._locate(M, mid)
            meta, raw = self._raw(M, uid)
            return full_from_mime(meta, raw, max_chars)
        return await self._run(fn)

    async def set_read(self, mid: str) -> None:
        def fn(M):
            _, uid = self._locate(M, mid, write=True)
            M.uid("STORE", str(uid), "+FLAGS", "(\\Seen)")
        await self._run(fn)

    async def thread(self, tid: str, max_chars: int) -> list[dict]:
        def fn(M):
            self._select(M, self._folder(M, "all"))
            uids = sorted(self._find(M, tid, "X-GM-THRID"))[:100]
            if not uids:
                raise GmailError("not found (no such thread)")
            out = []
            for u in uids:
                try:
                    meta, raw = self._raw(M, u)
                    out.append(full_from_mime(meta, raw, max_chars))
                except GmailError as e:
                    out.append({"error": str(e)})
            return out
        return await self._run(fn)

    async def attachment(self, mid: str, aid: str) -> tuple[dict, bytes]:
        def fn(M):
            _, uid = self._locate(M, mid)
            _, raw = self._raw(M, uid)
            msg = email.message_from_bytes(raw, policy=default)
            for i, p, is_att in _parts(msg):
                if is_att and f"p{i}" == aid:
                    data = p.get_payload(decode=True) or b""
                    if len(data) > MAX_ATTACHMENT:
                        raise GmailError(f"the attachment is {len(data) // 1024} KB; at most "
                                         f"{MAX_ATTACHMENT // 1048576} MB")
                    return {"filename": str(p.get_filename() or "attachment")[:200],
                            "mime_type": p.get_content_type()[:100]}, data
            raise GmailError("no such attachment in that message")
        return await self._run(fn)

    async def labels(self) -> list[dict]:
        def fn(M):
            f = self._folders(M)
            system = [{"id": x, "name": x, "type": "system"}
                      for x in ("INBOX", "UNREAD", "STARRED", "IMPORTANT", "SENT", "DRAFT", "TRASH", "SPAM")]
            return system + [{"id": n, "name": n, "type": "user"} for n in f["user"]]
        return await self._run(fn)

    async def reply_info(self, mid: str) -> tuple[dict, str]:
        def fn(M):
            _, uid = self._locate(M, mid)
            got = self._fetch(M, [uid], "UID X-GM-THRID BODY.PEEK[HEADER.FIELDS (FROM REPLY-TO SUBJECT MESSAGE-ID "
                                        "REFERENCES)]")
            if not got:
                raise GmailError("not found (no such message)")
            raw = next((v for k, v in got[0].items() if k.startswith("BODY[HEADER")), b"")
            return _headers_of(raw if isinstance(raw, bytes) else b""), _hex(got[0].get("X-GM-THRID"))
        return await self._run(fn)

    async def send(self, raw: bytes, thread: str = "") -> dict:
        await self._smtp(raw)
        return {"id": None, "thread_id": thread or None,
                "note": "sent with SMTP: Gmail files it in Sent; its id is not known right away"}

    async def _smtp(self, raw: bytes) -> None:
        sem = self.g._imap_slots.setdefault(self.acc.id, asyncio.Semaphore(3))
        async with sem:
            try:
                await asyncio.to_thread(smtp_send, self.g, self.acc, raw)
            except AuthFailed as e:
                self._failed(str(e))
                raise
        self.acc.last_used = time.time()

    def _append_draft(self, M, raw: bytes) -> str:
        folder = self._folder(M, "drafts")
        typ, data = M.append(q(folder), "(\\Draft \\Seen)", None, raw)
        if typ != "OK":
            raise GmailError("Gmail did not save the draft")
        m = re.search(rb"APPENDUID \d+ (\d+)", b" ".join(x for x in data if isinstance(x, bytes)))
        self._select(M, folder)
        if m:
            uids = [int(m.group(1))]
        else:                                                      # no UIDPLUS: find it by its Message-ID
            mid = email.message_from_bytes(raw, policy=default).get("Message-ID", "")
            typ, data = M.uid("SEARCH", "HEADER", "Message-ID", q(str(mid)))
            uids = _uids(data)[-1:]
        got = self._fetch(M, uids, "UID X-GM-MSGID")
        return _hex(got[0].get("X-GM-MSGID")) if got else ""

    def _drop_draft(self, M, did: str) -> None:
        folder = self._folder(M, "drafts")
        self._select(M, folder, write=True)
        uids = self._find(M, did)
        if not uids:
            raise GmailError("not found (no such draft)")
        self._move(M, uids, self._folder(M, "trash"))

    def _move(self, M, uids: list[int], dest: str) -> None:
        seq = ",".join(map(str, uids))
        if "MOVE" in M.capabilities:
            typ, _ = M.uid("MOVE", seq, q(dest))
        elif dest == self._folders(M)["special"].get("trash"):
            typ, _ = M.uid("COPY", seq, q(dest))      # in Gmail, copying into the Trash trashes the message
        else:
            # never \Deleted + EXPUNGE here: depending on Gmail's IMAP settings that deletes for good
            raise GmailError("Gmail's IMAP server does not offer MOVE")
        if typ != "OK":
            raise GmailError(f"Gmail did not move the messages to {dest}")

    async def draft_create(self, raw: bytes, thread: str = "") -> dict:
        did = await self._run(self._append_draft, raw)
        return {"draft_id": did, "message_id": did}

    async def draft_update(self, did: str, raw: bytes, thread: str = "") -> dict:
        def fn(M):
            self._drop_draft(M, did)
            return self._append_draft(M, raw)
        new = await self._run(fn)
        return {"draft_id": new, "note": "over IMAP an updated draft gets a new id"}

    async def drafts(self, n: int) -> list[dict]:
        def fn(M):
            self._select(M, self._folder(M, "drafts"))
            typ, data = M.uid("SEARCH", "ALL")
            uids = sorted(_uids(data), reverse=True)[:n]
            got = {d["UID"]: d for d in self._fetch(M, uids, f"{META} {HEADERS}")}
            out = []
            for u in uids:
                if u in got:
                    s = _summary(got[u])
                    out.append({"draft_id": s["id"], "message_id": s["id"], "to": s["to"], "subject": s["subject"],
                                "snippet": ""})
            return out
        return await self._run(fn)

    async def draft_send(self, did: str) -> dict:
        def get(M):
            self._select(M, self._folder(M, "drafts"))
            uids = self._find(M, did)
            if not uids:
                raise GmailError("not found (no such draft)")
            return self._raw(M, uids[0])
        meta, raw = await self._run(get)
        await self._smtp(raw)
        await self._run(self._drop_draft, did)
        return {"id": None, "thread_id": _hex(meta.get("X-GM-THRID")) or None}

    async def draft_delete(self, did: str) -> None:
        await self._run(self._drop_draft, did)

    async def modify(self, ids: list[str], add: list[str], rem: list[str]) -> dict:
        """add/rem: label names or system ids (INBOX, STARRED, IMPORTANT, UNREAD, ...)."""
        def fn(M):
            def split(names):
                labels, unread = [], None
                for n in names:
                    up = n.strip().upper()
                    if up == "UNREAD":
                        unread = True
                    elif up in SYS_TO_IMAP:
                        labels.append((up, SYS_TO_IMAP[up]))
                    else:
                        name = self._user_label(M, n)
                        labels.append((name, q(name)))
                return labels, unread
            add_l, add_unread = split(add)
            rem_l, rem_unread = split(rem)
            if not add_l and not rem_l and add_unread is None and rem_unread is None:
                raise GmailError("nothing to change")
            if {x[0] for x in add_l} & {x[0] for x in rem_l} or (add_unread and rem_unread):
                raise GmailError("a label cannot be added and removed at once")
            self._select(M, self._folder(M, "all"), write=True)
            uids = []
            for mid in ids:
                found = self._find(M, mid)
                if not found:
                    raise GmailError(f"not found (no message {mid})")
                uids += found
            seq = ",".join(map(str, uids))
            for sign, labels in (("+", add_l), ("-", rem_l)):
                if labels:
                    typ, _ = M.uid("STORE", seq, f"{sign}X-GM-LABELS", "(" + " ".join(x[1] for x in labels) + ")")
                    if typ != "OK":
                        raise GmailError("Gmail did not change the labels")
            if add_unread:
                M.uid("STORE", seq, "-FLAGS", "(\\Seen)")
            if rem_unread:
                M.uid("STORE", seq, "+FLAGS", "(\\Seen)")
            return {"added": sorted([x[0] for x in add_l] + ["UNREAD"] * bool(add_unread)),
                    "removed": sorted([x[0] for x in rem_l] + ["UNREAD"] * bool(rem_unread))}
        return await self._run(fn)

    async def trash(self, ids: list[str], restore: bool) -> None:
        def fn(M):
            src = self._folder(M, "trash" if restore else "all")
            self._select(M, src, write=True)
            uids = []
            for mid in ids:
                found = self._find(M, mid)
                if not found:
                    raise GmailError(f"not found (no message {mid}" + (" in the trash)" if restore else ")"))
                uids += found
            self._move(M, uids, "INBOX" if restore else self._folder(M, "trash"))
        await self._run(fn)


def smtp_send(g: "Gmail", acc: "GmailAccount", raw: bytes) -> None:
    msg = email.message_from_bytes(raw, policy=SMTP)
    rcpts = [a for _, a in getaddresses([str(v) for k in ("To", "Cc", "Bcc") for v in msg.get_all(k, [])]) if a]
    if not rcpts:
        raise GmailError("no recipient (to, cc or bcc)")
    del msg["Bcc"]                                    # envelope only: never in what the recipients get
    try:
        S = g.smtp_factory()
    except OSError as e:
        raise GmailError(f"cannot reach Gmail's SMTP server ({type(e).__name__})") from e
    try:
        try:
            S.login(acc.email, acc.app_password)
        except smtplib.SMTPAuthenticationError as e:
            raise AuthFailed("Gmail refused the address or app password (revoked, or the Google password was "
                             "changed: make a new app password)") from e
        try:
            S.sendmail(acc.email, rcpts, msg.as_bytes())
        except smtplib.SMTPRecipientsRefused as e:
            raise GmailError("Gmail refused the recipients: " + ", ".join(list(e.recipients)[:5])) from e
        except smtplib.SMTPException as e:
            raise GmailError(f"Gmail did not send it ({type(e).__name__}: {str(e)[:160]})") from e
    except OSError as e:
        raise GmailError(f"Gmail's SMTP server failed ({type(e).__name__})") from e
    finally:
        try:
            S.quit()
        except Exception:
            pass


def verify(g: "Gmail", acc: "GmailAccount") -> int | None:
    """Log in to IMAP and SMTP once (blocking; run in a thread). Returns the message count."""
    mail = ImapMail(g, acc)

    def fn(M):
        folder = mail._folder(M, "all")
        typ, data = M.status(q(folder), "(MESSAGES)")
        m = re.search(rb"MESSAGES (\d+)", b" ".join(x for x in data if isinstance(x, bytes))) if typ == "OK" else None
        return int(m.group(1)) if m else None
    count = mail._session(fn)
    try:
        S = g.smtp_factory()
    except OSError as e:
        raise GmailError(f"cannot reach Gmail's SMTP server ({type(e).__name__})") from e
    try:
        S.login(acc.email, acc.app_password)
    except smtplib.SMTPAuthenticationError as e:
        raise AuthFailed("Gmail's SMTP server refused the app password") from e
    except (smtplib.SMTPException, OSError) as e:
        raise GmailError(f"Gmail's SMTP server failed ({type(e).__name__})") from e
    finally:
        try:
            S.quit()
        except Exception:
            pass
    return count
