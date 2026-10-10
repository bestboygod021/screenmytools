"""Unit tests for :class:`app.core.settings.CaptureSettings`."""

from __future__ import annotations

import pytest

from app.core.settings import (
    AUTH_MODE_FORM,
    AUTH_MODE_HTTP,
    MAX_CANVAS_PX,
    CaptureSettings,
    SettingsError,
    default_settings,
)


@pytest.fixture
def base() -> CaptureSettings:
    settings = default_settings()
    settings.output_dir = "/tmp/out"
    return settings


class TestValidation:
    def test_defaults_are_valid(self, base: CaptureSettings):
        base.validate()  # must not raise

    def test_missing_output_dir(self, base: CaptureSettings):
        base.output_dir = "   "
        with pytest.raises(SettingsError, match="destination folder"):
            base.validate()

    @pytest.mark.parametrize(
        "field,value,match",
        [
            ("browser", "netscape", "Unknown browser"),
            ("image_format", "gif", "Image format must be"),
            ("jpeg_quality", 5, "between 10 and 100"),
            ("viewport_width", 100, "width must be"),
            ("viewport_height", 100, "height must be"),
            ("device_scale_factor", 8.0, "scale factor"),
            ("navigation_timeout_ms", 10, "Navigation timeout"),
            ("network_idle_timeout_ms", -1, "cannot be negative"),
            ("settle_delay_ms", -5, "cannot be negative"),
            ("retries", 99, "between 0 and 5"),
            ("max_concurrency", 0, "between 1 and 8"),
            ("max_concurrency", 9, "between 1 and 8"),
            ("lazy_scroll_step_px", 5, "at least 100 px"),
        ],
    )
    def test_out_of_range_values(self, base: CaptureSettings, field, value, match):
        setattr(base, field, value)
        with pytest.raises(SettingsError, match=match):
            base.validate()

    def test_auth_requires_username(self, base: CaptureSettings):
        base.auth_enabled = True
        base.username = ""
        with pytest.raises(SettingsError, match="no username"):
            base.validate()

    def test_auth_with_unknown_mode(self, base: CaptureSettings):
        base.auth_enabled = True
        base.username = "u"
        base.auth_mode = "kerberos"
        with pytest.raises(SettingsError, match="Authentication mode"):
            base.validate()

    def test_valid_auth_modes(self, base: CaptureSettings):
        for mode in (AUTH_MODE_FORM, AUTH_MODE_HTTP):
            base.auth_enabled = True
            base.auth_mode = mode
            base.username = "user"
            base.validate()


class TestDerivedValues:
    def test_browser_args_contain_quality_flags_and_extras(self, base: CaptureSettings):
        base.extra_browser_args = "--proxy-server=http://127.0.0.1:8080, --lang=en-GB"
        args = base.browser_args
        assert "--hide-scrollbars" in args
        assert "--force-color-profile=srgb" in args
        assert "--proxy-server=http://127.0.0.1:8080" in args
        assert "--lang=en-GB" in args

    def test_hide_scrollbars_can_be_disabled(self, base: CaptureSettings):
        base.hide_scrollbars = False
        assert "--hide-scrollbars" not in base.browser_args

    def test_file_extension(self, base: CaptureSettings):
        assert base.file_extension() == "png"
        base.image_format = "jpeg"
        assert base.file_extension() == "jpg"

    def test_canvas_limit_constant_is_sane(self):
        assert MAX_CANVAS_PX >= 8_192


class TestSerialisation:
    def test_round_trip(self, base: CaptureSettings):
        base.viewport_width = 1366
        base.device_scale_factor = 1.5
        base.headless = False
        clone = CaptureSettings.from_dict(base.to_dict())
        assert clone.to_dict() == base.to_dict()

    def test_qsettings_style_string_values_are_coerced(self):
        data = {
            "output_dir": "/tmp/x",
            "headless": "false",
            "viewport_width": "1440",
            "device_scale_factor": "1.25",
            "retries": "3",
            "auth_enabled": "True",
            "unknown_future_field": "ignored",
        }
        settings = CaptureSettings.from_dict(data)
        assert settings.headless is False
        assert settings.viewport_width == 1440
        assert settings.device_scale_factor == 1.25
        assert settings.retries == 3
        assert settings.auth_enabled is True
        assert not hasattr(settings, "unknown_future_field")

    def test_none_values_are_ignored(self):
        settings = CaptureSettings.from_dict({"output_dir": None, "headless": None})
        assert settings.output_dir == ""
        assert settings.headless is True

    def test_clone_is_independent(self, base: CaptureSettings):
        clone = base.clone()
        clone.output_dir = "/other"
        assert base.output_dir == "/tmp/out"


