"""A small Gmail-like IMAP server over TCP (no TLS) for tests: X-GM-EXT-1 (X-GM-RAW, X-GM-MSGID, X-GM-THRID,
X-GM-LABELS), special-use folders with a localized All Mail name, UIDPLUS, MOVE, literals from the client. Folders
are views of one message store, the way Gmail's labels are; UIDs are global (real Gmail has them per folder, which
the client must not rely on, and it doesn't: it always searches by X-GM-MSGID in the folder it selected)."""

from __future__ import annotations

import re
import socketserver
import threading
from email import message_from_bytes
from email.policy import default

ALL = "[Gmail]/T&APw-m Postalar"        # "[Gmail]/Tüm Postalar": localized, found by its \All flag
FOLDERS = [("\\HasNoChildren", "INBOX"), ("\\HasChildren \\Noselect", "[Gmail]"),
           ("\\All \\HasNoChildren", ALL), ("\\Drafts \\HasNoChildren", "[Gmail]/Drafts"),
           ("\\HasNoChildren \\Sent", "[Gmail]/Sent Mail"), ("\\HasNoChildren \\Trash", "[Gmail]/Trash"),
           ("\\HasNoChildren \\Junk", "[Gmail]/Spam"), ("\\Flagged \\HasNoChildren", "[Gmail]/Starred"),
           ("\\HasNoChildren", "Bills"), ("\\HasNoChildren", "&AMcA9g-zel")]      # "Çözel"


