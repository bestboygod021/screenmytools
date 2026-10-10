"""Unit tests for the auto-update helpers (HTTP layer mocked)."""

from __future__ import annotations

import pytest

from app import updater


class TestParseVersion:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("1.0.0", (1, 0, 0)),
            ("v1.2.3", (1, 2, 3)),
            ("1.2", (1, 2, 0)),
            ("2", (2, 0, 0)),
            ("release-1.9.4", (1, 9, 4)),
            ("", (0, 0, 0)),
            ("garbage", (0, 0, 0)),
        ],
    )
    def test_parses(self, text, expected):
        assert updater.parse_version(text) == expected


class TestIsNewer:
    def test_newer(self):
        assert updater.is_newer("v1.1.0", "1.0.0") is True
        assert updater.is_newer("2.0.0", "1.9.9") is True

    def test_same_or_older(self):
        assert updater.is_newer("1.0.0", "1.0.0") is False
        assert updater.is_newer("0.9.0", "1.0.0") is False

    def test_patch_bump(self):
        assert updater.is_newer("1.0.1", "1.0.0") is True


class TestLatestRelease:
    def test_returns_tag(self, monkeypatch):
        monkeypatch.setattr(updater, "_get_json", lambda url, timeout=10.0: {"tag_name": "v9.9.9"})
        assert updater.latest_release_tag() == "v9.9.9"

    def test_offline_returns_none(self, monkeypatch):
        def boom(url, timeout=10.0):
            raise OSError("no network")

        monkeypatch.setattr(updater, "_get_json", boom)
        assert updater.latest_release_tag() is None

    def test_missing_tag_returns_none(self, monkeypatch):
        monkeypatch.setattr(updater, "_get_json", lambda url, timeout=10.0: {})
        assert updater.latest_release_tag() is None


class TestCheckForUpdates:
    def test_update_available(self, monkeypatch):
        monkeypatch.setattr(updater, "_get_json", lambda url, timeout=10.0: {"tag_name": "v2.0.0"})
        result = updater.check_for_updates("1.0.0")
        assert result["available"] is True
        assert result["latest"] == "v2.0.0"
        assert "releases" in result["url"]

    def test_up_to_date(self, monkeypatch):
        monkeypatch.setattr(updater, "_get_json", lambda url, timeout=10.0: {"tag_name": "v1.0.0"})
        result = updater.check_for_updates("1.0.0")
        assert result["available"] is False

    def test_offline_reports_unavailable(self, monkeypatch):
        def boom(url, timeout=10.0):
            raise OSError("offline")

        monkeypatch.setattr(updater, "_get_json", boom)
        result = updater.check_for_updates("1.0.0")
        assert result["available"] is False
        assert result["latest"] is None
