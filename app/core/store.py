"""A lightweight SQLite index of capture history (Qt-free).

The timestamped JSON/CSV reports remain the source of truth; this store is a
durable, queryable index so trend queries stay fast as the number of runs grows
(scanning hundreds of JSON files gets slow, a single indexed table does not).

Every method is best-effort and side-effect free with respect to the rest of the
app: callers wrap construction in ``try/except`` and simply skip the index if it
cannot be opened. The schema mirrors :mod:`app.core.history` so the two can be
used interchangeably.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.core.url_utils import build_url_label

_SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    url       TEXT NOT NULL,
    label     TEXT NOT NULL DEFAULT '',
    status    TEXT NOT NULL DEFAULT '',
    diff      REAL,
    drift     REAL,
    file      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_captures_url_ts ON captures (url, timestamp);
CREATE TABLE IF NOT EXISTS alerts (
    url           TEXT PRIMARY KEY,
    last_alert_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS storage_samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at    TEXT NOT NULL,
    total       INTEGER NOT NULL DEFAULT 0,
    screenshots INTEGER NOT NULL DEFAULT 0,
    history     INTEGER NOT NULL DEFAULT 0,
    index_bytes INTEGER NOT NULL DEFAULT 0,
    reports     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_storage_samples_taken_at ON storage_samples (taken_at);
"""

_COLUMNS = ("timestamp", "url", "label", "status", "diff", "drift", "file")

#: Columns of one :meth:`HistoryStore.storage_series` sample.
SAMPLE_COLUMNS = ("taken_at", "total", "screenshots", "history", "index_bytes", "reports")


