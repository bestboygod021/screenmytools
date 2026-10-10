"""Unit tests for the SQLite history index (app.core.store)."""

from __future__ import annotations

import json

import pytest

from app import cli
from app.core.store import HistoryStore


def _result(url: str, status: str = "success", diff=None, file: str = "") -> dict:
    return {"url": url, "status": status, "diff": diff, "file_path": file}


class TestHistoryStore:
    def test_add_run_and_flat_rows_newest_first(self, tmp_path):
        db = tmp_path / "history.sqlite3"
        with HistoryStore(db) as store:
            store.add_run("2026-01-01T00:00:00", [_result("https://a.com", diff=0.0)])
            store.add_run("2026-01-02T00:00:00", [_result("https://a.com", diff=0.5)])
            rows = store.flat_rows()
        assert [r["timestamp"] for r in rows] == [
            "2026-01-02T00:00:00",
            "2026-01-01T00:00:00",
        ]
        assert rows[0]["diff"] == 0.5

    def test_trend_for_url_is_oldest_first(self, tmp_path):
        with HistoryStore(tmp_path / "h.sqlite3") as store:
            store.add_run("2026-01-01T00:00:00", [_result("https://a.com", diff=0.1)])
            store.add_run("2026-01-02T00:00:00", [_result("https://a.com", diff=0.9)])
            store.add_run("2026-01-02T00:00:00", [_result("https://other.com", diff=0.4)])
            trend = store.trend_for_url("https://a.com")
        assert [t["diff"] for t in trend] == [0.1, 0.9]

    def test_site_change_counts_only_positive_diffs(self, tmp_path):
        with HistoryStore(tmp_path / "h.sqlite3") as store:
            store.add_run("2026-01-01T00:00:00", [_result("https://a.com", diff=0.0)])
            store.add_run("2026-01-02T00:00:00", [_result("https://a.com", diff=0.3)])
            store.add_run("2026-01-03T00:00:00", [_result("https://a.com", diff=None)])
            counts = store.site_change_counts()
        assert counts == {"https://a.com": 1}

    def test_sites_distinct_newest_first(self, tmp_path):
        with HistoryStore(tmp_path / "h.sqlite3") as store:
            store.add_run("2026-01-01T00:00:00", [_result("https://a.com")])
            store.add_run("2026-01-02T00:00:00", [_result("https://b.com")])
            store.add_run("2026-01-03T00:00:00", [_result("https://a.com")])
            sites = store.sites()
        assert sites == ["https://a.com", "https://b.com"]

    def test_trend_summary(self, tmp_path):
        with HistoryStore(tmp_path / "h.sqlite3") as store:
            store.add_run("2026-01-01T00:00:00", [_result("https://a.com", diff=0.0)])
            store.add_run("2026-01-02T00:00:00", [_result("https://a.com", diff=0.4)])
            text = store.trend_summary()
        assert "a.com" in text
        assert "2 capture(s)" in text
        assert "1 change(s)" in text

    def test_trend_summary_empty(self, tmp_path):
        with HistoryStore(tmp_path / "h.sqlite3") as store:
            assert store.trend_summary() == "No history yet."

    def test_data_persists_across_reopen(self, tmp_path):
        db = tmp_path / "h.sqlite3"
        with HistoryStore(db) as store:
            store.add_run("2026-01-01T00:00:00", [_result("https://a.com", diff=0.2)])
        with HistoryStore(db) as store:  # reopened
            assert len(store.flat_rows()) == 1

    def test_add_run_returns_row_count(self, tmp_path):
        with HistoryStore(tmp_path / "h.sqlite3") as store:
            added = store.add_run(
                "2026-01-01T00:00:00",
                [_result("https://a.com"), _result("https://b.com")],
            )
        assert added == 2


