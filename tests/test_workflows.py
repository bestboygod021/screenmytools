"""Validate the GitHub Actions workflows (YAML shape + key steps).

A malformed workflow file fails silently until the run happens, so we parse
every file and assert the dashboard publisher really builds and uploads what we
think it does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _steps(job: dict) -> list[dict]:
    return job.get("steps", [])


def _run_text(job: dict) -> str:
    return "\n".join(str(step.get("run", "")) for step in _steps(job))


class TestAllWorkflows:
    def test_every_workflow_is_valid_yaml(self):
        files = sorted(WORKFLOWS.glob("*.yml"))
        assert files, "expected at least one workflow file"
        for path in files:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            assert isinstance(data, dict), f"{path.name} is not a mapping"
            assert "jobs" in data, f"{path.name} has no jobs"

    def test_every_job_has_steps_and_a_runner(self):
        for path in sorted(WORKFLOWS.glob("*.yml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job_name, job in data["jobs"].items():
                assert "runs-on" in job, f"{path.name}:{job_name} has no runs-on"
                assert job.get("steps"), f"{path.name}:{job_name} has no steps"


class TestDashboardWorkflow:
    def _dashboard(self) -> dict:
        return _load("dashboard.yml")

    def test_triggers_on_schedule_and_manual_dispatch(self):
        data = self._dashboard()
        # PyYAML reads the bare `on:` key as the boolean True.
        triggers = data.get("on", data.get(True))
        assert "schedule" in triggers and "workflow_dispatch" in triggers

    def test_has_the_pages_permissions(self):
        permissions = self._dashboard()["permissions"]
        assert permissions["pages"] == "write"
        assert permissions["id-token"] == "write"

    def test_builds_the_dashboard_with_the_cli(self):
        text = _run_text(self._dashboard()["jobs"]["build"])
        assert "python -m app.cli dashboard" in text
        assert "--baseline-max-age" in text

    def test_uploads_both_an_artifact_and_the_pages_bundle(self):
        steps = _steps(self._dashboard()["jobs"]["build"])
        uses = [step.get("uses", "") for step in steps]
        assert "actions/upload-artifact@v4" in uses
        assert "actions/upload-pages-artifact@v3" in uses

    def test_deploy_job_needs_the_build_and_runs_deploy_pages(self):
        jobs = self._dashboard()["jobs"]
        assert jobs["deploy"]["needs"] == "build"
        uses = [step.get("uses", "") for step in _steps(jobs["deploy"])]
        assert "actions/deploy-pages@v4" in uses


class TestDigestWorkflow:
    def _digest(self) -> dict:
        return _load("digest.yml")

    def test_runs_weekly_and_on_demand(self):
        data = self._digest()
        triggers = data.get("on", data.get(True))
        assert "schedule" in triggers and "workflow_dispatch" in triggers

    def test_invokes_the_digest_cli_in_send_mode(self):
        text = _run_text(self._digest()["jobs"]["digest"])
        assert "python -m app.cli digest" in text
        assert "--send" in text

    def test_passes_the_smtp_secrets_through_env(self):
        steps = _steps(self._digest()["jobs"]["digest"])
        env = {}
        for step in steps:
            env.update(step.get("env", {}))
        assert env["CAPTURE_SMTP_HOST"] == "${{ secrets.CAPTURE_SMTP_HOST }}"
        assert env["CAPTURE_DIGEST_TO"] == "${{ secrets.CAPTURE_DIGEST_TO }}"

    def test_guards_against_missing_secrets(self):
        steps = _steps(self._digest()["jobs"]["digest"])
        guarded = [step for step in steps if "secrets.CAPTURE_SMTP_HOST" in str(step.get("if", ""))]
        assert guarded, "expected an if-guard on the SMTP secrets"


class TestDashboardPreview:
    def _dashboard(self) -> dict:
        return _load("dashboard.yml")

    def test_pull_requests_trigger_the_workflow(self):
        data = self._dashboard()
        triggers = data.get("on", data.get(True))
        assert "pull_request" in triggers

    def test_preview_job_only_runs_for_pull_requests(self):
        job = self._dashboard()["jobs"]["preview"]
        assert "pull_request" in job["if"]
        assert job["permissions"]["pull-requests"] == "write"

    def test_preview_is_named_after_the_pull_request(self):
        steps = _steps(self._dashboard()["jobs"]["preview"])
        upload = next(s for s in steps if s.get("uses") == "actions/upload-artifact@v4")
        assert "pull_request.number" in upload["with"]["name"]
        assert upload["with"]["retention-days"] == 14

    def test_preview_comments_the_artifact_link(self):
        steps = _steps(self._dashboard()["jobs"]["preview"])
        comment = next(s for s in steps if "gh pr comment" in str(s.get("run", "")))
        assert comment.get("continue-on-error") is True
        assert "steps.upload.outputs.artifact-url" in comment["env"]["ARTIFACT_URL"]
        assert "head.repo.full_name == github.repository" in comment["if"]

    def test_preview_checks_out_the_base_branch_too(self):
        steps = _steps(self._dashboard()["jobs"]["preview"])
        checkouts = [s for s in steps if s.get("uses") == "actions/checkout@v4"]
        assert len(checkouts) == 2
        paths = {s["with"]["path"] for s in checkouts}
        assert paths == {"pr", "base"}
        base = next(s for s in checkouts if s["with"]["path"] == "base")
        assert base["with"]["ref"] == "${{ github.event.pull_request.base.sha }}"

    def test_preview_compares_the_two_histories(self):
        text = _run_text(self._dashboard()["jobs"]["preview"])
        assert "--base base/dashboard_data" in text
        assert "comparison.txt" in text

    def test_preview_comment_includes_the_comparison_block(self):
        steps = _steps(self._dashboard()["jobs"]["preview"])
        comment = next(s for s in steps if "gh pr comment" in str(s.get("run", "")))
        assert "cat _site/comparison.txt" in comment["run"]
        assert "--body-file comment.md" in comment["run"]

    def test_preview_comment_is_sticky(self):
        steps = _steps(self._dashboard()["jobs"]["preview"])
        comment = next(s for s in steps if "gh pr comment" in str(s.get("run", "")))
        run = comment["run"]
        marker = "<!-- capture-bot-dashboard-preview -->"
        assert marker in run
        # It looks for its own marker and PATCHes that comment instead of posting
        # a second one on the next push.
        assert "issues/${PR_NUMBER}/comments" in run
        assert r"contains(\"${MARKER}\")" in run
        assert "-X PATCH" in run
        assert "issues/comments/${existing}" in run
        assert 'if [ -n "$existing" ]' in run

    def test_preview_marker_is_written_into_the_body(self):
        steps = _steps(self._dashboard()["jobs"]["preview"])
        comment = next(s for s in steps if "gh pr comment" in str(s.get("run", "")))
        assert 'echo "$MARKER"' in comment["run"]

    def test_preview_builds_from_the_pr_checkout(self):
        text = _run_text(self._dashboard()["jobs"]["preview"])
        assert "--dir pr/dashboard_data" in text

    def test_deploy_stays_on_the_default_branch_and_skips_pull_requests(self):
        job = self._dashboard()["jobs"]["deploy"]
        assert "pull_request" in job["if"]
        assert "default_branch" in job["if"]

    def test_build_job_skips_pull_requests(self):
        assert "pull_request" in self._dashboard()["jobs"]["build"]["if"]


class TestDigestAnnounce:
    def _announce(self) -> dict:
        return _load("digest.yml")["jobs"]["announce"]

    def test_digest_workflow_may_write_issues(self):
        assert _load("digest.yml")["permissions"]["issues"] == "write"

    def test_builds_the_digest_text_without_email(self):
        text = _run_text(self._announce())
        assert "python -m app.cli digest" in text
        assert "--send" not in text  # the issue path needs no SMTP at all

    def test_issue_number_can_come_from_input_or_secret(self):
        steps = _steps(self._announce())
        post = next(s for s in steps if "gh api" in str(s.get("run", "")))
        assert "github.event.inputs.issue_number" in post["env"]["ISSUE"]
        assert "secrets.DIGEST_ISSUE_NUMBER" in post["env"]["ISSUE"]

    def test_comment_is_sticky_and_fork_safe(self):
        steps = _steps(self._announce())
        post = next(s for s in steps if "gh api" in str(s.get("run", "")))
        run = post["run"]
        assert "<!-- capture-bot-weekly-digest -->" in run
        assert "issues/comments/${existing}" in run
        assert "-X PATCH" in run
        assert "-X POST" in run
        assert 'if [ -n "$existing" ]' in run
        assert post.get("continue-on-error") is True

    def test_missing_configuration_is_explained_not_fatal(self):
        steps = _steps(self._announce())
        notice = next(s for s in steps if "::notice::" in str(s.get("run", "")))
        assert "DIGEST_ISSUE_NUMBER" in notice["run"]
        assert "== ''" in notice["if"]


class TestPerProfileDigestWorkflow:
    def _profiles_job(self) -> dict:
        return _load("digest.yml")["jobs"]["announce-profiles"]

    def test_only_runs_when_a_profiles_folder_is_given(self):
        job = self._profiles_job()
        assert "profiles_base" in job["if"]
        assert "!= ''" in job["if"]

    def test_it_builds_the_plan_with_the_cli(self):
        text = _run_text(self._profiles_job())
        assert "digest --dir" in text and "--profiles" in text
        assert "--profiles-base" in text and "--json" in text
        assert "plan.json" in text

    def test_it_posts_through_the_tool_with_the_run_url(self):
        steps = _steps(self._profiles_job())
        post = next(s for s in steps if "post_digest.py plan.json" in str(s.get("run", "")))
        assert "--run-url" in post["run"]
        assert post["env"]["GITHUB_REPOSITORY"] == "${{ github.repository }}"
        assert post.get("continue-on-error") is True

    def test_a_dry_run_step_keeps_the_log_readable(self):
        steps = _steps(self._profiles_job())
        assert any("--dry-run" in str(s.get("run", "")) for s in steps)

    def test_dispatch_accepts_the_new_input(self):
        data = _load("digest.yml")
        inputs = data.get("on", data.get(True))["workflow_dispatch"]["inputs"]
        assert "profiles_base" in inputs and "issue_number" in inputs


DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


class TestDeploySamples:
    """The systemd samples must keep matching the CLI they call."""

    def test_the_unit_runs_the_metrics_command(self):
        text = (DEPLOY / "capture-bot-metrics.service").read_text(encoding="utf-8")
        assert "python -m app.cli metrics" in text
        assert "--dir " in text and "--out " in text and "--write-if-changed" in text
        assert "Type=oneshot" in text

    def test_the_timer_refreshes_every_minute(self):
        text = (DEPLOY / "capture-bot-metrics.timer").read_text(encoding="utf-8")
        assert "OnUnitActiveSec=1min" in text
        assert "Unit=capture-bot-metrics.service" in text
        assert "WantedBy=timers.target" in text

    def test_the_readme_explains_the_textfile_collector(self):
        text = (DEPLOY / "README.md").read_text(encoding="utf-8")
        assert "node_exporter" in text or "node exporter" in text
        assert "systemctl enable --now capture-bot-metrics.timer" in text
        assert "capture_bot_last_success_timestamp_seconds" in text

    def test_the_flags_in_the_sample_really_exist(self):
        from app import cli

        args = cli.build_metrics_parser().parse_args(
            ["--dir", "shots", "--stale-after", "30", "--out", "m.prom", "--write-if-changed"]
        )
        assert args.out == "m.prom"
        assert args.write_if_changed is True
        assert args.stale_after == 30


class TestMonitoringBundle:
    """The deploy/ bundle must keep matching the metrics the code emits."""

    @staticmethod
    def _exposition() -> str:
        from app.core import status

        payload = status.status_payload(
            ".",
            "",
            30,
            storage={
                "total": 4096,
                "history": 2048,
                "index": 1024,
                "caps": {"screenshots_mb": 500, "history_mb": 50},
                "site_caps": {"news.example.com": 200.0},
                "days_to_cap": 3.5,
            },
        )
        return status.metrics_text(payload, version="test")

    @staticmethod
    def _metrics_in(text: str) -> set[str]:
        import re

        return set(re.findall(r"\bcapture_bot_[a-z0-9_]+", text))

    def test_the_rules_file_is_valid_yaml(self):
        data = yaml.safe_load((DEPLOY / "rules.yml").read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert data["groups"][0]["name"] == "fullpage-capture-bot"

    def test_every_metric_in_the_rules_really_exists(self):
        rules = (DEPLOY / "rules.yml").read_text(encoding="utf-8")
        known = self._metrics_in(self._exposition())
        assert known, "the exposition should contain gauges"
        unknown = sorted(self._metrics_in(rules) - known)
        assert unknown == [], f"rules.yml alerts on metrics that are never exported: {unknown}"

    def test_the_rules_cover_stale_forecast_and_queue(self):
        rules = (DEPLOY / "rules.yml").read_text(encoding="utf-8")
        assert 'capture_bot_health_state{state="stale"}' in rules
        assert "capture_bot_days_to_cap" in rules
        assert "capture_bot_pending_alerts" in rules
        assert "capture_bot_last_success_timestamp_seconds" in rules
        for alert in ("CaptureBotStale", "CaptureBotCapInSight", "CaptureBotQueueGrowing"):
            assert f"alert: {alert}" in rules

    def test_the_grafana_dashboard_is_importable_json(self):
        import json

        data = json.loads((DEPLOY / "capture-bot.grafana.json").read_text(encoding="utf-8"))
        assert data["panels"], "a dashboard with no panels is not a dashboard"
        assert data["__inputs"][0]["name"] == "DS_PROMETHEUS"
        assert data["uid"] == "fullpage-capture-bot"

    def test_the_grafana_panels_only_use_exported_metrics(self):
        import json

        data = json.loads((DEPLOY / "capture-bot.grafana.json").read_text(encoding="utf-8"))
        expressions = [
            str(target.get("expr", ""))
            for panel in data["panels"]
            for target in panel.get("targets", [])
        ]
        assert expressions
        known = self._metrics_in(self._exposition())
        unknown = sorted(set().union(*[self._metrics_in(expr) for expr in expressions]) - known)
        assert unknown == [], f"the dashboard queries metrics that do not exist: {unknown}"

    def test_the_healthcheck_unit_runs_the_check(self):
        text = (DEPLOY / "capture-bot-healthcheck.service").read_text(encoding="utf-8")
        assert "python -m app.cli metrics" in text
        assert "--check" in text and "--max-age" in text and "--caps" in text
        assert "Type=oneshot" in text
        assert "OnFailure=capture-bot-failed@%n.service" in text

    def test_the_healthcheck_timer_is_scheduled_and_points_at_the_unit(self):
        text = (DEPLOY / "capture-bot-healthcheck.timer").read_text(encoding="utf-8")
        assert "Unit=capture-bot-healthcheck.service" in text
        assert "OnUnitActiveSec=15min" in text
        assert "WantedBy=timers.target" in text

    def test_the_onfailure_unit_the_healthcheck_names_is_shipped(self):
        text = (DEPLOY / "capture-bot-healthcheck.service").read_text(encoding="utf-8")
        target = next(
            line.split("=", 1)[1] for line in text.splitlines() if line.startswith("OnFailure=")
        )
        assert target == "capture-bot-failed@%n.service"
        assert (DEPLOY / "capture-bot-failed@.service").is_file()

    def test_the_metrics_timer_knows_the_caps_so_the_forecast_is_real(self):
        text = (DEPLOY / "capture-bot-metrics.service").read_text(encoding="utf-8")
        assert "--caps " in text

    def test_the_readme_explains_the_bundle(self):
        text = (DEPLOY / "README.md").read_text(encoding="utf-8")
        for needle in ("rules.yml", "capture-bot.grafana.json", "--check", "OnFailure="):
            assert needle in text
