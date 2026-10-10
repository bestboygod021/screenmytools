"""Tests for the alert quiet-hours helper (app.core.quiet)."""

from __future__ import annotations

import json
from datetime import datetime

from app.core import quiet


class TestParseWindow:
    def test_hours_and_minutes(self):
        start, end = quiet.parse_window("22:00-07:00")
        assert (start.hour, start.minute, end.hour, end.minute) == (22, 0, 7, 0)

    def test_bare_hours_mean_on_the_hour(self):
        assert quiet.parse_window("22-7") == quiet.parse_window("22:00-07:00")

    def test_spaces_are_tolerated(self):
        assert quiet.parse_window("  22:30 - 06:15 ") == quiet.parse_window("22:30-06:15")

    def test_garbage_is_rejected(self):
        for text in ("", "22:00", "night", "22:00-", "-07:00", "25:00-07:00", "22:99-07:00"):
            assert quiet.parse_window(text) is None, text


class TestInWindow:
    def test_overnight_window_wraps_past_midnight(self):
        for hour, expected in (
            (23, True),
            (0, True),
            (3, True),
            (6, True),
            (7, False),
            (12, False),
        ):
            assert quiet.in_window(datetime(2026, 5, 4, hour, 30), "22:00-07:00") is expected

    def test_daytime_window(self):
        assert quiet.in_window(datetime(2026, 5, 4, 9, 0), "09:00-17:00")
        assert not quiet.in_window(datetime(2026, 5, 4, 8, 59), "09:00-17:00")
        assert not quiet.in_window(datetime(2026, 5, 4, 17, 0), "09:00-17:00")

    def test_empty_or_broken_window_never_holds_alerts(self):
        night = datetime(2026, 5, 4, 3, 0)
        assert quiet.in_window(night, "22:00-07:00")  # a real window does hold it
        assert not quiet.in_window(night, "")
        assert not quiet.in_window(night, "nonsense")

    def test_zero_length_window_is_not_quiet_all_day(self):
        assert not quiet.in_window(datetime(2026, 5, 4, 22, 0), "22:00-22:00")


class TestWindowEnd:
    def test_before_midnight_ends_tomorrow(self):
        end = quiet.window_end(datetime(2026, 5, 4, 23, 30), "22:00-07:00")
        assert end == datetime(2026, 5, 5, 7, 0)

    def test_after_midnight_ends_today(self):
        assert quiet.window_end(datetime(2026, 5, 5, 2, 0), "22:00-07:00") == datetime(
            2026, 5, 5, 7, 0
        )

    def test_outside_the_window_there_is_no_end(self):
        assert quiet.window_end(datetime(2026, 5, 5, 12, 0), "22:00-07:00") is None


class TestQueue:
    def test_queue_lives_beside_the_history(self, tmp_path):
        assert quiet.queue_path(tmp_path) == tmp_path / "pending-alerts.json"

    def test_missing_file_means_nothing_waiting(self, tmp_path):
        assert quiet.load(quiet.queue_path(tmp_path)) == []

    def test_a_corrupt_file_is_ignored(self, tmp_path):
        path = quiet.queue_path(tmp_path)
        path.write_text("{not json", encoding="utf-8")
        assert quiet.load(path) == []

    def test_a_json_object_is_ignored(self, tmp_path):
        path = quiet.queue_path(tmp_path)
        path.write_text(json.dumps({"url": "https://a.com"}), encoding="utf-8")
        assert quiet.load(path) == []

    def test_append_merges_and_counts(self, tmp_path):
        path = quiet.queue_path(tmp_path)
        assert quiet.append(path, [{"url": "https://a.com", "diff": 0.1}]) == 1
        assert quiet.append(path, [{"url": "https://a.com", "diff": 0.2}, {"url": "b"}]) == 2
        queued = json.loads(path.read_text(encoding="utf-8"))
        assert [item["url"] for item in queued] == ["https://a.com", "b"]
        assert queued[0]["diff"] == 0.2  # the newest capture of the site wins

    def test_take_drains_and_deletes(self, tmp_path):
        path = quiet.queue_path(tmp_path)
        quiet.append(path, [{"url": "https://a.com"}])
        assert [item["url"] for item in quiet.take(path)] == ["https://a.com"]
        assert quiet.load(path) == []
        assert not path.exists()

    def test_merge_keeps_first_seen_order_and_newest_item(self):
        merged = quiet.merge([{"url": "b", "n": 1}, {"url": "a", "n": 2}, {"url": "b", "n": 3}])
        assert [item["url"] for item in merged] == ["b", "a"]
        assert merged[0]["n"] == 3


