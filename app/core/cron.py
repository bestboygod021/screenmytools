"""A small, dependency-free cron parser (Qt-free).

Supports the five standard fields ``minute hour day-of-month month day-of-week``
with ``*``, lists (``1,5``), ranges (``1-5``) and steps (``*/15``, ``1-5/2``).
Day-of-week uses 0=Sunday..6=Saturday (7 also means Sunday). Fields are matched
with AND, which covers the common scheduling cases.
"""

from __future__ import annotations

from datetime import datetime, timedelta

_FIELD_BOUNDS = (
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day of month
    (1, 12),  # month
    (0, 7),  # day of week (0 and 7 = Sunday)
)


class CronError(ValueError):
    """Raised when a cron expression cannot be parsed."""


def _parse_field(field: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"Empty element in '{field}'")
        step = 1
        if "/" in part:
            range_part, _, step_part = part.partition("/")
            step = int(step_part)
            if step <= 0:
                raise CronError(f"Step must be positive in '{part}'")
        else:
            range_part = part

        if range_part == "*":
            start, end = low, high
        elif "-" in range_part:
            start_s, _, end_s = range_part.partition("-")
            start, end = int(start_s), int(end_s)
        else:
            start = end = int(range_part)

        if start < low or end > high or start > end:
            raise CronError(f"Value out of range in '{part}' (allowed {low}-{high})")
        values.update(range(start, end + 1, step))
    return values


def parse(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    """Parse a 5-field cron expression into five sets of allowed values."""
    fields = expr.split()
    if len(fields) != 5:
        raise CronError("A cron expression needs exactly 5 fields (min hour dom mon dow).")
    parsed = []
    for field, (low, high) in zip(fields, _FIELD_BOUNDS, strict=False):
        parsed.append(_parse_field(field, low, high))
    minutes, hours, doms, months, dows = parsed
    # Normalise 7 -> 0 (both mean Sunday) for the day-of-week field.
    if 7 in dows:
        dows.discard(7)
        dows.add(0)
    return minutes, hours, doms, months, dows


def _cron_dow(dt: datetime) -> int:
    """Python weekday (Mon=0..Sun=6) -> cron dow (Sun=0..Sat=6)."""
    return (dt.weekday() + 1) % 7


def matches(expr: str, dt: datetime) -> bool:
    minutes, hours, doms, months, dows = parse(expr)
    return (
        dt.minute in minutes
        and dt.hour in hours
        and dt.day in doms
        and dt.month in months
        and _cron_dow(dt) in dows
    )


def next_run(expr: str, from_dt: datetime | None = None, max_days: int = 366) -> datetime | None:
    """The next minute boundary matching ``expr`` after ``from_dt`` (or None)."""
    parse(expr)  # validate early
    start = (from_dt or datetime.now()).replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = start + timedelta(days=max_days)
    candidate = start
    while candidate <= limit:
        if matches(expr, candidate):
            return candidate
        candidate += timedelta(minutes=1)
    return None  # pragma: no cover - only for impossible expressions


def seconds_until(expr: str, from_dt: datetime | None = None) -> float | None:
    """Seconds from ``from_dt`` until the next matching minute (or None)."""
    now = from_dt or datetime.now()
    target = next_run(expr, now)
    if target is None:
        return None
    return max(0.0, (target - now).total_seconds())
