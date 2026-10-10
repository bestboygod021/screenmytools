"""Delete old capture images so long-running monitors don't fill the disk.

The SQLite index keeps the history of a run forever, but the screenshots
themselves are large. This module removes capture images older than a cutoff
while protecting the two files the engine still needs: the rolling ``latest_*``
reference and any pinned ``baseline_*`` image.

A second set of rules caps the *history* itself: :func:`prune_history` keeps the
reports plus the SQLite index under a size by forgetting the oldest runs, so a
bot that runs every five minutes for years cannot grow without bound - and
:func:`days_to_cap` turns the recent growth into the only question a cap really
raises: "how long until it bites?"

The folder is also measured *per site* (:func:`site_space`), because "which host
costs the most" is the question that actually leads to a decision - and
:func:`prune_site` acts on the answer without touching anyone else's history.

Forgetting a run is sometimes too much: with ``archive=True`` (the
``history_archive`` setting, ``history --archive``) the reports are zipped into
``archive-YYYY-MM.zip`` first (:func:`archive_reports`) and can be brought back
later with :func:`restore_archive`.

Everything here is best-effort and Qt-free: unreadable entries are skipped and
a missing folder is simply a no-op.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import time
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from app.core.url_utils import build_url_label, sanitize_component

# Every extension the engine can write (see CaptureEngine._file_extension).
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".avif")
# Files that must survive pruning: the comparison references.
PROTECTED_PREFIXES = ("latest_", "baseline_")
# One JSON + one CSV per run, named after the moment the run started.
REPORT_PREFIX = "capture-report-"
REPORT_SUFFIXES = (".json", ".csv")
# The SQLite index the engine maintains next to the reports.
INDEX_FILENAME = "history.sqlite3"


def is_capture_file(path: Path) -> bool:
    """True when ``path`` looks like a capture image we may prune."""
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        return False
    name = path.name
    if name.startswith("."):
        return False
    return not name.startswith(PROTECTED_PREFIXES)


def prune_screenshots(
    output_dir: str | Path,
    older_than_days: int | float,
    now: float | None = None,
    dry_run: bool = False,
) -> list[Path]:
    """Delete capture images older than ``older_than_days``.

    With ``dry_run`` the candidates are returned but nothing is deleted, so the
    caller can show what a retention window would remove.

    Returns the affected paths (empty when the setting is 0, the folder is
    missing, or nothing is old enough).
    """
    days = float(older_than_days or 0)
    if days <= 0:
        return []

    folder = Path(output_dir)
    if not folder.is_dir():
        return []

    cutoff = (now if now is not None else time.time()) - days * 86400.0
    removed: list[Path] = []
    for path in sorted(folder.iterdir()):
        try:
            if not path.is_file() or not is_capture_file(path):
                continue
            if path.stat().st_mtime < cutoff:
                if not dry_run:
                    path.unlink()
                removed.append(path)
        except OSError:  # pragma: no cover - a file we cannot touch must not stop the sweep
            continue
    return removed


def folder_bytes(output_dir: str | Path) -> int:
    """Total size of the files directly inside ``output_dir`` (0 when missing)."""
    folder = Path(output_dir)
    if not folder.is_dir():
        return 0
    total = 0
    try:
        entries = list(folder.iterdir())
    except OSError:  # pragma: no cover - an unreadable folder is treated as empty
        return 0
    for path in entries:
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:  # pragma: no cover - skip what we cannot measure
            continue
    return total


def prune_screenshots_by_size(
    output_dir: str | Path,
    max_mb: int | float,
    dry_run: bool = False,
) -> list[Path]:
    """Delete the oldest capture images until the folder fits under ``max_mb`` (in MB).

    The cap measures the folder as a whole (reports and the index are a few KB
    next to the images), but only capture images are candidates: the rolling
    ``latest_*`` reference and pinned ``baseline_*`` captures survive even when
    that leaves the folder above the cap. Oldest first, so recent history wins.

    Returns the affected paths, oldest first (empty when the cap is 0, the folder
    is missing, or it already fits).
    """
    cap = int(float(max_mb or 0) * 1024 * 1024)
    if cap <= 0:
        return []

    folder = Path(output_dir)
    if not folder.is_dir():
        return []

    try:
        entries = list(folder.iterdir())
    except OSError:  # pragma: no cover - best-effort housekeeping
        return []

    candidates: list[tuple[float, str, Path]] = []
    total = 0
    for path in entries:
        try:
            if not path.is_file():
                continue
            stat = path.stat()
        except OSError:  # pragma: no cover - a file we cannot measure is left alone
            continue
        total += stat.st_size
        if is_capture_file(path):
            candidates.append((stat.st_mtime, path.name, path))

    if total <= cap:
        return []

    candidates.sort()  # oldest mtime first, name as the tie-breaker
    removed: list[Path] = []
    for _mtime, _name, path in candidates:
        if total <= cap:
            break
        try:
            size = path.stat().st_size
            if not dry_run:
                path.unlink()
            total -= size
            removed.append(path)
        except OSError:  # pragma: no cover - a file we cannot delete must not stop the sweep
            continue
    return removed


def _size_of(path: Path) -> int:
    """The file's size in bytes, or 0 when it cannot be measured."""
    try:
        return path.stat().st_size
    except OSError:  # pragma: no cover - a file that vanished mid-sweep
        return 0


