"""Two periods side by side: parsing the ranges, pairing the sites, the table."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from app.core import compare

NOW = datetime(2026, 10, 15, 12, 0)


def _folder(tmp_path, rows):
    """``rows`` is ``[(days_ago, label, diff, size)]`` -> a folder of reports."""
    for index, (days_ago, label, diff, size) in enumerate(rows):
        when = NOW - timedelta(days=days_ago)
        stamp = when.strftime("%Y%m%d-%H%M%S")
        shot = tmp_path / f"{label}_{stamp}_{index}.png"
        shot.write_bytes(b"x" * size)
        (tmp_path / f"capture-report-{stamp}-{index}.json").write_text(
            json.dumps(
                {
                    "generated_at": when.isoformat(timespec="seconds"),
                    "results": [
                        {
                            "url": f"https://{label.replace('_', '.')}",
                            "label": label,
                            "status": "success",
                            "diff": diff,
                            "file_path": str(shot),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
    return tmp_path


class TestPeriodParsing:
    """A range the user can type without reading a manual."""

    def test_a_simple_range(self):
        period = compare.parse_period("2026-09-01:2026-10-01", NOW)
        assert period.start == datetime(2026, 9, 1)
        assert period.end == datetime(2026, 10, 1)
        assert period.days == 30.0
        assert period.text() == "2026-09-01..2026-09-30 (30d)"

    def test_relative_days(self):
        period = compare.parse_period("30d:today", NOW)
        assert period.start == NOW - timedelta(days=30)
        assert period.end == NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        # "today" is midnight, so the window is a little under 30 days.
        assert period.days == 29.5

    def test_the_dash_less_ago_spelling(self):
        assert compare.parse_period("30d", NOW).start == NOW - timedelta(days=30)
        assert compare.parse_period("12h:now", NOW).start == NOW - timedelta(hours=12)
        assert compare.parse_period("45m", NOW).start == NOW - timedelta(minutes=45)

    def test_the_dash_spelling_also_works(self):
        assert compare.parse_period("-30d:today", NOW).start == NOW - timedelta(days=30)

    def test_an_open_side_means_everything(self):
        assert compare.parse_period(":today", NOW).start == datetime.min
        assert compare.parse_period("2026-01-01:", NOW).end == NOW

    def test_a_bare_value_runs_until_now(self):
        period = compare.parse_period("2026-09-01", NOW)
        assert period.start == datetime(2026, 9, 1)
        assert period.end == NOW

    def test_a_time_on_either_side_is_read(self):
        period = compare.parse_period("2026-09-01T09:30:2026-09-01T10:00", NOW)
        assert period.start == datetime(2026, 9, 1, 9, 30)
        assert period.end == datetime(2026, 9, 1, 10, 0)
        dotted = compare.parse_period("2026-09-01T09:30..2026-09-01T10:00", NOW)
        assert dotted.start == period.start and dotted.end == period.end

    @pytest.mark.parametrize("bad", ["soon:today", "-3x:today", "today:yesterday", "", ":"])
    def test_a_bad_range_is_named(self, bad):
        with pytest.raises(compare.PeriodError):
            compare.parse_period(bad, NOW)

    def test_contains_is_half_open(self):
        period = compare.Period(start=datetime(2026, 9, 1), end=datetime(2026, 10, 1))
        assert period.contains("2026-09-01T00:00:00")
        assert period.contains("2026-09-30T23:59:59")
        assert not period.contains("2026-10-01T00:00:00")
        assert not period.contains("not a date")

    def test_the_mirror_window_has_the_same_length(self):
        period = compare.parse_period("30d:today", NOW)
        mirrored = compare.mirror_period(period)
        assert mirrored.end == period.start
        assert mirrored.days == period.days
        assert mirrored.label == "the period before"

    def test_the_mirror_of_everything_is_the_empty_range(self):
        mirrored = compare.mirror_period(compare.parse_period(":today", NOW))
        assert mirrored.start == datetime.min
        assert mirrored.label == "everything before"

    def test_periods_serialise(self):
        data = compare.parse_period("2026-09-01:2026-10-01", NOW).to_dict()
        assert set(data) == {"label", "start", "end", "days"}
        assert data["days"] == 30.0


class TestComparison:
    """The table itself: totals, per-site pairing and the movers list."""

    def test_sites_are_paired_across_the_two_periods(self, tmp_path):
        _folder(
            tmp_path,
            [
                (40, "shop_example_com", 0.2, 100_000),
                (35, "shop_example_com", 0.2, 100_000),
                (10, "shop_example_com", 0.2, 100_000),
                (5, "news_example_com", None, 50_000),
            ],
        )
        first = compare.parse_period("60d:30d", NOW)
        second = compare.parse_period("30d:today", NOW)
        report = compare.compare_periods(tmp_path, first, second, NOW)
        by_site = {entry.site: entry for entry in report.sites}
        assert by_site["shop_example_com"].captures_a == 2
        assert by_site["shop_example_com"].captures_b == 1
        assert by_site["shop_example_com"].delta_captures == -1
        assert by_site["shop_example_com"].status() == "quieter"
        assert by_site["news_example_com"].status() == "new"
        assert by_site["news_example_com"].delta_bytes == 50_000

    def test_a_site_that_disappeared_is_gone(self, tmp_path):
        _folder(tmp_path, [(50, "old_example_com", 0.1, 10_000)])
        report = compare.compare_periods(
            tmp_path, compare.parse_period("60d:30d", NOW), compare.parse_period("30d", NOW), NOW
        )
        assert report.sites[0].status() == "gone"

    def test_a_steady_site_says_steady(self, tmp_path):
        _folder(tmp_path, [(40, "a_example_com", None, 1_000), (10, "a_example_com", None, 1_000)])
        report = compare.compare_periods(
            tmp_path, compare.parse_period("60d:30d", NOW), compare.parse_period("30d", NOW), NOW
        )
        assert report.sites[0].status() == "steady"

    def test_only_successful_captures_count(self, tmp_path):
        when = NOW - timedelta(days=5)
        stamp = when.strftime("%Y%m%d-%H%M%S")
        (tmp_path / f"capture-report-{stamp}.json").write_text(
            json.dumps(
                {
                    "generated_at": when.isoformat(timespec="seconds"),
                    "results": [
                        {"url": "https://a", "label": "a", "status": "failed", "diff": 0.5},
                        {"url": "https://b", "label": "b", "status": "success", "diff": None},
                    ],
                }
            ),
            encoding="utf-8",
        )
        report = compare.compare_periods(
            tmp_path, compare.parse_period("60d:30d", NOW), compare.parse_period("30d", NOW), NOW
        )
        assert [entry.site for entry in report.sites] == ["b"]

    def test_a_pruned_capture_counts_as_zero_bytes(self, tmp_path):
        _folder(tmp_path, [(5, "a_example_com", 0.3, 4_000)])
        for shot in tmp_path.glob("*.png"):
            shot.unlink()  # the retention rule was here
        report = compare.compare_periods(
            tmp_path, compare.parse_period("60d:30d", NOW), compare.parse_period("30d", NOW), NOW
        )
        assert report.sites[0].captures_b == 1
        assert report.sites[0].bytes_b == 0

    def test_the_totals_add_the_sites_up(self, tmp_path):
        _folder(
            tmp_path,
            [(10, "a_example_com", None, 1_000), (8, "b_example_com", 0.4, 2_000)],
        )
        report = compare.compare_periods(
            tmp_path, compare.parse_period("60d:30d", NOW), compare.parse_period("30d", NOW), NOW
        )
        assert report.totals.captures_b == 2
        assert report.totals.changes_b == 1
        assert report.totals.bytes_b == 3_000
        assert report.busy is True

    def test_an_empty_folder_is_not_busy(self, tmp_path):
        report = compare.compare_periods(
            tmp_path, compare.parse_period("60d:30d", NOW), compare.parse_period("30d", NOW), NOW
        )
        assert report.busy is False
        assert "no captures recorded in either period." in report.summary()

    def test_the_movers_are_the_biggest_change_first(self, tmp_path):
        _folder(
            tmp_path,
            [
                (40, "small_example_com", None, 1_000),
                (40, "big_example_com", None, 10_000),
                (40, "big_example_com", None, 10_000),
                (5, "small_example_com", None, 1_000),
            ],
        )
        report = compare.compare_periods(
            tmp_path, compare.parse_period("60d:30d", NOW), compare.parse_period("30d", NOW), NOW
        )
        assert [entry.site for entry in report.movers(limit=1)] == ["big_example_com"]

    def test_the_summary_has_the_numbers_a_reader_wants(self, tmp_path):
        _folder(
            tmp_path, [(40, "shop_example_com", 0.2, 100_000), (5, "shop_example_com", 0.4, 80_000)]
        )
        report = compare.compare_periods(
            tmp_path, compare.parse_period("60d:30d", NOW), compare.parse_period("30d", NOW), NOW
        )
        text = report.summary()
        assert "Comparing" in text and "with" in text
        assert "shop_example_com" in text
        assert "1 -> 1 (+0)" in text
        assert "quieter" in text
        assert "TOTAL" in text

    def test_the_summary_says_when_it_hid_rows(self, tmp_path):
        _folder(tmp_path, [(5, f"site{index}_example_com", None, 1_000) for index in range(15)])
        report = compare.compare_periods(
            tmp_path, compare.parse_period("60d:30d", NOW), compare.parse_period("30d", NOW), NOW
        )
        assert "more site(s); use --limit 0" in report.summary(limit=5)
        assert "more site(s)" not in report.summary(limit=0)

    def test_json_carries_both_periods_and_the_rows(self, tmp_path):
        _folder(tmp_path, [(5, "a_example_com", 0.4, 2_000)])
        report = compare.compare_periods(
            tmp_path, compare.parse_period("60d:30d", NOW), compare.parse_period("30d", NOW), NOW
        )
        data = report.to_dict()
        assert set(data) == {"periods", "totals", "sites", "site_count"}
        assert data["sites"][0]["captures"] == {"a": 0, "b": 1, "delta": 1}
        assert data["sites"][0]["changes"]["delta"] == 1
        assert data["periods"]["b"]["days"] == 30.0
        assert data["totals"]["captures"]["b"] == 1

    def test_a_limit_trims_the_json_but_keeps_the_count(self, tmp_path):
        _folder(tmp_path, [(5, f"site{index}_example_com", None, 1_000) for index in range(6)])
        report = compare.compare_periods(
            tmp_path, compare.parse_period("60d:30d", NOW), compare.parse_period("30d", NOW), NOW
        )
        data = report.to_dict(limit=2)
        assert len(data["sites"]) == 2
        assert data["site_count"] == 6


class TestComparePdf:
    """The one-page PDF is the same story for people who do not use a terminal."""

    def test_the_page_is_written(self, tmp_path):
        from app.core import pdfreport

        _folder(tmp_path, [(5, "a_example_com", 0.4, 2_000)])
        report = compare.compare_periods(
            tmp_path, compare.parse_period("60d:30d", NOW), compare.parse_period("30d", NOW), NOW
        )
        target = tmp_path / "nested" / "compare.pdf"
        written = pdfreport.write_compare_pdf(report, target)
        assert written == target
        assert target.read_bytes().startswith(b"%PDF")

    def test_an_empty_comparison_still_renders(self, tmp_path):
        from app.core import pdfreport

        report = compare.compare_periods(
            tmp_path, compare.parse_period("60d:30d", NOW), compare.parse_period("30d", NOW), NOW
        )
        target = tmp_path / "empty.pdf"
        pdfreport.write_compare_pdf(report, target)
        assert target.stat().st_size > 0