class TestQueueStamps:
    def test_items_are_stamped_when_queued(self, tmp_path):
        path = quiet.queue_path(tmp_path)
        quiet.append(path, [{"url": "https://a.com"}])
        queued = quiet.load(path)
        assert queued[0]["queued_at"]  # ISO timestamp of when it started waiting

    def test_an_existing_stamp_is_kept(self, tmp_path):
        path = quiet.queue_path(tmp_path)
        quiet.append(path, [{"url": "https://a.com", "queued_at": "2026-01-01T00:00:00"}])
        assert quiet.load(path)[0]["queued_at"] == "2026-01-01T00:00:00"


class TestMultiWindowSetting:
    """One setting can hold several windows, and a window may span named days."""

    def test_several_windows_are_parsed_in_order(self):
        windows = quiet.parse_windows("22:00-07:00, 09:00-10:30")
        assert [(w.start.strftime("%H:%M"), w.end.strftime("%H:%M")) for w in windows] == [
            ("22:00", "07:00"),
            ("09:00", "10:30"),
        ]

    def test_a_day_span_keeps_the_weekend_quiet(self):
        text = "fri18:00-mon09:00"
        assert quiet.in_window(datetime(2026, 5, 8, 18, 0), text)  # Friday evening
        assert quiet.in_window(datetime(2026, 5, 9, 12, 0), text)  # Saturday noon
        assert quiet.in_window(datetime(2026, 5, 10, 3, 0), text)  # small hours of Sunday
        assert quiet.in_window(datetime(2026, 5, 11, 8, 59), text)  # Monday morning
        assert not quiet.in_window(datetime(2026, 5, 11, 9, 0), text)  # Monday at 09:00
        assert not quiet.in_window(datetime(2026, 5, 7, 12, 0), text)  # Thursday

    def test_the_day_names_ignore_case(self):
        assert quiet.parse_span("FRI18:00-MON09:00") == quiet.parse_span("fri18:00-mon09:00")

    def test_a_day_name_on_one_side_only_is_rejected(self):
        assert quiet.parse_span("fri18:00-07:00") is None
        assert quiet.parse_span("18:00-mon09:00") is None

    def test_an_unknown_day_name_is_rejected(self):
        assert quiet.parse_span("xyz18:00-mon09:00") is None

    def test_a_single_day_span_wraps_over_midnight(self):
        text = "fri22:00-fri07:00"
        assert quiet.in_window(datetime(2026, 5, 8, 23, 30), text)
        assert not quiet.in_window(datetime(2026, 5, 8, 12, 0), text)

    def test_a_single_day_span_inside_one_day(self):
        text = "mon09:00-mon17:00"
        assert quiet.in_window(datetime(2026, 5, 4, 12, 0), text)
        assert not quiet.in_window(datetime(2026, 5, 4, 18, 0), text)

    def test_the_windows_combine(self):
        text = "22:00-07:00, fri18:00-mon09:00"
        assert quiet.in_window(datetime(2026, 5, 12, 23, 0), text)  # Tuesday night
        assert not quiet.in_window(datetime(2026, 5, 12, 12, 0), text)  # Tuesday noon

    def test_a_broken_item_does_not_kill_the_good_ones(self):
        text = "garbage, 22:00-07:00"
        assert quiet.in_window(datetime(2026, 5, 4, 23, 0), text)
        assert quiet.invalid_windows(text) == ["garbage"]

    def test_valid_items_are_not_reported_as_broken(self):
        assert quiet.invalid_windows("22:00-07:00; fri18:00-mon09:00") == []
        assert quiet.invalid_windows("") == []

    def test_window_end_of_a_day_span(self):
        end = quiet.window_end(datetime(2026, 5, 9, 12, 0), "fri18:00-mon09:00")
        assert end == datetime(2026, 5, 11, 9, 0)

    def test_window_end_takes_the_earliest_end(self):
        end = quiet.window_end(datetime(2026, 5, 9, 12, 0), "fri18:00-mon09:00, 11:00-13:00")
        assert end == datetime(2026, 5, 9, 13, 0)

    def test_window_end_outside_every_window_is_none(self):
        assert quiet.window_end(datetime(2026, 5, 7, 12, 0), "fri18:00-mon09:00") is None

    def test_the_label_reads_back(self):
        assert quiet.parse_windows("fri18:00-mon09:00")[0].label() == "fri 18:00 - mon 09:00"
        assert quiet.parse_windows("22:00-07:00")[0].label() == "22:00 - 07:00"


