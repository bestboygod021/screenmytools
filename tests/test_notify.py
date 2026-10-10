"""Unit tests for the desktop-notification dispatcher."""

from __future__ import annotations

from app.core import notify


class TestNotify:
    def test_first_backend_wins(self, monkeypatch):
        monkeypatch.setattr(notify, "_plyer", lambda t, m: True)
        assert notify.send_notification("t", "m") is True

    def test_falls_through_to_next_backend(self, monkeypatch):
        monkeypatch.setattr(notify, "_plyer", lambda t, m: False)
        monkeypatch.setattr(notify, "_notify_send", lambda t, m: True)
        assert notify.send_notification("t", "m") is True

    def test_all_backends_fail(self, monkeypatch):
        monkeypatch.setattr(notify, "_plyer", lambda t, m: False)
        monkeypatch.setattr(notify, "_notify_send", lambda t, m: False)
        monkeypatch.setattr(notify, "_osascript", lambda t, m: False)
        assert notify.send_notification("t", "m") is False

    def test_backend_exception_is_swallowed(self, monkeypatch):
        def boom(t, m):
            raise RuntimeError("x")

        monkeypatch.setattr(notify, "_plyer", boom)
        monkeypatch.setattr(notify, "_notify_send", lambda t, m: True)
        assert notify.send_notification("t", "m") is True

    def test_notify_send_absent_returns_false(self, monkeypatch):
        monkeypatch.setattr(notify.shutil, "which", lambda name: None)
        assert notify._notify_send("t", "m") is False

    def test_osascript_absent_returns_false(self, monkeypatch):
        monkeypatch.setattr(notify.shutil, "which", lambda name: None)
        assert notify._osascript("t", "m") is False
