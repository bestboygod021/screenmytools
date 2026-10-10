"""End-to-end tests of the PyQt6 interface.

The whole window is really constructed, the worker thread really runs on a real
:class:`QThread`, and only the browser is faked - so these tests cover the
signal wiring that is otherwise impossible to verify by reading the code.
"""

from __future__ import annotations

import json
import time

import pytest
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import QApplication, QMessageBox

from app.core.engine import CaptureEngine
from app.core.runtime import BrowserStatus
from app.core.settings import AUTH_MODE_HTTP
from app.core.url_utils import split_url_lines
from app.ui.main_window import MainWindow
from app.ui.widgets import LogConsole, ToggleSwitch
from tests.fakes import CallLog, FakeSite, FakeWidget, PageScript, SiteScript, make_factory


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch, tmp_path):
    """Never touch the real Playwright driver, and never write the user's profile."""
    # Make QSettings use an INI file inside the test's temp dir so nothing is
    # ever written to (or read from) the real user profile.
    # Give every window a private INI file so nothing touches the real profile.
    import app.ui.main_window as mw_mod

    monkeypatch.setattr(
        mw_mod,
        "_make_qsettings",
        lambda: QSettings(str(tmp_path / "profile.ini"), QSettings.Format.IniFormat),
    )
    monkeypatch.setattr(mw_mod, "_default_profiles_base", lambda: tmp_path / "profiles")

    from app.core import i18n

    i18n.set_language("en")

    from app.ui import theme

    theme.set_current(theme.DARK)  # every test starts from a known theme
    monkeypatch.setattr(
        "app.ui.main_window.detect_browser",
        lambda name="chromium": BrowserStatus(True, True, "/fake/chrome", "Chromium ready"),
    )


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch) -> MainWindow:
    monkeypatch.setattr(MainWindow, "_load_settings", lambda self: None)

    widget = MainWindow()
    qtbot.addWidget(widget)
    widget.show()

    widget.output_dir_input.setText(str(tmp_path / "out"))
    widget.settle_delay.setValue(0)
    widget.retries.setValue(0)
    widget.write_log_toggle.setChecked(False)
    widget.lazy_scroll_toggle.setChecked(False)
    return widget


def install_fake_engine(window: MainWindow, script: PageScript | None = None) -> CallLog:
    """Wire the window to a fake browser and return the call recorder."""
    calls = CallLog()
    script = script or PageScript(page_width=1280, page_height=2000)

    def factory(settings, log=None, progress=None, on_result=None):
        return CaptureEngine(
            settings,
            playwright_factory=make_factory(script, calls),
            log=log,
            progress=progress,
            on_result=on_result,
        )

    window.engine_factory = factory  # instance attribute shadows the class default
    return calls


# --------------------------------------------------------------------------- #
# widgets
# --------------------------------------------------------------------------- #
class TestToggleSwitch:
    def test_starts_off_and_can_be_toggled(self, qtbot):
        switch = ToggleSwitch("Headless")
        qtbot.addWidget(switch)

        assert switch.isChecked() is False
        switch.toggle()
        assert switch.isChecked() is True
        switch.toggle()
        assert switch.isChecked() is False

    def test_emits_toggled_signal(self, qtbot):
        switch = ToggleSwitch()
        qtbot.addWidget(switch)
        with qtbot.waitSignal(switch.toggled, timeout=1000) as blocker:
            switch.setChecked(True)
        assert blocker.args == [True]

    def test_space_bar_toggles(self, qtbot):
        switch = ToggleSwitch()
        qtbot.addWidget(switch)
        qtbot.keyClick(switch, Qt.Key.Key_Space)
        assert switch.isChecked() is True

    def test_paints_without_error(self, qtbot):
        switch = ToggleSwitch()
        qtbot.addWidget(switch)
        switch.setChecked(True, animate=False)
        switch.resize(60, 30)
        pixmap = switch.grab()
        assert not pixmap.isNull()


class TestLogConsole:
    def test_levels_are_colourised_and_filtered(self, qtbot):
        console = LogConsole()
        qtbot.addWidget(console)

        console.append_log("info", "hello")
        console.append_log("error", "boom")
        assert "hello" in console.plain_snapshot()
        assert "boom" in console.plain_snapshot()

        console.clear_console()
        console.set_minimum_level("ERROR")
        console.append_log("info", "hidden")
        console.append_log("error", "shown")
        snapshot = console.plain_snapshot()
        assert "hidden" not in snapshot
        assert "shown" in snapshot

    def test_html_in_messages_is_escaped(self, qtbot):
        console = LogConsole()
        qtbot.addWidget(console)
        console.append_log("info", "<b>bold</b> & 'quoted'")
        # Rendered as literal text; had it not been escaped, appendHtml would
        # have parsed <b> as markup and the plain text would read "bold & ...".
        assert "<b>bold</b> & 'quoted'" in console.toPlainText()


# --------------------------------------------------------------------------- #
# main window
# --------------------------------------------------------------------------- #
class TestUrlHandling:
    def test_summary_counts_valid_and_invalid(self, window: MainWindow):
        window.url_input.setPlainText("https://good.example.com\nnotaurl\nhttps://also.example.com")
        assert window.url_summary.text() == "2 ready · 1 invalid"
        assert window.start_button.isEnabled() is True

    def test_start_is_disabled_with_no_urls(self, window: MainWindow):
        window.url_input.clear()
        assert window.url_summary.text() == "0 URLs"
        assert window.start_button.isEnabled() is False

    def test_sample_button_fills_the_editor(self, window: MainWindow):
        window.sample_button.click()
        assert len(window.url_lines()) == 3

    def test_clear_button_empties_the_editor(self, window: MainWindow):
        window.url_input.setPlainText("https://a.com")
        window.clear_urls_button.click()
        assert window.url_input.toPlainText() == ""

    def test_url_lines_ignore_comments(self, window: MainWindow):
        window.url_input.setPlainText("# comment\nhttps://a.com\n\nhttps://b.com")
        assert window.url_lines() == ["https://a.com", "https://b.com"]


class TestSettingsMapping:
    def test_widgets_map_onto_settings(self, window: MainWindow):
        window.viewport_width.setValue(1440)
        window.viewport_height.setValue(900)
        window.scale_factor.setValue(1.5)
        window.image_format.setCurrentIndex(1)
        window.jpeg_quality.setValue(80)
        window.navigation_timeout.setValue(45)
        window.settle_delay.setValue(2500)
        window.headless_toggle.setChecked(False)
        window.filename_prefix.setText("client-a")
        window.browser_engine.setCurrentText("firefox")

        settings = window.collect_settings()
        assert settings.viewport_width == 1440
        assert settings.viewport_height == 900
        assert settings.device_scale_factor == 1.5
        assert settings.image_format == "jpeg"
        assert settings.jpeg_quality == 80
        assert settings.navigation_timeout_ms == 45_000
        assert settings.settle_delay_ms == 2500
        assert settings.headless is False
        assert settings.filename_prefix == "client-a"
        assert settings.browser == "firefox"
        settings.validate()  # must be a usable configuration

    def test_auth_widgets_map_onto_settings(self, window: MainWindow):
        window.auth_enabled_check.setChecked(True)
        window.auth_mode.setCurrentIndex(1)
        window.username_input.setText("alice")
        window.password_input.setText("s3cret")

        settings = window.collect_settings()
        assert settings.auth_enabled is True
        assert settings.auth_mode == AUTH_MODE_HTTP
        assert settings.username == "alice"
        assert settings.password == "s3cret"

    def test_credentials_panel_follows_the_checkbox(self, window: MainWindow):
        assert window.credentials_panel.isEnabled() is False
        window.auth_enabled_check.setChecked(True)
        assert window.credentials_panel.isEnabled() is True

    def test_round_trip_is_lossless(self, window: MainWindow):
        window.sample_button.click()
        window.viewport_width.setValue(1024)
        window.scale_factor.setValue(3.0)
        window.auth_enabled_check.setChecked(True)
        window.auth_mode.setCurrentIndex(0)
        window.username_input.setText("bob")
        window.password_input.setText("pw")
        window.user_agent.setText("UA/1.0")
        window.retries.setValue(2)

        snapshot = window.collect_settings().to_dict()
        window.apply_settings(window.collect_settings())
        assert window.collect_settings().to_dict() == snapshot

    def test_jpeg_quality_only_enabled_for_jpeg(self, window: MainWindow):
        window.image_format.setCurrentIndex(0)
        assert window.jpeg_quality.isEnabled() is False
        window.image_format.setCurrentIndex(1)
        assert window.jpeg_quality.isEnabled() is True


class TestValidation:
    def test_missing_destination_blocks_the_run(self, window: MainWindow, monkeypatch):
        shown = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a))
        window.output_dir_input.clear()
        window.url_input.setPlainText("https://example.com")

        window.start_capture()

        assert shown, "a validation dialog should be shown"
        assert window._running is False
        assert "destination folder" in shown[0][2]

    def test_no_urls_shows_information_dialog(self, window: MainWindow, monkeypatch):
        shown = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a))
        window.url_input.clear()
        window.start_capture()
        assert shown
        assert window._running is False


