"""Tests for the periodic digest (app.core.digest)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from app.core import digest


def _report(directory, name, when, results):
    (directory / f"capture-report-{name}.json").write_text(
        json.dumps({"generated_at": when, "results": results}), encoding="utf-8"
    )


_seq = {"n": 0}


def _seed(directory, days_ago=1, url="https://a.example.com", diff=0.5, status="success"):
    """Write one report file. Each call gets a unique name so nothing collides."""
    when = (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds")
    _seq["n"] += 1
    stamp = when.replace(":", "").replace("-", "")[:15]
    _report(
        directory,
        f"{stamp}-{_seq['n']:03d}",
        when,
        [{"url": url, "status": status, "diff": diff, "file_path": ""}],
    )


class TestCollectStats:
    def test_counts_recent_captures_and_changes(self, tmp_path):
        _seed(tmp_path, days_ago=1, diff=0.4)
        _seed(tmp_path, days_ago=2, diff=0.0, url="https://b.example.com")
        stats = digest.collect_stats(tmp_path, days=7)
        assert stats["captures"] == 2
        assert stats["changed"] == 1
        assert stats["sites"] == 2

    def test_window_excludes_old_runs(self, tmp_path):
        _seed(tmp_path, days_ago=1)
        _seed(tmp_path, days_ago=30, url="https://old.example.com")
        assert digest.collect_stats(tmp_path, days=7)["captures"] == 1
        assert digest.collect_stats(tmp_path, days=60)["captures"] == 2

    def test_failures_are_counted(self, tmp_path):
        _seed(tmp_path, days_ago=1, status="failed", diff=None)
        assert digest.collect_stats(tmp_path, days=7)["failed"] == 1

    def test_top_sites_are_ranked_by_changes(self, tmp_path):
        for _ in range(3):
            _seed(tmp_path, days_ago=1, url="https://busy.example.com", diff=0.9)
        _seed(tmp_path, days_ago=1, url="https://quiet.example.com", diff=0.9)
        stats = digest.collect_stats(tmp_path, days=7)
        assert stats["top_sites"][0] == ("https://busy.example.com", 3)

    def test_empty_folder_is_safe(self, tmp_path):
        stats = digest.collect_stats(tmp_path / "nope", days=7)
        assert stats["captures"] == 0 and stats["top_sites"] == []


class TestFormatDigest:
    def test_body_mentions_the_key_numbers(self, tmp_path):
        _seed(tmp_path, days_ago=1, diff=0.5)
        body = digest.build_digest(tmp_path, days=7)
        assert "last 7 day(s)" in body
        assert "Captures recorded : 1" in body
        assert "Changes detected  : 1" in body
        assert "a.example.com" in body

    def test_no_changes_says_none(self, tmp_path):
        _seed(tmp_path, days_ago=1, diff=0.0)
        body = digest.build_digest(tmp_path, days=7)
        assert "Most changed sites:" in body and "(none)" in body

    def test_stale_baselines_are_listed(self, tmp_path):
        import os
        import time

        from app.core import baseline

        _seed(tmp_path, days_ago=1)
        url = "https://a.example.com"
        baseline.latest_path(tmp_path, url).write_bytes(b"png")
        pinned = baseline.pin_baseline(tmp_path, url)
        when = time.time() - 40 * 86400
        os.utime(pinned, (when, when))
        body = digest.build_digest(tmp_path, days=7)
        assert "Stale baselines" in body and url in body


class TestSendDigest:
    def test_sends_through_the_alert_smtp_settings(self, tmp_path, monkeypatch):
        from app.core import alerts
        from app.core.settings import CaptureSettings

        _seed(tmp_path, days_ago=1)
        sent = {}

        def fake_send_email(
            host, port, user, password, to, subject, body, timeout=10.0, attachments=None
        ):
            sent.update(host=host, port=port, to=to, subject=subject, body=body)
            return True

        monkeypatch.setattr(alerts, "send_email", fake_send_email)
        settings = CaptureSettings(
            output_dir=str(tmp_path),
            smtp_host="smtp.example.com",
            smtp_port=2525,
            smtp_user="bot@example.com",
            alert_email_to="ops@example.com",
        )
        assert digest.send_digest(settings, tmp_path, days=7) is True
        assert sent["to"] == "ops@example.com" and sent["port"] == 2525
        assert "digest" in sent["subject"].lower()
        assert "Captures recorded" in sent["body"]

    def test_missing_smtp_config_raises(self, tmp_path):
        from app.core.settings import CaptureSettings

        with pytest.raises(ValueError):
            digest.send_digest(CaptureSettings(output_dir=str(tmp_path)), tmp_path)


class TestAttachments:
    def test_dashboard_attachment_is_html(self, tmp_path):
        _seed(tmp_path, days_ago=1)
        filename, data, subtype = digest.build_attachment(tmp_path, "dashboard")
        assert filename == "dashboard.html" and subtype == "html"
        text = data.decode("utf-8")
        assert "<!doctype html>" in text.lower() and "a.example.com" in text

    def test_csv_attachment_has_a_header_and_a_row(self, tmp_path):
        _seed(tmp_path, days_ago=1)
        filename, data, subtype = digest.build_attachment(tmp_path, "csv")
        assert filename == "history.csv" and subtype == "csv"
        lines = data.decode("utf-8").strip().splitlines()
        assert lines[0].startswith("timestamp,url,label,status,diff,drift,file")
        assert len(lines) == 2 and "a.example.com" in lines[1]

    def test_none_means_no_attachment(self, tmp_path):
        assert digest.build_attachment(tmp_path, "none") is None

    def test_csv_escapes_quotes(self, tmp_path):
        _seed(tmp_path, days_ago=1, url='https://x.example.com/"quoted"')
        data = digest.history_csv(tmp_path).decode("utf-8")
        assert '""quoted""' in data

    def test_send_digest_forwards_the_attachment(self, tmp_path, monkeypatch):
        from app.core import alerts
        from app.core.settings import CaptureSettings

        _seed(tmp_path, days_ago=1)
        sent = {}

        def fake_send_email(
            host, port, user, password, to, subject, body, timeout=10.0, attachments=None
        ):
            sent["attachments"] = attachments
            return True

        monkeypatch.setattr(alerts, "send_email", fake_send_email)
        settings = CaptureSettings(
            output_dir=str(tmp_path), smtp_host="h", alert_email_to="ops@example.com"
        )
        assert digest.send_digest(settings, tmp_path, days=7, attach="csv") is True
        assert sent["attachments"][0][0] == "history.csv"

    def test_send_digest_can_skip_the_attachment(self, tmp_path, monkeypatch):
        from app.core import alerts
        from app.core.settings import CaptureSettings

        _seed(tmp_path, days_ago=1)
        sent = {}
        monkeypatch.setattr(
            alerts,
            "send_email",
            lambda *a, **k: sent.update(attachments=k.get("attachments")) or True,
        )
        settings = CaptureSettings(
            output_dir=str(tmp_path), smtp_host="h", alert_email_to="ops@example.com"
        )
        assert digest.send_digest(settings, tmp_path, days=7, attach="none") is True
        assert sent["attachments"] is None


class TestMailerAttachments:
    def test_send_email_builds_a_multipart_message(self, monkeypatch):
        import smtplib

        from app.core import alerts

        captured = {}

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                captured["host"] = host

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def login(self, user, password):
                captured["login"] = user

            def send_message(self, message):
                captured["message"] = message

        monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
        ok = alerts.send_email(
            "smtp.example.com",
            587,
            "bot@example.com",
            "secret",
            "ops@example.com",
            "subject",
            "body text",
            attachments=[("dashboard.html", b"<html>hi</html>", "html")],
        )
        assert ok is True
        message = captured["message"]
        names = [part.get_filename() for part in message.iter_attachments()]
        assert names == ["dashboard.html"]
        part = next(message.iter_attachments())
        assert part.get_content_type() == "text/html"
        assert b"<html>hi</html>" in part.get_payload(decode=True)

    def test_send_email_without_attachments_stays_simple(self, monkeypatch):
        import smtplib

        from app.core import alerts

        captured = {}

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def login(self, user, password):
                pass

            def send_message(self, message):
                captured["message"] = message

        monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
        alerts.send_email("h", 587, "", "", "ops@example.com", "s", "body")
        assert list(captured["message"].iter_attachments()) == []


class TestComparison:
    def _both(self, tmp_path):
        base_dir = tmp_path / "base"
        head_dir = tmp_path / "head"
        base_dir.mkdir()
        head_dir.mkdir()
        _seed(base_dir, days_ago=1, url="https://a.example.com", diff=0.2)
        _seed(head_dir, days_ago=1, url="https://a.example.com", diff=0.2)
        _seed(head_dir, days_ago=1, url="https://b.example.com", diff=0.9)
        return base_dir, head_dir

    def test_compare_stats_reports_deltas(self):
        base = {
            "captures": 1,
            "changes": 1,
            "failed": 0,
            "sites": 1,
            "top_sites": [],
            "stale_baselines": [],
        }
        head = {
            "captures": 3,
            "changes": 2,
            "failed": 1,
            "sites": 2,
            "top_sites": [],
            "stale_baselines": [],
        }
        lines = digest.compare_stats(base, head)
        assert "Captures : 1 -> 3 (+2)" in lines
        assert "Failures : 0 -> 1 (+1)" in lines

    def test_newly_busy_sites_are_called_out(self):
        base = {
            "captures": 0,
            "changes": 0,
            "failed": 0,
            "sites": 0,
            "top_sites": [("https://a.example.com", 1)],
            "stale_baselines": [],
        }
        head = {
            "captures": 1,
            "changes": 1,
            "failed": 0,
            "sites": 1,
            "top_sites": [("https://a.example.com", 1), ("https://b.example.com", 1)],
            "stale_baselines": [],
        }
        lines = digest.compare_stats(base, head)
        assert any("Newly busy sites: https://b.example.com" in line for line in lines)

    def test_build_comparison_reads_both_folders(self, tmp_path):
        base_dir, head_dir = self._both(tmp_path)
        text = digest.build_comparison(head_dir, base_dir, days=7)
        assert text.startswith("vs base branch")
        assert "Captures : 1 -> 2 (+1)" in text

    def test_no_changes_renders_a_zero_delta(self, tmp_path):
        base_dir, _ = self._both(tmp_path)
        text = digest.build_comparison(base_dir, base_dir, days=7)
        assert "(0)" in text

    def test_send_digest_appends_the_comparison(self, tmp_path, monkeypatch):
        from app.core import alerts
        from app.core.settings import CaptureSettings

        _seed(tmp_path, days_ago=1)
        sent = {}
        monkeypatch.setattr(
            alerts,
            "send_email",
            lambda *a, **k: sent.update(body=a[6]) or True,
        )
        settings = CaptureSettings(
            output_dir=str(tmp_path), smtp_host="h", alert_email_to="ops@example.com"
        )
        assert digest.send_digest(settings, tmp_path, days=7, extra="vs base branch\nX") is True
        assert "vs base branch" in sent["body"]


class TestDriftSection:
    def _with_baseline(self, tmp_path, reverse=True, url="https://a.example.com"):
        from app.core import baseline
        from tests.test_baseline import _gradient

        _seed(tmp_path, days_ago=1, url=url)
        _gradient(baseline.latest_path(tmp_path, url))
        baseline.pin_baseline(tmp_path, url)
        _gradient(baseline.latest_path(tmp_path, url), reverse=reverse)

    def test_top_drift_is_collected(self, tmp_path):
        self._with_baseline(tmp_path)
        stats = digest.collect_stats(tmp_path, days=7)
        assert stats["top_drift"] and stats["top_drift"][0][0] == "https://a.example.com"
        assert stats["top_drift"][0][1] > 0.5

    def test_drift_is_ranked_descending(self, tmp_path):
        self._with_baseline(tmp_path, url="https://big.example.com")
        self._with_baseline(tmp_path, reverse=False, url="https://flat.example.com")
        drift = digest.collect_stats(tmp_path, days=7)["top_drift"]
        assert [url for url, _ in drift] == ["https://big.example.com"]

    def test_body_lists_the_drift_section(self, tmp_path):
        self._with_baseline(tmp_path)
        body = digest.build_digest(tmp_path, days=7)
        assert "Top drift vs pinned baseline:" in body
        assert "a.example.com" in body

    def test_no_baselines_means_no_section(self, tmp_path):
        _seed(tmp_path, days_ago=1)
        body = digest.build_digest(tmp_path, days=7)
        assert "Top drift vs pinned baseline:" not in body
        assert digest.collect_stats(tmp_path, days=7)["top_drift"] == []


class TestAttachmentSizeGuard:
    def _big_dashboard(self, tmp_path):
        # A large history makes the dashboard HTML grow past a tiny limit.
        for index in range(40):
            _seed(tmp_path, days_ago=1, url=f"https://site{index}.example.com", diff=0.1)

    def test_attachment_is_dropped_over_the_limit(self, tmp_path):
        self._big_dashboard(tmp_path)
        full = digest.build_attachment(tmp_path, "dashboard")
        assert len(full[1]) > 2048
        assert digest.build_attachment(tmp_path, "dashboard", max_bytes=2048) is None

    def test_small_attachments_are_kept(self, tmp_path):
        self._big_dashboard(tmp_path)
        assert digest.build_attachment(tmp_path, "dashboard", max_bytes=10**7) is not None

    def test_skip_note_explains_the_drop(self, tmp_path):
        self._big_dashboard(tmp_path)
        note = digest.attachment_skip_note(tmp_path, "dashboard", 2048)
        assert "attachment was skipped" in note and "limit" in note

    def test_no_note_when_the_attachment_fits(self, tmp_path):
        self._big_dashboard(tmp_path)
        assert digest.attachment_skip_note(tmp_path, "dashboard", 10**7) == ""
        assert digest.attachment_skip_note(tmp_path, "none", 1) == ""
        assert digest.attachment_skip_note(tmp_path, "dashboard", 0) == ""

    def test_send_digest_drops_it_and_says_so(self, tmp_path, monkeypatch):
        from app.core import alerts
        from app.core.settings import CaptureSettings

        self._big_dashboard(tmp_path)
        sent = {}

        def fake_send_email(
            host, port, user, password, to, subject, body, timeout=10.0, attachments=None
        ):
            sent["attachments"] = attachments
            sent["body"] = body
            return True

        monkeypatch.setattr(alerts, "send_email", fake_send_email)
        settings = CaptureSettings(
            output_dir=str(tmp_path), smtp_host="h", alert_email_to="ops@example.com"
        )
        assert digest.send_digest(settings, tmp_path, days=7, max_attachment_bytes=2048) is True
        assert sent["attachments"] is None
        assert "attachment was skipped" in sent["body"]

    def test_csv_attachment_respects_the_limit_too(self, tmp_path):
        self._big_dashboard(tmp_path)
        assert digest.build_attachment(tmp_path, "csv", max_bytes=64) is None
        assert digest.build_attachment(tmp_path, "csv", max_bytes=10**7) is not None


class TestDownloadLinks:
    def test_link_lines_point_at_the_api_routes(self):
        lines = digest.link_lines("http://reports.local:8765/", days=14)
        joined = "\n".join(lines)
        assert "http://reports.local:8765/" in joined
        assert "/api/history" in joined and "/api/trend" in joined
        assert "?days=14" in joined

    def test_no_base_means_no_links(self):
        assert digest.link_lines("", days=7) == []
        assert digest.link_lines("   ", days=7) == []

    def test_send_digest_appends_the_links(self, tmp_path, monkeypatch):
        from app.core import alerts
        from app.core.settings import CaptureSettings

        _seed(tmp_path, days_ago=1)
        sent = {}
        monkeypatch.setattr(alerts, "send_email", lambda *a, **k: sent.update(body=a[6]) or True)
        settings = CaptureSettings(
            output_dir=str(tmp_path), smtp_host="h", alert_email_to="ops@example.com"
        )
        assert digest.send_digest(settings, tmp_path, days=7, link_base="http://host:8765") is True
        assert "/api/trend" in sent["body"]

    def test_links_and_attachment_can_be_combined(self, tmp_path, monkeypatch):
        from app.core import alerts
        from app.core.settings import CaptureSettings

        _seed(tmp_path, days_ago=1)
        sent = {}
        monkeypatch.setattr(
            alerts,
            "send_email",
            lambda *a, **k: sent.update(body=a[6], attachments=k.get("attachments")) or True,
        )
        settings = CaptureSettings(
            output_dir=str(tmp_path), smtp_host="h", alert_email_to="ops@example.com"
        )
        digest.send_digest(settings, tmp_path, days=7, attach="csv", link_base="http://host:8765")
        assert sent["attachments"][0][0] == "history.csv"
        assert "/api/history" in sent["body"]


def _settings():
    """A settings object that passes the digest's SMTP guard."""
    from app.core.settings import CaptureSettings

    return CaptureSettings(smtp_host="smtp.example.com", alert_email_to="ops@example.com")


