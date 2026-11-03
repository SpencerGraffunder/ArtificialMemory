"""Deterministic relative-date resolution (no LLM).

The extractor's job is to emit the *original phrase* + the utterance anchor; this module
resolves phrases like "next Friday", "the 14th", "in two weeks", "tomorrow night" into
(start, end, granularity). Both the original phrase and the resolved ISO range are
stored, so resolution is never lossy.

Documented conventions
- "next <weekday>" and "this <weekday>" both resolve to the **first occurrence strictly
  after the anchor date** (dateparser-style). Owners who mean "the Friday of next week"
  keep saying "next week on Friday", which is handled by the week rule.
- Bare weekday = first occurrence strictly after anchor ("Are we still on Friday?"
  spoken on a Monday resolves to this week's Friday).
- Hour without AM/PM: 1–7 → PM, 8–11 → AM, 12 → noon (documented heuristic).
- "weekend" = Saturday 00:00 → Sunday 23:59:59 in the anchor's local date frame.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from calendar import monthrange
from typing import Literal

Granularity = Literal["datetime", "part-of-day", "day", "week", "month"]

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
    "saturday": 5, "sunday": 6,
}
WEEKDAY_ABBREV = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3,
                  "thurs": 3, "fri": 4, "sat": 5, "sun": 6}
MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
UNITS = {
    "minute": "minute", "minutes": "minute", "hour": "hour", "hours": "hour",
    "day": "day", "days": "day", "week": "week", "weeks": "week",
    "month": "month", "months": "month", "year": "year", "years": "year",
    "fortnight": "fortnight", "fortnights": "fortnight",
}
_ONES = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "a": 1, "an": 1,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_ORDINAL_SUFFIXES = {"st": 1, "nd": 2, "rd": 3, "th": 4}

DAYPARTS: dict[str, tuple[time, time, str]] = {
    "morning": (time(6), time(12), "part-of-day"),
    "noon": (time(11, 30), time(12, 30), "part-of-day"),
    "midday": (time(11, 30), time(12, 30), "part-of-day"),
    "afternoon": (time(12), time(18), "part-of-day"),
    "evening": (time(17), time(23), "part-of-day"),
    "tonight": (time(18), time(23, 59, 59), "part-of-day"),
    "night": (time(18), time(23, 59, 59), "part-of-day"),
    "midnight": (time(0), time(0, 59, 59), "part-of-day"),
}


@dataclass
class ResolvedTime:
    start: datetime
    end: datetime | None
    granularity: Granularity

    @property
    def start_iso(self) -> str:
        return self.start.isoformat()

    @property
    def end_iso(self) -> str | None:
        return self.end.isoformat() if self.end else None


def _as_local(dt: datetime) -> datetime:
    # utterance timestamps are UTC; date arithmetic uses the UTC calendar
    # (single-user tool; conversation participants share a timezone).
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _day_bounds(d: date, tzinfo) -> tuple[datetime, datetime]:
    return (
        datetime.combine(d, time(0), tzinfo=tzinfo),
        datetime.combine(d, time(23, 59, 59), tzinfo=tzinfo),
    )


def _parse_number(word: str) -> int | None:
    word = word.strip()
    if word.isdigit():
        return int(word)
    if word in _ONES:
        return _ONES[word]
    if word in _TENS:
        return _TENS[word]
    m = re.fullmatch(r"([a-z]+)[\s-]*(one|two|three|four|five|six|seven|eight|nine)", word)
    if m and m.group(1) in _TENS:
        return _TENS[m.group(1)] + _ONES[m.group(2)]
    return None


def _num(tok: str) -> str:
    return r"(?:\d{1,2}|(?:%s)(?:[\s-]+(?:%s))?)" % (
        "|".join(sorted((k for k in _ONES if not k.isdigit()), key=len, reverse=True)),
        "|".join(_TENS) + "|one|two|three|four|five|six|seven|eight|nine",
    )


def _parse_clock(text: str, pm_hint: bool = False) -> tuple[time, time] | None:
    """'at 6', '6pm', '6:30 pm', 'noon', 'midnight', '6 in the morning'.
    pm_hint (from an evening/night context) forces 1–11 → PM."""
    t = text.strip().lower()
    t = re.sub(r"^at\s+(?:about\s+|around\s+|exactly\s+)?", "", t)
    if t in DAYPARTS and t in ("noon", "midday", "midnight"):
        return DAYPARTS[t][0], DAYPARTS[t][1]
    m = re.search(
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?",
        t,
    )
    if not m:
        return None
    hour = int(m.group(1))
    if hour > 23:
        return None
    minute = int(m.group(2) or 0)
    mer = (m.group(3) or "").replace(".", "")
    if mer == "pm" and hour < 12:
        hour += 12
    elif mer == "am" and hour == 12:
        hour = 0
    elif not mer:
        if pm_hint and 1 <= hour <= 11:
            hour += 12
        elif 1 <= hour <= 7:
            # heuristic (documented): 1–7 → PM, 8–11 → AM, 12 → noon
            hour += 12
    if hour > 23:
        return None
    st = time(hour, minute)
    return st, st


def _split_time_part(phrase: str) -> tuple[str, str]:
    """Split 'friday at 6pm' into ('friday', 'at 6pm')."""
    m = re.search(r"\s+at\s+(?:about\s+|around\s+|exactly\s+)?([\d:apm.\s]+(?:am|pm)?)", phrase)
    if m:
        return phrase[: m.start()].strip(), m.group(1).strip()
    m = re.search(r"\s(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b", phrase)
    if m:
        return phrase[: m.start()].strip(), m.group(1).strip()
    return phrase, ""


def _weekday_on_or_after(d: date, weekday: int) -> date:
    return d + timedelta(days=(weekday - d.weekday()) % 7 or 7)  # strictly after


def _resolve_date_part(part: str, anchor: datetime) -> tuple[date, date | None, str] | None:
    """Return (start_date, end_date_inclusive, granularity) or None."""
    p = part.strip().lower()
    p = re.sub(r"^(?:on|at|by|until|before|around|sometime|come this|this coming)\s+", "", p)
    if not p:
        return None
    today = anchor.date()

    if p in ("today", "right now", "now"):
        return today, today, "day"
    if p == "tomorrow":
        return today + timedelta(days=1), None, "day"
    if p == "yesterday":
        return today - timedelta(days=1), None, "day"
    if p in ("tonight", "this evening", "this afternoon", "this morning",
             "this night", "this noon"):
        day = today
        if p == "tonight":
            return day, day, "day"
        return day, day, "day"
    if p in ("the day after tomorrow", "day after tomorrow"):
        return today + timedelta(days=2), None, "day"
    if p in ("yesterday morning", "yesterday afternoon", "yesterday evening", "yesterday night"):
        return today - timedelta(days=1), None, "day"

    # in <N> <unit>
    m = re.fullmatch(r"(?:in|within)\s+(%s)\s+(%s)(?:s)?" % (_num(""), "|".join(UNITS)), p)
    if m:
        n = _parse_number(m.group(1))
        unit = UNITS[m.group(2)]
        if n:
            if unit == "day":
                d = today + timedelta(days=n)
                return d, None, "day"
            if unit == "hour":
                return (today, today, "day") if n < 24 else None
            if unit == "week":
                d = today + timedelta(weeks=n)
                return d, None, "day"
            if unit == "fortnight":
                return today + timedelta(weeks=2 * n), None, "day"
            if unit == "month":
                return _add_months(today, n), None, "month"
            if unit == "year":
                return _add_years(today, n), None, "year"

    # next/this/<bare> weekend
    m = re.fullmatch(r"(?:(next|this)\s+)?(?:the\s+)?weekend", p)
    if m:
        days_to_sat = (5 - today.weekday()) % 7
        this_sat = today + timedelta(days=days_to_sat)
        if m.group(1) == "next":
            sat = this_sat + timedelta(days=7)
        elif m.group(1) == "this" or days_to_sat > 0:
            sat = this_sat
        else:
            sat = this_sat  # in-weekend bare "weekend" → now
        return sat, sat + timedelta(days=1), "week"

    # next/this week, next/this month
    if p in ("next week", "this week"):
        monday = today + timedelta(days=-today.weekday())
        if p == "this week":
            return monday, monday + timedelta(days=6), "week"
        monday += timedelta(days=7)
        return monday, monday + timedelta(days=6), "week"
    if p in ("next month", "this month"):
        if p == "this month":
            first = today.replace(day=1)
            return first, first.replace(day=monthrange(first.year, first.month)[1]), "month"
        d = _add_months(today, 1)
        first = d.replace(day=1)
        return first, first.replace(day=monthrange(first.year, first.month)[1]), "month"
    if p in ("end of the month", "end of month", "beginning of the month", "start of the month"):
        first = today.replace(day=1)
        last = first.replace(day=monthrange(first.year, first.month)[1])
        return (last, last, "day") if "end" in p else (first, first, "day")

    # ISO date
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", p)
    if m:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return d, None, "day"

    # 'July 4', "July 4th 2026", "4 July", '7/4', '7/4/26'
    m = re.fullmatch(
        r"(?:(%s)\s+(\d{1,2})(?:st|nd|rd|th)?|\ (\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(%s))"
        r"(?:[,\s]+(\d{4}))?" % ("|".join(MONTHS), "|".join(MONTHS)),
        p,
    )
    if m:
        if m.group(1):
            month, day, year = MONTHS[m.group(1)], int(m.group(2)), m.group(5)
        else:
            day, month, year = int(m.group(3)), MONTHS[m.group(4)], m.group(5)
        d = _make_date(int(year) if year else today.year, month, day, today)
        return d, None, "day"
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?", p)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = m.group(3)
        if year:
            year = int(year) + 2000 if len(year) == 2 else int(year)
        d = _make_date(year or today.year, month, day, today)
        return d, None, "day"

    # 'the 14th', 'the 14th of march', 'the 25'
    m = re.fullmatch(r"the\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s+of\s+(%s))?" % "|".join(MONTHS), p)
    if m:
        day = int(m.group(1))
        if m.group(2):
            d = _make_date(today.year, MONTHS[m.group(2)], day, today)
        else:
            import calendar as _cal

            day = min(day, _cal.monthrange(today.year, today.month)[1])
            d = date(today.year, today.month, day)
            if d < today - timedelta(days=3):  # "the 14th" just passed → next month
                d = _add_months(d, 1)
        return d, None, "day"

    # weekday with next/this/bare — all "first occurrence strictly after today"
    wd_re = "|".join(list(WEEKDAYS) + list(WEEKDAY_ABBREV))
    m = re.fullmatch(r"(?:(next|this|coming)\s+)?(?:the\s+)?(%s)(?:s)?" % wd_re, p)
    if m:
        key = m.group(2)
        wd = WEEKDAYS.get(key) or WEEKDAY_ABBREV[key]
        return _weekday_on_or_after(today, wd), None, "day"

    # 'two weeks from friday', 'a week from tomorrow'
    m = re.fullmatch(r"(%s)\s+(%s)\s+from\s+(%s|tomorrow|today)" % (_num(""), "|".join(UNITS), wd_re), p)
    if m:
        n, unit = _parse_number(m.group(1)), UNITS[m.group(2)]
        if m.group(3) == "tomorrow":
            base = today + timedelta(days=1)
        elif m.group(3) == "today":
            base = today
        else:
            wd = WEEKDAYS.get(m.group(3)) or WEEKDAY_ABBREV[m.group(3)]
            base = _weekday_on_or_after(today, wd)
        delta = {"day": timedelta(days=n), "week": timedelta(weeks=n),
                 "hour": timedelta(hours=n), "fortnight": timedelta(weeks=2 * n)}
        if unit in ("month", "year"):
            d = _add_months(base, n) if unit == "month" else _add_years(base, n)
        else:
            d = base + delta[unit]
        return d, None, "day"

    return None


def _make_date(year: int, month: int, day: int, today: date) -> date:
    """Build a date; roll to next year if the day-of-year is well in the past."""
    import calendar

    day = min(day, calendar.monthrange(year, month)[1])
    d = date(year, month, day)
    if d < today - timedelta(days=3):
        try:
            d = date(year + 1, month, day)
        except ValueError:
            pass
    return d


def _add_months(d: date, n: int) -> date:
    month0 = d.month - 1 + n
    year = d.year + month0 // 12
    month = month0 % 12 + 1
    return d.replace(year=year, month=month, day=min(d.day, monthrange(year, month)[1]))


def _add_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:
        return d.replace(year=d.year + n, day=28)


def _apply_daypart(d: date, tz, daypart: str) -> tuple[datetime, datetime] | None:
    if daypart not in DAYPARTS:
        return None
    st, en, _ = DAYPARTS[daypart]
    return (datetime.combine(d, st, tzinfo=tz), datetime.combine(d, en, tzinfo=tz))


def resolve_time_phrase(phrase: str | None, anchor: datetime) -> ResolvedTime | None:
    """Resolve a spoken time phrase against the utterance timestamp. None = can't resolve."""
    if not phrase:
        return None
    anchor = _as_local(anchor)
    p = phrase.strip().lower().rstrip(".!?")
    p = re.sub(r"\s+", " ", p)
    if not p or p in ("sometime", "soon", "later", "recently", "lately", "before",
                      "the other day", "soon-ish"):
        # genuinely too vague — original phrase is kept by callers
        if p in ("the other day",):
            d = anchor.date() - timedelta(days=2)
            s, e = _day_bounds(d, anchor.tzinfo)
            return ResolvedTime(s, e, "day")
        return None

    date_part, time_part = _split_time_part(p)

    # 'tonight' / 'midnight' / 'noon' fuse date+part
    if date_part in DAYPARTS:
        dp = _apply_daypart(anchor.date(), anchor.tzinfo, date_part)
        if dp:
            if time_part:
                clock = _parse_clock(time_part, pm_hint=date_part in ("night", "tonight", "evening"))
                if clock:
                    st, _en = clock
                    s = datetime.combine(anchor.date(), st, tzinfo=anchor.tzinfo)
                    return ResolvedTime(s, s, "datetime")
            return ResolvedTime(dp[0], dp[1], "part-of-day")

    # 'tonight' fuses date+part
    date_res = _resolve_date_part(date_part, anchor)

    # day-part words attached to the date ("friday evening") — tonight handled above
    if date_res is None:
        m = re.fullmatch(
            r"(%s)\s+(?:this\s+|the\s+)?(morning|noon|midday|afternoon|evening|night)"
            % ("|".join(list(WEEKDAYS) + list(WEEKDAY_ABBREV))),
            date_part,
        )
        if m:
            base = _resolve_date_part(m.group(1), anchor)
            if base:
                dp = _apply_daypart(base[0], anchor.tzinfo, m.group(2))
                if dp:
                    return ResolvedTime(dp[0], dp[1], "part-of-day")
        m = re.fullmatch(
            r"(tomorrow|yesterday|today)\s+(morning|afternoon|evening|night)", date_part
        )
        if m:
            base = _resolve_date_part(m.group(1), anchor)
            if base:
                dp = _apply_daypart(base[0], anchor.tzinfo, m.group(2))
                if dp:
                    return ResolvedTime(dp[0], dp[1], "part-of-day")

    if date_res is None and not time_part:
        # maybe the whole phrase is a clock time ("at 6:30")
        clock = _parse_clock(p)
        if clock:
            tz = anchor.tzinfo
            st, en = clock
            if st == en:
                return ResolvedTime(
                    datetime.combine(anchor.date(), st, tzinfo=tz),
                    datetime.combine(anchor.date(), st, tzinfo=tz),
                    "datetime",
                )
            return ResolvedTime(
                datetime.combine(anchor.date(), st, tzinfo=tz),
                datetime.combine(anchor.date(), en, tzinfo=tz),
                "part-of-day",
            )
        return None
    if date_res is None and date_part.strip() in ("", "at", "around", "about", "by", "before", "until"):
        # time-only phrase with a preposition ("at 6:30pm") → today
        clock = _parse_clock(time_part or p)
        if clock:
            st, en = clock
            tz = anchor.tzinfo
            s = datetime.combine(anchor.date(), st, tzinfo=tz)
            e = datetime.combine(anchor.date(), en, tzinfo=tz)
            return ResolvedTime(s, e, "datetime")
        return None
    if date_res is None:
        return None

    start_d, end_d, gran = date_res
    tz = anchor.tzinfo

    # time-of-day refinement
    if time_part:
        clock = _parse_clock(time_part)
        if clock:
            st, en = clock
            s = datetime.combine(start_d, st, tzinfo=tz)
            e = datetime.combine(end_d or start_d, en, tzinfo=tz)
            return ResolvedTime(s, e, "datetime")
    # trailing day-part inside date_part already handled; check explicit
    m = re.search(r"\b(morning|noon|midday|afternoon|evening|night)\b", date_part)
    if m and gran in ("day", "week", "month"):
        base = m.group(1)
        if end_d and end_d != start_d and base == "night":
            base_date = end_d
        else:
            base_date = start_d
        dp = _apply_daypart(base_date, tz, base)
        if dp:
            return ResolvedTime(dp[0], dp[1], "part-of-day")

    s, e = _day_bounds(start_d, tz)
    if end_d:
        _, e = _day_bounds(end_d, tz)
        gran_out: Granularity = "week" if gran == "week" else ("month" if gran == "month" else "day")
    else:
        gran_out = gran  # type: ignore[assignment]
    return ResolvedTime(s, e, gran_out)