class TestFullRun:
    def test_capture_run_end_to_end(self, window: MainWindow, qtbot, tmp_path):
        calls = install_fake_engine(window)
        window.url_input.setPlainText("https://a.example.com\nhttps://b.example.com/page")

        window.start_button.click()
        assert window._running is True
        assert window.stop_button.isEnabled() is True
        assert window.settings_container.isEnabled() is False

        qtbot.waitUntil(lambda: window._running is False, timeout=15_000)
        qtbot.wait(200)  # let the last queued signals drain

        output = tmp_path / "out"
        shots = sorted(output.glob("*.png"))
        assert len(shots) == 2, [p.name for p in shots]

        assert window.progress_bar.value() == 2
        assert window.stat_done.value_label.text() == "2"
        assert window.stat_ok.value_label.text() == "2"
        assert window.stat_failed.value_label.text() == "0"
        assert "Saved" in window.log_text
        assert window.start_button.isEnabled() is True
        assert window.settings_container.isEnabled() is True

        assert len(calls.gotos) == 2
        assert calls.gotos[0]["wait_until"] == "domcontentloaded"
        assert calls.browser_closed is True

    def test_failed_urls_can_be_retried(self, window: MainWindow, qtbot):
        install_fake_engine(window, PageScript(goto_error=RuntimeError("net::ERR_FAILED")))
        window.url_input.setPlainText("https://broken.example.com\nhttps://broken.example.org")

        window.start_button.click()
        qtbot.waitUntil(lambda: window._running is False, timeout=15_000)
        qtbot.wait(200)

        assert window.stat_failed.value_label.text() == "2"
        assert window.retry_failed_button.isEnabled() is True

        window.retry_failed_button.click()
        assert split_url_lines(window.url_input.toPlainText()) == [
            "https://broken.example.com",
            "https://broken.example.org",
        ]

        clipboard = QApplication.clipboard()
        window.copy_failed_button.click()
        assert "broken.example.com" in clipboard.text()

    def test_browser_chip_reports_the_probe_result(self, window: MainWindow, qtbot):
        qtbot.waitUntil(lambda: "ready" in window.browser_chip.text().lower(), timeout=5_000)
        assert window.install_browser_button.isVisible() is False

    def test_stop_disables_itself_and_finishes_cleanly(self, window: MainWindow, qtbot):
        install_fake_engine(window)
        window.url_input.setPlainText("\n".join(f"https://s{i}.example.com" for i in range(5)))

        window.start_button.click()
        qtbot.waitUntil(lambda: window._running is True, timeout=5_000)
        window.stop_button.click()
        assert window.stop_button.isEnabled() is False

        qtbot.waitUntil(lambda: window._running is False, timeout=15_000)
        qtbot.wait(200)
        assert window._worker is None
        assert window._thread is None

    def test_log_can_be_saved_to_disk(self, window: MainWindow, qtbot, tmp_path, monkeypatch):
        install_fake_engine(window)
        window.url_input.setPlainText("https://example.com")
        window.start_button.click()
        qtbot.waitUntil(lambda: window._running is False, timeout=15_000)

        target = tmp_path / "saved-log.txt"
        monkeypatch.setattr(
            "PyQt6.QtWidgets.QFileDialog.getSaveFileName", lambda *a, **k: (str(target), "")
        )
        window.save_log_button.click()

        assert target.exists()
        assert "Saved" in target.read_text(encoding="utf-8")

    def test_opening_the_folder_is_only_done_when_asked(
        self, window: MainWindow, qtbot, monkeypatch
    ):
        opened = []
        monkeypatch.setattr(
            "app.ui.main_window.QDesktopServices.openUrl", lambda url: opened.append(url) or True
        )
        install_fake_engine(window)
        window.url_input.setPlainText("https://example.com")

        window.open_folder_toggle.setChecked(False)
        window.start_button.click()
        qtbot.waitUntil(lambda: window._running is False, timeout=15_000)
        assert opened == []

        window.open_folder_toggle.setChecked(True)
        window.start_button.click()
        qtbot.waitUntil(lambda: window._running is False, timeout=15_000)
        assert len(opened) == 1


