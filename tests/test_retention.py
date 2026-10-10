"""Tests for screenshot retention (app.core.retention)."""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

import pytest

from app.core import retention


def _touch(directory: Path, name: str, age_days: float = 0.0) -> Path:
    path = directory / name
    path.write_bytes(b"png-bytes")
    when = time.time() - age_days * 86400
    os.utime(path, (when, when))
    return path


class TestIsCaptureFile:
    def test_capture_images_are_prunable(self, tmp_path):
        assert retention.is_capture_file(tmp_path / "001_site_20260101-000000.png") is True
        assert retention.is_capture_file(tmp_path / "shot.webp") is True
        assert retention.is_capture_file(tmp_path / "shot.avif") is True

    def test_report_and_reference_files_are_protected(self, tmp_path):
        assert retention.is_capture_file(tmp_path / "capture-report-20260101-000000.json") is False
        assert retention.is_capture_file(tmp_path / "history.sqlite3") is False
        assert retention.is_capture_file(tmp_path / "latest_site.png") is False
        assert retention.is_capture_file(tmp_path / "baseline_site.png") is False

    def test_hidden_files_are_skipped(self, tmp_path):
        assert retention.is_capture_file(tmp_path / ".hidden.png") is False


class TestPruneScreenshots:
    def test_deletes_only_old_captures(self, tmp_path):
        old = _touch(tmp_path, "001_a_20260101-000000.png", age_days=30)
        fresh = _touch(tmp_path, "002_a_20260102-000000.png", age_days=1)
        removed = retention.prune_screenshots(tmp_path, 7)
        assert removed == [old]
        assert not old.exists() and fresh.exists()

    def test_zero_days_is_a_noop(self, tmp_path):
        old = _touch(tmp_path, "001_a.png", age_days=999)
        assert retention.prune_screenshots(tmp_path, 0) == []
        assert old.exists()

    def test_references_and_reports_survive(self, tmp_path):
        latest = _touch(tmp_path, "latest_site.png", age_days=999)
        pinned = _touch(tmp_path, "baseline_site.png", age_days=999)
        report = _touch(tmp_path, "capture-report-20260101-000000.json", age_days=999)
        retention.prune_screenshots(tmp_path, 7)
        assert latest.exists() and pinned.exists() and report.exists()

    def test_missing_folder_is_harmless(self, tmp_path):
        assert retention.prune_screenshots(tmp_path / "nope", 7) == []

    def test_now_parameter_makes_the_cutoff_deterministic(self, tmp_path):
        _touch(tmp_path, "001_a.png", age_days=10)
        # Pretend "now" is far in the future: everything is older than the window.
        removed = retention.prune_screenshots(tmp_path, 7, now=time.time() + 30 * 86400)
        assert len(removed) == 1


class TestDryRun:
    def test_dry_run_lists_without_deleting(self, tmp_path):
        old = _touch(tmp_path, "001_a_20260101-000000.png", age_days=30)
        candidates = retention.prune_screenshots(tmp_path, 7, dry_run=True)
        assert candidates == [old]
        assert old.exists()  # nothing was removed

    def test_dry_run_reports_the_same_set_as_the_real_run(self, tmp_path):
        _touch(tmp_path, "001_a.png", age_days=30)
        _touch(tmp_path, "002_b.png", age_days=1)
        dry = retention.prune_screenshots(tmp_path, 7, dry_run=True)
        real = retention.prune_screenshots(tmp_path, 7)
        assert [p.name for p in dry] == [p.name for p in real]


def _sized(directory: Path, name: str, kb: int, age_seconds: float) -> Path:
    """A file of roughly ``kb`` KB, ``age_seconds`` old."""
    path = directory / name
    path.write_bytes(b"x" * (kb * 1024))
    when = time.time() - age_seconds
    os.utime(path, (when, when))
    return path


class TestFolderBytes:
    def test_missing_folder_is_zero(self, tmp_path):
        assert retention.folder_bytes(tmp_path / "nope") == 0

    def test_every_file_counts(self, tmp_path):
        _sized(tmp_path, "a.png", 10, 0)
        (tmp_path / "capture-report-1.json").write_bytes(b"{}")
        assert retention.folder_bytes(tmp_path) == 10 * 1024 + 2


class TestPruneBySize:
    def test_cap_zero_is_a_no_op(self, tmp_path):
        _sized(tmp_path, "a.png", 200, 0)
        assert retention.prune_screenshots_by_size(tmp_path, 0) == []

    def test_a_folder_under_the_cap_is_left_alone(self, tmp_path):
        _sized(tmp_path, "a.png", 10, 0)
        assert retention.prune_screenshots_by_size(tmp_path, 5) == []

    def test_the_oldest_files_go_first(self, tmp_path):
        ago = _sized(tmp_path, "old.png", 600, 300)
        mid = _sized(tmp_path, "mid.png", 600, 200)
        new = _sized(tmp_path, "new.png", 600, 100)
        removed = retention.prune_screenshots_by_size(tmp_path, 1)
        assert [path.name for path in removed] == ["old.png", "mid.png"]
        assert not ago.exists() and not mid.exists()
        assert new.exists()  # the newest capture survives the sweep

    def test_it_stops_once_the_folder_fits(self, tmp_path):
        for index in range(4):
            _sized(tmp_path, f"shot{index}.png", 400, 400 - index * 10)
        retention.prune_screenshots_by_size(tmp_path, 1)
        assert retention.folder_bytes(tmp_path) <= 1024 * 1024

    def test_dry_run_lists_but_keeps(self, tmp_path):
        _sized(tmp_path, "old.png", 600, 300)
        _sized(tmp_path, "new.png", 600, 100)
        removed = retention.prune_screenshots_by_size(tmp_path, 1, dry_run=True)
        assert [path.name for path in removed] == ["old.png"]
        assert (tmp_path / "old.png").exists()
        assert retention.folder_bytes(tmp_path) == 1200 * 1024

    def test_references_are_never_deleted(self, tmp_path):
        _sized(tmp_path, "baseline_site.png", 600, 400)
        _sized(tmp_path, "latest_site.png", 600, 300)
        _sized(tmp_path, "001_site.png", 600, 200)
        removed = retention.prune_screenshots_by_size(tmp_path, 1)
        assert [path.name for path in removed] == ["001_site.png"]
        assert (tmp_path / "baseline_site.png").exists()
        assert (tmp_path / "latest_site.png").exists()

    def test_only_images_are_candidates(self, tmp_path):
        (tmp_path / "capture-report-1.json").write_bytes(b"x" * (200 * 1024))
        shot = _sized(tmp_path, "001_site.png", 200, 300)
        retention.prune_screenshots_by_size(tmp_path, 0.1)  # 100 KB
        assert not shot.exists()
        assert (tmp_path / "capture-report-1.json").exists()  # never a candidate

    def test_a_missing_folder_is_a_no_op(self, tmp_path):
        assert retention.prune_screenshots_by_size(tmp_path / "gone", 1) == []


