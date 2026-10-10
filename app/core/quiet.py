"""Quiet hours for change alerts: hold them overnight, flush them in the morning.

Paging someone at 03:00 for a cookie banner is the fastest way to make them mute
the bot. With ``alert_quiet_hours`` set (e.g. ``22:00-07:00``) the engine stops
sending alerts inside that window and appends them to a small JSON queue next to
the history instead. The next run outside the window drains the queue and sends
everything in one message, so a noisy night becomes a single morning summary.

Several windows can be given at once, separated by commas, and a window may span
named days - ``"22:00-07:00, fri18:00-mon09:00"`` means "quiet overnight, and
from Friday evening until Monday morning silently".

``alert_quiet_urls`` narrows that per host: ``"staging.example.com=22:00-07:00;
news.example.com="`` keeps staging quiet overnight while the newsletter page
never stops paging. Rules are separated by semicolons, each one is
``fragment=windows``, the first matching fragment wins and an empty window list
means "never quiet for this URL".

Everything here is best-effort and Qt-free: a missing or corrupt queue file is
treated as "nothing waiting", and no function raises on bad input.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

#: Where the deferred alerts wait (inside the capture output folder).
QUEUE_FILENAME = "pending-alerts.json"

_WINDOW = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*$")


def parse_window(text: str) -> tuple[time, time] | None:
    """``"22:00-07:00"`` -> ``(time(22, 0), time(7, 0))``; ``None`` when unreadable.

    ``"22-7"`` is accepted too (missing minutes mean ``:00``).
    """
    match = _WINDOW.match(str(text or ""))
    if match is None:
        return None
    start_hour, start_minute, end_hour, end_minute = match.groups()
    try:
        return time(int(start_hour), int(start_minute or 0)), time(
            int(end_hour), int(end_minute or 0)
        )
    except ValueError:  # e.g. 25:00 - an hour/minute outside the clock
        return None


def in_window(moment: datetime, text: str) -> bool:
    """True when ``moment`` is inside any quiet window in ``text``.

    An unreadable or empty setting means "never quiet", and so does ``22:00-22:00``
    (a zero-length window), which keeps a typo from silencing alerts forever.
    """
    return any(covers(window, moment) for window in parse_windows(text))


def window_end(moment: datetime, text: str) -> datetime | None:
    """When the quiet period containing ``moment`` ends, or ``None`` if it does not.

    With several windows the earliest end wins: that is when the queue can be
    flushed without cutting a later silence short.
    """
    ends = [
        _period_end(window, anchor)
        for window in parse_windows(text)
        for anchor in _anchors(window, moment)
        if anchor <= moment < _period_end(window, anchor)
    ]
    return min(ends) if ends else None


def _anchors(window: QuietWindow, moment: datetime) -> list[datetime]:
    """The start times of this window's recent occurrences, around ``moment``."""
    anchors: list[datetime] = []
    for offset in range(8):
        day = moment.date() - timedelta(days=offset)
        if window.first_day is not None and day.weekday() != window.first_day:
            continue
        anchors.append(datetime.combine(day, window.start))
    return anchors


