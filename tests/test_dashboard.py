"""Tests for the static HTML trend dashboard (app.core.dashboard) and its CLI."""

from __future__ import annotations

import json

import pytest

from app import cli
from app.core.dashboard import build_dashboard


def _seed(directory, url="https://a.example.com"):
    (directory / "capture-report-20260101-000000.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-01-01T00:00:00",
                "results": [{"url": url, "status": "success", "diff": 0.0, "file_path": ""}],
            }
        ),
        encoding="utf-8",
    )
    (directory / "capture-report-20260102-000000.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-01-02T00:00:00",
                "results": [{"url": url, "status": "success", "diff": 0.6, "file_path": ""}],
            }
        ),
        encoding="utf-8",
    )


class TestDashboard:
    def test_dashboard_has_table_and_sparkline(self, tmp_path):
        _seed(tmp_path)
        out = build_dashboard(tmp_path, tmp_path / "dashboard.html")
        html_text = out.read_text(encoding="utf-8")
        assert "a.example.com" in html_text
        assert "<table" in html_text
        assert "<polyline" in html_text  # the SVG trend line
        assert "2" in html_text  # capture count

    def test_empty_history_message(self, tmp_path):
        out = build_dashboard(tmp_path / "none", tmp_path / "dashboard.html")
        assert "No history yet." in out.read_text(encoding="utf-8")

    def test_urls_are_html_escaped(self, tmp_path):
        _seed(tmp_path, url="https://a.example.com/?x=1&y=2")
        out = build_dashboard(tmp_path, tmp_path / "dashboard.html")
        html_text = out.read_text(encoding="utf-8")
        assert "&amp;" in html_text
        assert "x=1&y=2" not in html_text

    def test_creates_parent_directories(self, tmp_path):
        _seed(tmp_path)
        out = build_dashboard(tmp_path, tmp_path / "nested" / "dir" / "dash.html")
        assert out.exists()


class TestDashboardCli:
    def test_dashboard_subcommand_writes_file(self, tmp_path, capsys):
        _seed(tmp_path)
        target = tmp_path / "report.html"
        code = cli.main(["dashboard", "--dir", str(tmp_path), "--out", str(target)])
        assert code == 0
        assert target.exists()
        assert "Dashboard written" in capsys.readouterr().out

    def test_dashboard_subcommand_is_registered(self):
        assert "dashboard" in cli.SUBCOMMANDS


class TestBaselineColumn:
    def _seed(self, tmp_path, url="https://a.example.com"):
        import json

        (tmp_path / "capture-report-20260101-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "results": [{"url": url, "status": "success", "diff": 0.1, "file_path": ""}],
                }
            ),
            encoding="utf-8",
        )

    def test_no_baseline_shows_a_dash(self, tmp_path):
        from app.core.dashboard import build_dashboard

        self._seed(tmp_path)
        out = build_dashboard(tmp_path, tmp_path / "d.html")
        assert "<th>Baseline</th>" in out.read_text(encoding="utf-8")

    def test_pinned_baseline_is_shown(self, tmp_path):
        from app.core import baseline
        from app.core.dashboard import build_dashboard

        self._seed(tmp_path)
        baseline.latest_path(tmp_path, "https://a.example.com").write_bytes(b"png")
        baseline.pin_baseline(tmp_path, "https://a.example.com")
        out = build_dashboard(tmp_path, tmp_path / "d.html")
        assert "pinned" in out.read_text(encoding="utf-8")

    def test_stale_baseline_is_flagged(self, tmp_path):
        import os
        import time

        from app.core import baseline
        from app.core.dashboard import build_dashboard

        self._seed(tmp_path)
        baseline.latest_path(tmp_path, "https://a.example.com").write_bytes(b"png")
        pinned = baseline.pin_baseline(tmp_path, "https://a.example.com")
        when = time.time() - 60 * 86400
        os.utime(pinned, (when, when))
        out = build_dashboard(tmp_path, tmp_path / "d.html", baseline_max_age_days=7)
        text = out.read_text(encoding="utf-8")
        assert "stale" in text and "class='stale'" in text


