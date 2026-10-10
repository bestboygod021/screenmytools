"""Tests for the folder verifier (app.core.verify) and its CLI."""

from __future__ import annotations

import json
import os

import pytest

from app import cli
from app.core import verify
from app.core.store import HistoryStore, rebuild_index


def _report(folder, stamp, urls=("https://a.example.com",), rows=True, when=None):
    """One report file; ``rows=False`` writes a report without results."""
    payload = {
        "generated_at": when or f"2026-01-0{stamp[-1]}T00:00:00",
        "results": (
            [{"url": url, "status": "success", "diff": 0.1, "file_path": ""} for url in urls]
            if rows
            else "nonsense"
        ),
    }
    path = folder / f"capture-report-2026010{stamp}-000000.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    (folder / f"capture-report-2026010{stamp}-000000.csv").write_text("h\n", encoding="utf-8")
    return path


def _clean_folder(folder):
    _report(folder, "1")
    _report(folder, "2")
    rebuild_index(folder)


class TestReportChecks:
    def test_a_consistent_folder_is_clean(self, tmp_path):
        _clean_folder(tmp_path)
        report = verify.verify_folder(tmp_path)
        assert report.clean
        assert (report.reports, report.report_rows, report.index_rows) == (2, 2, 2)
        assert report.summary().endswith("- clean.")

    def test_an_empty_folder_has_nothing_to_verify(self, tmp_path):
        report = verify.verify_folder(tmp_path)
        assert report.clean and report.reports == 0 and not report.index_exists

    def test_a_missing_folder_is_not_an_error(self, tmp_path):
        assert verify.verify_folder(tmp_path / "nope").clean

    def test_a_truncated_report_is_named(self, tmp_path):
        _clean_folder(tmp_path)
        (tmp_path / "capture-report-20260109-000000.json").write_text("{oops", encoding="utf-8")
        report = verify.verify_folder(tmp_path)
        kinds = report.kinds()
        assert kinds[verify.UNREADABLE_REPORT] == 1
        assert "capture-report-20260109-000000.json" in report.issues[0].detail or any(
            "20260109" in issue.detail for issue in report.issues
        )

    def test_a_report_without_a_results_list_is_flagged(self, tmp_path):
        _clean_folder(tmp_path)
        _report(tmp_path, "3", rows=False)
        kinds = verify.verify_folder(tmp_path).kinds()
        assert kinds[verify.EMPTY_REPORT] == 1
        assert not kinds.get(verify.UNINDEXED_ROWS)  # no rows, so nothing to miss

    def test_a_report_without_its_csv_twin_is_flagged(self, tmp_path):
        _clean_folder(tmp_path)
        path = _report(tmp_path, "3")
        path.with_suffix(".csv").unlink()
        assert verify.verify_folder(tmp_path).kinds()[verify.MISSING_CSV] == 1

    @pytest.mark.skipif(
        os.name == "nt",
        reason="NTFS is case-insensitive: the two names are one and the same file",
    )
    def test_two_reports_claiming_one_stamp_are_flagged(self, tmp_path):
        _clean_folder(tmp_path)
        source = tmp_path / "capture-report-20260101-000000.json"
        (tmp_path / "capture-report-20260101-000000.JSON").write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8"
        )
        assert verify.verify_folder(tmp_path).kinds()[verify.DUPLICATE_REPORT] == 1