class Store:
    def __init__(self, user="owner@gmail.com", password="abcdefghijklmnop"):
        self.user, self.password = user, password
        self.msgs: list[dict] = []
        self.next_uid = 100
        self.searches: list[str] = []
        self.logins = 0
        self.moves: list[str] = []
        self.lock = threading.Lock()

    def add(self, raw: bytes, labels=("\\Inbox",), flags=(), msgid=None, thrid=None, trashed=False) -> dict:
        with self.lock:
            self.next_uid += 1
            m = {"uid": self.next_uid, "msgid": msgid or 1_700_000_000_000_000 + self.next_uid,
                 "thrid": thrid or 1_800_000_000_000_000 + self.next_uid, "labels": set(labels), "flags": set(flags),
                 "raw": raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"), "trashed": trashed}
            self.msgs.append(m)
            return m

    def visible(self, folder: str) -> list[dict]:
        def ok(m):
            if folder == "[Gmail]/Trash":
                return m["trashed"]
            if m["trashed"]:
                return False
            if folder == ALL:
                return True
            if folder == "INBOX":
                return "\\Inbox" in m["labels"]
            if folder == "[Gmail]/Drafts":
                return "\\Draft" in m["labels"]
            if folder == "[Gmail]/Sent Mail":
                return "\\Sent" in m["labels"]
            if folder == "[Gmail]/Starred":
                return "\\Flagged" in m["flags"]
            return folder in m["labels"]
        return [m for m in self.msgs if ok(m)]


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unq(s: str) -> str:
    s = s.strip()
    if s.startswith('"') and s.endswith('"'):
        return re.sub(r"\\(.)", r"\1", s[1:-1])
    return s


def _names(listtext: str) -> list[str]:
    return [_unq(x) for x in re.findall(r'"(?:[^"\\]|\\.)*"|[^\s()]+', listtext)]


class Handler(socketserver.StreamRequestHandler):
    store: Store

    def w(self, line: str | bytes) -> None:
        self.wfile.write((line.encode() if isinstance(line, str) else line) + b"\r\n")

    def read_command(self) -> tuple[str, list[bytes]] | None:
        """One command line, with client literals ({n}) read in and replaced by \\x00<index>."""
        line = self.rfile.readline()
        if not line:
            return None
        lits: list[bytes] = []
        text = line.rstrip(b"\r\n")
        while (m := re.search(rb"\{(\d+)\}$", text)):
            self.w("+ go ahead")
            lits.append(self.rfile.read(int(m.group(1))))
            text = text[:m.start()] + b"\x00" + str(len(lits) - 1).encode() + self.rfile.readline().rstrip(b"\r\n")
        return text.decode(), lits

    def handle(self):
        st = self.store
        self.selected, self.write = None, False
        self.w("* OK Gimap ready")
        while True:
            got = self.read_command()
            if got is None:
                return
            text, lits = got
            tag, _, rest = text.partition(" ")
            cmd, _, args = rest.partition(" ")
            cmd = cmd.upper()
            if cmd == "UID":
                cmd2, _, args = args.partition(" ")
                cmd = "UID " + cmd2.upper()
            try:
                done = self.dispatch(tag, cmd, args, lits)
            except Exception as e:                                # a test bug: say so instead of hanging
                self.w(f"{tag} BAD {type(e).__name__}: {e}")
                continue
            if done:
                return

    def dispatch(self, tag, cmd, args, lits) -> bool:
        st = self.store
        if cmd == "CAPABILITY":                   # like Gmail: MOVE, UIDPLUS... only once logged in
            if getattr(self, "authed", False):
                self.w("* CAPABILITY IMAP4rev1 UNSELECT IDLE NAMESPACE QUOTA ID XLIST CHILDREN X-GM-EXT-1 UIDPLUS "
                       "COMPRESS=DEFLATE ENABLE MOVE CONDSTORE ESEARCH UTF8=ACCEPT LIST-EXTENDED LIST-STATUS "
                       "LITERAL- SPECIAL-USE APPENDLIMIT=35651584")
            else:
                self.w("* CAPABILITY IMAP4rev1 UNSELECT IDLE NAMESPACE QUOTA ID XLIST CHILDREN X-GM-EXT-1 XYZZY "
                       "SASL-IR AUTH=XOAUTH2 AUTH=PLAIN AUTH=PLAIN-CLIENTTOKEN AUTH=OAUTHBEARER")
            self.w(f"{tag} OK Thats all she wrote!")
        elif cmd == "LOGIN":
            user, pw = _names(args)[:2]
            st.logins += 1
            if user == st.user and pw == st.password:
                self.authed = True
                self.w(f"{tag} OK {user} authenticated (Success)")
            else:
                self.w(f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        elif cmd == "LOGOUT":
            self.w("* BYE LOGOUT Requested")
            self.w(f"{tag} OK 73 good day (Success)")
            return True
        elif cmd == "LIST":
            for flags, name in FOLDERS:
                self.w(f'* LIST ({flags}) "/" {_q(name)}')
            self.w(f"{tag} OK Success")
        elif cmd in ("SELECT", "EXAMINE"):
            name = _unq(args)
            if name not in [f[1] for f in FOLDERS] or name == "[Gmail]":
                self.w(f"{tag} NO [NONEXISTENT] Unknown Mailbox: {name} (Failure)")
            else:
                self.selected, self.write = name, cmd == "SELECT"
                self.w(f"* {len(st.visible(name))} EXISTS")
                self.w(f"{tag} OK [{'READ-WRITE' if self.write else 'READ-ONLY'}] {name} selected. (Success)")
        elif cmd == "STATUS":
            name = _unq(re.match(r'("(?:[^"\\]|\\.)*"|\S+)', args).group(1))
            self.w(f"* STATUS {_q(name)} (MESSAGES {len(st.visible(name))})")
            self.w(f"{tag} OK Success")
        elif cmd == "UID SEARCH":
            self.search(tag, args, lits)
        elif cmd == "UID FETCH":
            self.fetch(tag, args)
        elif cmd == "UID STORE":
            self.store_(tag, args)
        elif cmd in ("UID MOVE", "UID COPY"):
            st.moves.append(cmd)
            seq, _, dest = args.partition(" ")
            dest = _unq(dest)
            uids = {int(x) for x in seq.split(",")}
            for m in [m for m in st.visible(self.selected) if m["uid"] in uids]:
                if dest == "[Gmail]/Trash":
                    m["trashed"] = True
                elif dest == "INBOX":
                    m["trashed"] = False
                    m["labels"].add("\\Inbox")
                else:
                    m["labels"].add(dest)
                if self.selected not in (ALL, "[Gmail]/Trash") and dest != "[Gmail]/Trash":
                    m["labels"].discard({"INBOX": "\\Inbox", "[Gmail]/Drafts": "\\Draft"}.get(self.selected,
                                                                                           self.selected))
            self.w(f"{tag} OK [COPYUID 1 {seq} {seq}] (Success)")
        elif cmd == "APPEND":
            name = _unq(re.match(r'("(?:[^"\\]|\\.)*"|\S+)', args).group(1))
            flags = re.search(r"\(([^)]*)\)", args)
            fl = set(flags.group(1).split()) if flags else set()
            labels = {"\\Draft"} if name == "[Gmail]/Drafts" else ({"\\Inbox"} if name == "INBOX" else {name})
            m = st.add(lits[0], labels=labels, flags=fl)
            self.w(f"{tag} OK [APPENDUID 1 {m['uid']}] (Success)")
        elif cmd in ("NOOP", "CLOSE", "EXPUNGE"):
            self.w(f"{tag} OK Success")
        else:
            self.w(f"{tag} BAD Unknown command {cmd}")
        return False

    # -- the Gmail parts --------------------------------------------------------------------------------------
    def search(self, tag, args, lits):
        st = self.store
        msgs = st.visible(self.selected)
        m = re.match(r"CHARSET UTF-8 X-GM-RAW \x00(\d+)$", args)
        if m:
            query = lits[int(m.group(1))].decode()
            st.searches.append(query)
            hits = [x for x in (st.msgs if "in:anywhere" in query else msgs) if self.matches(x, query)]
        elif args.startswith("X-GM-MSGID "):
            hits = [x for x in msgs if x["msgid"] == int(args.split()[1])]
        elif args.startswith("X-GM-THRID "):
            hits = [x for x in msgs if x["thrid"] == int(args.split()[1])]
        elif args == "ALL":
            hits = msgs
        else:
            self.w(f"{tag} BAD unsupported search {args}")
            return
        self.w("* SEARCH " + " ".join(str(x["uid"]) for x in hits))
        self.w(f"{tag} OK SEARCH completed (Success)")

    @staticmethod
    def matches(m, query: str) -> bool:
        head = message_from_bytes(m["raw"], policy=default)
        for tok in re.findall(r'\w+:"[^"]*"|\S+', query):
            k, _, v = tok.partition(":")
            v = v.strip('"')
            if tok == "in:anywhere":
                continue
            if k == "is" and v == "unread" and "\\Seen" in m["flags"]:
                return False
            if k == "from" and v.lower() not in str(head["From"]).lower():
                return False
            if k == "label" and v not in m["labels"]:
                return False
            if k == "in" and v == "inbox" and "\\Inbox" not in m["labels"]:
                return False
            if not _ and tok.lower() not in m["raw"].decode("utf-8", "replace").lower():
                return False
        return True

    def fetch(self, tag, args):
        st = self.store
        seq, _, items = args.partition(" ")
        uids = [int(x) for x in seq.split(",")]
        msgs = {m["uid"]: m for m in st.visible(self.selected)}
        for i, uid in enumerate(uids, 1):
            m = msgs.get(uid)
            if not m:
                continue
            parts = [f"UID {uid}"]
            if "X-GM-MSGID" in items:
                parts.append(f"X-GM-MSGID {m['msgid']}")
            if "X-GM-THRID" in items:
                parts.append(f"X-GM-THRID {m['thrid']}")
            if "X-GM-LABELS" in items:
                labs = " ".join(x if x.startswith("\\") else _q(x) for x in sorted(m["labels"]))
                parts.append(f"X-GM-LABELS ({labs})")
            if "FLAGS" in items:
                parts.append(f"FLAGS ({' '.join(sorted(m['flags']))})")
            if "RFC822.SIZE" in items:
                parts.append(f"RFC822.SIZE {len(m['raw'])}")
            lit = None
            hf = re.search(r"BODY\.PEEK\[HEADER\.FIELDS \(([^)]*)\)\]", items)
            if hf:
                want = hf.group(1).upper().split()
                msg = message_from_bytes(m["raw"], policy=default)
                lit = b"".join(f"{k}: {v}\r\n".encode() for k, v in msg.items() if k.upper() in want) + b"\r\n"
                key = f"BODY[HEADER.FIELDS ({hf.group(1).upper()})]"
            elif "BODY.PEEK[]" in items:
                lit, key = m["raw"], "BODY[]"
            head = f"* {i} FETCH (" + " ".join(parts)
            if lit is not None:
                self.wfile.write(f"{head} {key} {{{len(lit)}}}\r\n".encode() + lit + b")\r\n")
            else:
                self.w(head + ")")
        self.w(f"{tag} OK Success")

    def store_(self, tag, args):
        st = self.store
        seq, op, rest = args.split(" ", 2)
        uids = {int(x) for x in seq.split(",")}
        vals = _names(rest)
        for m in st.visible(self.selected):
            if m["uid"] not in uids:
                continue
            target = m["labels"] if "X-GM-LABELS" in op else m["flags"]
            for v in vals:
                (target.add if op.startswith("+") else target.discard)(v)
            self.w(f"* 1 FETCH (UID {m['uid']} FLAGS ({' '.join(sorted(m['flags']))}))")
        self.w(f"{tag} OK Success")


class FakeImap:
    def __init__(self, store: Store):
        handler = type("H", (Handler,), {"store": store})
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeSmtp:
    """Records what smtplib would send (login + sendmail)."""
    sent: list = []
    password = "abcdefghijklmnop"

    def __init__(self):
        self.logged_in = False

    def login(self, user, pw):
        import smtplib
        if pw != self.password:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
        self.logged_in = True

    def sendmail(self, frm, rcpts, data):
        assert self.logged_in
        type(self).sent.append((frm, list(rcpts), data))
        return {}

    def quit(self):
        pass