class TestDriftColumn:
    def _seed(self, tmp_path, with_images=True, same=False):
        import json

        from app.core import baseline
        from tests.test_baseline import _gradient

        url = "https://a.example.com"
        (tmp_path / "capture-report-20260101-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "results": [{"url": url, "status": "success", "diff": 0.1, "file_path": ""}],
                }
            ),
            encoding="utf-8",
        )
        if with_images:
            _gradient(baseline.latest_path(tmp_path, url))
            baseline.pin_baseline(tmp_path, url)
            _gradient(baseline.latest_path(tmp_path, url), reverse=not same)
        return url

    def test_header_is_present(self, tmp_path):
        from app.core.dashboard import build_dashboard

        self._seed(tmp_path)
        out = build_dashboard(tmp_path, tmp_path / "d.html")
        assert "Drift vs baseline" in out.read_text(encoding="utf-8")

    def test_drift_bar_is_rendered(self, tmp_path):
        from app.core.dashboard import build_dashboard

        self._seed(tmp_path)  # a reversed gradient drifts a lot
        text = build_dashboard(tmp_path, tmp_path / "d.html").read_text(encoding="utf-8")
        assert "class='bar'" in text and "class='drift-num'" in text
        assert "width:0%" not in text

    def test_zero_drift_shows_a_zero_percent_bar(self, tmp_path):
        from app.core.dashboard import build_dashboard

        self._seed(tmp_path, same=True)
        text = build_dashboard(tmp_path, tmp_path / "d.html").read_text(encoding="utf-8")
        assert "width:0%" in text

    def test_without_images_the_cell_is_a_dash(self, tmp_path):
        from app.core.dashboard import build_dashboard

        self._seed(tmp_path, with_images=False)
        text = build_dashboard(tmp_path, tmp_path / "d.html").read_text(encoding="utf-8")
        assert "class='bar'" not in text