#: ``mon``..``sun`` -> ``datetime.weekday()``.
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# "22:00-07:00" (every day) or "fri18:00-mon09:00" (Friday evening to Monday morning)
_SPAN = re.compile(
    r"^\s*(?:(?P<first>[a-z]{3})\s*)?(?P<start_h>\d{1,2})(?::(?P<start_m>\d{2}))?\s*-\s*"
    r"(?:(?P<last>[a-z]{3})\s*)?(?P<end_h>\d{1,2})(?::(?P<end_m>\d{2}))?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class QuietWindow:
    """One quiet period: daily, or from ``first_day`` through ``last_day``.

    ``first_day``/``last_day`` are ``datetime.weekday()`` values (``None`` for a
    window that repeats every day). ``fri18:00-mon09:00`` therefore starts on
    Friday evening and ends on Monday morning, days included.
    """

    start: time
    end: time
    first_day: int | None = None
    last_day: int | None = None

    @property
    def empty(self) -> bool:
        """True for a zero-length daily window (a typo, not a 24h silence)."""
        return self.first_day is None and self.start == self.end

    def label(self) -> str:
        """The window back as text (``"fri 18:00 - mon 09:00"``)."""
        names = {value: key for key, value in WEEKDAYS.items()}
        head = f"{names[self.first_day]} " if self.first_day is not None else ""
        tail = f"{names[self.last_day]} " if self.last_day is not None else ""
        return f"{head}{self.start:%H:%M} - {tail}{self.end:%H:%M}"


def _clock(hour: str, minute: str | None) -> time | None:
    """``("7", "30")`` -> ``time(7, 30)``; ``None`` when it is not a clock time."""
    try:
        return time(int(hour), int(minute or 0))
    except ValueError:
        return None


def parse_span(text: str) -> QuietWindow | None:
    """Parse one window item, day names included; ``None`` when unreadable.

    ``"22:00-07:00"``, ``"22-7"`` and ``"fri18:00-mon09:00"`` are all valid. Day
    names must be spelled out on both sides (``"fri18:00-07:00"`` is rejected)
    because "Friday to who?" is exactly the kind of guess a typo would hide in.
    """
    match = _SPAN.match(str(text or ""))
    if match is None:
        return None
    first, last = match.group("first"), match.group("last")
    if (first is None) != (last is None):
        return None
    if first is not None:
        first, last = first.lower(), str(last).lower()
        if first not in WEEKDAYS or last not in WEEKDAYS:
            return None
    start = _clock(match.group("start_h"), match.group("start_m"))
    end = _clock(match.group("end_h"), match.group("end_m"))
    if start is None or end is None:
        return None
    window = QuietWindow(
        start=start,
        end=end,
        first_day=WEEKDAYS[first] if first else None,
        last_day=WEEKDAYS[last] if last else None,
    )
    return None if window.empty else window


def split_windows(text: str) -> list[str]:
    """The raw comma/semicolon separated items of a quiet-hours setting."""
    return [part.strip() for part in re.split(r"[,;]", str(text or "")) if part.strip()]


def parse_windows(text: str) -> list[QuietWindow]:
    """Every readable window in ``text``; unreadable items are skipped."""
    return [window for window in (parse_span(item) for item in split_windows(text)) if window]


def invalid_windows(text: str) -> list[str]:
    """The items of ``text`` that make no sense (for a clean settings error)."""
    return [item for item in split_windows(text) if parse_span(item) is None]


def covers(window: QuietWindow, moment: datetime) -> bool:
    """True when ``moment`` falls inside this window's current occurrence."""
    for offset in range(8):  # enough to catch a period that started last week
        day = moment.date() - timedelta(days=offset)
        if window.first_day is not None and day.weekday() != window.first_day:
            continue
        anchor = datetime.combine(day, window.start)
        if anchor <= moment < _period_end(window, anchor):
            return True
    return False


def split_rules(text: str) -> list[str]:
    """The ``fragment=windows`` items of an ``alert_quiet_urls`` setting."""
    return [part.strip() for part in str(text or "").split(";") if part.strip()]


def parse_rule(item: str) -> tuple[str, str] | None:
    """``"news.example.com="`` -> ``("news.example.com", "")``.

    ``None`` when the item has no ``=`` or no URL fragment before it, which is
    what a typo looks like and should not silently silence a whole host.
    """
    fragment, separator, spec = str(item or "").partition("=")
    fragment = fragment.strip()
    if not separator or not fragment:
        return None
    return fragment, spec.strip()


def invalid_rules(text: str) -> list[str]:
    """The rule items that make no sense (for a clean settings error)."""
    broken: list[str] = []
    for item in split_rules(text):
        parsed = parse_rule(item)
        if parsed is None or invalid_windows(parsed[1]):
            broken.append(item)
    return broken


def compile_rules(text: str) -> tuple[tuple[str, tuple[QuietWindow, ...]], ...]:
    """Validate and compile per-URL rules into ``(fragment, windows)`` pairs.

    Fragments are lower-cased for matching. A rule whose windows are unreadable
    is dropped rather than treated as "never quiet", so a typo cannot turn a
    quiet host into a chatty one.
    """
    rules: list[tuple[str, tuple[QuietWindow, ...]]] = []
    for item in split_rules(text):
        parsed = parse_rule(item)
        if parsed is None:
            continue
        fragment, spec = parsed
        windows = tuple(parse_windows(spec))
        if spec and not windows:
            continue
        rules.append((fragment.lower(), windows))
    return tuple(rules)


def silent_now(
    moment: datetime,
    url: str,
    global_text: str = "",
    rules: Sequence[tuple[str, tuple[QuietWindow, ...]]] = (),
) -> bool:
    """True when alerts about ``url`` should be held at ``moment``.

    The first rule whose fragment appears in the URL decides (its windows
    replace the global ones - an empty list means "never quiet"); URLs without a
    matching rule fall back to ``global_text``.
    """
    target = str(url or "").lower()
    for fragment, windows in rules:
        if fragment in target:
            return any(covers(window, moment) for window in windows)
    return in_window(moment, global_text)


def _period_end(window: QuietWindow, anchor: datetime) -> datetime:
    """When the period that starts at ``anchor`` ends (adds days as needed)."""
    if window.first_day is None:
        end = anchor.replace(hour=window.end.hour, minute=window.end.minute)
        return end + timedelta(days=1) if end <= anchor else end
    span = (window.last_day - window.first_day) % 7
    end = datetime.combine((anchor + timedelta(days=span)).date(), window.end)
    if end <= anchor:  # a single-day window that wraps over midnight
        end += timedelta(days=max(span, 1))
    return end


def queue_path(output_dir: str | Path) -> Path:
    """Where deferred alerts wait: ``pending-alerts.json`` in the output folder."""
    return Path(output_dir) / QUEUE_FILENAME


def merge(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """One item per URL and channel - the newest wins - in first-seen order.

    A site that changed on every nightly run must not appear five times in the
    morning mail, so the queue collapses repeats. The channel is part of the
    identity: two named channels (``alert_channels``) can hold the same URL on
    their own schedules, and one must not swallow the other's alert.
    """
    merged: dict[tuple[str, ...], dict[str, Any]] = {}
    order: list[tuple[str, ...]] = []
    for item in items:
        key = item_key(item)
        if key not in merged:
            order.append(key)
        merged[key] = item
    return [merged[key] for key in order]


def item_key(item: dict[str, Any]) -> tuple[str, ...]:
    """What makes two queued items the same: the URL, per tagged channel."""
    tags = item.get("channels") or ()
    channel = ",".join(sorted(str(tag) for tag in tags))
    return (channel, str(item.get("url", "")))


def load(path: str | Path) -> list[dict[str, Any]]:
    """The queued items; an empty list when the file is missing or unreadable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def append(path: str | Path, items: Sequence[dict[str, Any]]) -> int:
    """Add ``items`` to the queue (merged with what waits); returns the queue size.

    Each item is stamped with ``queued_at`` so a status endpoint can report how
    long the oldest alert has been waiting. The write is best-effort: a read-only
    folder logs nothing and loses nothing that mattered, because the caller only
    needs to stay silent.
    """
    stamp = datetime.now().isoformat(timespec="seconds")
    fresh = [{**item, "queued_at": item.get("queued_at") or stamp} for item in items]
    queued = merge(load(path) + fresh)
    target = Path(path)
    try:
        if target.parent and not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(queued, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
    except OSError:  # pragma: no cover - the folder disappeared mid-run
        pass
    return len(queued)


def take(path: str | Path) -> list[dict[str, Any]]:
    """Drain the queue: return what was waiting and delete the file."""
    items = load(path)
    try:
        Path(path).unlink()
    except OSError:
        pass  # already gone
    return items
