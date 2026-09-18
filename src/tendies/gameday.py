"""Game-calendar helpers (§4). Pure functions, no database.

Game-time is a real ``date`` stored per guild. Monday–Friday are business days
(full ticks); Saturday/Sunday are closed (the day advances, nothing produces).
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from tzlocal import get_localzone

#: index 0..6 -> name, matching ``date.weekday()`` (Monday == 0).
WEEKDAYS: tuple[str, ...] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def calendar_timezone(name: str | None = None) -> ZoneInfo:
    """Return the configured IANA zone, or the host's actual local zone.

    ``datetime.now().astimezone().tzinfo`` can be a fixed UTC offset on some
    systems, which silently drifts by an hour after a DST transition.  tzlocal
    resolves the host's IANA zone instead, preserving its transition rules.
    """
    return ZoneInfo(name) if name else get_localzone()


def local_now(name: str | None = None) -> dt.datetime:
    """Current wall-clock time in the economy's calendar zone."""
    return dt.datetime.now(calendar_timezone(name))


def local_date(name: str | None = None) -> dt.date:
    """Current wall-clock date in the economy's calendar zone."""
    return local_now(name).date()


def week_monday(day: dt.date) -> dt.date:
    """The Monday belonging to ``day``'s calendar week."""
    return day - dt.timedelta(days=day.weekday())


def weekday_name(day: dt.date) -> str:
    """Lowercase weekday name for a date (e.g. ``"monday"``)."""
    return WEEKDAYS[day.weekday()]


def is_business_day(day: dt.date) -> bool:
    """True Monday–Friday."""
    return day.weekday() < 5


def is_weekend(day: dt.date) -> bool:
    return day.weekday() >= 5


def is_monday(day: dt.date) -> bool:
    return day.weekday() == 0


def next_day(day: dt.date) -> dt.date:
    return day + dt.timedelta(days=1)


def parse_weekday(name: str) -> int | None:
    """Parse a weekday name (case-insensitive, prefix-tolerant) to 0..6.

    Accepts ``"mon"``, ``"Monday"``, ``"MONDAY"`` etc. Returns ``None`` if no
    unambiguous match.
    """
    if not name:
        return None
    key = name.strip().lower()
    for i, full in enumerate(WEEKDAYS):
        if full == key:
            return i
    # prefix match (e.g. "mon" -> monday), require >= 3 chars to avoid ambiguity
    if len(key) >= 3:
        matches = [i for i, full in enumerate(WEEKDAYS) if full.startswith(key)]
        if len(matches) == 1:
            return matches[0]
    return None


def date_for_weekday(reference: dt.date, weekday_index: int) -> dt.date:
    """Return a date whose weekday equals ``weekday_index``, on or after
    ``reference`` (used by ``$setday`` to re-anchor the calendar)."""
    delta = (weekday_index - reference.weekday()) % 7
    return reference + dt.timedelta(days=delta)


def business_days_before(day: dt.date, n: int) -> dt.date:
    """The date that is ``n`` business days before ``day`` (walking back, skipping
    weekends). ``n == 0`` returns ``day`` unchanged.

    Used to compute trailing windows: the last *k* business days are the
    transactions with ``game_day >= business_days_before(current, k - 1)``.
    """
    if n <= 0:
        return day
    cursor = day
    counted = 0
    while counted < n:
        cursor -= dt.timedelta(days=1)
        if is_business_day(cursor):
            counted += 1
    return cursor


def business_days_in_span(start: dt.date, end: dt.date) -> int:
    """Count business days in the inclusive range ``[start, end]``."""
    if end < start:
        return 0
    count = 0
    cursor = start
    while cursor <= end:
        if is_business_day(cursor):
            count += 1
        cursor += dt.timedelta(days=1)
    return count
