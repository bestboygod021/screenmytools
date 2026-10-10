"""The SMTP password: out of the settings file, into the OS secret store."""

from __future__ import annotations

import json

import pytest

from app.core import secrets
from app.core.settings import CaptureSettings


@pytest.fixture
def memory_keychain(monkeypatch):
    """A keychain that behaves, without needing a desktop session."""
    monkeypatch.setenv("CAPTURE_SECRETS", "memory")
    secrets._MEMORY.clear()
    yield secrets
    secrets._MEMORY.clear()


class TestStore:
    """Store, read back, forget - the three things a password store does."""

    def test_without_a_store_nothing_pretends_to_work(self, monkeypatch):
        monkeypatch.setenv("CAPTURE_SECRETS", "off")
        assert secrets.available() is False
        assert secrets.backend_name() == ""
        assert secrets.store("hunter2") is False
        assert secrets.load() == ""
        assert secrets.forget() is False

    def test_a_stored_password_comes_back(self, memory_keychain):
        assert memory_keychain.available() is True
        assert memory_keychain.backend_name() == "the in-process test keychain"
        assert memory_keychain.store("hunter2") is True
        assert memory_keychain.load() == "hunter2"

    def test_the_same_service_is_used_for_every_account(self, memory_keychain):
        memory_keychain.store("one", account="smtp_password")
        memory_keychain.store("two", account="other")
        assert memory_keychain.load("smtp_password") == "one"
        assert memory_keychain.load("other") == "two"
        assert memory_keychain.SERVICE == "fullpage-capture-bot"

    def test_storing_nothing_forgets(self, memory_keychain):
        memory_keychain.store("hunter2")
        assert memory_keychain.store("") is True
        assert memory_keychain.load() == ""

    def test_forget_is_idempotent(self, memory_keychain):
        memory_keychain.store("hunter2")
        assert memory_keychain.forget() is True
        assert memory_keychain.forget() is False
        assert memory_keychain.load() == ""

    def test_the_mask_is_recognised(self):
        assert secrets.is_mask(secrets.MASK) is True
        assert secrets.is_mask(" ******** ") is True
        assert secrets.is_mask("hunter2") is False
        assert secrets.is_mask("") is False
        assert secrets.is_mask(None) is False


class TestResolution:
    """Which password a sender ends up using."""

    def test_the_settings_value_wins(self, memory_keychain):
        memory_keychain.store("from-the-store")
        settings = CaptureSettings(smtp_password="typed-by-hand")
        assert secrets.smtp_password(settings) == "typed-by-hand"

    def test_an_empty_setting_falls_back_to_the_store(self, memory_keychain):
        memory_keychain.store("from-the-store")
        assert secrets.smtp_password(CaptureSettings()) == "from-the-store"

    def test_the_mask_is_not_a_password(self, memory_keychain):
        memory_keychain.store("from-the-store")
        settings = CaptureSettings(smtp_password=secrets.MASK)
        assert secrets.smtp_password(settings) == "from-the-store"

    def test_without_a_store_an_empty_setting_stays_empty(self, monkeypatch):
        monkeypatch.setenv("CAPTURE_SECRETS", "off")
        assert secrets.smtp_password(CaptureSettings()) == ""

    def test_resolve_fills_the_object_in(self, memory_keychain):
        memory_keychain.store("from-the-store")
        settings = CaptureSettings(smtp_host="smtp.example.com")
        assert secrets.resolve(settings) is settings
        assert settings.smtp_password == "from-the-store"

    def test_resolve_leaves_a_typed_password_alone(self, memory_keychain):
        memory_keychain.store("from-the-store")
        settings = CaptureSettings(smtp_password="typed-by-hand")
        assert secrets.resolve(settings).smtp_password == "typed-by-hand"

    def test_resolve_tolerates_an_object_that_refuses(self, memory_keychain):
        memory_keychain.store("from-the-store")

        class Frozen:
            def __setattr__(self, name, value):  # noqa: N807
                raise AttributeError(name)

        frozen = Frozen()
        object.__setattr__(frozen, "smtp_password", "")
        assert secrets.resolve(frozen) is frozen