class TestHistoryFootprint:
    """The history is its reports plus the SQLite index - nothing else."""

    def test_a_missing_or_empty_folder_measures_zero(self, tmp_path):
        assert retention.history_footprint(tmp_path / "gone") == 0
        assert retention.history_footprint(tmp_path) == 0

    def test_reports_and_the_index_both_count(self, tmp_path):
        (tmp_path / "capture-report-20260101-000000.json").write_bytes(b"x" * 300)
        (tmp_path / "capture-report-20260101-000000.csv").write_bytes(b"y" * 200)
        (tmp_path / "history.sqlite3").write_bytes(b"z" * 100)
        assert len(retention.report_files(tmp_path)) == 2
        assert retention.history_footprint(tmp_path) == 600

    def test_screenshots_are_not_part_of_the_history(self, tmp_path):
        (tmp_path / "001_site.png").write_bytes(b"x" * 5000)
        (tmp_path / "notes.txt").write_bytes(b"x" * 5000)
        assert retention.history_footprint(tmp_path) == 0


class TestReportGroups:
    def test_one_run_is_one_group(self, tmp_path):
        for suffix in (".json", ".csv"):
            (tmp_path / f"capture-report-20260101-000000{suffix}").write_text("x", encoding="utf-8")
        groups = retention.report_groups(tmp_path)
        assert len(groups) == 1
        assert sorted(path.suffix for path in groups[0]) == [".csv", ".json"]

    def test_groups_are_oldest_first(self, tmp_path):
        for stamp in ("20260103-000000", "20260101-000000", "20260102-000000"):
            (tmp_path / f"capture-report-{stamp}.json").write_text("x", encoding="utf-8")
        assert [group[0].stem for group in retention.report_groups(tmp_path)] == [
            "capture-report-20260101-000000",
            "capture-report-20260102-000000",
            "capture-report-20260103-000000",
        ]

    def test_an_empty_folder_groups_nothing(self, tmp_path):
        assert retention.report_groups(tmp_path) == []


