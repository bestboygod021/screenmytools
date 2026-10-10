"""Unit tests for change-alert dispatch (HTTP/SMTP mocked)."""

from __future__ import annotations

import pytest

from app.core import alerts
from app.core.settings import CaptureSettings
from tests.fakes import PageScript
from tests.test_engine import run_engine

CHANGED = [{"label": "site", "url": "https://x", "diff": 0.3, "timestamp": "t"}]


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestPayload:
    def test_build_payload(self):
        payload = alerts.build_payload(CHANGED, run_id="r1")
        assert payload["event"] == "visual_change"
        assert payload["count"] == 1
        assert payload["run_id"] == "r1"

    def test_email_body_lists_items(self):
        body = alerts.format_email_body(CHANGED)
        assert "site" in body and "0.300" in body


class TestSendWebhook:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(
            alerts.urllib.request, "urlopen", lambda req, timeout=10.0: _FakeResponse(204)
        )
        assert alerts.send_webhook("https://hook", {}) is True

    def test_non_2xx(self, monkeypatch):
        monkeypatch.setattr(
            alerts.urllib.request, "urlopen", lambda req, timeout=10.0: _FakeResponse(500)
        )
        assert alerts.send_webhook("https://hook", {}) is False


class TestNotify:
    def test_no_changes_sends_nothing(self):
        settings = CaptureSettings(output_dir="/tmp/x", alert_webhook_url="https://hook")
        assert alerts.notify(settings, []) == []

    def test_webhook_only(self, monkeypatch):
        sent = {}

        def fake_webhook(url, payload, timeout=10.0):
            sent["url"] = url
            return True

        monkeypatch.setattr(alerts, "send_webhook", fake_webhook)
        settings = CaptureSettings(output_dir="/tmp/x", alert_webhook_url="https://hook")
        assert alerts.notify(settings, CHANGED) == [("webhook", True)]
        assert sent["url"] == "https://hook"

    def test_webhook_failure_is_captured(self, monkeypatch):
        def boom(url, payload, timeout=10.0):
            raise OSError("down")

        monkeypatch.setattr(alerts, "send_webhook", boom)
        settings = CaptureSettings(output_dir="/tmp/x", alert_webhook_url="https://hook")
        assert alerts.notify(settings, CHANGED) == [("webhook", False)]

    def test_email_only(self, monkeypatch):
        monkeypatch.setattr(alerts, "send_email", lambda *a, **k: True)
        settings = CaptureSettings(output_dir="/tmp/x", alert_email_to="a@b.com", smtp_host="smtp")
        assert alerts.notify(settings, CHANGED) == [("email", True)]

    def test_both_channels(self, monkeypatch):
        monkeypatch.setattr(alerts, "send_webhook", lambda url, payload, timeout=10.0: True)
        monkeypatch.setattr(alerts, "send_email", lambda *a, **k: True)
        settings = CaptureSettings(
            output_dir="/tmp/x",
            alert_webhook_url="https://h",
            alert_email_to="a@b",
            smtp_host="smtp",
        )
        result = alerts.notify(settings, CHANGED)
        assert ("webhook", True) in result and ("email", True) in result


class TestEngineAlertHook:
    def test_changed_page_triggers_alert(self, tmp_path, monkeypatch):
        import app.core.engine as eng

        captured = {}

        def fake_notify(settings, changed, run_id=""):
            captured["changed"] = changed
            return [("webhook", True)]

        monkeypatch.setattr(alerts, "notify", fake_notify)
        monkeypatch.setattr(eng, "diff_ratio", lambda a, b: 0.9)

        settings = CaptureSettings(
            output_dir=tmp_path,
            alert_enabled=True,
            alert_webhook_url="https://hook",
            change_detection_enabled=True,
        )
        # First run seeds the baseline (no change); the second run differs.
        run_engine(settings, ["https://example.com"], PageScript())
        assert captured == {}
        run_engine(settings, ["https://example.com"], PageScript())

        assert captured.get("changed")
        assert captured["changed"][0]["url"] == "https://example.com"

    def test_alerts_disabled_means_no_notify(self, tmp_path, monkeypatch):
        called = []
        monkeypatch.setattr(alerts, "notify", lambda *a, **k: called.append(1) or [])

        settings = CaptureSettings(output_dir=tmp_path, alert_enabled=False)
        run_engine(settings, ["https://example.com"], PageScript())
        assert called == []


