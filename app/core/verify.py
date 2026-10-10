"""Check that the index, the reports and the folder still agree with each other.

Every file in a capture folder has a role: ``capture-report-*.json`` is the
source of truth, ``history.sqlite3`` is a fast copy of it, and ``latest_*`` /
``baseline_*`` are the references a comparison needs. When the copies drift
apart - a cap that pruned reports but not rows, an index deleted by hand, a
report that got truncated in a crash - the *symptoms* are subtle: the dashboard
shows fewer sites than the CSV export, ``--trend`` disagrees with the timeline,
a baseline never updates.

This module is the boring referee: it reads everything, writes nothing, and
returns one small object listing what does not add up. ``history --verify``
prints it and exits non-zero, so CI can notice before a human does.

It parses the reports itself instead of reusing :mod:`app.core.history`: a
verifier that crashes on the very file it is supposed to report would be a
cruel joke, so every malformed shape becomes an issue and the sweep goes on.

"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core import retention
from app.core.store import HistoryStore
from app.core.url_utils import build_url_label

#: Issue kinds the verifier can report (stable strings, safe to match on).
UNREADABLE_REPORT = "unreadable_report"
EMPTY_REPORT = "empty_report"
DUPLICATE_REPORT = "duplicate_report"
MISSING_CSV = "missing_csv"
MISSING_INDEX = "missing_index"
BROKEN_INDEX = "broken_index"
UNINDEXED_ROWS = "unindexed_rows"
GHOST_ROWS = "ghost_rows"
DUPLICATE_ROWS = "duplicate_rows"
ORPHAN_REFERENCE = "orphan_reference"


@dataclass(frozen=True)
class Issue:
    """One thing that does not add up, with enough detail to act on it."""

    kind: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "detail": self.detail}


@dataclass(frozen=True)
class VerifyReport:
    """The verdict of one sweep over a capture folder."""

    reports: int = 0
    report_rows: int = 0
    index_rows: int = 0
    index_exists: bool = False
    issues: tuple[Issue, ...] = field(default_factory=tuple)

    @property
    def clean(self) -> bool:
        """True when nothing was wrong (an empty folder counts as clean)."""
        return not self.issues

    def kinds(self) -> dict[str, int]:
        """``{kind: count}`` so a caller can summarise without walking details."""
        counted: dict[str, int] = {}
        for issue in self.issues:
            counted[issue.kind] = counted.get(issue.kind, 0) + 1
        return counted

    def summary(self) -> str:
        """One line for a log file or a console."""
        head = f"{self.report_rows} row(s) in {self.reports} report file(s); " + (
            f"the index holds {self.index_rows} row(s)" if self.index_exists else "no index"
        )
        if self.clean:
            return f"{head} - clean."
        return f"{head} - {len(self.issues)} problem(s): {self._kind_list()}."

    def _kind_list(self) -> str:
        return ", ".join(f"{kind} x{count}" for kind, count in sorted(self.kinds().items()))

    def to_dict(self) -> dict[str, Any]:
        """The whole verdict as plain data (``history --verify --json``)."""
        return {
            "reports": self.reports,
            "report_rows": self.report_rows,
            "index_rows": self.index_rows,
            "index_exists": self.index_exists,
            "clean": self.clean,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _read_reports(folder: Path) -> tuple[int, int, set[tuple[str, str]], set[str], list[Issue]]:
    """``(reports, rows, pairs, urls, issues)`` for the JSON reports.

    ``pairs`` are the ``(timestamp, url)`` captures the reports claim to hold -
    the same identity :mod:`app.core.history` uses, built here so a broken report
    degrades into an issue instead of an exception.
    """
    files = retention.report_files(folder)
    jsons = [path for path in files if path.suffix.lower() == ".json"]
    csv_stems = {path.stem for path in files if path.suffix.lower() == ".csv"}
    issues: list[Issue] = []
    rows = 0
    pairs: set[tuple[str, str]] = set()
    urls: set[str] = set()
    seen_stamps: set[str] = set()

    for path in jsons:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(Issue(UNREADABLE_REPORT, f"{path.name}: {exc}"))
            continue
        if not isinstance(data, dict):
            issues.append(Issue(UNREADABLE_REPORT, f"{path.name}: not a JSON object"))
            continue

        stamp = path.stem[len(retention.REPORT_PREFIX) :]
        if stamp in seen_stamps:
            issues.append(Issue(DUPLICATE_REPORT, f"{path.name} repeats the stamp {stamp}"))
        seen_stamps.add(stamp)

        results = data.get("results")
        if not isinstance(results, list):
            issues.append(Issue(EMPTY_REPORT, f"{path.name} has no results list"))
            results = []
        broken = [item for item in results if not isinstance(item, dict)]
        if broken:
            issues.append(
                Issue(UNREADABLE_REPORT, f"{path.name}: {len(broken)} result(s) are not objects")
            )

        timestamp = str(data.get("generated_at", ""))
        for item in results:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url", ""))
            pairs.add((timestamp, url))
            urls.add(url)
            rows += 1

        if path.stem not in csv_stems:
            issues.append(Issue(MISSING_CSV, f"{path.name} has no .csv twin"))

    return len(jsons), rows, pairs, urls, issues


def _read_index(folder: Path) -> tuple[bool, list[dict[str, Any]] | None, list[Issue]]:
    """``(exists, rows, issues)``; ``rows`` is None when the index is unusable."""
    db_path = folder / retention.INDEX_FILENAME
    if not db_path.is_file():
        return (
            False,
            None,
            [
                Issue(
                    MISSING_INDEX,
                    f"{db_path.name} does not exist; run 'history --reindex' to rebuild it",
                )
            ],
        )
    try:
        with HistoryStore(db_path) as store:
            return True, store.flat_rows(), []
    except sqlite3.DatabaseError as exc:
        return (
            True,
            None,
            [Issue(BROKEN_INDEX, f"{db_path.name} cannot be read ({exc}); --reindex repairs it")],
        )


def _compare_rows(indexed: list[dict[str, Any]], expected: set[tuple[str, str]]) -> list[Issue]:
    """Row-level differences between the index and the reports."""
    issues: list[Issue] = []
    present = {(row.get("timestamp", ""), row.get("url", "")) for row in indexed}

    unindexed = sorted(expected - present)
    if unindexed:
        stamp, url = unindexed[0]
        issues.append(
            Issue(
                UNINDEXED_ROWS,
                f"{len(unindexed)} row(s) are in the reports but not in the index, "
                f"e.g. {url} at {stamp} (--reindex adds them)",
            )
        )

    ghosts = sorted(present - expected)
    if ghosts:
        stamp, url = ghosts[0]
        issues.append(
            Issue(
                GHOST_ROWS,
                f"{len(ghosts)} index row(s) have no report left, "
                f"e.g. {url} at {stamp} (a pruned report; --reindex drops them)",
            )
        )

    if len(indexed) != len(present):
        issues.append(
            Issue(
                DUPLICATE_ROWS,
                f"the index holds {len(indexed)} row(s) for {len(present)} distinct "
                "capture(s); --reindex rewrites it",
            )
        )
    return issues


def _orphan_references(folder: Path, known_labels: set[str]) -> list[Issue]:
    """``latest_*``/``baseline_*`` files whose site is not in the history."""
    issues: list[Issue] = []
    for prefix in retention.PROTECTED_PREFIXES:
        for path in sorted(folder.glob(f"{prefix}*")):
            if not path.is_file():
                continue
            label = path.stem[len(prefix) :]
            if label and label not in known_labels:
                issues.append(
                    Issue(
                        ORPHAN_REFERENCE,
                        f"{path.name} has no capture in the history "
                        "(the site was renamed, muted or pruned)",
                    )
                )
    return issues


def verify_folder(output_dir: str | Path) -> VerifyReport:
    """Check one capture folder; never raises, never writes.

    A folder without reports *and* without an index has nothing to verify and
    comes back clean - "empty" is not a problem the doctor can fix.
    """
    folder = Path(output_dir)
    if not folder.is_dir():
        return VerifyReport()

    reports, report_rows, expected, urls, issues = _read_reports(folder)

    index_exists, indexed, index_issues = _read_index(folder)
    issues.extend(index_issues)
    if not reports and not index_exists:
        return VerifyReport(index_exists=False)
    if indexed is not None:
        issues.extend(_compare_rows(indexed, expected))

    known_labels = {build_url_label(url) for url in urls}
    issues.extend(_orphan_references(folder, known_labels))

    return VerifyReport(
        reports=reports,
        report_rows=report_rows,
        index_rows=0 if indexed is None else len(indexed),
        index_exists=index_exists,
        issues=tuple(issues),
    )
