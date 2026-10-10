"""Unit tests for the daily-scheduling math."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.core import schedule


class TestParseHhmm:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("09:00", (9, 0)),
            ("23:59", (23, 59)),
            ("0:05", (0, 5)),
        ],
    )
    def test_valid(self, text, expected):
        assert schedule.parse_hhmm(text) == expected

    @pytest.mark.parametrize("text", ["", "9", "25:00", "10:60", "ab:cd", "10:00:00", None])
    def test_invalid(self, text):
        assert schedule.parse_hhmm(text) is None


class TestSecondsUntilDaily:
    def test_later_today(self):
        now = datetime(2026, 10, 7, 8, 0, 0)
        assert schedule.seconds_until_daily("09:00", now) == 3600

    def test_rolls_to_tomorrow(self):
        now = datetime(2026, 10, 7, 10, 0, 0)
        # 23h until 09:00 tomorrow
        assert schedule.seconds_until_daily("09:00", now) == 23 * 3600

    def test_same_time_rolls_a_day(self):
        now = datetime(2026, 10, 7, 9, 0, 0)
        assert schedule.seconds_until_daily("09:00", now) == 24 * 3600

    def test_invalid_falls_back_to_0900(self):
        now = datetime(2026, 10, 7, 8, 0, 0)
        assert schedule.seconds_until_daily("bogus", now) == 3600


class TestLabel:
    def test_today_vs_tomorrow(self):
        now = datetime(2026, 10, 7, 8, 0, 0)
        assert "today" in schedule.format_daily_label("09:00", now)
        now = datetime(2026, 10, 7, 10, 0, 0)
        assert "tomorrow" in schedule.format_daily_label("09:00", now)
