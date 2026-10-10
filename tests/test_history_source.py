"""Tests for the history reader facade (app.core.history_source)."""

from __future__ import annotations

import json

from app.core.history_source import JsonReader, get_reader
from app.core.store import HistoryStore


def _seed_json(directory):
    (directory / "capture-report-20260101-000000.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-01-01T00:00:00",
                "results": [
                    {
                        "url": "https://a.example.com",
                        "status": "success",
                        "diff": 0.2,
                        "file_path": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


class TestGetReader:
    def test_falls_back_to_json_without_index(self, tmp_path):
        _seed_json(tmp_path)
        with get_reader(tmp_path) as reader:
            assert isinstance(reader, JsonReader)
            rows = reader.flat_rows()
        assert len(rows) == 1 and rows[0]["url"] == "https://a.example.com"

    def test_prefers_sqlite_index_when_present(self, tmp_path):
        # JSON says one thing, the index another - the index must win.
        _seed_json(tmp_path)
        with HistoryStore(tmp_path / "history.sqlite3") as store:
            store.add_run(
                "2026-01-02T00:00:00",
                [
                    {
                        "url": "https://from-index.example.com",
                        "status": "success",
                        "diff": 0.4,
                        "file_path": "",
                    }
                ],
            )
        with get_reader(tmp_path) as reader:
            assert isinstance(reader, HistoryStore)
            rows = reader.flat_rows()
        assert rows[0]["url"] == "https://from-index.example.com"

    def test_json_reader_query_surface(self, tmp_path):
        _seed_json(tmp_path)
        reader = JsonReader(tmp_path)
        assert reader.sites() == ["https://a.example.com"]
        assert reader.site_change_counts() == {"https://a.example.com": 1}
        assert len(reader.trend_for_url("https://a.example.com")) == 1
        assert "a.example.com" in reader.trend_summary()

    def test_corrupt_index_falls_back_to_json(self, tmp_path):
        _seed_json(tmp_path)
        (tmp_path / "history.sqlite3").write_text("not a real sqlite file", encoding="utf-8")
        with get_reader(tmp_path) as reader:
            # A corrupt index must not crash; we fall back to the JSON scan.
            assert isinstance(reader, JsonReader)
            rows = reader.flat_rows()
        assert rows[0]["url"] == "https://a.example.com"