class TestTheSendersUseIt:
    """The three places that send mail all ask the same question."""

    def test_an_alert_email_gets_the_stored_password(self, memory_keychain, monkeypatch):
        from app.core import alerts

        memory_keychain.store("from-the-store")
        handed: list[tuple] = []
        monkeypatch.setattr(
            alerts, "send_email", lambda *args, **kwargs: handed.append(args) or True
        )
        settings = CaptureSettings(
            alert_email_to="ops@example.com", smtp_host="smtp.example.com", smtp_user="bot"
        )
        assert alerts.notify(settings, [{"url": "https://a", "label": "a", "diff": 0.4}])
        assert handed[0][3] == "from-the-store"

    def test_a_note_email_gets_the_stored_password(self, memory_keychain, monkeypatch):
        from app.core import alerts

        memory_keychain.store("from-the-store")
        handed: list[tuple] = []
        monkeypatch.setattr(
            alerts, "send_email", lambda *args, **kwargs: handed.append(args) or True
        )
        settings = CaptureSettings(
            alert_email_to="ops@example.com", smtp_host="smtp.example.com", smtp_user="bot"
        )
        assert alerts.notify_note(settings, "Backup finished", "All good.") == [("email", True)]
        assert handed[0][3] == "from-the-store"

    def test_a_channel_routing_falls_back_to_the_store(self, memory_keychain):
        from app.core import channels

        memory_keychain.store("from-the-store")
        channel = channels.Channel(name="team", kind="email", to=("ops@example.com",))
        host, port, user, password = channels._routing(CaptureSettings(), channel)
        assert (host, port, user, password) == ("", 587, "", "from-the-store")

    def test_a_channels_own_password_still_wins(self, memory_keychain):
        from app.core import channels

        memory_keychain.store("from-the-store")
        channel = channels.Channel(
            name="team",
            kind="email",
            to=("ops@example.com",),
            smtp={"smtp_host": "smtp.office.example", "smtp_password": "in-the-file"},
        )
        host, port, user, password = channels._routing(CaptureSettings(), channel)
        assert host == "smtp.office.example"
        assert password == "in-the-file"


class TestSecretsCli:
    """``secrets status|set|show|forget`` from a terminal or a script."""

    def test_status_reports_the_bad_news(self, monkeypatch, capsys):
        from app import cli

        monkeypatch.setenv("CAPTURE_SECRETS", "off")
        assert cli.main(["secrets", "status"]) == 1
        assert "No system keychain is reachable" in capsys.readouterr().out

    def test_status_reports_a_working_store(self, memory_keychain, capsys):
        from app import cli

        assert cli.main(["secrets", "status", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["available"] is True
        assert payload["stored"] is False
        assert payload["service"] == "fullpage-capture-bot"

    def test_set_then_show(self, memory_keychain, capsys):
        from app import cli

        assert cli.main(["secrets", "set", "--value", "hunter2"]) == 0
        assert "the in-process test keychain" in capsys.readouterr().out
        assert memory_keychain.load() == "hunter2"
        assert cli.main(["secrets", "show"]) == 0
        assert "An SMTP password is stored." in capsys.readouterr().out

    def test_show_never_prints_the_password(self, memory_keychain, capsys):
        from app import cli

        cli.main(["secrets", "set", "--value", "hunter2", "--json"])
        capsys.readouterr()
        cli.main(["secrets", "show", "--json"])
        assert "hunter2" not in capsys.readouterr().out

    def test_set_with_nothing_to_store_is_refused(self, memory_keychain, capsys):
        from app import cli

        assert cli.main(["secrets", "set"]) == 2
        assert "Nothing to store" in capsys.readouterr().err

    def test_set_without_a_store_is_refused(self, monkeypatch, capsys):
        from app import cli

        monkeypatch.setenv("CAPTURE_SECRETS", "off")
        assert cli.main(["secrets", "set", "--value", "hunter2"]) == 2
        assert "was not stored" in capsys.readouterr().err

    def test_forget(self, memory_keychain, capsys):
        from app import cli

        memory_keychain.store("hunter2")
        assert cli.main(["secrets", "forget"]) == 0
        assert "removed from the keychain" in capsys.readouterr().out
        assert memory_keychain.load() == ""

    def test_show_when_nothing_is_stored_is_a_failure(self, memory_keychain, capsys):
        from app import cli

        assert cli.main(["secrets", "show"]) == 1
        assert "No SMTP password is stored." in capsys.readouterr().out