def report_files(output_dir: str | Path) -> list[Path]:
    """The engine's ``capture-report-*`` files, oldest first.

    The stamp in the name is ``YYYYMMDD-HHMMSS``, so a plain name sort is also a
    chronological sort - and a run's JSON and CSV stay next to each other.
    """
    folder = Path(output_dir)
    if not folder.is_dir():
        return []
    try:
        found = [
            path
            for path in folder.iterdir()
            if path.is_file()
            and path.name.startswith(REPORT_PREFIX)
            and path.suffix.lower() in REPORT_SUFFIXES
        ]
    except OSError:  # pragma: no cover - an unreadable folder is treated as empty
        return []
    return sorted(found, key=lambda path: (path.name, path.suffix))


def report_groups(output_dir: str | Path) -> list[tuple[Path, ...]]:
    """The report files grouped per run (its JSON and CSV together), oldest first.

    A run is deleted as a whole: leaving half a run behind would make the JSON
    and CSV views disagree about the same timestamp.
    """
    groups: dict[str, list[Path]] = {}
    for path in report_files(output_dir):
        groups.setdefault(path.stem, []).append(path)
    return [tuple(paths) for _stem, paths in sorted(groups.items())]


def history_footprint(output_dir: str | Path) -> int:
    """Bytes the history occupies: the reports plus the SQLite index."""
    total = _size_of(Path(output_dir) / INDEX_FILENAME)
    for path in report_files(output_dir):
        total += _size_of(path)
    return total


# Capture names are ``<index>_<label>_<stamp>.<ext>`` (see ``build_filename``);
# the stamp is the only fixed-width anchor, so the label is whatever sits
# between the index and it.
CAPTURE_NAME_RE = re.compile(
    r"^(?P<index>\d+)_(?P<label>.+)_(?P<stamp>\d{8}-\d{6})(?P<suffix>\.[A-Za-z0-9]+)$"
)


def parse_capture_name(path: str | Path) -> tuple[str, str] | None:
    """``(label, stamp)`` for a capture file name, or ``None`` for anything else.

    ``build_filename`` may insert a user-chosen prefix between the index and the
    label, which is why the label is read as "everything in the middle" rather
    than by counting underscores: the references (``latest_<label>``) and the
    index rows use the same label, so grouping by it keeps the three views in
    step even for someone who renamed their prefix.
    """
    match = CAPTURE_NAME_RE.match(Path(path).name)
    if not match:
        return None
    return match.group("label"), match.group("stamp")


@dataclass(frozen=True)
class SiteSpace:
    """What one site costs on disk, and how far back its captures reach."""

    label: str
    files: int = 0
    bytes: int = 0
    references: int = 0  # latest_/baseline_ files, which are never pruned
    oldest: str = ""  # ``20260101-090000`` stamps, empty when unknown
    newest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "files": self.files,
            "bytes": self.bytes,
            "references": self.references,
            "oldest": self.oldest,
            "newest": self.newest,
        }


@dataclass(frozen=True)
class SiteRemoval:
    """The captures one :func:`prune_site` sweep deleted (or would delete)."""

    site: str = ""
    paths: tuple[Path, ...] = ()
    bytes: int = 0
    dry_run: bool = False

    @property
    def removed_anything(self) -> bool:
        return bool(self.paths)

    def summary(self) -> str:
        """``"14 file(s) of staging_example_com (1.8 MB)"``-ish, without units."""
        if not self.paths:
            return "nothing"
        return f"{len(self.paths)} file(s) matching '{self.site}'"

    def to_dict(self) -> dict[str, Any]:
        return {
            "site": self.site,
            "deleted": len(self.paths),
            "bytes": self.bytes,
            "dry_run": self.dry_run,
        }


