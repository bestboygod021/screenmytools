"""Unit tests for the durable pending-URL queue."""

from __future__ import annotations

from app.core import queue_store


class TestQueueStore:
    def test_save_and_load_round_trip(self, tmp_path):
        queue_store.save_queue(tmp_path, ["https://a.com", "https://b.com"])
        assert queue_store.load_queue(tmp_path) == ["https://a.com", "https://b.com"]

    def test_load_missing_returns_empty(self, tmp_path):
        assert queue_store.load_queue(tmp_path / "nope") == []

    def test_load_corrupt_returns_empty(self, tmp_path):
        queue_store.save_queue(tmp_path, ["https://a.com"])
        (tmp_path / "queue.json").write_text("{broken", encoding="utf-8")
        assert queue_store.load_queue(tmp_path) == []

    def test_clear(self, tmp_path):
        queue_store.save_queue(tmp_path, ["https://a.com"])
        assert queue_store.clear_queue(tmp_path) is True
        assert queue_store.clear_queue(tmp_path) is False
        assert queue_store.load_queue(tmp_path) == []

    def test_non_string_entries_are_dropped(self, tmp_path):
        (tmp_path / "queue.json").write_text(
            '{"urls": ["https://a.com", 5, null]}', encoding="utf-8"
        )
        assert queue_store.load_queue(tmp_path) == ["https://a.com"]
