"""Tests for proxy launch and storage_state session injection."""

from __future__ import annotations

import pytest

from app.core.settings import CaptureSettings
from tests.fakes import PageScript
from tests.test_engine import run_engine


class TestProxy:
    def test_proxy_passed_to_launch(self, tmp_path):
        settings = CaptureSettings(output_dir=str(tmp_path), proxy_server="http://proxy:8080")
        _, _, calls = run_engine(settings, ["https://example.com"], PageScript())
        assert calls.launched[0]["proxy"] == {"server": "http://proxy:8080"}

    def test_no_proxy_by_default(self, tmp_path):
        settings = CaptureSettings(output_dir=str(tmp_path))
        _, _, calls = run_engine(settings, ["https://example.com"], PageScript())
        assert calls.launched[0]["proxy"] is None

    def test_proxy_needs_scheme(self):
        settings = CaptureSettings(output_dir="/tmp/x", proxy_server="proxy:8080")
        with pytest.raises(Exception):
            settings.validate()

    def test_proxy_with_scheme_ok(self):
        CaptureSettings(output_dir="/tmp/x", proxy_server="http://proxy:8080").validate()


class TestStorageState:
    def test_existing_state_is_injected(self, tmp_path):
        state = tmp_path / "state.json"
        state.write_text('{"cookies": []}', encoding="utf-8")
        settings = CaptureSettings(output_dir=str(tmp_path), storage_state_path=str(state))
        _, _, calls = run_engine(settings, ["https://example.com"], PageScript())
        assert calls.contexts[0].get("storage_state") == str(state)

    def test_missing_state_is_ignored(self, tmp_path):
        settings = CaptureSettings(
            output_dir=str(tmp_path), storage_state_path=str(tmp_path / "nope.json")
        )
        _, _, calls = run_engine(settings, ["https://example.com"], PageScript())
        assert "storage_state" not in calls.contexts[0]

    def test_no_state_by_default(self, tmp_path):
        settings = CaptureSettings(output_dir=str(tmp_path))
        _, _, calls = run_engine(settings, ["https://example.com"], PageScript())
        assert "storage_state" not in calls.contexts[0]