def site_space(output_dir: str | Path, limit: int = 0) -> list[SiteSpace]:
    """Disk usage per site, biggest first (``limit`` > 0 keeps the top N).

    Reference files count towards their site's footprint - they really do occupy
    the disk - but are tracked separately so a caller can say "1.8 MB of
    screenshots, 2 of them the references we must keep".
    """
    folder = Path(output_dir)
    if not folder.is_dir():
        return []
    try:
        entries = sorted(folder.iterdir())
    except OSError:  # pragma: no cover - an unreadable folder has no sites
        return []

    buckets: dict[str, dict[str, Any]] = {}
    for path in entries:
        parsed = parse_capture_name(path)
        if parsed is not None and is_capture_file(path):
            label, stamp = parsed
        elif path.is_file() and path.name.startswith(PROTECTED_PREFIXES):
            label, stamp = path.stem.split("_", 1)[-1], ""
        else:
            continue
        if not label:
            continue
        bucket = buckets.setdefault(
            label,
            {"label": label, "files": 0, "bytes": 0, "references": 0, "oldest": "", "newest": ""},
        )
        bucket["files"] += 1
        bucket["bytes"] += _size_of(path)
        if not stamp:
            bucket["references"] += 1
        else:
            if not bucket["oldest"] or stamp < bucket["oldest"]:
                bucket["oldest"] = stamp
            if stamp > bucket["newest"]:
                bucket["newest"] = stamp

    ordered = sorted(
        (SiteSpace(**bucket) for bucket in buckets.values()),
        key=lambda item: (-item.bytes, item.label),
    )
    wanted = int(limit or 0)
    return ordered[:wanted] if wanted > 0 else ordered


def site_needles(site: str) -> tuple[str, ...]:
    """The label fragments a ``--site``/``--prune-site`` query stands for.

    A user types ``staging.example.com``; the files say ``staging_example_com``.
    Both spellings, plus the full URL form, are returned so a caller only has to
    ask "does any of these sit inside the label?".
    """
    raw = str(site or "").strip().lower()
    if not raw:
        return ()
    candidates = {raw}
    try:
        candidates.add(sanitize_component(raw))
        candidates.add(build_url_label(raw if "://" in raw else f"https://{raw}"))
    except ValueError:  # pragma: no cover - build_url_label never raises today
        pass
    return tuple(sorted(item for item in candidates if item))


def prune_site(output_dir: str | Path, site: str, dry_run: bool = False) -> SiteRemoval:
    """Delete the captures of one site, matched by a label fragment.

    ``site`` accepts the host the user knows (``staging.example.com``) or the
    label the files carry (``staging_example_com``); see :func:`site_needles`.
    References and reports are never touched: the point is to stop one chatty
    host from filling the disk, not to forget that it is monitored.
    """
    needles = site_needles(site)
    folder = Path(output_dir)
    if not needles or not folder.is_dir():
        return SiteRemoval(site=str(site or ""), dry_run=dry_run)

    removed: list[Path] = []
    freed = 0
    try:
        entries = sorted(folder.iterdir())
    except OSError:  # pragma: no cover - best-effort housekeeping
        return SiteRemoval(site=needles[0], dry_run=dry_run)

    for path in entries:
        if not is_capture_file(path):
            continue
        parsed = parse_capture_name(path)
        haystack = (parsed[0] if parsed else path.name).lower()
        if not any(needle in haystack for needle in needles):
            continue
        size = _size_of(path)
        if not dry_run:
            try:
                path.unlink()
            except OSError:  # pragma: no cover - a locked file is skipped
                continue
        removed.append(path)
        freed += size

    return SiteRemoval(site=needles[0], paths=tuple(removed), bytes=freed, dry_run=dry_run)


#: The key that gives every site without a cap of its own a budget.
SITE_CAP_ANY = "*"


class SiteCapError(ValueError):
    """``site_caps`` text that does not read as ``host=MB`` pairs."""