class TestAlertCooldown:
    def test_record_and_read(self, tmp_path):
        with HistoryStore(tmp_path / "h.sqlite3") as store:
            assert store.last_alert_at("https://a.com") is None
            store.record_alert("https://a.com", "2026-01-01T00:00:00")
            assert store.last_alert_at("https://a.com") == "2026-01-01T00:00:00"

    def test_upsert_updates_timestamp(self, tmp_path):
        with HistoryStore(tmp_path / "h.sqlite3") as store:
            store.record_alert("https://a.com", "2026-01-01T00:00:00")
            store.record_alert("https://a.com", "2026-01-02T00:00:00")
            assert store.last_alert_at("https://a.com") == "2026-01-02T00:00:00"


class TestPrune:
    def _seed(self, path, *stamps):
        with HistoryStore(path) as store:
            for stamp in stamps:
                store.add_run(
                    stamp,
                    [{"url": "https://a.com", "status": "success", "diff": 0.1, "file_path": ""}],
                )

    def test_prune_drops_old_rows(self, tmp_path):
        from datetime import datetime, timedelta

        db = tmp_path / "h.sqlite3"
        old = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
        fresh = datetime.now().isoformat(timespec="seconds")
        self._seed(db, old, fresh)
        with HistoryStore(db) as store:
            assert store.prune(7) == 1
            assert len(store.flat_rows()) == 1

    def test_prune_zero_is_a_noop(self, tmp_path):
        db = tmp_path / "h.sqlite3"
        self._seed(db, "2020-01-01T00:00:00")
        with HistoryStore(db) as store:
            assert store.prune(0) == 0
            assert store.prune(-5) == 0
            assert len(store.flat_rows()) == 1

    def test_vacuum_after_prune_keeps_the_db_usable(self, tmp_path):
        db = tmp_path / "h.sqlite3"
        self._seed(db, "2020-01-01T00:00:00")
        with HistoryStore(db) as store:
            store.prune(30)
            store.vacuum()
            assert store.flat_rows() == []


class TestDriftColumn:
    def test_drift_round_trips(self, tmp_path):
        with HistoryStore(tmp_path / "h.sqlite3") as store:
            store.add_run(
                "2026-01-01T00:00:00",
                [
                    {
                        "url": "https://a.com",
                        "status": "success",
                        "diff": 0.2,
                        "drift": 0.35,
                        "file_path": "x.png",
                    }
                ],
            )
            row = store.flat_rows()[0]
        assert row["drift"] == pytest.approx(0.35)

    def test_trend_points_carry_drift(self, tmp_path):
        with HistoryStore(tmp_path / "h.sqlite3") as store:
            store.add_run(
                "2026-01-01T00:00:00",
                [{"url": "https://a.com", "status": "success", "diff": 0.1, "drift": 0.4}],
            )
            store.add_run(
                "2026-01-02T00:00:00",
                [{"url": "https://a.com", "status": "success", "diff": 0.1, "drift": 0.5}],
            )
            trend = store.trend_for_url("https://a.com")
        assert [point["drift"] for point in trend] == [0.4, 0.5]

    def test_drift_for_url_skips_runs_without_a_baseline(self, tmp_path):
        with HistoryStore(tmp_path / "h.sqlite3") as store:
            store.add_run(
                "2026-01-01T00:00:00",
                [{"url": "https://a.com", "status": "success", "diff": 0.1, "drift": None}],
            )
            store.add_run(
                "2026-01-02T00:00:00",
                [{"url": "https://a.com", "status": "success", "diff": 0.1, "drift": 0.7}],
            )
            points = store.drift_for_url("https://a.com")
        assert [point["drift"] for point in points] == [0.7]

    def test_old_database_is_migrated_in_place(self, tmp_path):
        import sqlite3

        db = tmp_path / "old.sqlite3"
        legacy = sqlite3.connect(str(db))
        legacy.execute(
            "CREATE TABLE captures (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,"
            " url TEXT NOT NULL, label TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '',"
            " diff REAL, file TEXT NOT NULL DEFAULT '')"
        )
        legacy.execute(
            "INSERT INTO captures (timestamp, url, label, status, diff, file)"
            " VALUES ('2025-01-01T00:00:00', 'https://old.com', 'old', 'success', 0.1, '')"
        )
        legacy.commit()
        legacy.close()

        with HistoryStore(db) as store:  # opens the legacy file and adds the column
            assert store.flat_rows()[0]["url"] == "https://old.com"
            assert store.flat_rows()[0]["drift"] is None
            store.add_run(
                "2026-01-02T00:00:00",
                [{"url": "https://new.com", "status": "success", "diff": 0.0, "drift": 0.2}],
            )
            assert store.flat_rows()[0]["drift"] == pytest.approx(0.2)

        # Re-opening must not try to add the column twice.
        with HistoryStore(db) as store:
            assert len(store.flat_rows()) == 2


