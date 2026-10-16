"""Business-hours schedule parsing + is_within (fixed `now`, no clock/tz dependence). Offline."""
from datetime import datetime

from supportal.hours import _parse_days, is_within, parse_schedule

# Known weekdays: 2024-01-01 was a Monday → +2 Wed, +5 Sat, +6 Sun.
MON = datetime(2024, 1, 1, 10, 0)
WED = datetime(2024, 1, 3, 10, 0)
SAT = datetime(2024, 1, 6, 11, 0)


# ── day parsing ────────────────────────────────────────────────────────────────
def test_parse_days_range_single_list_and_wrap():
    assert _parse_days("mon-fri") == {0, 1, 2, 3, 4}
    assert _parse_days("sat") == {5}
    assert _parse_days("mon,wed,fri") == {0, 2, 4}
    assert _parse_days("fri-mon") == {4, 5, 6, 0}       # wraps across the week end
    assert _parse_days("xxx") == set()


def test_parse_schedule_skips_garbage():
    windows = parse_schedule("mon-fri 09:00-18:00; garbage; sat 10:00-14:00")
    assert len(windows) == 2
    assert windows[0][0] == {0, 1, 2, 3, 4}
    assert parse_schedule("") == [] and parse_schedule("nonsense") == []


# ── is_within ──────────────────────────────────────────────────────────────────
def test_within_and_outside_basic():
    sched = "mon-fri 09:00-18:00"
    assert is_within(sched, now=WED) is True                       # Wed 10:00 — open
    assert is_within(sched, now=WED.replace(hour=20)) is False     # Wed 20:00 — closed
    assert is_within(sched, now=SAT) is False                      # Saturday — not in mon-fri


def test_boundaries_start_inclusive_end_exclusive():
    sched = "mon-fri 09:00-18:00"
    assert is_within(sched, now=MON.replace(hour=9, minute=0)) is True
    assert is_within(sched, now=MON.replace(hour=18, minute=0)) is False


def test_second_clause_matches():
    sched = "mon-fri 09:00-18:00; sat 10:00-14:00"
    assert is_within(sched, now=SAT) is True                       # Sat 11:00 — in the sat clause
    assert is_within(sched, now=SAT.replace(hour=15)) is False


def test_overnight_window():
    sched = "mon-fri 22:00-06:00"
    assert is_within(sched, now=WED.replace(hour=23)) is True
    assert is_within(sched, now=WED.replace(hour=5)) is True
    assert is_within(sched, now=WED.replace(hour=12)) is False


def test_fail_open_on_empty_or_unparseable():
    assert is_within("", now=WED) is True                          # no schedule → never "away"
    assert is_within("total nonsense", now=WED) is True