class TestChangeAlertThreshold:
    def test_default_is_zero(self):
        from app.core.settings import CaptureSettings

        assert CaptureSettings().change_alert_threshold == 0.0

    def test_out_of_range_rejected(self):
        import pytest

        from app.core.settings import CaptureSettings, SettingsError

        bad = CaptureSettings(
            output_dir="x",
            alert_enabled=True,
            alert_webhook_url="https://h",
            change_alert_threshold=1.5,
        )
        with pytest.raises(SettingsError):
            bad.validate()


class TestAlertCooldown:
    def test_negative_rejected(self):
        import pytest

        from app.core.settings import CaptureSettings, SettingsError

        bad = CaptureSettings(
            output_dir="x",
            alert_enabled=True,
            alert_webhook_url="https://h",
            alert_cooldown_minutes=-5,
        )
        with pytest.raises(SettingsError):
            bad.validate()


class TestHistoryRetention:
    def test_negative_rejected(self):
        import pytest

        from app.core.settings import CaptureSettings, SettingsError

        with pytest.raises(SettingsError):
            CaptureSettings(output_dir="x", history_retention_days=-1).validate()


class TestBaselineMaxAge:
    def test_negative_rejected(self):
        import pytest

        from app.core.settings import CaptureSettings, SettingsError

        with pytest.raises(SettingsError):
            CaptureSettings(output_dir="x", baseline_max_age_days=-1).validate()


class TestScreenshotRetention:
    def test_negative_rejected(self):
        import pytest

        from app.core.settings import CaptureSettings, SettingsError

        with pytest.raises(SettingsError):
            CaptureSettings(output_dir="x", screenshot_retention_days=-1).validate()


class TestBaselineDriftThreshold:
    def test_default_is_zero(self):
        from app.core.settings import CaptureSettings

        assert CaptureSettings().baseline_drift_alert_threshold == 0.0

    def test_out_of_range_rejected(self):
        import pytest

        from app.core.settings import CaptureSettings, SettingsError

        with pytest.raises(SettingsError):
            CaptureSettings(output_dir="x", baseline_drift_alert_threshold=1.5).validate()


class TestDigestSettings:
    def test_defaults(self):
        from app.core.settings import CaptureSettings

        settings = CaptureSettings()
        assert settings.digest_days == 7 and settings.digest_issue_number == 0

    def test_window_must_be_at_least_a_day(self):
        import pytest

        from app.core.settings import CaptureSettings, SettingsError

        with pytest.raises(SettingsError):
            CaptureSettings(output_dir="x", digest_days=0).validate()

    def test_issue_number_cannot_be_negative(self):
        import pytest

        from app.core.settings import CaptureSettings, SettingsError

        with pytest.raises(SettingsError):
            CaptureSettings(output_dir="x", digest_issue_number=-1).validate()


class TestQuietHoursSettings:
    def test_the_default_is_no_quiet_hours(self):
        from app.core.settings import CaptureSettings

        assert CaptureSettings().alert_quiet_hours == ""

    def test_a_window_is_accepted(self):
        from app.core.settings import CaptureSettings

        CaptureSettings(output_dir="x", alert_quiet_hours="22:00-07:00").validate()

    def test_an_empty_window_is_accepted(self):
        from app.core.settings import CaptureSettings

        CaptureSettings(output_dir="x", alert_quiet_hours="  ").validate()

    def test_a_broken_window_is_rejected(self):
        import pytest

        from app.core.settings import CaptureSettings, SettingsError

        with pytest.raises(SettingsError, match="22:00-07:00"):
            CaptureSettings(output_dir="x", alert_quiet_hours="overnight").validate()


