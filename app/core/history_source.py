"""Pick the fastest available history reader for an output folder.

The engine writes both timestamped JSON reports and a ``history.sqlite3`` index.
Reading the indexed table is much faster than re-scanning every JSON file once
history grows, so the UI prefers the SQLite index when it exists and falls back
to the JSON scan otherwise. Both expose the same query surface, so callers can
use either interchangeably via :func:`get_reader`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core import history
from app.core.store import HistoryStore


class JsonReader:
    """Adapter exposing the :class:`HistoryStore` query surface over the JSON files."""

    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = output_dir

    def flat_rows(self) -> list[dict[str, Any]]:
        return history.flat_rows(self.output_dir)

    def sites(self) -> list[str]:
        return history.sites(self.output_dir)

    def trend_for_url(self, url: str) -> list[dict[str, Any]]:
        return history.trend_for_url(self.output_dir, url)

    def site_change_counts(self) -> dict[str, int]:
        return history.site_change_counts(self.output_dir)

    def trend_summary(self) -> str:
        return history.trend_summary(self.output_dir)

    def close(self) -> None:  # noqa: D102 - parity with HistoryStore
        pass

    def __enter__(self) -> JsonReader:
        return self

    def __exit__(self, *exc: object) -> None:
        pass


def get_reader(output_dir: str | Path) -> Any:
    """Return a ``HistoryStore`` if the SQLite index exists, else a ``JsonReader``.

    The result is a context manager; always use ``with get_reader(...) as reader:``
    so the SQLite connection is closed promptly.
    """
    db = Path(output_dir) / "history.sqlite3"
    if db.exists():
        try:
            return HistoryStore(db)
        except Exception:  # noqa: BLE001 - fall back to the JSON scan
            pass
    return JsonReader(output_dir)
