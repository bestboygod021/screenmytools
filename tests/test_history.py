"""Unit tests for the report-derived change history."""

from __future__ import annotations

import json

from app.core import history


def _write_report(directory, stamp, generated_at, results):
    path = directory / f"capture-report-{stamp}.json"
    path.write_text(
        json.dumps({"generated_at": generated_at, "results": results}), encoding="utf-8"
    )
    return path


class TestHistory:
    def test_load_runs_sorted_oldest_first(self, tmp_path):
        _write_report(
            tmp_path,
            "20260102-000000",
            "2026-01-02T00:00:00",
            [{"url": "https://a", "status": "success", "diff": 0.2}],
        )
        _write_report(
            tmp_path,
            "20260101-000000",
            "2026-01-01T00:00:00",
            [{"url": "https://a", "status": "success", "diff": 0.0}],
        )
        runs = history.load_runs(tmp_path)
        assert [r["generated_at"] for r in runs] == ["2026-01-01T00:00:00", "2026-01-02T00:00:00"]

    def test_flat_rows_newest_first(self, tmp_path):
        _write_report(
            tmp_path,
            "20260101-000000",
            "2026-01-01T00:00:00",
            [{"url": "https://a", "status": "success", "diff": 0.0}],
        )
        _write_report(
            tmp_path,
            "20260102-000000",
            "2026-01-02T00:00:00",
            [{"url": "https://a", "status": "success", "diff": 0.5}],
        )
        rows = history.flat_rows(tmp_path)
        assert rows[0]["timestamp"] == "2026-01-02T00:00:00"
        assert rows[0]["diff"] == 0.5
        assert rows[0]["label"] == "a"

    def test_sites_are_distinct(self, tmp_path):
        _write_report(
            tmp_path,
            "20260101-000000",
            "2026-01-01T00:00:00",
            [
                {"url": "https://a", "status": "success", "diff": 0.0},
                {"url": "https://b", "status": "failed", "diff": None},
            ],
        )
        _write_report(
            tmp_path,
            "20260102-000000",
            "2026-01-02T00:00:00",
            [{"url": "https://a", "status": "success", "diff": 0.1}],
        )
        assert set(history.sites(tmp_path)) == {"https://a", "https://b"}

    def test_timeline_for_url_filters(self, tmp_path):
        _write_report(
            tmp_path,
            "20260101-000000",
            "2026-01-01T00:00:00",
            [
                {"url": "https://a", "status": "success", "diff": 0.0},
                {"url": "https://b", "status": "failed", "diff": None},
            ],
        )
        timeline = history.timeline_for_url(tmp_path, "https://a")
        assert len(timeline) == 1
        assert timeline[0]["url"] == "https://a"

    def test_corrupt_file_is_skipped(self, tmp_path):
        _write_report(
            tmp_path,
            "20260101-000000",
            "2026-01-01T00:00:00",
            [{"url": "https://a", "status": "success", "diff": 0.0}],
        )
        (tmp_path / "capture-report-20260102-000000.json").write_text("{not json", encoding="utf-8")
        assert len(history.load_runs(tmp_path)) == 1

    def test_missing_dir_returns_empty(self, tmp_path):
        assert history.load_runs(tmp_path / "nope") == []
        assert history.flat_rows(tmp_path / "nope") == []


class TestTrend:
    def _seed(self, directory):
        import json

        (directory / "capture-report-20260101-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "results": [
                        {
                            "url": "https://a.example.com",
                            "status": "success",
                            "diff": 0.0,
                            "file_path": "",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (directory / "capture-report-20260102-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-02T00:00:00",
                    "results": [
                        {
                            "url": "https://a.example.com",
                            "status": "success",
                            "diff": 0.4,
                            "file_path": "",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_trend_for_url_oldest_first(self, tmp_path):
        from app.core import history

        self._seed(tmp_path)
        trend = history.trend_for_url(tmp_path, "https://a.example.com")
        assert [t["diff"] for t in trend] == [0.0, 0.4]

    def test_site_change_counts(self, tmp_path):
        from app.core import history

        self._seed(tmp_path)
        assert history.site_change_counts(tmp_path) == {"https://a.example.com": 1}


class TestTrendSummary:
    def _seed(self, directory):
        import json

        (directory / "capture-report-20260101-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "results": [
                        {
                            "url": "https://a.example.com",
                            "status": "success",
                            "diff": 0.0,
                            "file_path": "",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (directory / "capture-report-20260102-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-02T00:00:00",
                    "results": [
                        {
                            "url": "https://a.example.com",
                            "status": "success",
                            "diff": 0.4,
                            "file_path": "",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_summary_lists_site_stats(self, tmp_path):
        from app.core import history

        self._seed(tmp_path)
        text = history.trend_summary(tmp_path)
        assert "a.example.com" in text
        assert "2 capture(s)" in text
        assert "1 change(s)" in text

    def test_summary_empty(self, tmp_path):
        from app.core import history

        assert history.trend_summary(tmp_path / "none") == "No history yet."


class TestDriftSeries:
    def _report(self, directory, name, when, url, diff, drift=None):
        import json

        (directory / f"capture-report-{name}.json").write_text(
            json.dumps(
                {
                    "generated_at": when,
                    "results": [
                        {
                            "url": url,
                            "status": "success",
                            "diff": diff,
                            "drift": drift,
                            "file_path": "",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_flat_rows_carry_drift(self, tmp_path):
        from app.core import history

        self._report(tmp_path, "20260101", "2026-01-01T00:00:00", "https://a.com", 0.1, 0.4)
        assert history.flat_rows(tmp_path)[0]["drift"] == 0.4

    def test_drift_for_url_is_oldest_first_and_skips_missing(self, tmp_path):
        from app.core import history

        self._report(tmp_path, "20260101", "2026-01-01T00:00:00", "https://a.com", 0.1, None)
        self._report(tmp_path, "20260102", "2026-01-02T00:00:00", "https://a.com", 0.1, 0.4)
        self._report(tmp_path, "20260103", "2026-01-03T00:00:00", "https://a.com", 0.1, 0.6)
        points = history.drift_for_url(tmp_path, "https://a.com")
        assert [point["drift"] for point in points] == [0.4, 0.6]
        assert points[0]["timestamp"] < points[1]["timestamp"]
