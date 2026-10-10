"""Engine tests driven by the fake Playwright implementation.

These exercise the real :class:`app.core.engine.CaptureEngine` code path -
navigation, waiting, login, lazy scrolling, measurement, stitching, file
naming, retries, cancellation and error containment.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from app.core.engine import (
    BrowserNotInstalledError,
    CaptureEngine,
    CaptureStatus,
    LogLevel,
)
from app.core.settings import AUTH_MODE_FORM, AUTH_MODE_HTTP, SEGMENT_PX, CaptureSettings
from tests.fakes import CallLog, FakeTimeoutError, PageScript, make_factory

FILENAME_RE = re.compile(r"^\d{3}_[a-z0-9_-]+\.png$")


def run_engine(
    settings: CaptureSettings, urls, script=None, calls=None, launch_error=None, **kwargs
):
    calls = calls or CallLog()
    engine = CaptureEngine(
        settings,
        playwright_factory=make_factory(script, calls, launch_error),
        **kwargs,
    )
    summary = engine.run(urls)
    return engine, summary, calls


class TestHappyPath:
    def test_one_url_produces_one_png(self, settings, tmp_output):
        script = PageScript(page_width=1200, page_height=2400)
        engine, summary, calls = run_engine(settings, ["https://example.com"], script)

        assert summary.total == 1
        assert summary.succeeded == 1
        assert summary.failed == 0
        assert summary.elapsed_ms >= 0

        files = sorted(tmp_output.glob("*.png"))
        assert len(files) == 1
        assert FILENAME_RE.match(files[0].name), files[0].name
        assert files[0].stat().st_size > 0

        with Image.open(files[0]) as image:
            assert image.size == (1200, 2400)

        assert summary.results[0].page_height_px == 2400
        assert summary.results[0].http_status == 200
        assert summary.results[0].file_path == str(files[0])
        assert engine.log_lines

    def test_navigation_waits_for_domcontentloaded_then_networkidle(self, settings):
        calls = CallLog()
        settings.network_idle_timeout_ms = 500
        _, _, calls = run_engine(settings, ["https://example.com"], PageScript(), calls)

        assert calls.gotos[0]["wait_until"] == "domcontentloaded"
        assert calls.gotos[0]["timeout"] == settings.navigation_timeout_ms
        assert ("networkidle", 500) in calls.wait_states

    def test_multiple_urls_keep_a_stable_order_and_unique_names(self, settings, tmp_output):
        urls = ["https://a.example.com", "https://b.example.com/x", "https://a.example.com/y"]
        _, summary, _ = run_engine(settings, urls, PageScript(page_height=800))

        assert [r.index for r in summary.results] == [1, 2, 3]
        assert [r.status for r in summary.results] == [CaptureStatus.SUCCESS] * 3

        names = [Path(r.file_path).name for r in summary.results]
        assert len(set(names)) == 3
        assert names[0].startswith("001_")
        assert names[2].startswith("003_")

    def test_creates_a_missing_destination_folder(self, tmp_path):
        settings = CaptureSettings(
            output_dir=str(tmp_path / "nested" / "deeper"),
            settle_delay_ms=0,
            network_idle_timeout_ms=0,
            retries=0,
            write_log_file=False,
            device_scale_factor=1.0,
        )
        _, summary, _ = run_engine(settings, ["https://example.com"], PageScript())
        assert summary.succeeded == 1
        assert Path(summary.results[0].file_path).exists()

    def test_log_file_is_written_when_enabled(self, settings, tmp_output):
        settings.write_log_file = True
        run_engine(settings, ["https://example.com"], PageScript())
        logs = list(tmp_output.glob("capture-log-*.txt"))
        assert len(logs) == 1
        assert "Starting batch" in logs[0].read_text(encoding="utf-8")


class TestContextConfiguration:
    def test_viewport_and_scale_factor_reach_the_browser(self, settings):
        settings.viewport_width = 1366
        settings.viewport_height = 900
        settings.device_scale_factor = 2.0
        settings.user_agent = "TestAgent/1.0"
        settings.locale = "en-GB"
        settings.timezone = "Europe/London"

        _, _, calls = run_engine(settings, ["https://example.com"], PageScript())
        context = calls.contexts[0]

        assert context["viewport"] == {"width": 1366, "height": 900}
        assert context["device_scale_factor"] == 2.0
        assert context["user_agent"] == "TestAgent/1.0"
        assert context["locale"] == "en-GB"
        assert context["timezone_id"] == "Europe/London"
        assert context["ignore_https_errors"] is True

    def test_headless_flag_and_browser_choice(self, settings):
        settings.headless = False
        settings.browser = "firefox"
        _, _, calls = run_engine(settings, ["https://example.com"], PageScript())
        assert calls.launched == [
            {
                "browser": "firefox",
                "headless": False,
                "args": calls.launched[0]["args"],
                "proxy": None,
            }
        ]
        assert "--force-color-profile=srgb" in calls.launched[0]["args"]

    def test_screenshot_requests_full_page_png(self, settings):
        _, _, calls = run_engine(settings, ["https://example.com"], PageScript())
        shot = calls.screenshots[-1]
        assert shot["full_page"] is True
        assert shot["type"] == "png"
        assert shot["animations"] == "disabled"

    def test_jpeg_output_uses_quality(self, settings):
        settings.image_format = "jpeg"
        settings.jpeg_quality = 77
        _, summary, calls = run_engine(settings, ["https://example.com"], PageScript())
        assert calls.screenshots[-1]["type"] == "jpeg"
        assert calls.screenshots[-1]["quality"] == 77
        assert Path(summary.results[0].file_path).suffix == ".jpg"


class TestResilience:
    def test_networkidle_timeout_is_not_fatal(self, settings):
        settings.network_idle_timeout_ms = 100
        script = PageScript(networkidle_error=FakeTimeoutError("Timeout 100ms exceeded"))
        _, summary, _ = run_engine(settings, ["https://example.com"], script)
        assert summary.succeeded == 1

    def test_navigation_timeout_retries_with_commit(self, settings):
        script = PageScript(goto_timeout_first_attempt=True)
        _, summary, calls = run_engine(settings, ["https://example.com"], script)

        assert [g["wait_until"] for g in calls.gotos] == ["domcontentloaded", "commit"]
        assert summary.succeeded == 1

    def test_one_bad_url_does_not_stop_the_batch(self, settings, tmp_output):
        script = PageScript(goto_error=FakeTimeoutError("Timeout 60000ms exceeded"))
        urls = ["https://broken.example.com", "https://ok.example.com"]

        _, summary, _ = run_engine(settings, urls, script)

        assert summary.total == 2
        assert summary.failed == 2  # the fake fails every goto
        assert all("Timed out" in r.message or "Timeout" in r.message for r in summary.results)

    def test_second_url_is_attempted_after_the_first_fails(self, settings):
        calls = CallLog()
        script = PageScript(fail_first_n_gotos=1)
        settings.retries = 0
        _, summary, calls = run_engine(
            settings, ["https://bad.example.com", "https://good.example.com"], script, calls
        )

        assert len(calls.gotos) == 2
        assert summary.results[0].status is CaptureStatus.FAILED
        assert summary.results[1].status is CaptureStatus.SUCCESS

    def test_retry_succeeds_on_second_attempt(self, settings):
        settings.retries = 1
        script = PageScript(fail_first_n_gotos=1)
        _, summary, calls = run_engine(settings, ["https://example.com"], script)

        assert len(calls.gotos) == 2
        assert summary.succeeded == 1
        assert summary.results[0].attempts == 2

    def test_invalid_url_fails_without_touching_the_browser(self, settings):
        _, summary, calls = run_engine(settings, ["not a url"], PageScript())
        assert summary.failed == 1
        assert "Invalid URL" in summary.results[0].message
        assert calls.gotos == []
        assert calls.contexts == []

    def test_http_error_status_is_reported_but_captured(self, settings):
        script = PageScript(http_status=503)
        _, summary, _ = run_engine(settings, ["https://example.com"], script)
        assert summary.succeeded == 1
        assert summary.results[0].http_status == 503

    def test_http_error_can_abort_when_configured(self, settings):
        settings.continue_on_http_error = False
        script = PageScript(http_status=404)
        _, summary, _ = run_engine(settings, ["https://example.com"], script)
        assert summary.failed == 1
        assert "404" in summary.results[0].message

    def test_browser_not_installed_raises_a_helpful_error(self, settings):
        error = Exception("Executable doesn't exist at /root/.cache/ms-playwright/chromium/chrome")
        with pytest.raises(BrowserNotInstalledError) as info:
            run_engine(settings, ["https://example.com"], PageScript(), launch_error=error)
        assert "playwright install chromium" in str(info.value)

    def test_missing_system_dependencies_are_explained(self, settings):
        error = Exception("Host system is missing dependencies to run browsers")
        engine = CaptureEngine(settings, playwright_factory=make_factory(PageScript(), None, error))
        summary = engine.run(["https://example.com"])
        assert summary.total == 0
        assert any("install-deps" in line for line in engine.log_lines)

    def test_browser_and_context_are_closed(self, settings):
        _, _, calls = run_engine(settings, ["https://example.com"], PageScript())
        assert calls.browser_closed is True
        assert calls.closed_contexts == 1
        assert calls.closed_pages == 1

    def test_empty_url_list_is_a_no_op(self, settings):
        engine, summary, calls = run_engine(settings, [], PageScript())
        assert summary.total == 0
        assert calls.gotos == []
        assert any("No URLs" in line for line in engine.log_lines)

    def test_stop_on_first_error_aborts_the_rest(self, settings):
        settings.stop_on_first_error = True
        script = PageScript(goto_error=RuntimeError("boom"))
        _, summary, _ = run_engine(settings, ["https://a.com", "https://b.com"], script)
        assert summary.failed == 1
        assert summary.skipped == 1
        assert summary.stopped_by_user is True


class TestCancellation:
    def test_stop_marks_remaining_urls_as_skipped(self, settings):
        engine = CaptureEngine(
            settings,
            playwright_factory=make_factory(PageScript(), CallLog()),
        )
        seen = []

        def on_result(result):
            seen.append(result)
            engine.request_stop()  # stop as soon as the first page is done

        engine._result_cb = on_result  # noqa: SLF001 - deliberate white-box test
        summary = engine.run(["https://a.com", "https://b.com", "https://c.com"])

        assert summary.results[0].status is CaptureStatus.SUCCESS
        assert summary.results[1].status is CaptureStatus.SKIPPED
        assert summary.results[2].status is CaptureStatus.SKIPPED
        assert summary.stopped_by_user is True

    def test_interruptible_sleep_exits_early(self, settings):
        engine = CaptureEngine(settings, playwright_factory=make_factory(PageScript()))
        engine.request_stop()
        import time

        started = time.monotonic()
        engine._interruptible_sleep(5_000)  # noqa: SLF001
        assert time.monotonic() - started < 0.5


class TestAuthentication:
    def test_form_login_fills_and_submits(self, settings):
        settings.auth_enabled = True
        settings.auth_mode = AUTH_MODE_FORM
        settings.username = "alice"
        settings.password = "s3cret"
        settings.login_wait_ms = 0

        script = PageScript(has_login_form=True)
        _, summary, calls = run_engine(settings, ["https://example.com/login"], script)

        assert ("input[type='email']", "alice") in calls.fills
        assert ("input[type='password']", "s3cret") in calls.fills
        assert calls.clicks and calls.clicks[0] == "button[type='submit']"
        assert summary.succeeded == 1

    def test_custom_selectors_win_over_auto_detection(self, settings):
        settings.auth_enabled = True
        settings.auth_mode = AUTH_MODE_FORM
        settings.username = "bob"
        settings.password = "pw"
        settings.login_username_selector = "#login-user"
        settings.login_password_selector = "#login-pass"
        settings.login_submit_selector = "#go"
        settings.login_wait_ms = 0

        _, _, calls = run_engine(settings, ["https://example.com"], PageScript(has_login_form=True))
        assert ("#login-user", "bob") in calls.fills
        assert ("#login-pass", "pw") in calls.fills
        assert calls.clicks[0] == "#go"

    def test_missing_form_is_a_warning_not_a_failure(self, settings):
        settings.auth_enabled = True
        settings.auth_mode = AUTH_MODE_FORM
        settings.username = "alice"
        settings.login_wait_ms = 0

        engine, summary, calls = run_engine(
            settings, ["https://example.com"], PageScript(has_login_form=False)
        )
        assert summary.succeeded == 1
        assert calls.fills == []
        assert any("No login form found" in line for line in engine.log_lines)

    def test_http_basic_credentials_go_to_the_context(self, settings):
        settings.auth_enabled = True
        settings.auth_mode = AUTH_MODE_HTTP
        settings.username = "carol"
        settings.password = "hunter2"

        _, _, calls = run_engine(settings, ["https://example.com"], PageScript())
        assert calls.contexts[0]["http_credentials"] == {"username": "carol", "password": "hunter2"}

    def test_disabled_auth_adds_no_credentials(self, settings):
        settings.auth_enabled = False
        settings.username = "ignored"
        _, _, calls = run_engine(settings, ["https://example.com"], PageScript())
        assert "http_credentials" not in calls.contexts[0]


class TestLazyScrolling:
    def test_scrolls_down_and_returns_to_top(self, settings):
        settings.scroll_to_load_lazy_content = True
        settings.lazy_scroll_step_px = 400
        script = PageScript(page_height=1_200)

        _, summary, calls = run_engine(settings, ["https://example.com"], script)

        scroll_calls = [c for c in calls.evaluates if "window.scrollTo" in c]
        assert len(scroll_calls) >= 4
        assert calls.evaluates.index("() => window.scrollTo(0, 0)") == 0 or scroll_calls
        assert summary.succeeded == 1

    def test_disabled_by_default_in_test_settings(self, settings):
        _, _, calls = run_engine(settings, ["https://example.com"], PageScript())
        assert not any("window.scrollTo" in c for c in calls.evaluates)


class TestTallPages:
    def test_page_over_the_canvas_limit_is_segmented_and_stitched(self, settings, tmp_output):
        settings.device_scale_factor = 1.0
        script = PageScript(page_width=800, page_height=20_000)

        _, summary, calls = run_engine(settings, ["https://example.com"], script)

        clips = [s["clip"] for s in calls.screenshots if s["clip"]]
        assert len(clips) == 3
        assert clips[0]["height"] == SEGMENT_PX
        assert sum(c["height"] for c in clips) == 20_000
        assert not any(s["full_page"] for s in calls.screenshots)

        result = summary.results[0]
        assert result.status is CaptureStatus.SUCCESS
        assert result.stitched is True

        with Image.open(result.file_path) as image:
            assert image.size == (800, 20_000)

    def test_full_page_failure_falls_back_to_segments(self, settings):
        script = PageScript(
            page_width=900, page_height=2_500, full_page_error=RuntimeError("Surface too large")
        )
        engine, summary, calls = run_engine(settings, ["https://example.com"], script)

        assert summary.succeeded == 1
        assert summary.results[0].stitched is True
        assert any("falling back to segmented capture" in line for line in engine.log_lines)
        assert len([s for s in calls.screenshots if s["clip"]]) == 1

    def test_scale_factor_is_applied_to_the_output(self, settings, tmp_output):
        settings.device_scale_factor = 2.0
        script = PageScript(page_width=500, page_height=900)
        _, summary, _ = run_engine(settings, ["https://example.com"], script)

        with Image.open(summary.results[0].file_path) as image:
            assert image.size == (1_000, 1_800)


class TestLogging:
    def test_every_level_is_collected(self, settings):
        engine, _, _ = run_engine(settings, ["https://example.com"], PageScript())
        text = "\n".join(engine.log_lines)
        assert "[INFO]" in text
        assert "[SUCCESS]" in text

    def test_broken_log_sink_does_not_break_the_run(self, settings):
        def explode(level, message):
            raise RuntimeError("sink is broken")

        engine = CaptureEngine(
            settings,
            playwright_factory=make_factory(PageScript()),
            log=explode,
        )
        summary = engine.run(["https://example.com"])
        assert summary.succeeded == 1

    def test_log_level_enum_values(self):
        assert LogLevel.WARNING.value == "warning"
        assert LogLevel("error") is LogLevel.ERROR


class TestReports:
    def test_json_and_csv_reports_are_written(self, settings, tmp_output):
        import csv as _csv
        import json as _json

        settings.write_report = True
        _, summary, _ = run_engine(
            settings, ["https://a.com", "https://broken.com"], PageScript(fail_first_n_gotos=1)
        )

        jsons = list(tmp_output.glob("capture-report-*.json"))
        csvs = list(tmp_output.glob("capture-report-*.csv"))
        assert len(jsons) == 1 and len(csvs) == 1

        data = _json.loads(jsons[0].read_text(encoding="utf-8"))
        assert data["totals"]["total"] == 2
        assert data["totals"]["succeeded"] == summary.succeeded
        assert data["totals"]["failed"] == summary.failed
        assert len(data["results"]) == 2
        assert data["results"][0]["url"] == "https://a.com"
        # fail_first_n_gotos=1 makes the *first* navigation in the batch fail.
        assert data["results"][0]["status"] == "failed"
        assert data["results"][1]["status"] == "success"

        with csvs[0].open(encoding="utf-8-sig", newline="") as handle:
            rows = list(_csv.DictReader(handle))
        assert len(rows) == 2
        assert rows[0]["url"] == "https://a.com"
        assert rows[0]["status"] == "failed"
        assert rows[1]["status"] == "success"
        assert set(rows[0].keys()) >= {"index", "url", "status", "file_path", "duration_ms"}

    def test_reports_can_be_disabled(self, settings, tmp_output):
        settings.write_report = False
        run_engine(settings, ["https://a.com"], PageScript())
        assert not list(tmp_output.glob("capture-report-*"))

    def test_report_survives_a_failed_url(self, settings, tmp_output):
        settings.write_report = True
        _, summary, _ = run_engine(
            settings, ["https://x.com"], PageScript(goto_error=RuntimeError("boom"))
        )
        import json as _json

        data = _json.loads(
            next(tmp_output.glob("capture-report-*.json")).read_text(encoding="utf-8")
        )
        assert data["results"][0]["status"] == "failed"
        assert data["totals"]["failed"] == 1

    def test_sqlite_history_index_is_written(self, settings, tmp_output):
        settings.write_report = True
        run_engine(settings, ["https://a.com", "https://b.com"], PageScript())

        from app.core.store import HistoryStore

        db = tmp_output / "history.sqlite3"
        assert db.exists()
        with HistoryStore(db) as store:
            rows = store.flat_rows()
        assert len(rows) == 2
        assert {r["url"] for r in rows} == {"https://a.com", "https://b.com"}

    def test_auto_dashboard_writes_html(self, settings, tmp_output):
        settings.write_report = True
        settings.auto_dashboard = True
        run_engine(settings, ["https://a.com"], PageScript())
        assert (tmp_output / "dashboard.html").exists()

    def test_auto_dashboard_off_by_default(self, settings, tmp_output):
        settings.write_report = True
        run_engine(settings, ["https://a.com"], PageScript())
        assert not (tmp_output / "dashboard.html").exists()


class TestParallel:
    def test_results_complete_and_sorted_by_index(self, settings, tmp_output):
        settings.max_concurrency = 3
        settings.write_report = False
        urls = [f"https://site{i}.example.com/page" for i in range(6)]

        _, summary, calls = run_engine(settings, urls, PageScript(page_height=700))

        assert summary.total == 6
        assert summary.succeeded == 6
        assert [r.index for r in summary.results] == [1, 2, 3, 4, 5, 6]
        assert len(list(tmp_output.glob("*.png"))) == 6
        # 3 workers -> 3 independently launched browsers.
        assert len(calls.launched) == 3
        assert all(c["headless"] for c in calls.launched)

    def test_concurrency_is_clamped_to_url_count(self, settings):
        settings.max_concurrency = 5
        _, summary, calls = run_engine(settings, ["https://a.com", "https://b.com"], PageScript())
        assert summary.succeeded == 2
        assert len(calls.launched) == 2  # min(5, 2)

    def test_one_invalid_url_does_not_break_the_others(self, settings):
        settings.max_concurrency = 2
        urls = ["not a url", "https://good.example.com"]
        _, summary, _ = run_engine(settings, urls, PageScript())
        assert summary.failed == 1
        assert summary.succeeded == 1

    def test_stop_skips_queued_urls(self, settings):
        settings.max_concurrency = 2
        engine = CaptureEngine(settings, playwright_factory=make_factory(PageScript(), CallLog()))

        def on_result(result):
            engine.request_stop()

        engine._result_cb = on_result  # noqa: SLF001
        urls = [f"https://s{i}.example.com" for i in range(6)]
        summary = engine.run(urls)

        assert summary.stopped_by_user is True
        assert summary.skipped >= 1
        assert summary.succeeded + summary.skipped == summary.total

    def test_browser_not_installed_still_raises_in_parallel(self, settings):
        settings.max_concurrency = 2
        error = Exception("Executable doesn't exist at /x/chrome")
        with pytest.raises(BrowserNotInstalledError):
            run_engine(
                settings, ["https://a.com", "https://b.com"], PageScript(), launch_error=error
            )

    def test_parallel_writes_a_single_consistent_report(self, settings, tmp_output):
        import json as _json

        settings.max_concurrency = 3
        settings.write_report = True
        urls = [f"https://r{i}.example.com" for i in range(5)]
        _, summary, _ = run_engine(settings, urls, PageScript())

        reports = list(tmp_output.glob("capture-report-*.json"))
        assert len(reports) == 1
        data = _json.loads(reports[0].read_text(encoding="utf-8"))
        assert [r["index"] for r in data["results"]] == [1, 2, 3, 4, 5]
        assert data["totals"]["succeeded"] == 5


class TestChangeDetection:
    URL = "https://monitor.example.com"

    def _timestamps(self, tmp_output):
        return [p for p in tmp_output.glob("*.png") if not p.name.startswith("latest_")]

    def test_disabled_writes_no_baseline(self, settings, tmp_output):
        settings.change_detection_enabled = False
        run_engine(settings, [self.URL], PageScript())
        assert not list(tmp_output.glob("latest_*"))
        assert len(self._timestamps(tmp_output)) == 1

    def test_first_run_creates_baseline(self, settings, tmp_output, monkeypatch):
        import app.core.engine as eng

        settings.change_detection_enabled = True
        monkeypatch.setattr(eng, "diff_ratio", lambda a, b: 0.0)
        run_engine(settings, [self.URL], PageScript())
        assert (tmp_output / "latest_monitor_example_com.png").exists()
        assert len(self._timestamps(tmp_output)) == 1

    def test_unchanged_page_is_not_saved_again(self, settings, tmp_output, monkeypatch):
        import app.core.engine as eng

        settings.change_detection_enabled = True
        monkeypatch.setattr(eng, "diff_ratio", lambda a, b: 0.0)

        run_engine(settings, [self.URL], PageScript())
        _, second, _ = run_engine(settings, [self.URL], PageScript())

        assert len(self._timestamps(tmp_output)) == 1  # no duplicate capture
        result = second.results[0]
        assert result.unchanged is True
        assert result.diff == 0.0
        assert result.status is CaptureStatus.SUCCESS
        assert result.file_path.endswith("latest_monitor_example_com.png")

    def test_changed_page_saves_a_new_capture(self, settings, tmp_output, monkeypatch):
        import app.core.engine as eng

        settings.change_detection_enabled = True
        monkeypatch.setattr(eng, "diff_ratio", lambda a, b: 0.9)

        run_engine(settings, [self.URL], PageScript())
        _, second, _ = run_engine(settings, [self.URL], PageScript())

        assert len(self._timestamps(tmp_output)) == 2
        result = second.results[0]
        assert result.unchanged is False
        assert result.diff == 0.9

    def test_threshold_boundary(self, settings, tmp_output, monkeypatch):
        import app.core.engine as eng

        settings.change_detection_enabled = True
        settings.change_threshold = 0.5
        monkeypatch.setattr(eng, "diff_ratio", lambda a, b: 0.5)  # >= threshold

        run_engine(settings, [self.URL], PageScript())
        _, second, _ = run_engine(settings, [self.URL], PageScript())
        assert second.results[0].unchanged is False  # exactly at threshold => changed

    def test_report_includes_unchanged_and_diff(self, settings, tmp_output, monkeypatch):
        import json as _json

        import app.core.engine as eng

        settings.change_detection_enabled = True
        settings.write_report = True
        monkeypatch.setattr(eng, "diff_ratio", lambda a, b: 0.0)

        run_engine(settings, [self.URL], PageScript())
        run_engine(settings, [self.URL], PageScript())

        # Each run writes its own report; the second one must flag the URL as
        # unchanged with the recorded diff.
        reports = [
            _json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(tmp_output.glob("capture-report-*.json"))
        ]
        assert any(
            report["results"][0]["unchanged"] is True and report["results"][0]["diff"] == 0.0
            for report in reports
        )


class TestRetryBackoff:
    def test_backoff_grows_exponentially_and_caps(self):
        settings = CaptureSettings(
            output_dir="/tmp/x", retries=5, retry_backoff_ms=100, retry_backoff_max_ms=250
        )
        engine = CaptureEngine(settings)
        assert engine._backoff_ms(1) == 100
        assert engine._backoff_ms(2) == 200
        assert engine._backoff_ms(3) == 250  # capped
        assert engine._backoff_ms(4) == 250

    def test_zero_base_disables_backoff(self):
        engine = CaptureEngine(CaptureSettings(output_dir="/tmp/x", retry_backoff_ms=0))
        assert engine._backoff_ms(3) == 0

    def test_retries_apply_backoff_then_succeed(self, tmp_path):
        settings = CaptureSettings(
            output_dir=str(tmp_path), retries=2, retry_backoff_ms=1, retry_backoff_max_ms=2
        )
        _, summary, _ = run_engine(
            settings, ["https://example.com"], PageScript(fail_first_n_gotos=2)
        )
        assert summary.results[0].attempts == 3
        assert summary.succeeded == 1

    def test_negative_backoff_rejected(self):
        with pytest.raises(Exception):
            CaptureSettings(output_dir="/tmp/x", retry_backoff_ms=-1).validate()

    def test_cap_below_base_rejected(self):
        with pytest.raises(Exception):
            CaptureSettings(
                output_dir="/tmp/x", retry_backoff_ms=500, retry_backoff_max_ms=100
            ).validate()


class TestWatchdog:
    def test_health_probe_reads_is_connected(self):
        from tests.fakes import CallLog, FakeBrowser, PageScript

        engine = CaptureEngine(CaptureSettings(output_dir="/tmp/x"))
        browser = FakeBrowser(PageScript(), CallLog())
        assert engine._browser_is_healthy(browser) is True
        browser.disconnect()
        assert engine._browser_is_healthy(browser) is False
        assert engine._browser_is_healthy(None) is False

    def test_ensure_relaunches_a_dead_browser(self):
        from tests.fakes import CallLog, FakeBrowser, PageScript

        calls = CallLog()
        engine = CaptureEngine(CaptureSettings(output_dir="/tmp/x"))
        dead = FakeBrowser(PageScript(), calls)
        dead.disconnect()
        fresh = FakeBrowser(PageScript(), calls)
        engine._launch = lambda pw: fresh

        assert engine._ensure_healthy_browser(dead, object()) is fresh
        assert calls.browser_closed is True

    def test_ensure_keeps_a_healthy_browser(self):
        from tests.fakes import CallLog, FakeBrowser, PageScript

        engine = CaptureEngine(CaptureSettings(output_dir="/tmp/x"))
        browser = FakeBrowser(PageScript(), CallLog())
        assert engine._ensure_healthy_browser(browser, object()) is browser

    def test_sequential_relaunches_after_disconnect(self, tmp_path):
        settings = CaptureSettings(output_dir=str(tmp_path), watchdog_enabled=True)
        _, summary, calls = run_engine(
            settings,
            ["https://a.example.com", "https://b.example.com"],
            PageScript(disconnect_after=1),
        )
        assert summary.succeeded == 2
        assert len(calls.launched) == 2  # relaunched before the 2nd URL

    def test_parallel_relaunches_after_disconnect(self, tmp_path):
        settings = CaptureSettings(
            output_dir=str(tmp_path), max_concurrency=2, watchdog_enabled=True
        )
        _, summary, calls = run_engine(
            settings,
            [
                "https://a.example.com",
                "https://b.example.com",
                "https://c.example.com",
                "https://d.example.com",
            ],
            PageScript(disconnect_after=1),
        )
        assert summary.succeeded == 4
        assert len(calls.launched) >= 3  # 2 workers + at least one relaunch

    def test_watchdog_disabled_never_relaunches(self, tmp_path):
        settings = CaptureSettings(output_dir=str(tmp_path), watchdog_enabled=False)
        _, summary, calls = run_engine(
            settings,
            ["https://a.example.com", "https://b.example.com"],
            PageScript(disconnect_after=1),
        )
        assert summary.succeeded == 2
        assert len(calls.launched) == 1


class TestRateLimitAndRobots:
    def test_rate_limit_sleeps_when_set(self, monkeypatch):
        engine = CaptureEngine(CaptureSettings(output_dir="/tmp/x", request_delay_ms=50))
        calls = []
        monkeypatch.setattr(engine, "_interruptible_sleep", lambda ms: calls.append(ms))
        engine._rate_limit_sleep()
        assert calls == [50]

    def test_rate_limit_zero_does_not_sleep(self, monkeypatch):
        engine = CaptureEngine(CaptureSettings(output_dir="/tmp/x", request_delay_ms=0))
        calls = []
        monkeypatch.setattr(engine, "_interruptible_sleep", lambda ms: calls.append(ms))
        engine._rate_limit_sleep()
        assert calls == []

    def test_robots_blocks_url(self, monkeypatch, tmp_path):
        import app.core.engine as eng

        monkeypatch.setattr(eng.CaptureEngine, "_robots_allows", lambda self, url: False)
        settings = CaptureSettings(output_dir=str(tmp_path), respect_robots=True)
        _, summary, _ = run_engine(settings, ["https://example.com"], PageScript())
        assert summary.results[0].status is CaptureStatus.SKIPPED
        assert "robots" in summary.results[0].message.lower()

    def test_robots_allows_capture(self, monkeypatch, tmp_path):
        import app.core.engine as eng

        monkeypatch.setattr(eng.CaptureEngine, "_robots_allows", lambda self, url: True)
        settings = CaptureSettings(output_dir=str(tmp_path), respect_robots=True)
        _, summary, _ = run_engine(settings, ["https://example.com"], PageScript())
        assert summary.succeeded == 1

    def test_robots_allows_when_unreachable(self, monkeypatch):
        import urllib.robotparser as rp

        class FakeRP:
            def set_url(self, url):
                pass

            def read(self):
                raise OSError("no network")

            def can_fetch(self, ua, url):
                return False

        monkeypatch.setattr(rp, "RobotFileParser", FakeRP)
        engine = CaptureEngine(CaptureSettings(output_dir="/tmp/x", respect_robots=True))
        assert engine._robots_allows("https://example.com/x") is True

    def test_robots_disallow_is_honoured(self, monkeypatch):
        import urllib.robotparser as rp

        class FakeRP:
            def set_url(self, url):
                pass

            def read(self):
                pass

            def can_fetch(self, ua, url):
                return False

        monkeypatch.setattr(rp, "RobotFileParser", FakeRP)
        engine = CaptureEngine(CaptureSettings(output_dir="/tmp/x", respect_robots=True))
        assert engine._robots_allows("https://example.com/x") is False

    def test_negative_delay_rejected(self):
        with pytest.raises(Exception):
            CaptureSettings(output_dir="/tmp/x", request_delay_ms=-1).validate()


class TestWebPFormat:
    def test_webp_output_is_written(self, tmp_path):
        from PIL import Image

        settings = CaptureSettings(output_dir=str(tmp_path), image_format="webp", jpeg_quality=80)
        _, summary, _ = run_engine(settings, ["https://example.com"], PageScript())
        assert summary.succeeded == 1
        files = list(tmp_path.glob("*.webp"))
        assert files, "expected a .webp file"
        with Image.open(files[0]) as image:
            assert image.format == "WEBP"

    def test_webp_extension_and_validation(self):
        settings = CaptureSettings(output_dir="/tmp/x", image_format="webp")
        settings.validate()
        assert settings.file_extension() == "webp"


class TestPageDiagnostics:
    def test_console_and_page_errors_are_collected(self, tmp_path, monkeypatch):
        # Emit events from the fake page right after a screenshot is taken.
        import tests.fakes as fakes

        original = fakes.FakePage.screenshot

        def patched(self, *a, **k):
            data = original(self, *a, **k)
            self.emit("console", type("M", (), {"type": "error", "text": "boom"})())
            self.emit("pageerror", "ReferenceError: x")
            self.emit("requestfailed", type("R", (), {"url": "https://cdn/x.js"})())
            return data

        monkeypatch.setattr(fakes.FakePage, "screenshot", patched)

        settings = CaptureSettings(output_dir=str(tmp_path))
        _, summary, _ = run_engine(settings, ["https://example.com"], PageScript())
        result = summary.results[0]
        assert result.console_errors == ["boom"]
        assert result.page_errors == ["ReferenceError: x"]
        assert result.failed_requests == ["https://cdn/x.js"]

    def test_no_listeners_means_empty(self, tmp_path):
        settings = CaptureSettings(output_dir=str(tmp_path))
        _, summary, _ = run_engine(settings, ["https://example.com"], PageScript())
        assert summary.results[0].console_errors == []


class TestHarExport:
    def test_har_path_added_when_enabled(self, tmp_path):
        settings = CaptureSettings(output_dir=str(tmp_path), save_har=True)
        _, _, calls = run_engine(settings, ["https://example.com"], PageScript())
        assert "record_har_path" in calls.contexts[0]
        assert calls.contexts[0]["record_har_path"].endswith(".har")

    def test_no_har_by_default(self, tmp_path):
        settings = CaptureSettings(output_dir=str(tmp_path))
        _, _, calls = run_engine(settings, ["https://example.com"], PageScript())
        assert "record_har_path" not in calls.contexts[0]


class TestPerHostLimit:
    def test_semaphore_is_shared_per_host_and_capped(self):
        settings = CaptureSettings(output_dir="/tmp/x", max_per_host=2, max_concurrency=4)
        engine = CaptureEngine(settings)
        sem = engine._host_semaphore("example.com")
        assert engine._host_semaphore("example.com") is sem
        assert engine._host_semaphore("other.com") is not sem
        assert sem.acquire(timeout=0) is True
        assert sem.acquire(timeout=0) is True
        assert sem.acquire(timeout=0.01) is False  # capacity reached

    def test_parallel_with_per_host_limit_completes(self, tmp_path):
        settings = CaptureSettings(output_dir=str(tmp_path), max_concurrency=3, max_per_host=1)
        _, summary, _ = run_engine(
            settings,
            ["https://a.example.com", "https://b.example.com", "https://c.example.com"],
            PageScript(),
        )
        assert summary.succeeded == 3

    def test_negative_per_host_rejected(self):
        with pytest.raises(Exception):
            CaptureSettings(output_dir="/tmp/x", max_per_host=-1).validate()


class TestAvifFormat:
    def test_avif_falls_back_to_png_when_unsupported(self, tmp_path, monkeypatch):
        import app.core.engine as eng

        monkeypatch.setattr(eng.CaptureEngine, "_avif_supported", staticmethod(lambda: False))
        settings = CaptureSettings(output_dir=str(tmp_path), image_format="avif")
        _, summary, _ = run_engine(settings, ["https://example.com"], PageScript())
        assert summary.succeeded == 1
        assert list(tmp_path.glob("*.png")), "expected a PNG fallback"
        assert not list(tmp_path.glob("*.avif"))

    def test_avif_written_when_supported(self, tmp_path):
        from PIL import Image, features

        if not features.check("avif"):
            import pytest

            pytest.skip("Pillow built without AVIF")
        settings = CaptureSettings(output_dir=str(tmp_path), image_format="avif", jpeg_quality=70)
        _, summary, _ = run_engine(settings, ["https://example.com"], PageScript())
        assert summary.succeeded == 1
        files = list(tmp_path.glob("*.avif"))
        assert files, "expected an .avif file"
        with Image.open(files[0]) as image:
            assert image.format == "AVIF"

    def test_avif_extension_and_validation(self):
        settings = CaptureSettings(output_dir="/tmp/x", image_format="avif")
        settings.validate()
        assert settings.file_extension() == "avif"


class TestPriorityQueue:
    def test_higher_priority_runs_first(self, tmp_path):
        calls = CallLog()
        settings = CaptureSettings(output_dir=str(tmp_path))
        items = [
            (0, "https://low.example.com"),
            (5, "https://high.example.com"),
            (0, "https://mid.example.com"),
        ]
        _, summary, calls = run_engine(settings, items, PageScript(), calls)
        gotos = [g["url"] for g in calls.gotos]
        assert gotos[0] == "https://high.example.com"
        # results are reported in the original input order regardless of run order
        assert [r.index for r in summary.results] == [1, 2, 3]

    def test_dict_items_supported(self, tmp_path):
        settings = CaptureSettings(output_dir=str(tmp_path))
        items = [
            {"priority": 1, "url": "https://a.example.com"},
            {"priority": 9, "url": "https://b.example.com"},
        ]
        _, summary, calls = run_engine(settings, items, PageScript(), CallLog())
        assert calls.gotos[0]["url"] == "https://b.example.com"
        assert summary.succeeded == 2

    def test_plain_strings_keep_order(self, tmp_path):
        settings = CaptureSettings(output_dir=str(tmp_path))
        _, summary, calls = run_engine(
            settings, ["https://a.example.com", "https://b.example.com"], PageScript(), CallLog()
        )
        assert [g["url"] for g in calls.gotos] == ["https://a.example.com", "https://b.example.com"]
        assert [r.index for r in summary.results] == [1, 2]


class TestAlertThreshold:
    def _capture_notify(self, monkeypatch):
        from app.core import alerts

        captured = {}

        def fake_notify(settings, changed, run_id=""):
            captured["changed"] = list(changed)
            return [("webhook", True)]

        monkeypatch.setattr(alerts, "notify", fake_notify)
        return captured

    def _result(self, diff):
        from app.core.engine import CaptureResult

        return CaptureResult(index=0, url="https://a.com", status=CaptureStatus.SUCCESS, diff=diff)

    def test_below_threshold_does_not_alert(self, settings, monkeypatch):
        captured = self._capture_notify(monkeypatch)
        settings.alert_enabled = True
        settings.alert_webhook_url = "https://hook"
        settings.change_alert_threshold = 0.5
        CaptureEngine(settings)._maybe_send_alerts([self._result(0.2)])
        assert "changed" not in captured  # notify never reached

    def test_above_threshold_alerts(self, settings, monkeypatch):
        captured = self._capture_notify(monkeypatch)
        settings.alert_enabled = True
        settings.alert_webhook_url = "https://hook"
        settings.change_alert_threshold = 0.1
        CaptureEngine(settings)._maybe_send_alerts([self._result(0.4)])
        assert captured["changed"] and captured["changed"][0]["diff"] == 0.4

    def test_zero_threshold_alerts_on_any_change(self, settings, monkeypatch):
        captured = self._capture_notify(monkeypatch)
        settings.alert_enabled = True
        settings.alert_webhook_url = "https://hook"
        settings.change_alert_threshold = 0.0
        CaptureEngine(settings)._maybe_send_alerts([self._result(0.01)])
        assert captured["changed"]


class TestAlertCooldown:
    def _capture_notify(self, monkeypatch):
        from app.core import alerts

        calls = []

        def fake_notify(settings, changed, run_id=""):
            calls.append(list(changed))
            return [("webhook", True)]

        monkeypatch.setattr(alerts, "notify", fake_notify)
        return calls

    def _result(self, diff=0.5):
        from app.core.engine import CaptureResult

        return CaptureResult(index=0, url="https://a.com", status=CaptureStatus.SUCCESS, diff=diff)

    def test_second_alert_within_cooldown_suppressed(self, settings, tmp_output, monkeypatch):
        calls = self._capture_notify(monkeypatch)
        settings.alert_enabled = True
        settings.alert_webhook_url = "https://hook"
        settings.output_dir = str(tmp_output)
        settings.alert_cooldown_minutes = 60
        engine = CaptureEngine(settings)
        engine._maybe_send_alerts([self._result()])
        engine._maybe_send_alerts([self._result()])  # within the cooldown window
        assert len(calls) == 1

    def test_no_cooldown_alerts_every_time(self, settings, tmp_output, monkeypatch):
        calls = self._capture_notify(monkeypatch)
        settings.alert_enabled = True
        settings.alert_webhook_url = "https://hook"
        settings.output_dir = str(tmp_output)
        settings.alert_cooldown_minutes = 0
        engine = CaptureEngine(settings)
        engine._maybe_send_alerts([self._result()])
        engine._maybe_send_alerts([self._result()])
        assert len(calls) == 2


class TestBaselinePreference:
    def test_compare_path_prefers_a_pinned_baseline(self, settings, tmp_output):
        from app.core import baseline
        from app.core.engine import CaptureEngine

        baseline.latest_path(tmp_output, "https://a.example.com").write_bytes(b"latest")
        pinned = baseline.pin_baseline(tmp_output, "https://a.example.com")
        engine = CaptureEngine(settings)
        assert engine._compare_path(tmp_output, "https://a.example.com") == pinned

    def test_compare_path_falls_back_to_latest(self, settings, tmp_output):
        from app.core import baseline
        from app.core.engine import CaptureEngine

        engine = CaptureEngine(settings)
        expected = baseline.latest_path(tmp_output, "https://a.example.com")
        assert engine._compare_path(tmp_output, "https://a.example.com") == expected


class TestHistoryRetention:
    def test_retention_prunes_the_index(self, settings, tmp_output):
        from app.core.store import HistoryStore

        db = tmp_output / "history.sqlite3"
        with HistoryStore(db) as store:
            store.add_run(
                "2020-01-01T00:00:00",
                [{"url": "https://old.com", "status": "success", "diff": None, "file_path": ""}],
            )
        settings.write_report = True
        settings.history_retention_days = 7
        run_engine(settings, ["https://a.com"], PageScript())
        with HistoryStore(db) as store:
            urls = {row["url"] for row in store.flat_rows()}
        assert urls == {"https://a.com"}  # the 2020 row is gone, today's remains

    def test_retention_off_keeps_old_rows(self, settings, tmp_output):
        from app.core.store import HistoryStore

        db = tmp_output / "history.sqlite3"
        with HistoryStore(db) as store:
            store.add_run(
                "2020-01-01T00:00:00",
                [{"url": "https://old.com", "status": "success", "diff": None, "file_path": ""}],
            )
        settings.write_report = True
        settings.history_retention_days = 0
        run_engine(settings, ["https://a.com"], PageScript())
        with HistoryStore(db) as store:
            urls = {row["url"] for row in store.flat_rows()}
        assert urls == {"https://a.com", "https://old.com"}


class TestStaleBaselineWarning:
    def _pin_old_baseline(self, tmp_output, url="https://example.com", age_days=60):
        import os
        import time

        from app.core import baseline

        baseline.latest_path(tmp_output, url).write_bytes(b"png")
        pinned = baseline.pin_baseline(tmp_output, url)
        when = time.time() - age_days * 86400
        os.utime(pinned, (when, when))

    def test_warns_when_a_pinned_baseline_is_old(self, settings, tmp_output):
        self._pin_old_baseline(tmp_output)
        settings.write_report = True
        settings.baseline_max_age_days = 7
        messages = []
        run_engine(
            settings,
            ["https://example.com"],
            PageScript(),
            log=lambda level, message: messages.append(message),
        )
        assert any("re-pin" in message for message in messages)

    def test_no_warning_when_the_check_is_off(self, settings, tmp_output):
        self._pin_old_baseline(tmp_output)
        settings.write_report = True
        settings.baseline_max_age_days = 0
        messages = []
        run_engine(
            settings,
            ["https://example.com"],
            PageScript(),
            log=lambda level, message: messages.append(message),
        )
        assert not any("re-pin" in message for message in messages)


class TestUndecodableBaseline:
    def test_a_broken_reference_does_not_fail_the_capture(self, settings, tmp_output):
        from app.core import baseline

        url = "https://example.com"
        # A pinned reference that is not a real image must mean "no baseline".
        baseline.latest_path(tmp_output, url).write_bytes(b"not-an-image")
        baseline.pin_baseline(tmp_output, url)
        settings.change_detection_enabled = True
        settings.change_threshold = 0.0
        engine, summary, _ = run_engine(settings, [url], PageScript())
        assert summary.succeeded == 1
        assert summary.results[0].file_path


class TestScreenshotRetention:
    def _old_capture(self, tmp_output):
        import os
        import time

        old = tmp_output / "001_old_20250101-000000.png"
        old.write_bytes(b"png")
        when = time.time() - 60 * 86400
        os.utime(old, (when, when))
        return old

    def test_old_screenshots_are_deleted_after_a_run(self, settings, tmp_output):
        old = self._old_capture(tmp_output)
        settings.screenshot_retention_days = 7
        run_engine(settings, ["https://a.com"], PageScript())
        assert not old.exists()

    def test_retention_off_keeps_the_files(self, settings, tmp_output):
        old = self._old_capture(tmp_output)
        settings.screenshot_retention_days = 0
        run_engine(settings, ["https://a.com"], PageScript())
        assert old.exists()

    def test_a_real_capture_is_never_removed(self, settings, tmp_output):
        settings.screenshot_retention_days = 7
        run_engine(settings, ["https://a.com"], PageScript())
        assert len(list(tmp_output.glob("*.png"))) == 1


class TestDriftAlerts:
    def _seed_drift(self, tmp_output, url="https://example.com", reverse=True):
        from app.core import baseline
        from tests.test_baseline import _gradient

        _gradient(baseline.latest_path(tmp_output, url))
        baseline.pin_baseline(tmp_output, url)
        _gradient(baseline.latest_path(tmp_output, url), reverse=reverse)

    def _capture(self, monkeypatch):
        from app.core import alerts

        calls = []

        def fake_notify(settings, changed, run_id=""):
            calls.append(list(changed))
            return [("webhook", True)]

        monkeypatch.setattr(alerts, "notify", fake_notify)
        return calls

    def _alertable(self, settings):
        settings.alert_enabled = True
        settings.alert_webhook_url = "https://hook"

    def test_drift_alert_fires_even_without_change_detection(
        self, settings, tmp_output, monkeypatch
    ):
        calls = self._capture(monkeypatch)
        self._seed_drift(tmp_output)
        self._alertable(settings)
        settings.change_detection_enabled = False  # no per-run diff at all
        settings.baseline_drift_alert_threshold = 0.3
        run_engine(settings, ["https://example.com"], PageScript())
        assert calls and calls[0][0]["reason"] == "baseline_drift"
        assert calls[0][0]["diff"] > 0.3

    def test_small_drift_does_not_alert(self, settings, tmp_output, monkeypatch):
        calls = self._capture(monkeypatch)
        self._seed_drift(tmp_output, reverse=False)  # identical images: no drift
        self._alertable(settings)
        settings.change_detection_enabled = False
        settings.baseline_drift_alert_threshold = 0.3
        run_engine(settings, ["https://example.com"], PageScript())
        assert calls == []

    def test_drift_threshold_zero_disables_the_check(self, settings, tmp_output, monkeypatch):
        calls = self._capture(monkeypatch)
        self._seed_drift(tmp_output)
        self._alertable(settings)
        settings.change_detection_enabled = False
        settings.baseline_drift_alert_threshold = 0.0
        run_engine(settings, ["https://example.com"], PageScript())
        assert calls == []

    def test_a_changed_page_is_not_paged_twice(self, settings, tmp_output, monkeypatch):
        import app.core.engine as eng

        calls = self._capture(monkeypatch)
        self._seed_drift(tmp_output)
        self._alertable(settings)
        settings.change_detection_enabled = True
        settings.change_threshold = 0.0
        settings.baseline_drift_alert_threshold = 0.3
        # The run sees a real change (0.9) *and* the files show a real drift, so
        # both gates are hot for the same page - it must still be paged once.
        monkeypatch.setattr(eng, "diff_ratio", lambda a, b: 0.9)
        engine, summary, _ = run_engine(settings, ["https://example.com"], PageScript())
        assert summary.results[0].diff == 0.9
        assert len(calls) == 1
        assert calls[0][0].get("reason") != "baseline_drift"


class TestDriftIsRecorded:
    def _pin_baseline(self, tmp_output, url="https://example.com"):
        from app.core import baseline
        from tests.test_baseline import _gradient

        _gradient(baseline.latest_path(tmp_output, url))
        baseline.pin_baseline(tmp_output, url)
        _gradient(baseline.latest_path(tmp_output, url), reverse=True)

    def test_report_and_index_carry_the_drift(self, settings, tmp_output):
        import json

        from app.core.store import HistoryStore

        self._pin_baseline(tmp_output)
        settings.write_report = True
        run_engine(settings, ["https://example.com"], PageScript())

        report = next(tmp_output.glob("capture-report-*.json"))
        row = json.loads(report.read_text(encoding="utf-8"))["results"][0]
        assert row["drift"] is not None and row["drift"] > 0.5

        with HistoryStore(tmp_output / "history.sqlite3") as store:
            indexed = store.flat_rows()[0]
            assert indexed["drift"] == pytest.approx(row["drift"])

    def test_sites_without_a_baseline_record_no_drift(self, settings, tmp_output):
        import json

        settings.write_report = True
        run_engine(settings, ["https://example.com"], PageScript())
        report = next(tmp_output.glob("capture-report-*.json"))
        row = json.loads(report.read_text(encoding="utf-8"))["results"][0]
        assert row["drift"] is None

    def test_drift_is_computed_once_per_run(self, settings, tmp_output, monkeypatch):
        import app.core.baseline as baseline_mod
        import app.core.engine as eng

        self._pin_baseline(tmp_output)
        calls = []
        real = baseline_mod.baseline_drift

        def counting(output_dir, url):
            calls.append(url)
            return real(output_dir, url)

        monkeypatch.setattr(eng, "baseline_drift", counting)
        settings.write_report = True
        settings.alert_enabled = True
        settings.alert_webhook_url = "https://hook"
        settings.baseline_drift_alert_threshold = 0.3
        monkeypatch.setattr(
            eng.alerts, "notify", lambda settings, changed, run_id="": [("webhook", True)]
        )
        run_engine(settings, ["https://example.com"], PageScript())
        # The report writer and the drift alert share one measurement.
        assert calls.count("https://example.com") == 1


class TestQuietHours:
    """Alerts inside the window wait; the first run after it sends them together."""

    def _capture_notify(self, monkeypatch):
        from app.core import alerts

        calls = []

        def fake_notify(settings, changed, run_id=""):
            calls.append(list(changed))
            return [("webhook", True)]

        monkeypatch.setattr(alerts, "notify", fake_notify)
        return calls

    def _engine(self, settings, tmp_output, when, monkeypatch):
        self._capture_notify(monkeypatch)
        settings.alert_enabled = True
        settings.alert_webhook_url = "https://hook"
        settings.output_dir = str(tmp_output)
        settings.alert_quiet_hours = "22:00-07:00"
        return CaptureEngine(settings, now=lambda: when)

    def _result(self, url="https://a.com", diff=0.5):
        from app.core.engine import CaptureResult

        return CaptureResult(index=0, url=url, status=CaptureStatus.SUCCESS, diff=diff)

    def test_night_run_queues_instead_of_alerting(self, settings, tmp_output, monkeypatch):
        from app.core import quiet

        engine = self._engine(settings, tmp_output, datetime(2026, 5, 4, 23, 30), monkeypatch)
        engine._maybe_send_alerts([self._result()])
        queued = quiet.load(quiet.queue_path(tmp_output))
        assert [item["url"] for item in queued] == ["https://a.com"]

    def test_morning_run_flushes_the_queue(self, settings, tmp_output, monkeypatch):
        engine = self._engine(settings, tmp_output, datetime(2026, 5, 4, 23, 30), monkeypatch)
        engine._maybe_send_alerts([self._result()])

        morning = CaptureEngine(settings, now=lambda: datetime(2026, 5, 5, 7, 30))
        calls = []
        from app.core import alerts

        monkeypatch.setattr(
            alerts,
            "notify",
            lambda s, changed, run_id="": calls.append(list(changed)) or [("webhook", True)],
        )
        morning._maybe_send_alerts([])  # a quiet morning with no changes of its own
        assert [item["url"] for item in calls[0]] == ["https://a.com"]

    def test_queue_is_empty_again_after_the_flush(self, settings, tmp_output, monkeypatch):
        from app.core import quiet

        engine = self._engine(settings, tmp_output, datetime(2026, 5, 4, 23, 30), monkeypatch)
        engine._maybe_send_alerts([self._result()])
        CaptureEngine(settings, now=lambda: datetime(2026, 5, 5, 7, 30))._maybe_send_alerts([])
        assert not quiet.queue_path(tmp_output).exists()

    def test_a_site_that_changed_twice_is_sent_once(self, settings, tmp_output, monkeypatch):
        engine = self._engine(settings, tmp_output, datetime(2026, 5, 4, 23, 30), monkeypatch)
        engine._maybe_send_alerts([self._result(diff=0.2)])
        engine._maybe_send_alerts([self._result(diff=0.6)])

        captured = self._capture_notify(monkeypatch)
        CaptureEngine(settings, now=lambda: datetime(2026, 5, 5, 8, 0))._maybe_send_alerts([])
        assert len(captured[0]) == 1 and captured[0][0]["diff"] == 0.6

    def test_queued_and_fresh_items_are_merged(self, settings, tmp_output, monkeypatch):
        engine = self._engine(settings, tmp_output, datetime(2026, 5, 4, 23, 30), monkeypatch)
        engine._maybe_send_alerts([self._result(url="https://a.com")])

        captured = self._capture_notify(monkeypatch)
        CaptureEngine(settings, now=lambda: datetime(2026, 5, 5, 8, 0))._maybe_send_alerts(
            [self._result(url="https://b.com")]
        )
        assert sorted(item["url"] for item in captured[0]) == ["https://a.com", "https://b.com"]

    def test_outside_the_window_nothing_is_queued(self, settings, tmp_output, monkeypatch):
        from app.core import quiet

        engine = self._engine(settings, tmp_output, datetime(2026, 5, 5, 12, 0), monkeypatch)
        engine._maybe_send_alerts([self._result()])
        assert not quiet.queue_path(tmp_output).exists()

    def test_the_queue_is_announced_in_the_log(self, settings, tmp_output, monkeypatch):
        from app.core.engine import CaptureEngine

        lines = []
        self._capture_notify(monkeypatch)
        settings.alert_enabled = True
        settings.alert_webhook_url = "https://hook"
        settings.output_dir = str(tmp_output)
        settings.alert_quiet_hours = "22:00-07:00"
        engine = CaptureEngine(
            settings,
            now=lambda: datetime(2026, 5, 4, 23, 30),
            log=lambda level, message: lines.append(message),
        )
        engine._maybe_send_alerts([self._result()])
        assert any("Quiet hours" in line and "queued" in line for line in lines)
        assert any("07:00" in line for line in lines)

    def test_disabled_alerts_ignore_quiet_hours(self, settings, tmp_output, monkeypatch):
        from app.core import quiet

        calls = self._capture_notify(monkeypatch)
        settings.alert_enabled = False
        settings.output_dir = str(tmp_output)
        settings.alert_quiet_hours = "22:00-07:00"
        CaptureEngine(settings, now=lambda: datetime(2026, 5, 4, 23, 30))._maybe_send_alerts(
            [self._result()]
        )
        assert calls == [] and not quiet.queue_path(tmp_output).exists()


class TestScreenshotSizeCap:
    """The size cap trims the oldest captures after a run, on top of the age window."""

    def _fat_files(self, output_dir, count=3, kb=600):
        import os
        import time

        for index in range(count):
            path = output_dir / f"fat{index}.png"
            path.write_bytes(b"x" * (kb * 1024))
            when = time.time() - (count - index) * 3600  # fat0 is the oldest
            os.utime(path, (when, when))
        return output_dir

    def test_the_cap_removes_the_oldest_captures(self, settings, tmp_output):
        lines = []
        self._fat_files(tmp_output)
        settings.screenshot_retention_mb = 1
        script = PageScript(page_width=1200, page_height=900)
        run_engine(
            settings,
            ["https://example.com"],
            script,
            log=lambda level, message: lines.append(message),
        )
        left = sorted(path.name for path in tmp_output.glob("fat*.png"))
        assert left == ["fat2.png"]  # the two oldest gave way to the cap
        assert any("to stay under 1 MB" in line for line in lines)

    def test_without_a_cap_nothing_is_removed(self, settings, tmp_output):
        self._fat_files(tmp_output)
        settings.screenshot_retention_mb = 0
        run_engine(settings, ["https://example.com"], PageScript())
        assert len(list(tmp_output.glob("fat*.png"))) == 3

    def test_a_folder_under_the_cap_is_left_alone(self, settings, tmp_output):
        self._fat_files(tmp_output, count=1, kb=10)
        settings.screenshot_retention_mb = 50
        run_engine(settings, ["https://example.com"], PageScript())
        assert (tmp_output / "fat0.png").exists()


class TestAlertMuteList:
    """Muted fragments never reach the webhook/email channel."""

    def _capture_notify(self, monkeypatch):
        from app.core import alerts

        calls = []

        def fake_notify(settings, changed, run_id=""):
            calls.append(list(changed))
            return [("webhook", True)]

        monkeypatch.setattr(alerts, "notify", fake_notify)
        return calls

    def _result(self, url, diff=0.5):
        from app.core.engine import CaptureResult

        return CaptureResult(index=0, url=url, status=CaptureStatus.SUCCESS, diff=diff)

    def test_a_muted_site_is_not_notified(self, settings, tmp_output, monkeypatch):
        calls = self._capture_notify(monkeypatch)
        settings.alert_enabled = True
        settings.alert_webhook_url = "https://hook"
        settings.output_dir = str(tmp_output)
        settings.alert_mute_urls = "staging"
        CaptureEngine(settings)._maybe_send_alerts([self._result("https://staging.example.com")])
        assert calls == []

    def test_the_other_sites_still_alert(self, settings, tmp_output, monkeypatch):
        calls = self._capture_notify(monkeypatch)
        settings.alert_enabled = True
        settings.alert_webhook_url = "https://hook"
        settings.output_dir = str(tmp_output)
        settings.alert_mute_urls = "staging"
        CaptureEngine(settings)._maybe_send_alerts(
            [self._result("https://staging.example.com"), self._result("https://shop.example.com")]
        )
        assert [item["url"] for item in calls[0]] == ["https://shop.example.com"]

    def test_the_mute_is_logged(self, settings, tmp_output, monkeypatch):
        from app.core.engine import CaptureEngine

        lines = []
        self._capture_notify(monkeypatch)
        settings.alert_enabled = True
        settings.alert_webhook_url = "https://hook"
        settings.output_dir = str(tmp_output)
        settings.alert_mute_urls = "staging"
        CaptureEngine(
            settings, log=lambda level, message: lines.append(message)
        )._maybe_send_alerts([self._result("https://staging.example.com")])
        assert any("muted by the alert mute list" in line for line in lines)

    def test_an_empty_list_keeps_everything(self, settings, tmp_output, monkeypatch):
        calls = self._capture_notify(monkeypatch)
        settings.alert_enabled = True
        settings.alert_webhook_url = "https://hook"
        settings.output_dir = str(tmp_output)
        settings.alert_mute_urls = ""
        CaptureEngine(settings)._maybe_send_alerts([self._result("https://staging.example.com")])
        assert len(calls) == 1


class TestHistorySizeCap:
    """The reports and the index are the history - and they can grow forever."""

    def _seed_reports(self, output_dir, count=6, kb=150):
        """Six runs of two files each, oldest first, ~1.8 MB in total."""
        from datetime import timedelta

        stamps = []
        for index in range(count):
            when = datetime.now() - timedelta(days=count - index)
            stamp = when.strftime("%Y%m%d-%H%M%S")
            stamps.append(stamp)
            for suffix, filler in ((".json", b"x"), (".csv", b"y")):
                (output_dir / f"capture-report-{stamp}{suffix}").write_bytes(filler * (kb * 1024))
        return stamps

    def test_the_cap_forgets_the_oldest_runs(self, settings, tmp_output):
        from app.core import retention

        lines = []
        stamps = self._seed_reports(tmp_output)
        settings.history_retention_mb = 1
        run_engine(
            settings,
            ["https://example.com"],
            PageScript(),
            log=lambda level, message: lines.append(message),
        )
        names = {path.name for path in retention.report_files(tmp_output)}
        assert f"capture-report-{stamps[0]}.json" not in names  # the oldest run is gone
        assert f"capture-report-{stamps[-1]}.json" in names  # the newest survived
        assert all(  # a run is forgotten whole, never half of one
            (f"capture-report-{stamp}.json" in names) == (f"capture-report-{stamp}.csv" in names)
            for stamp in stamps
        )
        assert any("to keep the history under 1 MB" in line for line in lines)

    def test_without_a_cap_the_reports_stay(self, settings, tmp_output):
        from app.core import retention

        self._seed_reports(tmp_output)
        settings.history_retention_mb = 0
        settings.write_report = False
        run_engine(settings, ["https://example.com"], PageScript())
        assert len(retention.report_files(tmp_output)) == 12  # six runs, untouched

    def test_a_folder_under_the_cap_logs_nothing(self, settings, tmp_output):
        lines = []
        settings.history_retention_mb = 50
        run_engine(
            settings,
            ["https://example.com"],
            PageScript(),
            log=lambda level, message: lines.append(message),
        )
        assert not any("keep the history under" in line for line in lines)


class TestWatchdogStaleHistory:
    """The watchdog also notices a bot that runs but never captures anything."""

    def _seed_report(self, output_dir, when, status="success", url="https://a.example.com"):
        import json
        from datetime import timedelta  # noqa: F401 - kept for the callers

        payload = {
            "generated_at": when.isoformat(timespec="seconds"),
            "results": [{"url": url, "status": status, "diff": 0.0}],
        }
        path = output_dir / f"capture-report-{when:%Y%m%d-%H%M%S}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _watch(self, settings, output_dir, lines):
        engine = CaptureEngine(
            settings,
            log=lambda level, message: lines.append(message),
            now=datetime.now,
        )
        engine._watch_history(Path(output_dir))

    def test_an_old_success_warns(self, settings, tmp_output):
        from datetime import timedelta

        lines = []
        settings.watchdog_stale_minutes = 60
        self._seed_report(tmp_output, datetime.now() - timedelta(hours=3))
        self._watch(settings, tmp_output, lines)
        assert any("no successful capture in the last 60 minute(s)" in line for line in lines)

    def test_a_fresh_success_is_silent(self, settings, tmp_output):
        lines = []
        settings.watchdog_stale_minutes = 60
        self._seed_report(tmp_output, datetime.now())
        self._watch(settings, tmp_output, lines)
        assert not any("[watchdog]" in line for line in lines)

    def test_a_failed_capture_does_not_count_as_fresh(self, settings, tmp_output):
        from datetime import timedelta

        lines = []
        settings.watchdog_stale_minutes = 60
        self._seed_report(tmp_output, datetime.now() - timedelta(hours=3))
        self._seed_report(tmp_output, datetime.now(), status="failed")
        self._watch(settings, tmp_output, lines)
        assert any("[watchdog]" in line for line in lines)

    def test_an_empty_history_is_not_an_alarm(self, settings, tmp_output):
        lines = []
        settings.watchdog_stale_minutes = 60
        self._watch(settings, tmp_output, lines)
        assert lines == []

    def test_a_wildly_old_timestamp_counts_as_stale(self, settings, tmp_output):
        lines = []
        settings.watchdog_stale_minutes = 60
        self._seed_report(tmp_output, datetime(2020, 1, 1, 12, 0))
        self._watch(settings, tmp_output, lines)
        assert any("old (https://a.example.com" in line for line in lines)

    def test_without_a_limit_nothing_is_checked(self, settings, tmp_output):
        from datetime import timedelta

        lines = []
        settings.watchdog_stale_minutes = 0
        self._seed_report(tmp_output, datetime.now() - timedelta(days=30))
        self._watch(settings, tmp_output, lines)
        assert lines == []

    def test_a_successful_run_keeps_it_quiet(self, settings, tmp_output):
        from datetime import timedelta

        lines = []
        settings.watchdog_stale_minutes = 60
        self._seed_report(tmp_output, datetime.now() - timedelta(days=2))
        run_engine(
            settings,
            ["https://example.com"],
            PageScript(),
            log=lambda level, message: lines.append(message),
        )
        assert not any("[watchdog]" in line for line in lines)


class TestCapDropAlert:
    """When the history cap really drops runs, say so - and page when asked."""

    def _seed(self, output_dir, count=6, kb=150):
        from datetime import timedelta

        stamps = []
        for index in range(count):
            when = datetime.now() - timedelta(days=count - index)
            stamp = when.strftime("%Y%m%d-%H%M%S")
            stamps.append(stamp)
            for suffix, filler in ((".json", b"x"), (".csv", b"y")):
                (output_dir / f"capture-report-{stamp}{suffix}").write_bytes(filler * (kb * 1024))
        return stamps

    def test_the_dropped_runs_are_a_warning_with_advice(self, settings, tmp_output):
        lines = []
        self._seed(tmp_output)
        settings.history_retention_mb = 1
        settings.alert_enabled = False
        run_engine(
            settings,
            ["https://example.com"],
            PageScript(),
            log=lambda level, message: lines.append((level, message)),
        )
        dropped = [
            message
            for level, message in lines
            if level is LogLevel.WARNING and "history cap dropped" in message
        ]
        assert dropped, lines
        assert "raise the cap" in dropped[0]
        assert "to keep the history under 1 MB" in dropped[0]

    def test_an_alert_goes_out_when_alerting_is_switched_on(
        self, settings, tmp_output, monkeypatch
    ):
        from app.core import alerts

        notes = []
        monkeypatch.setattr(
            alerts,
            "notify_note",
            lambda settings, title, body: notes.append((title, body)) or [("webhook", True)],
        )
        lines = []
        self._seed(tmp_output)
        settings.history_retention_mb = 1
        settings.alert_enabled = True
        settings.alert_webhook_url = "https://hook.example.com"
        run_engine(
            settings,
            ["https://example.com"],
            PageScript(),
            log=lambda level, message: lines.append(message),
        )
        assert len(notes) == 1
        title, body = notes[0]
        assert title == "History cap dropped old runs"
        assert "history_retention_mb=1" in body
        assert "report file(s)" in body  # what exactly went
        assert any("alert sent" in line for line in lines)

    def test_a_row_only_trim_stays_an_info_line(self, settings, tmp_output):
        """The index trimming its own rows is routine; losing whole runs is not."""
        from app.core.store import HistoryStore

        lines = []
        settings.history_retention_mb = 1
        settings.alert_enabled = False
        rows = [
            {
                "timestamp": f"2026-01-{(index % 28) + 1:02d}T00:00:00",
                "url": "https://example.com/" + "p" * 400,  # a genuinely big index
                "label": "big",
                "status": "success",
                "diff": 0.1,
                "drift": None,
                "file": "",
            }
            for index in range(6000)
        ]
        with HistoryStore(tmp_output / "history.sqlite3") as store:
            store.replace_all(rows)
        run_engine(
            settings,
            ["https://example.com"],
            PageScript(),
            log=lambda level, message: lines.append((level, message)),
        )
        assert any(
            "Cleaned up" in message and "history row(s)" in message for _level, message in lines
        )
        assert not any(
            level is LogLevel.WARNING and "history cap" in message for level, message in lines
        )


class TestQuietHoursPerUrl:
    """``alert_quiet_urls``: one host can sleep while another keeps paging."""

    def _engine(self, settings, tmp_output, when, quiet_urls):
        settings.alert_enabled = True
        settings.alert_webhook_url = "https://hook"
        settings.output_dir = str(tmp_output)
        settings.alert_quiet_hours = "fri18:00-mon09:00"
        settings.alert_quiet_urls = quiet_urls
        return CaptureEngine(settings, now=lambda: when)

    def _result(self, url, diff=0.5):
        from app.core.engine import CaptureResult

        return CaptureResult(index=0, url=url, status=CaptureStatus.SUCCESS, diff=diff)

    def _notify(self, monkeypatch):
        from app.core import alerts

        calls = []
        monkeypatch.setattr(
            alerts,
            "notify",
            lambda s, changed, run_id="": (
                calls.append([item["url"] for item in changed]) or [("webhook", True)]
            ),
        )
        return calls

    def test_the_newsletter_pages_on_the_weekend_while_staging_waits(
        self, settings, tmp_output, monkeypatch
    ):
        from app.core import quiet

        calls = self._notify(monkeypatch)
        engine = self._engine(
            settings, tmp_output, datetime(2026, 5, 9, 12, 0), "news.example.com="
        )
        engine._maybe_send_alerts(
            [
                self._result("https://staging.example.com/a"),
                self._result("https://news.example.com/b"),
            ]
        )
        assert calls == [["https://news.example.com/b"]]
        queued = quiet.load(quiet.queue_path(tmp_output))
        assert [item["url"] for item in queued] == ["https://staging.example.com/a"]

    def test_a_held_alert_goes_out_once_its_own_window_ends(
        self, settings, tmp_output, monkeypatch
    ):
        from app.core import quiet

        self._notify(monkeypatch)
        saturday = self._engine(
            settings,
            tmp_output,
            datetime(2026, 5, 9, 12, 0),
            "staging.example.com=fri18:00-mon09:00",
        )
        saturday._maybe_send_alerts([self._result("https://staging.example.com/a")])

        calls = self._notify(monkeypatch)  # a fresh spy for the second run
        monday = CaptureEngine(settings, now=lambda: datetime(2026, 5, 11, 10, 0))
        monday._maybe_send_alerts([])  # nothing new, but the queue is now free to go
        assert calls == [["https://staging.example.com/a"]]
        assert not quiet.queue_path(tmp_output).exists()

    def test_a_url_without_a_rule_still_waits_for_the_global_window(
        self, settings, tmp_output, monkeypatch
    ):
        from app.core import quiet

        calls = self._notify(monkeypatch)
        engine = self._engine(
            settings, tmp_output, datetime(2026, 5, 9, 12, 0), "news.example.com="
        )
        engine._maybe_send_alerts([self._result("https://prod.example.com/c")])
        assert calls == []  # nothing was sent
        assert len(quiet.load(quiet.queue_path(tmp_output))) == 1


class TestArchiveInsteadOfDelete:
    """``history_archive`` zips the runs the size cap drops."""

    def _seed(self, output_dir, count=6, kb=150):
        from datetime import timedelta

        stamps = []
        for index in range(count):
            when = datetime.now() - timedelta(days=count - index)
            stamp = when.strftime("%Y%m%d-%H%M%S")
            stamps.append(stamp)
            for suffix, filler in ((".json", b"x"), (".csv", b"y")):
                (output_dir / f"capture-report-{stamp}{suffix}").write_bytes(filler * (kb * 1024))
        return stamps

    def test_the_dropped_runs_end_up_in_a_month_archive(self, settings, tmp_output):
        lines = []
        stamps = self._seed(tmp_output)
        settings.history_retention_mb = 1
        settings.history_archive = True
        settings.alert_enabled = False
        run_engine(
            settings,
            ["https://example.com"],
            PageScript(),
            log=lambda level, message: lines.append((level, message)),
        )
        month = f"{stamps[0][:4]}-{stamps[0][4:6]}"
        assert [path.name for path in tmp_output.glob("archive-*.zip")] == [f"archive-{month}.zip"]
        kept = [
            message for level, message in lines if level is LogLevel.INFO and "kept in" in message
        ]
        assert kept and f"archive-{month}.zip" in kept[0]

    def test_the_archive_holds_the_dropped_reports(self, settings, tmp_output):
        import zipfile

        stamps = self._seed(tmp_output)
        settings.history_retention_mb = 1
        settings.history_archive = True
        settings.alert_enabled = False
        run_engine(settings, ["https://example.com"], PageScript())
        with zipfile.ZipFile(
            tmp_output / f"archive-{stamps[0][:4]}-{stamps[0][4:6]}.zip"
        ) as handle:
            names = handle.namelist()
        assert f"capture-report-{stamps[0]}.json" in names
        assert f"capture-report-{stamps[0]}.csv" in names
        assert not (tmp_output / f"capture-report-{stamps[0]}.json").exists()

    def test_the_alert_body_names_the_archive(self, settings, tmp_output, monkeypatch):
        from app.core import alerts

        notes = []
        monkeypatch.setattr(
            alerts,
            "notify_note",
            lambda settings, title, body: notes.append((title, body)) or [("webhook", True)],
        )
        self._seed(tmp_output)
        settings.history_retention_mb = 1
        settings.history_archive = True
        settings.alert_enabled = True
        settings.alert_webhook_url = "https://hook.example.com"
        run_engine(settings, ["https://example.com"], PageScript())
        assert len(notes) == 1
        _title, body = notes[0]
        assert "They were archived into archive-" in body

    def test_the_advice_mentions_archiving_when_it_is_off(self, settings, tmp_output, monkeypatch):
        from app.core import alerts

        notes = []
        monkeypatch.setattr(
            alerts,
            "notify_note",
            lambda settings, title, body: notes.append((title, body)) or [("webhook", True)],
        )
        self._seed(tmp_output)
        settings.history_retention_mb = 1
        settings.history_archive = False
        settings.alert_enabled = True
        settings.alert_webhook_url = "https://hook.example.com"
        run_engine(settings, ["https://example.com"], PageScript())
        assert "history_archive=on does it for you" in notes[0][1]
        assert not list(tmp_output.glob("archive-*.zip"))


class TestStorageSamplePerRun:
    """Each run records what the folder cost, so the trend is measured."""

    @staticmethod
    def _samples(output_dir):
        from app.core.store import HistoryStore

        with HistoryStore(output_dir / "history.sqlite3") as store:
            return store.storage_series()

    @staticmethod
    def _run(settings):
        return run_engine(settings, ["https://example.com"], PageScript())

    def test_a_run_records_one_sample(self, settings, tmp_output):
        settings.auto_dashboard = False
        self._run(settings)
        rows = self._samples(tmp_output)
        assert len(rows) == 1
        assert rows[0]["total"] > 0
        assert rows[0]["history"] > 0
        assert rows[0]["reports"] == 2  # the run's .json + its .csv twin
        assert rows[0]["index_bytes"] > 0

    def test_the_sample_describes_the_folder_the_run_wrote(self, settings, tmp_output):
        settings.auto_dashboard = False
        self._run(settings)
        row = self._samples(tmp_output)[0]
        from app.core.dashboard import storage_stats

        stats = storage_stats(tmp_output)
        # The sample is taken while the run is still finishing, so the folder may
        # have grown a little since (the dashboard, a VACUUM); it must never be
        # smaller than what the run had just written, and it must agree on shape.
        assert 0 < row["total"] <= stats["total"]
        assert row["history"] <= stats["history"]
        assert row["reports"] == stats["reports"] == 2

    def test_two_runs_leave_two_samples(self, settings, tmp_output):
        settings.auto_dashboard = False
        self._run(settings)
        self._run(settings)
        assert len(self._samples(tmp_output)) == 2

    def test_capture_retention_prunes_the_samples_with_the_rows(self, settings, tmp_output):
        from app.core.store import HistoryStore

        settings.auto_dashboard = False
        self._run(settings)
        with HistoryStore(tmp_output / "history.sqlite3") as store:
            store.add_storage_sample("2020-01-01T09:00:00", {"total": 1})
            assert store.count_storage_samples() == 2
            store.prune(1)
            left = store.storage_series()
        assert len(left) == 1  # only the sample this run recorded survived

    def test_one_sample_is_not_a_trend_yet(self, settings, tmp_output):
        settings.auto_dashboard = True
        self._run(settings)
        page = (tmp_output / "dashboard.html").read_text(encoding="utf-8")
        assert "<dt>Samples</dt><dd>1 since " in page  # the panel shows it ...
        from app.core.dashboard import storage_stats

        # ... but one point cannot be fitted, so the runs still answer.
        assert storage_stats(tmp_output)["growth"]["source"] == "reports"

    def test_two_samples_a_day_apart_are_a_measured_trend(self, settings, tmp_output):
        from datetime import timedelta

        from app.core.dashboard import storage_stats
        from app.core.store import HistoryStore

        settings.auto_dashboard = False
        self._run(settings)  # leaves the history plus one sample behind
        with HistoryStore(tmp_output / "history.sqlite3") as store:
            moment = datetime.fromisoformat(store.storage_series()[0]["taken_at"])
            store._conn.execute("DELETE FROM storage_samples")
            for index, when in enumerate((moment, moment + timedelta(days=1))):
                store.add_storage_sample(
                    when.isoformat(timespec="seconds"),
                    {
                        "total": 500_000 + 50_000 * index,
                        "screenshots": 400_000 + 30_000 * index,
                        "history": 100_000 + 20_000 * index,
                        "index": 4096,
                        "reports": index + 2,
                    },
                )
        growth = storage_stats(tmp_output)["growth"]
        assert growth["source"] == "samples"
        assert growth["history_bytes_per_day"] == pytest.approx(20_000.0)


class TestChannelsInTheEngine:
    """A channels file takes the run over: match, threshold and quiet per channel."""

    def _engine(self, settings, tmp_output, when, monkeypatch, text):
        from app.core import alerts

        sent: list[tuple] = []
        monkeypatch.setattr(
            alerts,
            "send_webhook",
            lambda url, payload, timeout=10.0: sent.append((url, payload)) or True,
        )
        target = tmp_output / "channels.toml"
        target.write_text(text, encoding="utf-8")
        settings.alert_enabled = True
        settings.alert_webhook_url = "https://hooks.example.com/default"
        settings.output_dir = str(tmp_output)
        settings.alert_channels = str(target)
        return CaptureEngine(settings, now=lambda: when), sent

    def _result(self, url="https://a.com", diff=0.5):
        from app.core.engine import CaptureResult

        return CaptureResult(index=0, url=url, status=CaptureStatus.SUCCESS, diff=diff)

    def test_the_file_replaces_the_default_webhook(self, settings, tmp_output, monkeypatch):
        engine, sent = self._engine(
            settings,
            tmp_output,
            datetime(2026, 5, 5, 12, 0),
            monkeypatch,
            '[ops]\nurl = "https://hooks.example.com/ops"\n',
        )
        engine._maybe_send_alerts([self._result()])
        assert [url for url, _ in sent] == ["https://hooks.example.com/ops"]
        assert any("Alert (ops:webhook) sent." in line for line in engine.log_lines)

    def test_a_threshold_only_applies_to_its_channel(self, settings, tmp_output, monkeypatch):
        engine, sent = self._engine(
            settings,
            tmp_output,
            datetime(2026, 5, 5, 12, 0),
            monkeypatch,
            '[picky]\nurl = "https://hooks/picky"\nmin_diff = 0.2\n'
            '\n[wide]\nurl = "https://hooks/wide"\n',
        )
        engine._maybe_send_alerts([self._result(diff=0.05)])
        assert [url for url, _ in sent] == ["https://hooks/wide"]

    def test_the_global_mute_list_steps_aside(self, settings, tmp_output, monkeypatch):
        engine, sent = self._engine(
            settings,
            tmp_output,
            datetime(2026, 5, 5, 12, 0),
            monkeypatch,
            '[ops]\nurl = "https://hooks/ops"\n',
        )
        settings.alert_mute_urls = "a.com"  # the file owns the routing now
        engine._maybe_send_alerts([self._result()])
        assert [url for url, _ in sent] == ["https://hooks/ops"]

    def test_a_quiet_channel_queues_its_own_alerts(self, settings, tmp_output, monkeypatch):
        from app.core import quiet

        engine, sent = self._engine(
            settings,
            tmp_output,
            datetime(2026, 5, 5, 23, 30),
            monkeypatch,
            '[ops]\nurl = "https://hooks/ops"\nquiet = "22:00-07:00"\n'
            '\n[news]\nurl = "https://hooks/news"\nmatch = "news"\n',
        )
        engine._maybe_send_alerts([self._result()])
        assert sent == []
        queued = quiet.load(quiet.queue_path(tmp_output))
        assert [item["channels"] for item in queued] == [["ops"]]
        assert any("(ops:queued) sent" in line for line in engine.log_lines)

    def test_the_morning_run_releases_the_channel_s_queue(self, settings, tmp_output, monkeypatch):
        engine, _sent = self._engine(
            settings,
            tmp_output,
            datetime(2026, 5, 5, 23, 30),
            monkeypatch,
            '[ops]\nurl = "https://hooks/ops"\nquiet = "22:00-07:00"\n',
        )
        engine._maybe_send_alerts([self._result()])

        morning, sent = self._engine(
            settings,
            tmp_output,
            datetime(2026, 5, 6, 8, 0),
            monkeypatch,
            '[ops]\nurl = "https://hooks/ops"\nquiet = "22:00-07:00"\n',
        )
        morning._maybe_send_alerts([])  # nothing changed overnight-except-this
        assert [url for url, _ in sent] == ["https://hooks/ops"]
        assert any(
            "Quiet hours over for 1 queued alert(s) (ops)." in line for line in morning.log_lines
        )

    def test_a_broken_file_is_a_warning_not_a_crash(self, settings, tmp_output, monkeypatch):
        engine, sent = self._engine(
            settings,
            tmp_output,
            datetime(2026, 5, 5, 12, 0),
            monkeypatch,
            "[ops]\nquiett = 1\n",
        )
        engine._maybe_send_alerts([self._result()])
        assert sent == []
        assert any("Channels file ignored" in line for line in engine.log_lines)
        assert any("channels" in line and "failed" in line for line in engine.log_lines)

    def test_a_channel_that_matches_nothing_sends_nothing(self, settings, tmp_output, monkeypatch):
        engine, sent = self._engine(
            settings,
            tmp_output,
            datetime(2026, 5, 5, 12, 0),
            monkeypatch,
            '[ops]\nurl = "https://hooks/ops"\nmatch = "nowhere.example.com"\n',
        )
        engine._maybe_send_alerts([self._result()])
        assert sent == []


class TestHeartbeatInTheEngine:
    """A run checks the heartbeats, even (especially) when nothing changed."""

    def _engine(self, settings, tmp_output, when, monkeypatch, text):
        from app.core import alerts

        sent: list[dict] = []
        monkeypatch.setattr(
            alerts,
            "send_webhook",
            lambda url, payload, timeout=10.0: sent.append(payload) or True,
        )
        target = tmp_output / "channels.toml"
        target.write_text(text, encoding="utf-8")
        settings.alert_enabled = True
        settings.output_dir = str(tmp_output)
        settings.alert_channels = str(target)
        return CaptureEngine(settings, now=lambda: when), sent

    def test_a_quiet_week_still_produces_a_note(self, settings, tmp_output, monkeypatch):
        engine, sent = self._engine(
            settings,
            tmp_output,
            datetime(2026, 10, 5, 9, 30),
            monkeypatch,
            '[ops]\nurl = "https://hooks/ops"\nheartbeat = "mon 09:00"\n',
        )
        engine._maybe_send_heartbeats()
        assert [payload["event"] for payload in sent] == ["notice"]
        assert any("Alert (ops:heartbeat) sent." in line for line in engine.log_lines)

    def test_a_run_with_nothing_to_report_is_not_a_second_beat(
        self, settings, tmp_output, monkeypatch
    ):
        engine, sent = self._engine(
            settings,
            tmp_output,
            datetime(2026, 10, 5, 9, 30),
            monkeypatch,
            '[ops]\nurl = "https://hooks/ops"\nheartbeat = "mon 09:00"\n',
        )
        engine._maybe_send_heartbeats()
        engine._maybe_send_heartbeats()  # another run the same morning
        assert len(sent) == 1

    def test_a_failed_beat_is_logged_as_a_warning(self, settings, tmp_output, monkeypatch):
        from app.core import alerts

        monkeypatch.setattr(alerts, "send_webhook", lambda url, payload, timeout=10.0: False)
        target = tmp_output / "channels.toml"
        target.write_text(
            '[ops]\nurl = "https://hooks/ops"\nheartbeat = "09:00"\n', encoding="utf-8"
        )
        settings.alert_enabled = True
        settings.output_dir = str(tmp_output)
        settings.alert_channels = str(target)
        engine = CaptureEngine(settings, now=lambda: datetime(2026, 10, 5, 9, 30))
        engine._maybe_send_heartbeats()
        assert any("Alert (ops:heartbeat) failed." in line for line in engine.log_lines)

    def test_without_a_channels_file_nothing_is_checked(self, settings, tmp_output, monkeypatch):
        engine, sent = self._engine(
            settings, tmp_output, datetime(2026, 10, 5, 9, 30), monkeypatch, '[ops]\nurl = "x"\n'
        )
        settings.alert_channels = ""
        engine._maybe_send_heartbeats()
        assert sent == []

    def test_alerts_switched_off_switches_the_beats_off(self, settings, tmp_output, monkeypatch):
        engine, sent = self._engine(
            settings,
            tmp_output,
            datetime(2026, 10, 5, 9, 30),
            monkeypatch,
            '[ops]\nurl = "https://hooks/ops"\nheartbeat = "09:00"\n',
        )
        settings.alert_enabled = False
        engine._maybe_send_heartbeats()
        assert sent == []

    def test_a_broken_file_never_breaks_the_run(self, settings, tmp_output, monkeypatch):
        engine, sent = self._engine(
            settings, tmp_output, datetime(2026, 10, 5, 9, 30), monkeypatch, "[ops]\nquiett = 1\n"
        )
        engine._maybe_send_heartbeats()  # must not raise
        assert sent == []


class TestSiteSizeCaps:
    """One site's own budget: the chatty host is trimmed, its neighbour is not."""

    @staticmethod
    def _shots(output_dir, label, count=6, kb=300):
        for index in range(count):
            name = f"{index + 1:03d}_{label}_2026{index + 1:02d}01-090000.png"
            (output_dir / name).write_bytes(b"x" * (kb * 1024))

    def test_a_site_over_its_budget_is_trimmed_after_a_run(self, settings, tmp_output):
        lines = []
        self._shots(tmp_output, "news_example_com")
        settings.site_caps = "news.example.com=1"
        run_engine(
            settings,
            ["https://example.com"],
            PageScript(page_width=1200, page_height=900),
            log=lambda level, message: lines.append(message),
        )
        left = sorted(path.name for path in tmp_output.glob("*_news_example_com_*.png"))
        assert len(left) == 3  # three of the six oldest went
        assert any("Site cap: news_example_com: deleted 3 file(s)" in line for line in lines)

    def test_a_neighbour_inside_its_budget_is_untouched(self, settings, tmp_output):
        self._shots(tmp_output, "news_example_com")
        self._shots(tmp_output, "shop_example_com", count=2, kb=100)
        settings.site_caps = "news.example.com=1"
        run_engine(settings, ["https://example.com"], PageScript(), log=lambda level, message: None)
        assert len(list(tmp_output.glob("*_shop_example_com_*.png"))) == 2

    def test_without_a_budget_nothing_is_trimmed(self, settings, tmp_output):
        self._shots(tmp_output, "news_example_com")
        settings.site_caps = ""
        run_engine(settings, ["https://example.com"], PageScript())
        assert len(list(tmp_output.glob("*_news_example_com_*.png"))) == 6

    def test_a_reference_that_keeps_the_site_over_is_explained(self, settings, tmp_output):
        lines = []
        self._shots(tmp_output, "news_example_com", count=12, kb=100)
        (tmp_output / "latest_news_example_com.png").write_bytes(b"y" * 3 * 1024 * 1024)
        settings.site_caps = "news.example.com=1"
        run_engine(
            settings,
            ["https://example.com"],
            PageScript(),
            log=lambda level, message: lines.append(message),
        )
        assert (tmp_output / "latest_news_example_com.png").exists()
        assert any("which are never deleted" in line for line in lines)

    def test_the_budget_travels_with_the_storage_caps(self, settings):
        settings.site_caps = "news.example.com=200"
        settings.screenshot_retention_mb = 500
        caps = CaptureEngine(settings)._storage_caps()
        assert caps["screenshots_mb"] == 500
        assert caps["site_caps"] == "news.example.com=200"