def parse_site_caps(text: str | dict | None) -> dict[str, float]:
    """``"news.example.com=500, *=1000"`` -> ``{"news.example.com": 500.0, "*": 1000.0}``.

    A bare number with no host means "every site", so ``site_caps = "500"`` reads
    as ``"*=500"`` - the shorthand people type first. The same text can arrive as
    a dict (a profile, an API query) without changing its meaning.
    """
    if text is None:
        return {}
    if isinstance(text, dict):
        pairs: list[tuple[Any, Any]] = list(text.items())
    else:
        raw = str(text).strip()
        if not raw:
            return {}
        pairs = []
        for chunk in raw.replace(";", ",").split(","):
            item = chunk.strip()
            if not item:
                continue
            host, separator, value = item.partition("=")
            if not separator:
                host, value = SITE_CAP_ANY, item
            pairs.append((host, value))

    caps: dict[str, float] = {}
    for host, value in pairs:
        key = str(host).strip().lower() or SITE_CAP_ANY
        try:
            mb = float(str(value).strip())
        except (TypeError, ValueError):
            raise SiteCapError(f"'{value}' is not a size in MB for '{host}'.") from None
        if mb <= 0:
            raise SiteCapError(f"the cap for '{host}' must be more than 0 MB.")
        caps[key] = mb
    return caps


def site_cap_for(label: str, caps: dict[str, float]) -> float:
    """The cap that applies to one site label (``0.0`` = no cap).

    The user writes the host they know (``news.example.com``); the files carry
    ``news_example_com``. Both spellings match, an exact label beats a fragment,
    and ``*`` is the budget for everything left over.
    """
    if not caps:
        return 0.0
    wanted = str(label or "").strip().lower()
    for key, cap in caps.items():
        if key != SITE_CAP_ANY and key == wanted:
            return cap
    for key, cap in caps.items():
        if key == SITE_CAP_ANY:
            continue
        if any(needle in wanted for needle in site_needles(key)):
            return cap
    return float(caps.get(SITE_CAP_ANY, 0.0) or 0.0)


def _friendly_size(size: int | float) -> str:
    """``1500000`` -> ``"1.4 MB"`` (housekeeping output, not a report)."""
    value = float(size or 0)
    if value < 1024:
        return f"{value:.0f} B"
    for unit in ("KB", "MB", "GB"):
        value /= 1024
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} GB"  # pragma: no cover - unreachable, kept for the type checker


@dataclass(frozen=True)
class SiteTrim:
    """What one site's own budget cost it: the cap, the freed bytes, what is left."""

    label: str = ""
    cap_mb: float = 0.0
    paths: tuple[Path, ...] = ()
    bytes: int = 0  # freed
    kept_bytes: int = 0  # capture bytes still on disk
    reference_bytes: int = 0  # latest_/baseline_ files, which are never deleted
    dry_run: bool = False

    @property
    def removed_anything(self) -> bool:
        return bool(self.paths)

    @property
    def still_over(self) -> bool:
        """True when the references alone keep the site above its cap."""
        return self.kept_bytes + self.reference_bytes > self.cap_mb * 1024 * 1024

    def summary(self) -> str:
        """``"news_example_com: deleted 12 file(s), 1.8 MB freed, cap 500.0 MB"``."""
        verb = "would delete" if self.dry_run else "deleted"
        return (
            f"{self.label}: {verb} {len(self.paths)} file(s), "
            f"{_friendly_size(self.bytes)} freed, cap {self.cap_mb:g} MB"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "site": self.label,
            "cap_mb": self.cap_mb,
            "deleted": len(self.paths),
            "bytes": self.bytes,
            "kept_bytes": self.kept_bytes,
            "reference_bytes": self.reference_bytes,
            "still_over": self.still_over,
            "dry_run": self.dry_run,
        }