class TestPruneHistory:
    """A folder that grows forever gets a cap, oldest runs first."""

    def _run_files(self, folder, stamp, kb=30):
        for suffix, filler in ((".json", b"x"), (".csv", b"y")):
            (folder / f"capture-report-{stamp}{suffix}").write_bytes(filler * (kb * 1024))

    def test_without_a_cap_nothing_is_touched(self, tmp_path):
        self._run_files(tmp_path, "20260101-000000")
        report = retention.prune_history(tmp_path, 0)
        assert not report.removed_anything and report.reports == () and report.rows == 0
        assert retention.report_files(tmp_path)

    def test_a_folder_under_the_cap_is_left_alone(self, tmp_path):
        self._run_files(tmp_path, "20260101-000000", kb=10)
        report = retention.prune_history(tmp_path, 10)
        assert not report.removed_anything
        assert len(retention.report_files(tmp_path)) == 2

    def test_a_missing_folder_is_a_no_op(self, tmp_path):
        report = retention.prune_history(tmp_path / "gone", 1)
        assert not report.removed_anything and report.bytes_before == 0

    def test_the_oldest_runs_go_first(self, tmp_path):
        for index in (1, 2, 3, 4):
            self._run_files(tmp_path, f"2026010{index}-000000", kb=30)  # 60 KB per run
        report = retention.prune_history(tmp_path, 0.1)  # 100 KB: one run can stay
        assert [path.name for path in report.reports] == [
            f"capture-report-2026010{index}-000000{suffix}"
            for index in (1, 2, 3)
            for suffix in (".csv", ".json")
        ]
        assert report.bytes_after <= report.cap_bytes
        assert len(retention.report_files(tmp_path)) == 2

    def test_a_run_is_deleted_whole(self, tmp_path):
        for index in (1, 2):
            self._run_files(tmp_path, f"2026010{index}-000000", kb=30)
        report = retention.prune_history(tmp_path, 0.08)  # room for one run only
        assert [path.name for path in report.reports] == [
            "capture-report-20260101-000000.csv",
            "capture-report-20260101-000000.json",
        ]
        assert [path.suffix for path in retention.report_files(tmp_path)] == [".csv", ".json"]

    def test_a_summary_names_what_went(self, tmp_path):
        self._run_files(tmp_path, "20260101-000000", kb=30)
        self._run_files(tmp_path, "20260102-000000", kb=30)
        report = retention.prune_history(tmp_path, 0.04)
        assert report.summary() == "2 report file(s)"  # the newest run is never a victim
        assert retention.HistoryPrune().summary() == "nothing"
        assert not retention.HistoryPrune().removed_anything

    def test_a_dry_run_touches_nothing(self, tmp_path):
        for index in (1, 2, 3):
            self._run_files(tmp_path, f"2026010{index}-000000", kb=30)
        report = retention.prune_history(tmp_path, 0.04, dry_run=True)
        assert report.dry_run and report.removed_anything
        assert len(retention.report_files(tmp_path)) == 6  # 3 runs, 6 files

    def test_the_dry_run_predicts_the_real_sweep(self, tmp_path):
        for index in (1, 2, 3, 4):
            self._run_files(tmp_path, f"2026010{index}-000000", kb=25)
        dry = retention.prune_history(tmp_path, 0.06, dry_run=True)
        real = retention.prune_history(tmp_path, 0.06)
        assert [path.name for path in dry.reports] == [path.name for path in real.reports]
        assert dry.rows == real.rows

    def test_the_index_is_trimmed_when_the_reports_are_not_enough(self, tmp_path):
        from app.core.store import HistoryStore

        db = tmp_path / "history.sqlite3"
        with HistoryStore(db) as store:
            for index in range(800):
                store.add_run(
                    f"2026-01-{(index % 28) + 1:02d}T00:00:00",
                    [
                        {
                            "url": f"https://site{index}.example.com",
                            "status": "success",
                            "diff": 0.1,
                            "file_path": f"/tmp/shots/capture-{index}.png",
                        }
                    ],
                )
            before = store.size_bytes()
        report = retention.prune_history(tmp_path, 0.02)  # 20 KB, impossible with any rows
        assert report.rows > 0 and report.bytes_after < before
        with HistoryStore(db) as store:
            assert store.count() == 1  # the newest capture survives
            assert store.flat_rows()[0]["url"] == "https://site783.example.com"

    def test_a_healthy_index_is_left_alone(self, tmp_path):
        from app.core.store import HistoryStore

        db = tmp_path / "history.sqlite3"
        with HistoryStore(db) as store:
            store.add_run("2026-01-01T00:00:00", [{"url": "https://a.com", "status": "success"}])
        report = retention.prune_history(tmp_path, 5)
        assert report.rows == 0 and not report.removed_anything
        with HistoryStore(db) as store:
            assert store.count() == 1


class TestPruneIndexToSize:
    """The helper the history cap uses to shrink the SQLite file itself."""

    def test_a_missing_index_is_not_an_error(self, tmp_path):
        assert retention.prune_index_to_size(tmp_path / "history.sqlite3", 1024) == 0

    def test_a_corrupt_index_is_not_an_error(self, tmp_path):
        db = tmp_path / "history.sqlite3"
        db.write_text("not a database", encoding="utf-8")
        assert retention.prune_index_to_size(db, 1024) == 0


class TestHistoryCapNeverEatsTheNewestRun:
    """A cap smaller than one run must not leave an empty history behind."""

    def _run(self, folder, stamp, kb=30):
        for suffix, filler in ((".json", b"x"), (".csv", b"y")):
            (folder / f"capture-report-{stamp}{suffix}").write_bytes(filler * (kb * 1024))

    def test_the_newest_reports_stay_even_when_the_cap_is_tiny(self, tmp_path):
        for index in (1, 2, 3):
            self._run(tmp_path, f"2026010{index}-000000")
        report = retention.prune_history(tmp_path, 0.001)  # 1 KB: nothing fits
        assert [path.name for path in retention.report_files(tmp_path)] == [
            "capture-report-20260103-000000.csv",
            "capture-report-20260103-000000.json",
        ]
        assert report.bytes_after > report.cap_bytes  # and it says so with the numbers

    def test_a_single_run_is_left_alone(self, tmp_path):
        self._run(tmp_path, "20260101-000000", kb=500)
        report = retention.prune_history(tmp_path, 0.1)
        assert report.reports == () and not report.removed_anything
        assert len(retention.report_files(tmp_path)) == 2

    def test_the_newest_row_stays_when_only_the_index_shrinks(self, tmp_path):
        from app.core.store import HistoryStore

        db = tmp_path / "history.sqlite3"
        with HistoryStore(db) as store:
            for index in range(200):
                store.add_run(
                    f"2026-01-{(index % 28) + 1:02d}T00:00:00",
                    [{"url": f"https://site{index}.example.com", "status": "success", "diff": 0.1}],
                )
        report = retention.prune_history(tmp_path, 0.01)
        assert report.rows > 0 and report.reports == ()
        with HistoryStore(db) as store:
            rows = store.flat_rows()
            assert rows
            assert rows[0]["timestamp"] == max(row["timestamp"] for row in rows)


