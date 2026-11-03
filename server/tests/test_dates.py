from datetime import datetime, timezone

import pytest

from artmem_server.core.dates import resolve_time_phrase

# Tuesday 2026-07-07 15:00 UTC
TUE = datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc)
# Friday 2026-07-10 22:00 UTC (late-night anchor: "tomorrow" = Saturday)
FRI_NIGHT = datetime(2026, 7, 10, 22, 0, tzinfo=timezone.utc)


def r(phrase, anchor=TUE):
    return resolve_time_phrase(phrase, anchor)


# ---- spec §11 required cases ------------------------------------------------
def test_next_friday():
    res = r("next Friday")
    assert res.start.isoweekday() == 5  # Friday
    assert res.start.date().isoformat() == "2026-07-10"


def test_the_14th():
    res = r("the 14th")
    assert res.start.day == 14 and res.start.month == 7


def test_the_14th_rolls_forward_if_passed():
    res = r("the 3rd")  # July 3 just passed → next month
    assert res.start.month == 8 and res.start.day == 3


def test_in_two_weeks():
    res = r("in two weeks")
    assert res.start.date().isoformat() == "2026-07-21"


def test_tomorrow_night_is_evening_band():
    res = r("tomorrow night")
    assert res.start.day == 8
    assert res.start.hour == 18 and res.end.hour == 23
    assert res.granularity == "part-of-day"


# ---- extended cases ----------------------------------------------------------
@pytest.mark.parametrize(
    "phrase,day,month",
    [
        ("tomorrow", 8, 7), ("today", 7, 7), ("yesterday", 6, 7),
        ("in 3 days", 10, 7), ("next week", 13, 7), ("this weekend", 11, 7),
        ("next weekend", 18, 7), ("end of the month", 31, 7),
        ("july 4th 2027", 4, 7), ("december 25", 25, 12),
        ("the 25th of december", 25, 12), ("7/25", 25, 7),
        ("2026-08-01", 1, 8), ("in a month", 7, 8),
    ],
)
def test_date_shapes(phrase, day, month):
    res = r(phrase)
    assert res is not None, phrase
    assert (res.start.day, res.start.month) == (day, month), phrase


def test_bare_weekday_is_upcoming():
    res = r("friday")
    assert res.start.date().isoformat() == "2026-07-10"
    res = r("tuesday")  # today is Tuesday → next Tuesday
    assert res.start.date().isoformat() == "2026-07-14"


def test_clock_time_heuristics():
    assert r("at 6").start.hour == 18       # 1–7 → PM
    assert r("at 9").start.hour == 9        # 8–11 → AM
    assert r("at 6:30pm").start.hour == 18 and r("at 6:30pm").start.minute == 30
    assert r("at noon").start.hour in (11, 12)


def test_evening_hint_forces_pm():
    res = r("tonight at 8")
    assert res.start.hour == 20


def test_date_with_time():
    res = r("next Friday at 7")
    assert res.start.isoweekday() == 5 and res.start.hour == 19


def test_late_night_anchor_tomorrow_is_next_calendar_day():
    res = r("tomorrow", FRI_NIGHT)
    assert res.start.date().isoformat() == "2026-07-11"  # Saturday


def test_vague_phrases_return_none_but_callers_keep_phrase():
    assert r("sometime") is None
    assert r("") is None
    assert r(None) is None
    assert r("next year") is None or r("next year").start.year == 2027


def test_weekend_range():
    res = r("this weekend")
    assert res.start.isoweekday() == 6 and res.end.isoweekday() == 7
