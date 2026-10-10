"""Unit tests for the cron parser/scheduler."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.core import cron


class TestParse:
    def test_star_expands_full_range(self):
        minutes, *_ = cron.parse("* * * * *")
        assert minutes == set(range(0, 60))

    def test_step(self):
        minutes, *_ = cron.parse("*/15 * * * *")
        assert minutes == {0, 15, 30, 45}

    def test_range_and_list(self):
        assert cron._parse_field("1-3", 0, 59) == {1, 2, 3}
        assert cron._parse_field("1,5", 0, 59) == {1, 5}
        assert cron._parse_field("1-5/2", 0, 59) == {1, 3, 5}

    def test_seven_is_sunday(self):
        *_, dows = cron.parse("0 0 * * 7")
        assert dows == {0}

    def test_bad_field_count(self):
        with pytest.raises(cron.CronError):
            cron.parse("* * *")

    def test_out_of_range(self):
        with pytest.raises(cron.CronError):
            cron.parse("99 * * * *")


class TestMatches:
    def test_specific_time(self):
        assert cron.matches("30 9 * * *", datetime(2026, 10, 8, 9, 30)) is True
        assert cron.matches("30 9 * * *", datetime(2026, 10, 8, 9, 31)) is False

    def test_weekday(self):
        # 2026-10-08 is a Thursday -> cron dow 4
        assert cron.matches("0 0 * * 4", datetime(2026, 10, 8, 0, 0)) is True
        assert cron.matches("0 0 * * 0", datetime(2026, 10, 8, 0, 0)) is False


class TestNextRun:
    def test_next_daily_noon(self):
        start = datetime(2026, 10, 8, 10, 0)
        assert cron.next_run("0 12 * * *", start) == datetime(2026, 10, 8, 12, 0)

    def test_rolls_to_tomorrow(self):
        start = datetime(2026, 10, 8, 13, 0)
        assert cron.next_run("0 12 * * *", start) == datetime(2026, 10, 9, 12, 0)

    def test_seconds_until_positive(self):
        start = datetime(2026, 10, 8, 11, 0)
        assert cron.seconds_until("0 12 * * *", start) == 3600

    def test_invalid_raises(self):
        with pytest.raises(cron.CronError):
            cron.next_run("bad", datetime(2026, 1, 1))