class TestStorageForecast:
    """The growth numbers the Storage panel turns into "days of room left"."""

    def _report(self, folder, stamp, kb=1):
        path = folder / f"capture-report-{stamp}.json"
        path.write_bytes(b"x" * (kb * 1024))
        return path

    def test_days_to_cap_needs_a_cap(self):
        assert retention.days_to_cap(1024, 0, 1024.0) is None

    def test_days_to_cap_with_room_and_a_rate(self):
        assert retention.days_to_cap(0, 10 * 1024, 1024.0) == 10.0

    def test_days_to_cap_at_the_cap_is_zero(self):
        assert retention.days_to_cap(2048, 1024, 512.0) == 0.0

    def test_days_to_cap_without_growth_is_unknown(self):
        assert retention.days_to_cap(1024, 10 * 1024, 0.0) is None

    def test_stamp_moment_reads_the_report_name(self, tmp_path):
        path = self._report(tmp_path, "20260301-120000")
        stamp = retention.stamp_moment(path)
        assert stamp is not None
        assert (stamp.year, stamp.month, stamp.day, stamp.hour) == (2026, 3, 1, 12)

    def test_stamp_moment_falls_back_to_the_file_mtime(self, tmp_path):
        path = tmp_path / "renamed-report.json"
        path.write_bytes(b"{}")
        os.utime(path, (1_700_000_000, 1_700_000_000))
        stamp = retention.stamp_moment(path)
        assert stamp is not None and stamp.year == 2023

    def test_run_sizes_are_oldest_first_and_limited(self, tmp_path):
        for index in (1, 2, 3):
            self._report(tmp_path, f"2026010{index}-000000", kb=index)
        samples = retention.run_sizes(tmp_path, limit=2)
        assert [size for _moment, size in samples] == [2 * 1024, 3 * 1024]

    def test_history_growth_uses_the_median_run_and_the_real_span(self, tmp_path):
        for index, kb in enumerate((1, 1, 5), start=1):
            self._report(tmp_path, f"2026010{index}-000000", kb=kb)
        # median 1 KB, three runs over two days -> 1.5 KB/day.
        assert retention.history_growth_per_day(tmp_path) == pytest.approx(1.5 * 1024)

    def test_a_single_run_has_no_rate(self, tmp_path):
        self._report(tmp_path, "20260101-000000")
        assert retention.history_growth_per_day(tmp_path) == 0.0

    def test_a_span_under_ten_minutes_is_not_a_rate(self, tmp_path):
        # Two captures five minutes apart would extrapolate to terabytes a week.
        self._report(tmp_path, "20260101-000000", kb=100)
        self._report(tmp_path, "20260101-000500", kb=100)
        assert retention.history_growth_per_day(tmp_path) == 0.0

    def test_screenshot_growth_from_the_capture_mtimes(self, tmp_path):
        base = time.time() - 2 * 86400
        for index in range(3):
            path = tmp_path / f"{index:03d}_a_com_2026010{index + 1}-000000.png"
            path.write_bytes(b"p" * 1024)
            os.utime(path, (base + index * 86400, base + index * 86400))
        rate = retention.screenshot_growth_per_day(tmp_path)
        assert rate == pytest.approx(1.5 * 1024, rel=0.05)

    def test_a_missing_folder_has_no_screenshot_rate(self, tmp_path):
        assert retention.screenshot_growth_per_day(tmp_path / "nope") == 0.0

    def test_reports_are_not_counted_as_captures(self, tmp_path):
        self._report(tmp_path, "20260101-000000")
        assert retention.screenshot_growth_per_day(tmp_path) == 0.0


class TestArchiveRuns:
    """Zipping the runs a retention rule would otherwise destroy."""

    def _run(self, folder, stamp, kb=30):
        for suffix, filler in ((".json", b"x"), (".csv", b"y")):
            (folder / f"capture-report-{stamp}{suffix}").write_bytes(filler * (kb * 1024))

    def test_runs_older_than_the_cutoff_are_archived(self, tmp_path):
        self._run(tmp_path, "20200101-000000")
        self._run(tmp_path, "20200102-000000")
        fresh = tmp_path / "capture-report-29990101-000000.json"
        fresh.write_bytes(b"{}")
        result = retention.archive_runs(tmp_path, 30)
        assert len(result.files) == 4
        assert [path.name for path in result.archives] == ["archive-2020-01.zip"]
        assert fresh.exists()  # only the old runs moved
        assert retention.report_files(tmp_path) == [fresh]

    def test_a_dry_run_keeps_everything(self, tmp_path):
        self._run(tmp_path, "20200101-000000")
        result = retention.archive_runs(tmp_path, 30, dry_run=True)
        assert result.dry_run and result.removed_anything
        assert not retention.archive_files(tmp_path)
        assert len(retention.report_files(tmp_path)) == 2

    def test_one_zip_per_month_and_an_append_does_not_duplicate(self, tmp_path):
        import zipfile

        self._run(tmp_path, "20200101-000000")
        self._run(tmp_path, "20200201-000000")
        retention.archive_runs(tmp_path, 30)
        assert [path.name for path in retention.archive_files(tmp_path)] == [
            "archive-2020-01.zip",
            "archive-2020-02.zip",
        ]
        # The same run appears again (a hand-copied file): the zip must not grow a
        # second copy of it.
        (tmp_path / "capture-report-20200101-000000.json").write_bytes(b"x" * 30 * 1024)
        retention.archive_runs(tmp_path, 30)
        with zipfile.ZipFile(tmp_path / "archive-2020-01.zip") as handle:
            assert handle.namelist().count("capture-report-20200101-000000.json") == 1

    def test_zero_days_is_a_noop(self, tmp_path):
        self._run(tmp_path, "20200101-000000")
        assert not retention.archive_runs(tmp_path, 0).removed_anything

    def test_the_archive_holds_the_original_bytes(self, tmp_path):
        import zipfile

        payload = b"json-body" * 100
        (tmp_path / "capture-report-20200101-000000.json").write_bytes(payload)
        retention.archive_runs(tmp_path, 30)
        with zipfile.ZipFile(tmp_path / "archive-2020-01.zip") as handle:
            assert handle.read("capture-report-20200101-000000.json") == payload

    def test_restore_brings_a_month_back(self, tmp_path):
        import zipfile

        self._run(tmp_path, "20200101-000000")
        retention.archive_runs(tmp_path, 30)
        archive = retention.archive_files(tmp_path)[0]
        restored = retention.restore_archive(tmp_path, archive)
        assert [path.name for path in restored] == [
            "capture-report-20200101-000000.csv",
            "capture-report-20200101-000000.json",
        ]
        assert len(retention.report_files(tmp_path)) == 2
        assert zipfile.is_zipfile(archive)

    def test_restore_keeps_a_report_that_is_already_there(self, tmp_path):
        self._run(tmp_path, "20200101-000000")
        retention.archive_runs(tmp_path, 30)
        archive = retention.archive_files(tmp_path)[0]
        kept = tmp_path / "capture-report-20200101-000000.csv"
        kept.write_bytes(b"the newer copy")
        restored = retention.restore_archive(tmp_path, archive)
        assert [path.name for path in restored] == ["capture-report-20200101-000000.json"]
        assert kept.read_bytes() == b"the newer copy"
        again = retention.restore_archive(tmp_path, archive, overwrite=True)
        assert len(again) == 2

    def test_restore_is_a_dry_run_when_asked(self, tmp_path):
        self._run(tmp_path, "20200101-000000")
        retention.archive_runs(tmp_path, 30)
        archive = retention.archive_files(tmp_path)[0]
        planned = retention.restore_archive(tmp_path, archive, dry_run=True)
        assert len(planned) == 2
        assert retention.report_files(tmp_path) == []

    def test_restore_ignores_foreign_entries(self, tmp_path):
        import zipfile

        archive = tmp_path / "archive-2020-01.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("../escape.json", "{}")
            handle.writestr("001_a_20260101-000000.png", "png")
            handle.writestr("notes.txt", "hello")
            handle.writestr("capture-report-20200101-000000.json", "{}")
        restored = retention.restore_archive(tmp_path, archive)
        assert [path.name for path in restored] == ["capture-report-20200101-000000.json"]
        assert not (tmp_path.parent / "escape.json").exists()

    def test_a_broken_zip_is_a_value_error(self, tmp_path):
        archive = tmp_path / "archive-2020-01.zip"
        archive.write_text("not a zip", encoding="utf-8")
        with pytest.raises(ValueError, match="Could not read"):
            retention.restore_archive(tmp_path, archive)

    def test_a_missing_zip_is_a_value_error(self, tmp_path):
        with pytest.raises(ValueError, match="Could not read"):
            retention.restore_archive(tmp_path, tmp_path / "nope.zip")