class TestQuietHoursCliFlag:
    def test_the_flag_reaches_the_settings(self):
        from app import cli

        args = cli.build_parser().parse_args(
            ["https://a.com", "--out", "x", "--quiet-hours", "23:30-06:30"]
        )
        assert cli._settings_from_args(args).alert_quiet_hours == "23:30-06:30"

    def test_the_default_keeps_alerts_instant(self):
        from app import cli

        args = cli.build_parser().parse_args(["https://a.com", "--out", "x"])
        assert cli._settings_from_args(args).alert_quiet_hours == ""


class TestScreenshotSizeCapSettings:
    def test_the_default_is_no_cap(self):
        from app.core.settings import CaptureSettings

        assert CaptureSettings().screenshot_retention_mb == 0

    def test_a_cap_is_accepted(self):
        from app.core.settings import CaptureSettings

        CaptureSettings(output_dir="x", screenshot_retention_mb=500).validate()

    def test_a_negative_cap_is_rejected(self):
        import pytest

        from app.core.settings import CaptureSettings, SettingsError

        with pytest.raises(SettingsError, match="size cap"):
            CaptureSettings(output_dir="x", screenshot_retention_mb=-1).validate()


class TestMuteUrlsCliFlag:
    def test_the_flag_reaches_the_settings(self):
        from app import cli

        args = cli.build_parser().parse_args(
            ["https://a.com", "--out", "x", "--mute-urls", "staging,preview"]
        )
        assert cli._settings_from_args(args).alert_mute_urls == "staging,preview"

    def test_the_default_mutes_nothing(self):
        from app import cli

        args = cli.build_parser().parse_args(["https://a.com", "--out", "x"])
        assert cli._settings_from_args(args).alert_mute_urls == ""


class TestHistorySizeCapSettings:
    def test_the_default_is_off(self):
        from app.core.settings import CaptureSettings

        assert CaptureSettings().history_retention_mb == 0

    def test_a_cap_is_accepted(self):
        from app.core.settings import CaptureSettings

        CaptureSettings(output_dir="x", history_retention_mb=250).validate()

    def test_a_negative_cap_is_rejected(self):
        import pytest

        from app.core.settings import CaptureSettings, SettingsError

        with pytest.raises(SettingsError, match="History size cap"):
            CaptureSettings(output_dir="x", history_retention_mb=-1).validate()


class TestWatchdogAgeSettings:
    def test_the_default_is_off(self):
        from app.core.settings import CaptureSettings

        assert CaptureSettings().watchdog_stale_minutes == 0

    def test_a_limit_is_accepted(self):
        from app.core.settings import CaptureSettings

        CaptureSettings(output_dir="x", watchdog_stale_minutes=90).validate()

    def test_a_negative_limit_is_rejected(self):
        import pytest

        from app.core.settings import CaptureSettings, SettingsError

        with pytest.raises(SettingsError, match="Watchdog age"):
            CaptureSettings(output_dir="x", watchdog_stale_minutes=-5).validate()


class TestQuietHoursValidation:
    def test_a_day_span_is_accepted(self):
        from app.core.settings import CaptureSettings

        CaptureSettings(output_dir="x", alert_quiet_hours="fri18:00-mon09:00").validate()

    def test_several_windows_are_accepted(self):
        from app.core.settings import CaptureSettings

        CaptureSettings(
            output_dir="x", alert_quiet_hours="22:00-07:00, fri18:00-mon09:00"
        ).validate()

    def test_a_broken_item_names_itself(self):
        import pytest

        from app.core.settings import CaptureSettings, SettingsError

        with pytest.raises(SettingsError, match="garbage"):
            CaptureSettings(output_dir="x", alert_quiet_hours="22:00-07:00, garbage").validate()

    def test_bare_hours_still_work(self):
        from app.core.settings import CaptureSettings

        CaptureSettings(output_dir="x", alert_quiet_hours="23-6").validate()