def apply_site_caps(
    output_dir: str | Path,
    caps: str | dict | None,
    dry_run: bool = False,
    limit: int = 0,
) -> list[SiteTrim]:
    """Trim every site over its own budget, oldest captures first.

    The sibling of :func:`prune_screenshots_by_size` for the case that actually
    fills a disk - one chatty host. Each site gets its own cap (``*`` covers the
    rest) and only that site's oldest *capture* files are deleted: the rolling
    ``latest_*`` reference, the pinned baselines and the reports stay, because
    forgetting that a site is monitored is not housekeeping. A site whose
    references alone are bigger than its cap is reported (``still_over``) rather
    than pruned further.

    Returns one :class:`SiteTrim` per site that was over budget, biggest site
    first (``limit`` > 0 keeps the N biggest). Raises :class:`SiteCapError` for
    text that is not ``host=MB`` pairs.
    """
    wanted = parse_site_caps(caps)
    folder = Path(output_dir)
    if not wanted or not folder.is_dir():
        return []

    captures: dict[str, list[tuple[str, Path]]] = {}
    sizes: dict[str, int] = {}
    references: dict[str, int] = {}
    try:
        entries = sorted(folder.iterdir())
    except OSError:  # pragma: no cover - best-effort housekeeping
        return []

    for path in entries:
        parsed = parse_capture_name(path)
        if parsed is not None and is_capture_file(path):
            label, stamp = parsed
            captures.setdefault(label, []).append((stamp, path))
            sizes[label] = sizes.get(label, 0) + _size_of(path)
        elif path.is_file() and path.name.startswith(PROTECTED_PREFIXES):
            label = path.stem.split("_", 1)[-1]
            if label:
                references[label] = references.get(label, 0) + _size_of(path)

    trims: list[SiteTrim] = []
    for label in sorted(sizes, key=lambda name: (-sizes[name], name)):
        cap_mb = site_cap_for(label, wanted)
        if cap_mb <= 0:
            continue
        cap_bytes = int(cap_mb * 1024 * 1024)
        left = sizes[label]
        if left <= cap_bytes:
            continue
        removed: list[Path] = []
        freed = 0
        for _stamp, path in sorted(captures[label]):  # oldest first
            if left <= cap_bytes:
                break
            size = _size_of(path)
            if not dry_run:
                try:
                    path.unlink()
                except OSError:  # pragma: no cover - a locked file is skipped
                    continue
            removed.append(path)
            freed += size
            left -= size
        trims.append(
            SiteTrim(
                label=label,
                cap_mb=cap_mb,
                paths=tuple(removed),
                bytes=freed,
                kept_bytes=left,
                reference_bytes=references.get(label, 0),
                dry_run=dry_run,
            )
        )

    wanted_limit = int(limit or 0)
    return trims[:wanted_limit] if wanted_limit > 0 else trims


@dataclass(frozen=True)
class ArchiveResult:
    """What one archiving sweep did (or would do, with ``dry_run``)."""

    archives: tuple[Path, ...] = ()
    files: tuple[Path, ...] = ()
    bytes_before: int = 0
    bytes_after: int = 0
    dry_run: bool = False

    @property
    def removed_anything(self) -> bool:
        """True when the sweep had something to report."""
        return bool(self.files)

    def summary(self) -> str:
        """``"6 report file(s) into 1 archive(s)"`` (or ``"nothing"``)."""
        if not self.files:
            return "nothing"
        return f"{len(self.files)} report file(s) into {len(self.archives)} archive(s)"


@dataclass(frozen=True)
class HistoryPrune:
    """What the history size cap removed (or would remove) in one sweep."""

    reports: tuple[Path, ...] = ()
    rows: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    cap_bytes: int = 0
    dry_run: bool = False
    archives: tuple[Path, ...] = ()  # zips the dropped runs were kept in, if any

    @property
    def removed_anything(self) -> bool:
        """True when the sweep had something to report."""
        return bool(self.reports) or self.rows > 0

    def summary(self) -> str:
        """``"3 report file(s) and 120 history row(s)"`` (or ``"nothing"``)."""
        parts: list[str] = []
        if self.reports:
            parts.append(f"{len(self.reports)} report file(s)")
        if self.rows:
            parts.append(f"{self.rows} history row(s)")
        return " and ".join(parts) if parts else "nothing"


#: Month archives of old runs (``archive-2026-01.zip``).
ARCHIVE_PREFIX = "archive-"
ARCHIVE_SUFFIX = ".zip"
#: How many recent runs the growth estimate averages over.
FORECAST_SAMPLE = 20
#: How many recent images the screenshot estimate averages over.
FORECAST_IMAGES = 80
_DAY = 86400.0
#: A growth rate needs a real span: two captures a minute apart would extrapolate
#: a folder to terabytes by the weekend, so anything shorter reports "unknown".
MIN_SPAN_DAYS = 10.0 / 1440.0


def archive_files(output_dir: str | Path) -> list[Path]:
    """The month archives in the folder, oldest first (empty when there are none)."""
    folder = Path(output_dir)
    if not folder.is_dir():
        return []
    try:
        found = [
            path
            for path in folder.iterdir()
            if path.is_file()
            and path.name.startswith(ARCHIVE_PREFIX)
            and path.suffix.lower() == ARCHIVE_SUFFIX
        ]
    except OSError:  # pragma: no cover - an unreadable folder is treated as empty
        return []
    return sorted(found, key=lambda path: path.name)