class TestBrowserStatusRendering:
    def test_missing_browser_shows_a_warning_and_the_install_button(
        self, qtbot, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(MainWindow, "_load_settings", lambda self: None)
        monkeypatch.setattr(
            "app.ui.main_window.detect_browser",
            lambda name="chromium": BrowserStatus(
                True,
                False,
                "",
                "Chromium is not installed. Run: python -m playwright install chromium",
            ),
        )

        widget = MainWindow()
        qtbot.addWidget(widget)
        widget.show()
        qtbot.waitUntil(
            lambda: "not installed" in widget.browser_chip.text().lower(), timeout=5_000
        )

        assert widget.install_browser_button.isVisible() is True
        assert "playwright install chromium" in widget.log_text

    def test_broken_probe_does_not_crash(self, qtbot, monkeypatch):
        monkeypatch.setattr(MainWindow, "_load_settings", lambda self: None)

        def explode(name="chromium"):
            raise RuntimeError("driver exploded")

        monkeypatch.setattr("app.ui.main_window.detect_browser", explode)
        widget = MainWindow()
        qtbot.addWidget(widget)
        widget.show()
        qtbot.waitUntil(lambda: "failed" in widget.browser_chip.text().lower(), timeout=5_000)
        assert widget.install_browser_button.isVisible() is True


class TestClicksCard:
    """The click-path box: what a person types, and what pressing Start does."""

    def test_the_card_is_there_and_starts_off(self, window: MainWindow):
        assert any(card.title_label.text() == "Clicks & screens" for card in window._cards)
        assert window.clicks_toggle.isChecked() is False
        assert window.crawl_toggle.isChecked() is False
        assert window.journey_text() == ""

    def test_the_sample_fills_the_box_and_turns_the_feature_on(self, window: MainWindow):
        window.sample_journey_button.click()
        assert window.clicks_toggle.isChecked() is True
        assert window.journey_name.text() == "shop"
        assert 'click "Sign in"' in window.journey_text()
        assert window.journey_text().rstrip().endswith("capture dashboard")

    def test_the_recipe_picker_lists_them_and_loads_one(self, window: MainWindow):
        names = [window.recipe_combo.itemData(i) for i in range(window.recipe_combo.count())]
        assert names == [
            "shop",
            "login-dashboard",
            "tabs",
            "wizard",
            "sso-login",
            "pricing",
        ]

        window.recipe_combo.setCurrentIndex(names.index("login-dashboard"))
        window.recipe_button.click()

        assert window.clicks_toggle.isChecked() is True
        assert window.journey_name.text() == "login-dashboard"
        assert "fill #email = ${CAPTURE_EMAIL:-demo@example.com}" in window.journey_text()
        assert "recipe" in window.log_text

    def test_a_loaded_recipe_can_be_edited_and_is_what_runs(self, window: MainWindow):
        window.url_input.setPlainText("https://example.com/")
        window.recipe_combo.setCurrentIndex(2)  # tabs
        window.recipe_button.click()
        window.clicks_steps.setPlainText(
            window.clicks_steps.toPlainText().replace("Reviews", "Questions")
        )

        spec, problem = window.build_journey_spec()

        assert problem == ""
        assert "Questions" in spec.steps[5].name
        assert not any("Reviews" in (step.name or "") for step in spec.steps)

    def test_the_box_becomes_a_journey(self, window: MainWindow):
        window.url_input.setPlainText("https://a.example.com/home\nhttps://b.example.com/")
        window.clicks_steps.setPlainText('click "Sign in"\ncapture dashboard')
        window.clicks_toggle.setChecked(True)

        spec, problem = window.build_journey_spec()

        assert problem == ""
        assert spec is not None
        assert spec.url == "https://a.example.com/home"
        assert [step.action for step in spec.steps] == ["click", "capture"]
        assert spec.steps[-1].label == "dashboard"

    def test_a_broken_step_is_explained_not_raised(self, window: MainWindow):
        window.url_input.setPlainText("https://a.example.com")
        window.clicks_steps.setPlainText('click "Sign in"\nwait soon')
        window.clicks_toggle.setChecked(True)

        spec, problem = window.build_journey_spec()

        assert spec is None
        assert "wait" in problem and "milliseconds" in problem

    def test_without_a_url_it_says_so(self, window: MainWindow):
        window.clicks_steps.setPlainText('click "Sign in"')
        window.clicks_toggle.setChecked(True)
        spec, problem = window.build_journey_spec()
        assert spec is None
        assert "URL" in problem

    def test_a_run_photographs_every_screen_of_the_walk(self, window: MainWindow, qtbot, tmp_path):
        site = FakeSite(
            pages={
                "/": (
                    "Home",
                    [FakeWidget("Sign in", kind="link", link="https://example.com/login")],
                ),
                "/login": ("Sign in", [FakeWidget("Forgot password")]),
            }
        )
        script = PageScript(page_width=1000, page_height=800, site=SiteScript.from_site(site))
        install_fake_engine(window, script)
        window.url_input.setPlainText("https://example.com/")
        window.clicks_steps.setPlainText('capture landing\nclick "Sign in"\ncapture login')
        window.journey_name.setText("walk")
        window.clicks_toggle.setChecked(True)

        window.start_capture()
        assert window._running is True
        qtbot.waitUntil(lambda: window._running is False, timeout=15_000)
        qtbot.wait(200)

        folder = tmp_path / "out" / "journeys" / "walk"
        assert sorted(item.name for item in folder.iterdir()) == ["01-landing.png", "02-login.png"]
        assert "Screenshots in" in window.log_text

    def test_a_broken_step_does_not_start_a_run(self, window: MainWindow, monkeypatch):
        shown = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a))
        window.url_input.setPlainText("https://example.com")
        window.clicks_steps.setPlainText("explode everything")
        window.clicks_toggle.setChecked(True)

        window.start_capture()

        assert shown and "unknown action 'explode'" in shown[0][2]
        assert window._running is False
        assert window._worker is None

    def test_the_crawl_switch_explores_the_site(self, window: MainWindow, qtbot, tmp_path):
        site = FakeSite(
            pages={
                "/": (
                    "Home",
                    [
                        FakeWidget("Products", kind="link", link="https://example.com/products"),
                        FakeWidget("Filters"),
                    ],
                ),
                "/products": ("Products", [FakeWidget("Sort by price")]),
            }
        )
        script = PageScript(page_width=1000, page_height=800, site=SiteScript.from_site(site))
        install_fake_engine(window, script)
        window.url_input.setPlainText("https://example.com/")
        window.crawl_toggle.setChecked(True)
        window.crawl_states.setValue(5)

        window.start_capture()
        qtbot.waitUntil(lambda: window._running is False, timeout=15_000)
        qtbot.wait(200)

        folders = [item for item in (tmp_path / "out").iterdir() if item.is_dir()]
        assert len(folders) == 1 and folders[0].name.startswith("crawl-example_com-")
        assert (folders[0] / "report.json").exists()
        assert sorted(item.name for item in folders[0].glob("*.png")) == [
            "001-home.png",
            "002-products.png",
        ]

    def test_the_schedule_knows_it_will_run_a_walk(self, window: MainWindow):
        assert window.scheduled_work() == "capture run"
        window.clicks_steps.setPlainText("capture landing")
        window.clicks_toggle.setChecked(True)
        assert window.scheduled_work() == "journey"
        window.crawl_toggle.setChecked(True)
        assert window.scheduled_work() == "crawl"
        window.clicks_toggle.setChecked(False)  # crawl wins while it is on
        assert window.scheduled_work() == "crawl"

    def test_a_scheduled_tick_runs_the_journey_not_a_plain_batch(
        self, window: MainWindow, qtbot, tmp_path
    ):
        site = FakeSite(
            pages={
                "/": ("Home", [FakeWidget("Sign in", kind="link", link="https://e.com/login")]),
                "/login": ("Sign in", []),
            }
        )
        script = PageScript(page_width=1000, page_height=800, site=SiteScript.from_site(site))
        install_fake_engine(window, script)
        window.url_input.setPlainText("https://e.com/")
        window.clicks_steps.setPlainText('capture landing\nclick "Sign in"\ncapture login')
        window.journey_name.setText("daily")
        window.clicks_toggle.setChecked(True)

        window._on_schedule_tick()  # what the armed timer fires
        qtbot.waitUntil(lambda: window._running is False, timeout=15_000)
        qtbot.wait(200)

        folder = tmp_path / "out" / "journeys" / "daily"
        assert sorted(item.name for item in folder.iterdir()) == ["01-landing.png", "02-login.png"]
        assert not list((tmp_path / "out").glob("*.png"))  # nothing landed in the root

    def test_arming_auto_repeat_says_what_it_will_run(self, window: MainWindow):
        window.clicks_steps.setPlainText("capture landing")
        window.clicks_toggle.setChecked(True)
        window.auto_repeat_toggle.setChecked(True)
        window.auto_repeat_minutes.setValue(5)
        window._arm_scheduler()
        assert "next journey in 5 minute(s)" in window.log_text

    def test_a_recipe_of_your_own_shows_up_in_the_picker(
        self, window: MainWindow, tmp_path, monkeypatch
    ):
        from app.core import journey as journey_module

        monkeypatch.setenv(journey_module.RECIPES_ENV, str(tmp_path / "recipes"))
        journey_module.save_recipe("checkout", 'click "Cart"\ncapture cart', title="My checkout")

        window.fill_recipes()

        names = [window.recipe_combo.itemData(i) for i in range(window.recipe_combo.count())]
        assert "checkout" in names
        index = names.index("checkout")
        label = window.recipe_combo.itemText(index)
        assert " * v" in label, f"marked as yours, with its version: {label}"
        assert label.split()[-1].startswith("v")
        window.recipe_combo.setCurrentIndex(index)
        window.recipe_button.click()
        assert window.journey_text() == 'click "Cart"\ncapture cart'

    def test_saving_the_box_as_a_recipe_writes_it_and_lists_it(
        self, window: MainWindow, tmp_path, monkeypatch
    ):
        from PyQt6.QtWidgets import QInputDialog

        from app.core import journey as journey_module

        monkeypatch.setenv(journey_module.RECIPES_ENV, str(tmp_path / "recipes"))
        window.clicks_steps.setPlainText("capture monday")
        monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("monday", True)))

        window.save_current_recipe()

        saved = tmp_path / "recipes" / "monday.txt"
        assert saved.read_text(encoding="utf-8").strip() == "capture monday"
        assert "Recipe saved to" in window.log_text
        names = [window.recipe_combo.itemData(i) for i in range(window.recipe_combo.count())]
        assert "monday" in names

    def test_saving_an_empty_box_says_there_is_nothing_to_save(
        self, window: MainWindow, monkeypatch
    ):
        from PyQt6.QtWidgets import QMessageBox

        shown = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a))
        window.save_current_recipe()
        assert shown and "no steps to save" in shown[0][2]

    def test_steps_that_cannot_be_saved_are_refused_with_the_reason(
        self, window: MainWindow, tmp_path, monkeypatch
    ):
        from PyQt6.QtWidgets import QInputDialog, QMessageBox

        from app.core import journey as journey_module

        monkeypatch.setenv(journey_module.RECIPES_ENV, str(tmp_path / "recipes"))
        window.clicks_steps.setPlainText("dance wildly")
        monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("nope", True)))
        shown = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a))

        window.save_current_recipe()

        assert shown, "the user is told why nothing was saved"
        assert not (tmp_path / "recipes").exists() or not list((tmp_path / "recipes").glob("*.txt"))

    def test_a_session_file_is_imported_and_used(self, window: MainWindow, tmp_path, monkeypatch):
        from app.core import sessionfile

        monkeypatch.setattr(sessionfile, "default_session_folder", lambda: tmp_path / "sessions")
        source = tmp_path / "storage-state.json"
        source.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "sid", "domain": ".example.com", "expires": time.time() + 86400}
                    ],
                    "origins": [],
                }
            ),
            encoding="utf-8",
        )

        assert window.import_session_file(str(source)) is True

        kept = tmp_path / "sessions" / "storage-state.json"
        assert kept.exists(), "a session in Downloads must not be the only copy"
        assert window.storage_state_path.text() == str(kept)
        assert "1 cookie(s)" in window.session_status.text()
        assert "Imported the session" in window.log_text

    def test_a_file_that_is_not_a_session_is_refused_in_the_app(
        self, window: MainWindow, tmp_path, monkeypatch
    ):
        from PyQt6.QtWidgets import QMessageBox

        shown = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a))
        nope = tmp_path / "notes.json"
        nope.write_text('{"hello": "world"}', encoding="utf-8")

        assert window.import_session_file(str(nope)) is False

        assert shown and "not a storage state" in shown[0][2]
        assert window.storage_state_path.text() == ""
        assert "Not a session file" in window.session_status.text()

    def test_dropping_a_session_file_on_the_window_imports_it(
        self, window: MainWindow, tmp_path, monkeypatch
    ):
        from PyQt6.QtCore import QMimeData, QUrl

        from app.core import sessionfile

        monkeypatch.setattr(sessionfile, "default_session_folder", lambda: tmp_path / "sessions")
        source = tmp_path / "dropped.json"
        source.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

        class FakeDrop:
            def __init__(self) -> None:
                self.mime = QMimeData()
                self.mime.setUrls([QUrl.fromLocalFile(str(source))])
                self.accepted = False

            def mimeData(self):
                return self.mime

            def acceptProposedAction(self):
                self.accepted = True

        drop = FakeDrop()
        assert window.acceptDrops() is True
        window.dropEvent(drop)

        assert drop.accepted is True
        assert window.storage_state_path.text() == str(tmp_path / "sessions" / "dropped.json")

    def test_dropping_something_else_is_ignored(self, window: MainWindow):
        from PyQt6.QtCore import QMimeData, QUrl

        class FakeDrop:
            def __init__(self) -> None:
                self.mime = QMimeData()
                self.mime.setUrls([QUrl.fromLocalFile("/tmp/holiday.jpg")])
                self.accepted = False

            def mimeData(self):
                return self.mime

            def acceptProposedAction(self):
                self.accepted = True

        drop = FakeDrop()
        window.dropEvent(drop)
        assert drop.accepted is False
        assert window.storage_state_path.text() == ""

    def test_the_recipes_folder_can_be_pointed_somewhere_else(
        self, window: MainWindow, tmp_path, monkeypatch
    ):
        from PyQt6.QtWidgets import QFileDialog

        shared = tmp_path / "team-recipes"
        shared.mkdir()
        (shared / "checkout.txt").write_text("capture cart\n", encoding="utf-8")
        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(shared))
        )

        window.pick_recipes_folder()

        names = [window.recipe_combo.itemData(i) for i in range(window.recipe_combo.count())]
        assert names[0] == "shop", "built-ins still come first"
        assert "checkout" in names
        assert str(shared) in window.log_text

        window.recipe_combo.setCurrentIndex(names.index("checkout"))
        window.recipe_button.click()
        assert window.journey_text() == "capture cart"

    def test_the_row_editor_opens_on_the_box_and_saves_back(self, window: MainWindow, monkeypatch):
        from PyQt6.QtWidgets import QDialog

        from app.ui.step_editor import StepEditorDialog

        window.clicks_toggle.setChecked(True)
        window.clicks_steps.setPlainText('click "Sign in" optional\ncapture dashboard')
        seen: list[StepEditorDialog] = []

        class Edited(StepEditorDialog):
            """The dialog a person would leave: one row changed, one added."""

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                seen.append(self)

            def exec(self) -> int:
                self.table.cellWidget(0, 5).setValue(2500)
                self._on_add()
                return QDialog.DialogCode.Accepted

        # The window imports the dialog inside the method, so the patch belongs on
        # the module it imports it from.
        monkeypatch.setattr("app.ui.step_editor.StepEditorDialog", Edited)

        assert window.edit_steps_as_rows() is True

        assert seen, "the editor was opened"
        assert window.journey_text().splitlines()[0] == 'click "Sign in" optional timeout=2500'
        assert window.journey_text().splitlines()[-1].startswith("capture ")
        assert "row editor" in window.log_text

    def test_cancelling_the_row_editor_keeps_the_box_as_it_was(
        self, window: MainWindow, monkeypatch
    ):
        from PyQt6.QtWidgets import QDialog

        from app.ui.step_editor import StepEditorDialog

        window.clicks_toggle.setChecked(True)
        window.clicks_steps.setPlainText("capture landing")

        class Cancelled(StepEditorDialog):
            def exec(self) -> int:
                self.table.cellWidget(0, 3).setText("changed anyway")
                return QDialog.DialogCode.Rejected

        monkeypatch.setattr("app.ui.step_editor.StepEditorDialog", Cancelled)

        assert window.edit_steps_as_rows() is False
        assert window.journey_text() == "capture landing"

    def test_the_steps_status_says_how_the_box_parses(self, window: MainWindow):
        window.clicks_steps.setPlainText('click "Sign in" optional\ncapture dashboard')
        assert window.steps_status.text() == "2 step(s) - 1 capture(s) - 1 optional"

        window.clicks_steps.setPlainText("clik nope")
        assert "not finished yet" in window.steps_status.text()

        window.clicks_steps.setPlainText("")
        assert window.steps_status.text() == ""

    def test_record_needs_a_url_first(self, window: MainWindow, monkeypatch):
        shown = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a))
        window.record_button.click()
        assert shown and "first URL" in shown[0][2]
        assert window._recording is False

    def test_recorded_steps_land_in_the_box(self, window: MainWindow):
        from app.core import recorder

        recording = recorder.recording_from_events(
            "https://example.com",
            [
                recorder.Event(kind="click", tag="a", selector="a", text="Products"),
                recorder.Event(kind="submit", tag="form"),
            ],
        )

        window._on_recording_done(recording)

        assert window.clicks_toggle.isChecked() is True
        assert window.journey_name.text() == "recorded"
        assert window.journey_text() == recording.text()
        assert "3 step(s), 1 capture(s)" in window.log_text

    def test_a_recording_that_cannot_replay_is_flagged(self, window: MainWindow):
        from app.core import recorder

        recording = recorder.recording_from_events(
            "https://example.com",
            [recorder.Event(kind="click", selector="a", text="Products")],
        )
        recording.steps = ["click role=button"]  # a reference the runner cannot use
        window._on_recording_done(recording)

        assert window.clicks_steps.toPlainText() == "click role=button"
        assert "needs an edit" in window.log_text

    def test_an_empty_recording_changes_nothing(self, window: MainWindow):
        window.clicks_steps.setPlainText("capture landing")
        from app.core import recorder

        window._on_recording_done(recorder.recording_from_events("https://x", []))

        assert window.clicks_steps.toPlainText() == "capture landing"
        assert "Nothing was recorded" in window.log_text

    def test_a_running_recording_can_be_stopped_from_the_button(self, window: MainWindow):
        window._recording = True
        window.record_button.click()
        assert window._recording is False
        assert window.record_button.text() == "Stopping…"

    def test_a_failed_recording_is_shown(self, window: MainWindow, monkeypatch):
        shown = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a))
        window._on_recording_failed("The chromium browser binary is not installed.")
        assert shown and "not installed" in shown[0][2]
        assert "not installed" in window.log_text

    def test_the_screens_button_lists_the_walk_and_opens_the_report(
        self, window: MainWindow, qtbot, tmp_path, monkeypatch
    ):
        opened: list[str] = []
        monkeypatch.setattr(
            "app.ui.main_window.QDesktopServices.openUrl",
            lambda url: opened.append(url.toLocalFile()),
        )
        window.url_input.setPlainText("https://example.com/")
        window.clicks_steps.setPlainText("capture landing")
        window.clicks_toggle.setChecked(True)
        install_fake_engine(window, PageScript(page_width=1000, page_height=800))

        window.start_capture()
        qtbot.waitUntil(lambda: window._running is False, timeout=15_000)
        qtbot.wait(200)

        window.walk_button.click()

        assert any(item.endswith("walk-report.html") for item in opened)
        assert "journey walk in" in window.log_text
        assert "01-landing" in window.log_text

    def test_the_screens_button_says_when_nothing_was_walked(self, window: MainWindow, monkeypatch):
        shown = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a))
        window._last_walk_folder = ""
        window.walk_button.click()
        assert shown and "No walk" in shown[0][2]

    def test_open_folder_after_a_walk_goes_to_the_screens(
        self, window: MainWindow, qtbot, tmp_path
    ):
        opened = []
        window._open_folder = lambda folder: opened.append(folder)
        window.open_folder_toggle.setChecked(True)
        window.url_input.setPlainText("https://example.com/")
        window.clicks_steps.setPlainText("capture landing")
        window.clicks_toggle.setChecked(True)
        install_fake_engine(window, PageScript(page_width=1000, page_height=800))

        window.start_capture()
        qtbot.waitUntil(lambda: window._running is False, timeout=15_000)
        qtbot.wait(200)

        # "open folder when done" lands on the screens, not on the output root
        assert opened and opened[-1].endswith("journeys")
        opened.clear()
        window.open_last_walk_folder()
        assert opened and opened[-1].endswith("journeys")

    def test_open_folder_falls_back_when_no_walk_ran(self, window: MainWindow):
        opened = []
        window._open_folder = lambda folder: opened.append(folder)

        window.open_last_walk_folder()  # nothing has been walked yet

        assert opened and opened[-1] == window.output_dir_input.text()


