"""Two periods side by side: what grew, what changed, what went quiet.

"Which sites changed the most this month?" is a question a retention rule cannot
answer: the reports hold the answer, but reading thirty of them by hand is not a
report. This module turns two date ranges into one table - captures, changes and
bytes per site - plus a per-site delta, so a monthly meeting has numbers instead
of impressions.

    python -m app.cli history --dir ./shots --compare -30d:today -60d:-30d

A period is ``START:END``. Either side may be left out (``:today`` is "everything
up to now", ``-30d:`` is "the last thirty days"), each side accepts ``today``,
``now``, ``YYYY-MM-DD``, ``YYYY-MM-DDTHH:MM`` and relative forms like ``-30d``,
``-12h``, ``-45m``, and a bare value means "from there until now".

The report files stay the source of truth: bytes are measured from the capture
files still on disk, so a folder that pruned a month ago honestly reports less
than it wrote.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

#: Sites shown when the caller does not ask for a number.
DEFAULT_LIMIT = 12


class PeriodError(ValueError):
    """A period that cannot be read, with a message a user can act on."""


@dataclass(frozen=True)
class Period:
    """A half-open range ``[start, end)`` with a label for the table header."""

    start: datetime
    end: datetime
    label: str = ""

    @property
    def days(self) -> float:
        """How long the period is, in days (0.0 for a zero-length range)."""
        return max(0.0, (self.end - self.start).total_seconds() / 86400.0)

    def contains(self, stamp: str) -> bool:
        """Whether a report stamp falls inside the period."""
        moment = parse_stamp(stamp)
        return moment is not None and self.start <= moment < self.end

    def text(self) -> str:
        """``2026-09-01..2026-09-30 (29d)``."""
        start = self.start.strftime("%Y-%m-%d")
        end = (self.end - timedelta(seconds=1)).strftime("%Y-%m-%d")
        return f"{start}..{end} ({self.days:.0f}d)"

    def to_dict(self) -> dict[str, Any]:
        """Plain data for ``--json``."""
        return {
            "label": self.label or self.text(),
            "start": self.start.isoformat(timespec="seconds"),
            "end": self.end.isoformat(timespec="seconds"),
            "days": round(self.days, 2),
        }


def parse_stamp(text: str) -> datetime | None:
    """A report stamp as a datetime, or ``None`` when it is not one."""
    cleaned = str(text or "").strip().replace("Z", "")
    if not cleaned:
        return None
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        pass
    for shape in ("%Y-%m-%d %H:%M:%S", "%Y%m%d-%H%M%S"):
        try:
            return datetime.strptime(cleaned, shape)
        except ValueError:
            continue
    return None


def _token(text: str, now: datetime) -> datetime:
    """One side of a period: a date, a keyword, or a relative shift."""
    word = text.strip().lower().rstrip(".")
    if word in ("", "start", "beginning"):
        return datetime.min
    if word == "end":
        # One second past now, so a report stamped this very second still counts.
        return now + timedelta(seconds=1)
    if word in ("now", "today-end"):
        return now
    if word == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    # ``-30d`` and ``30d`` both mean "thirty days ago"; the dash-less spelling
    # exists because argparse reads a value that starts with ``-`` as a flag.
    relative = re.fullmatch(r"-?(\d+(?:\.\d+)?)([dhm])(?:ago)?", word)
    if relative:
        amount = float(relative.group(1))
        unit = {"d": 86400.0, "h": 3600.0, "m": 60.0}[relative.group(2)]
        return now - timedelta(seconds=amount * unit)
    for shape in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), shape)
        except ValueError:
            continue
    raise PeriodError(
        f"'{text.strip()}' is not a date range end; use today, YYYY-MM-DD, "
        "YYYY-MM-DDTHH:MM or -30d/-12h/-45m."
    )


#: One side of a period: a keyword, a date, or "how long ago".
_TOKEN = (
    r"(?:today|now|end|start|beginning"
    r"|\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?"
    r"|-?\d+(?:\.\d+)?[dhm](?:ago)?)"
)

#: ``START:END`` - or ``START..END``, which avoids the colon inside an ISO time.
_SPEC = re.compile(rf"^\s*({_TOKEN})?\s*(?::|\.\.)\s*({_TOKEN})?\s*$", re.IGNORECASE)


def parse_period(text: str, now: datetime | None = None, label: str = "") -> Period:
    """``"START:END"`` -> :class:`Period`. Raises :class:`PeriodError`.

    Either side may be left out (``:today`` is "everything up to now", ``30d:``
    is "the last thirty days"). Use ``..`` instead of ``:`` when a side carries a
    time, or simply write the time first - the split understands both.
    """
    now = now or datetime.now()
    raw = str(text or "").strip()
    if not raw:
        raise PeriodError("An empty period; use something like 30d:today.")
    match = _SPEC.match(raw)
    if match:
        left, right = match.group(1) or "", match.group(2) or ""
        if not left.strip() and not right.strip():
            raise PeriodError("An empty period; use something like 30d:today.")
        start = _token(left, now) if left.strip() else datetime.min
        end = _token(right, now) if right.strip() else now
    else:
        start = _token(raw, now)
        end = now
    if end <= start:
        raise PeriodError(f"'{raw}' ends before it starts ({start} >= {end}).")
    return Period(start=start, end=end, label=label or f"{text}")


def mirror_period(period: Period, label: str = "the period before") -> Period:
    """ "The window just before this one, of the same length.

    ``--compare 30d:today`` compares the last thirty days with the thirty days
    before them - the question everyone actually has - without spelling the older
    window out every month.
    """
    length = period.end - period.start
    if period.start == datetime.min or length <= timedelta(0):
        return Period(start=datetime.min, end=period.start, label="everything before")
    return Period(start=period.start - length, end=period.start, label=label)


def site_rows(folder: str | Path, period: Period) -> dict[str, dict[str, Any]]:
    """Per-site totals for one period, keyed by the site label."""
    from app.core import history

    totals: dict[str, dict[str, Any]] = {}
    for run in history.load_runs(folder):
        stamp = str(run.get("generated_at", ""))
        if not period.contains(stamp):
            continue
        for row in run.get("results") or []:
            # Only captures that happened: a site whose runs all failed is not a
            # site that "went quiet", it is a site that is broken - and the
            # watchdog says so louder than a table can.
            if str(row.get("status") or "").lower() not in ("", "success"):
                continue
            url = str(row.get("url") or "")
            if not url:
                continue
            label = str(row.get("label") or history.build_url_label(url))
            entry = totals.setdefault(
                label, {"site": label, "url": url, "captures": 0, "changes": 0, "bytes": 0}
            )
            entry["captures"] += 1
            diff = row.get("diff")
            if diff is not None and float(diff or 0.0) > 0:
                entry["changes"] += 1
            entry["bytes"] += _file_size(row.get("file_path") or row.get("file") or "")
    return totals


def _file_size(path: str) -> int:
    """The capture file's size, or 0 when it is gone (pruned, moved)."""
    if not path:
        return 0
    try:
        return int(Path(path).stat().st_size)
    except OSError:
        return 0


