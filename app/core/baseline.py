"""Pin a known-good baseline capture per site (Qt-free).

Change detection normally compares each new capture against the previous one
(``latest_<label>.<ext>``), which moves every run. Pinning a baseline copies the
current capture to ``baseline_<label>.<ext>``; the engine then measures the diff
against that fixed reference instead, so "changed" means "different from the
approved baseline" rather than "different from last time".
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from app.core.url_utils import build_url_label

# Every extension the engine can write (see CaptureEngine._file_extension).
KNOWN_EXTS = ("png", "jpg", "webp", "avif")


def latest_path(output_dir: str | Path, url: str, ext: str = "png") -> Path:
    """The rolling 'most recent capture' file the engine maintains."""
    return Path(output_dir) / f"latest_{build_url_label(url)}.{ext}"


def baseline_path(output_dir: str | Path, url: str, ext: str = "png") -> Path:
    """The pinned, known-good reference for ``url`` in a given format."""
    return Path(output_dir) / f"baseline_{build_url_label(url)}.{ext}"


def find_latest(output_dir: str | Path, url: str) -> Path | None:
    """The newest-format 'latest' capture for ``url``, whatever format it is in."""
    for ext in KNOWN_EXTS:
        candidate = latest_path(output_dir, url, ext)
        if candidate.exists():
            return candidate
    return None


def find_baseline(output_dir: str | Path, url: str) -> Path | None:
    """The pinned baseline for ``url`` in any known format, or ``None``."""
    for ext in KNOWN_EXTS:
        candidate = baseline_path(output_dir, url, ext)
        if candidate.exists():
            return candidate
    return None


def has_baseline(output_dir: str | Path, url: str) -> bool:
    return find_baseline(output_dir, url) is not None


def pin_baseline(output_dir: str | Path, url: str) -> Path | None:
    """Copy the latest capture to the baseline. Returns the path, or ``None``.

    ``None`` means there is no latest capture yet to promote.
    """
    src = find_latest(output_dir, url)
    if src is None:
        return None
    dst = baseline_path(output_dir, url, src.suffix.lstrip(".") or "png")
    shutil.copyfile(src, dst)
    return dst


def clear_baseline(output_dir: str | Path, url: str) -> bool:
    """Remove any pinned baseline for ``url``. Returns ``True`` if one was deleted."""
    found = find_baseline(output_dir, url)
    if found is None:
        return False
    found.unlink()
    return True


# -- staleness -------------------------------------------------------------
def baseline_age_days(output_dir: str | Path, url: str) -> float | None:
    """Days since the baseline was pinned, or ``None`` when there is no baseline."""
    found = find_baseline(output_dir, url)
    if found is None:
        return None
    try:
        pinned_at = found.stat().st_mtime
    except OSError:  # pragma: no cover - file vanished between find and stat
        return None
    return max(0.0, (time.time() - pinned_at) / 86400.0)


def is_baseline_stale(output_dir: str | Path, url: str, max_age_days: int | float) -> bool:
    """True when the pinned baseline is older than ``max_age_days``.

    ``max_age_days <= 0`` disables the check, and a site without a baseline is
    never reported as stale (there is nothing to refresh).
    """
    if float(max_age_days or 0) <= 0:
        return False
    age = baseline_age_days(output_dir, url)
    return age is not None and age > float(max_age_days)


def stale_baselines(
    output_dir: str | Path, urls: list[str], max_age_days: int | float
) -> list[str]:
    """The subset of ``urls`` whose pinned baseline has gone stale."""
    return [url for url in urls if is_baseline_stale(output_dir, url, max_age_days)]


def baseline_drift(output_dir: str | Path, url: str) -> float | None:
    """How far the newest capture has drifted from the pinned baseline (0..1).

    ``None`` when there is no baseline, no rolling ``latest_*`` capture, or the
    images cannot be compared (Pillow missing / undecodable).
    """
    pinned = find_baseline(output_dir, url)
    newest = find_latest(output_dir, url)
    if pinned is None or newest is None:
        return None

    from app.core.imagediff import diff_ratio  # local: keeps this module Pillow-free

    try:
        return diff_ratio(pinned.read_bytes(), newest.read_bytes())
    except Exception:  # noqa: BLE001 - an unreadable pair simply has no drift value
        return None
