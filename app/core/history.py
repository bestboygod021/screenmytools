"""Build a per-site change timeline from the timestamped report files (Qt-free).

Each run already writes ``capture-report-<stamp>.json``; this module scans them
and flattens the results into a chronological timeline so the UI can show how
each site's captures (and visual diffs) evolved over time. Nothing here raises
on a missing or corrupt file - it is simply skipped.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from app.core.url_utils import build_url_label

#: Column order of the CSV export (and of the digest attachment).
CSV_COLUMNS = ("timestamp", "url", "label", "status", "diff", "drift", "file")


def load_runs(output_dir: str | Path) -> list[dict[str, Any]]:
    """Return every report in ``output_dir``, oldest first."""
    runs: list[dict[str, Any]] = []
    directory = Path(output_dir)
    if not directory.exists():
        return runs
    for path in sorted(directory.glob("capture-report-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            runs.append(data)
    runs.sort(key=lambda run: run.get("generated_at", ""))
    return runs


def flat_rows(output_dir: str | Path) -> list[dict[str, Any]]:
    """Flatten all runs into per-URL rows, newest first."""
    rows: list[dict[str, Any]] = []
    for run in load_runs(output_dir):
        timestamp = run.get("generated_at", "")
        for item in run.get("results", []):
            url = item.get("url", "")
            rows.append(
                {
                    "timestamp": timestamp,
                    "url": url,
                    "label": build_url_label(url),
                    "status": item.get("status", ""),
                    "diff": item.get("diff"),
                    "drift": item.get("drift"),
                    "file": item.get("file_path", ""),
                }
            )
    rows.sort(key=lambda row: row["timestamp"], reverse=True)
    return rows


def sites(output_dir: str | Path) -> list[str]:
    """Distinct URLs seen across the history, in first-seen order (newest first)."""
    seen: list[str] = []
    for row in flat_rows(output_dir):
        if row["url"] not in seen:
            seen.append(row["url"])
    return seen


def timeline_for_url(output_dir: str | Path, url: str) -> list[dict[str, Any]]:
    """Chronological (newest-first) history for one URL."""
    return [row for row in flat_rows(output_dir) if row["url"] == url]


def trend_for_url(output_dir: str | Path, url: str) -> list[dict[str, Any]]:
    """Chronological (oldest-first) diff/drift series for one URL, for charting."""
    rows = [
        {"timestamp": row["timestamp"], "diff": row["diff"], "drift": row.get("drift")}
        for row in timeline_for_url(output_dir, url)
    ]
    rows.reverse()  # timeline_for_url is newest-first; charts read oldest-first
    return rows


def drift_for_url(output_dir: str | Path, url: str) -> list[dict[str, Any]]:
    """Oldest-first drift-vs-baseline points, skipping runs without a drift value."""
    return [
        {"timestamp": point["timestamp"], "drift": point["drift"]}
        for point in trend_for_url(output_dir, url)
        if point.get("drift") is not None
    ]


def site_change_counts(output_dir: str | Path) -> dict[str, int]:
    """How many times each site registered a visual change (diff > 0)."""
    counts: dict[str, int] = {}
    for row in flat_rows(output_dir):
        diff = row["diff"]
        if diff is not None and diff > 0:
            counts[row["url"]] = counts.get(row["url"], 0) + 1
    return counts


def filter_rows(rows: Iterable[dict[str, Any]], url_filter: str = "") -> list[dict[str, Any]]:
    """Rows whose URL (or label) contains ``url_filter``, case-insensitively.

    An empty filter keeps everything, so callers can pass a query-string value
    straight through without a guard.
    """
    wanted = (url_filter or "").strip().lower()
    if not wanted:
        return list(rows)
    return [
        row
        for row in rows
        if wanted in str(row.get("url", "")).lower() or wanted in str(row.get("label", "")).lower()
    ]


def rows_to_csv(rows: Iterable[dict[str, Any]]) -> bytes:
    """The given rows as CSV bytes, header included (RFC-4180 quoting)."""
    lines = [",".join(CSV_COLUMNS)]
    for row in rows:
        diff = row.get("diff")
        drift = row.get("drift")
        cells = [
            str(row.get("timestamp", "")),
            str(row.get("url", "")),
            str(row.get("label", "")),
            str(row.get("status", "")),
            "" if diff is None else f"{float(diff):.4f}",
            "" if drift is None else f"{float(drift):.4f}",
            str(row.get("file", "")),
        ]
        lines.append(",".join('"' + cell.replace('"', '""') + '"' for cell in cells))
    return ("\n".join(lines) + "\n").encode("utf-8")


def trend_summary(output_dir: str | Path) -> str:
    """A compact multi-line per-site trend report (captures, changes, last diff)."""
    rows = flat_rows(output_dir)
    if not rows:
        return "No history yet."
    counts = site_change_counts(output_dir)
    captures: dict[str, int] = {}
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:  # newest-first
        url = row["url"]
        captures[url] = captures.get(url, 0) + 1
        if url not in latest:
            latest[url] = row
    lines = []
    for url, last in latest.items():
        diff = last["diff"]
        diff_text = f"{diff:.2f}" if diff is not None else "-"
        lines.append(
            f"{url}: {captures[url]} capture(s), {counts.get(url, 0)} change(s), last diff {diff_text}"
        )
    return "\n".join(lines)