class TestQuietUrlsSetting:
    """``alert_quiet_urls`` is validated where the user types it, not at 03:00."""

    def test_the_default_keeps_every_url_on_the_global_window(self):
        from app.core.settings import CaptureSettings

        assert CaptureSettings().alert_quiet_urls == ""

    def test_a_well_formed_list_passes(self):
        from app.core.settings import CaptureSettings

        CaptureSettings(
            output_dir="x",
            alert_quiet_urls="staging.example.com=22:00-07:00; news.example.com=",
        ).validate()

    def test_a_rule_without_an_equals_sign_names_itself(self):
        import pytest

        from app.core.settings import CaptureSettings, SettingsError

        with pytest.raises(SettingsError, match="Per-URL quiet hours must look like"):
            CaptureSettings(output_dir="x", alert_quiet_urls="staging.example.com").validate()

    def test_a_broken_window_is_reported_too(self):
        import pytest

        from app.core.settings import CaptureSettings, SettingsError

        with pytest.raises(SettingsError, match="99:99-07:00"):
            CaptureSettings(output_dir="x", alert_quiet_urls="s=99:99-07:00").validate()

    def test_an_empty_fragment_is_refused(self):
        import pytest

        from app.core.settings import CaptureSettings, SettingsError

        with pytest.raises(SettingsError, match="Per-URL quiet hours"):
            CaptureSettings(output_dir="x", alert_quiet_urls="=22:00-07:00").validate()


class TestHistoryArchiveSetting:
    """``history_archive`` decides whether a cap forgets a run or zips it."""

    def test_the_default_keeps_deleting(self):
        from app.core.settings import CaptureSettings

        assert CaptureSettings().history_archive is False

    def test_it_survives_a_round_trip(self):
        from app.core.settings import CaptureSettings

        clone = CaptureSettings.from_dict(CaptureSettings(history_archive=True).to_dict())
        assert clone.history_archive is True

    def test_a_string_from_qsettings_is_coerced(self):
        from app.core.settings import CaptureSettings

        assert CaptureSettings.from_dict({"history_archive": "True"}).history_archive is True
        assert CaptureSettings.from_dict({"history_archive": "false"}).history_archive is False


class TestRouteUrlsSetting:
    """``alert_route_urls`` is checked where it is typed, not at 03:00."""

    def test_the_default_routes_nothing(self):
        from app.core.settings import CaptureSettings

        assert CaptureSettings().alert_route_urls == ""

    def test_a_well_formed_list_passes(self):
        from app.core.settings import CaptureSettings

        CaptureSettings(
            output_dir="x",
            alert_route_urls=(
                "staging.example.com=https://hooks.example.com/staging; "
                "*=https://hooks.example.com/all"
            ),
        ).validate()

    def test_a_relative_target_names_itself(self):
        from app.core.settings import CaptureSettings, SettingsError

        with pytest.raises(SettingsError, match="Alert routes must look like"):
            CaptureSettings(output_dir="x", alert_route_urls="staging=/hooks/x").validate()

    def test_an_entry_without_a_target_names_itself(self):
        from app.core.settings import CaptureSettings, SettingsError

        with pytest.raises(SettingsError, match="staging.example.com"):
            CaptureSettings(
                output_dir="x", alert_route_urls="ok=https://x; staging.example.com"
            ).validate()


class TestSiteCapsSetting:
    """``site_caps`` is text, so it is validated at the door."""

    @staticmethod
    def _settings(tmp_path, spec):
        return CaptureSettings(output_dir=str(tmp_path), site_caps=spec)

    def test_a_good_spec_passes(self, tmp_path):
        self._settings(tmp_path, "news.example.com=500, *=1000").validate()  # must not raise

    def test_an_empty_spec_is_fine(self, tmp_path):
        self._settings(tmp_path, "").validate()

    def test_a_misspelled_size_is_refused(self, tmp_path):
        with pytest.raises(SettingsError) as excinfo:
            self._settings(tmp_path, "news.example.com=huge").validate()
        assert "site_caps expects 'host=MB' pairs" in str(excinfo.value)
        assert "huge" in str(excinfo.value)

    def test_a_zero_cap_is_refused(self, tmp_path):
        with pytest.raises(SettingsError):
            self._settings(tmp_path, "news.example.com=0").validate()

    def test_the_field_travels_through_a_profile(self):
        settings = CaptureSettings(site_caps="*=500")
        assert CaptureSettings.from_dict(settings.to_dict()).site_caps == "*=500"