def archive_bytes(output_dir: str | Path) -> int:
    """Total size of the month archives (0 when there are none)."""
    return sum(_size_of(path) for path in archive_files(output_dir))


def stamp_moment(path: Path) -> datetime | None:
    """When a report was written: from its name, else from the file's mtime."""
    stamp = path.stem[len(REPORT_PREFIX) :]
    try:
        return datetime.strptime(stamp, "%Y%m%d-%H%M%S")
    except ValueError:
        pass
    try:  # pragma: no cover - only for hand-renamed reports
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:  # pragma: no cover - the file vanished mid-sweep
        return None


def run_sizes(output_dir: str | Path, limit: int = FORECAST_SAMPLE) -> list[tuple[datetime, int]]:
    """``(started_at, bytes)`` for the newest ``limit`` runs, oldest first."""
    samples: list[tuple[datetime, int]] = []
    for group in report_groups(output_dir)[-max(1, int(limit)) :]:
        moment = stamp_moment(group[0])
        if moment is not None:
            samples.append((moment, sum(_size_of(path) for path in group)))
    return samples


def history_growth_per_day(output_dir: str | Path, limit: int = FORECAST_SAMPLE) -> float:
    """Bytes per day the history grows at, from the median run and the run rate.

    The median (not the mean) is deliberate: one 500 MB run must not make the
    forecast look hopeless, and one quiet week must not make it look empty. Fewer
    than two runs carry no rate at all, so they report 0.
    """
    samples = run_sizes(output_dir, limit)
    if len(samples) < 2:
        return 0.0
    span_days = (samples[-1][0] - samples[0][0]).total_seconds() / _DAY
    if span_days < MIN_SPAN_DAYS:
        return 0.0
    return median([size for _moment, size in samples]) * len(samples) / span_days


def screenshot_growth_per_day(output_dir: str | Path, limit: int = FORECAST_IMAGES) -> float:
    """Bytes per day the capture images grow at (mtime-based, median capture size)."""
    folder = Path(output_dir)
    if not folder.is_dir():
        return 0.0
    entries: list[tuple[float, int]] = []
    try:
        candidates = list(folder.iterdir())
    except OSError:  # pragma: no cover - an unreadable folder has no rate
        return 0.0
    for path in candidates:
        if not is_capture_file(path):
            continue
        try:
            stat = path.stat()
        except OSError:  # pragma: no cover - skip what we cannot measure
            continue
        entries.append((stat.st_mtime, stat.st_size))
    if len(entries) < 2:
        return 0.0
    entries.sort()
    recent = entries[-max(2, int(limit)) :]
    span_days = (recent[-1][0] - recent[0][0]) / _DAY
    if span_days < MIN_SPAN_DAYS:
        return 0.0
    return median([size for _moment, size in recent]) * len(recent) / span_days


def series_rate(
    samples: list[tuple[datetime, float | int]], min_span_days: float = 0.0
) -> float | None:
    """Bytes per day from a ``(moment, bytes)`` series, by least squares.

    A regression is the honest answer to "how fast is this growing?": a median of
    run sizes ignores *when* the runs happened, so three busy mornings in a quiet
    week look exactly like a steady drip. The slope is clamped at 0 (a folder
    that shrank is not growing) and ``None`` means "cannot tell" - fewer than two
    samples, or every sample at the same instant, in which case there is no
    slope to fit.

    ``min_span_days`` refuses to extrapolate from a burst: a handful of runs
    inside ten minutes says nothing about a day. Callers compare it against
    :data:`MIN_SPAN_DAYS`.
    """
    points = [(moment, float(value)) for moment, value in samples if value is not None]
    if len(points) < 2:
        return None
    points.sort(key=lambda item: item[0])
    origin = points[0][0]
    xs = [(moment - origin).total_seconds() / _DAY for moment, _value in points]
    ys = [value for _moment, value in points]
    if (max(xs) - min(xs)) < max(0.0, float(min_span_days or 0.0)):
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    spread = sum((x - mean_x) ** 2 for x in xs)
    if spread <= 0:  # pragma: no cover - guarded by the span check above
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / spread
    return max(0.0, slope)