class TestDriftTrend:
    def _seed_with_drift_history(self, tmp_path):
        import json

        from app.core import baseline
        from tests.test_baseline import _gradient

        url = "https://a.example.com"
        for stamp, drift in (("2026-01-01T00:00:00", 0.1), ("2026-01-02T00:00:00", 0.4)):
            name = stamp.replace(":", "").replace("-", "").replace("T", "-")
            (tmp_path / f"capture-report-{name}.json").write_text(
                json.dumps(
                    {
                        "generated_at": stamp,
                        "results": [
                            {
                                "url": url,
                                "status": "success",
                                "diff": 0.2,
                                "drift": drift,
                                "file_path": "",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
        _gradient(baseline.latest_path(tmp_path, url))
        baseline.pin_baseline(tmp_path, url)
        _gradient(baseline.latest_path(tmp_path, url), reverse=True)
        return url

    def test_recorded_drift_is_charted(self, tmp_path):
        from app.core.dashboard import build_dashboard

        self._seed_with_drift_history(tmp_path)
        text = build_dashboard(tmp_path, tmp_path / "d.html").read_text(encoding="utf-8")
        # One sparkline for the diff series, one for the recorded drift series.
        assert text.count('<svg class="spark"') >= 2

    def test_single_measurement_has_no_drift_chart(self, tmp_path):
        import json

        from app.core import baseline
        from tests.test_baseline import _gradient

        url = "https://a.example.com"
        (tmp_path / "capture-report-20260101-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "results": [
                        {
                            "url": url,
                            "status": "success",
                            "diff": 0.2,
                            "drift": 0.3,
                            "file_path": "",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        _gradient(baseline.latest_path(tmp_path, url))
        baseline.pin_baseline(tmp_path, url)
        _gradient(baseline.latest_path(tmp_path, url), reverse=True)

        text = build_dashboard(tmp_path, tmp_path / "d.html").read_text(encoding="utf-8")
        assert text.count('<svg class="spark"') == 1  # only the diff trend


class TestSiteFilter:
    def _two_sites(self, tmp_path):
        """Two sites, each in its own report file (names must not collide)."""
        for index, url in enumerate(("https://shop.example.com", "https://news.example.com")):
            (tmp_path / f"capture-report-2026010{index + 1}-000000.json").write_text(
                json.dumps(
                    {
                        "generated_at": f"2026-01-0{index + 1}T00:00:00",
                        "results": [
                            {"url": url, "status": "success", "diff": 0.2, "file_path": ""}
                        ],
                    }
                ),
                encoding="utf-8",
            )
        return tmp_path

    def test_the_filter_drops_the_other_sites(self, tmp_path):
        from app.core.dashboard import render_dashboard

        page = render_dashboard(self._two_sites(tmp_path), url_filter="shop")
        assert "shop.example.com" in page
        assert "news.example.com" not in page

    def test_the_filter_is_case_insensitive(self, tmp_path):
        from app.core.dashboard import render_dashboard

        page = render_dashboard(self._two_sites(tmp_path), url_filter="SHOP")
        assert "shop.example.com" in page and "news.example.com" not in page

    def test_the_meta_line_says_what_is_filtered(self, tmp_path):
        from app.core.dashboard import render_dashboard

        page = render_dashboard(self._two_sites(tmp_path), url_filter="shop")
        assert "filtered by" in page and "shop" in page

    def test_a_filter_without_matches_says_so(self, tmp_path):
        from app.core.dashboard import render_dashboard

        page = render_dashboard(self._two_sites(tmp_path), url_filter="nothing-here")
        assert "No site matches" in page

    def test_an_empty_folder_still_says_no_history(self, tmp_path):
        from app.core.dashboard import render_dashboard

        page = render_dashboard(tmp_path / "missing")
        assert "No history yet." in page and "No site matches" not in page

    def test_build_dashboard_passes_the_filter_through(self, tmp_path):
        from app.core.dashboard import build_dashboard as build

        out = build(self._two_sites(tmp_path), tmp_path / "shop.html", url_filter="news")
        text = out.read_text(encoding="utf-8")
        assert "news.example.com" in text and "shop.example.com" not in text

    def test_the_parser_takes_the_flag(self):
        args = cli.build_dashboard_parser().parse_args(["--dir", "x", "--url", "shop"])
        assert args.url == "shop"

    def test_the_cli_url_flag_writes_a_filtered_page(self, tmp_path, capsys):
        self._two_sites(tmp_path)
        out = tmp_path / "cli.html"
        code = cli.main(["dashboard", "--dir", str(tmp_path), "--out", str(out), "--url", "shop"])
        assert code == 0
        text = out.read_text(encoding="utf-8")
        assert "shop.example.com" in text and "news.example.com" not in text


class TestSearchBox:
    def test_the_box_comes_with_a_table(self, tmp_path):
        from app.core.dashboard import render_dashboard

        _seed(tmp_path, "https://a.example.com")
        page = render_dashboard(tmp_path)
        assert 'id="site-search"' in page
        assert 'type="search"' in page
        assert 'id="match-count"' in page

    def test_the_script_filters_the_table_rows(self, tmp_path):
        from app.core.dashboard import render_dashboard

        _seed(tmp_path, "https://a.example.com")
        page = render_dashboard(tmp_path)
        assert "querySelectorAll('tbody tr')" in page.replace('"', "'")
        assert "addEventListener('input'" in page.replace('"', "'")
        assert "row.hidden = !hit" in page

    def test_an_empty_table_has_no_search_box(self, tmp_path):
        from app.core.dashboard import render_dashboard

        assert 'id="site-search"' not in render_dashboard(tmp_path / "missing")

    def test_the_script_needs_no_network(self, tmp_path):
        from app.core.dashboard import render_dashboard

        _seed(tmp_path, "https://a.example.com")
        page = render_dashboard(tmp_path)
        assert "http://" not in page.split("<script>")[1]
        assert "src=" not in page


class TestStoragePanel:
    """The Space panel: what the folder costs and which cap keeps it in check."""

    def test_bytes_read_like_a_person_wrote_them(self):
        from app.core.dashboard import human_bytes

        assert human_bytes(0) == "0 B"
        assert human_bytes(999) == "999 B"
        assert human_bytes(1536) == "1.5 KB"
        assert human_bytes(5 * 1024 * 1024) == "5.0 MB"
        assert human_bytes(2 * 1024**3) == "2.0 GB"
        assert human_bytes(-5) == "0 B"  # a nonsense size never renders as "-5 B"

    def test_the_measurements_split_screenshots_from_history(self, tmp_path):
        from app.core import retention
        from app.core.dashboard import storage_stats

        _seed(tmp_path)
        (tmp_path / "001_a.example.com.png").write_bytes(b"p" * 4096)
        (tmp_path / "history.sqlite3").write_bytes(b"i" * 512)

        stats = storage_stats(tmp_path, {"screenshots_mb": 500, "history_mb": 0})
        reports = sum(path.stat().st_size for path in retention.report_files(tmp_path))
        assert stats["screenshots"] == 4096
        assert stats["index"] == 512
        assert stats["history"] == reports + 512  # the reports plus the index
        assert stats["total"] == stats["screenshots"] + stats["history"]
        assert stats["reports"] == 2
        assert stats["oldest"] == "2026-01-01 00:00"  # read from the file name
        assert stats["caps"] == {"screenshots_mb": 500}  # a 0 cap is off, not shown

    def test_the_panel_shows_sizes_dates_and_the_active_caps(self, tmp_path):
        from app.core.dashboard import render_dashboard

        _seed(tmp_path)
        page = render_dashboard(tmp_path, caps={"screenshots_mb": 500, "history_mb": 50})
        assert "<h2>Storage</h2>" in page
        assert "<dt>Screenshots</dt>" in page
        assert "<dt>Oldest run</dt><dd>2026-01-01 00:00</dd>" in page
        assert "history 50 MB, screenshots 500 MB" in page

    def test_without_caps_the_row_is_simply_absent(self, tmp_path):
        from app.core.dashboard import render_dashboard

        _seed(tmp_path)
        assert "<dt>Caps</dt>" not in render_dashboard(tmp_path)

    def test_an_empty_folder_reads_as_zero(self, tmp_path):
        from app.core.dashboard import render_dashboard

        page = render_dashboard(tmp_path / "missing")
        assert "0 B" in page
        assert "0 report file(s)" in page
        assert "Oldest run" not in page  # there is no oldest run yet

    def test_a_renamed_report_falls_back_to_the_file_date(self, tmp_path):
        from app.core.dashboard import storage_stats

        (tmp_path / "capture-report-mystery.json").write_text("{}", encoding="utf-8")
        oldest = storage_stats(tmp_path)["oldest"]
        assert len(oldest) == 16 and oldest[4] == "-" and oldest[10] == " "  # "YYYY-MM-DD HH:MM"


class TestDashboardCapsCli:
    """``dashboard --caps`` labels the panel; the numbers themselves are measured."""

    def test_the_caps_reach_the_page(self, tmp_path, capsys):
        out = tmp_path / "dash.html"
        _seed(tmp_path)
        code = cli.main(
            [
                "dashboard",
                "--dir",
                str(tmp_path),
                "--out",
                str(out),
                "--caps",
                "500,50",
            ]
        )
        assert code == 0
        assert "history 50 MB, screenshots 500 MB" in out.read_text(encoding="utf-8")
        assert "Dashboard written to" in capsys.readouterr().out

    def test_one_number_covers_the_screenshots_only(self, tmp_path):
        out = tmp_path / "dash.html"
        _seed(tmp_path)
        assert (
            cli.main(["dashboard", "--dir", str(tmp_path), "--out", str(out), "--caps", "500"]) == 0
        )
        page = out.read_text(encoding="utf-8")
        assert "Caps</dt><dd>screenshots 500 MB" in page
        assert "history" not in page.split("<dt>Caps</dt>")[1].split("</dd>")[0]

    def test_a_word_instead_of_a_number_is_refused(self, tmp_path, capsys):
        code = cli.main(
            [
                "dashboard",
                "--dir",
                str(tmp_path),
                "--out",
                str(tmp_path / "d.html"),
                "--caps",
                "big",
            ]
        )
        assert code == 2
        assert "--caps expects MB numbers" in capsys.readouterr().err


class TestStoragePanelForecast:
    """The Storage panel answers "how long until the cap bites?"."""

    def _runs(self, folder, count=6, kb=60, hours=12):
        """``count`` valid runs, ``hours`` apart, each a JSON + CSV report."""
        from datetime import datetime, timedelta

        start = datetime.now() - timedelta(hours=hours * count)
        for index in range(count):
            stamp = (start + timedelta(hours=hours * index)).strftime("%Y%m%d-%H%M%S")
            payload = {"generated_at": stamp, "results": [], "padding": "x" * (kb * 1024)}
            (folder / f"capture-report-{stamp}.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            (folder / f"capture-report-{stamp}.csv").write_text("y" * (kb * 1024), encoding="utf-8")

    def test_the_panel_shows_the_growth_and_the_forecast(self, tmp_path):
        from app.core.dashboard import render_dashboard

        self._runs(tmp_path)
        page = render_dashboard(tmp_path, caps={"history_mb": 100})
        assert "<dt>Growth</dt>" in page
        assert "<dt>Forecast</dt><dd>~" in page
        assert "day(s) until the history cap" in page

    def test_a_cap_in_sight_warns(self, tmp_path):
        from app.core.dashboard import render_dashboard

        self._runs(tmp_path)  # ~720 KB of reports, growing every 12 h
        page = render_dashboard(tmp_path, caps={"history_mb": 1})
        assert "Cap in sight" in page
        assert "raise it or shorten the retention window" in page

    def test_a_cap_far_away_does_not_warn(self, tmp_path):
        from app.core.dashboard import render_dashboard

        self._runs(tmp_path)
        page = render_dashboard(tmp_path, caps={"history_mb": 500})
        assert "Cap in sight" not in page
        assert "day(s) until the history cap" in page

    def test_a_reached_cap_is_reported_as_such(self, tmp_path):
        from app.core.dashboard import render_dashboard

        self._runs(tmp_path, count=12)  # ~1.4 MB of reports against a 1 MB cap
        page = render_dashboard(tmp_path, caps={"history_mb": 1})
        assert "<dt>Forecast</dt><dd>history cap already reached</dd>" in page
        assert "The history cap is already reached" in page

    def test_no_caps_means_no_forecast_row(self, tmp_path):
        from app.core.dashboard import render_dashboard

        self._runs(tmp_path)
        page = render_dashboard(tmp_path)
        assert "<dt>Forecast</dt>" not in page
        assert "<dt>Growth</dt>" in page

    def test_archives_are_listed_when_there_are_any(self, tmp_path):
        from app.core.dashboard import render_dashboard

        self._runs(tmp_path)
        assert "<dt>Archives</dt>" not in render_dashboard(tmp_path)
        (tmp_path / "archive-2026-01.zip").write_bytes(b"z" * 4096)
        page = render_dashboard(tmp_path)
        assert "<dt>Archives</dt><dd>1 zip(s), 4.0 KB</dd>" in page

    def test_the_download_links_need_the_server(self, tmp_path):
        from app.core.dashboard import render_dashboard

        self._runs(tmp_path)
        assert "/api/storage" not in render_dashboard(tmp_path)
        page = render_dashboard(tmp_path, downloads=True)
        assert "/api/storage?format=csv" in page and "/api/storage?format=json" in page


class TestStorageExport:
    """:func:`storage_export` is what ``/api/storage`` and the CLI share."""

    def _runs(self, folder, count=6, kb=60, hours=12):
        TestStoragePanelForecast._runs(self, folder, count, kb, hours)

    def test_json_export_carries_the_numbers(self, tmp_path):
        from app.core.dashboard import storage_export

        self._runs(tmp_path)
        body, content_type, filename = storage_export(tmp_path, {"history_mb": 100}, "json")
        payload = json.loads(body)
        assert content_type.startswith("application/json") and filename == "storage.json"
        assert payload["reports"] == 12
        assert payload["history"] > 0
        assert payload["caps"] == {"history_mb": 100}
        assert payload["forecasts"]["history"] is not None
        assert payload["days_to_cap"] == payload["forecasts"]["history"]
        assert payload["growth"]["history_bytes_per_day"] > 0

    def test_csv_export_is_one_row_per_number(self, tmp_path):
        from app.core.dashboard import storage_export

        self._runs(tmp_path)
        body, content_type, filename = storage_export(tmp_path, {"history_mb": 100}, "csv")
        assert content_type.startswith("text/csv") and filename == "storage.csv"
        lines = body.decode("utf-8").splitlines()
        assert lines[0] == "key,value"
        keys = [line.split(",")[0] for line in lines[1:]]
        assert keys[:5] == [
            "total_bytes",
            "screenshot_bytes",
            "history_bytes",
            "index_bytes",
            "reports",
        ]
        assert "days_to_history_cap" in keys and "days_to_cap" in keys
        assert "reports,12" in lines

    def test_an_unknown_forecast_is_an_empty_value(self, tmp_path):
        from app.core.dashboard import storage_export

        self._runs(tmp_path, count=1, kb=10)  # one run: no rate, so no forecast
        body, _content_type, _filename = storage_export(tmp_path, {"history_mb": 100}, "csv")
        rows = dict(
            line.split(",", 1) for line in body.decode("utf-8").splitlines()[1:] if "," in line
        )
        assert rows["days_to_history_cap"] == ""
        assert rows["days_to_cap"] == ""
        assert rows["history_bytes_per_day"] == "0.0"

    def test_a_bad_format_is_refused(self, tmp_path):
        from app.core.dashboard import storage_export

        with pytest.raises(ValueError, match="format must be one of"):
            storage_export(tmp_path, None, "xml")

    def test_the_format_is_case_insensitive(self, tmp_path):
        from app.core.dashboard import storage_export

        assert storage_export(tmp_path, None, "CSV")[2] == "storage.csv"


class TestTopSites:
    """The Storage panel and the exports name the hosts that cost the most."""

    @staticmethod
    def _captures(folder, label, size, stamp="20260101-000000", index="001"):
        for name, count in (
            (f"{index}_{label}_{stamp}.png", size),
            (f"latest_{label}.png", 5),
        ):
            (folder / name).write_bytes(b"x" * count)

    @staticmethod
    def _page(folder):
        html = folder / "dashboard.html"
        build_dashboard(folder, html)
        return html.read_text(encoding="utf-8")

    def test_the_panel_names_the_biggest_sites(self, tmp_path):
        self._captures(tmp_path, "big_example_com", 2000)
        self._captures(tmp_path, "small_example_com", 10)
        row = self._page(tmp_path).split("<dt>Top sites</dt><dd>")[1].split("</dd>")[0]
        assert row.startswith("big_example_com ")
        assert "small_example_com" in row

    def test_the_panel_omits_the_row_when_there_is_nothing_to_measure(self, tmp_path):
        assert "<dt>Top sites</dt>" not in self._page(tmp_path)

    def test_storage_stats_carries_the_breakdown(self, tmp_path):
        from app.core.dashboard import storage_stats

        self._captures(tmp_path, "big_example_com", 2000)
        sites = storage_stats(tmp_path)["sites"]
        assert sites[0]["label"] == "big_example_com"
        assert sites[0]["bytes"] == 2005
        assert sites[0]["references"] == 1

    def test_the_breakdown_is_capped(self, tmp_path):
        from app.core import dashboard as dashboard_module
        from app.core.dashboard import storage_stats

        for index in range(dashboard_module.TOP_SITES + 2):
            self._captures(tmp_path, f"site{index}_example_com", 10 + index, index=f"{index:03d}")
        assert len(storage_stats(tmp_path)["sites"]) == dashboard_module.TOP_SITES

    def test_the_csv_export_flattens_three_sites(self, tmp_path):
        from app.core.dashboard import storage_export

        for index in range(4):
            self._captures(tmp_path, f"site{index}_example_com", 100 + index, index=f"{index:03d}")
        body, _content_type, _filename = storage_export(tmp_path, None, "csv")
        rows = dict(
            line.split(",", 1) for line in body.decode("utf-8").splitlines()[1:] if "," in line
        )
        assert rows["site_1_label"] == "site3_example_com"  # biggest first
        assert rows["site_1_bytes"] == "108"  # 103 bytes + its 5-byte reference
        assert rows["site_1_files"] == "2"
        assert "site_4_label" not in rows  # only three fit, by design

    def test_the_json_export_carries_the_sites(self, tmp_path):
        from app.core.dashboard import storage_export

        self._captures(tmp_path, "big_example_com", 2000)
        body, _content_type, _filename = storage_export(tmp_path, None, "json")
        assert json.loads(body.decode("utf-8"))["sites"][0]["label"] == "big_example_com"


class TestMeasuredGrowth:
    """Recorded samples beat estimated run sizes - when they exist."""

    @staticmethod
    def _seed_samples(folder, step=20_000, days=5, shrink_last=False):
        from datetime import datetime, timedelta

        from app.core.store import HistoryStore

        base = datetime.now() - timedelta(days=days - 1)
        with HistoryStore(folder / "history.sqlite3") as store:
            for index in range(days):
                history = 100_000 + step * index
                if shrink_last and index == days - 1:
                    history -= 4 * step
                store.add_storage_sample(
                    (base + timedelta(days=index)).isoformat(timespec="seconds"),
                    {
                        "total": 600_000 + step * 3 * index,
                        "screenshots": 500_000 + step * 2 * index,
                        "history": history,
                        "index": 4096,
                        "reports": index + 1,
                    },
                )

    def test_the_fitted_growth_replaces_the_estimate(self, tmp_path):
        from app.core.dashboard import storage_stats

        self._seed_samples(tmp_path)
        growth = storage_stats(tmp_path)["growth"]
        assert growth["source"] == "samples"
        assert growth["samples"] == 5
        assert growth["history_bytes_per_day"] == pytest.approx(20_000.0)
        assert growth["screenshots_bytes_per_day"] == pytest.approx(40_000.0)
        assert growth["total_bytes_per_day"] == pytest.approx(60_000.0)

    def test_without_samples_the_runs_are_used(self, tmp_path):
        _seed(tmp_path)
        growth = storage_stats_import()(tmp_path)["growth"]
        assert growth["source"] == "reports"
        assert growth["samples"] == 0

    def test_the_forecast_uses_the_fitted_number(self, tmp_path):
        from app.core.dashboard import storage_stats

        self._seed_samples(tmp_path)
        stats = storage_stats(tmp_path, {"history_mb": 1})
        # 1 MiB is ~52 days away at 20 KB/day from a ~180 KB history.
        assert stats["forecasts"]["history"] == pytest.approx(
            (1024 * 1024 - stats["history"]) / 20_000.0, rel=0.01
        )

    def test_the_panel_shows_the_samples_and_the_chart(self, tmp_path):
        self._seed_samples(tmp_path)
        page = folder_page(tmp_path)
        assert "<dt>Samples</dt><dd>5 since " in page
        assert "<dt>Bytes/day</dt><dd><svg" in page
        assert "aria-label=" in page.split("<dt>Bytes/day</dt>")[1]

    def test_a_day_that_freed_space_is_drawn_below_the_axis(self, tmp_path):
        self._seed_samples(tmp_path, shrink_last=True)
        page = folder_page(tmp_path)
        chart = page.split("<dt>Bytes/day</dt>")[1].split("</dd>")[0]
        assert "#e26464" in chart  # the negative bar
        assert "height=" in chart

    def test_a_corrupt_index_does_not_break_the_panel(self, tmp_path):
        _seed(tmp_path)
        (tmp_path / "history.sqlite3").write_text("not a database", encoding="utf-8")
        from app.core.dashboard import storage_samples

        assert storage_samples(tmp_path) == []
        assert "Storage" in folder_page(tmp_path)

    def test_reading_samples_without_an_index_is_empty(self, tmp_path):
        from app.core.dashboard import storage_samples

        assert storage_samples(tmp_path) == []


def storage_stats_import():
    from app.core.dashboard import storage_stats

    return storage_stats


def folder_page(folder):
    """The rendered dashboard HTML for a folder."""
    html = folder / "dashboard.html"
    build_dashboard(folder, html)
    return html.read_text(encoding="utf-8")


class TestStorageSeriesExport:
    """``storage_export(..., series=True)`` is what ``/api/storage?series=1`` serves."""

    def _seed_samples(self, folder, days=4, step=25_000):
        from datetime import datetime, timedelta

        from app.core.store import HistoryStore

        base = datetime.now() - timedelta(days=days - 1)
        with HistoryStore(folder / "history.sqlite3") as store:
            for index in range(days):
                store.add_storage_sample(
                    (base + timedelta(days=index)).isoformat(timespec="seconds"),
                    {
                        "total": 500_000 + step * 2 * index,
                        "screenshots": 400_000 + step * index,
                        "history": 100_000 + step * index,
                        "index": 4096,
                        "reports": index + 1,
                    },
                )

    def test_the_series_is_opt_in(self, tmp_path):
        from app.core.dashboard import storage_export

        self._seed_samples(tmp_path)
        body, _content_type, _filename = storage_export(tmp_path, None, "json")
        payload = json.loads(body.decode("utf-8"))
        assert "series" not in payload
        assert payload["growth"]["source"] == "samples"

    def test_the_series_and_its_days_travel_together(self, tmp_path):
        from app.core.dashboard import storage_export

        self._seed_samples(tmp_path)
        body, _content_type, _filename = storage_export(tmp_path, None, "json", series=True)
        payload = json.loads(body.decode("utf-8"))
        assert len(payload["series"]) == 4
        assert payload["growth_by_day"] == [
            {"day": row["taken_at"][:10], "bytes": 25_000} for row in payload["series"][1:]
        ]

    def test_a_window_trims_the_series_only(self, tmp_path):
        from app.core.dashboard import storage_export

        self._seed_samples(tmp_path)
        body, _content_type, _filename = storage_export(
            tmp_path, None, "json", series=True, days=2, limit=1
        )
        payload = json.loads(body.decode("utf-8"))
        assert len(payload["series"]) == 1
        assert payload["growth"]["samples"] == 4
        assert payload["growth"]["history_bytes_per_day"] == 25_000.0

    def test_a_csv_series_is_refused(self, tmp_path):
        from app.core.dashboard import storage_export

        self._seed_samples(tmp_path)
        with pytest.raises(ValueError, match="series is only available as JSON"):
            storage_export(tmp_path, None, "csv", series=True)

    def test_the_csv_rows_name_the_source(self, tmp_path):
        from app.core.dashboard import storage_export

        self._seed_samples(tmp_path)
        rows = dict(
            line.split(",", 1)
            for line in storage_export(tmp_path, None, "csv")[0].decode("utf-8").splitlines()[1:]
            if "," in line
        )
        assert rows["growth_source"] == "samples"
        assert rows["samples"] == "4"
        assert rows["total_bytes_per_day"] == "50000.0"


class TestSiteCapsInTheStoragePanel:
    """A per-site budget decorates the metrics, not the numeric caps of the panel."""

    def test_a_per_site_budget_is_kept_apart(self, tmp_path):
        from app.core.dashboard import storage_stats

        stats = storage_stats(
            tmp_path, {"screenshots_mb": 500, "site_caps": "news.example.com=200,*=1000"}
        )
        assert stats["caps"] == {"screenshots_mb": 500}
        assert stats["site_caps"] == {"news.example.com": 200.0, "*": 1000.0}

    def test_rubbish_in_site_caps_is_ignored(self, tmp_path):
        from app.core.dashboard import storage_stats

        stats = storage_stats(tmp_path, {"screenshots_mb": 500, "site_caps": "news=huge"})
        assert stats["caps"] == {"screenshots_mb": 500}
        assert stats["site_caps"] == {}

    def test_the_panel_still_renders_with_a_budget(self, tmp_path):
        from app.core.dashboard import render_dashboard

        page = render_dashboard(tmp_path, caps={"screenshots_mb": 500, "site_caps": "*=1000"})
        assert "screenshots 500 MB" in page
        assert "site_caps" not in page
