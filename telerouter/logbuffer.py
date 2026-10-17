"""In-memory ring buffer of log records, served to the web UI."""

from __future__ import annotations

import logging
from collections import deque


class LogBuffer(logging.Handler):
    def __init__(self, capacity: int = 1500):
        super().__init__()
        self.records: deque[dict] = deque(maxlen=capacity)
        self.seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += "\n" + logging.Formatter().formatException(record.exc_info)
        except Exception:
            msg = str(record.msg)
        self.seq += 1
        self.records.append({
            "seq": self.seq,
            "ts": record.created,
            "level": record.levelname,
            "logger": record.name.removeprefix("telerouter."),
            "msg": msg,
        })

    def since(self, seq: int, limit: int = 500) -> list[dict]:
        out = [r for r in self.records if r["seq"] > seq]
        return out[-limit:]