class TestPruneHistoryArchive:
    """The size cap either forgets a run or zips it - never both by accident."""

    def _run(self, folder, stamp, kb=30):
        for suffix, filler in ((".json", b"x"), (".csv", b"y")):
            (folder / f"capture-report-{stamp}{suffix}").write_bytes(filler * (kb * 1024))

    def test_the_cap_keeps_what_it_drops(self, tmp_path):
        for index in (1, 2, 3):
            self._run(tmp_path, f"2026010{index}-000000")
        report = retention.prune_history(tmp_path, 0.04, archive=True)
        assert report.archives == (tmp_path / "archive-2026-01.zip",)
        assert report.bytes_after < report.bytes_before
        assert retention.archive_files(tmp_path) == [tmp_path / "archive-2026-01.zip"]
        assert len(retention.report_files(tmp_path)) == 2  # the newest run stays

    def test_a_dry_run_archives_nothing(self, tmp_path):
        for index in (1, 2, 3):
            self._run(tmp_path, f"2026010{index}-000000")
        report = retention.prune_history(tmp_path, 0.04, archive=True, dry_run=True)
        assert report.archives == (tmp_path / "archive-2026-01.zip",)
        assert not retention.archive_files(tmp_path)
        assert len(retention.report_files(tmp_path)) == 6

    def test_without_the_flag_a_run_is_gone_for_good(self, tmp_path):
        for index in (1, 2, 3):
            self._run(tmp_path, f"2026010{index}-000000")
        report = retention.prune_history(tmp_path, 0.04)
        assert report.archives == ()
        assert not retention.archive_files(tmp_path)

    def test_the_archive_can_be_restored_afterwards(self, tmp_path):
        for index in (1, 2, 3):
            self._run(tmp_path, f"2026010{index}-000000")
        retention.prune_history(tmp_path, 0.04, archive=True)
        restored = retention.restore_archive(tmp_path, retention.archive_files(tmp_path)[0])
        assert sorted(path.name for path in restored) == [
            "capture-report-20260101-000000.csv",
            "capture-report-20260101-000000.json",
            "capture-report-20260102-000000.csv",
            "capture-report-20260102-000000.json",
        ]


class TestArchiveListing:
    def test_archives_are_listed_oldest_first(self, tmp_path):
        for name in ("archive-2026-02.zip", "archive-2026-01.zip", "notes.txt"):
            (tmp_path / name).write_bytes(b"z")
        assert [path.name for path in retention.archive_files(tmp_path)] == [
            "archive-2026-01.zip",
            "archive-2026-02.zip",
        ]

    def test_archive_bytes_sums_them(self, tmp_path):
        (tmp_path / "archive-2026-01.zip").write_bytes(b"z" * 100)
        assert retention.archive_bytes(tmp_path) == 100
        assert retention.archive_bytes(tmp_path / "nope") == 0