class TestUrlQuietRules:
    """``alert_quiet_urls``: one host can sleep while another keeps paging."""

    def test_a_rule_becomes_a_fragment_and_its_windows(self):
        rules = quiet.compile_rules("staging.example.com=22:00-07:00; news.example.com=")
        assert [fragment for fragment, _windows in rules] == [
            "staging.example.com",
            "news.example.com",
        ]
        assert rules[1][1] == ()  # an empty window list means "never quiet"

    def test_the_newsletter_pages_on_the_weekend_while_staging_sleeps(self):
        rules = quiet.compile_rules("staging.example.com=fri18:00-mon09:00; news.example.com=")
        saturday = datetime(2026, 5, 9, 12, 0)
        assert quiet.silent_now(saturday, "https://staging.example.com/x", "", rules) is True
        assert quiet.silent_now(saturday, "https://news.example.com/y", "", rules) is False

    def test_a_url_without_a_rule_follows_the_global_window(self):
        rules = quiet.compile_rules("staging.example.com=22:00-07:00")
        saturday = datetime(2026, 5, 9, 12, 0)
        assert quiet.silent_now(saturday, "https://prod.example.com/z", "fri18:00-mon09:00", rules)
        assert not quiet.silent_now(saturday, "https://prod.example.com/z", "22:00-07:00", rules)

    def test_the_first_matching_rule_wins(self):
        rules = quiet.compile_rules("staging.example.com=; example.com=22:00-07:00")
        saturday = datetime(2026, 5, 9, 23, 0)
        assert not quiet.silent_now(saturday, "https://staging.example.com/x", "", rules)

    def test_matching_ignores_the_case_of_both_sides(self):
        rules = quiet.compile_rules("STAGING.EXAMPLE.COM=22:00-07:00")
        assert quiet.silent_now(
            datetime(2026, 5, 9, 23, 0), "https://Staging.Example.com/x", "", rules
        )

    def test_a_label_counts_as_a_fragment_too(self):
        rules = quiet.compile_rules("news.example.com=22:00-07:00")
        assert quiet.silent_now(datetime(2026, 5, 9, 23, 0), "news.example.com", "", rules)

    def test_malformed_rules_are_named(self):
        assert quiet.invalid_rules("staging.example.com=22:00-07:00") == []
        assert quiet.invalid_rules("news.example.com=") == []
        assert quiet.invalid_rules("") == []
        assert quiet.invalid_rules("no-equals-here") == ["no-equals-here"]
        assert quiet.invalid_rules("=22:00-07:00") == ["=22:00-07:00"]
        assert quiet.invalid_rules("a=garbage") == ["a=garbage"]

    def test_an_unreadable_window_falls_back_to_the_global_setting(self):
        rules = quiet.compile_rules("staging.example.com=garbage")
        assert rules == ()  # the rule is dropped, not read as "never quiet"
        assert quiet.silent_now(
            datetime(2026, 5, 9, 23, 0), "https://staging.example.com", "22:00-07:00", rules
        )
