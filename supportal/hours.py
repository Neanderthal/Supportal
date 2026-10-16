"""Business-hours schedule parsing (feature SUPPORT_BUSINESS_HOURS).

Pure/offline-testable: `is_within(schedule, tz, now)` decides whether a moment falls inside the open
windows. The relay uses it to send a one-time "we're away" note when a ticket is opened out of hours.

Schedule grammar (``SUPPORT_HOURS``): ``;``-separated clauses, each ``<days> HH:MM-HH:MM``.
``<days>`` is a range (``mon-fri``), a single day (``sat``) or a comma list (``mon,wed,fri``).
Example: ``mon-fri 09:00-18:00; sat 10:00-14:00``. A window with end < start crosses midnight.
"""
from __future__ import annotations

import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_LOG = logging.getLogger("supportal.hours")

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _parse_days(token: str) -> set[int]:
    """"mon-fri" | "sat" | "mon,wed" → weekday indices (Mon=0). Ranges wrap (e.g. fri-mon)."""
    out: set[int] = set()
    for part in token.lower().split(","):
        part = part.strip()
        if "-" in part:
            a, b = (x.strip() for x in part.split("-", 1))
            if a in _DAYS and b in _DAYS:
                i, j = _DAYS[a], _DAYS[b]
                out.update((i + k) % 7 for k in range((j - i) % 7 + 1))
        elif part in _DAYS:
            out.add(_DAYS[part])
    return out


def _parse_time(token: str) -> time | None:
    try:
        h, m = token.strip().split(":", 1)
        return time(int(h), int(m))
    except (ValueError, TypeError):
        return None


def parse_schedule(schedule: str) -> list[tuple[set[int], time, time]]:
    """Schedule string → [(weekdays, start, end)]. Malformed clauses are skipped (with a warning)."""
    windows: list[tuple[set[int], time, time]] = []
    for clause in (schedule or "").split(";"):
        clause = clause.strip()
        if not clause:
            continue
        parts = clause.split()
        if len(parts) != 2 or "-" not in parts[1]:
            _LOG.warning("[hours] ignoring unparseable schedule clause: %r", clause)
            continue
        days = _parse_days(parts[0])
        start_s, end_s = parts[1].split("-", 1)
        start, end = _parse_time(start_s), _parse_time(end_s)
        if days and start is not None and end is not None:
            windows.append((days, start, end))
        else:
            _LOG.warning("[hours] ignoring unparseable schedule clause: %r", clause)
    return windows


def is_within(schedule: str, tz: str = "UTC", now: datetime | None = None) -> bool:
    """True if `now` falls inside an open window. Fail-open: an empty/unparseable schedule → always open
    (the feature stays inert rather than flagging everyone as "away"). `now` None → current time in `tz`."""
    windows = parse_schedule(schedule)
    if not windows:
        return True
    if now is None:
        try:
            zone = ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            _LOG.warning("[hours] unknown timezone %r — falling back to UTC", tz)
            zone = ZoneInfo("UTC")
        now = datetime.now(zone)
    wd, t = now.weekday(), now.time()
    for days, start, end in windows:
        if wd not in days:
            continue
        if start <= end:
            if start <= t < end:
                return True
        elif t >= start or t < end:        # window crosses midnight
            return True
    return False