class HistoryStore:
    """A SQLite-backed capture index with the same query surface as ``history``."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent and not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        try:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()
        except Exception:
            # A half-built store must not keep the file open: on Windows an open
            # handle blocks the repair (unlink + recreate) that happens next, so
            # "the index is corrupt, rebuild it" would fail instead of healing.
            self._conn.close()
            raise

    def _migrate(self) -> None:
        """Add columns that were introduced after the first release."""
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(captures)")}
        if "drift" not in existing:
            self._conn.execute("ALTER TABLE captures ADD COLUMN drift REAL")

    # -- writing -----------------------------------------------------------
    def add_run(self, generated_at: str, results: list[dict[str, Any]]) -> int:
        """Insert every result of one run; returns the number of rows added."""
        rows = [
            (
                generated_at,
                item.get("url", ""),
                build_url_label(item.get("url", "")),
                item.get("status", ""),
                item.get("diff"),
                item.get("drift"),
                item.get("file_path", ""),
            )
            for item in results
        ]
        self._conn.executemany(
            "INSERT INTO captures (timestamp, url, label, status, diff, drift, file) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()
        return len(rows)

    # -- reading -----------------------------------------------------------
    def flat_rows(self) -> list[dict[str, Any]]:
        """All captures, newest first."""
        cursor = self._conn.execute(
            "SELECT timestamp, url, label, status, diff, drift, file FROM captures "
            "ORDER BY timestamp DESC, id DESC"
        )
        return [dict(zip(_COLUMNS, row, strict=False)) for row in cursor.fetchall()]

    def sites(self) -> list[str]:
        """Distinct URLs in first-seen (newest-first) order."""
        cursor = self._conn.execute("SELECT url FROM captures ORDER BY timestamp DESC, id DESC")
        seen: list[str] = []
        for (url,) in cursor.fetchall():
            if url not in seen:
                seen.append(url)
        return seen

    def trend_for_url(self, url: str) -> list[dict[str, Any]]:
        """Oldest-first ``{timestamp, diff, drift}`` series for one URL."""
        cursor = self._conn.execute(
            "SELECT timestamp, diff, drift FROM captures WHERE url = ? "
            "ORDER BY timestamp ASC, id ASC",
            (url,),
        )
        return [
            {"timestamp": ts, "diff": diff, "drift": drift} for ts, diff, drift in cursor.fetchall()
        ]

    def drift_for_url(self, url: str) -> list[dict[str, Any]]:
        """Oldest-first ``{timestamp, drift}`` points, skipping runs without drift."""
        cursor = self._conn.execute(
            "SELECT timestamp, drift FROM captures WHERE url = ? AND drift IS NOT NULL "
            "ORDER BY timestamp ASC, id ASC",
            (url,),
        )
        return [{"timestamp": ts, "drift": drift} for ts, drift in cursor.fetchall()]

    def site_change_counts(self) -> dict[str, int]:
        """How many times each site registered a visual change (diff > 0)."""
        cursor = self._conn.execute(
            "SELECT url, COUNT(*) FROM captures WHERE diff IS NOT NULL AND diff > 0 GROUP BY url"
        )
        return dict(cursor.fetchall())

    def trend_summary(self) -> str:
        """A compact multi-line per-site trend report (captures/changes/last diff)."""
        rows = self.flat_rows()
        if not rows:
            return "No history yet."
        counts = self.site_change_counts()
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
                f"{url}: {captures[url]} capture(s), {counts.get(url, 0)} "
                f"change(s), last diff {diff_text}"
            )
        return "\n".join(lines)

    # -- storage samples ---------------------------------------------------
    def add_storage_sample(self, taken_at: str, stats: dict[str, Any]) -> int:
        """Record what the capture folder cost at the end of one run.

        The report files describe the *pages*; these samples describe the
        *folder* (bytes, history footprint, report count). They are what turns
        "the history grows by ~1 MB a day" from a median of run sizes into a
        measured trend, so the Storage panel can forecast honestly and
        ``/api/storage?series=1`` can chart it.

        Returns the id of the new row.
        """
        cursor = self._conn.execute(
            "INSERT INTO storage_samples "
            "(taken_at, total, screenshots, history, index_bytes, reports) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                taken_at,
                int(stats.get("total", 0) or 0),
                int(stats.get("screenshots", 0) or 0),
                int(stats.get("history", 0) or 0),
                int(stats.get("index", 0) or 0),
                int(stats.get("reports", 0) or 0),
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid or 0)

    def storage_series(self, days: int = 0, limit: int = 0) -> list[dict[str, Any]]:
        """The recorded storage samples, oldest first.

        ``days > 0`` keeps only the samples taken inside that window and
        ``limit > 0`` only the newest ones; a chart wants a bounded query, not a
        folder scan per request.
        """
        sql = (
            "SELECT taken_at, total, screenshots, history, index_bytes, reports "
            "FROM storage_samples"
        )
        params: list[Any] = []
        if int(days or 0) > 0:
            sql += " WHERE taken_at >= ?"
            params.append(
                (datetime.now() - timedelta(days=int(days))).isoformat(timespec="seconds")
            )
        sql += " ORDER BY taken_at DESC, id DESC"
        if int(limit or 0) > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = [
            dict(zip(SAMPLE_COLUMNS, row, strict=False)) for row in self._conn.execute(sql, params)
        ]
        rows.reverse()  # newest-first out of SQL, oldest-first for a chart
        return rows

    def count_storage_samples(self) -> int:
        """How many storage samples the index holds."""
        return int(self._conn.execute("SELECT COUNT(*) FROM storage_samples").fetchone()[0])

    # -- alert cooldown ----------------------------------------------------
    def last_alert_at(self, url: str) -> str | None:
        """ISO timestamp of the last alert fired for ``url``, or ``None``."""
        cursor = self._conn.execute("SELECT last_alert_at FROM alerts WHERE url = ?", (url,))
        row = cursor.fetchone()
        return row[0] if row else None

    def record_alert(self, url: str, timestamp: str) -> None:
        """Record (upsert) that an alert was fired for ``url`` at ``timestamp``."""
        self._conn.execute(
            "INSERT INTO alerts (url, last_alert_at) VALUES (?, ?) "
            "ON CONFLICT(url) DO UPDATE SET last_alert_at = excluded.last_alert_at",
            (url, timestamp),
        )
        self._conn.commit()

    # -- maintenance -------------------------------------------------------
    def replace_all(self, rows: list[dict[str, Any]]) -> int:
        """Drop every capture row and insert ``rows`` instead (an index rebuild).

        Used by :func:`rebuild_index` when the SQLite file was lost, truncated or
        edited outside the app: the JSON reports stay the source of truth, so the
        index can always be recreated from them.
        """
        payload = [
            (
                row.get("timestamp", ""),
                row.get("url", ""),
                row.get("label") or build_url_label(row.get("url", "")),
                row.get("status", ""),
                row.get("diff"),
                row.get("drift"),
                row.get("file", ""),
            )
            for row in rows
        ]
        self._conn.execute("DELETE FROM captures")
        try:  # keep row ids small again; not fatal when the table never used AUTOINCREMENT
            self._conn.execute("DELETE FROM sqlite_sequence WHERE name = 'captures'")
        except sqlite3.DatabaseError:
            pass
        self._conn.executemany(
            "INSERT INTO captures (timestamp, url, label, status, diff, drift, file) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            payload,
        )
        self._conn.commit()
        return len(payload)

    def prune(self, older_than_days: int | float) -> int:
        """Delete captures older than the cutoff; returns the rows removed.

        ``older_than_days <= 0`` is a no-op, so a setting can be passed straight
        through without a guard at the call site. Cooldown records for sites that
        fell out of the window go with them - a stale entry can only suppress an
        alert that should be sent. Each row carries its own ``drift`` value, so
        pruning the row prunes that column too.
        """
        days = float(older_than_days or 0)
        if days <= 0:
            return 0
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        cursor = self._conn.execute("DELETE FROM captures WHERE timestamp < ?", (cutoff,))
        kept = max(cursor.rowcount, 0)
        self._conn.execute("DELETE FROM alerts WHERE last_alert_at < ?", (cutoff,))
        # Samples are tiny, but an always-on bot records one per run: they follow
        # the same window as the rows they describe.
        self._conn.execute("DELETE FROM storage_samples WHERE taken_at < ?", (cutoff,))
        self._conn.commit()
        return kept

    def count(self) -> int:
        """How many capture rows the index holds."""
        return int(self._conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0])

    def prune_to_size(self, max_bytes: int) -> int:
        """Delete the oldest captures until the index file fits under ``max_bytes``.

        A size cap is blunter than a retention window: the file has to shrink, so
        the rows are dropped oldest-first in 10% batches and the space is
        reclaimed with ``VACUUM`` after each batch. The newest capture always
        survives, so a cap smaller than the minimum database size cannot leave an
        unreadable (and uselessly empty) index behind.

        Returns the number of rows deleted. The reports stay the source of truth,
        so anything dropped here can be restored with a rebuild.
        """
        limit = max(0, int(max_bytes or 0))
        removed = 0
        # Each pass drops at least one row, so this terminates; the counter is
        # only a belt-and-braces guard against a database that refuses to shrink.
        for _attempt in range(64):
            if self.size_bytes() <= limit:
                break
            total = self.count()
            if total <= 1:  # the newest capture always survives
                break
            batch = min(max(1, total // 10), total - 1)
            cursor = self._conn.execute(
                "DELETE FROM captures WHERE id IN ("
                "SELECT id FROM captures ORDER BY timestamp ASC, id ASC LIMIT ?)",
                (batch,),
            )
            self._conn.commit()
            if cursor.rowcount <= 0:  # pragma: no cover - the table emptied under us
                break
            removed += int(cursor.rowcount)
            self.vacuum()
        return removed

    def size_bytes(self) -> int:
        """On-disk size of the database file (0 when it does not exist yet)."""
        try:
            return self.path.stat().st_size
        except OSError:  # pragma: no cover - the file is created on connect
            return 0

    def vacuum(self) -> int:
        """Reclaim the disk space freed by :meth:`prune`.

        Returns the number of bytes reclaimed (0 when the file was already
        compact), which is what the CLI reports back to the user.
        """
        before = self.size_bytes()
        self._conn.execute("VACUUM")
        return max(before - self.size_bytes(), 0)

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> HistoryStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _open_for_rebuild(target: Path) -> HistoryStore:
    """Open ``target``, deleting it first when SQLite cannot read it at all.

    The index is derived data, so a file that is not a database (truncated by a
    crash, half-copied, edited by hand) is worth nothing: starting over is the
    repair.
    """
    try:
        return HistoryStore(target)
    except sqlite3.DatabaseError:
        target.unlink(missing_ok=True)
        return HistoryStore(target)


def rebuild_index(output_dir: str | Path, db_path: str | Path | None = None) -> int:
    """Recreate the SQLite index of ``output_dir`` from its JSON reports.

    Storage samples describe the folder as it was, which cannot be derived from
    the reports afterwards, so a rebuild keeps them: the trend stays honest even
    when the index had to be repaired.

    Returns the number of rows written. The index is derived data, so throwing it
    away and rebuilding is always safe - this is the repair path for a corrupt,
    deleted or out-of-date ``history.sqlite3``.
    """
    from app.core import history

    target = Path(db_path) if db_path else Path(output_dir) / "history.sqlite3"
    rows = history.flat_rows(output_dir)
    with _open_for_rebuild(target) as store:
        count = store.replace_all(rows)
        store.vacuum()
    return count
