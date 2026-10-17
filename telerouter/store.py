"""JSON persistence: /data/config.json (0600, atomic writes) plus small history files."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

from .models import Agent, Client, Config, Contact, Integration, Machine, McpToken

log = logging.getLogger("telerouter.store")

HISTORY_MAX = 300


def write_private(path: Path, data: str | bytes) -> None:
    """Atomically write `data` to `path` with mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode() if isinstance(data, str) else data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def migrate(cfg: Config) -> bool:
    """0.3 -> 0.4: agents carried their own SSH target; now they run on a Machine."""
    changed = False
    v = cfg.voice                                  # 0.4.4 -> 0.4.5: old defaults cut people off mid-sentence
    if v.end_silence_ms == 900:
        v.end_silence_ms, changed = 1200, True
    if v.max_utterance == 20.0:
        v.max_utterance, changed = 45.0, True
    for a in cfg.agents:
        if a.machine_id and any(m.id == a.machine_id for m in cfg.machines):
            continue
        m = next((m for m in cfg.machines if (m.host, m.port, m.username) == (a.host, a.port, a.username)), None)
        if m is None and a.host and a.username:
            m = Machine(name=a.host, host=a.host, port=a.port, username=a.username, host_key=a.host_key,
                        agent_kind=a.kind)
            cfg.machines.append(m)
        if m is not None:
            a.machine_id = m.id
            changed = True
    return changed


class Store:
    def __init__(self, data_dir: str):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        self.path = self.dir / "config.json"
        self.history_path = self.dir / "history.json"
        self._lock = threading.Lock()
        self.config = self._load()
        self.history: list[dict] = self._load_list(self.history_path)

    def _load(self) -> Config:
        if self.path.exists():
            try:
                raw = self.path.read_text()
                cfg = Config.model_validate_json(raw)
                if migrate(cfg):
                    write_private(self.path.with_name(f"config.json.bak-{int(time.time())}"), raw)
                    self._write(cfg)
                return cfg
            except Exception as e:
                # never silently start empty over a broken file: keep a copy for the owner
                backup = self.path.with_suffix(f".broken-{int(time.time())}")
                self.path.rename(backup)
                log.error("config.json could not be read (%s); moved it to %s", e, backup.name)
        cfg = Config()
        self._write(cfg)
        return cfg

    @staticmethod
    def _load_list(path: Path) -> list[dict]:
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _write(self, cfg: Config) -> None:
        write_private(self.path, cfg.model_dump_json(indent=1))

    def save(self) -> None:
        with self._lock:
            self._write(self.config)

    # -- history -------------------------------------------------------------------------------
    def add_history(self, entry: dict) -> None:
        entry.setdefault("at", time.time())
        self.history.append(entry)
        del self.history[:-HISTORY_MAX]
        try:
            write_private(self.history_path, json.dumps(self.history))
        except OSError as e:
            log.warning("could not write history: %s", e)

    # -- lookups ---------------------------------------------------------------------------------
    def client(self, cid: str) -> Client | None:
        return next((c for c in self.config.clients if c.id == cid), None)

    def agent(self, aid: str) -> Agent | None:
        return next((a for a in self.config.agents if a.id == aid), None)

    def default_agent(self) -> Agent | None:
        cfg = self.config
        return self.agent(cfg.default_agent_id) or next((a for a in cfg.agents if a.enabled), None)

    def agent_for_client(self, client: Client) -> Agent | None:
        return (self.agent(client.agent_id) if client.agent_id else None) or self.default_agent()

    def contact(self, cid: str) -> Contact | None:
        return next((c for c in self.config.contacts if c.id == cid), None)

    def integration(self, iid: str) -> Integration | None:
        return next((i for i in self.config.integrations if i.id == iid), None)

    def machine(self, mid: str) -> Machine | None:
        return next((m for m in self.config.machines if m.id == mid), None)

    def mcp_token(self, tid: str) -> McpToken | None:
        return next((t for t in self.config.mcp_tokens if t.id == tid), None)
