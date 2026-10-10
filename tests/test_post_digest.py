"""Tests for the per-profile issue commenter (tools/post_digest.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import post_digest  # noqa: E402

from app.core import digest  # noqa: E402


def _plan(*profiles) -> dict:
    return {
        "profiles": [
            {
                "profile": name,
                "issue": issue,
                "days": 7,
                "captures": 3,
                "changed": 1,
                "failed": 0,
                "sites": 2,
                "top_sites": [["https://a.example.com", 1]],
                "top_drift": [],
                "stale_baselines": [],
            }
            for name, issue in profiles
        ]
    }


class TestCommentBody:
    def test_marker_is_per_profile(self):
        assert "daily" in digest.comment_marker("daily")
        assert digest.comment_marker("daily") != digest.comment_marker("monthly")

    def test_marker_survives_odd_names(self):
        assert "--" in digest.comment_marker("client a/b")  # slugged, still a comment

    def test_body_contains_marker_title_and_text(self):
        body = digest.comment_body("daily", "Captures recorded : 3", "https://run")
        assert body.startswith(digest.comment_marker("daily"))
        assert "Weekly digest - daily" in body
        assert "Captures recorded : 3" in body
        assert "https://run" in body

    def test_body_without_a_run_url(self):
        body = digest.comment_body("daily", "x")
        assert "this run" not in body


class TestPlanEntries:
    def test_only_profiles_with_an_issue_are_posted(self):
        entries = post_digest.plan_entries(_plan(("daily", 5), ("quiet", 0)))
        assert [entry["profile"] for entry in entries] == ["daily"]

    def test_comment_for_returns_issue_and_body(self):
        issue, body = post_digest.comment_for(_plan(("daily", 5))["profiles"][0], "https://run")
        assert issue == 5
        assert "Captures recorded : 3" in body and digest.comment_marker("daily") in body


class _FakeGh:
    """Records gh calls and pretends one comment already exists."""

    def __init__(self, existing: dict[int, list[dict]] | None = None) -> None:
        self.calls: list[tuple[list[str], dict | None]] = []
        self.existing = existing or {}

    def __call__(self, args: list[str], payload: dict | None = None) -> str:
        self.calls.append((list(args), payload))
        # The listing call looks like ["repos/o/r/issues/5/comments", "--paginate"].
        if "/issues/" in args[0] and args[0].endswith("/comments") and "-X" not in args:
            issue = int(args[0].split("/issues/")[1].split("/")[0])
            return json.dumps(self.existing.get(issue, []))
        return ""


class TestPostOrUpdate:
    def test_creates_a_comment_when_none_exists(self, monkeypatch):
        fake = _FakeGh()
        monkeypatch.setattr(post_digest, "_gh", fake)
        action = post_digest.post_or_update("me/repo", 5, "<!-- marker -->", "body")
        assert action == "posted"
        assert any("-X" in args and "POST" in args for args, _payload in fake.calls)

    def test_updates_the_comment_carrying_the_marker(self, monkeypatch):
        fake = _FakeGh({5: [{"id": 99, "body": "old <!-- marker --> text"}]})
        monkeypatch.setattr(post_digest, "_gh", fake)
        action = post_digest.post_or_update("me/repo", 5, "<!-- marker -->", "new")
        assert action == "updated"
        assert any("PATCH" in args for args, _payload in fake.calls)
        assert not any("POST" in args for args, _payload in fake.calls)

    def test_ignores_other_comments(self, monkeypatch):
        fake = _FakeGh({5: [{"id": 1, "body": "unrelated"}]})
        monkeypatch.setattr(post_digest, "_gh", fake)
        assert post_digest.post_or_update("me/repo", 5, "<!-- marker -->", "new") == "posted"


class TestMain:
    def _plan_file(self, tmp_path, *profiles) -> Path:
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(_plan(*profiles)), encoding="utf-8")
        return path

    def test_dry_run_prints_instead_of_posting(self, tmp_path, capsys, monkeypatch):
        def explode(*_args, **_kwargs):  # pragma: no cover - must not be called
            raise AssertionError("dry run must not talk to GitHub")

        monkeypatch.setattr(post_digest, "_gh", explode)
        code = post_digest.main([str(self._plan_file(tmp_path, ("daily", 5))), "--dry-run"])
        out = capsys.readouterr().out
        assert code == 0
        assert "would post to issue #5" in out and "Captures recorded : 3" in out

    def test_nothing_to_do_is_a_success(self, tmp_path, capsys):
        code = post_digest.main([str(self._plan_file(tmp_path, ("quiet", 0)))])
        assert code == 0
        assert "nothing to post" in capsys.readouterr().out

    def test_posting_reports_the_action_per_profile(self, tmp_path, capsys, monkeypatch):
        fake = _FakeGh()
        monkeypatch.setattr(post_digest, "_gh", fake)
        monkeypatch.setattr(post_digest.shutil, "which", lambda _name: "/usr/bin/gh")
        code = post_digest.main(
            [
                str(self._plan_file(tmp_path, ("daily", 5), ("monthly", 7))),
                "--repo",
                "me/repo",
            ]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "issue #5: posted" in out and "issue #7: posted" in out

    def test_a_failing_post_reports_which_profile(self, tmp_path, capsys, monkeypatch):
        def boom(args, payload=None):
            if "PATCH" in args or "POST" in args:
                raise RuntimeError("gh exploded")
            return json.dumps([])

        monkeypatch.setattr(post_digest, "_gh", boom)
        monkeypatch.setattr(post_digest.shutil, "which", lambda _name: "/usr/bin/gh")
        code = post_digest.main([str(self._plan_file(tmp_path, ("daily", 5))), "--repo", "me/repo"])
        assert code == 1
        assert "issue #5: gh exploded" in capsys.readouterr().err

    def test_missing_repo_is_a_clean_error(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(post_digest.shutil, "which", lambda _name: "/usr/bin/gh")
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        code = post_digest.main([str(self._plan_file(tmp_path, ("daily", 5)))])
        assert code == 2
        assert "GITHUB_REPOSITORY" in capsys.readouterr().err

    def test_missing_gh_is_a_clean_error(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(post_digest.shutil, "which", lambda _name: None)
        code = post_digest.main([str(self._plan_file(tmp_path, ("daily", 5))), "--repo", "me/repo"])
        assert code == 2
        assert "gh" in capsys.readouterr().err