class TestHidingNoisyElements:
    """Cookie banners, ad slots and clocks: hidden before the shutter."""

    def test_the_selectors_are_hidden_on_the_page(self, settings):
        settings.hide_selectors = ".cookie-banner, #ad-slot\n.clock"
        _, _, calls = run_engine(settings, ["https://example.com"], PageScript())

        assert len(calls.style_tags) == 1
        css = calls.style_tags[0]
        assert ".cookie-banner,#ad-slot,.clock{display:none" in css
        assert "#ad-slot" in css and ".clock" in css

    def test_nothing_is_hidden_when_nothing_was_asked_for(self, settings):
        _, _, calls = run_engine(settings, ["https://example.com"], PageScript())
        assert calls.style_tags == []

    def test_the_hidden_rule_is_added_once_per_page(self, settings):
        settings.hide_selectors = ".banner"
        settings.scroll_to_load_lazy_content = True
        script = PageScript(page_width=1200, page_height=6000)
        _, _, calls = run_engine(settings, ["https://example.com", "https://example.com/x"], script)

        # two URLs, two pages, two rules - not one per stitched segment
        assert len(calls.style_tags) == 2
        assert all(
            rule == ".banner{display:none !important;visibility:hidden !important;}"
            for rule in calls.style_tags
        )

    def test_a_page_that_refuses_the_style_does_not_fail_the_capture(self, settings):
        settings.hide_selectors = ".banner"
        script = PageScript(style_error=RuntimeError("Refused to apply style"))

        engine, summary, calls = run_engine(settings, ["https://example.com"], script)

        assert summary.succeeded == 1, "hiding is a nicety, not a step"
        assert calls.screenshots, "the screenshot still happened"
        assert any("Could not hide" in line for line in engine.log_lines)

def test_session_metrics_records_after_run(tmp_path):
    from app.core.metrics import SessionMetrics
    m = SessionMetrics(str(tmp_path))
    m.record("https://t", 1200, "ok", 4, 2)
    assert (tmp_path / "session_metrics.json").exists()

def test_metrics_alert_after_repeated_failures(tmp_path):
    from app.core.metrics import SessionMetrics
    m = SessionMetrics(str(tmp_path))
    for _ in range(4):
        m.record("https://f", 100, "fail", 0, 0)
    assert "ALERT" in m.alert_on_failures(3)
