"""When does a scheduled task run next? Daily times, intervals, one-off times and 5-field cron expressions."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_FIELDS = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]      # minute hour day-of-month month day-of-week
_ALIASES = {3: {m.lower(): i + 1 for i, m in enumerate(
                ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])},
            4: {d.lower(): (i + 1) % 7 for i, d in enumerate(DAY_NAMES)}}


class Cron:
    """A standard cron expression: `*`, lists, ranges, steps and month/day names; day-of-week 0 and 7 are
    Sunday. Like cron, a restricted day-of-month and day-of-week match when either does."""

    def __init__(self, expr: str):
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError("a cron expression has 5 fields: minute hour day month weekday")
        sets = [self._field(p, i) for i, p in enumerate(parts)]
        self.minutes, self.hours, self.mdays, self.months, wdays = sets
        self.wdays = {(d - 1) % 7 for d in wdays}           # cron (0 = Sun) -> Python weekday (0 = Mon)
        self.any_mday = parts[2] == "*"
        self.any_wday = parts[4] == "*"

    @staticmethod
    def _field(text: str, i: int) -> set[int]:
        lo, hi = _FIELDS[i]
        out: set[int] = set()
        for part in text.lower().split(","):
            rng, _, step_s = part.partition("/")
            step = int(step_s) if step_s else 1
            if step < 1:
                raise ValueError(f"bad step in '{text}'")
            if rng == "*":
                a, b = lo, hi
            else:
                a_s, _, b_s = rng.partition("-")
                a = _num(a_s, i)
                b = _num(b_s, i) if b_s else (hi if step_s else a)
            if not (lo <= a <= hi and lo <= b <= hi and a <= b):
                raise ValueError(f"'{text}' is out of range ({lo}-{hi})")
            out.update(range(a, b + 1, step))
        return out

    def _day_ok(self, d: dt.date) -> bool:
        if d.month not in self.months:
            return False
        m_ok, w_ok = d.day in self.mdays, d.weekday() in self.wdays
        if self.any_mday and self.any_wday:
            return True
        if self.any_mday:
            return w_ok
        if self.any_wday:
            return m_ok
        return m_ok or w_ok

    def next_after(self, after: dt.datetime) -> dt.datetime | None:
        """The first matching minute strictly after `after` (an aware datetime), within about 4 years."""
        tz = after.tzinfo
        start = after.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        day = start.date()
        for _ in range(366 * 4 + 1):
            if self._day_ok(day):
                for h in sorted(self.hours):
                    for m in sorted(self.minutes):
                        t = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
                        if t >= start:
                            return t
            day += dt.timedelta(days=1)
        return None


def _num(s: str, i: int) -> int:
    s = s.strip().lower()
    if s in _ALIASES.get(i, {}):
        return _ALIASES[i][s]
    if not s.isdigit():
        raise ValueError(f"'{s}' is not a number")
    return int(s)


def next_run(task, after: float) -> float | None:
    """Unix time of the task's next run after `after` (None: never again)."""
    tz = ZoneInfo(task.timezone)
    now = dt.datetime.fromtimestamp(after, tz)
    kind = task.schedule
    if kind == "interval":
        return after + task.every_minutes * 60
    if kind == "once":
        t = parse_local(task.at, tz)
        return t.timestamp() if t and t.timestamp() > after else None
    if kind == "cron":
        nxt = Cron(task.cron).next_after(now)
        return nxt.timestamp() if nxt else None
    best = None                                          # daily
    wd = ",".join(str((d + 1) % 7) for d in task.days) if task.days else "*"
    for hhmm in task.times:
        h, m = hhmm.split(":")
        nxt = Cron(f"{int(m)} {int(h)} * * {wd}").next_after(now)
        if nxt and (best is None or nxt < best):
            best = nxt
    return best.timestamp() if best else None


def parse_local(text: str, tz: ZoneInfo) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    return t.replace(tzinfo=tz) if t.tzinfo is None else t


def describe(task) -> str:
    """'every day at 09:00', 'Mon, Fri at 08:30', 'every 30 min', 'once on 2026-10-20 09:00', 'cron 0 */2 * * *'."""
    kind = task.schedule
    if kind == "interval":
        n = task.every_minutes
        if n % 1440 == 0:
            return "every day" if n == 1440 else f"every {n // 1440} days"
        if n % 60 == 0:
            return "every hour" if n == 60 else f"every {n // 60} hours"
        return f"every {n} min"
    if kind == "once":
        return f"once on {task.at.replace('T', ' ')}"
    if kind == "cron":
        return f"cron {task.cron}"
    days = sorted(set(task.days))
    when = ", ".join(task.times)
    if not days or len(days) == 7:
        return f"every day at {when}"
    if days == [0, 1, 2, 3, 4]:
        return f"weekdays at {when}"
    if days == [5, 6]:
        return f"weekends at {when}"
    return f"{', '.join(DAY_NAMES[d] for d in days)} at {when}"
