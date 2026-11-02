"""Shared memory for the owner's agents: messages between agents and project notes in folders.

Several agents (Claude Code, Hermes, ...) work on the same servers and repos without seeing each other's
conversations. Here they leave each other messages ("I'm restarting CT 118, don't deploy now") and keep notes
in a folder tree they build themselves (e.g. servers/pve1/ct-118-telerouter/telerouter, repos/telerouter):
architecture, what was changed recently, how to undo it.

Who is who: an agent telerouter runs is `agent:<id>`, any other MCP token `token:<id>`, the web UI `owner`.
Several sessions of one agent share its token and therefore its identity and inbox.

SQLite in /data/memory.db (0600). Calls are synchronous and short; async callers go through `run()`, which
uses a worker thread so the event loop never waits on the disk. Edits keep the previous text (`revisions`,
the last KEEP_REVISIONS per note) and deleted notes stay in the trash for TRASH_DAYS, so an agent that
overwrites or deletes the wrong note can be undone from the web UI.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

OWNER = "owner"
EVERYONE = "*"

MAX_BODY = 100_000           # characters per note
MAX_TITLE = 120
MAX_MESSAGE = 4000
MAX_NOTES = 5000             # live notes
MAX_FOLDERS = 2000
MAX_DEPTH = 12
MAX_PATH = 400
KEEP_MESSAGES = 5000
KEEP_REVISIONS = 20
TRASH_DAYS = 30
WRITES_PER_MIN = 60          # notes/folders, per identity
MESSAGES_PER_MIN = 20

SEGMENT_RE = re.compile(r"^[\w][\w .@+()-]{0,63}$")
TITLE_RE = re.compile(r"^[^\x00-\x1f]{1,120}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    path TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '', created REAL NOT NULL, created_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY, folder TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
    version INTEGER NOT NULL, created REAL NOT NULL, created_by TEXT NOT NULL,
    updated REAL NOT NULL, updated_by TEXT NOT NULL, deleted REAL NOT NULL DEFAULT 0, deleted_by TEXT NOT NULL DEFAULT '');
CREATE UNIQUE INDEX IF NOT EXISTS notes_title ON notes(folder, title COLLATE NOCASE) WHERE deleted = 0;
CREATE INDEX IF NOT EXISTS notes_updated ON notes(updated);
CREATE TABLE IF NOT EXISTS revisions (
    note_id TEXT NOT NULL, version INTEGER NOT NULL, folder TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
    at REAL NOT NULL, by TEXT NOT NULL, PRIMARY KEY (note_id, version));
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, sender_key TEXT NOT NULL, sender TEXT NOT NULL,
    recipient_key TEXT NOT NULL, recipient TEXT NOT NULL, project TEXT NOT NULL DEFAULT '', text TEXT NOT NULL,
    reply_to INTEGER);
CREATE TABLE IF NOT EXISTS reads (
    message_id INTEGER NOT NULL, reader_key TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (message_id, reader_key));
CREATE TABLE IF NOT EXISTS peers (
    key TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL DEFAULT '', status_at REAL NOT NULL DEFAULT 0,
    seen REAL NOT NULL DEFAULT 0);
"""


class MemError(Exception):
    """A user-facing refusal (bad path, version conflict, not found, limit reached)."""


class Who:
    """The identity behind a call: a stable key and the name others see."""

    def __init__(self, key: str, name: str):
        self.key = key
        self.name = name

    def __repr__(self) -> str:
        return f"Who({self.key!r}, {self.name!r})"


def norm_path(path: str) -> str:
    """'servers / pve1//ct-118 ' -> 'servers/pve1/ct-118'; '' is the top level. Refuses '..', dot-names, odd
    characters and very deep paths."""
    parts = [" ".join(p.split()) for p in str(path or "").replace("\\", "/").split("/")]
    parts = [p for p in parts if p]
    for p in parts:
        if not SEGMENT_RE.match(p):
            raise MemError(f"bad folder name '{p[:70]}': 1-64 letters, digits, space . _ - @ + ( ), "
                              "not starting with a dot or a symbol")
    if len(parts) > MAX_DEPTH:
        raise MemError(f"folders nest at most {MAX_DEPTH} deep")
    out = "/".join(parts)
    if len(out) > MAX_PATH:
        raise MemError("the folder path is too long")
    return out


