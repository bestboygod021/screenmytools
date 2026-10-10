"""Named alert channels: one TOML file for routing, quiet, mutes and thresholds."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta

import pytest

from app.core import alerts, channels
from app.core.settings import CaptureSettings

GOOD = """
[ops]
kind = "webhook"
url = "https://hooks.example.com/ops"
match = "staging, preview"
quiet = "22:00-07:00"
mute = "banner"
min_diff = 0.05

[team]
kind = "email"
to = "team@example.com"
smtp_host = "smtp.example.com"
"""


def _settings(tmp_path, text: str = GOOD) -> CaptureSettings:
    target = tmp_path / "channels.toml"
    target.write_text(text, encoding="utf-8")
    settings = CaptureSettings()
    settings.output_dir = str(tmp_path)
    settings.alert_channels = str(target)
    return settings


def _item(url: str, diff: float = 0.5, **extra) -> dict:
    label = url.split("//", 1)[-1].replace("/", "_").replace(".", "_").rstrip("_")
    return {"url": url, "label": label, "diff": diff, "file": "shot.png", **extra}


class TestParsing:
    """A config file is only useful if a typo is named, not swallowed."""

    def test_a_good_file_is_read_with_its_rules(self):
        parsed = channels.parse_channels(GOOD)
        assert [channel.name for channel in parsed] == ["ops", "team"]
        ops = parsed[0]
        assert ops.kind == "webhook"
        assert ops.url == "https://hooks.example.com/ops"
        assert ops.matches == ("staging", "preview")
        assert ops.quiet == "22:00-07:00"
        assert ops.mutes == ("banner",)
        assert ops.min_diff == 0.05
        assert ops.enabled is True
        assert parsed[1].to == "team@example.com"
        assert parsed[1].smtp["smtp_host"] == "smtp.example.com"

    def test_defaults_are_the_wide_ones(self):
        parsed = channels.parse_channels('[all]\nkind = "webhook"\nurl = "https://hooks/x"\n')
        assert parsed[0].matches == ()
        assert parsed[0].mutes == ()
        assert parsed[0].quiet == ""
        assert parsed[0].min_diff is None
        assert channels.matches(parsed[0], _item("https://anything.example.com"))

    def test_a_bare_star_means_everything(self):
        parsed = channels.parse_channels('[all]\nurl = "https://hooks/x"\nmatch = "*"\n')
        assert parsed[0].matches == ()
        assert parsed[0].to_dict()["match"] == ["*"]

    def test_a_glob_style_fragment_still_matches(self):
        parsed = channels.parse_channels(
            '[shop]\nurl = "https://hooks/x"\nmatch = "*.shop.example.com"\n'
        )
        assert parsed[0].matches == ("shop.example.com",)

    def test_match_accepts_a_list_too(self):
        parsed = channels.parse_channels(
            '[ops]\nurl = "https://hooks/x"\nmatch = ["a.example.com", "b.example.com"]\n'
        )
        assert parsed[0].matches == ("a.example.com", "b.example.com")

    def test_an_unknown_key_is_named(self):
        with pytest.raises(channels.ChannelError) as excinfo:
            channels.parse_channels('[ops]\nurl = "https://hooks/x"\nquiett = "22:00-07:00"\n')
        assert "quiett" in str(excinfo.value)

    def test_an_unknown_kind_is_named(self):
        with pytest.raises(channels.ChannelError) as excinfo:
            channels.parse_channels('[ops]\nkind = "sms"\nurl = "https://hooks/x"\n')
        assert "sms" in str(excinfo.value)
        assert "webhook" in str(excinfo.value)

    def test_a_webhook_without_a_url_is_refused(self):
        with pytest.raises(channels.ChannelError) as excinfo:
            channels.parse_channels('[ops]\nkind = "webhook"\n')
        assert "url" in str(excinfo.value)

    def test_an_email_without_an_address_is_refused(self):
        with pytest.raises(channels.ChannelError) as excinfo:
            channels.parse_channels('[team]\nkind = "email"\n')
        assert "to" in str(excinfo.value)

    def test_a_silly_min_diff_is_refused(self):
        with pytest.raises(channels.ChannelError) as excinfo:
            channels.parse_channels('[ops]\nurl = "https://hooks/x"\nmin_diff = "soon"\n')
        assert "min_diff" in str(excinfo.value)
        with pytest.raises(channels.ChannelError):
            channels.parse_channels('[ops]\nurl = "https://hooks/x"\nmin_diff = 3\n')

    def test_a_silly_quiet_window_is_refused(self):
        with pytest.raises(channels.ChannelError) as excinfo:
            channels.parse_channels('[ops]\nurl = "https://hooks/x"\nquiet = "whenever"\n')
        assert "quiet" in str(excinfo.value)

    def test_an_empty_file_explains_what_to_add(self):
        with pytest.raises(channels.ChannelError) as excinfo:
            channels.parse_channels("")
        assert "[ops]" in str(excinfo.value)

    def test_a_broken_toml_is_reported_with_the_location(self, tmp_path):
        target = tmp_path / "channels.toml"
        target.write_text("[ops]\nurl = \n", encoding="utf-8")
        with pytest.raises(channels.ChannelError) as excinfo:
            channels.load_channels(target)
        assert "channels.toml" in str(excinfo.value)

    def test_a_missing_file_says_so(self, tmp_path):
        with pytest.raises(channels.ChannelError) as excinfo:
            channels.load_channels(tmp_path / "nope.toml")
        assert "nope.toml" in str(excinfo.value)

    def test_a_boolean_where_text_belongs_is_refused(self):
        with pytest.raises(channels.ChannelError):
            channels.parse_channels('[ops]\nurl = "https://hooks/x"\nenabled = "yes"\n')


class TestPlanning:
    """Who hears about what: match, mute and threshold, per channel."""

    def test_a_change_goes_to_every_channel_that_wants_it(self):
        parsed = channels.parse_channels(
            '[ops]\nurl = "https://hooks/ops"\n\n[team]\nurl = "https://hooks/team"\n'
        )
        plan = channels.plan(parsed, [_item("https://a.example.com")])
        assert [channel.name for channel, _ in plan] == ["ops", "team"]

    def test_match_narrows_a_channel(self):
        parsed = channels.parse_channels(GOOD)
        plan = channels.plan(parsed, [_item("https://news.example.com")])
        assert [channel.name for channel, _ in plan] == ["team"]

    def test_match_reads_the_label_as_well_as_the_url(self):
        parsed = channels.parse_channels(
            '[ops]\nurl = "https://hooks/ops"\nmatch = "staging_example_com"\n'
        )
        plan = channels.plan(
            parsed, [_item("https://www.example.com", label="staging_example_com_a")]
        )
        assert [channel.name for channel, _ in plan] == ["ops"]

    def test_a_mute_only_silences_that_channel(self):
        parsed = channels.parse_channels(GOOD)
        items = [_item("https://staging.example.com/banner")]
        plan = channels.plan(parsed, items)
        assert [channel.name for channel, _ in plan] == ["team"]

    def test_a_threshold_only_applies_to_its_channel(self):
        parsed = channels.parse_channels(GOOD)
        plan = channels.plan(parsed, [_item("https://staging.example.com/a", diff=0.01)])
        assert [channel.name for channel, _ in plan] == ["team"]

    def test_a_disabled_channel_stays_silent(self):
        parsed = channels.parse_channels('[ops]\nurl = "https://hooks/ops"\nenabled = false\n')
        assert channels.plan(parsed, [_item("https://a.example.com")]) == []

    def test_wants_is_the_single_item_version(self):
        parsed = channels.parse_channels(GOOD)
        assert channels.wants(parsed[0], _item("https://staging.example.com/a", diff=0.9))
        assert not channels.wants(parsed[0], _item("https://staging.example.com/a", diff=0.01))
        assert not channels.wants(parsed[0], _item("https://news.example.com"))


class TestDispatch:
    """Delivery: one POST per channel, one mail per email channel."""

    @staticmethod
    def _capture(monkeypatch) -> list[tuple]:
        sent: list[tuple] = []
        monkeypatch.setattr(
            alerts,
            "send_webhook",
            lambda url, payload, timeout=10.0: sent.append(("webhook", url, payload)) or True,
        )
        monkeypatch.setattr(
            alerts,
            "send_email",
            lambda host, port, user, password, to, subject, body: (
                sent.append(("email", host, port, to, subject, body)) or True
            ),
        )
        return sent

    def test_each_channel_gets_its_own_delivery(self, tmp_path, monkeypatch):
        sent = self._capture(monkeypatch)
        settings = _settings(tmp_path)
        results = channels.notify(settings, [_item("https://staging.example.com/a")], "run-1")
        assert sorted(results) == [("ops:webhook", True), ("team:email", True)]
        kinds = sorted(kind for kind, *_ in sent)
        assert kinds == ["email", "webhook"]
        payload = next(entry[2] for entry in sent if entry[0] == "webhook")
        assert payload["items"][0]["url"] == "https://staging.example.com/a"

    def test_a_channel_that_refuses_is_reported_not_raised(self, tmp_path, monkeypatch):
        monkeypatch.setattr(alerts, "send_webhook", lambda url, payload, timeout=10.0: False)
        monkeypatch.setattr(alerts, "send_email", lambda *args, **kwargs: False)
        settings = _settings(tmp_path)
        results = channels.notify(settings, [_item("https://staging.example.com/a")])
        assert ("ops:webhook", False) in results

    def test_a_broken_file_is_a_failed_result_not_an_exception(self, tmp_path):
        settings = _settings(tmp_path, text="[ops]\nquiett = 1\n")
        assert channels.notify(settings, [_item("https://a.example.com")]) == [("channels", False)]

    def test_quiet_marks_the_item_with_its_channel(self, tmp_path, monkeypatch):
        sent = self._capture(monkeypatch)
        settings = _settings(
            tmp_path,
            text='[ops]\nurl = "https://hooks/ops"\nquiet = "00:00-23:59"\n'
            '\n[team]\nurl = "https://hooks/team"\n',
        )
        results = channels.notify(settings, [_item("https://a.example.com")])
        assert ("ops:queued", True) in results
        assert ("team:webhook", True) in results
        assert [entry[1] for entry in sent] == ["https://hooks/team"]
        queued = json.loads((tmp_path / "pending-alerts.json").read_text(encoding="utf-8"))
        assert queued[0]["channels"] == ["ops"]

    def test_a_queued_item_is_not_offered_to_the_other_channels(self, tmp_path, monkeypatch):
        sent = self._capture(monkeypatch)
        settings = _settings(
            tmp_path,
            text='[ops]\nurl = "https://hooks/ops"\nquiet = "00:00-23:59"\n'
            '\n[team]\nurl = "https://hooks/team"\n',
        )
        tagged = _item("https://a.example.com", channels=["ops"])
        results = channels.notify(settings, [tagged])
        assert results == [("ops:queued", True)]
        assert sent == []

    def test_two_channels_can_hold_the_same_url_separately(self, tmp_path, monkeypatch):
        self._capture(monkeypatch)
        settings = _settings(
            tmp_path,
            text='[ops]\nurl = "https://hooks/ops"\nquiet = "00:00-23:59"\n'
            '\n[team]\nurl = "https://hooks/team"\nquiet = "00:00-23:59"\n',
        )
        channels.notify(settings, [_item("https://a.example.com")])
        queued = json.loads((tmp_path / "pending-alerts.json").read_text(encoding="utf-8"))
        assert sorted(tag for item in queued for tag in item["channels"]) == ["ops", "team"]

    def test_nothing_changed_sends_nothing(self, tmp_path, monkeypatch):
        sent = self._capture(monkeypatch)
        settings = _settings(tmp_path)
        assert channels.notify(settings, []) == []
        assert sent == []

    def test_the_summary_lists_every_channel(self):
        text = channels.summary(channels.parse_channels(GOOD))
        assert "2 channel(s) (2 enabled):" in text
        assert "ops" in text and "https://hooks.example.com/ops" in text
        assert "match: staging, preview" in text
        assert "quiet: 22:00-07:00" in text
        assert "mute: banner" in text
        assert "min diff: 0.05" in text
        assert "team@example.com" in text

    def test_to_dict_is_plain_data(self):
        data = channels.parse_channels(GOOD)[0].to_dict()
        assert data == {
            "name": "ops",
            "kind": "webhook",
            "target": "https://hooks.example.com/ops",
            "match": ["staging", "preview"],
            "quiet": "22:00-07:00",
            "mute": ["banner"],
            "min_diff": 0.05,
            "enabled": True,
            "heartbeat": "",
        }


class TestTestSend:
    """``channels --test``: prove the credentials before a page changes."""

    def test_every_enabled_channel_is_exercised(self, tmp_path, monkeypatch):
        sent = TestDispatch._capture(monkeypatch)
        settings = _settings(
            tmp_path,
            text=GOOD + '\n[off]\nurl = "https://hooks/off"\nenabled = false\n',
        )
        results = channels.test_notify(settings, "Capture Bot test", "hello")
        assert sorted(results) == [("ops:webhook", True), ("team:email", True)]
        assert sorted(entry[0] for entry in sent) == ["email", "webhook"]

    def test_the_webhook_payload_is_a_note(self, tmp_path, monkeypatch):
        sent = TestDispatch._capture(monkeypatch)
        settings = _settings(tmp_path, text='[ops]\nurl = "https://hooks/ops"\n')
        channels.test_notify(settings, "Capture Bot test", "hello there")
        payload = sent[0][2]
        assert payload["event"] == "notice"
        assert payload["title"] == "Capture Bot test"
        assert payload["body"] == "hello there"

    def test_a_slack_channel_gets_a_slack_note(self, tmp_path, monkeypatch):
        sent = TestDispatch._capture(monkeypatch)
        settings = _settings(tmp_path, text='[ops]\nurl = "https://hooks/ops"\n')
        settings.alert_webhook_kind = "slack"
        channels.test_notify(settings, "Capture Bot test", "hello there")
        assert sent[0][2]["text"].startswith("*Capture Bot*: Capture Bot test")

    def test_a_channel_refusing_is_reported(self, tmp_path, monkeypatch):
        monkeypatch.setattr(alerts, "send_webhook", lambda url, payload, timeout=10.0: False)
        settings = _settings(tmp_path, text='[ops]\nurl = "https://hooks/ops"\n')
        assert channels.test_notify(settings, "t", "b") == [("ops:webhook", False)]

    def test_a_webhook_that_raises_is_reported_not_raised(self, tmp_path, monkeypatch):
        def boom(url, payload, timeout=10.0):
            raise OSError("no route to host")

        monkeypatch.setattr(alerts, "send_webhook", boom)
        settings = _settings(tmp_path, text='[ops]\nurl = "https://hooks/ops"\n')
        assert channels.test_notify(settings, "t", "b") == [("ops:webhook", False)]

    def test_an_email_channel_uses_the_settings_smtp_when_it_has_no_own(
        self, tmp_path, monkeypatch
    ):
        sent = TestDispatch._capture(monkeypatch)
        settings = _settings(tmp_path, text='[team]\nkind = "email"\nto = "t@example.com"\n')
        settings.smtp_host = "smtp.from.settings"
        settings.smtp_port = 2525
        settings.smtp_user = "bot"
        channels.test_notify(settings, "t", "b")
        assert sent[0][:4] == ("email", "smtp.from.settings", 2525, "t@example.com")


class TestSettingsAndEngine:
    """The file travels through ``CaptureSettings.validate`` and the run itself."""

    def test_validate_accepts_a_good_file(self, tmp_path):
        settings = _settings(tmp_path)
        settings.validate()
        assert settings.alert_channels.endswith("channels.toml")

    def test_validate_refuses_a_bad_file(self, tmp_path):
        from app.core.settings import SettingsError

        settings = _settings(tmp_path, text="[ops]\nquiett = 1\n")
        with pytest.raises(SettingsError) as excinfo:
            settings.validate()
        assert "quiett" in str(excinfo.value)

    def test_validate_ignores_an_empty_path(self, tmp_path):
        settings = CaptureSettings(output_dir=str(tmp_path), alert_channels="")
        settings.validate()  # no file to check, no error about it

    def test_a_file_still_parses_when_the_engine_reads_it(self, tmp_path):
        settings = _settings(tmp_path)
        assert channels.load_channels(settings.alert_channels)[0].name == "ops"


class TestCommandChannels:
    """``kind = "command"``: any destination curl cannot reach."""

    def test_the_argv_is_parsed_and_never_a_shell_string(self):
        parsed = channels.parse_channels(
            '[pager]\nkind = "command"\nexec = "page-oncall --team ops --quiet"\n'
        )[0]
        assert parsed.command == ("page-oncall", "--team", "ops", "--quiet")
        assert parsed.timeout == channels.DEFAULT_COMMAND_TIMEOUT
        assert parsed.target() == "page-oncall --team ops --quiet"

    def test_a_list_argv_is_kept_as_it_is(self):
        parsed = channels.parse_channels(
            '[pager]\nkind = "command"\nexec = ["/usr/local/bin/page", "--team", "ops"]\ntimeout = 12\n'
        )[0]
        assert parsed.command == ("/usr/local/bin/page", "--team", "ops")
        assert parsed.timeout == 12.0
        assert "timeout: 12s" in parsed.describe()

    def test_quotes_in_the_string_form_are_honoured(self):
        parsed = channels.parse_channels(
            '[pager]\nkind = "command"\nexec = \'/usr/bin/notify "the ops room"\'\n'
        )[0]
        assert parsed.command == ("/usr/bin/notify", "the ops room")

    def test_a_command_without_exec_is_refused(self):
        with pytest.raises(channels.ChannelError) as excinfo:
            channels.parse_channels('[pager]\nkind = "command"\n')
        assert "exec" in str(excinfo.value)

    def test_an_empty_exec_is_refused(self):
        with pytest.raises(channels.ChannelError) as excinfo:
            channels.parse_channels('[pager]\nkind = "command"\nexec = "   "\n')
        assert "exec" in str(excinfo.value)

    def test_exec_on_a_webhook_is_refused(self):
        with pytest.raises(channels.ChannelError) as excinfo:
            channels.parse_channels(
                '[ops]\nkind = "webhook"\nurl = "https://hooks/x"\nexec = "cat"\n'
            )
        assert "command" in str(excinfo.value)

    def test_a_silly_timeout_is_refused(self):
        with pytest.raises(channels.ChannelError) as excinfo:
            channels.parse_channels('[pager]\nkind = "command"\nexec = "cat"\ntimeout = 9999\n')
        assert "timeout" in str(excinfo.value)

    def test_to_dict_carries_the_command(self):
        data = channels.parse_channels('[pager]\nkind = "command"\nexec = ["cat"]\ntimeout = 5\n')[
            0
        ].to_dict()
        assert data["exec"] == ["cat"]
        assert data["timeout"] == 5.0
        assert data["target"] == "cat"

    def test_the_payload_arrives_on_stdin_with_its_environment(self, tmp_path):
        """A one-line script should not need a JSON parser to read the event.

        The script runs under the *current* interpreter, so the same test proves
        the same thing on Windows and on Linux.
        """
        script = (
            "import os, pathlib, sys\n"
            f"pathlib.Path({str(tmp_path / 'payload.json')!r}).write_bytes(sys.stdin.buffer.read())\n"
            f"pathlib.Path({str(tmp_path / 'event')!r}).write_text(os.environ['CAPTURE_BOT_EVENT'])\n"
            f"pathlib.Path({str(tmp_path / 'count')!r}).write_text(os.environ['CAPTURE_BOT_COUNT'])\n"
        )
        parsed = channels.parse_channels(
            f'[pager]\nkind = "command"\nexec = [{json.dumps(sys.executable)}, "-c", '
            f"{json.dumps(script)}]\n"
        )[0]
        assert channels.send_command(parsed, {"event": "visual_change", "count": 3}) is True
        assert (tmp_path / "event").read_text(encoding="utf-8").strip() == "visual_change"
        assert (tmp_path / "count").read_text(encoding="utf-8").strip() == "3"
        payload = json.loads((tmp_path / "payload.json").read_text(encoding="utf-8"))
        assert payload["event"] == "visual_change"

    def test_an_exit_code_that_is_not_zero_is_a_failure(self):
        parsed = channels.parse_channels(
            f'[pager]\nkind = "command"\nexec = [{json.dumps(sys.executable)}, "-c", '
            '"raise SystemExit(1)"]\n'
        )[0]
        assert channels.send_command(parsed, {"event": "notice"}) is False

    def test_a_missing_program_is_a_failure_not_an_exception(self):
        parsed = channels.parse_channels('[pager]\nkind = "command"\nexec = "/no/such/program"\n')[
            0
        ]
        assert channels.send_command(parsed, {"event": "notice"}) is False

    def test_a_slow_command_hits_its_timeout(self):
        parsed = channels.parse_channels(
            f'[pager]\nkind = "command"\n'
            f'exec = [{json.dumps(sys.executable)}, "-c", "import time; time.sleep(5)"]\n'
            "timeout = 0.2\n"
        )[0]
        assert channels.send_command(parsed, {"event": "notice"}) is False

    def test_a_change_goes_through_the_command_channel(self, tmp_path, monkeypatch):
        seen: list[tuple] = []
        monkeypatch.setattr(
            channels,
            "send_command",
            lambda channel, payload, timeout=None: seen.append(payload) or True,
        )
        target = tmp_path / "channels.toml"
        target.write_text('[pager]\nkind = "command"\nexec = "cat"\n', encoding="utf-8")
        settings = CaptureSettings(output_dir=str(tmp_path), alert_channels=str(target))
        results = channels.notify(settings, [_item("https://a.example.com")], "run-1")
        assert results == [("pager:command", True)]
        assert seen[0]["items"][0]["url"] == "https://a.example.com"

    def test_a_test_send_uses_the_same_transport(self, tmp_path, monkeypatch):
        seen: list[tuple] = []
        monkeypatch.setattr(
            channels,
            "send_command",
            lambda channel, payload, timeout=None: seen.append(payload) or True,
        )
        target = tmp_path / "channels.toml"
        target.write_text('[pager]\nkind = "command"\nexec = "cat"\n', encoding="utf-8")
        settings = CaptureSettings(output_dir=str(tmp_path), alert_channels=str(target))
        assert channels.test_notify(settings, "Capture Bot test", "hello") == [
            ("pager:command", True)
        ]
        assert seen[0]["body"] == "hello"

    def test_deliver_routes_each_kind(self, tmp_path, monkeypatch):
        sent: list[str] = []
        monkeypatch.setattr(
            alerts,
            "send_webhook",
            lambda url, payload, timeout=10.0: sent.append("webhook") or True,
        )
        monkeypatch.setattr(
            alerts, "send_email", lambda *args, **kwargs: sent.append("email") or True
        )
        monkeypatch.setattr(
            channels,
            "send_command",
            lambda channel, payload, timeout=None: sent.append("command") or True,
        )
        settings = CaptureSettings(output_dir=str(tmp_path), smtp_host="smtp.example.com")
        parsed = channels.parse_channels(
            '[a]\nkind = "webhook"\nurl = "https://hooks/a"\n\n'
            '[b]\nkind = "email"\nto = "b@example.com"\n\n'
            '[c]\nkind = "command"\nexec = "cat"\n'
        )
        for channel in parsed:
            assert channels.deliver(channel, settings, {"event": "notice", "title": "t"})
        assert sent == ["webhook", "email", "command"]


class TestHeartbeatSchedule:
    """``heartbeat = "mon 09:00"`` and the arithmetic behind "due"."""

    def test_a_weekly_schedule_is_parsed(self):
        beat = channels.parse_heartbeat("mon 09:00")
        assert (beat.weekday, beat.hour, beat.minute) == (0, 9, 0)
        assert beat.text == "mon 09:00"
        assert channels.parse_heartbeat("monday 09:00").weekday == 0
        assert channels.parse_heartbeat("Friday 17:30").text == "fri 17:30"

    def test_a_daily_schedule_is_parsed(self):
        beat = channels.parse_heartbeat("07:05")
        assert beat.weekday is None
        assert beat.text == "07:05"

    def test_an_empty_schedule_means_no_heartbeat(self):
        assert channels.parse_heartbeat("") is None
        assert channels.parse_heartbeat(None) is None

    @pytest.mark.parametrize(
        "bad", ["someday 09:00", "mon 25:00", "mon 09:60", "monday", "09:00 monday"]
    )
    def test_a_silly_schedule_is_named(self, bad):
        with pytest.raises(channels.ChannelError) as excinfo:
            channels.parse_heartbeat(bad)
        assert "heartbeat" in str(excinfo.value) or "weekday" in str(excinfo.value)

    def test_a_non_text_schedule_is_refused(self):
        with pytest.raises(channels.ChannelError):
            channels.parse_heartbeat(900)

    def test_the_previous_moment_moves_with_the_clock(self):
        beat = channels.parse_heartbeat("mon 09:00")
        assert beat.previous(datetime(2026, 10, 5, 9, 30)) == datetime(2026, 10, 5, 9, 0)
        assert beat.previous(datetime(2026, 10, 5, 8, 0)) == datetime(2026, 9, 28, 9, 0)
        assert beat.previous(datetime(2026, 10, 7, 12, 0)) == datetime(2026, 10, 5, 9, 0)

    def test_the_next_moment_moves_with_the_clock(self):
        beat = channels.parse_heartbeat("mon 09:00")
        assert beat.next_after(datetime(2026, 10, 5, 9, 30)) == datetime(2026, 10, 12, 9, 0)
        assert beat.next_after(datetime(2026, 10, 5, 9, 0)) == datetime(2026, 10, 12, 9, 0)

    def test_a_daily_beat_steps_one_day_at_a_time(self):
        beat = channels.parse_heartbeat("09:00")
        assert beat.previous(datetime(2026, 10, 7, 8, 0)) == datetime(2026, 10, 6, 9, 0)
        assert beat.previous(datetime(2026, 10, 7, 12, 0)) == datetime(2026, 10, 7, 9, 0)
        assert beat.next_after(datetime(2026, 10, 7, 9, 0)) == datetime(2026, 10, 8, 9, 0)

    def test_the_channel_carries_its_schedule(self):
        parsed = channels.parse_channels(
            '[ops]\nurl = "https://hooks/ops"\nheartbeat = "mon 09:00"\n'
        )[0]
        assert parsed.heartbeat is not None
        assert "heartbeat: mon 09:00" in parsed.describe()
        assert parsed.to_dict()["heartbeat"] == "mon 09:00"

    def test_to_dict_says_so_when_there_is_none(self):
        parsed = channels.parse_channels('[ops]\nurl = "https://hooks/ops"\n')[0]
        assert parsed.heartbeat is None
        assert parsed.to_dict()["heartbeat"] == ""


class TestHeartbeatDispatch:
    """Who gets a "still here", and when."""

    @staticmethod
    def _folder(tmp_path, channels_text: str):
        target = tmp_path / "channels.toml"
        target.write_text(channels_text, encoding="utf-8")
        settings = CaptureSettings(output_dir=str(tmp_path), alert_channels=str(target))
        return settings

    def _sends(self, monkeypatch) -> list[dict]:
        sent: list[dict] = []
        monkeypatch.setattr(
            alerts,
            "send_webhook",
            lambda url, payload, timeout=10.0: sent.append(payload) or True,
        )
        return sent

    def test_a_channel_that_never_spoke_is_due(self, tmp_path):
        parsed = channels.parse_channels(
            '[ops]\nurl = "https://hooks/ops"\nheartbeat = "mon 09:00"\n'
        )
        due = channels.beats_due(parsed, tmp_path, datetime(2026, 10, 5, 9, 30))
        assert [(channel.name, when) for channel, when in due] == [
            ("ops", datetime(2026, 10, 5, 9, 0))
        ]

    def test_a_channel_without_a_schedule_is_never_due(self, tmp_path):
        parsed = channels.parse_channels('[ops]\nurl = "https://hooks/ops"\n')
        assert channels.beats_due(parsed, tmp_path, datetime(2026, 10, 5, 9, 30)) == []

    def test_a_disabled_channel_is_never_due(self, tmp_path):
        parsed = channels.parse_channels(
            '[ops]\nurl = "https://hooks/ops"\nheartbeat = "09:00"\nenabled = false\n'
        )
        assert channels.beats_due(parsed, tmp_path, datetime(2026, 10, 5, 9, 30)) == []

    def test_the_moment_is_the_point_not_the_run(self, tmp_path, monkeypatch):
        sent = self._sends(monkeypatch)
        settings = self._folder(
            tmp_path, '[ops]\nurl = "https://hooks/ops"\nheartbeat = "mon 09:00"\n'
        )
        assert channels.run_heartbeats(settings, datetime(2026, 10, 5, 9, 30)) == [
            ("ops:heartbeat", True)
        ]
        # A second run on Tuesday has nothing to do.
        assert channels.run_heartbeats(settings, datetime(2026, 10, 6, 9, 30)) == []
        # The next Monday is a new moment.
        assert channels.run_heartbeats(settings, datetime(2026, 10, 12, 9, 30)) == [
            ("ops:heartbeat", True)
        ]
        assert len(sent) == 2

    def test_an_alert_counts_as_hearing_from_the_channel(self, tmp_path, monkeypatch):
        self._sends(monkeypatch)
        settings = self._folder(
            tmp_path, '[ops]\nurl = "https://hooks/ops"\nheartbeat = "mon 09:00"\n'
        )
        # An alert goes out on the Monday morning...
        channels.notify(
            settings, [_item("https://a.example.com")], "run-1", moment=datetime(2026, 10, 5, 9, 15)
        )
        # ...so the heartbeat has nothing to prove that week.
        assert channels.run_heartbeats(settings, datetime(2026, 10, 5, 9, 30)) == []
        assert channels.run_heartbeats(settings, datetime(2026, 10, 12, 9, 30)) != []

    def test_a_test_send_also_counts(self, tmp_path, monkeypatch):
        self._sends(monkeypatch)
        settings = self._folder(
            tmp_path, '[ops]\nurl = "https://hooks/ops"\nheartbeat = "mon 09:00"\n'
        )
        channels.test_notify(settings, "Capture Bot test", "hello")
        assert channels.run_heartbeats(settings, datetime(2026, 10, 5, 9, 30)) == []

    def test_the_note_says_what_happened_lately(self, tmp_path, monkeypatch):
        sent = self._sends(monkeypatch)
        (tmp_path / "capture-report-20261002-090000.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "results": [
                        {
                            "url": "https://a.example.com",
                            "label": "a",
                            "status": "success",
                            "diff": 0.4,
                        },
                        {
                            "url": "https://b.example.com",
                            "label": "b",
                            "status": "success",
                            "diff": None,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        settings = self._folder(tmp_path, '[ops]\nurl = "https://hooks/ops"\nheartbeat = "09:00"\n')
        channels.run_heartbeats(settings, datetime(2026, 10, 5, 9, 30))
        assert sent[0]["title"] == "Capture Bot heartbeat"
        assert "2 capture(s) of 2 site(s)" in sent[0]["body"]
        assert "1 page(s) changed" in sent[0]["body"]
        assert sent[0]["scheduled"] == "2026-10-05T09:00:00"

    def test_a_dry_run_sends_nothing_and_records_nothing(self, tmp_path, monkeypatch):
        sent = self._sends(monkeypatch)
        settings = self._folder(tmp_path, '[ops]\nurl = "https://hooks/ops"\nheartbeat = "09:00"\n')
        assert channels.run_heartbeats(settings, datetime(2026, 10, 5, 9, 30), dry_run=True) == [
            ("ops:heartbeat", True)
        ]
        assert sent == []
        assert channels.load_beats(tmp_path) == {}
        # ...so the real one still goes out afterwards.
        assert channels.run_heartbeats(settings, datetime(2026, 10, 5, 10, 0)) != []

    def test_a_failed_heartbeat_is_not_recorded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(alerts, "send_webhook", lambda url, payload, timeout=10.0: False)
        settings = self._folder(tmp_path, '[ops]\nurl = "https://hooks/ops"\nheartbeat = "09:00"\n')
        assert channels.run_heartbeats(settings, datetime(2026, 10, 5, 9, 30)) == [
            ("ops:heartbeat", False)
        ]
        assert channels.load_beats(tmp_path) == {}

    def test_the_state_file_survives_and_reads_back(self, tmp_path):
        channels.record_beats(tmp_path, ["ops"], datetime(2026, 10, 5, 9, 0))
        assert channels.load_beats(tmp_path) == {"ops": datetime(2026, 10, 5, 9, 0)}
        channels.record_beats(tmp_path, ["team"], datetime(2026, 10, 6, 9, 0))
        assert sorted(channels.load_beats(tmp_path)) == ["ops", "team"]

    def test_unreadable_state_means_never(self, tmp_path):
        (tmp_path / channels.BEAT_STATE_FILENAME).write_text("{not json", encoding="utf-8")
        assert channels.load_beats(tmp_path) == {}
        (tmp_path / channels.BEAT_STATE_FILENAME).write_text(
            '{"ops": "yesterday"}', encoding="utf-8"
        )
        assert channels.load_beats(tmp_path) == {}

    def test_a_command_channel_gets_its_heartbeat_too(self, tmp_path, monkeypatch):
        seen: list[dict] = []
        monkeypatch.setattr(
            channels,
            "send_command",
            lambda channel, payload, timeout=None: seen.append(payload) or True,
        )
        settings = self._folder(
            tmp_path, '[pager]\nkind = "command"\nexec = "cat"\nheartbeat = "09:00"\n'
        )
        assert channels.run_heartbeats(settings, datetime(2026, 10, 5, 9, 30)) == [
            ("pager:heartbeat", True)
        ]
        assert seen[0]["event"] == "notice"

    def test_the_summary_counts_only_the_window(self, tmp_path):
        old = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
        (tmp_path / "capture-report-old.json").write_text(
            json.dumps({"generated_at": old, "results": [{"url": "https://old", "diff": 0.9}]}),
            encoding="utf-8",
        )
        summary = channels.heartbeat_summary(tmp_path, days=7)
        assert "0 capture(s)" in summary
        assert "1 page(s) changed" not in summary