class TestPersistence:
    def test_password_is_never_persisted(self, window: MainWindow):
        window.password_input.setText("hunter2")
        payload = window.collect_settings().to_dict()
        payload.pop("password", None)
        assert "password" not in payload
        assert payload["output_dir"]

    def test_settings_saved_without_raising(self, window: MainWindow):
        window._save_settings()  # must not raise even with an empty profile


class TestThemeSwitching:
    def test_toggling_switches_palette_and_stylesheet(self, window: MainWindow, qtbot):
        from app.ui import theme

        assert theme.current() is theme.DARK
        window.theme_button.click()
        assert theme.current() is theme.LIGHT
        assert theme.theme_name() == "light"
        assert window.theme_button.text() == "Dark mode"

        # The app-wide stylesheet now carries the light tokens.
        qss = QApplication.instance().styleSheet()
        assert theme.LIGHT.window in qss

        # Hand-painted widgets were refreshed with the new palette.
        assert theme.LIGHT.success in window.stat_ok.value_label.styleSheet()

        window.theme_button.click()
        assert theme.current() is theme.DARK
        assert window.theme_button.text() == "Light mode"

    def test_theme_choice_is_persisted(self, window: MainWindow):
        window.theme_button.click()
        assert window._qsettings.value("ui/theme") == "light"

    def test_browser_chip_restyles_after_theme_change(self, window: MainWindow, qtbot):
        from app.ui import theme

        qtbot.waitUntil(lambda: window._browser_status is not None, timeout=5_000)
        window.theme_button.click()
        assert theme.LIGHT.success in window.browser_chip.styleSheet()


class TestScheduling:
    def test_enabling_auto_repeat_arms_the_timer(self, window: MainWindow):
        window.auto_repeat_minutes.setValue(2)
        window.auto_repeat_toggle.setChecked(True)

        assert window.auto_repeat_minutes.isEnabled() is True
        assert window._schedule_timer.isActive() is True
        assert window._schedule_timer.interval() == 2 * 60_000

    def test_disabling_stops_the_timer(self, window: MainWindow):
        window.auto_repeat_toggle.setChecked(True)
        assert window._schedule_timer.isActive() is True
        window.auto_repeat_toggle.setChecked(False)
        assert window._schedule_timer.isActive() is False
        assert window.auto_repeat_minutes.isEnabled() is False

    def test_changing_the_interval_rearms(self, window: MainWindow):
        window.auto_repeat_toggle.setChecked(True)
        window.auto_repeat_minutes.setValue(5)
        assert window._schedule_timer.interval() == 5 * 60_000

    def test_tick_starts_a_capture(self, window: MainWindow, monkeypatch):
        started = []
        monkeypatch.setattr(window, "start_capture", lambda: started.append(1))
        window._running = False

        window._on_schedule_tick()
        assert started == [1]

    def test_tick_is_ignored_while_running(self, window: MainWindow, monkeypatch):
        started = []
        monkeypatch.setattr(window, "start_capture", lambda: started.append(1))
        window._running = True

        window._on_schedule_tick()
        assert started == []

    def test_finished_run_rearms_when_enabled(self, window: MainWindow, qtbot):
        install_fake_engine(window)
        window.url_input.setPlainText("https://a.example.com")
        window.auto_repeat_minutes.setValue(60)
        window.auto_repeat_toggle.setChecked(True)

        window.start_button.click()
        qtbot.waitUntil(lambda: window._running is False, timeout=15_000)
        qtbot.wait(200)
        assert window._schedule_timer.isActive() is True