@dataclass
class SiteChange:
    """One site's numbers in both periods, with the difference."""

    site: str
    url: str = ""
    captures_a: int = 0
    captures_b: int = 0
    changes_a: int = 0
    changes_b: int = 0
    bytes_a: int = 0
    bytes_b: int = 0

    @property
    def delta_captures(self) -> int:
        return self.captures_b - self.captures_a

    @property
    def delta_changes(self) -> int:
        return self.changes_b - self.changes_a

    @property
    def delta_bytes(self) -> int:
        return self.bytes_b - self.bytes_a

    def status(self) -> str:
        """``new``/``gone``/``busier``/``quieter``/``steady`` - for the table."""
        if not self.captures_a and self.captures_b:
            return "new"
        if self.captures_a and not self.captures_b:
            return "gone"
        if self.delta_changes > 0 or self.delta_bytes > 0:
            return "busier"
        if self.delta_changes < 0 or self.delta_bytes < 0:
            return "quieter"
        return "steady"

    def to_dict(self) -> dict[str, Any]:
        """Plain data for ``--json``."""
        return {
            "site": self.site,
            "url": self.url,
            "status": self.status(),
            "captures": {"a": self.captures_a, "b": self.captures_b, "delta": self.delta_captures},
            "changes": {"a": self.changes_a, "b": self.changes_b, "delta": self.delta_changes},
            "bytes": {"a": self.bytes_a, "b": self.bytes_b, "delta": self.delta_bytes},
        }