def norm_title(title: str) -> str:
    t = " ".join(str(title or "").split())
    if not TITLE_RE.match(t):
        raise MemError(f"a note title is 1-{MAX_TITLE} characters on one line")
    return t


def parents(path: str) -> list[str]:
    """'a/b/c' -> ['a', 'a/b', 'a/b/c']."""
    parts = path.split("/") if path else []
    return ["/".join(parts[:i + 1]) for i in range(len(parts))]


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SharedMemory:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        if new:
            os.chmod(self.path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._rate: dict[tuple[str, str], list[float]] = {}
        self.purge()

    def close(self) -> None:
        with self._lock:
            self.db.close()

    async def run(self, fn, *args, **kwargs):
        """Call a method off the event loop."""
        return await asyncio.to_thread(fn, *args, **kwargs)

    # -- helpers ------------------------------------------------------------------------------------------
    def _tx(self):
        return _Tx(self)

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, args).fetchall()

    def _one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        return self.db.execute(sql, args).fetchone()

    def _rate_ok(self, kind: str, who: Who, limit: int) -> None:
        if who.key == OWNER:
            return
        now = time.time()
        recent = [t for t in self._rate.get((kind, who.key), []) if now - t < 60]
        if len(recent) >= limit:
            raise MemError(f"too many {kind} in a minute; slow down")
        recent.append(now)
        self._rate[(kind, who.key)] = recent

    def _ensure_folders(self, path: str, who: Who) -> None:
        now = time.time()
        for p in parents(path):
            if not self._one("SELECT 1 FROM folders WHERE path=?", (p,)):
                if self._one("SELECT count(*) AS n FROM folders")["n"] >= MAX_FOLDERS:
                    raise MemError(f"the shared memory holds at most {MAX_FOLDERS} folders")
                self.db.execute("INSERT INTO folders(path, description, created, created_by) VALUES (?,?,?,?)",
                                (p, "", now, who.name))

    def _note(self, note_id: str, deleted: bool = False) -> sqlite3.Row:
        row = self._one("SELECT * FROM notes WHERE id=? AND " + ("deleted>0" if deleted else "deleted=0"),
                        (str(note_id or ""),))
        if not row:
            raise MemError(f"no note '{note_id}'" + (" in the trash" if deleted else ""))
        return row

    def _find(self, folder: str, title: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM notes WHERE deleted=0 AND folder=? AND title=? COLLATE NOCASE",
                         (folder, title))

    def _keep_revision(self, n: sqlite3.Row, at: float, by: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO revisions(note_id, version, folder, title, body, at, by) "
                        "VALUES (?,?,?,?,?,?,?)", (n["id"], n["version"], n["folder"], n["title"], n["body"],
                                                   n["updated"], n["updated_by"]))
        self.db.execute("DELETE FROM revisions WHERE note_id=? AND version <= ?",
                        (n["id"], n["version"] - KEEP_REVISIONS))

    @staticmethod
    def _meta(n: sqlite3.Row) -> dict:
        return {"id": n["id"], "folder": n["folder"], "title": n["title"], "version": n["version"],
                "size": len(n["body"]), "updated": n["updated"], "updated_by": n["updated_by"],
                "created": n["created"], "created_by": n["created_by"]}

    @classmethod
    def _full(cls, n: sqlite3.Row) -> dict:
        return {**cls._meta(n), "body": n["body"]}

    def purge(self) -> None:
        """Drop trash older than TRASH_DAYS and the oldest messages beyond KEEP_MESSAGES."""
        with self._lock, self._tx():
            old = time.time() - TRASH_DAYS * 86400
            ids = [r["id"] for r in self._q("SELECT id FROM notes WHERE deleted>0 AND deleted<?", (old,))]
            for i in ids:
                self.db.execute("DELETE FROM revisions WHERE note_id=?", (i,))
                self.db.execute("DELETE FROM notes WHERE id=?", (i,))
            cut = self._one("SELECT id FROM messages ORDER BY id DESC LIMIT 1 OFFSET ?", (KEEP_MESSAGES,))
            if cut:
                self.db.execute("DELETE FROM reads WHERE message_id<=?", (cut["id"],))
                self.db.execute("DELETE FROM messages WHERE id<=?", (cut["id"],))

    # -- peers --------------------------------------------------------------------------------------------
    def touch(self, who: Who) -> None:
        """Note that `who` used the shared memory now (shown as 'last seen')."""
        with self._lock:
            self.db.execute("INSERT INTO peers(key, name, seen) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
                            "name=excluded.name, seen=excluded.seen", (who.key, who.name, time.time()))

    def set_status(self, who: Who, text: str) -> dict:
        text = " ".join(str(text or "").split())[:300]
        now = time.time()
        with self._lock:
            self.db.execute("INSERT INTO peers(key, name, status, status_at, seen) VALUES (?,?,?,?,?) "
                            "ON CONFLICT(key) DO UPDATE SET name=excluded.name, status=excluded.status, "
                            "status_at=excluded.status_at, seen=excluded.seen", (who.key, who.name, text, now, now))
        return {"status": text, "at": now}

    def peers(self) -> dict[str, dict]:
        with self._lock:
            return {r["key"]: dict(r) for r in self._q("SELECT * FROM peers")}

    # -- folders ------------------------------------------------------------------------------------------
    def create_folder(self, who: Who, path: str, description: str | None = None) -> dict:
        path = norm_path(path)
        if not path:
            raise MemError("name the folder, e.g. servers/pve1")
        desc = None if description is None else str(description).strip()[:2000]
        with self._lock, self._tx():
            self._rate_ok("writes", who, WRITES_PER_MIN)
            existed = bool(self._one("SELECT 1 FROM folders WHERE path=?", (path,)))
            self._ensure_folders(path, who)
            if desc is not None:
                self.db.execute("UPDATE folders SET description=? WHERE path=?", (desc, path))
            row = self._one("SELECT * FROM folders WHERE path=?", (path,))
        return {"path": path, "description": row["description"], "created": not existed}

    def list_folder(self, path: str = "", depth: int = 1) -> dict:
        """The folder's description, its notes (no bodies) and its sub-folders down to `depth` levels."""
        path = norm_path(path)
        depth = max(1, min(int(depth or 1), 6))
        with self._lock:
            me = self._one("SELECT * FROM folders WHERE path=?", (path,)) if path else None
            if path and not me:
                raise MemError(f"no folder '{path}'" + self._hint(path))
            prefix = path + "/" if path else ""
            rows = self._q("SELECT * FROM folders WHERE path LIKE ? ESCAPE '\\' ORDER BY path",
                           (_like(prefix) + "%",))
            notes = self._q("SELECT * FROM notes WHERE deleted=0 AND (folder=? OR folder LIKE ? ESCAPE '\\') "
                            "ORDER BY title COLLATE NOCASE", (path, _like(prefix) + "%"))
        by_folder: dict[str, list[dict]] = {}
        for n in notes:
            by_folder.setdefault(n["folder"], []).append(self._meta(n))

        def node(p: str, desc: str, level: int) -> dict:
            out = {"path": p, "description": desc, "notes": by_folder.get(p, [])}
            kids = [r for r in rows if r["path"].rsplit("/", 1)[0] == p and "/" in r["path"]] if p else \
                [r for r in rows if "/" not in r["path"]]
            if level < depth:
                out["folders"] = [node(r["path"], r["description"], level + 1) for r in kids]
            else:
                out["folders"] = [{"path": r["path"], "description": r["description"],
                                   "notes_inside": sum(len(v) for k, v in by_folder.items()
                                                       if k == r["path"] or k.startswith(r["path"] + "/"))}
                                  for r in kids]
            return out
        return node(path, me["description"] if me else "", 0)

    def _hint(self, path: str) -> str:
        last = path.rsplit("/", 1)[-1]
        near = [r["path"] for r in self._q("SELECT path FROM folders WHERE path LIKE ? ESCAPE '\\' LIMIT 5",
                                           ("%" + _like(last) + "%",))]
        return f" (similar: {', '.join(near)})" if near else ""

    def folders(self) -> list[dict]:
        with self._lock:
            rows = self._q("SELECT * FROM folders ORDER BY path")
            counts = {r["folder"]: r["n"] for r in self._q(
                "SELECT folder, count(*) AS n FROM notes WHERE deleted=0 GROUP BY folder")}
        return [{"path": r["path"], "description": r["description"], "notes": counts.get(r["path"], 0)}
                for r in rows]

    def move_folder(self, who: Who, path: str, new_path: str) -> dict:
        path, new_path = norm_path(path), norm_path(new_path)
        if not path or not new_path:
            raise MemError("name both folders")
        if new_path == path:
            return {"path": path}
        if new_path.startswith(path + "/"):
            raise MemError("a folder can't move into itself")
        with self._lock, self._tx():
            self._rate_ok("writes", who, WRITES_PER_MIN)
            if not self._one("SELECT 1 FROM folders WHERE path=?", (path,)):
                raise MemError(f"no folder '{path}'")
            if self._one("SELECT 1 FROM folders WHERE path=?", (new_path,)):
                raise MemError(f"'{new_path}' exists already")
            parent = new_path.rsplit("/", 1)[0] if "/" in new_path else ""
            if parent:
                self._ensure_folders(parent, who)
            n = len(path)
            for table, col in (("folders", "path"), ("notes", "folder"), ("revisions", "folder")):
                self.db.execute(f"UPDATE {table} SET {col} = ? || substr({col}, ?) "
                                f"WHERE {col} = ? OR {col} LIKE ? ESCAPE '\\'",
                                (new_path, n + 1, path, _like(path + "/") + "%"))
        return {"path": new_path, "moved_from": path}

    def delete_folder(self, who: Who, path: str, recursive: bool = False) -> dict:
        """Empty folders only, unless `recursive` (web UI): then its notes go to the trash."""
        path = norm_path(path)
        if not path:
            raise MemError("the top level can't be deleted")
        like = _like(path + "/") + "%"
        with self._lock, self._tx():
            self._rate_ok("writes", who, WRITES_PER_MIN)
            if not self._one("SELECT 1 FROM folders WHERE path=?", (path,)):
                raise MemError(f"no folder '{path}'")
            notes = self._q("SELECT * FROM notes WHERE deleted=0 AND (folder=? OR folder LIKE ? ESCAPE '\\')",
                            (path, like))
            subs = self._one("SELECT count(*) AS n FROM folders WHERE path LIKE ? ESCAPE '\\'", (like,))["n"]
            if (notes or subs) and not recursive:
                raise MemError(f"'{path}' is not empty ({len(notes)} notes, {subs} sub-folders): delete or move "
                                  "those first")
            now = time.time()
            for n in notes:
                self.db.execute("UPDATE notes SET deleted=?, deleted_by=? WHERE id=?", (now, who.name, n["id"]))
            self.db.execute("DELETE FROM folders WHERE path=? OR path LIKE ? ESCAPE '\\'", (path, like))
        return {"deleted": path, "notes_to_trash": len(notes)}

    # -- notes ------------------------------------------------------------------------------------------------
    def read_note(self, note_id: str = "", folder: str = "", title: str = "") -> dict:
        with self._lock:
            if note_id:
                return self._full(self._note(note_id))
            f, t = norm_path(folder), norm_title(title)
            n = self._find(f, t)
            if not n:
                raise MemError(f"no note '{t}' in '{f or '(top level)'}'")
            return self._full(n)

    def write_note(self, who: Who, folder: str, title: str, body: str, version: int | None = None) -> dict:
        """Create a note, or replace one when `version` is the version you read (so nobody's edit is lost)."""
        folder, title = norm_path(folder), norm_title(title)
        body = str(body or "")
        if len(body) > MAX_BODY:
            raise MemError(f"a note holds at most {MAX_BODY} characters; split it")
        now = time.time()
        with self._lock, self._tx():
            self._rate_ok("writes", who, WRITES_PER_MIN)
            n = self._find(folder, title)
            if n is None:
                if self._one("SELECT count(*) AS n FROM notes WHERE deleted=0")["n"] >= MAX_NOTES:
                    raise MemError(f"the shared memory holds at most {MAX_NOTES} notes")
                self._ensure_folders(folder, who)
                nid = "n" + secrets.token_hex(5)
                self.db.execute("INSERT INTO notes(id, folder, title, body, version, created, created_by, updated, "
                                "updated_by) VALUES (?,?,?,?,1,?,?,?,?)",
                                (nid, folder, title, body, now, who.name, now, who.name))
                return {**self._meta(self._note(nid)), "created": True}
            if version is None:
                raise MemError(f"'{title}' exists already (version {n['version']}, last changed by "
                                  f"{n['updated_by']}): read it and pass its version to replace it, or use "
                                  "append_note to add to it")
            if int(version) != n["version"]:
                raise MemError(f"'{title}' changed since you read it (now version {n['version']}, by "
                                  f"{n['updated_by']}): read it again and merge your change")
            self._keep_revision(n, now, who.name)
            self.db.execute("UPDATE notes SET body=?, version=version+1, updated=?, updated_by=? WHERE id=?",
                            (body, now, who.name, n["id"]))
            return {**self._meta(self._note(n["id"])), "created": False}

    def update_note(self, who: Who, note_id: str, version: int, body: str | None = None,
                    title: str | None = None, folder: str | None = None) -> dict:
        """Change a note's text and/or move/rename it (web UI and move_note)."""
        if body is not None and len(body) > MAX_BODY:
            raise MemError(f"a note holds at most {MAX_BODY} characters; split it")
        now = time.time()
        with self._lock, self._tx():
            self._rate_ok("writes", who, WRITES_PER_MIN)
            n = self._note(note_id)
            if int(version) != n["version"]:
                raise MemError(f"'{n['title']}' changed since you read it (now version {n['version']}, by "
                                  f"{n['updated_by']}): read it again")
            new_folder = n["folder"] if folder is None else norm_path(folder)
            new_title = n["title"] if title is None else norm_title(title)
            other = self._find(new_folder, new_title)
            if other and other["id"] != n["id"]:
                raise MemError(f"there is already a note '{new_title}' in '{new_folder or '(top level)'}'")
            self._ensure_folders(new_folder, who)
            self._keep_revision(n, now, who.name)
            self.db.execute("UPDATE notes SET folder=?, title=?, body=?, version=version+1, updated=?, updated_by=? "
                            "WHERE id=?", (new_folder, new_title, n["body"] if body is None else str(body),
                                           now, who.name, n["id"]))
            return self._meta(self._note(n["id"]))

    def append_note(self, who: Who, folder: str, title: str, text: str) -> dict:
        """Add a dated entry at the end of a note (created when missing): the change-log way to write."""
        folder, title = norm_path(folder), norm_title(title)
        text = str(text or "").strip()
        if not text:
            raise MemError("nothing to append")
        stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
        entry = f"### {stamp} · {who.name}\n{text}\n"
        now = time.time()
        with self._lock, self._tx():
            self._rate_ok("writes", who, WRITES_PER_MIN)
            n = self._find(folder, title)
            if n is None:
                if len(entry) > MAX_BODY:
                    raise MemError(f"a note holds at most {MAX_BODY} characters")
                if self._one("SELECT count(*) AS n FROM notes WHERE deleted=0")["n"] >= MAX_NOTES:
                    raise MemError(f"the shared memory holds at most {MAX_NOTES} notes")
                self._ensure_folders(folder, who)
                nid = "n" + secrets.token_hex(5)
                self.db.execute("INSERT INTO notes(id, folder, title, body, version, created, created_by, updated, "
                                "updated_by) VALUES (?,?,?,?,1,?,?,?,?)",
                                (nid, folder, title, entry, now, who.name, now, who.name))
                return {**self._meta(self._note(nid)), "created": True}
            body = n["body"].rstrip("\n") + "\n\n" + entry if n["body"].strip() else entry
            if len(body) > MAX_BODY:
                raise MemError(f"'{title}' would grow past {MAX_BODY} characters: summarize the old entries "
                                  "(write_note with its version) or start a new note")
            self._keep_revision(n, now, who.name)
            self.db.execute("UPDATE notes SET body=?, version=version+1, updated=?, updated_by=? WHERE id=?",
                            (body, now, who.name, n["id"]))
            return {**self._meta(self._note(n["id"])), "created": False}

    def delete_note(self, who: Who, note_id: str) -> dict:
        with self._lock, self._tx():
            self._rate_ok("writes", who, WRITES_PER_MIN)
            n = self._note(note_id)
            self.db.execute("UPDATE notes SET deleted=?, deleted_by=? WHERE id=?", (time.time(), who.name, n["id"]))
        return {"deleted": n["id"], "title": n["title"], "folder": n["folder"],
                "note": f"in the trash for {TRASH_DAYS} days; the owner can restore it"}

    def trash(self) -> list[dict]:
        with self._lock:
            rows = self._q("SELECT * FROM notes WHERE deleted>0 ORDER BY deleted DESC LIMIT 500")
        return [{**self._meta(r), "deleted": r["deleted"], "deleted_by": r["deleted_by"]} for r in rows]

    def restore_note(self, who: Who, note_id: str) -> dict:
        with self._lock, self._tx():
            n = self._note(note_id, deleted=True)
            title = n["title"]
            while self._find(n["folder"], title):
                title = (title[:100] + " (restored)")
            self._ensure_folders(n["folder"], who)
            self.db.execute("UPDATE notes SET deleted=0, deleted_by='', title=? WHERE id=?", (title, n["id"]))
            return self._meta(self._note(n["id"]))

    def revisions(self, note_id: str) -> list[dict]:
        with self._lock:
            rows = self._q("SELECT note_id, version, folder, title, at, by, length(body) AS size FROM revisions "
                           "WHERE note_id=? ORDER BY version DESC", (note_id,))
        return [dict(r) for r in rows]

    def revision(self, note_id: str, version: int) -> dict:
        with self._lock:
            r = self._one("SELECT * FROM revisions WHERE note_id=? AND version=?", (note_id, int(version)))
        if not r:
            raise MemError("no such version")
        return dict(r)

    def search(self, query: str, folder: str = "", limit: int = 20) -> list[dict]:
        """Notes whose title, folder or text contain every word of `query` (case-insensitive)."""
        words = [w for w in str(query or "").split() if w][:8]
        if not words:
            raise MemError("search for something")
        folder = norm_path(folder)
        where, args = ["deleted=0"], []
        if folder:
            where.append("(folder=? OR folder LIKE ? ESCAPE '\\')")
            args += [folder, _like(folder + "/") + "%"]
        for w in words:
            where.append("(title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' OR folder LIKE ? ESCAPE '\\')")
            args += ["%" + _like(w) + "%"] * 3
        with self._lock:
            rows = self._q(f"SELECT * FROM notes WHERE {' AND '.join(where)} ORDER BY updated DESC LIMIT ?",
                           (*args, max(1, min(int(limit or 20), 50))))
        out = []
        for r in rows:
            body = r["body"]
            i = body.lower().find(words[0].lower())
            snippet = body[max(0, i - 80):i + 160] if i >= 0 else body[:200]
            out.append({**self._meta(r), "snippet": " ".join(snippet.split())})
        return out

    def recent(self, folder: str = "", since: float = 0, limit: int = 30) -> list[dict]:
        folder = norm_path(folder)
        where, args = ["deleted=0", "updated>=?"], [since]
        if folder:
            where.append("(folder=? OR folder LIKE ? ESCAPE '\\')")
            args += [folder, _like(folder + "/") + "%"]
        with self._lock:
            rows = self._q(f"SELECT * FROM notes WHERE {' AND '.join(where)} ORDER BY updated DESC LIMIT ?",
                           (*args, max(1, min(int(limit or 30), 100))))
        return [self._meta(r) for r in rows]

    def stats(self) -> dict:
        with self._lock:
            return {"notes": self._one("SELECT count(*) AS n FROM notes WHERE deleted=0")["n"],
                    "folders": self._one("SELECT count(*) AS n FROM folders")["n"],
                    "trash": self._one("SELECT count(*) AS n FROM notes WHERE deleted>0")["n"],
                    "messages": self._one("SELECT count(*) AS n FROM messages")["n"]}

    # -- messages -------------------------------------------------------------------------------------------
    def send(self, who: Who, to: Who | None, text: str, project: str = "", reply_to: int | None = None) -> dict:
        """`to` None = everyone."""
        text = str(text or "").strip()
        if not text:
            raise MemError("the message is empty")
        if len(text) > MAX_MESSAGE:
            raise MemError(f"a message is at most {MAX_MESSAGE} characters; put long details in a note")
        project = " ".join(str(project or "").split())[:120]
        with self._lock, self._tx():
            self._rate_ok("messages", who, MESSAGES_PER_MIN)
            if reply_to is not None and not self._one("SELECT 1 FROM messages WHERE id=?", (int(reply_to),)):
                raise MemError(f"no message #{reply_to}")
            cur = self.db.execute(
                "INSERT INTO messages(at, sender_key, sender, recipient_key, recipient, project, text, reply_to) "
                "VALUES (?,?,?,?,?,?,?,?)", (time.time(), who.key, who.name, to.key if to else EVERYONE,
                                             to.name if to else "everyone", project, text,
                                             int(reply_to) if reply_to is not None else None))
            mid = cur.lastrowid
            self.db.execute("INSERT OR IGNORE INTO reads(message_id, reader_key, at) VALUES (?,?,?)",
                            (mid, who.key, time.time()))
        return {"id": mid, "to": to.name if to else "everyone"}

    def _inbox_where(self, key: str) -> tuple[str, tuple]:
        return ("(recipient_key=? OR recipient_key=?) AND sender_key<>?", (key, EVERYONE, key))

    def unread(self, who: Who) -> int:
        w, a = self._inbox_where(who.key)
        with self._lock:
            return self._one(f"SELECT count(*) AS n FROM messages m WHERE {w} AND NOT EXISTS "
                             "(SELECT 1 FROM reads r WHERE r.message_id=m.id AND r.reader_key=?)",
                             (*a, who.key))["n"]

    def inbox(self, who: Who, include_read: bool = False, limit: int = 20, project: str = "",
              mark_read: bool = True) -> list[dict]:
        """Messages to `who` or everyone (not its own), oldest first; returned unread ones become read."""
        w, a = self._inbox_where(who.key)
        args: list = [*a]
        if project:
            w += " AND project LIKE ? ESCAPE '\\'"
            args.append("%" + _like(project.strip()) + "%")
        if not include_read:
            w += " AND NOT EXISTS (SELECT 1 FROM reads r WHERE r.message_id=m.id AND r.reader_key=?)"
            args.append(who.key)
        limit = max(1, min(int(limit or 20), 100))
        with self._lock, self._tx():
            rows = self._q(f"SELECT m.*, EXISTS (SELECT 1 FROM reads r WHERE r.message_id=m.id AND r.reader_key=?) "
                           f"AS was_read FROM messages m WHERE {w} ORDER BY id DESC LIMIT ?",
                           (who.key, *args, limit))
            if mark_read:
                now = time.time()
                for r in rows:
                    self.db.execute("INSERT OR IGNORE INTO reads(message_id, reader_key, at) VALUES (?,?,?)",
                                    (r["id"], who.key, now))
        return [self._msg(r) for r in reversed(rows)]

    @staticmethod
    def _msg(r: sqlite3.Row) -> dict:
        d = {"id": r["id"], "at": r["at"], "from": r["sender"], "to": r["recipient"], "project": r["project"],
             "text": r["text"], "reply_to": r["reply_to"]}
        if "was_read" in r.keys():
            d["new"] = not r["was_read"]
        return d

    def all_messages(self, after: int = 0, limit: int = 200) -> list[dict]:
        """Every message (the owner's view), with who has read it."""
        with self._lock:
            rows = self._q("SELECT * FROM (SELECT * FROM messages WHERE id>? ORDER BY id DESC LIMIT ?) ORDER BY id",
                           (int(after), max(1, min(int(limit), 500))))
            reads: dict[int, list[str]] = {}
            if rows:
                for r in self._q("SELECT r.message_id, r.reader_key, coalesce(p.name, r.reader_key) AS name "
                                 "FROM reads r LEFT JOIN peers p ON p.key=r.reader_key WHERE r.message_id>=?",
                                 (rows[0]["id"],)):
                    reads.setdefault(r["message_id"], []).append((r["reader_key"], r["name"]))
        out = []
        for r in rows:
            d = self._msg(r)
            d["from_key"], d["to_key"] = r["sender_key"], r["recipient_key"]
            d["read_by"] = [n for k, n in reads.get(r["id"], []) if k != r["sender_key"]]
            out.append(d)
        return out


class _Tx:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK around a block (the connection is in autocommit mode)."""

    def __init__(self, mem: SharedMemory):
        self.db = mem.db

    def __enter__(self):
        self.db.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.db.execute("ROLLBACK" if exc_type else "COMMIT")
        return False