def daily_delta(samples: list[tuple[datetime, float | int]]) -> list[tuple[str, int]]:
    """Net bytes added per calendar day, from an oldest-first ``(moment, bytes)``.

    Each day is compared with the last sample of the previous day *that has
    samples*, so a gap in the record (a stopped bot, a quiet weekend) is spread
    over the days it covers instead of looking like a spike on the day the bot
    came back. Negative values are kept: a retention rule that fired really did
    free that space, and a chart that hid it would be a nicer lie.
    """
    last_of_day: dict[str, int] = {}
    order: list[str] = []
    for moment, value in samples:
        if value is None:  # pragma: no cover - callers pass measured samples
            continue
        day = moment.strftime("%Y-%m-%d")
        if day not in last_of_day:
            order.append(day)
        last_of_day[day] = int(value)
    return [
        (day, last_of_day[day] - last_of_day[previous])
        for previous, day in zip(order, order[1:], strict=False)
    ]


def daily_growth(output_dir: str | Path) -> list[tuple[str, int]]:
    """Net history bytes per day, from the run sizes (the no-samples fallback).

    When the index has no storage samples yet, the reports still say what each
    run cost and when it happened - which is enough to draw days.
    """
    return daily_delta([(moment, size) for moment, size in run_sizes(output_dir)])


def days_to_cap(used_bytes: int, cap_bytes: int, per_day: float) -> float | None:
    """Days until ``used_bytes`` reaches ``cap_bytes`` at ``per_day``.

    ``None`` means "not knowable" (no cap, or nothing growing); ``0.0`` means the
    folder is at or over the cap already, which is a different message.
    """
    if cap_bytes <= 0:
        return None
    if used_bytes >= cap_bytes:
        return 0.0
    if per_day <= 0:
        return None
    return (cap_bytes - used_bytes) / per_day


def archive_name_for(moment: datetime) -> str:
    """The archive a run belongs to: ``2026-01`` -> ``archive-2026-01.zip``."""
    return f"{ARCHIVE_PREFIX}{moment.strftime('%Y-%m')}{ARCHIVE_SUFFIX}"


def archive_reports(
    output_dir: str | Path,
    paths: Iterable[str | Path],
    dry_run: bool = False,
) -> ArchiveResult:
    """Move report files into the month archive of the run they came from.

    A month piles into one zip (the archive is opened in append mode), a name
    that is already inside is skipped instead of duplicated, and the originals
    are removed **only after** the archive holds them - a folder that cannot be
    written keeps its reports.
    """
    folder = Path(output_dir)
    files = [Path(item) for item in paths]
    sizes = {item: _size_of(item) for item in files}
    grouped: dict[Path, list[Path]] = {}
    for item in files:
        moment = stamp_moment(item)
        if moment is None:
            continue
        grouped.setdefault(folder / archive_name_for(moment), []).append(item)

    archives: list[Path] = []
    moved: list[Path] = []
    for archive, group in sorted(grouped.items()):
        if dry_run:
            archives.append(archive)
            moved.extend(group)
            continue
        try:
            with zipfile.ZipFile(archive, "a", compression=zipfile.ZIP_DEFLATED) as handle:
                existing = set(handle.namelist())
                for item in group:
                    if item.name not in existing:
                        handle.write(item, arcname=item.name)
        except (OSError, zipfile.BadZipFile):  # pragma: no cover - unwritable folder
            continue
        for item in group:
            try:
                item.unlink()
            except OSError:  # pragma: no cover - best-effort housekeeping
                continue
            moved.append(item)
        archives.append(archive)

    before = sum(sizes.get(item, 0) for item in moved)
    return ArchiveResult(
        archives=tuple(archives),
        files=tuple(moved),
        bytes_before=before,
        bytes_after=sum(_size_of(item) for item in archives),
        dry_run=dry_run,
    )


def archive_runs(
    output_dir: str | Path,
    older_than_days: int | float,
    dry_run: bool = False,
    now: datetime | None = None,
) -> ArchiveResult:
    """Archive every report run older than ``older_than_days`` days, a run whole."""
    days = float(older_than_days or 0)
    if days <= 0:
        return ArchiveResult(dry_run=dry_run)
    cutoff = (now or datetime.now()) - timedelta(days=days)
    paths: list[Path] = []
    for group in report_groups(output_dir):
        moment = stamp_moment(group[0])
        if moment is None or moment >= cutoff:
            continue
        paths.extend(group)
    return archive_reports(output_dir, paths, dry_run=dry_run)