@dataclass
class Comparison:
    """The two periods side by side, plus one row per site."""

    first: Period
    second: Period
    sites: list[SiteChange] = field(default_factory=list)
    totals: SiteChange = field(default_factory=lambda: SiteChange(site="all sites"))

    @property
    def busy(self) -> bool:
        """Whether anything at all was captured in either period."""
        return bool(self.totals.captures_a or self.totals.captures_b)

    def movers(self, limit: int = DEFAULT_LIMIT) -> list[SiteChange]:
        """The sites that moved most, biggest absolute change first."""
        ranked = sorted(
            self.sites,
            key=lambda entry: (
                abs(entry.delta_changes) * 1_000_000 + abs(entry.delta_bytes),
                entry.captures_b + entry.captures_a,
            ),
            reverse=True,
        )
        return ranked[:limit] if limit > 0 else ranked

    def to_dict(self, limit: int = 0) -> dict[str, Any]:
        """Plain data for ``--json`` (``limit`` keeps the biggest movers only)."""
        sites = self.movers(limit) if limit else self.sites
        return {
            "periods": {"a": self.first.to_dict(), "b": self.second.to_dict()},
            "totals": self.totals.to_dict(),
            "sites": [entry.to_dict() for entry in sites],
            "site_count": len(self.sites),
        }

    def summary(self, limit: int = DEFAULT_LIMIT) -> str:
        """The text table a terminal shows."""
        from app.core.dashboard import human_bytes

        lines = [
            f"Comparing {self.first.text()} with {self.second.text()}",
            f"  {'SITE':<24} {'CAPTURES':>14}  {'CHANGES':>12}  {'BYTES':>26}  STATUS",
        ]
        if not self.busy:
            lines.append("  no captures recorded in either period.")
            return "\n".join(lines)
        for entry in self.movers(limit):
            captures = f"{entry.captures_a} -> {entry.captures_b} ({entry.delta_captures:+d})"
            changes = f"{entry.changes_a} -> {entry.changes_b} ({entry.delta_changes:+d})"
            size = (
                f"{human_bytes(entry.bytes_a)} -> {human_bytes(entry.bytes_b)} "
                f"({human_bytes(abs(entry.delta_bytes))} "
                f"{'more' if entry.delta_bytes >= 0 else 'less'})"
            )
            lines.append(
                f"  {entry.site[:24]:<24} {captures:>14}  {changes:>12}  {size:>26}"
                f"  {entry.status()}"
            )
        hidden = len(self.sites) - len(self.movers(limit))
        if hidden > 0:
            lines.append(f"  ... {hidden} more site(s); use --limit 0 to see them all.")
        total = self.totals
        lines.append(
            f"  {'TOTAL':<24} "
            f"{total.captures_a} -> {total.captures_b} ({total.delta_captures:+d})"
            f"  {total.changes_a} -> {total.changes_b} ({total.delta_changes:+d})"
            f"  {human_bytes(total.bytes_a)} -> {human_bytes(total.bytes_b)}"
        )
        return "\n".join(lines)


def compare_periods(
    folder: str | Path, first: Period, second: Period, now: datetime | None = None
) -> Comparison:
    """Aggregate both periods and pair the sites up."""
    now = now or datetime.now()
    if not first.label:
        first = Period(start=first.start, end=first.end, label=first.text())
    if not second.label:
        second = Period(start=second.start, end=second.end, label=second.text())
    left = site_rows(folder, first)
    right = site_rows(folder, second)
    sites: list[SiteChange] = []
    for site in sorted(set(left) | set(right)):
        a = left.get(site, {})
        b = right.get(site, {})
        sites.append(
            SiteChange(
                site=site,
                url=str(b.get("url") or a.get("url") or ""),
                captures_a=int(a.get("captures", 0)),
                captures_b=int(b.get("captures", 0)),
                changes_a=int(a.get("changes", 0)),
                changes_b=int(b.get("changes", 0)),
                bytes_a=int(a.get("bytes", 0)),
                bytes_b=int(b.get("bytes", 0)),
            )
        )
    totals = SiteChange(
        site="all sites",
        captures_a=sum(entry.captures_a for entry in sites),
        captures_b=sum(entry.captures_b for entry in sites),
        changes_a=sum(entry.changes_a for entry in sites),
        changes_b=sum(entry.changes_b for entry in sites),
        bytes_a=sum(entry.bytes_a for entry in sites),
        bytes_b=sum(entry.bytes_b for entry in sites),
    )
    return Comparison(first=first, second=second, sites=sites, totals=totals)
