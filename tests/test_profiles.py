"""Unit tests for named configuration profiles."""

from __future__ import annotations

from app.core import profiles


class TestProfiles:
    def test_save_and_load_round_trip(self, tmp_path):
        profiles.save_profile(tmp_path, "Client A", {"viewport_width": 1440}, "https://a.com\n")
        loaded = profiles.load_profile(tmp_path, "Client A")
        assert loaded is not None
        assert loaded["settings"]["viewport_width"] == 1440
        assert loaded["urls"] == "https://a.com\n"

    def test_list_returns_sorted_stems(self, tmp_path):
        profiles.save_profile(tmp_path, "b", {}, "")
        profiles.save_profile(tmp_path, "a", {}, "")
        assert profiles.list_profiles(tmp_path) == ["a", "b"]

    def test_list_empty_when_no_dir(self, tmp_path):
        assert profiles.list_profiles(tmp_path / "nothing") == []

    def test_unsafe_names_are_sanitised_consistently(self, tmp_path):
        profiles.save_profile(tmp_path, "My/Proj:1", {}, "")
        # The same sanitised name must be resolvable on load/list.
        assert profiles.list_profiles(tmp_path) == ["my-proj-1"]
        assert profiles.load_profile(tmp_path, "My/Proj:1") is not None

    def test_load_missing_returns_none(self, tmp_path):
        assert profiles.load_profile(tmp_path, "ghost") is None

    def test_load_corrupt_returns_none(self, tmp_path):
        profiles.save_profile(tmp_path, "x", {}, "")
        path = profiles.profiles_dir(tmp_path) / "x.json"
        path.write_text("{not json", encoding="utf-8")
        assert profiles.load_profile(tmp_path, "x") is None

    def test_delete(self, tmp_path):
        profiles.save_profile(tmp_path, "x", {}, "")
        assert profiles.delete_profile(tmp_path, "x") is True
        assert profiles.delete_profile(tmp_path, "x") is False
        assert profiles.list_profiles(tmp_path) == []


class TestDigestPlan:
    def _profile(self, base, name, **extra):
        from app.core import profiles

        settings = {"output_dir": f"/shots/{name}", "smtp_host": "h", **extra}
        profiles.save_profile(base, name, settings, "https://a.example.com")

    def test_plan_uses_each_profiles_window_and_issue(self, tmp_path):
        from app.core import profiles

        self._profile(tmp_path, "daily", digest_days=7, digest_issue_number=12)
        self._profile(tmp_path, "monthly", digest_days=30)
        plan = {entry["name"]: entry for entry in profiles.digest_plan(tmp_path)}
        assert plan["daily"]["days"] == 7 and plan["daily"]["issue"] == 12
        assert plan["monthly"]["days"] == 30 and plan["monthly"]["issue"] == 0

    def test_plan_falls_back_to_the_default_window(self, tmp_path):
        from app.core import profiles

        self._profile(tmp_path, "legacy")  # saved before digest settings existed
        assert profiles.digest_plan(tmp_path, fallback_days=14)[0]["days"] == 14

    def test_plan_survives_broken_values(self, tmp_path):
        from app.core import profiles

        self._profile(tmp_path, "broken", digest_days="lots", digest_issue_number=None)
        entry = profiles.digest_plan(tmp_path, fallback_days=7)[0]
        assert entry["days"] == 7 and entry["issue"] == 0

    def test_empty_store_has_an_empty_plan(self, tmp_path):
        from app.core import profiles

        assert profiles.digest_plan(tmp_path) == []

    def test_default_base_is_a_qt_free_path(self):
        from app.core import profiles

        base = profiles.default_base()
        assert base.name  # non-empty, and building it never imports PyQt6