def restore_archive(
    output_dir: str | Path,
    archive: str | Path,
    dry_run: bool = False,
    overwrite: bool = False,
) -> list[Path]:
    """Extract a month archive back into the history folder.

    Only ``capture-report-*`` entries travel (an archive from elsewhere is not a
    way to write arbitrary files) and an existing report is kept unless
    ``overwrite`` is set. Raises ``ValueError`` when the archive cannot be read.
    """
    folder = Path(output_dir)
    source = Path(archive)
    restored: list[Path] = []
    try:
        with zipfile.ZipFile(source) as handle:
            for name in handle.namelist():
                base = Path(name).name
                if not base.startswith(REPORT_PREFIX):
                    continue
                if Path(base).suffix.lower() not in REPORT_SUFFIXES:
                    continue
                destination = folder / base
                if destination.exists() and not overwrite:
                    continue
                restored.append(destination)
                if dry_run:
                    continue
                folder.mkdir(parents=True, exist_ok=True)
                with handle.open(name) as member:
                    destination.write_bytes(member.read())
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError(f"Could not read {source.name}: {exc}") from exc
    return restored


def prune_history(
    output_dir: str | Path,
    max_mb: int | float,
    dry_run: bool = False,
    archive: bool = False,
) -> HistoryPrune:
    """Keep the history (reports + SQLite index) under ``max_mb`` (in MB).

    The oldest report files go first, a run at a time; only when the index alone
    is still too big are its oldest rows dropped, and the newest capture always
    survives - both as a report and as an index row - so a cap smaller than a
    single run cannot leave an empty history behind. Deleting a report forgets
    that run for the JSON readers, which is why the cap is off by default and
    ``dry_run`` reports what would go without touching anything.

    With ``archive=True`` the dropped runs are zipped into their month archive
    first, so the cap buys disk space without losing the data for good.
    """
    cap = int(float(max_mb or 0) * 1024 * 1024)
    folder = Path(output_dir)
    before = history_footprint(folder)
    if cap <= 0 or not folder.is_dir() or before <= cap:
        return HistoryPrune(bytes_before=before, bytes_after=before, cap_bytes=cap, dry_run=dry_run)

    total = before
    removed: list[Path] = []
    archived: list[Path] = []
    groups = report_groups(folder)
    newest = groups[-1] if groups else ()
    for group in groups:
        if total <= cap:
            break
        if group == newest:
            continue  # the newest run survives, exactly like the newest index row
        try:
            sizes = [(path, path.stat().st_size) for path in group]
        except OSError:  # pragma: no cover - a run we cannot measure is skipped
            continue
        if archive:
            result = archive_reports(folder, [path for path, _size in sizes], dry_run=dry_run)
            archived.extend(result.archives)
            if not dry_run and not result.files:
                continue  # the zip could not be written: the reports stay put
        elif not dry_run:
            for path, _size in sizes:
                try:
                    path.unlink()
                except OSError:  # pragma: no cover - best-effort housekeeping
                    continue
        for path, size in sizes:
            total -= size
            removed.append(path)

    rows = 0
    index_bytes = _size_of(folder / INDEX_FILENAME)
    if total > cap and index_bytes > 0:
        kept_reports = max(total - index_bytes, 0)
        budget = max(cap - kept_reports, 0)
        if dry_run:
            rows = _rows_over_budget(folder / INDEX_FILENAME, budget)
            total = kept_reports + min(index_bytes, budget)
        else:
            rows = prune_index_to_size(folder / INDEX_FILENAME, budget)
            total = history_footprint(folder)

    return HistoryPrune(
        reports=tuple(removed),
        rows=rows,
        bytes_before=before,
        bytes_after=total,
        cap_bytes=cap,
        dry_run=dry_run,
        archives=tuple(sorted(set(archived))),
    )


def prune_index_to_size(db_path: str | Path, max_bytes: int) -> int:
    """Drop the oldest index rows until ``db_path`` fits under ``max_bytes``.

    A missing or unreadable index is not an error - the reports remain the source
    of truth, so there is simply nothing to trim.
    """
    target = Path(db_path)
    if not target.exists():
        return 0
    from app.core.store import HistoryStore

    try:
        with HistoryStore(target) as store:
            return store.prune_to_size(max_bytes)
    except Exception:  # noqa: BLE001 - housekeeping must never break a capture run
        return 0


def _rows_over_budget(db_path: Path, max_bytes: int) -> int:
    """How many rows a real trim would drop, measured on a throwaway copy."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / db_path.name
            shutil.copy2(db_path, copy)
            return prune_index_to_size(copy, max_bytes)
    except OSError:  # pragma: no cover - an unreadable index is reported as zero rows
        return 0