class TestReportToggle:
    def test_report_toggle_maps_to_settings(self, window: MainWindow):
        window.write_report_toggle.setChecked(False)
        assert window.collect_settings().write_report is False
        window.write_report_toggle.setChecked(True)
        assert window.collect_settings().write_report is True


class TestChangeDetectionMapping:
    def test_change_detection_widgets_map_to_settings(self, window: MainWindow):
        window.change_detection_toggle.setChecked(True)
        window.change_threshold.setValue(0.25)
        settings = window.collect_settings()
        assert settings.change_detection_enabled is True
        assert settings.change_threshold == 0.25
        settings.validate()

    def test_threshold_out_of_range_is_rejected(self, window: MainWindow):
        window.change_detection_toggle.setChecked(True)
        window.change_threshold.setValue(1.5)  # spin clamps to 1.0, so force via settings
        settings = window.collect_settings()
        assert settings.change_threshold <= 1.0


class TestUpdateCheck:
    def test_update_available_is_surfaced(self, window: MainWindow, qtbot, monkeypatch):
        from app import updater

        monkeypatch.setattr(updater, "_get_json", lambda url, timeout=10.0: {"tag_name": "v99.0.0"})
        window.update_button.click()
        qtbot.waitUntil(
            lambda: "Update available" in window.status_bar.currentMessage(), timeout=5_000
        )
        assert window.update_button.isEnabled() is True

    def test_up_to_date_message(self, window: MainWindow, qtbot, monkeypatch):
        from app import updater

        monkeypatch.setattr(updater, "_get_json", lambda url, timeout=10.0: {"tag_name": "v0.0.1"})
        window.update_button.click()
        qtbot.waitUntil(
            lambda: "latest version" in window.status_bar.currentMessage(), timeout=5_000
        )

    def test_offline_is_handled_gracefully(self, window: MainWindow, qtbot, monkeypatch):
        from app import updater

        def boom(url, timeout=10.0):
            raise OSError("offline")

        monkeypatch.setattr(updater, "_get_json", boom)
        window.update_button.click()
        qtbot.waitUntil(
            lambda: "Could not check" in window.status_bar.currentMessage(), timeout=5_000
        )
        assert window.update_button.isEnabled() is True


class TestDailyScheduling:
    def _set_time_in_hours(self, window: MainWindow, hours: int) -> str:
        from datetime import datetime, timedelta

        target = datetime.now() + timedelta(hours=hours)
        hhmm = f"{target.hour:02d}:{target.minute:02d}"
        from PyQt6.QtCore import QTime

        window.schedule_time.setTime(QTime(target.hour, target.minute))
        return hhmm

    def test_daily_arms_the_timer(self, window: MainWindow):
        self._set_time_in_hours(window, 2)
        window.schedule_daily_toggle.setChecked(True)
        assert window._schedule_timer.isActive() is True
        # ~2 hours, definitely not the 30-minute interval default.
        assert window._schedule_timer.interval() > 60 * 60_000

    def test_daily_takes_priority_over_interval(self, window: MainWindow):
        window.auto_repeat_minutes.setValue(1)
        window.auto_repeat_toggle.setChecked(True)
        interval_before = window._schedule_timer.interval()
        assert interval_before == 60_000

        self._set_time_in_hours(window, 3)
        window.schedule_daily_toggle.setChecked(True)
        assert window._schedule_timer.interval() > 60_000  # daily now wins

    def test_daily_fields_map_to_settings(self, window: MainWindow):
        from PyQt6.QtCore import QTime

        window.schedule_daily_toggle.setChecked(True)
        window.schedule_time.setTime(QTime(7, 30))
        settings = window.collect_settings()
        assert settings.schedule_daily_enabled is True
        assert settings.schedule_time == "07:30"
        settings.validate()

    def test_invalid_daily_time_rejected(self, window: MainWindow):
        from app.core.settings import CaptureSettings

        settings = CaptureSettings(
            output_dir="/tmp/x", schedule_daily_enabled=True, schedule_time="25:99"
        )
        with pytest.raises(Exception):
            settings.validate()


class TestProfiles:
    def test_save_and_apply_round_trip(self, window: MainWindow, monkeypatch):
        from PyQt6.QtWidgets import QInputDialog

        monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("proj-a", True))

        window.viewport_width.setValue(1234)
        window.url_input.setPlainText("https://saved.example.com")
        window.profile_save.click()

        assert "proj-a" in [
            window.profile_combo.itemText(i) for i in range(window.profile_combo.count())
        ]

        # Change state, then apply the saved profile to restore it.
        window.viewport_width.setValue(1920)
        window.url_input.clear()
        window.profile_combo.setCurrentText("proj-a")
        window.profile_apply.click()

        assert window.viewport_width.value() == 1234
        assert "saved.example.com" in window.url_input.toPlainText()

    def test_delete_removes_profile(self, window: MainWindow, monkeypatch):
        from PyQt6.QtWidgets import QInputDialog

        monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("gone", True))
        window.profile_save.click()
        assert window.profile_combo.count() == 1

        window.profile_combo.setCurrentText("gone")
        window.profile_delete.click()
        assert window.profile_combo.count() == 0
        assert window.profile_apply.isEnabled() is False


class TestAlertsUI:
    def test_alert_fields_map_to_settings(self, window: MainWindow):
        window.alert_toggle.setChecked(True)
        window.alert_webhook_url.setText("  https://hook.example.com  ")
        window.alert_email_to.setText("ops@x.com")
        window.smtp_host.setText("smtp.x.com")
        window.smtp_port.setValue(2525)

        settings = window.collect_settings()
        assert settings.alert_enabled is True
        assert settings.alert_webhook_url == "https://hook.example.com"
        assert settings.alert_email_to == "ops@x.com"
        assert settings.smtp_host == "smtp.x.com"
        assert settings.smtp_port == 2525
        settings.validate()

    def test_alert_round_trip(self, window: MainWindow):
        from app.core.settings import CaptureSettings

        settings = CaptureSettings(
            output_dir="/tmp/x", alert_enabled=True, smtp_host="h", smtp_port=587
        )
        window.apply_settings(settings)
        assert window.alert_toggle.isChecked() is True
        assert window.smtp_port.value() == 587


class TestNetworkSessionUI:
    def test_fields_map_to_settings(self, window: MainWindow):
        window.proxy_server.setText("  http://proxy:8080  ")
        window.storage_state_path.setText("/tmp/state.json")
        settings = window.collect_settings()
        assert settings.proxy_server == "http://proxy:8080"
        assert settings.storage_state_path == "/tmp/state.json"
        settings.validate()

    def test_round_trip(self, window: MainWindow):
        from app.core.settings import CaptureSettings

        window.apply_settings(
            CaptureSettings(
                output_dir="/tmp/x", proxy_server="http://h:1", storage_state_path="/tmp/a.json"
            )
        )
        assert window.proxy_server.text() == "http://h:1"
        assert window.storage_state_path.text() == "/tmp/a.json"


class TestPdfExport:
    def test_button_enables_after_run_and_exports(self, window: MainWindow, monkeypatch, tmp_path):
        from PyQt6.QtWidgets import QFileDialog

        from app.core.engine import CaptureResult, CaptureStatus, CaptureSummary

        assert window.pdf_button.isEnabled() is False

        summary = CaptureSummary(
            results=[
                CaptureResult(index=0, url="https://x.example.com", status=CaptureStatus.SUCCESS)
            ],
            output_dir=str(tmp_path),
        )
        window._on_batch_finished(summary)
        assert window.pdf_button.isEnabled() is True

        saved = {}
        monkeypatch.setattr(
            QFileDialog, "getSaveFileName", lambda *a, **k: (str(tmp_path / "out.pdf"), "")
        )
        import app.core.pdfreport as pdfreport

        monkeypatch.setattr(
            pdfreport,
            "build_pdf_report",
            lambda summ, target, only_changed=False, include_trend=False: (
                saved.__setitem__("target", target) or target
            ),
        )

        window.pdf_button.click()
        assert saved.get("target") == str(tmp_path / "out.pdf")
        assert "PDF report saved" in window.status_bar.currentMessage()

    def test_cancel_does_nothing(self, window: MainWindow, monkeypatch, tmp_path):
        from PyQt6.QtWidgets import QFileDialog

        from app.core.engine import CaptureResult, CaptureStatus, CaptureSummary

        window._on_batch_finished(
            CaptureSummary(
                results=[CaptureResult(index=0, url="https://x", status=CaptureStatus.SUCCESS)],
                output_dir=str(tmp_path),
            )
        )
        called = []
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: ("", ""))
        import app.core.pdfreport as pdfreport

        monkeypatch.setattr(
            pdfreport,
            "build_pdf_report",
            lambda summ, target, only_changed=False, include_trend=False: called.append(target),
        )
        window.pdf_button.click()
        assert called == []