class TestSettingsValidation:
    @pytest.mark.parametrize(
        "kwargs,ok",
        [
            ({"alert_enabled": True, "alert_webhook_url": "https://h"}, True),
            ({"alert_enabled": True, "alert_email_to": "a@b", "smtp_host": "smtp"}, True),
            ({"alert_enabled": True}, False),  # no channel configured
            ({"smtp_port": 0}, False),
            ({"smtp_port": 70000}, False),
        ],
    )
    def test_validate(self, kwargs, ok):
        settings = CaptureSettings(output_dir="/tmp/x", **kwargs)
        if ok:
            settings.validate()
        else:
            with pytest.raises(Exception):
                settings.validate()


class TestWebhookKinds:
    def test_generic_is_the_default_shape(self):
        payload = alerts.build_payload(CHANGED, run_id="r1", kind="generic")
        assert payload["event"] == "visual_change" and payload["items"] == CHANGED

    def test_slack_payload_has_text(self):
        payload = alerts.build_payload(CHANGED, kind="slack")
        assert set(payload) == {"text"}
        assert "1 page(s) changed" in payload["text"]
        assert "site" in payload["text"]

    def test_teams_payload_is_a_message_card(self):
        payload = alerts.build_payload(CHANGED, kind="teams")
        assert payload["@type"] == "MessageCard"
        assert payload["summary"] == "1 page(s) changed"
        assert "site" in payload["text"]

    def test_unknown_kind_falls_back_to_generic(self):
        payload = alerts.build_payload(CHANGED, kind="nope")
        assert payload["event"] == "visual_change"

    def test_notify_sends_the_configured_kind(self, monkeypatch):
        seen = {}

        def fake_webhook(url, payload, timeout=10.0):
            seen.update(payload)
            return True

        monkeypatch.setattr(alerts, "send_webhook", fake_webhook)
        settings = CaptureSettings(
            output_dir="/tmp/x", alert_webhook_url="https://hook", alert_webhook_kind="slack"
        )
        assert alerts.notify(settings, CHANGED) == [("webhook", True)]
        assert set(seen) == {"text"}  # the Slack shape, not the generic one


class TestWebhookKindValidation:
    def test_valid_kinds_pass(self):
        for kind in alerts.WEBHOOK_KINDS:
            CaptureSettings(output_dir="x", alert_webhook_kind=kind).validate()

    def test_invalid_kind_is_rejected(self):
        from app.core.settings import SettingsError

        with pytest.raises(SettingsError):
            CaptureSettings(output_dir="x", alert_webhook_kind="irc").validate()


class TestMuteList:
    def test_the_list_splits_on_commas_and_spaces(self):
        assert alerts.parse_mute_list("staging, preview ,qa") == ["staging", "preview", "qa"]

    def test_an_empty_list_is_empty(self):
        assert alerts.parse_mute_list("") == []
        assert alerts.parse_mute_list("  ,  ") == []

    def test_matching_is_case_insensitive_and_covers_the_label(self):
        assert alerts.is_muted({"url": "https://Staging.example.com", "label": "x"}, ["staging"])
        assert alerts.is_muted({"url": "https://a.com", "label": "preview_a_com"}, ["preview"])
        assert not alerts.is_muted({"url": "https://a.com", "label": "a_com"}, ["staging"])

    def test_split_muted_keeps_the_rest(self):
        items = [
            {"url": "https://staging.example.com", "label": "staging"},
            {"url": "https://shop.example.com", "label": "shop"},
        ]
        kept, muted = alerts.split_muted(items, "staging")
        assert [item["url"] for item in kept] == ["https://shop.example.com"]
        assert [item["url"] for item in muted] == ["https://staging.example.com"]

    def test_no_list_mutes_nothing(self):
        items = [{"url": "https://staging.example.com"}]
        kept, muted = alerts.split_muted(items, "")
        assert kept == items and muted == []


