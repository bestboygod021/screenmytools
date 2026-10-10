"""Tests for the headless CLI front end (no Qt, no real browser)."""

from __future__ import annotations

import json
import sys

from app import cli
from app.core.engine import CaptureEngine
from tests.fakes import CallLog, PageScript
from tests.fakes import make_factory as fake_pw_factory


def make_factory(script=None, calls=None, launch_error=None):
    def factory(settings, log=None, progress=None, on_result=None):
        return CaptureEngine(
            settings,
            playwright_factory=fake_pw_factory(script, calls, launch_error),
            log=log,
            progress=progress,
            on_result=on_result,
        )

    return factory


class TestCliSuccess:
    def test_positional_urls_exit_zero_and_write_files(self, tmp_path):
        out = tmp_path / "shots"
        code = cli.main(
            ["https://a.example.com", "https://b.example.com", "--out", str(out)],
            engine_factory=make_factory(PageScript()),
        )
        assert code == 0
        assert len(list(out.glob("*.png"))) == 2

    def test_urls_file_is_read(self, tmp_path):
        urls_file = tmp_path / "list.txt"
        urls_file.write_text("https://one.example.com\n# comment\nhttps://two.example.com\n")
        out = tmp_path / "out"
        code = cli.main(
            ["--urls-file", str(urls_file), "--out", str(out)], engine_factory=make_factory()
        )
        assert code == 0
        assert len(list(out.glob("*.png"))) == 2

    def test_concurrency_is_honoured(self, tmp_path):
        calls = CallLog()
        out = tmp_path / "out"
        code = cli.main(
            [
                "https://a.com",
                "https://b.com",
                "https://c.com",
                "--out",
                str(out),
                "--concurrency",
                "3",
            ],
            engine_factory=make_factory(PageScript(), calls),
        )
        assert code == 0
        assert len(calls.launched) == 3

    def test_json_summary_is_valid(self, tmp_path, capsys):
        out = tmp_path / "out"
        cli.main(
            ["https://a.com", "--out", str(out), "--json", "--no-report", "--quiet"],
            engine_factory=make_factory(PageScript()),
        )
        data = json.loads(capsys.readouterr().out)
        assert data["total"] == 1
        assert data["succeeded"] == 1
        assert data["results"][0]["status"] == "success"


class TestCliFailures:
    def test_failed_url_exits_one(self, tmp_path):
        out = tmp_path / "out"
        code = cli.main(
            ["https://broken.example.com", "--out", str(out)],
            engine_factory=make_factory(PageScript(goto_error=RuntimeError("boom"))),
        )
        assert code == 1

    def test_no_urls_exits_two(self, tmp_path):
        code = cli.main(["--out", str(tmp_path / "o")], engine_factory=make_factory())
        assert code == 2

    def test_invalid_urls_file_exits_two(self, tmp_path):
        code = cli.main(
            ["--urls-file", str(tmp_path / "missing.txt"), "--out", str(tmp_path / "o")],
            engine_factory=make_factory(),
        )
        assert code == 2

    def test_bad_viewport_exits_two(self, tmp_path):
        code = cli.main(
            ["https://a.com", "--out", str(tmp_path / "o"), "--viewport", "notasize"],
            engine_factory=make_factory(),
        )
        assert code == 2

    def test_missing_browser_exits_three(self, tmp_path):
        code = cli.main(
            ["https://a.com", "--out", str(tmp_path / "o")],
            engine_factory=make_factory(
                launch_error=Exception("Executable doesn't exist at /x/chrome")
            ),
        )
        assert code == 3


class TestCliQuietAndMapping:
    def test_quiet_hides_ok_lines(self, tmp_path, capsys):
        out = tmp_path / "out"
        cli.main(
            ["https://a.com", "--out", str(out), "--quiet"],
            engine_factory=make_factory(PageScript()),
        )
        captured = capsys.readouterr()
        assert "[OK" not in captured.out

    def test_viewport_and_scale_map_to_settings(self, tmp_path):
        seen = {}

        def spy_factory(settings, **kwargs):
            seen["w"] = settings.viewport_width
            seen["h"] = settings.viewport_height
            seen["scale"] = settings.device_scale_factor
            return CaptureEngine(settings, playwright_factory=make_factory(PageScript()), **kwargs)

        cli.main(
            [
                "https://a.com",
                "--out",
                str(tmp_path / "o"),
                "--viewport",
                "1440x900",
                "--scale",
                "1.5",
            ],
            engine_factory=spy_factory,
        )
        assert seen == {"w": 1440, "h": 900, "scale": 1.5}


