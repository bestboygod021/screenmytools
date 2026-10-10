"""Pure scheduling math (Qt-free) so it can be unit-tested precisely.

Two modes are supported by the UI:

* **interval** - re-run every N minutes (already present).
* **daily**    - re-run once a day at a fixed ``HH:MM``.

This module only computes *when* the next run is; the GUI owns the QTimer.
"""

from __future__ import annotations

from datetime import datetime, timedelta


def parse_hhmm(text: str) -> tuple[int, int] | None:
    """Parse ``HH:MM`` into ``(hour, minute)`` or ``None`` when invalid."""
    if not text or ":" not in text:
        return None
    parts = text.split(":")
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return (hour, minute)
    return None


def seconds_until_daily(hhmm: str, now: datetime | None = None) -> float:
    """Seconds from ``now`` until the next occurrence of ``hhmm`` (>= 0).

    If the time has already passed today, the target rolls over to tomorrow.
    """
    now = now or datetime.now()
    parsed = parse_hhmm(hhmm) or (9, 0)
    target = now.replace(hour=parsed[0], minute=parsed[1], second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(0.0, (target - now).total_seconds())


def format_daily_label(hhmm: str, now: datetime | None = None) -> str:
    """Human-readable description of when the next daily run fires."""
    now = now or datetime.now()
    parsed = parse_hhmm(hhmm) or (9, 0)
    target = now.replace(hour=parsed[0], minute=parsed[1], second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    when = "today" if target.date() == now.date() else "tomorrow"
    return f"{when} at {parsed[0]:02d}:{parsed[1]:02d}"