class TestVacuum:
    def _fill(self, path, rows=400):
        with HistoryStore(path) as store:
            for index in range(rows):
                store.add_run(
                    f"2026-01-{(index % 28) + 1:02d}T00:00:00",
                    [
                        {
                            "url": f"https://site{index}.example.com",
                            "status": "success",
                            "diff": 0.1,
                            "drift": 0.2,
                            "file_path": f"/tmp/shot-{index}.png",
                        }
                    ],
                )

    def test_vacuum_reclaims_space_after_a_prune(self, tmp_path):
        db = tmp_path / "h.sqlite3"
        self._fill(db)
        with HistoryStore(db) as store:
            before = store.size_bytes()
            store.prune(1)  # every row is dated in the past
            reclaimed = store.vacuum()
            after = store.size_bytes()
            assert store.flat_rows() == []
        assert before > after
        assert reclaimed == pytest.approx(before - after)

    def test_vacuum_on_a_compact_database_is_harmless(self, tmp_path):
        db = tmp_path / "h.sqlite3"
        with HistoryStore(db) as store:
            store.add_run("2026-01-01T00:00:00", [{"url": "https://a.com", "status": "success"}])
            assert store.vacuum() >= 0
            assert len(store.flat_rows()) == 1

    def test_prune_also_drops_expired_cooldown_entries(self, tmp_path):
        db = tmp_path / "h.sqlite3"
        with HistoryStore(db) as store:
            store.add_run("2026-01-01T00:00:00", [{"url": "https://a.com", "status": "success"}])
            store.record_alert("https://a.com", "2020-01-01T00:00:00")  # long expired
            store.record_alert("https://b.com", "2999-01-01T00:00:00")  # far future
            store.prune(7)
            assert store.last_alert_at("https://a.com") is None
            assert store.last_alert_at("https://b.com") == "2999-01-01T00:00:00"


class TestReplaceAll:
    def test_rows_are_replaced_not_appended(self, tmp_path):
        db = tmp_path / "h.sqlite3"
        with HistoryStore(db) as store:
            store.add_run("2026-01-01T00:00:00", [{"url": "https://old.com", "status": "success"}])
            added = store.replace_all(
                [
                    {
                        "timestamp": "2026-02-01T00:00:00",
                        "url": "https://new.com",
                        "status": "success",
                    }
                ]
            )
            assert added == 1
            assert [row["url"] for row in store.flat_rows()] == ["https://new.com"]

    def test_the_label_is_filled_in_when_missing(self, tmp_path):
        db = tmp_path / "h.sqlite3"
        with HistoryStore(db) as store:
            store.replace_all([{"timestamp": "2026-02-01T00:00:00", "url": "https://a.com/x"}])
            assert store.flat_rows()[0]["label"] == "a_com_x"

    def test_diff_and_drift_survive_the_rebuild(self, tmp_path):
        db = tmp_path / "h.sqlite3"
        with HistoryStore(db) as store:
            store.replace_all(
                [
                    {
                        "timestamp": "2026-02-01T00:00:00",
                        "url": "https://a.com",
                        "status": "success",
                        "diff": 0.25,
                        "drift": 0.5,
                    }
                ]
            )
            row = store.flat_rows()[0]
            assert row["diff"] == 0.25 and row["drift"] == 0.5

    def test_an_empty_list_clears_the_index(self, tmp_path):
        db = tmp_path / "h.sqlite3"
        with HistoryStore(db) as store:
            store.add_run("2026-01-01T00:00:00", [{"url": "https://a.com", "status": "success"}])
            assert store.replace_all([]) == 0
            assert store.flat_rows() == []