class TestSiteSpace:
    """``site_space`` answers "which host costs the most?"."""

    @staticmethod
    def _capture(folder, label, stamp, size=100, suffix=".png", index="001"):
        path = folder / f"{index}_{label}_{stamp}{suffix}"
        path.write_bytes(b"x" * size)
        return path

    def test_an_empty_folder_has_no_sites(self, tmp_path):
        assert retention.site_space(tmp_path) == []

    def test_a_missing_folder_is_not_an_error(self, tmp_path):
        assert retention.site_space(tmp_path / "nope") == []

    def test_the_biggest_site_comes_first(self, tmp_path):
        self._capture(tmp_path, "small_com", "20260101-000000", size=10)
        self._capture(tmp_path, "big_com", "20260101-000000", size=500)
        spaces = retention.site_space(tmp_path)
        assert [space.label for space in spaces] == ["big_com", "small_com"]
        assert spaces[0].bytes == 500 and spaces[0].files == 1

    def test_limit_keeps_the_top_n(self, tmp_path):
        self._capture(tmp_path, "a_com", "20260101-000000", size=10)
        self._capture(tmp_path, "b_com", "20260101-000000", size=500)
        spaces = retention.site_space(tmp_path, limit=1)
        assert [space.label for space in spaces] == ["b_com"]

    def test_captures_of_one_site_are_added_up(self, tmp_path):
        self._capture(tmp_path, "a_com", "20260101-000000", size=100)
        self._capture(tmp_path, "a_com", "20260102-000000", size=200, index="002", suffix=".webp")
        space = retention.site_space(tmp_path)[0]
        assert (space.files, space.bytes, space.references) == (2, 300, 0)
        assert (space.oldest, space.newest) == ("20260101-000000", "20260102-000000")

    def test_references_count_towards_their_site_but_are_marked(self, tmp_path):
        self._capture(tmp_path, "a_com", "20260101-000000", size=100)
        (tmp_path / "latest_a_com.png").write_bytes(b"y" * 50)
        (tmp_path / "baseline_a_com.png").write_bytes(b"y" * 25)
        space = retention.site_space(tmp_path)[0]
        assert space.references == 2
        assert space.files == 3
        assert space.bytes == 175

    def test_reports_archives_and_notes_are_not_sites(self, tmp_path):
        (tmp_path / "capture-report-20260101-000000.json").write_text("{}", encoding="utf-8")
        (tmp_path / "archive-2026-01.zip").write_bytes(b"z")
        (tmp_path / "notes.txt").write_bytes(b"z")
        assert retention.site_space(tmp_path) == []

    def test_a_prefixed_capture_reports_the_whole_middle_as_its_label(self, tmp_path):
        # A user-chosen filename_prefix sits between the index and the label; the
        # panel then groups by "pre_a_com". Pruning still finds the site either
        # way, because a query is matched as a fragment (see TestPruneSite).
        self._capture(tmp_path, "a_com", "20260101-000000", size=100, index="001_pre")
        space = retention.site_space(tmp_path)[0]
        assert space.label == "pre_a_com" and space.bytes == 100


class TestSiteNeedles:
    """A user types a host; the files carry a label."""

    def test_the_host_form_becomes_the_label(self):
        assert "a_example_com" in retention.site_needles("a.example.com")

    def test_a_label_is_kept_as_typed(self):
        assert "a_example_com" in retention.site_needles("a_example_com")

    def test_a_full_url_matches_the_label_of_its_path(self):
        assert "a_example_com_login" in retention.site_needles("https://a.example.com/login")

    def test_case_does_not_matter(self):
        assert "a_example_com" in retention.site_needles("A.Example.COM")

    def test_an_empty_query_has_no_needles(self):
        assert retention.site_needles("") == ()
        assert retention.site_needles("   ") == ()


class TestPruneSite:
    """Pruning one site must leave every other site (and the history) alone."""

    def _two_sites(self, folder):
        (folder / "001_a_example_com_20260101-000000.png").write_bytes(b"a" * 300)
        (folder / "002_a_example_com_20260102-000000.png").write_bytes(b"a" * 100)
        (folder / "001_b_example_com_20260101-000000.png").write_bytes(b"b" * 50)
        (folder / "latest_a_example_com.png").write_bytes(b"r" * 10)
        (folder / "capture-report-20260101-000000.json").write_text("{}", encoding="utf-8")

    def test_a_dry_run_deletes_nothing(self, tmp_path):
        self._two_sites(tmp_path)
        before = sorted(path.name for path in tmp_path.iterdir())
        result = retention.prune_site(tmp_path, "a.example.com", dry_run=True)
        assert result.dry_run is True
        assert result.summary() == "2 file(s) matching 'a.example.com'"
        assert result.bytes == 400
        assert sorted(path.name for path in tmp_path.iterdir()) == before

    def test_only_that_site_is_deleted(self, tmp_path):
        self._two_sites(tmp_path)
        result = retention.prune_site(tmp_path, "a.example.com")
        left = sorted(path.name for path in tmp_path.iterdir())
        assert result.removed_anything is True
        assert result.to_dict() == {
            "site": "a.example.com",
            "deleted": 2,
            "bytes": 400,
            "dry_run": False,
        }
        assert left == [
            "001_b_example_com_20260101-000000.png",
            "capture-report-20260101-000000.json",
            "latest_a_example_com.png",
        ]

    def test_the_label_form_matches_too(self, tmp_path):
        self._two_sites(tmp_path)
        assert retention.prune_site(tmp_path, "a_example_com").to_dict()["deleted"] == 2

    def test_a_full_url_matches_its_site(self, tmp_path):
        self._two_sites(tmp_path)
        result = retention.prune_site(tmp_path, "https://a.example.com/")
        assert result.to_dict()["deleted"] == 2

    def test_an_unknown_site_is_a_no_op(self, tmp_path):
        self._two_sites(tmp_path)
        result = retention.prune_site(tmp_path, "zzz.example.com")
        assert result.removed_anything is False
        assert result.summary() == "nothing"
        assert len(list(tmp_path.iterdir())) == 5

    def test_an_empty_query_touches_nothing(self, tmp_path):
        self._two_sites(tmp_path)
        assert retention.prune_site(tmp_path, "   ").removed_anything is False

    def test_a_missing_folder_is_a_no_op(self, tmp_path):
        assert retention.prune_site(tmp_path / "nope", "a.example.com").removed_anything is False

    def test_references_and_reports_are_never_pruned(self, tmp_path):
        self._two_sites(tmp_path)
        retention.prune_site(tmp_path, "a.example.com")
        assert (tmp_path / "latest_a_example_com.png").is_file()
        assert (tmp_path / "capture-report-20260101-000000.json").is_file()