class TestSubcommands:
    def test_capture_subcommand(self, tmp_path):
        out = tmp_path / "shots"
        code = cli.main(
            ["capture", "https://a.example.com", "--out", str(out)],
            engine_factory=make_factory(PageScript()),
        )
        assert code == 0
        assert len(list(out.glob("*.png"))) == 1

    def test_schedule_runs_multiple_times(self, tmp_path, capsys):
        out = tmp_path / "shots"
        code = cli.main(
            ["schedule", "https://a.example.com", "--out", str(out), "--runs", "2", "--every", "0"],
            engine_factory=make_factory(PageScript()),
        )
        assert code == 0
        assert "2/2" in capsys.readouterr().out

    def test_history_json(self, tmp_path, capsys):
        report = tmp_path / "capture-report-20260101-000000.json"
        report.write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "results": [
                        {
                            "url": "https://a.example.com",
                            "status": "success",
                            "diff": 0.1,
                            "file_path": "x.png",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        code = cli.main(["history", "--dir", str(tmp_path), "--json"])
        assert code == 0
        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["url"] == "https://a.example.com"

    def test_history_empty(self, tmp_path, capsys):
        code = cli.main(["history", "--dir", str(tmp_path / "none")])
        assert code == 0
        assert "No history" in capsys.readouterr().out

    def test_history_site_filter(self, tmp_path, capsys):
        (tmp_path / "capture-report-20260101-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "results": [
                        {
                            "url": "https://a.example.com",
                            "status": "success",
                            "diff": None,
                            "file_path": "",
                        },
                        {
                            "url": "https://b.example.com",
                            "status": "failed",
                            "diff": None,
                            "file_path": "",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        cli.main(["history", "--dir", str(tmp_path), "--site", "https://b.example.com", "--json"])
        rows = json.loads(capsys.readouterr().out)
        assert len(rows) == 1 and rows[0]["url"] == "https://b.example.com"

    def test_history_trend(self, tmp_path, capsys):
        (tmp_path / "capture-report-20260101-000000.json").write_text(
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
        (tmp_path / "capture-report-20260102-000000.json").write_text(
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
        code = cli.main(["history", "--dir", str(tmp_path), "--trend"])
        out = capsys.readouterr().out
        assert code == 0
        assert "a.example.com" in out and "1 change(s)" in out

    def test_history_trend_json(self, tmp_path, capsys):
        (tmp_path / "capture-report-20260101-000000.json").write_text(
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
        cli.main(["history", "--dir", str(tmp_path), "--trend", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert "trend" in payload and "a.example.com" in payload["trend"]


class TestBaselineSubcommand:
    def _seed_latest(self, directory, url="https://a.example.com"):
        from app.core import baseline

        path = baseline.latest_path(directory, url)
        path.write_bytes(b"png-bytes")
        return path

    def test_pin_promotes_the_latest_capture(self, tmp_path, capsys):
        from app.core import baseline

        self._seed_latest(tmp_path)
        code = cli.main(["baseline", "--dir", str(tmp_path), "--pin", "https://a.example.com"])
        assert code == 0
        assert "Baseline pinned" in capsys.readouterr().out
        assert baseline.has_baseline(tmp_path, "https://a.example.com")

    def test_pin_without_a_capture_fails(self, tmp_path):
        code = cli.main(["baseline", "--dir", str(tmp_path), "--pin", "https://a.example.com"])
        assert code == 1

    def test_list_marks_pinned_sites(self, tmp_path, capsys):
        from app.core import baseline

        report = tmp_path / "capture-report-20260101-000000.json"
        report.write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "results": [
                        {
                            "url": "https://a.example.com",
                            "status": "success",
                            "diff": 0.1,
                            "file_path": "",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self._seed_latest(tmp_path)
        baseline.pin_baseline(tmp_path, "https://a.example.com")
        code = cli.main(["baseline", "--dir", str(tmp_path), "--list"])
        out = capsys.readouterr().out
        assert code == 0 and "[pinned]" in out and "a.example.com" in out

    def test_clear_removes_the_pin(self, tmp_path, capsys):
        from app.core import baseline

        self._seed_latest(tmp_path)
        baseline.pin_baseline(tmp_path, "https://a.example.com")
        code = cli.main(["baseline", "--dir", str(tmp_path), "--clear", "https://a.example.com"])
        assert code == 0
        assert "Baseline cleared." in capsys.readouterr().out
        assert not baseline.has_baseline(tmp_path, "https://a.example.com")

    def test_no_action_returns_usage_error(self, tmp_path):
        assert cli.main(["baseline", "--dir", str(tmp_path)]) == 2


class TestHistoryPrune:
    def _seed(self, directory, *stamps):
        from app.core.store import HistoryStore

        with HistoryStore(directory / "history.sqlite3") as store:
            for stamp in stamps:
                store.add_run(
                    stamp,
                    [
                        {
                            "url": "https://a.example.com",
                            "status": "success",
                            "diff": 0.1,
                            "file_path": "",
                        }
                    ],
                )

    def test_prune_days_removes_old_rows(self, tmp_path, capsys):
        self._seed(tmp_path, "2020-01-01T00:00:00", "2020-01-02T00:00:00")
        code = cli.main(["history", "--dir", str(tmp_path), "--prune-days", "7"])
        out = capsys.readouterr().out
        assert code == 0 and "pruned: 2 row(s)" in out
        from app.core.store import HistoryStore

        with HistoryStore(tmp_path / "history.sqlite3") as store:
            assert store.flat_rows() == []

    def test_prune_without_an_index_is_harmless(self, tmp_path, capsys):
        code = cli.main(["history", "--dir", str(tmp_path), "--prune-days", "7"])
        assert code == 0 and "pruned: 0 row(s)" in capsys.readouterr().out


class TestDashboardBaselineFlag:
    def test_baseline_max_age_is_accepted(self, tmp_path, capsys):
        import json

        (tmp_path / "capture-report-20260101-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "results": [
                        {
                            "url": "https://a.example.com",
                            "status": "success",
                            "diff": 0.1,
                            "file_path": "",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        out = tmp_path / "d.html"
        code = cli.main(
            ["dashboard", "--dir", str(tmp_path), "--out", str(out), "--baseline-max-age", "7"]
        )
        assert code == 0 and out.exists()


class TestPruneScreenshots:
    def test_prune_screenshots_deletes_old_files(self, tmp_path, capsys):
        import os
        import time

        old = tmp_path / "001_a_20250101-000000.png"
        old.write_bytes(b"png")
        when = time.time() - 60 * 86400
        os.utime(old, (when, when))
        fresh = tmp_path / "002_a_20260101-000000.png"
        fresh.write_bytes(b"png")

        code = cli.main(["history", "--dir", str(tmp_path), "--prune-screenshots", "7"])
        out = capsys.readouterr().out
        assert code == 0 and "Screenshots deleted: 1 file(s)" in out
        assert not old.exists() and fresh.exists()


class TestDigestSubcommand:
    def _report(self, directory, name, when, results):
        (directory / f"capture-report-{name}.json").write_text(
            json.dumps({"generated_at": when, "results": results}), encoding="utf-8"
        )

    def _seed(self, directory):
        from datetime import datetime, timedelta

        when = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
        self._report(
            directory,
            "20260101-000000",
            when,
            [{"url": "https://a.example.com", "status": "success", "diff": 0.5, "file_path": ""}],
        )

    def test_prints_the_digest(self, tmp_path, capsys):
        self._seed(tmp_path)
        code = cli.main(["digest", "--dir", str(tmp_path), "--days", "7"])
        out = capsys.readouterr().out
        assert code == 0
        assert "last 7 day(s)" in out and "a.example.com" in out

    def test_json_output(self, tmp_path, capsys):
        self._seed(tmp_path)
        code = cli.main(["digest", "--dir", str(tmp_path), "--days", "7", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["captures"] == 1 and payload["changed"] == 1

    def test_send_without_smtp_config_exits_two(self, tmp_path, capsys):
        self._seed(tmp_path)
        code = cli.main(["digest", "--dir", str(tmp_path), "--send"])
        assert code == 2
        assert "Could not send the digest" in capsys.readouterr().err

    def test_send_uses_the_passed_smtp_settings(self, tmp_path, capsys, monkeypatch):
        from app.core import alerts

        self._seed(tmp_path)
        sent = {}
        monkeypatch.setattr(
            alerts,
            "send_email",
            lambda host, port, user, password, to, subject, body, timeout=10.0, attachments=None: (
                sent.update(host=host, to=to) or True
            ),
        )
        code = cli.main(
            [
                "digest",
                "--dir",
                str(tmp_path),
                "--send",
                "--smtp-host",
                "smtp.example.com",
                "--email-to",
                "ops@example.com",
            ]
        )
        assert code == 0
        assert sent == {"host": "smtp.example.com", "to": "ops@example.com"}
        assert "Digest emailed to ops@example.com" in capsys.readouterr().out


class TestPruneScreenshotsDryRun:
    def test_dry_run_lists_and_keeps_files(self, tmp_path, capsys):
        import os
        import time

        old = tmp_path / "001_a_20250101-000000.png"
        old.write_bytes(b"png")
        when = time.time() - 60 * 86400
        os.utime(old, (when, when))

        code = cli.main(
            ["history", "--dir", str(tmp_path), "--prune-screenshots", "7", "--dry-run"]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "Would delete 1 file(s)" in out
        assert old.name in out
        assert old.exists()  # the dry run must not touch the disk


class TestDigestAttach:
    def test_attach_flag_reaches_the_sender(self, tmp_path, monkeypatch, capsys):
        from app.core import alerts

        sent = {}
        monkeypatch.setattr(
            alerts,
            "send_email",
            lambda *a, **k: sent.update(attachments=k.get("attachments")) or True,
        )
        code = cli.main(
            [
                "digest",
                "--dir",
                str(tmp_path),
                "--send",
                "--attach",
                "none",
                "--smtp-host",
                "h",
                "--email-to",
                "ops@example.com",
            ]
        )
        assert code == 0
        assert sent["attachments"] is None


class TestDigestBaseComparison:
    def _seed(self, directory, url, diff):
        from datetime import datetime, timedelta

        when = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
        stamp = when.replace(":", "").replace("-", "").replace("T", "-")
        slug = "".join(ch if ch.isalnum() else "_" for ch in url)
        (directory / f"capture-report-{stamp}-{slug}.json").write_text(
            json.dumps(
                {
                    "generated_at": when,
                    "results": [{"url": url, "status": "success", "diff": diff, "file_path": ""}],
                }
            ),
            encoding="utf-8",
        )

    def _folders(self, tmp_path):
        base = tmp_path / "base"
        head = tmp_path / "head"
        base.mkdir()
        head.mkdir()
        self._seed(base, "https://a.example.com", 0.2)
        self._seed(head, "https://a.example.com", 0.2)
        self._seed(head, "https://b.example.com", 0.9)
        return base, head

    def test_base_flag_prints_the_comparison(self, tmp_path, capsys):
        base, head = self._folders(tmp_path)
        code = cli.main(["digest", "--dir", str(head), "--base", str(base), "--days", "7"])
        out = capsys.readouterr().out
        assert code == 0
        assert "vs base branch" in out and "Captures : 1 -> 2 (+1)" in out

    def test_base_flag_in_json_mode(self, tmp_path, capsys):
        base, head = self._folders(tmp_path)
        code = cli.main(
            ["digest", "--dir", str(head), "--base", str(base), "--days", "7", "--json"]
        )
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["head"]["captures"] == 2 and payload["base"]["captures"] == 1
        assert any("vs base branch" in line for line in payload["comparison"])

    def test_base_flag_reaches_the_email_body(self, tmp_path, monkeypatch, capsys):
        from app.core import alerts

        base, head = self._folders(tmp_path)
        sent = {}
        monkeypatch.setattr(alerts, "send_email", lambda *a, **k: sent.update(body=a[6]) or True)
        code = cli.main(
            [
                "digest",
                "--dir",
                str(head),
                "--base",
                str(base),
                "--send",
                "--smtp-host",
                "h",
                "--email-to",
                "ops@example.com",
            ]
        )
        assert code == 0
        assert "vs base branch" in sent["body"]


class TestDigestAttachmentLimit:
    def _send(self, tmp_path, monkeypatch, extra_args):
        from app.core import alerts

        sent = {}
        monkeypatch.setattr(
            alerts,
            "send_email",
            lambda *a, **k: sent.update(attachments=k.get("attachments"), body=a[6]) or True,
        )
        code = cli.main(
            [
                "digest",
                "--dir",
                str(tmp_path),
                "--send",
                *extra_args,
                "--smtp-host",
                "h",
                "--email-to",
                "ops@example.com",
            ]
        )
        return code, sent

    def test_limit_below_the_dashboard_size_drops_it(self, tmp_path, monkeypatch):
        # Even an empty folder renders a >1 KB dashboard, so 1 KB is a tight limit.
        code, sent = self._send(tmp_path, monkeypatch, ["--max-attachment-kb", "1"])
        assert code == 0
        assert sent["attachments"] is None
        assert "attachment was skipped" in sent["body"]

    def test_no_limit_keeps_the_attachment(self, tmp_path, monkeypatch):
        code, sent = self._send(tmp_path, monkeypatch, ["--max-attachment-kb", "0"])
        assert code == 0
        assert sent["attachments"] is not None
        assert "attachment was skipped" not in sent["body"]


class TestDigestLinks:
    def _seed(self, tmp_path):
        from datetime import datetime, timedelta

        when = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
        (tmp_path / "capture-report-20260101-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": when,
                    "results": [
                        {
                            "url": "https://a.example.com",
                            "status": "success",
                            "diff": 0.5,
                            "file_path": "",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_link_base_prints_the_routes(self, tmp_path, capsys):
        self._seed(tmp_path)
        code = cli.main(["digest", "--dir", str(tmp_path), "--link-base", "http://host:8765"])
        out = capsys.readouterr().out
        assert code == 0
        assert "/api/history" in out and "/api/digest?days=7" in out

    def test_link_base_reaches_the_email(self, tmp_path, monkeypatch):
        from app.core import alerts

        self._seed(tmp_path)
        sent = {}
        monkeypatch.setattr(alerts, "send_email", lambda *a, **k: sent.update(body=a[6]) or True)
        code = cli.main(
            [
                "digest",
                "--dir",
                str(tmp_path),
                "--send",
                "--link-base",
                "http://host:8765",
                "--attach",
                "none",
                "--smtp-host",
                "h",
                "--email-to",
                "ops@example.com",
            ]
        )
        assert code == 0
        assert "/api/trend" in sent["body"]
        assert "attachments" not in sent or sent.get("attachments") is None


class TestHistoryVacuum:
    def test_vacuum_reports_reclaimed_space(self, tmp_path, capsys):
        from app.core.store import HistoryStore

        with HistoryStore(tmp_path / "history.sqlite3") as store:
            for index in range(300):
                store.add_run(
                    "2026-01-01T00:00:00",
                    [
                        {
                            "url": f"https://site{index}.example.com",
                            "status": "success",
                            "diff": 0.1,
                            "drift": 0.3,
                            "file_path": f"/tmp/x{index}.png",
                        }
                    ],
                )
            store.prune(1)

        code = cli.main(["history", "--dir", str(tmp_path), "--vacuum"])
        out = capsys.readouterr().out
        assert code == 0
        assert "Index compacted:" in out and "KB reclaimed" in out

    def test_vacuum_without_an_index_says_so(self, tmp_path, capsys):
        code = cli.main(["history", "--dir", str(tmp_path), "--vacuum"])
        assert code == 0
        assert "No history index to compact." in capsys.readouterr().out


class TestDigestProfiles:
    def _setup(self, tmp_path):
        from app.core import profiles

        shots_a = tmp_path / "shots-a"
        shots_b = tmp_path / "shots-b"
        shots_a.mkdir()
        shots_b.mkdir()
        base = tmp_path / "config"
        profiles.save_profile(
            base,
            "daily",
            {"output_dir": str(shots_a), "digest_days": 7, "digest_issue_number": 5},
            "",
        )
        profiles.save_profile(base, "monthly", {"output_dir": str(shots_b), "digest_days": 30}, "")
        return base, shots_a, shots_b

    def _seed_report(self, directory, url="https://a.example.com"):
        from datetime import datetime, timedelta

        when = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
        (directory / "capture-report-20260101-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": when,
                    "results": [{"url": url, "status": "success", "diff": 0.4, "file_path": ""}],
                }
            ),
            encoding="utf-8",
        )

    def test_prints_one_section_per_profile(self, tmp_path, capsys):
        base, shots_a, shots_b = self._setup(tmp_path)
        self._seed_report(shots_a)
        code = cli.main(
            ["digest", "--dir", str(shots_a), "--profiles", "--profiles-base", str(base)]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "daily: last 7 day(s) (issue #5)" in out
        assert "monthly: last 30 day(s)" in out

    def test_json_lists_the_profiles(self, tmp_path, capsys):
        base, shots_a, _shots_b = self._setup(tmp_path)
        self._seed_report(shots_a)
        code = cli.main(
            ["digest", "--dir", str(shots_a), "--profiles", "--profiles-base", str(base), "--json"]
        )
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        entries = {entry["profile"]: entry for entry in payload["profiles"]}
        assert entries["daily"]["days"] == 7 and entries["daily"]["issue"] == 5
        assert entries["daily"]["captures"] == 1

    def test_missing_profile_store_is_not_an_error(self, tmp_path, capsys):
        code = cli.main(
            [
                "digest",
                "--dir",
                str(tmp_path),
                "--profiles",
                "--profiles-base",
                str(tmp_path / "none"),
            ]
        )
        assert code == 0
        assert "No profiles found" in capsys.readouterr().err

    def test_send_uses_each_profiles_smtp_and_window(self, tmp_path, capsys, monkeypatch):
        from app.core import alerts

        base, shots_a, shots_b = self._setup(tmp_path)
        self._seed_report(shots_a)
        self._seed_report(shots_b)
        sent = []
        monkeypatch.setattr(
            alerts,
            "send_email",
            lambda host, port, user, password, to, subject, body, timeout=10.0, attachments=None: (
                sent.append((subject, host, to, attachments)) or True
            ),
        )
        code = cli.main(
            [
                "digest",
                "--dir",
                str(shots_a),
                "--profiles",
                "--profiles-base",
                str(base),
                "--send",
                "--smtp-host",
                "smtp.example.com",
                "--email-to",
                "ops@example.com",
            ]
        )
        assert code == 0
        assert len(sent) == 2
        # Each profile emails its own window, all through the same SMTP settings.
        assert "7 day(s)" in sent[0][0] and "30 day(s)" in sent[1][0]
        assert all(item[1] == "smtp.example.com" for item in sent)
        assert all(item[2] == "ops@example.com" for item in sent)

    def test_a_broken_profile_does_not_stop_the_others(self, tmp_path, capsys, monkeypatch):
        base, shots_a, shots_b = self._setup(tmp_path)
        self._seed_report(shots_a)
        self._seed_report(shots_b)
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("smtp down")
            return True

        from app.core import alerts

        monkeypatch.setattr(alerts, "send_email", flaky)
        code = cli.main(
            [
                "digest",
                "--dir",
                str(shots_a),
                "--profiles",
                "--profiles-base",
                str(base),
                "--send",
                "--smtp-host",
                "h",
                "--email-to",
                "ops@example.com",
            ]
        )
        out = capsys.readouterr()
        assert code == 1  # one failure is reported through the exit code
        assert "could not send the digest" in out.err
        assert "digest emailed" in out.out  # the second profile still went out


class TestDigestAttachKinds:
    def _seed(self, tmp_path):
        from datetime import datetime, timedelta

        when = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
        (tmp_path / "capture-report-20260101-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": when,
                    "results": [
                        {
                            "url": "https://a.example.com",
                            "status": "success",
                            "diff": 0.4,
                            "drift": 0.3,
                            "file_path": "",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def _send(self, tmp_path, monkeypatch, extra):
        from app.core import alerts

        sent = {}
        monkeypatch.setattr(
            alerts,
            "send_email",
            lambda *a, **k: sent.update(attachments=k.get("attachments")) or True,
        )
        code = cli.main(
            [
                "digest",
                "--dir",
                str(tmp_path),
                "--send",
                "--smtp-host",
                "smtp.example.com",
                "--email-to",
                "ops@example.com",
                *extra,
            ]
        )
        return code, sent

    def test_the_drift_kind_attaches_a_png(self, tmp_path, monkeypatch, capsys):
        self._seed(tmp_path)
        code, sent = self._send(tmp_path, monkeypatch, ["--attach", "drift"])
        assert code == 0
        assert [item[0] for item in sent["attachments"]] == ["drift-chart.png"]

    def test_two_kinds_travel_together(self, tmp_path, monkeypatch, capsys):
        self._seed(tmp_path)
        code, sent = self._send(tmp_path, monkeypatch, ["--attach", "csv,drift"])
        assert code == 0
        assert [item[0] for item in sent["attachments"]] == ["history.csv", "drift-chart.png"]

    def test_an_unknown_kind_is_a_clean_error(self, tmp_path, capsys):
        self._seed(tmp_path)
        code = cli.main(["digest", "--dir", str(tmp_path), "--attach", "xlsx"])
        captured = capsys.readouterr()
        assert code == 2
        assert "xlsx" in captured.err and "drift" in captured.err

    def test_none_still_sends_without_attachments(self, tmp_path, monkeypatch, capsys):
        self._seed(tmp_path)
        code, sent = self._send(tmp_path, monkeypatch, ["--attach", "none"])
        assert code == 0 and sent["attachments"] is None


class TestHistoryExport:
    def _seed(self, tmp_path):
        for index, (stamp, url, diff, drift) in enumerate(
            (
                ("2026-01-01T00:00:00", "https://shop.example.com", 0.0, None),
                ("2026-01-02T00:00:00", "https://shop.example.com", 0.4, 0.1),
                ("2026-01-02T01:00:00", "https://news.example.com", 0.1, None),
            )
        ):
            name = stamp.replace(":", "").replace("-", "").replace("T", "-") + f"-{index}"
            (tmp_path / f"capture-report-{name}.json").write_text(
                json.dumps(
                    {
                        "generated_at": stamp,
                        "results": [
                            {
                                "url": url,
                                "status": "success",
                                "diff": diff,
                                "drift": drift,
                                "file_path": "x.png",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
        return tmp_path

    def test_csv_export_writes_a_file_and_reports_the_count(self, tmp_path, capsys):
        target = tmp_path / "history.csv"
        code = cli.main(
            ["history", "--dir", str(self._seed(tmp_path)), "--export", "csv", "--out", str(target)]
        )
        captured = capsys.readouterr()
        assert code == 0
        lines = target.read_text(encoding="utf-8").strip().splitlines()
        assert lines[0] == "timestamp,url,label,status,diff,drift,file"
        assert len(lines) == 4
        assert "Exported 3 of 3 row(s)" in captured.err
        assert captured.out.strip() == ""  # the file got the data, not the console

    def test_json_export_is_the_same_envelope_as_the_api(self, tmp_path, capsys):
        from app.core import api

        target = tmp_path / "history.json"
        code = cli.main(
            [
                "history",
                "--dir",
                str(self._seed(tmp_path)),
                "--export",
                "json",
                "--out",
                str(target),
            ]
        )
        assert code == 0
        page = json.loads(target.read_text(encoding="utf-8"))
        assert {key: page[key] for key in ("total", "count", "limit", "offset")} == {
            "total": 3,
            "count": 3,
            "limit": 0,
            "offset": 0,
        }
        expected = api.export_payload(tmp_path, "json")[0]
        assert target.read_bytes() == expected  # the offline twin, byte for byte

    def test_the_url_filter_narrows_the_export(self, tmp_path, capsys):
        target = tmp_path / "shop.csv"
        code = cli.main(
            [
                "history",
                "--dir",
                str(self._seed(tmp_path)),
                "--export",
                "csv",
                "--url",
                "shop",
                "--out",
                str(target),
            ]
        )
        assert code == 0
        text = target.read_text(encoding="utf-8")
        assert text.count("shop.example.com") == 2 and "news.example.com" not in text
        assert "Exported 2 of 2 row(s)" in capsys.readouterr().err

    def test_limit_and_offset_page_the_export(self, tmp_path, capsys):
        target = tmp_path / "page.csv"
        code = cli.main(
            [
                "history",
                "--dir",
                str(self._seed(tmp_path)),
                "--export",
                "csv",
                "--limit",
                "1",
                "--offset",
                "1",
                "--out",
                str(target),
            ]
        )
        assert code == 0
        assert len(target.read_text(encoding="utf-8").strip().splitlines()) == 2
        assert "Exported 1 of 3 row(s)" in capsys.readouterr().err

    def test_without_out_the_export_goes_to_stdout(self, tmp_path, capsys):
        code = cli.main(["history", "--dir", str(self._seed(tmp_path)), "--export", "csv"])
        captured = capsys.readouterr()
        assert code == 0
        assert captured.out.splitlines()[0] == "timestamp,url,label,status,diff,drift,file"

    def test_an_empty_history_still_writes_the_header(self, tmp_path, capsys):
        target = tmp_path / "empty.csv"
        code = cli.main(
            ["history", "--dir", str(tmp_path), "--export", "csv", "--out", str(target)]
        )
        assert code == 0
        assert target.read_text(encoding="utf-8").strip().splitlines() == [
            "timestamp,url,label,status,diff,drift,file"
        ]

    def test_an_unwritable_out_is_a_clean_error(self, tmp_path, capsys):
        self._seed(tmp_path)
        code = cli.main(
            ["history", "--dir", str(tmp_path), "--export", "csv", "--out", str(tmp_path)]
        )
        captured = capsys.readouterr()
        assert code == 2
        assert "Could not write" in captured.err

    def test_an_unknown_format_is_rejected_by_the_parser(self, tmp_path):
        import pytest

        with pytest.raises(SystemExit) as excinfo:
            cli.main(["history", "--dir", str(tmp_path), "--export", "xlsx"])
        assert excinfo.value.code == 2


class TestPruneScreenshotsBySize:
    def _seed(self, tmp_path, count=3, kb=600):
        import os
        import time

        for index in range(count):
            path = tmp_path / f"shot{index}.png"
            path.write_bytes(b"x" * (kb * 1024))
            when = time.time() - (count - index) * 3600
            os.utime(path, (when, when))
        return tmp_path

    def test_it_deletes_the_oldest_until_under_the_cap(self, tmp_path, capsys):
        self._seed(tmp_path)
        code = cli.main(["history", "--dir", str(tmp_path), "--prune-screenshots-mb", "1"])
        out = capsys.readouterr().out
        assert code == 0
        assert "Screenshots deleted: 2 file(s) to stay under 1 MB." in out
        assert sorted(path.name for path in tmp_path.glob("shot*.png")) == ["shot2.png"]

    def test_dry_run_lists_and_keeps(self, tmp_path, capsys):
        self._seed(tmp_path)
        code = cli.main(
            ["history", "--dir", str(tmp_path), "--prune-screenshots-mb", "1", "--dry-run"]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "Would delete 2 file(s) to get under 1 MB:" in out
        assert len(list(tmp_path.glob("shot*.png"))) == 3

    def test_zero_means_off(self, tmp_path, capsys):
        self._seed(tmp_path)
        code = cli.main(["history", "--dir", str(tmp_path), "--prune-screenshots-mb", "0"])
        assert code == 0
        assert "Screenshots deleted" not in capsys.readouterr().out
        assert len(list(tmp_path.glob("shot*.png"))) == 3


class TestDigestPdfAttach:
    def test_the_pdf_kind_attaches_the_trend_report(self, tmp_path, monkeypatch, capsys):
        from datetime import datetime, timedelta

        from app.core import alerts

        when = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
        (tmp_path / "capture-report-20260101-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": when,
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
        sent = {}
        monkeypatch.setattr(
            alerts,
            "send_email",
            lambda *a, **k: sent.update(attachments=k.get("attachments")) or True,
        )
        code = cli.main(
            [
                "digest",
                "--dir",
                str(tmp_path),
                "--send",
                "--smtp-host",
                "smtp.example.com",
                "--email-to",
                "ops@example.com",
                "--attach",
                "pdf",
            ]
        )
        assert code == 0
        assert [item[0] for item in sent["attachments"]] == ["trend-report.pdf"]
        assert sent["attachments"][0][1].startswith(b"%PDF-")


class TestPruneHistoryMb:
    """The offline twin of the engine's history size cap."""

    def _seed(self, folder, runs=4, kb=300):
        from datetime import datetime, timedelta

        for index in range(runs):
            when = datetime.now() - timedelta(days=runs - index)
            stamp = when.strftime("%Y%m%d-%H%M%S")
            for suffix, filler in ((".json", b"x"), (".csv", b"y")):
                (folder / f"capture-report-{stamp}{suffix}").write_bytes(filler * (kb * 1024))

    def test_a_dry_run_lists_the_runs_and_keeps_them(self, tmp_path, capsys):
        from app.core import retention

        self._seed(tmp_path)
        code = cli.main(["history", "--dir", str(tmp_path), "--prune-history-mb", "1", "--dry-run"])
        assert code == 0
        out = capsys.readouterr().out
        assert "Would delete" in out and "capture-report-" in out
        assert len(retention.report_files(tmp_path)) == 8  # nothing was deleted

    def test_the_real_run_deletes_them(self, tmp_path, capsys):
        from app.core import retention

        self._seed(tmp_path)
        code = cli.main(["history", "--dir", str(tmp_path), "--prune-history-mb", "1"])
        assert code == 0
        assert "History cleaned up:" in capsys.readouterr().out
        assert retention.history_footprint(tmp_path) <= 1024 * 1024
        assert len(retention.report_files(tmp_path)) <= 4

    def test_a_folder_under_the_cap_says_so(self, tmp_path, capsys):
        self._seed(tmp_path, runs=1, kb=10)
        code = cli.main(["history", "--dir", str(tmp_path), "--prune-history-mb", "50"])
        assert code == 0
        assert "Nothing to clean up" in capsys.readouterr().out

    def test_without_the_flag_nothing_is_touched(self, tmp_path, capsys):
        from app.core import retention

        self._seed(tmp_path)
        code = cli.main(["history", "--dir", str(tmp_path), "--json"])
        assert code == 0
        capsys.readouterr()
        assert len(retention.report_files(tmp_path)) == 8


class TestWatchdogMaxAgeFlag:
    def test_the_flag_reaches_the_settings(self):
        from app import cli

        args = cli.build_parser().parse_args(
            ["https://a.com", "--out", "x", "--watchdog-max-age", "90"]
        )
        assert cli._settings_from_args(args).watchdog_stale_minutes == 90

    def test_the_default_keeps_the_watchdog_off(self):
        from app import cli

        args = cli.build_parser().parse_args(["https://a.com", "--out", "x"])
        assert cli._settings_from_args(args).watchdog_stale_minutes == 0


class TestServeStaleAfterFlag:
    def test_the_flag_reaches_the_server(self, tmp_path, monkeypatch):
        from app import cli
        from app.core import api

        started = {}
        monkeypatch.setattr(
            api, "serve", lambda directory, host, port, **kwargs: started.update(kwargs)
        )
        code = cli.main(["serve", "--dir", str(tmp_path), "--stale-after", "45"])
        assert code == 0
        assert started["stale_after"] == 45

    def test_the_default_is_zero(self, tmp_path, monkeypatch):
        from app import cli
        from app.core import api

        started = {}
        monkeypatch.setattr(
            api, "serve", lambda directory, host, port, **kwargs: started.update(kwargs)
        )
        assert cli.main(["serve", "--dir", str(tmp_path)]) == 0
        assert started["stale_after"] == 0


class TestMetricsCommand:
    """``metrics`` prints the textfile a Prometheus node exporter can scrape."""

    def _seed(self, directory):
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

    def test_the_gauges_are_printed(self, tmp_path, capsys):
        from app import cli

        self._seed(tmp_path)
        assert cli.main(["metrics", "--dir", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert "capture_bot_captures 1" in out
        assert "capture_bot_sites 1" in out
        assert 'capture_bot_health_state{state="ok"} 1' in out
        assert 'capture_bot_build_info{version="' in out
        assert out.endswith("\n")

    def test_the_stale_flag_reaches_the_verdict(self, tmp_path, capsys):
        from app import cli

        self._seed(tmp_path)
        assert cli.main(["metrics", "--dir", str(tmp_path), "--stale-after", "1"]) == 0
        out = capsys.readouterr().out
        assert 'capture_bot_health_state{state="stale"} 1' in out
        assert 'capture_bot_health_state{state="ok"} 0' in out


class TestQuietUrlsFlag:
    def test_the_flag_reaches_the_settings(self, tmp_path, monkeypatch):
        from app import cli

        captured = {}
        monkeypatch.setattr(
            cli,
            "_run_capture",
            lambda args, engine_factory=None: captured.update(vars(args)) or 0,
        )
        code = cli.main(
            [
                "capture",
                "https://a.example.com",
                "--out",
                str(tmp_path),
                "--quiet-urls",
                "staging.example.com=22:00-07:00",
            ]
        )
        assert code == 0
        assert captured["quiet_urls"] == "staging.example.com=22:00-07:00"

    def test_the_environment_can_supply_it(self, monkeypatch):
        from app import cli

        monkeypatch.setenv("CAPTURE_QUIET_URLS", "news.example.com=")
        args = cli.build_parser().parse_args(["https://a.example.com", "--out", "/tmp/x"])
        assert args.quiet_urls == "news.example.com="

    def test_the_settings_builder_stores_it(self, tmp_path):
        from app import cli

        args = cli.build_parser().parse_args(
            [
                "https://a.example.com",
                "--out",
                str(tmp_path),
                "--quiet-urls",
                "s.example.com=22:00-07:00",
            ]
        )
        settings = cli._settings_from_args(args)
        assert settings.alert_quiet_urls == "s.example.com=22:00-07:00"
        settings.validate()


class TestDigestSkipUnchanged:
    """A weekly digest that mails the same numbers forever is noise."""

    def _seed(self, directory, when="2026-01-01T00:00:00"):
        (directory / "capture-report-20260101-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": when,
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

    def _send(self, tmp_path, monkeypatch, extra=()):
        from app.core import digest

        sent = []
        monkeypatch.setattr(digest, "send_digest", lambda *a, **k: sent.append(a) or True)
        code = cli.main(
            [
                "digest",
                "--dir",
                str(tmp_path),
                "--send",
                "--smtp-host",
                "smtp.example.com",
                "--email-to",
                "ops@example.com",
                "--skip-if-unchanged",
                *extra,
            ]
        )
        return code, sent

    def test_the_first_digest_goes_out_and_leaves_a_marker(self, tmp_path, monkeypatch, capsys):
        self._seed(tmp_path)
        code, sent = self._send(tmp_path, monkeypatch)
        assert code == 0
        assert len(sent) == 1
        assert "Digest emailed to ops@example.com." in capsys.readouterr().out
        assert (tmp_path / ".digest-marker.json").exists()

    def test_a_second_run_without_new_captures_is_skipped(self, tmp_path, monkeypatch, capsys):
        self._seed(tmp_path)
        self._send(tmp_path, monkeypatch)
        capsys.readouterr()
        code, sent = self._send(tmp_path, monkeypatch)
        assert code == 0
        assert sent == []  # nothing mailed
        assert "Digest skipped: the history has not changed" in capsys.readouterr().out

    def test_force_overrides_the_skip(self, tmp_path, monkeypatch, capsys):
        self._seed(tmp_path)
        self._send(tmp_path, monkeypatch)
        capsys.readouterr()
        code, sent = self._send(tmp_path, monkeypatch, extra=("--force",))
        assert code == 0 and len(sent) == 1
        assert "Digest emailed" in capsys.readouterr().out

    def test_a_new_capture_makes_it_worth_sending_again(self, tmp_path, monkeypatch, capsys):
        self._seed(tmp_path)
        self._send(tmp_path, monkeypatch)
        capsys.readouterr()
        (tmp_path / "capture-report-20260102-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-02T00:00:00",
                    "results": [
                        {
                            "url": "https://a.example.com",
                            "status": "success",
                            "diff": 0.9,
                            "file_path": "",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        code, sent = self._send(tmp_path, monkeypatch)
        assert code == 0 and len(sent) == 1
        assert "Digest emailed" in capsys.readouterr().out

    def test_without_the_flag_it_always_sends(self, tmp_path, monkeypatch, capsys):
        from app.core import digest

        self._seed(tmp_path)
        digest.record_marker(tmp_path, 7, 1)
        sent = []
        monkeypatch.setattr(digest, "send_digest", lambda *a, **k: sent.append(a) or True)
        code = cli.main(
            [
                "digest",
                "--dir",
                str(tmp_path),
                "--send",
                "--smtp-host",
                "smtp.example.com",
                "--email-to",
                "ops@example.com",
            ]
        )
        assert code == 0 and len(sent) == 1
        capsys.readouterr()


class TestHistoryArchiveCli:
    """``history --archive/--older-than/--restore`` from the command line."""

    def _run(self, folder, stamp, kb=60):
        for suffix, filler in ((".json", b"x"), (".csv", b"y")):
            (folder / f"capture-report-{stamp}{suffix}").write_bytes(filler * (kb * 1024))

    def test_older_than_archives_and_reports_the_saving(self, tmp_path, capsys):
        self._run(tmp_path, "20200101-000000")
        self._run(tmp_path, "20200102-000000")
        code = cli.main(["history", "--dir", str(tmp_path), "--archive", "--older-than", "30"])
        out = capsys.readouterr().out
        assert code == 0
        assert "Archived 4 report file(s) into 1 archive(s)" in out
        assert "KB saved" in out
        assert (tmp_path / "archive-2020-01.zip").is_file()
        assert not list(tmp_path.glob("capture-report-2020*.json"))

    def test_a_dry_run_lists_the_files_and_keeps_them(self, tmp_path, capsys):
        self._run(tmp_path, "20200101-000000")
        code = cli.main(
            ["history", "--dir", str(tmp_path), "--archive", "--older-than", "30", "--dry-run"]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "Would archive 2 report file(s)" in out
        assert "capture-report-20200101-000000.json" in out
        assert list(tmp_path.glob("capture-report-2020*.json"))
        assert not (tmp_path / "archive-2020-01.zip").exists()

    def test_the_cap_archives_instead_of_deleting(self, tmp_path, capsys):
        for index in range(1, 9):  # ~2.4 MB of reports against a 1 MB cap
            self._run(tmp_path, f"2026010{index}-000000", kb=150)
        code = cli.main(["history", "--dir", str(tmp_path), "--prune-history-mb", "1", "--archive"])
        out = capsys.readouterr().out
        assert code == 0
        assert "archived to stay under 1 MB" in out
        assert "kept in archive-2026-01.zip" in out
        assert (tmp_path / "archive-2026-01.zip").is_file()

    def test_the_cap_without_the_flag_deletes(self, tmp_path, capsys):
        for index in range(1, 9):
            self._run(tmp_path, f"2026010{index}-000000", kb=150)
        code = cli.main(["history", "--dir", str(tmp_path), "--prune-history-mb", "1"])
        out = capsys.readouterr().out
        assert code == 0
        assert "deleted to stay under 1 MB" in out
        assert not (tmp_path / "archive-2026-01.zip").exists()

    def test_the_cap_dry_run_says_archive_not_delete(self, tmp_path, capsys):
        for index in range(1, 9):
            self._run(tmp_path, f"2026010{index}-000000", kb=150)
        code = cli.main(
            ["history", "--dir", str(tmp_path), "--prune-history-mb", "1", "--archive", "--dry-run"]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "Would archive 10 report file(s) to stay under 1 MB" in out
        assert not (tmp_path / "archive-2026-01.zip").exists()

    def test_nothing_old_enough_is_reported(self, tmp_path, capsys):
        self._run(tmp_path, "29990101-000000")
        code = cli.main(["history", "--dir", str(tmp_path), "--archive", "--older-than", "30"])
        assert code == 0
        assert "Nothing older than 30 day(s) to archive." in capsys.readouterr().out

    def test_restore_brings_the_files_back_and_reindexes(self, tmp_path, capsys):
        for index in (1, 2):
            stamp = f"2026010{index}-000000"
            payload = json.dumps(
                {
                    "generated_at": f"2026-01-0{index}T00:00:00",
                    "results": [{"url": "https://a.example.com", "status": "success", "diff": 0.1}],
                }
            ).encode("utf-8")
            (tmp_path / f"capture-report-{stamp}.json").write_bytes(payload)
            (tmp_path / f"capture-report-{stamp}.csv").write_bytes(b"csv")
        cli.main(["history", "--dir", str(tmp_path), "--archive", "--older-than", "30"])
        capsys.readouterr()
        code = cli.main(["history", "--dir", str(tmp_path), "--restore", "archive-2026-01.zip"])
        out = capsys.readouterr().out
        assert code == 0
        assert "Restored 4 file(s) from archive-2026-01.zip." in out
        assert "Index rebuilt: 2 row(s)." in out
        assert (tmp_path / "capture-report-20260101-000000.json").is_file()

    def test_a_restore_dry_run_keeps_the_archive_untouched(self, tmp_path, capsys):
        self._run(tmp_path, "20200101-000000")
        cli.main(["history", "--dir", str(tmp_path), "--archive", "--older-than", "30"])
        capsys.readouterr()
        code = cli.main(
            ["history", "--dir", str(tmp_path), "--restore", "archive-2020-01.zip", "--dry-run"]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "Would restore 2 file(s) from archive-2020-01.zip" in out
        assert not list(tmp_path.glob("capture-report-2020*.json"))

    def test_a_broken_zip_is_a_clean_error(self, tmp_path, capsys):
        (tmp_path / "archive-2020-01.zip").write_text("not a zip", encoding="utf-8")
        code = cli.main(["history", "--dir", str(tmp_path), "--restore", "archive-2020-01.zip"])
        assert code == 2
        assert "Could not read archive-2020-01.zip" in capsys.readouterr().err

    def test_restoring_nothing_is_reported(self, tmp_path, capsys):
        self._run(tmp_path, "20200101-000000")
        cli.main(["history", "--dir", str(tmp_path), "--archive", "--older-than", "30"])
        cli.main(["history", "--dir", str(tmp_path), "--restore", "archive-2020-01.zip"])
        capsys.readouterr()
        code = cli.main(["history", "--dir", str(tmp_path), "--restore", "archive-2020-01.zip"])
        assert code == 0
        assert "Nothing to restore from archive-2020-01.zip" in capsys.readouterr().out


class TestMetricsFile:
    """``metrics --out`` for a systemd timer / cron job."""

    def _seed(self, tmp_path):
        (tmp_path / "capture-report-20260101-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "results": [{"url": "https://a.example.com", "status": "success", "diff": 0.1}],
                }
            ),
            encoding="utf-8",
        )

    def test_out_writes_the_file_and_says_so(self, tmp_path, capsys):
        self._seed(tmp_path)
        target = tmp_path / "capture_bot.prom"
        code = cli.main(["metrics", "--dir", str(tmp_path), "--out", str(target)])
        out = capsys.readouterr().out
        assert code == 0
        assert f"Metrics written to {target} (" in out
        assert "capture_bot_captures 1" in target.read_text(encoding="utf-8")
        assert "capture_bot_last_success_timestamp_seconds" in target.read_text(encoding="utf-8")

    def test_stdout_stays_the_default(self, tmp_path, capsys):
        self._seed(tmp_path)
        assert cli.main(["metrics", "--dir", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert out.startswith("# HELP capture_bot_captures")

    def test_write_if_changed_keeps_the_old_file(self, tmp_path, capsys):
        self._seed(tmp_path)
        # Outside the watched folder: the file's own bytes are part of the
        # folder gauge, so a metrics file *inside* it can never stop changing.
        target = tmp_path.parent / "textfile" / "capture_bot.prom"
        cli.main(["metrics", "--dir", str(tmp_path), "--out", str(target)])
        before = target.stat().st_mtime_ns
        capsys.readouterr()
        code = cli.main(
            ["metrics", "--dir", str(tmp_path), "--out", str(target), "--write-if-changed"]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "Metrics unchanged in" in out
        assert target.stat().st_mtime_ns == before

    def test_a_new_capture_rewrites_the_file(self, tmp_path, capsys):
        self._seed(tmp_path)
        target = tmp_path / "capture_bot.prom"
        cli.main(["metrics", "--dir", str(tmp_path), "--out", str(target), "--write-if-changed"])
        (tmp_path / "capture-report-20260102-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-02T00:00:00",
                    "results": [{"url": "https://a.example.com", "status": "success", "diff": 0.2}],
                }
            ),
            encoding="utf-8",
        )
        capsys.readouterr()
        code = cli.main(
            ["metrics", "--dir", str(tmp_path), "--out", str(target), "--write-if-changed"]
        )
        assert code == 0
        assert "Metrics written to" in capsys.readouterr().out
        assert "capture_bot_captures 2" in target.read_text(encoding="utf-8")

    def test_an_unwritable_target_is_a_clean_error(self, tmp_path, capsys):
        self._seed(tmp_path)
        code = cli.main(["metrics", "--dir", str(tmp_path), "--out", str(tmp_path)])
        assert code == 2
        assert "Could not write the metrics" in capsys.readouterr().err


class TestDashboardStorageJson:
    """``dashboard --storage-json`` is the cron-friendly twin of /api/storage."""

    def _report(self, directory):
        (directory / "capture-report-20260101-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "results": [{"url": "https://a.example.com", "status": "success", "diff": 0.1}],
                }
            ),
            encoding="utf-8",
        )

    def test_the_report_is_written_next_to_the_dashboard(self, tmp_path, capsys):
        self._report(tmp_path)
        out = tmp_path / "dashboard.html"
        report = tmp_path / "storage.json"
        code = cli.main(
            [
                "dashboard",
                "--dir",
                str(tmp_path),
                "--out",
                str(out),
                "--caps",
                "500,50",
                "--storage-json",
                str(report),
            ]
        )
        printed = capsys.readouterr().out
        assert code == 0
        assert f"Storage report written to {report}" in printed
        payload = json.loads(report.read_text(encoding="utf-8"))
        assert payload["reports"] == 1
        assert payload["caps"] == {"screenshots_mb": 500, "history_mb": 50}
        assert payload["total"] > 0

    def test_the_numbers_match_the_panel(self, tmp_path, capsys):
        from app.core.dashboard import storage_stats

        self._report(tmp_path)
        # Outside the measured folder: the report itself must not change the numbers.
        report = tmp_path.parent / "storage.json"
        cli.main(
            [
                "dashboard",
                "--dir",
                str(tmp_path),
                "--out",
                str(tmp_path / "d.html"),
                "--caps",
                "10",
                "--storage-json",
                str(report),
            ]
        )
        payload = json.loads(report.read_text(encoding="utf-8"))
        expected = storage_stats(tmp_path, {"screenshots_mb": 10})
        assert payload["total"] == expected["total"]
        assert payload["history"] == expected["history"]

    def test_a_bad_cap_stops_before_writing_anything(self, tmp_path, capsys):
        report = tmp_path / "storage.json"
        code = cli.main(
            [
                "dashboard",
                "--dir",
                str(tmp_path),
                "--out",
                str(tmp_path / "d.html"),
                "--caps",
                "big",
                "--storage-json",
                str(report),
            ]
        )
        assert code == 2
        assert "--caps expects MB numbers" in capsys.readouterr().err
        assert not report.exists()

    def test_an_unwritable_report_is_a_clean_error(self, tmp_path, capsys):
        code = cli.main(
            [
                "dashboard",
                "--dir",
                str(tmp_path),
                "--out",
                str(tmp_path / "d.html"),
                "--storage-json",
                str(tmp_path),
            ]
        )
        assert code == 2
        assert "Could not write the storage report" in capsys.readouterr().err


class TestRouteUrlsFlag:
    """``--route-urls`` (and CAPTURE_ROUTE_URLS) for headless runs."""

    def test_the_flag_reaches_the_settings(self):
        args = cli.build_parser().parse_args(
            ["https://a", "--out", "x", "--route-urls", "staging=https://hooks/x"]
        )
        assert cli._settings_from_args(args).alert_route_urls == "staging=https://hooks/x"

    def test_the_env_variable_is_the_default(self, monkeypatch):
        monkeypatch.setenv("CAPTURE_ROUTE_URLS", "news=https://hooks/news")
        args = cli.build_parser().parse_args(["https://a", "--out", "x"])
        assert cli._settings_from_args(args).alert_route_urls == "news=https://hooks/news"

    def test_the_default_is_empty(self, monkeypatch):
        monkeypatch.delenv("CAPTURE_ROUTE_URLS", raising=False)
        args = cli.build_parser().parse_args(["https://a", "--out", "x"])
        assert cli._settings_from_args(args).alert_route_urls == ""


def _site_files(folder, label="a_example_com", sizes=(300, 100)):
    """Two captures of one site plus a reference and a report, on disk."""
    for index, size in enumerate(sizes, start=1):
        (folder / f"{index:03d}_{label}_2026010{index}-000000.png").write_bytes(b"p" * size)
    (folder / f"latest_{label}.png").write_bytes(b"r" * 10)
    (folder / "capture-report-20260101-000000.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-01-01T00:00:00",
                "results": [{"url": "https://a.example.com", "status": "success", "diff": 0.1}],
            }
        ),
        encoding="utf-8",
    )


class TestTopSpaceCli:
    """``history --top-space`` names the hosts that cost the most."""

    def test_the_table_lists_the_biggest_site_first(self, tmp_path, capsys):
        _site_files(tmp_path, "a_example_com", sizes=(300, 100))
        _site_files(tmp_path, "b_example_com", sizes=(900,))
        code = cli.main(["history", "--dir", str(tmp_path), "--top-space"])
        out = capsys.readouterr().out
        assert code == 0
        assert "BYTES" in out and "SITE" in out
        lines = [line for line in out.splitlines() if line.strip().endswith("_com")]
        assert lines[0].split()[-1] == "b_example_com"
        # The reference file counts towards the site's footprint (910 = 900 + 10).
        assert lines[0].split()[0] == "910"
        assert lines[1].split()[-1] == "a_example_com"
        assert "never pruned" in out

    def test_the_limit_keeps_the_top_rows_only(self, tmp_path, capsys):
        _site_files(tmp_path, "a_example_com", sizes=(100,))
        _site_files(tmp_path, "b_example_com", sizes=(900,))
        cli.main(["history", "--dir", str(tmp_path), "--top-space", "--limit", "1"])
        out = capsys.readouterr().out
        assert "b_example_com" in out and "a_example_com" not in out

    def test_json_output_is_machine_readable(self, tmp_path, capsys):
        _site_files(tmp_path, "a_example_com", sizes=(300, 100))
        code = cli.main(["history", "--dir", str(tmp_path), "--top-space", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload[0]["label"] == "a_example_com"
        assert payload[0]["bytes"] == 410
        assert payload[0]["references"] == 1

    def test_an_empty_folder_says_so(self, tmp_path, capsys):
        code = cli.main(["history", "--dir", str(tmp_path), "--top-space"])
        assert code == 0
        assert "No captures to measure." in capsys.readouterr().out


class TestPruneSiteCli:
    """``history --prune-site`` deletes exactly one site's captures."""

    def test_a_dry_run_lists_the_files_and_keeps_them(self, tmp_path, capsys):
        _site_files(tmp_path)
        code = cli.main(
            ["history", "--dir", str(tmp_path), "--prune-site", "a.example.com", "--dry-run"]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "Would delete 2 file(s) matching 'a.example.com' (0.4 KB)." in out
        assert "001_a_example_com_20260101-000000.png" in out
        assert (tmp_path / "001_a_example_com_20260101-000000.png").is_file()

    def test_the_files_really_go_away(self, tmp_path, capsys):
        _site_files(tmp_path)
        code = cli.main(["history", "--dir", str(tmp_path), "--prune-site", "a_example_com"])
        out = capsys.readouterr().out
        assert code == 0
        assert "Deleted 2 file(s) matching 'a_example_com' (0.4 KB)." in out
        assert not (tmp_path / "001_a_example_com_20260101-000000.png").exists()
        assert (tmp_path / "latest_a_example_com.png").is_file()
        assert (tmp_path / "capture-report-20260101-000000.json").is_file()

    def test_an_unknown_site_is_reported_not_guessed(self, tmp_path, capsys):
        _site_files(tmp_path)
        code = cli.main(["history", "--dir", str(tmp_path), "--prune-site", "zzz.example.com"])
        assert code == 0
        assert "No captures found for 'zzz.example.com'." in capsys.readouterr().out

    def test_the_dry_run_lists_at_most_limit_files(self, tmp_path, capsys):
        _site_files(tmp_path)
        cli.main(
            [
                "history",
                "--dir",
                str(tmp_path),
                "--prune-site",
                "a.example.com",
                "--dry-run",
                "--limit",
                "1",
            ]
        )
        out = capsys.readouterr().out
        assert out.count("_a_example_com_") == 1


class TestStorageSeriesFlag:
    """``dashboard --storage-series N`` puts the measured trend in the file."""

    @staticmethod
    def _seed_samples(folder, days=4, step=25_000):
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

    def test_the_flag_writes_the_newest_samples(self, tmp_path, capsys):
        self._seed_samples(tmp_path)
        target = tmp_path.parent / "storage.json"
        code = cli.main(
            [
                "dashboard",
                "--dir",
                str(tmp_path),
                "--out",
                str(tmp_path / "dashboard.html"),
                "--storage-json",
                str(target),
                "--storage-series",
                "2",
            ]
        )
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert code == 0
        assert len(payload["series"]) == 2
        assert payload["growth"]["source"] == "samples"
        assert "Storage report written to" in capsys.readouterr().out

    def test_without_the_flag_the_report_stays_small(self, tmp_path):
        self._seed_samples(tmp_path)
        target = tmp_path.parent / "storage-plain.json"
        cli.main(
            [
                "dashboard",
                "--dir",
                str(tmp_path),
                "--out",
                str(tmp_path / "dashboard.html"),
                "--storage-json",
                str(target),
            ]
        )
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert "series" not in payload
        assert payload["growth"]["source"] == "samples"


def _success_report(folder, minutes_ago=1.0, status_value="success", stamp=None):
    """One report whose last capture succeeded (or did not) minutes ago."""
    from datetime import datetime, timedelta

    when = datetime.now() - timedelta(minutes=minutes_ago)
    stamp = stamp or when.strftime("%Y%m%d-%H%M%S")
    (folder / f"capture-report-{stamp}.json").write_text(
        json.dumps(
            {
                "generated_at": when.isoformat(timespec="seconds"),
                "results": [{"url": "https://a.example.com", "status": status_value, "diff": 0.1}],
            }
        ),
        encoding="utf-8",
    )


class TestMetricsCheck:
    """``metrics --check`` is the exit code a systemd OnFailure= or cron reads."""

    def test_a_recent_success_exits_zero(self, tmp_path, capsys):
        _success_report(tmp_path)
        code = cli.main(["metrics", "--dir", str(tmp_path), "--check", "--max-age", "90"])
        out = capsys.readouterr().out
        assert code == 0
        assert out.startswith("OK: The last successful capture was ")

    def test_an_old_success_exits_one(self, tmp_path, capsys):
        _success_report(tmp_path, minutes_ago=600)
        code = cli.main(["metrics", "--dir", str(tmp_path), "--check", "--max-age", "90"])
        out = capsys.readouterr().out
        assert code == 1
        assert "FAILED: The last successful capture was 600 minute(s) ago" in out
        assert "more than the 90 minute(s) allowed" in out

    def test_a_history_of_only_failures_exits_one(self, tmp_path, capsys):
        _success_report(tmp_path, status_value="failed")
        code = cli.main(["metrics", "--dir", str(tmp_path), "--check", "--max-age", "90"])
        assert code == 1
        assert "No successful capture has been recorded yet." in capsys.readouterr().out

    def test_the_stale_after_value_is_the_fallback_limit(self, tmp_path, capsys):
        _success_report(tmp_path, minutes_ago=45)
        code = cli.main(["metrics", "--dir", str(tmp_path), "--stale-after", "30", "--check"])
        assert code == 1
        assert "more than the 30 minute(s) allowed" in capsys.readouterr().out

    def test_without_a_limit_only_existence_matters(self, tmp_path, capsys):
        _success_report(tmp_path, minutes_ago=10_000)
        assert cli.main(["metrics", "--dir", str(tmp_path), "--check"]) == 0
        assert capsys.readouterr().out.startswith("OK: ")

    def test_json_verdict_is_machine_readable(self, tmp_path, capsys):
        _success_report(tmp_path, minutes_ago=600)
        code = cli.main(["metrics", "--dir", str(tmp_path), "--check", "--max-age", "90", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 1
        assert payload["ok"] is False
        assert payload["max_age_minutes"] == 90
        assert payload["age_seconds"] >= 600 * 60 - 5
        assert payload["last_success"]["url"] == "https://a.example.com"

    def test_the_caps_reach_the_storage_gauges(self, tmp_path, capsys):
        _success_report(tmp_path)
        code = cli.main(["metrics", "--dir", str(tmp_path), "--caps", "500,50"])
        out = capsys.readouterr().out
        assert code == 0
        assert "capture_bot_history_cap_bytes 52428800" in out
        assert "capture_bot_screenshots_cap_bytes 524288000" in out
        assert "capture_bot_total_bytes " in out

    def test_bad_caps_are_refused(self, tmp_path, capsys):
        code = cli.main(["metrics", "--dir", str(tmp_path), "--caps", "lots"])
        assert code == 2
        assert "--caps expects MB numbers" in capsys.readouterr().err

    def test_the_out_file_is_written_even_when_the_check_fails(self, tmp_path, capsys):
        _success_report(tmp_path, minutes_ago=600)
        target = tmp_path.parent / "capture_bot.prom"
        code = cli.main(
            [
                "metrics",
                "--dir",
                str(tmp_path),
                "--out",
                str(target),
                "--write-if-changed",
                "--check",
                "--max-age",
                "90",
            ]
        )
        out = capsys.readouterr().out
        assert code == 1
        assert "Metrics written to" in out
        assert "capture_bot_captures 1" in target.read_text(encoding="utf-8")

    def test_without_check_and_without_out_the_text_goes_to_stdout(self, tmp_path, capsys):
        _success_report(tmp_path)
        assert cli.main(["metrics", "--dir", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert out.count("# HELP capture_bot_") >= 5
        assert "capture_bot_total_bytes" in out  # the storage gauges travel too

    def test_the_serve_parser_takes_caps(self):
        args = cli.build_serve_parser().parse_args(["--dir", "shots", "--caps", "500,50"])
        assert cli._parse_caps(args.caps) == {"screenshots_mb": 500, "history_mb": 50}

    def test_the_metrics_parser_takes_caps_and_check(self):
        args = cli.build_metrics_parser().parse_args(
            ["--dir", "shots", "--check", "--max-age", "90", "--caps", "1,2", "--json"]
        )
        assert args.check is True and args.max_age == 90 and args.json is True
        assert cli._parse_caps(args.caps) == {"screenshots_mb": 1, "history_mb": 2}


class TestChannelsSubcommand:
    """``channels``: check the file before a page changes, not after."""

    SAMPLE_HEADER = "# One table per destination."

    def test_the_sample_can_be_used_as_it_is(self, tmp_path, capsys):
        assert cli.main(["channels", "--sample"]) == 0
        text = capsys.readouterr().out
        assert text.startswith(self.SAMPLE_HEADER)
        from app.core import channels

        target = tmp_path / "channels.toml"
        target.write_text(text, encoding="utf-8")
        parsed = channels.load_channels(target)
        assert [channel.name for channel in parsed] == ["ops", "shop", "team", "pager"]
        assert parsed[1].min_diff == 0.01
        assert parsed[3].kind == "command" and parsed[3].command[0].endswith("page-oncall")

    def test_a_good_file_is_summarised(self, tmp_path, capsys):
        target = tmp_path / "channels.toml"
        target.write_text(
            '[ops]\nkind = "webhook"\nurl = "https://hooks/ops"\nmatch = "staging"\n',
            encoding="utf-8",
        )
        assert cli.main(["channels", "--file", str(target)]) == 0
        out = capsys.readouterr().out
        assert "1 channel(s) (1 enabled):" in out
        assert "match: staging" in out

    def test_json_output_is_for_scripts(self, tmp_path, capsys):
        target = tmp_path / "channels.toml"
        target.write_text('[ops]\nurl = "https://hooks/ops"\n', encoding="utf-8")
        assert cli.main(["channels", "--file", str(target), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["channels"][0]["target"] == "https://hooks/ops"
        assert payload["channels"][0]["kind"] == "webhook"

    def test_without_a_file_it_says_which_flag(self, capsys):
        assert cli.main(["channels"]) == 2
        err = capsys.readouterr().err
        assert "--file" in err
        assert "--sample" in err

    def test_a_missing_file_is_an_error_not_a_traceback(self, tmp_path, capsys):
        assert cli.main(["channels", "--file", str(tmp_path / "nope.toml")]) == 2
        assert "[ERROR]" in capsys.readouterr().err

    def test_a_broken_file_names_the_offending_key(self, tmp_path, capsys):
        target = tmp_path / "channels.toml"
        target.write_text('[ops]\nurl = "https://hooks/ops"\nquiett = "1"\n', encoding="utf-8")
        assert cli.main(["channels", "--file", str(target)]) == 2
        assert "quiett" in capsys.readouterr().err

    def test_test_sends_through_every_channel(self, tmp_path, capsys, monkeypatch):
        from app.core import alerts

        sent: list[str] = []
        monkeypatch.setattr(
            alerts, "send_webhook", lambda url, payload, timeout=10.0: sent.append(url) or True
        )
        target = tmp_path / "channels.toml"
        target.write_text(
            '[ops]\nurl = "https://hooks/ops"\n\n[off]\nurl = "https://hooks/off"\nenabled = false\n',
            encoding="utf-8",
        )
        assert cli.main(["channels", "--file", str(target), "--test"]) == 0
        out = capsys.readouterr().out
        assert "ops:webhook: sent" in out
        assert sent == ["https://hooks/ops"]
        assert "off:webhook" not in out  # a disabled channel is not exercised

    def test_a_failed_test_send_exits_non_zero(self, tmp_path, capsys, monkeypatch):
        from app.core import alerts

        monkeypatch.setattr(alerts, "send_webhook", lambda url, payload, timeout=10.0: False)
        target = tmp_path / "channels.toml"
        target.write_text('[ops]\nurl = "https://hooks/ops"\n', encoding="utf-8")
        assert cli.main(["channels", "--file", str(target), "--test"]) == 1
        assert "ops:webhook: FAILED" in capsys.readouterr().out

    def test_a_file_with_no_enabled_channel_teaches_the_field(self, tmp_path, capsys):
        target = tmp_path / "channels.toml"
        target.write_text('[ops]\nurl = "https://hooks/ops"\nenabled = false\n', encoding="utf-8")
        assert cli.main(["channels", "--file", str(target), "--test"]) == 2
        out = capsys.readouterr().out
        assert "enabled" in out

    def test_the_capture_parser_hands_the_file_over(self):
        args = cli.build_parser().parse_args(
            ["--out", "shots", "--url", "https://a.example.com", "--channels", "ch.toml"]
        )
        assert cli._settings_from_args(args).alert_channels == "ch.toml"

    def test_the_env_var_is_the_default(self, monkeypatch):
        monkeypatch.setenv("CAPTURE_CHANNELS", "/etc/capture/channels.toml")
        args = cli.build_parser().parse_args(["--out", "shots", "--url", "https://a.example.com"])
        assert cli._settings_from_args(args).alert_channels == "/etc/capture/channels.toml"


class TestChannelsHeartbeat:
    """``channels --heartbeat``: the proof-of-life note, on demand."""

    @staticmethod
    def _file(tmp_path, text='[ops]\nurl = "https://hooks/ops"\nheartbeat = "09:00"\n'):
        target = tmp_path / "channels.toml"
        target.write_text(text, encoding="utf-8")
        folder = tmp_path / "shots"
        folder.mkdir(exist_ok=True)
        return target, folder

    def test_a_dry_run_says_who_is_due(self, tmp_path, capsys, monkeypatch):
        from app.core import alerts

        sent: list = []
        monkeypatch.setattr(
            alerts, "send_webhook", lambda url, payload, timeout=10.0: sent.append(payload) or True
        )
        target, folder = self._file(tmp_path)
        code = cli.main(
            [
                "channels",
                "--file",
                str(target),
                "--heartbeat",
                "--dry-run",
                "--dir",
                str(folder),
                "--days",
                "3",
            ]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "ops:heartbeat: would send" in out
        assert "note: Still here:" in out
        assert "in the last 3 day(s)" in out
        assert sent == []
        assert not (folder / ".channel-heartbeats.json").exists()

    def test_a_real_beat_is_sent_and_remembered(self, tmp_path, capsys, monkeypatch):
        from app.core import alerts

        sent: list = []
        monkeypatch.setattr(
            alerts, "send_webhook", lambda url, payload, timeout=10.0: sent.append(payload) or True
        )
        target, folder = self._file(tmp_path)
        code = cli.main(["channels", "--file", str(target), "--heartbeat", "--dir", str(folder)])
        assert code == 0
        assert "ops:heartbeat: sent" in capsys.readouterr().out
        assert sent and sent[0]["event"] == "notice"
        assert "ops" in (folder / ".channel-heartbeats.json").read_text(encoding="utf-8")
        # Nothing is due a second later.
        assert (
            cli.main(["channels", "--file", str(target), "--heartbeat", "--dir", str(folder)]) == 0
        )
        assert "No channel is due" in capsys.readouterr().out

    def test_a_failed_beat_exits_non_zero(self, tmp_path, capsys, monkeypatch):
        from app.core import alerts

        monkeypatch.setattr(alerts, "send_webhook", lambda url, payload, timeout=10.0: False)
        target, folder = self._file(tmp_path)
        code = cli.main(["channels", "--file", str(target), "--heartbeat", "--dir", str(folder)])
        assert code == 1
        assert "ops:heartbeat: FAILED" in capsys.readouterr().out

    def test_a_channel_without_a_schedule_says_so(self, tmp_path, capsys):
        target, folder = self._file(tmp_path, text='[ops]\nurl = "https://hooks/ops"\n')
        assert (
            cli.main(["channels", "--file", str(target), "--heartbeat", "--dir", str(folder)]) == 0
        )
        assert "No channel is due for a heartbeat." in capsys.readouterr().out

    def test_the_command_form_works_too(self, tmp_path, capsys, monkeypatch):
        written = tmp_path / "beaten.txt"
        script = (
            "import pathlib, sys; "
            f"pathlib.Path({str(written)!r}).write_bytes(sys.stdin.buffer.read())"
        )
        target, folder = self._file(
            tmp_path,
            text=(
                '[pager]\nkind = "command"\n'
                f'exec = [{json.dumps(sys.executable)}, "-c", {json.dumps(script)}]\n'
                'heartbeat = "09:00"\n'
            ),
        )
        code = cli.main(["channels", "--file", str(target), "--heartbeat", "--dir", str(folder)])
        assert code == 0
        assert "pager:heartbeat: sent" in capsys.readouterr().out
        assert json.loads(written.read_text(encoding="utf-8"))["event"] == "notice"


class TestCompareCli:
    """``history --compare``: two periods, one table, optional PDF."""

    @staticmethod
    def _folder(tmp_path, rows):
        from datetime import datetime, timedelta

        now = datetime.now()
        folder = tmp_path / "shots"
        folder.mkdir(exist_ok=True)
        for index, (days_ago, label, diff, size) in enumerate(rows):
            when = now - timedelta(days=days_ago)
            stamp = when.strftime("%Y%m%d-%H%M%S")
            shot = folder / f"{label}_{stamp}_{index}.png"
            shot.write_bytes(b"x" * size)
            (folder / f"capture-report-{stamp}-{index}.json").write_text(
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
        return folder

    def test_one_period_compares_with_the_one_before_it(self, tmp_path, capsys):
        folder = self._folder(
            tmp_path,
            [(40, "old_example_com", 0.2, 100_000), (5, "new_example_com", 0.3, 50_000)],
        )
        assert cli.main(["history", "--dir", str(folder), "--compare", "30d:today"]) == 0
        out = capsys.readouterr().out
        assert "Comparing" in out and "with" in out
        assert "new_example_com" in out
        assert "new" in out  # it did not exist in the previous window

    def test_two_periods_are_both_read(self, tmp_path, capsys):
        folder = self._folder(tmp_path, [(10, "a_example_com", 0.4, 1_000)])
        code = cli.main(["history", "--dir", str(folder), "--compare", "60d:30d", "30d:today"])
        out = capsys.readouterr().out
        assert code == 0
        assert "a_example_com" in out
        assert "0 -> 1 (+1)" in out

    def test_json_is_machine_readable(self, tmp_path, capsys):
        folder = self._folder(tmp_path, [(5, "a_example_com", 0.4, 2_000)])
        assert cli.main(["history", "--dir", str(folder), "--compare", "30d", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert set(data) == {"periods", "totals", "sites", "site_count"}
        assert data["sites"][0]["captures"]["b"] == 1
        assert data["totals"]["bytes"]["b"] == 2_000

    def test_the_limit_trims_the_text_and_the_json(self, tmp_path, capsys):
        folder = self._folder(
            tmp_path, [(5, f"site{index}_example_com", None, 1_000) for index in range(6)]
        )
        assert (
            cli.main(
                ["history", "--dir", str(folder), "--compare", "30d", "--json", "--limit", "2"]
            )
            == 0
        )
        assert len(json.loads(capsys.readouterr().out)["sites"]) == 2

    def test_the_pdf_is_written_when_asked(self, tmp_path, capsys):
        folder = self._folder(tmp_path, [(5, "a_example_com", 0.4, 2_000)])
        target = tmp_path / "compare.pdf"
        code = cli.main(
            [
                "history",
                "--dir",
                str(folder),
                "--compare",
                "30d",
                "--compare-pdf",
                str(target),
            ]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "Comparison PDF written to" in out
        assert target.read_bytes().startswith(b"%PDF")

    def test_a_silly_period_is_refused(self, tmp_path, capsys):
        folder = self._folder(tmp_path, [(5, "a_example_com", None, 1_000)])
        assert cli.main(["history", "--dir", str(folder), "--compare", "soon:today"]) == 2
        assert "is not a date range end" in capsys.readouterr().err

    def test_a_reversed_period_is_refused(self, tmp_path, capsys):
        folder = self._folder(tmp_path, [(5, "a_example_com", None, 1_000)])
        assert cli.main(["history", "--dir", str(folder), "--compare", "today:30d"]) == 2
        assert "ends before it starts" in capsys.readouterr().err

    def test_an_empty_folder_says_so(self, tmp_path, capsys):
        folder = tmp_path / "shots"
        folder.mkdir()
        assert cli.main(["history", "--dir", str(folder), "--compare", "30d"]) == 0
        assert "no captures recorded in either period." in capsys.readouterr().out


class TestSiteCapsCli:
    """``history --site-caps`` trims now; ``capture --site-caps`` trims after each run."""

    @staticmethod
    def _shots(tmp_path, label, count, kb):
        for index in range(count):
            name = f"{index + 1:03d}_{label}_2026{index + 1:02d}01-090000.png"
            (tmp_path / name).write_bytes(b"x" * (kb * 1024))
        return tmp_path

    def test_a_dry_run_lists_what_would_go(self, tmp_path, capsys):
        self._shots(tmp_path, "news_example_com", 6, 300)
        code = cli.main(
            ["history", "--dir", str(tmp_path), "--site-caps", "news.example.com=1", "--dry-run"]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "would delete 3 file(s)" in out
        assert "site(s) over budget" in out
        assert len(list(tmp_path.glob("*_news_example_com_*.png"))) == 6  # nothing happened

    def test_the_trim_really_deletes(self, tmp_path, capsys):
        self._shots(tmp_path, "news_example_com", 6, 300)
        assert (
            cli.main(["history", "--dir", str(tmp_path), "--site-caps", "news.example.com=1"]) == 0
        )
        assert "deleted 3 file(s)" in capsys.readouterr().out
        assert len(list(tmp_path.glob("*_news_example_com_*.png"))) == 3

    def test_json_is_machine_readable(self, tmp_path, capsys):
        self._shots(tmp_path, "news_example_com", 6, 300)
        code = cli.main(
            ["history", "--dir", str(tmp_path), "--site-caps", "1", "--dry-run", "--json"]
        )
        data = json.loads(capsys.readouterr().out)
        assert code == 0
        assert data[0]["site"] == "news_example_com"
        assert data[0]["dry_run"] is True

    def test_a_folder_that_fits_says_so(self, tmp_path, capsys):
        self._shots(tmp_path, "news_example_com", 2, 100)
        code = cli.main(["history", "--dir", str(tmp_path), "--site-caps", "news.example.com=10"])
        assert code == 0
        assert "Every site fits its budget" in capsys.readouterr().out

    def test_a_bad_spec_exits_two(self, tmp_path, capsys):
        code = cli.main(["history", "--dir", str(tmp_path), "--site-caps", "news.example.com=x"])
        assert code == 2
        assert "is not a size in MB" in capsys.readouterr().err

    def test_the_capture_parser_carries_the_caps_into_the_settings(self):
        args = cli.build_parser().parse_args(
            ["https://a.com", "--out", "shots", "--site-caps", "news.example.com=500,*=1000"]
        )
        assert args.site_caps == "news.example.com=500,*=1000"
        assert cli._settings_from_args(args).site_caps == "news.example.com=500,*=1000"

    def test_the_capture_parser_reads_the_environment(self, monkeypatch):
        monkeypatch.setenv("CAPTURE_SITE_CAPS", "*=250")
        args = cli.build_parser().parse_args(["https://a.com", "--out", "shots"])
        assert cli._settings_from_args(args).site_caps == "*=250"

    def test_the_metrics_parser_takes_per_site_budgets(self):
        args = cli.build_metrics_parser().parse_args(
            ["--dir", "shots", "--caps", "500,50", "--site-caps", "news.example.com=200"]
        )
        caps = cli._caps_from_args(args)
        assert caps == {
            "screenshots_mb": 500,
            "history_mb": 50,
            "site_caps": {"news.example.com": 200.0},
        }

    def test_a_per_site_budget_can_ride_inside_caps(self):
        caps = cli._parse_caps("500,50,news.example.com=200,*=1000")
        assert caps["screenshots_mb"] == 500
        assert caps["site_caps"] == {"news.example.com": 200.0, "*": 1000.0}

    def test_caps_beyond_the_two_numbers_are_still_refused(self, tmp_path, capsys):
        args = cli.build_metrics_parser().parse_args(["--dir", str(tmp_path), "--caps", "1,2,3"])
        assert cli._caps_from_args(args) is None
        assert "--caps expects MB numbers" in capsys.readouterr().err