class TestIndexChecks:
    def test_no_index_at_all_is_reported_with_a_hint(self, tmp_path):
        _report(tmp_path, "1")
        report = verify.verify_folder(tmp_path)
        assert report.kinds()[verify.MISSING_INDEX] == 1
        assert "--reindex" in report.issues[0].detail

    def test_rows_the_index_never_learned_about(self, tmp_path):
        _clean_folder(tmp_path)
        _report(tmp_path, "3", urls=("https://late.example.com",))
        report = verify.verify_folder(tmp_path)
        assert report.kinds()[verify.UNINDEXED_ROWS] == 1
        assert "late.example.com" in next(
            issue.detail for issue in report.issues if issue.kind == verify.UNINDEXED_ROWS
        )

    def test_rows_whose_report_is_gone(self, tmp_path):
        _clean_folder(tmp_path)
        (tmp_path / "capture-report-20260101-000000.json").unlink()
        (tmp_path / "capture-report-20260101-000000.csv").unlink()
        report = verify.verify_folder(tmp_path)
        assert report.kinds()[verify.GHOST_ROWS] == 1

    def test_a_rebuilt_index_clears_the_row_problems(self, tmp_path):
        _clean_folder(tmp_path)
        (tmp_path / "capture-report-20260102-000000.json").unlink()
        (tmp_path / "capture-report-20260102-000000.csv").unlink()
        rebuild_index(tmp_path)
        assert verify.verify_folder(tmp_path).clean

    def test_duplicate_rows_in_the_index(self, tmp_path):
        _clean_folder(tmp_path)
        with HistoryStore(tmp_path / "history.sqlite3") as store:
            store.add_run("2026-01-03T00:00:00", [{"url": "https://a.example.com"}])
            store.add_run("2026-01-03T00:00:00", [{"url": "https://a.example.com"}])
        _report(tmp_path, "3")  # the report exists, so this is a pure duplicate
        assert verify.verify_folder(tmp_path).kinds()[verify.DUPLICATE_ROWS] == 1

    def test_a_corrupt_index_is_named_not_raised(self, tmp_path):
        _clean_folder(tmp_path)
        (tmp_path / "history.sqlite3").write_text("not a database", encoding="utf-8")
        report = verify.verify_folder(tmp_path)
        assert report.kinds()[verify.BROKEN_INDEX] == 1
        assert report.index_exists


class TestReferences:
    def test_an_orphan_reference_is_flagged(self, tmp_path):
        _clean_folder(tmp_path)
        (tmp_path / "latest_ghost_example_com.png").write_bytes(b"png")
        report = verify.verify_folder(tmp_path)
        assert report.kinds()[verify.ORPHAN_REFERENCE] == 1
        assert "latest_ghost_example_com.png" in report.issues[-1].detail

    def test_a_reference_of_a_known_site_is_fine(self, tmp_path):
        _clean_folder(tmp_path)
        (tmp_path / "latest_a_example_com.png").write_bytes(b"png")
        (tmp_path / "baseline_a_example_com.png").write_bytes(b"png")
        assert verify.verify_folder(tmp_path).clean


class TestVerifyCli:
    def test_a_clean_folder_exits_zero(self, tmp_path, capsys):
        _clean_folder(tmp_path)
        code = cli.main(["history", "--dir", str(tmp_path), "--verify"])
        out = capsys.readouterr().out
        assert code == 0
        assert "- clean." in out

    def test_problems_exit_one_with_the_fix_hint(self, tmp_path, capsys):
        _report(tmp_path, "1")
        code = cli.main(["history", "--dir", str(tmp_path), "--verify"])
        out = capsys.readouterr().out
        assert code == 1
        assert "missing_index:" in out
        assert "--reindex" in out

    def test_json_output_is_machine_readable(self, tmp_path, capsys):
        _report(tmp_path, "1")
        code = cli.main(["history", "--dir", str(tmp_path), "--verify", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 1
        assert payload["clean"] is False
        assert payload["issues"][0]["kind"] == verify.MISSING_INDEX

    def test_json_of_a_clean_folder_is_a_short_yes(self, tmp_path, capsys):
        _clean_folder(tmp_path)
        code = cli.main(["history", "--dir", str(tmp_path), "--verify", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0 and payload["clean"] is True and payload["issues"] == []

    def test_verify_does_not_print_the_timeline(self, tmp_path, capsys):
        _clean_folder(tmp_path)
        cli.main(["history", "--dir", str(tmp_path), "--verify"])
        assert "success" not in capsys.readouterr().out