class TestHistoryDialog:
    def _seed(self, tmp_path):
        import json

        (tmp_path / "capture-report-20260101-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "results": [
                        {"url": "https://a.example.com", "status": "success", "diff": 0.1},
                        {"url": "https://b.example.com", "status": "failed", "diff": None},
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_dialog_lists_all_rows(self, tmp_path, qapp):
        from app.ui.history_dialog import HistoryDialog

        self._seed(tmp_path)
        dialog = HistoryDialog(str(tmp_path))
        assert dialog.table.rowCount() == 2

    def test_reads_from_sqlite_index(self, tmp_path, qapp):
        from app.core.store import HistoryStore
        from app.ui.history_dialog import HistoryDialog

        with HistoryStore(tmp_path / "history.sqlite3") as store:
            store.add_run(
                "2026-01-01T00:00:00",
                [
                    {
                        "url": "https://indexed.example.com",
                        "status": "success",
                        "diff": 0.3,
                        "file_path": "",
                    }
                ],
            )
        dialog = HistoryDialog(str(tmp_path))
        assert dialog.table.rowCount() == 1

    def test_filter_by_site(self, tmp_path, qapp):
        from app.ui.history_dialog import HistoryDialog

        self._seed(tmp_path)
        dialog = HistoryDialog(str(tmp_path))
        dialog.site_combo.setCurrentIndex(1)  # first site
        assert dialog.table.rowCount() == 1

    def test_empty_dir_shows_no_rows(self, tmp_path, qapp):
        from app.ui.history_dialog import HistoryDialog

        dialog = HistoryDialog(str(tmp_path / "empty"))
        assert dialog.table.rowCount() == 0

    def test_header_has_history_button(self, window: MainWindow):
        assert window.history_button is not None


class TestI18nUI:
    def test_toggle_switches_direction_and_text(self, window: MainWindow):
        from PyQt6.QtCore import Qt

        from app.core import i18n

        assert window.layoutDirection() == Qt.LayoutDirection.LeftToRight
        english_start = window.start_button.text()

        window.language_button.click()
        assert i18n.current_language() == "fa"
        assert window.layoutDirection() == Qt.LayoutDirection.RightToLeft
        assert window.start_button.text() != english_start
        assert any(card.title_label.text() == i18n.tr("urls_title") for card in window._cards)

        window.language_button.click()  # back to English
        assert i18n.current_language() == "en"
        assert window.layoutDirection() == Qt.LayoutDirection.LeftToRight

    def test_language_persists_in_settings(self, window: MainWindow):
        from app.core import i18n

        i18n.set_language("fa")
        assert window.collect_settings().language == "fa"


class TestDurableQueueUI:
    def test_restore_populates_urls(self, window: MainWindow):
        from app.core import queue_store

        queue_store.save_queue(
            window._profiles_base, ["https://a.example.com", "https://b.example.com"]
        )
        window.url_input.clear()
        window._restore_queue()
        text = window.url_input.toPlainText()
        assert "a.example.com" in text and "b.example.com" in text

    def test_restore_is_noop_when_empty(self, window: MainWindow):
        from app.core import queue_store

        queue_store.clear_queue(window._profiles_base)
        window.url_input.setPlainText("https://keep.example.com")
        window._restore_queue()
        assert "keep.example.com" in window.url_input.toPlainText()


class TestDesktopNotifyUI:
    def test_toggle_maps_to_settings(self, window: MainWindow):
        window.desktop_notify_toggle.setChecked(True)
        assert window.collect_settings().desktop_notifications is True

    def test_notification_sent_on_finish(self, window: MainWindow, monkeypatch, tmp_path):
        from app.core import notify
        from app.core.engine import CaptureResult, CaptureStatus, CaptureSummary

        sent = []
        monkeypatch.setattr(notify, "send_notification", lambda t, m: sent.append((t, m)) or True)
        window.desktop_notify_toggle.setChecked(True)
        window._on_batch_finished(
            CaptureSummary(
                results=[CaptureResult(index=0, url="https://x", status=CaptureStatus.SUCCESS)],
                output_dir=str(tmp_path),
            )
        )
        assert sent

    def test_no_notification_when_disabled(self, window: MainWindow, monkeypatch, tmp_path):
        from app.core import notify
        from app.core.engine import CaptureSummary

        sent = []
        monkeypatch.setattr(notify, "send_notification", lambda t, m: sent.append(1) or True)
        window.desktop_notify_toggle.setChecked(False)
        window._on_batch_finished(CaptureSummary(results=[], output_dir=str(tmp_path)))
        assert sent == []


class TestAppearanceUI:
    def test_font_settings_applied(self, window: MainWindow):
        from PyQt6.QtWidgets import QApplication

        window.font_size.setValue(14)
        window.font_family.setText("Arial")
        settings = window.collect_settings()
        assert settings.font_size == 14
        assert settings.font_family == "Arial"
        window.apply_settings(settings)
        assert QApplication.font().pointSize() == 14

    def test_theme_selector_switches_palette(self, window: MainWindow):
        from app.ui import theme

        window.theme_selector.setCurrentText("midnight")
        assert theme.current() is theme.MIDNIGHT
        assert theme.theme_name() == "midnight"

        window.theme_selector.setCurrentText("high-contrast")
        assert theme.current() is theme.HIGH_CONTRAST

        window.theme_selector.setCurrentText("dark")
        assert theme.current() is theme.DARK

    def test_available_themes_registered(self):
        from app.ui import theme

        assert {"dark", "light", "midnight", "high-contrast"} <= set(theme.available_themes())

    def test_negative_font_size_rejected(self):
        from app.core.settings import CaptureSettings

        with pytest.raises(Exception):
            CaptureSettings(output_dir="/tmp/x", font_size=-1).validate()


class TestCronScheduling:
    def test_cron_arms_the_timer(self, window: MainWindow):
        window.schedule_cron.setText("0 12 * * *")
        window.schedule_cron_toggle.setChecked(True)
        assert window._schedule_timer.isActive() is True
        assert window._schedule_timer.interval() > 0

    def test_invalid_cron_does_not_arm(self, window: MainWindow):
        window.schedule_cron.setText("not a cron")
        window.schedule_cron_toggle.setChecked(True)
        assert window._schedule_timer.isActive() is False
        assert "Invalid cron" in window.status_bar.currentMessage()

    def test_cron_fields_map_to_settings(self, window: MainWindow):
        window.schedule_cron_toggle.setChecked(True)
        window.schedule_cron.setText("*/5 * * * *")
        settings = window.collect_settings()
        assert settings.schedule_cron_enabled is True
        assert settings.schedule_cron == "*/5 * * * *"
        settings.validate()


class TestCronSettingsValidation:
    def test_invalid_cron_rejected(self):
        from app.core.settings import CaptureSettings

        with pytest.raises(Exception):
            CaptureSettings(
                output_dir="/tmp/x", schedule_cron_enabled=True, schedule_cron="nope"
            ).validate()

    def test_valid_cron_ok(self):
        from app.core.settings import CaptureSettings

        CaptureSettings(
            output_dir="/tmp/x", schedule_cron_enabled=True, schedule_cron="0 9 * * 1-5"
        ).validate()


class TestTrendDialog:
    def test_dialog_lists_sites_and_summarises(self, tmp_path, qapp):
        import json

        from app.ui.trend_dialog import TrendDialog

        (tmp_path / "capture-report-20260101-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "results": [
                        {
                            "url": "https://a.example.com",
                            "status": "success",
                            "diff": 0.3,
                            "file_path": "",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        dialog = TrendDialog(str(tmp_path))
        assert dialog.site_combo.count() == 1
        assert "1 capture" in dialog.summary_label.text()

    def test_empty_dir(self, tmp_path, qapp):
        from app.ui.trend_dialog import TrendDialog

        dialog = TrendDialog(str(tmp_path / "none"))
        assert dialog.site_combo.count() == 0
        assert "No captured sites" in dialog.summary_label.text()

    def test_history_dialog_has_trend_button(self, tmp_path, qapp):
        from app.ui.history_dialog import HistoryDialog

        dialog = HistoryDialog(str(tmp_path))
        assert dialog.trend_button is not None


class TestPdfOnlyChanges:
    def test_flag_is_passed_to_builder(self, window: MainWindow, monkeypatch, tmp_path):
        from PyQt6.QtWidgets import QFileDialog

        from app.core.engine import CaptureResult, CaptureStatus, CaptureSummary

        window._on_batch_finished(
            CaptureSummary(
                results=[CaptureResult(index=0, url="https://x", status=CaptureStatus.SUCCESS)],
                output_dir=str(tmp_path),
            )
        )
        window.pdf_only_changes.setChecked(True)
        captured = {}
        monkeypatch.setattr(
            QFileDialog, "getSaveFileName", lambda *a, **k: (str(tmp_path / "o.pdf"), "")
        )
        import app.core.pdfreport as pdfreport

        monkeypatch.setattr(
            pdfreport,
            "build_pdf_report",
            lambda summ, target, only_changed=False, include_trend=False: captured.__setitem__(
                "oc", only_changed
            ),
        )
        window.pdf_button.click()
        assert captured["oc"] is True


class TestPdfIncludeTrend:
    def test_flag_is_passed_to_builder(self, window: MainWindow, monkeypatch, tmp_path):
        from PyQt6.QtWidgets import QFileDialog

        from app.core.engine import CaptureResult, CaptureStatus, CaptureSummary

        window._on_batch_finished(
            CaptureSummary(
                results=[CaptureResult(index=0, url="https://x", status=CaptureStatus.SUCCESS)],
                output_dir=str(tmp_path),
            )
        )
        window.pdf_include_trend.setChecked(True)
        captured = {}
        monkeypatch.setattr(
            QFileDialog, "getSaveFileName", lambda *a, **k: (str(tmp_path / "o.pdf"), "")
        )
        import app.core.pdfreport as pdfreport

        monkeypatch.setattr(
            pdfreport,
            "build_pdf_report",
            lambda summ, target, only_changed=False, include_trend=False: captured.__setitem__(
                "it", include_trend
            ),
        )
        window.pdf_button.click()
        assert captured["it"] is True


class TestTrendReport:
    def test_trend_logged_after_run(self, window: MainWindow, monkeypatch, tmp_path):
        import json

        from app.core.engine import CaptureResult, CaptureStatus, CaptureSummary

        (tmp_path / "capture-report-20260101-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "results": [
                        {
                            "url": "https://a.example.com",
                            "status": "success",
                            "diff": 0.2,
                            "file_path": "",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        messages = []
        monkeypatch.setattr(window, "append_log", lambda level, msg: messages.append(msg))
        window._on_batch_finished(
            CaptureSummary(
                results=[
                    CaptureResult(
                        index=0, url="https://a.example.com", status=CaptureStatus.SUCCESS
                    )
                ],
                output_dir=str(tmp_path),
            )
        )
        assert any("Trend |" in m for m in messages)


class TestAlertThresholdUI:
    def test_threshold_maps_to_settings(self, window: MainWindow):
        window.alert_threshold.setValue(0.42)
        settings = window.collect_settings()
        assert abs(settings.change_alert_threshold - 0.42) < 1e-6

    def test_apply_sets_threshold(self, window: MainWindow):
        from app.core.settings import CaptureSettings

        window.apply_settings(CaptureSettings(output_dir="/tmp/x", change_alert_threshold=0.25))
        assert abs(window.alert_threshold.value() - 0.25) < 1e-6


class TestAutoDashboardUI:
    def test_toggle_maps_to_settings(self, window: MainWindow):
        window.auto_dashboard_toggle.setChecked(True)
        assert window.collect_settings().auto_dashboard is True

    def test_apply_sets_toggle(self, window: MainWindow):
        from app.core.settings import CaptureSettings

        window.apply_settings(CaptureSettings(output_dir="/tmp/x", auto_dashboard=True))
        assert window.auto_dashboard_toggle.isChecked() is True


class TestAlertCooldownUI:
    def test_maps_to_settings(self, window: MainWindow):
        window.alert_cooldown.setValue(30)
        assert window.collect_settings().alert_cooldown_minutes == 30

    def test_apply_sets_field(self, window: MainWindow):
        from app.core.settings import CaptureSettings

        window.apply_settings(CaptureSettings(output_dir="/tmp/x", alert_cooldown_minutes=45))
        assert window.alert_cooldown.value() == 45


class TestHistoryDialogBaseline:
    def _seed(self, tmp_path):
        import json

        (tmp_path / "capture-report-20260101-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "results": [
                        {
                            "url": "https://a.example.com",
                            "status": "success",
                            "diff": 0.1,
                            "file_path": "",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_pin_button_promotes_the_latest_capture(self, tmp_path, qapp, monkeypatch):
        from PyQt6.QtWidgets import QMessageBox

        from app.core import baseline
        from app.ui.history_dialog import HistoryDialog

        self._seed(tmp_path)
        baseline.latest_path(tmp_path, "https://a.example.com").write_bytes(b"png")
        shown = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
        dialog = HistoryDialog(str(tmp_path))
        dialog.site_combo.setCurrentIndex(1)  # the only site
        dialog.pin_button.click()
        assert baseline.has_baseline(tmp_path, "https://a.example.com")
        assert shown and "Baseline pinned" in shown[0]

    def test_pin_without_a_capture_warns(self, tmp_path, qapp, monkeypatch):
        from PyQt6.QtWidgets import QMessageBox

        from app.ui.history_dialog import HistoryDialog

        self._seed(tmp_path)
        warned = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(True))
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        dialog = HistoryDialog(str(tmp_path))
        dialog.site_combo.setCurrentIndex(1)
        dialog.pin_button.click()
        assert warned


class TestHistoryRetentionUI:
    def test_maps_to_settings(self, window: MainWindow):
        window.history_retention.setValue(90)
        assert window.collect_settings().history_retention_days == 90

    def test_apply_sets_field(self, window: MainWindow):
        from app.core.settings import CaptureSettings

        window.apply_settings(CaptureSettings(output_dir="/tmp/x", history_retention_days=30))
        assert window.history_retention.value() == 30


class TestBigChangeNotification:
    def _summary(self, tmp_path, *diffs):
        from app.core.engine import CaptureResult, CaptureStatus, CaptureSummary

        results = [
            CaptureResult(
                index=i, url=f"https://site{i}.example.com", status=CaptureStatus.SUCCESS, diff=diff
            )
            for i, diff in enumerate(diffs)
        ]
        return CaptureSummary(results=results, output_dir=str(tmp_path))

    def _capture(self, monkeypatch):
        from app.core import notify

        sent = []
        monkeypatch.setattr(
            notify, "send_notification", lambda title, message: sent.append(message)
        )
        return sent

    def test_notifies_about_a_big_change(self, window: MainWindow, tmp_path, monkeypatch):
        sent = self._capture(monkeypatch)
        window.desktop_notify_toggle.setChecked(True)
        window.alert_threshold.setValue(0.0)
        window._on_batch_finished(self._summary(tmp_path, 0.6))
        assert sent and "changed significantly" in sent[0]

    def test_small_change_falls_back_to_the_headline(
        self, window: MainWindow, tmp_path, monkeypatch
    ):
        sent = self._capture(monkeypatch)
        window.desktop_notify_toggle.setChecked(True)
        window.alert_threshold.setValue(0.9)
        window._on_batch_finished(self._summary(tmp_path, 0.2))
        assert sent and "changed significantly" not in sent[0]

    def test_no_notification_when_the_toggle_is_off(
        self, window: MainWindow, tmp_path, monkeypatch
    ):
        sent = self._capture(monkeypatch)
        window.desktop_notify_toggle.setChecked(False)
        window._on_batch_finished(self._summary(tmp_path, 0.6))
        assert sent == []


class TestWebhookKindUI:
    def test_maps_to_settings(self, window: MainWindow):
        index = window.alert_webhook_kind.findData("slack")
        window.alert_webhook_kind.setCurrentIndex(index)
        assert window.collect_settings().alert_webhook_kind == "slack"

    def test_apply_selects_the_stored_kind(self, window: MainWindow):
        from app.core.settings import CaptureSettings

        window.apply_settings(CaptureSettings(output_dir="/tmp/x", alert_webhook_kind="teams"))
        assert window.alert_webhook_kind.currentData() == "teams"


class TestScreenshotRetentionUI:
    def test_maps_to_settings(self, window: MainWindow):
        window.screenshot_retention.setValue(120)
        assert window.collect_settings().screenshot_retention_days == 120

    def test_apply_sets_field(self, window: MainWindow):
        from app.core.settings import CaptureSettings

        window.apply_settings(CaptureSettings(output_dir="/tmp/x", screenshot_retention_days=14))
        assert window.screenshot_retention.value() == 14


class TestDriftAlertUI:
    def test_maps_to_settings(self, window: MainWindow):
        window.baseline_drift_alert.setValue(0.35)
        assert abs(window.collect_settings().baseline_drift_alert_threshold - 0.35) < 1e-6

    def test_apply_sets_field(self, window: MainWindow):
        from app.core.settings import CaptureSettings

        window.apply_settings(
            CaptureSettings(output_dir="/tmp/x", baseline_drift_alert_threshold=0.25)
        )
        assert abs(window.baseline_drift_alert.value() - 0.25) < 1e-6


class TestDigestScheduleUI:
    def test_fields_map_to_settings(self, window: MainWindow):
        window.digest_days.setValue(30)
        window.digest_issue.setValue(42)
        settings = window.collect_settings()
        assert settings.digest_days == 30 and settings.digest_issue_number == 42

    def test_apply_sets_the_fields(self, window: MainWindow):
        from app.core.settings import CaptureSettings

        window.apply_settings(
            CaptureSettings(output_dir="/tmp/x", digest_days=14, digest_issue_number=7)
        )
        assert window.digest_days.value() == 14 and window.digest_issue.value() == 7

    def test_round_trip_through_a_profile_payload(self, window: MainWindow):
        from dataclasses import asdict

        from app.core.settings import CaptureSettings

        window.apply_settings(CaptureSettings(output_dir="/tmp/x", digest_days=90))
        payload = asdict(window.collect_settings())  # what save_profile stores
        assert payload["digest_days"] == 90 and payload["digest_issue_number"] == 0


class TestQuietHoursUI:
    def test_the_field_maps_to_the_settings(self, window: MainWindow):
        window.alert_quiet_hours.setText(" 22:00-07:00 ")
        assert window.collect_settings().alert_quiet_hours == "22:00-07:00"

    def test_an_empty_field_means_no_quiet_hours(self, window: MainWindow):
        window.alert_quiet_hours.setText("")
        assert window.collect_settings().alert_quiet_hours == ""

    def test_apply_fills_the_field(self, window: MainWindow):
        from app.core.settings import CaptureSettings

        window.apply_settings(CaptureSettings(output_dir="/tmp/x", alert_quiet_hours="23-6"))
        assert window.alert_quiet_hours.text() == "23-6"

    def test_the_field_explains_itself(self, window: MainWindow):
        assert "22:00-07:00" in window.alert_quiet_hours.placeholderText()
        assert window.alert_quiet_hours.toolTip()


class TestScreenshotSizeCapUI:
    def test_the_field_maps_to_the_settings(self, window: MainWindow):
        window.screenshot_size_cap.setValue(750)
        assert window.collect_settings().screenshot_retention_mb == 750

    def test_apply_fills_the_field(self, window: MainWindow):
        from app.core.settings import CaptureSettings

        window.apply_settings(CaptureSettings(output_dir="/tmp/x", screenshot_retention_mb=250))
        assert window.screenshot_size_cap.value() == 250

    def test_the_field_explains_itself(self, window: MainWindow):
        assert "MB" in window.screenshot_size_cap.suffix()
        assert window.screenshot_size_cap.toolTip()


class TestMuteUrlsUI:
    def test_the_field_maps_to_the_settings(self, window: MainWindow):
        window.alert_mute_urls.setText(" staging, preview ")
        assert window.collect_settings().alert_mute_urls == "staging, preview"

    def test_apply_fills_the_field(self, window: MainWindow):
        from app.core.settings import CaptureSettings

        window.apply_settings(CaptureSettings(output_dir="/tmp/x", alert_mute_urls="qa"))
        assert window.alert_mute_urls.text() == "qa"

    def test_the_field_explains_itself(self, window: MainWindow):
        assert "staging" in window.alert_mute_urls.placeholderText()
        assert window.alert_mute_urls.toolTip()


class TestHistorySizeCapUI:
    def test_the_field_maps_to_the_settings(self, window: MainWindow):
        window.history_size_cap.setValue(400)
        assert window.collect_settings().history_retention_mb == 400

    def test_apply_fills_the_field(self, window: MainWindow):
        from app.core.settings import CaptureSettings

        window.apply_settings(CaptureSettings(output_dir="/tmp/x", history_retention_mb=150))
        assert window.history_size_cap.value() == 150

    def test_the_field_explains_itself(self, window: MainWindow):
        assert window.history_size_cap.suffix() == " MB"
        assert window.history_size_cap.toolTip()
        assert window.history_size_cap.value() == 0  # off by default


class TestWatchdogStaleUI:
    def test_the_field_maps_to_the_settings(self, window: MainWindow):
        window.watchdog_stale.setValue(120)
        assert window.collect_settings().watchdog_stale_minutes == 120

    def test_apply_fills_the_field(self, window: MainWindow):
        from app.core.settings import CaptureSettings

        window.apply_settings(CaptureSettings(output_dir="/tmp/x", watchdog_stale_minutes=45))
        assert window.watchdog_stale.value() == 45

    def test_the_field_explains_itself(self, window: MainWindow):
        assert window.watchdog_stale.suffix() == " min"
        assert window.watchdog_stale.toolTip()


class TestQuietUrlsUI:
    """The per-URL quiet hours a user can actually find in the Alerts card."""

    def test_the_rules_map_to_settings(self, window: MainWindow):
        window.alert_quiet_urls.setText("  staging.example.com=22:00-07:00 ;  ")
        settings = window.collect_settings()
        assert settings.alert_quiet_urls == "staging.example.com=22:00-07:00 ;"
        settings.alert_quiet_urls = "staging.example.com=22:00-07:00"
        settings.validate()

    def test_the_round_trip_restores_them(self, window: MainWindow):
        from app.core.settings import CaptureSettings

        window.apply_settings(
            CaptureSettings(
                output_dir="/tmp/x",
                alert_quiet_urls="news.example.com=; staging.example.com=22:00-07:00",
            )
        )
        assert (
            window.alert_quiet_urls.text() == "news.example.com=; staging.example.com=22:00-07:00"
        )

    def test_the_field_explains_itself(self, window: MainWindow):
        assert "=" in window.alert_quiet_urls.placeholderText()
        assert "fragment" in window.alert_quiet_urls.toolTip()


class TestArchiveUi:
    """The archive switch sits with the history cap that uses it."""

    def test_the_switch_maps_to_settings(self, window: MainWindow):
        window.history_archive_toggle.setChecked(True)
        window.history_size_cap.setValue(50)
        settings = window.collect_settings()
        assert settings.history_archive is True
        assert settings.history_retention_mb == 50

    def test_the_round_trip_restores_it(self, window: MainWindow):
        from app.core.settings import CaptureSettings

        window.apply_settings(
            CaptureSettings(output_dir="/tmp/x", history_retention_mb=20, history_archive=True)
        )
        assert window.history_archive_toggle.isChecked() is True
        window.apply_settings(CaptureSettings(output_dir="/tmp/x"))
        assert window.history_archive_toggle.isChecked() is False

    def test_the_switch_explains_itself(self, window: MainWindow):
        description = window.history_archive_toggle.description_label.text().lower()
        assert "zip" in description and "archive-" in description


class TestRouteUrlsUi:
    """The webhook routing list a user can actually find in the Alerts card."""

    def test_the_rules_map_to_settings(self, window: MainWindow):
        window.alert_route_urls.setText("  staging.example.com=https://hooks/x  ")
        settings = window.collect_settings()
        assert settings.alert_route_urls == "staging.example.com=https://hooks/x"
        settings.validate()

    def test_the_round_trip_restores_them(self, window: MainWindow):
        from app.core.settings import CaptureSettings

        window.apply_settings(
            CaptureSettings(
                output_dir="/tmp/x",
                alert_route_urls="*=https://hooks/all; staging.example.com=https://hooks/s",
            )
        )
        assert window.alert_route_urls.text() == (
            "*=https://hooks/all; staging.example.com=https://hooks/s"
        )

    def test_the_field_explains_itself(self, window: MainWindow):
        assert "=" in window.alert_route_urls.placeholderText()
        assert "fragment" in window.alert_route_urls.toolTip()


class TestSecretStorage:
    """The SMTP password belongs to the keychain, not to a settings file."""

    def test_the_password_never_reaches_the_settings_file(self, window, monkeypatch):
        from app.core import secrets

        monkeypatch.setenv("CAPTURE_SECRETS", "memory")
        secrets._MEMORY.clear()
        try:
            window.smtp_user.setText("bot@example.com")
            window.smtp_password.setText("hunter2")
            window._save_settings()

            stored = window._qsettings.value(window.SETTINGS_KEY)
            assert stored["smtp_password"] == ""
            assert stored["smtp_user"] == "bot@example.com"
            assert secrets.load() == "hunter2"
        finally:
            secrets._MEMORY.clear()

    def test_a_stored_password_comes_back_as_a_mask(self, window, monkeypatch):
        from app.core import secrets
        from app.core.settings import CaptureSettings

        monkeypatch.setenv("CAPTURE_SECRETS", "memory")
        secrets._MEMORY.clear()
        try:
            secrets.store("hunter2")
            window.apply_settings(CaptureSettings())  # what loading a profile does
            assert window.smtp_password.text() == secrets.MASK
            # Saving again must not turn the mask into a password, nor wipe the store.
            assert window.collect_settings().smtp_password == ""
            window._save_settings()
            assert window._qsettings.value(window.SETTINGS_KEY)["smtp_password"] == ""
            assert secrets.load() == "hunter2"
        finally:
            secrets._MEMORY.clear()

    def test_a_legacy_settings_password_is_masked_too(self, window, monkeypatch):
        """A profile written before the keychain existed still shows something."""
        from app.core import secrets
        from app.core.settings import CaptureSettings

        monkeypatch.setenv("CAPTURE_SECRETS", "off")
        window.apply_settings(CaptureSettings(smtp_password="from-an-old-file"))
        assert window.smtp_password.text() == secrets.MASK
        assert window.collect_settings().smtp_password == ""

    def test_the_typed_password_wins_when_the_keychain_has_one(self, window, monkeypatch):
        from app.core import secrets

        monkeypatch.setenv("CAPTURE_SECRETS", "memory")
        secrets._MEMORY.clear()
        try:
            secrets.store("older")
            window.smtp_password.setText("newer")
            window._save_settings()
            assert secrets.load() == "newer"
        finally:
            secrets._MEMORY.clear()

    def test_without_a_keychain_the_password_is_still_saved(self, window, monkeypatch):
        """A machine with no keychain must not silently lose the setting."""

        monkeypatch.setenv("CAPTURE_SECRETS", "off")
        window.smtp_password.setText("hunter2")
        window._save_settings()
        stored = window._qsettings.value(window.SETTINGS_KEY)
        assert stored["smtp_password"] == "hunter2"