class TestNotifyNote:
    """One-off operational notes (no change list) through the alert channels."""

    def test_the_generic_body_names_the_event(self):
        payload = alerts.build_note_payload("Cap hit", "3 runs dropped")
        assert payload["event"] == "notice"
        assert payload["title"] == "Cap hit"
        assert payload["body"] == "3 runs dropped"

    def test_slack_and_teams_get_their_own_shape(self):
        slack = alerts.build_note_payload("Cap hit", "3 runs dropped", "slack")
        assert "Cap hit" in slack["text"] and "3 runs dropped" in slack["text"]
        teams = alerts.build_note_payload("Cap hit", "3 runs dropped", "teams")
        assert teams["summary"] == "Cap hit"
        assert teams["title"] == "Capture Bot: Cap hit"
        assert teams["text"] == "3 runs dropped"

    def test_every_configured_channel_receives_it(self, monkeypatch):
        from types import SimpleNamespace

        sent = []
        monkeypatch.setattr(
            alerts,
            "send_webhook",
            lambda url, payload, **kwargs: sent.append(("webhook", url, payload)) or True,
        )
        monkeypatch.setattr(
            alerts,
            "send_email",
            lambda *args, **kwargs: sent.append(("email", args[4], args[5])) or True,
        )
        settings = SimpleNamespace(
            alert_webhook_url="https://hook.example.com",
            alert_webhook_kind="slack",
            alert_email_to="ops@example.com",
            smtp_host="smtp.example.com",
            smtp_port=587,
        )
        assert alerts.notify_note(settings, "Cap hit", "3 runs dropped") == [
            ("webhook", True),
            ("email", True),
        ]
        assert sent[0][2]["text"].startswith("*Capture Bot*")  # the Slack shape was used
        assert sent[1][1] == "ops@example.com"
        assert sent[1][2] == "[Capture Bot] Cap hit"

    def test_no_channel_configured_is_silent(self):
        from types import SimpleNamespace

        assert alerts.notify_note(SimpleNamespace(), "Cap hit", "body") == []

    def test_a_failing_channel_is_reported_never_raised(self, monkeypatch):
        from types import SimpleNamespace

        def boom(*args, **kwargs):
            raise RuntimeError("the webhook is down")

        monkeypatch.setattr(alerts, "send_webhook", boom)
        settings = SimpleNamespace(alert_webhook_url="https://hook.example.com")
        assert alerts.notify_note(settings, "Cap hit", "body") == [("webhook", False)]

    def test_an_unknown_kind_falls_back_to_generic(self):
        payload = alerts.build_note_payload("Cap hit", "body", "carrier-pigeon")
        assert payload["event"] == "notice"


class TestRouteList:
    """Parsing the per-URL webhook routing list."""

    def test_entries_are_split_on_semicolons(self):
        assert alerts.split_routes("a=https://x; b=https://y\nc=https://z") == [
            "a=https://x",
            "b=https://y",
            "c=https://z",
        ]

    def test_an_empty_list_is_empty(self):
        assert alerts.split_routes("") == []
        assert alerts.valid_routes("") == ()

    def test_a_malformed_entry_is_named(self):
        assert alerts.invalid_routes("a=https://x; b; ftp=ftp://y; =https://z") == [
            "b",
            "ftp=ftp://y",
            "=https://z",
        ]

    def test_parse_routes_raises_on_a_bad_entry(self):
        with pytest.raises(ValueError, match="fragment=http"):
            alerts.parse_routes("staging=ftp://x")

    def test_valid_routes_ignores_the_bad_ones(self):
        assert alerts.valid_routes("good=https://good; broken") == (("good", "https://good"),)

    def test_a_match_is_case_insensitive(self):
        routes = alerts.parse_routes("staging.example.com=https://hooks/x")
        assert alerts.route_for("https://STAGING.example.com/a", routes) == "https://hooks/x"

    def test_a_wildcard_is_the_catch_all(self):
        routes = alerts.parse_routes("staging=https://x; *=https://all")
        assert alerts.route_for("https://news.example.com", routes) == "https://all"

    def test_the_first_matching_rule_wins(self):
        routes = alerts.parse_routes("news=https://news; *news.example.com=https://host")
        assert alerts.route_for("https://news.example.com/1", routes) == "https://news"

    def test_nothing_matches_means_no_route(self):
        assert alerts.route_for("https://a.com", alerts.parse_routes("b=https://x")) == ""