class TestRebuildIndex:
    def _write_report(self, directory, name, stamp, url):
        (directory / f"capture-report-{name}.json").write_text(
            json.dumps(
                {
                    "generated_at": stamp,
                    "results": [
                        {"url": url, "status": "success", "diff": 0.3, "file_path": "x.png"}
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_the_index_is_built_from_the_reports(self, tmp_path):
        from app.core.store import rebuild_index

        self._write_report(tmp_path, "a", "2026-01-01T00:00:00", "https://a.com")
        self._write_report(tmp_path, "b", "2026-01-02T00:00:00", "https://b.com")
        assert rebuild_index(tmp_path) == 2
        assert (tmp_path / "history.sqlite3").exists()
        with HistoryStore(tmp_path / "history.sqlite3") as store:
            assert [row["url"] for row in store.flat_rows()] == ["https://b.com", "https://a.com"]

    def test_a_stale_index_is_corrected(self, tmp_path):
        from app.core.store import rebuild_index

        self._write_report(tmp_path, "a", "2026-01-01T00:00:00", "https://a.com")
        with HistoryStore(tmp_path / "history.sqlite3") as store:
            store.add_run("1999-01-01T00:00:00", [{"url": "https://ghost.com", "status": "failed"}])
        rebuild_index(tmp_path)
        with HistoryStore(tmp_path / "history.sqlite3") as store:
            assert [row["url"] for row in store.flat_rows()] == ["https://a.com"]

    def test_no_reports_gives_an_empty_index(self, tmp_path):
        from app.core.store import rebuild_index

        assert rebuild_index(tmp_path) == 0

    def test_a_custom_database_path_is_honoured(self, tmp_path):
        from app.core.store import rebuild_index

        self._write_report(tmp_path, "a", "2026-01-01T00:00:00", "https://a.com")
        target = tmp_path / "nested" / "other.sqlite3"
        assert rebuild_index(tmp_path, target) == 1
        assert target.exists()


class TestHistoryReindexCli:
    def _seed_reports(self, tmp_path):
        for index, url in enumerate(("https://a.example.com", "https://b.example.com")):
            (tmp_path / f"capture-report-2026010{index + 1}-000000.json").write_text(
                json.dumps(
                    {
                        "generated_at": f"2026-01-0{index + 1}T00:00:00",
                        "results": [
                            {"url": url, "status": "success", "diff": 0.2, "file_path": "x.png"}
                        ],
                    }
                ),
                encoding="utf-8",
            )

    def test_the_index_is_rebuilt_from_the_reports(self, tmp_path, capsys):
        from app.core.store import HistoryStore

        self._seed_reports(tmp_path)
        code = cli.main(["history", "--dir", str(tmp_path), "--reindex"])
        out = capsys.readouterr().out
        assert code == 0
        assert "Index rebuilt: 2 row(s) from 2 report file(s)." in out
        with HistoryStore(tmp_path / "history.sqlite3") as store:
            assert len(store.flat_rows()) == 2

    def test_a_corrupt_index_is_replaced(self, tmp_path, capsys):
        from app.core.store import HistoryStore

        self._seed_reports(tmp_path)
        (tmp_path / "history.sqlite3").write_bytes(b"not a database")
        code = cli.main(["history", "--dir", str(tmp_path), "--reindex"])
        assert code == 0
        with HistoryStore(tmp_path / "history.sqlite3") as store:
            assert len(store.flat_rows()) == 2
        assert "Index rebuilt" in capsys.readouterr().out

    def test_the_parser_takes_the_flag(self):
        args = cli.build_history_parser().parse_args(["--dir", "x", "--reindex"])
        assert args.reindex is True

    def test_no_reports_means_an_empty_index(self, tmp_path, capsys):
        code = cli.main(["history", "--dir", str(tmp_path), "--reindex"])
        assert code == 0
        assert "Index rebuilt: 0 row(s) from 0 report file(s)." in capsys.readouterr().out


class TestPruneToSize:
    """A size cap is blunter than a retention window: the file has to shrink."""

    def _fill(self, path, rows=800):
        with HistoryStore(path) as store:
            for index in range(rows):
                store.add_run(
                    f"2026-01-{(index % 28) + 1:02d}T00:00:00",
                    [
                        {
                            "url": f"https://site{index}.example.com",
                            "status": "success",
                            "diff": 0.1,
                            "drift": 0.2,
                            "file_path": f"/tmp/shots/capture-{index}.png",
                        }
                    ],
                )
            return store.count(), store.size_bytes()

    def test_count_reports_the_rows(self, tmp_path):
        db = tmp_path / "h.sqlite3"
        self._fill(db, rows=3)
        with HistoryStore(db) as store:
            assert store.count() == 3

    def test_under_the_limit_nothing_is_deleted(self, tmp_path):
        db = tmp_path / "h.sqlite3"
        self._fill(db, rows=3)
        with HistoryStore(db) as store:
            assert store.prune_to_size(store.size_bytes() + 1) == 0
            assert store.count() == 3

    def test_the_oldest_rows_go_first(self, tmp_path):
        db = tmp_path / "h.sqlite3"
        total, before = self._fill(db)
        with HistoryStore(db) as store:
            target = before // 3
            removed = store.prune_to_size(target)
            assert 0 < removed < total
            assert store.count() == total - removed
            assert store.size_bytes() <= target
            assert store.flat_rows()[0]["url"] == "https://site783.example.com"
            assert store.flat_rows()[-1]["timestamp"] > "2026-01-01T00:00:00"

    def test_the_newest_capture_always_survives(self, tmp_path):
        db = tmp_path / "h.sqlite3"
        total, _before = self._fill(db)
        with HistoryStore(db) as store:
            assert store.prune_to_size(1) == total - 1
            assert store.count() == 1
            assert store.flat_rows()[0]["url"] == "https://site783.example.com"

    def test_a_negative_limit_is_treated_as_zero(self, tmp_path):
        db = tmp_path / "h.sqlite3"
        total, _before = self._fill(db, rows=5)
        with HistoryStore(db) as store:
            assert store.prune_to_size(-100) == total - 1
            assert store.count() == 1

    def test_an_empty_index_is_a_no_op(self, tmp_path):
        with HistoryStore(tmp_path / "h.sqlite3") as store:
            assert store.prune_to_size(1024) == 0
            assert store.count() == 0

    def test_cooldown_records_are_left_alone(self, tmp_path):
        db = tmp_path / "h.sqlite3"
        self._fill(db, rows=50)
        with HistoryStore(db) as store:
            store.record_alert("https://a.com", "2026-01-01T00:00:00")
            store.prune_to_size(1)
            assert store.last_alert_at("https://a.com") == "2026-01-01T00:00:00"


class TestStorageSamples:
    """The folder-size samples that make the growth trend measurable."""

    @staticmethod
    def _stats(total=1000, screenshots=700, history=300, index=50, reports=2):
        return {
            "total": total,
            "screenshots": screenshots,
            "history": history,
            "index": index,
            "reports": reports,
        }

    def test_a_sample_keeps_every_number(self, tmp_path):
        with HistoryStore(tmp_path / "history.sqlite3") as store:
            assert store.add_storage_sample("2026-05-01T09:00:00", self._stats()) > 0
            row = store.storage_series()[0]
        assert row == {
            "taken_at": "2026-05-01T09:00:00",
            "total": 1000,
            "screenshots": 700,
            "history": 300,
            "index_bytes": 50,
            "reports": 2,
        }

    def test_missing_numbers_count_as_zero(self, tmp_path):
        with HistoryStore(tmp_path / "history.sqlite3") as store:
            store.add_storage_sample("2026-05-01T09:00:00", {})
            assert store.storage_series()[0]["history"] == 0

    def test_the_series_is_oldest_first(self, tmp_path):
        with HistoryStore(tmp_path / "history.sqlite3") as store:
            for hour in (11, 9, 10):  # written out of order on purpose
                store.add_storage_sample(f"2026-05-01T{hour:02d}:00:00", self._stats(total=hour))
            assert [row["total"] for row in store.storage_series()] == [9, 10, 11]

    def test_limit_keeps_the_newest(self, tmp_path):
        with HistoryStore(tmp_path / "history.sqlite3") as store:
            for hour in (9, 10, 11):
                store.add_storage_sample(f"2026-05-01T{hour:02d}:00:00", self._stats(total=hour))
            rows = store.storage_series(limit=2)
        assert [row["total"] for row in rows] == [10, 11]

    def test_days_windows_the_series(self, tmp_path):
        from datetime import datetime, timedelta

        fresh = (datetime.now() - timedelta(hours=6)).isoformat(timespec="seconds")
        with HistoryStore(tmp_path / "history.sqlite3") as store:
            store.add_storage_sample("2020-01-01T09:00:00", self._stats(total=1))
            store.add_storage_sample(fresh, self._stats(total=2))
            rows = store.storage_series(days=30)
        assert [row["total"] for row in rows] == [2]

    def test_the_count_is_reported(self, tmp_path):
        with HistoryStore(tmp_path / "history.sqlite3") as store:
            assert store.count_storage_samples() == 0
            store.add_storage_sample("2026-05-01T09:00:00", self._stats())
            assert store.count_storage_samples() == 1

    def test_a_retention_prune_takes_the_old_samples_with_it(self, tmp_path):
        with HistoryStore(tmp_path / "history.sqlite3") as store:
            store.add_storage_sample("2020-01-01T09:00:00", self._stats(total=1))
            store.add_storage_sample("2999-01-01T09:00:00", self._stats(total=2))
            assert store.prune(30) == 0
            left = store.storage_series()
        assert [row["total"] for row in left] == [2]

    def test_a_rebuild_keeps_the_samples(self, tmp_path):
        from app.core.store import rebuild_index

        (tmp_path / "capture-report-20260501-090000.json").write_text(
            '{"generated_at": "2026-05-01T09:00:00", "results": [{"url": "https://a"}]}',
            encoding="utf-8",
        )
        with HistoryStore(tmp_path / "history.sqlite3") as store:
            store.add_storage_sample("2026-05-01T09:00:00", self._stats())
        rebuild_index(tmp_path)
        with HistoryStore(tmp_path / "history.sqlite3") as store:
            assert store.count_storage_samples() == 1
            assert store.count() == 1


class TestBrokenStoreConstruction:
    """A store that cannot be built must not keep the file open.

    On Windows an open handle blocks the unlink that repairs a corrupt index, so
    the failure has to close its own connection before it propagates.
    """

    def test_a_failed_open_closes_the_connection(self, tmp_path, monkeypatch):
        import sqlite3

        from app.core import store as store_module

        closed = []

        class FakeConnection:
            def executescript(self, _sql):
                raise sqlite3.DatabaseError("file is not a database")

            def close(self):
                closed.append(True)

        monkeypatch.setattr(store_module.sqlite3, "connect", lambda *_a, **_k: FakeConnection())

        with pytest.raises(sqlite3.DatabaseError):
            HistoryStore(tmp_path / "history.sqlite3")

        assert closed == [True]

    def test_a_corrupt_index_is_unlinked_and_rebuilt(self, tmp_path):
        """The repair path: an unreadable file is deleted, not worked around."""
        from app.core.store import rebuild_index

        db = tmp_path / "history.sqlite3"
        db.write_bytes(b"not a database")

        assert rebuild_index(tmp_path) == 0
        with HistoryStore(db) as store:
            assert store.count() == 0