def _seed_drift(directory, url, drift, days_ago=1):
    """One report carrying a recorded drift value (unique name per call)."""
    when = (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds")
    _seq["n"] += 1
    name = when.replace(":", "").replace("-", "").replace("T", "-") + f"-{_seq['n']:03d}"
    _report(
        directory,
        name,
        when,
        [{"url": url, "status": "success", "diff": 0.2, "drift": drift, "file_path": ""}],
    )


class TestDriftAttachment:
    def test_a_drift_chart_is_a_png(self, tmp_path):
        _seed_drift(tmp_path, "https://a.example.com", 0.2)
        _seed_drift(tmp_path, "https://a.example.com", 0.4)
        attachment = digest.build_attachment(tmp_path, "drift")
        assert attachment is not None
        filename, data, subtype = attachment
        assert filename == "drift-chart.png" and subtype == "png"
        assert data.startswith(b"\x89PNG\r\n\x1a\n")

    def test_no_recorded_drift_means_no_chart(self, tmp_path):
        _seed(tmp_path, url="https://a.example.com", diff=0.5)
        assert digest.build_attachment(tmp_path, "drift") is None

    def test_the_chart_travels_with_the_email(self, tmp_path, monkeypatch):
        from app.core import alerts

        _seed_drift(tmp_path, "https://a.example.com", 0.3)
        sent = {}
        monkeypatch.setattr(
            alerts,
            "send_email",
            lambda *a, **k: sent.update(attachments=k.get("attachments")) or True,
        )
        settings = _settings()
        assert digest.send_digest(settings, tmp_path, days=7, attach="drift") is True
        assert sent["attachments"][0][0] == "drift-chart.png"

    def test_the_skip_note_mentions_missing_drift_data(self, tmp_path):
        _seed(tmp_path, url="https://a.example.com", diff=0.5)
        note = digest.attachment_skip_note(tmp_path, "drift", 10**7)
        assert "no drift data" in note

    def test_no_note_for_drift_when_the_chart_exists(self, tmp_path):
        _seed_drift(tmp_path, "https://a.example.com", 0.3)
        assert digest.attachment_skip_note(tmp_path, "drift", 10**7) == ""

    def test_an_oversized_chart_is_dropped_with_a_note(self, tmp_path):
        _seed_drift(tmp_path, "https://a.example.com", 0.3)
        assert digest.build_attachment(tmp_path, "drift", max_bytes=512) is None
        note = digest.attachment_skip_note(tmp_path, "drift", 512)
        assert "the drift attachment was skipped" in note


class TestAttachmentKinds:
    def test_a_comma_list_asks_for_two(self):
        assert digest.parse_kinds("dashboard,drift") == ["dashboard", "drift"]

    def test_spaces_and_semicolons_are_tolerated(self):
        assert digest.parse_kinds(" drift ; csv ") == ["drift", "csv"]

    def test_none_means_nothing(self):
        assert digest.parse_kinds("none") == []

    def test_the_default_is_the_dashboard(self):
        assert digest.parse_kinds("") == ["dashboard"]
        assert digest.parse_kinds(None) == ["dashboard"]

    def test_unknown_names_are_ignored_but_reported(self):
        assert digest.parse_kinds("dashboard,zip") == ["dashboard"]
        assert digest.unknown_kinds("dashboard,zip") == ["zip"]
        assert digest.unknown_kinds("drift") == []

    def test_duplicates_collapse(self):
        assert digest.parse_kinds("csv,csv,drift") == ["csv", "drift"]

    def test_build_attachments_returns_every_kind(self, tmp_path):
        _seed_drift(tmp_path, "https://a.example.com", 0.3)
        found = digest.build_attachments(tmp_path, "dashboard,csv,drift")
        assert [item[0] for item in found] == [
            "dashboard.html",
            "history.csv",
            "drift-chart.png",
        ]

    def test_the_size_limit_drops_only_the_big_ones(self, tmp_path):
        _seed_drift(tmp_path, "https://a.example.com", 0.3)
        found = digest.build_attachments(tmp_path, "dashboard,csv,drift", max_bytes=1024)
        assert [item[0] for item in found] == ["history.csv"]

    def test_send_digest_forwards_both_attachments(self, tmp_path, monkeypatch):
        from app.core import alerts

        _seed_drift(tmp_path, "https://a.example.com", 0.3)
        sent = {}
        monkeypatch.setattr(
            alerts,
            "send_email",
            lambda *a, **k: sent.update(attachments=k.get("attachments")) or True,
        )
        assert digest.send_digest(_settings(), tmp_path, days=7, attach="dashboard,drift") is True
        assert [item[0] for item in sent["attachments"]] == ["dashboard.html", "drift-chart.png"]


class TestPdfAttachment:
    def test_the_trend_report_is_a_pdf(self, tmp_path):
        _seed(tmp_path, days_ago=1, diff=0.4)
        attachment = digest.build_attachment(tmp_path, "pdf")
        assert attachment is not None
        filename, data, subtype = attachment
        assert filename == "trend-report.pdf" and subtype == "pdf"
        assert data.startswith(b"%PDF-")

    def test_no_history_means_no_pdf(self, tmp_path):
        assert digest.build_attachment(tmp_path, "pdf") is None

    def test_the_skip_note_mentions_the_missing_history(self, tmp_path):
        note = digest.attachment_skip_note(tmp_path, "pdf", 10**7)
        assert "no history" in note and "trend PDF" in note

    def test_no_note_when_the_pdf_exists(self, tmp_path):
        _seed(tmp_path, days_ago=1)
        assert digest.attachment_skip_note(tmp_path, "pdf", 10**7) == ""

    def test_pdf_is_a_known_kind(self):
        assert digest.parse_kinds("pdf") == ["pdf"]
        assert digest.parse_kinds("csv,pdf") == ["csv", "pdf"]
        assert digest.unknown_kinds("pdf") == []

    def test_it_can_travel_with_the_others(self, tmp_path):
        _seed(tmp_path, days_ago=1, diff=0.4)
        found = digest.build_attachments(tmp_path, "csv,pdf")
        assert [item[0] for item in found] == ["history.csv", "trend-report.pdf"]

    def test_send_digest_forwards_the_pdf(self, tmp_path, monkeypatch):
        from app.core import alerts

        _seed(tmp_path, days_ago=1, diff=0.4)
        sent = {}
        monkeypatch.setattr(
            alerts,
            "send_email",
            lambda *a, **k: sent.update(attachments=k.get("attachments")) or True,
        )
        assert digest.send_digest(_settings(), tmp_path, days=7, attach="pdf") is True
        assert sent["attachments"][0][0] == "trend-report.pdf"

    def test_an_oversized_pdf_is_dropped(self, tmp_path):
        _seed(tmp_path, days_ago=1)
        assert digest.build_attachment(tmp_path, "pdf", max_bytes=1024) is None
        assert "the pdf attachment was skipped" in digest.attachment_skip_note(
            tmp_path, "pdf", 1024
        )


class TestDigestMarker:
    """``--skip-if-unchanged`` compares the bytes the mail would carry."""

    def _seed(self, directory, name="capture-report-20260101-000000.json", diff=0.2):
        _report(
            directory,
            name.removeprefix("capture-report-").removesuffix(".json"),
            "2026-01-01T00:00:00",
            [{"url": "https://a.example.com", "status": "success", "diff": diff, "file_path": ""}],
        )

    def test_the_marker_is_a_hash_of_the_csv_bytes(self, tmp_path):
        import hashlib

        from app.core import history

        self._seed(tmp_path)
        expected = hashlib.sha256(history.rows_to_csv(history.flat_rows(tmp_path))).hexdigest()[:32]
        assert digest.digest_marker(tmp_path) == expected
        assert len(digest.digest_marker(tmp_path)) == 32

    def test_a_new_capture_moves_the_marker(self, tmp_path):
        self._seed(tmp_path)
        before = digest.digest_marker(tmp_path)
        _report(
            tmp_path,
            "20260102-000000",
            "2026-01-02T00:00:00",
            [{"url": "https://a.example.com", "status": "success", "diff": 0.9, "file_path": ""}],
        )
        assert digest.digest_marker(tmp_path) != before

    def test_an_empty_folder_still_has_a_stable_marker(self, tmp_path):
        assert digest.digest_marker(tmp_path) == digest.digest_marker(tmp_path)

    def test_nothing_recorded_yet_means_do_not_skip(self, tmp_path):
        skip, current, previous = digest.should_skip_digest(tmp_path)
        assert skip is False and current and previous == ""

    def test_a_recorded_marker_skips_until_something_changes(self, tmp_path):
        self._seed(tmp_path)
        digest.record_marker(tmp_path, days=7, rows=1)
        assert digest.should_skip_digest(tmp_path)[0] is True
        _report(
            tmp_path,
            "20260102-000000",
            "2026-01-02T00:00:00",
            [{"url": "https://a.example.com", "status": "success", "diff": 0.9, "file_path": ""}],
        )
        assert digest.should_skip_digest(tmp_path)[0] is False

    def test_the_marker_file_sits_next_to_the_history(self, tmp_path):
        assert digest.marker_path(tmp_path).name == ".digest-marker.json"

    def test_what_was_stored_can_be_read_back(self, tmp_path):
        self._seed(tmp_path)
        written = digest.record_marker(tmp_path, days=3, rows=2)
        assert written is not None and written.exists()
        stored = digest.last_marker(tmp_path)
        assert stored["marker"] == digest.digest_marker(tmp_path)
        assert stored["days"] == 3 and stored["rows"] == 2
        assert stored["digest_at"]

    def test_a_corrupt_marker_is_ignored(self, tmp_path):
        digest.marker_path(tmp_path).write_text("{not json", encoding="utf-8")
        assert digest.last_marker(tmp_path) == {}
        assert digest.should_skip_digest(tmp_path)[0] is False

    def test_a_write_failure_is_not_fatal(self, tmp_path, monkeypatch):
        def boom(*args, **kwargs):
            raise OSError("read-only folder")

        from pathlib import Path

        monkeypatch.setattr(Path, "write_text", boom)
        assert digest.record_marker(tmp_path, days=7) is None

    def test_the_row_count_is_what_the_mail_would_carry(self, tmp_path):
        self._seed(tmp_path)
        _report(
            tmp_path,
            "20260102-000000",
            "2026-01-02T00:00:00",
            [{"url": "https://b.example.com", "status": "success", "diff": 0.3, "file_path": ""}],
        )
        assert digest.marker_row_count(tmp_path) == 2