class TestWebhookRouting:
    """One endpoint per host, and the default for everything unclaimed."""

    ITEMS = [
        {"url": "https://staging.example.com/a", "label": "staging", "diff": 0.3},
        {"url": "https://shop.example.com/b", "label": "shop", "diff": 0.2},
        {"url": "https://news.example.com/c", "label": "news", "diff": 0.1},
    ]

    def _settings(self, **kwargs):
        from types import SimpleNamespace

        base = {
            "alert_webhook_url": "https://default",
            "alert_webhook_kind": "generic",
            "alert_route_urls": "staging.example.com=https://staging-hook",
        }
        base.update(kwargs)
        return SimpleNamespace(**base)

    def test_each_host_gets_its_own_endpoint(self, monkeypatch):
        sent = []
        monkeypatch.setattr(
            alerts, "send_webhook", lambda url, payload, **kw: sent.append((url, payload)) or True
        )
        results = alerts.notify(self._settings(), self.ITEMS)
        targets = [url for url, _payload in sent]
        assert targets == ["https://staging-hook", "https://default"]
        assert [entry[0] for entry in results] == [
            "webhook[https://staging-hook]",
            "webhook[https://default]",
        ]
        assert len(sent[0][1]["items"]) == 1  # the staging page went alone
        assert len(sent[1][1]["items"]) == 2  # shop + news together

    def test_a_single_endpoint_keeps_the_plain_label(self, monkeypatch):
        monkeypatch.setattr(alerts, "send_webhook", lambda *a, **k: True)
        settings = self._settings(alert_route_urls="")
        assert alerts.notify(settings, self.ITEMS) == [("webhook", True)]

    def test_without_a_default_unclaimed_changes_are_webhook_silent(self, monkeypatch):
        sent = []
        monkeypatch.setattr(alerts, "send_webhook", lambda url, *a, **k: sent.append(url) or True)
        settings = self._settings(alert_webhook_url="")
        results = alerts.notify(settings, self.ITEMS)
        assert sent == ["https://staging-hook"]
        # One endpoint only, so the label stays the plain "webhook".
        assert results == [("webhook", True)]

    def test_a_wildcard_replaces_the_default(self, monkeypatch):
        sent = []
        monkeypatch.setattr(alerts, "send_webhook", lambda url, *a, **k: sent.append(url) or True)
        settings = self._settings(alert_route_urls="staging=https://s; *=https://all")
        alerts.notify(settings, self.ITEMS)
        assert sent == ["https://s", "https://all"]

    def test_the_email_stays_one_message(self, monkeypatch):
        sent = []
        monkeypatch.setattr(alerts, "send_webhook", lambda *a, **k: True)
        monkeypatch.setattr(
            alerts, "send_email", lambda *args, **kwargs: sent.append(args[6]) or True
        )
        settings = self._settings(alert_email_to="ops@example.com", smtp_host="smtp")
        results = alerts.notify(settings, self.ITEMS)
        assert len(sent) == 1
        assert "staging" in sent[0] and "shop" in sent[0] and "news" in sent[0]
        assert ("email", True) in results

    def test_a_broken_route_list_still_alerts(self, monkeypatch):
        sent = []
        monkeypatch.setattr(alerts, "send_webhook", lambda url, *a, **k: sent.append(url) or True)
        settings = self._settings(alert_route_urls="staging=ftp://oops; =nonsense")
        assert alerts.notify(settings, self.ITEMS) == [("webhook", True)]
        assert sent == ["https://default"]

    def test_the_enabled_via_engine_setting_is_read_tolerantly(self):
        assert alerts.valid_routes("a=https://x; broken; b=https://y") == (
            ("a", "https://x"),
            ("b", "https://y"),
        )