class TestSeriesRate:
    """A least-squares slope, with honest answers for the awkward cases."""

    BASE = datetime(2026, 5, 1, 9, 0)

    def _series(self, step=500, days=5):
        from datetime import timedelta

        return [(self.BASE + timedelta(days=index), 1000 + step * index) for index in range(days)]

    def test_a_steady_drip_is_its_slope(self):
        assert retention.series_rate(self._series(step=500)) == pytest.approx(500.0)

    def test_the_same_total_over_more_days_is_slower(self):
        from datetime import timedelta

        series = [(self.BASE, 0), (self.BASE + timedelta(days=10), 1000)]
        assert retention.series_rate(series) == pytest.approx(100.0)

    def test_a_flat_series_does_not_grow(self):
        assert retention.series_rate(self._series(step=0)) == 0.0

    def test_a_shrinking_series_is_never_negative(self):
        assert retention.series_rate(self._series(step=-100)) == 0.0

    def test_one_sample_cannot_be_fitted(self):
        assert retention.series_rate(self._series()[:1]) is None
        assert retention.series_rate([]) is None

    def test_samples_at_one_instant_have_no_slope(self):
        assert retention.series_rate([(self.BASE, 10), (self.BASE, 90)]) is None

    def test_a_burst_inside_the_span_guard_is_refused(self):
        from datetime import timedelta

        series = [(self.BASE, 10), (self.BASE + timedelta(minutes=5), 900)]
        assert retention.series_rate(series, retention.MIN_SPAN_DAYS) is None
        assert retention.series_rate(series) is not None

    def test_unsorted_samples_are_sorted_first(self):
        assert retention.series_rate(list(reversed(self._series(step=250)))) == pytest.approx(250.0)


class TestDailyDelta:
    """The chart's numbers: net bytes per calendar day."""

    def _at(self, day, hour=9):
        return datetime(2026, 5, day, hour, 0)

    def test_each_day_is_compared_with_the_day_before(self):
        deltas = retention.daily_delta([(self._at(1), 100), (self._at(2), 300)])
        assert deltas == [("2026-05-02", 200)]

    def test_the_last_sample_of_a_day_wins(self):
        deltas = retention.daily_delta(
            [(self._at(1), 100), (self._at(2, 9), 300), (self._at(2, 18), 250)]
        )
        assert deltas == [("2026-05-02", 150)]

    def test_a_gap_is_not_a_spike(self):
        deltas = retention.daily_delta([(self._at(1), 100), (self._at(4), 700)])
        assert deltas == [("2026-05-04", 600)]

    def test_a_shrinking_day_is_negative(self):
        deltas = retention.daily_delta([(self._at(1), 500), (self._at(2), 100)])
        assert deltas == [("2026-05-02", -400)]

    def test_one_day_is_not_a_trend(self):
        assert retention.daily_delta([(self._at(1), 500)]) == []
        assert retention.daily_delta([]) == []

    def test_daily_growth_reads_the_report_sizes(self, tmp_path):
        for day, size in ((1, 100), (2, 260)):
            (tmp_path / f"capture-report-2026050{day}-090000.json").write_bytes(b"{" + b"x" * size)
        assert retention.daily_growth(tmp_path) == [("2026-05-02", 160)]


class TestSiteCaps:
    """A size budget per site: trim the chatty host, not the whole folder."""

    def _shots(self, tmp_path, label, count, kb, start=1):
        for index in range(count):
            stamp = f"2026{start + index:02d}01-090000"
            path = tmp_path / f"{index + 1:03d}_{label}_{stamp}.png"
            path.write_bytes(b"x" * (kb * 1024))
        return tmp_path

    def test_the_text_form_is_read(self):
        assert retention.parse_site_caps("news.example.com=500, *=1000") == {
            "news.example.com": 500.0,
            "*": 1000.0,
        }
        assert retention.parse_site_caps("news.example.com=2;*=3") == {
            "news.example.com": 2.0,
            "*": 3.0,
        }

    def test_a_bare_number_means_every_site(self):
        assert retention.parse_site_caps("500") == {"*": 500.0}

    def test_an_empty_spec_is_no_caps(self):
        assert retention.parse_site_caps("") == {}
        assert retention.parse_site_caps(None) == {}
        assert retention.parse_site_caps("  ,  ") == {}

    def test_a_dict_is_accepted_as_it_is(self):
        assert retention.parse_site_caps({"news.example.com": "500"}) == {"news.example.com": 500.0}

    def test_rubbish_is_refused_by_name(self):
        with pytest.raises(retention.SiteCapError):
            retention.parse_site_caps("news.example.com=soon")
        with pytest.raises(retention.SiteCapError):
            retention.parse_site_caps("news.example.com=0")
        with pytest.raises(retention.SiteCapError):
            retention.parse_site_caps("news.example.com=-5")

    def test_the_host_the_user_knows_matches_the_label_the_files_carry(self):
        caps = retention.parse_site_caps("news.example.com=500")
        assert retention.site_cap_for("news_example_com", caps) == 500.0
        assert retention.site_cap_for("news_example_com_live", caps) == 500.0

    def test_the_star_covers_everything_else(self):
        caps = retention.parse_site_caps("news.example.com=500, *=1000")
        assert retention.site_cap_for("shop_example_com", caps) == 1000.0
        assert retention.site_cap_for("news_example_com", caps) == 500.0

    def test_a_site_with_no_cap_has_none(self):
        caps = retention.parse_site_caps("news.example.com=500")
        assert retention.site_cap_for("shop_example_com", caps) == 0.0
        assert retention.site_cap_for("anything", {}) == 0.0

    def test_the_oldest_captures_go_first(self, tmp_path):
        self._shots(tmp_path, "news_example_com", count=6, kb=300)  # 1.8 MB
        trims = retention.apply_site_caps(tmp_path, "news.example.com=1")
        assert len(trims) == 1
        trim = trims[0]
        assert trim.label == "news_example_com"
        assert trim.cap_mb == 1.0
        assert trim.removed_anything is True
        assert trim.kept_bytes <= 1024 * 1024
        names = sorted(path.name for path in tmp_path.glob("*_news_example_com_*.png"))
        assert names[0].startswith("004_")  # 001, 002 and 003 were the oldest
        assert len(names) == 3

    def test_references_are_never_deleted(self, tmp_path):
        self._shots(tmp_path, "news_example_com", count=6, kb=300)
        (tmp_path / "latest_news_example_com.png").write_bytes(b"y" * 200 * 1024)
        (tmp_path / "baseline_news_example_com.png").write_bytes(b"y" * 100 * 1024)
        retention.apply_site_caps(tmp_path, "news.example.com=1")
        assert (tmp_path / "latest_news_example_com.png").exists()
        assert (tmp_path / "baseline_news_example_com.png").exists()

    def test_a_site_whose_references_alone_exceed_the_cap_is_reported(self, tmp_path):
        self._shots(tmp_path, "news_example_com", count=12, kb=100)  # 1.2 MB of captures
        (tmp_path / "latest_news_example_com.png").write_bytes(b"y" * 3 * 1024 * 1024)
        trims = retention.apply_site_caps(tmp_path, "news.example.com=1")
        assert trims[0].removed_anything is True  # the oldest captures went
        assert trims[0].still_over is True
        assert trims[0].reference_bytes == 3 * 1024 * 1024
        assert "document" not in trims[0].summary()  # the summary stays about the captures

    def test_a_site_inside_its_budget_is_left_alone(self, tmp_path):
        self._shots(tmp_path, "news_example_com", count=2, kb=100)
        assert retention.apply_site_caps(tmp_path, "news.example.com=10") == []

    def test_the_dry_run_deletes_nothing(self, tmp_path):
        self._shots(tmp_path, "news_example_com", count=6, kb=300)
        trims = retention.apply_site_caps(tmp_path, "news.example.com=1", dry_run=True)
        assert trims[0].dry_run is True
        assert "would delete" in trims[0].summary()
        assert len(list(tmp_path.glob("*_news_example_com_*.png"))) == 6

    def test_only_the_sites_over_their_own_budget_are_touched(self, tmp_path):
        self._shots(tmp_path, "news_example_com", count=6, kb=300)
        self._shots(tmp_path, "shop_example_com", count=6, kb=300)
        trims = retention.apply_site_caps(tmp_path, "news.example.com=1")
        assert [trim.label for trim in trims] == ["news_example_com"]
        assert len(list(tmp_path.glob("*_shop_example_com_*.png"))) == 6

    def test_the_star_cap_trims_everything_that_is_left(self, tmp_path):
        self._shots(tmp_path, "news_example_com", count=6, kb=300)
        self._shots(tmp_path, "shop_example_com", count=6, kb=300)
        trims = retention.apply_site_caps(tmp_path, "1")
        assert sorted(trim.label for trim in trims) == ["news_example_com", "shop_example_com"]

    def test_the_biggest_site_is_reported_first(self, tmp_path):
        self._shots(tmp_path, "small_example_com", count=3, kb=400)
        self._shots(tmp_path, "big_example_com", count=9, kb=400)
        trims = retention.apply_site_caps(tmp_path, "1")
        assert [trim.label for trim in trims[:2]] == ["big_example_com", "small_example_com"]

    def test_the_limit_keeps_the_n_biggest(self, tmp_path):
        self._shots(tmp_path, "small_example_com", count=3, kb=400)
        self._shots(tmp_path, "big_example_com", count=9, kb=400)
        assert len(retention.apply_site_caps(tmp_path, "1", limit=1)) == 1

    def test_a_missing_folder_is_not_an_error(self, tmp_path):
        assert retention.apply_site_caps(tmp_path / "nope", "1") == []

    def test_no_caps_means_no_work(self, tmp_path):
        self._shots(tmp_path, "news_example_com", count=6, kb=300)
        assert retention.apply_site_caps(tmp_path, "") == []

    def test_a_bad_spec_raises(self, tmp_path):
        with pytest.raises(retention.SiteCapError):
            retention.apply_site_caps(tmp_path, "news.example.com=soon")

    def test_the_trim_serialises(self, tmp_path):
        self._shots(tmp_path, "news_example_com", count=6, kb=300)
        data = retention.apply_site_caps(tmp_path, "news.example.com=1")[0].to_dict()
        assert data["site"] == "news_example_com"
        assert data["deleted"] > 0
        assert data["bytes"] > 0
        assert data["still_over"] is False
        assert data["dry_run"] is False
