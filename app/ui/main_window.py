"""Main application window.

Responsibilities, in order of importance:

1. Collect a valid :class:`~app.core.settings.CaptureSettings` from the user.
2. Hand it to :class:`~app.worker.CaptureWorker` on a background thread.
3. Render the worker's signals. Nothing heavy ever happens on this thread.
"""

from __future__ import annotations

import time
import webbrowser
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import (
    QSettings,
    QSize,
    Qt,
    QThread,
    QTime,
    QTimer,
    QUrl,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import QDesktopServices, QIcon
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from app import updater
from app.core import history, i18n, profiles, queue_store, schedule
from app.core.engine import (
    BrowserNotInstalledError,
    CaptureEngine,
    CaptureResult,
    CaptureStatus,
    CaptureSummary,
    LogLevel,
)
from app.core.runtime import BrowserStatus, detect_browser
from app.core.settings import (
    AUTH_MODE_FORM,
    AUTH_MODE_HTTP,
    SUPPORTED_BROWSERS,
    CaptureSettings,
    SettingsError,
    default_settings,
)
from app.core.url_utils import build_url_label, split_url_lines, validate_url
from app.ui.theme import (
    DARK,
    LIGHT,
    available_themes,
    current,
    set_by_name,
    set_current,
    theme_name,
)
from app.ui.theme import (
    apply_theme as apply_app_theme,
)
from app.ui.widgets import (
    Card,
    CollapsibleSection,
    LogConsole,
    PasswordField,
    StatChip,
    ToggleRow,
    ghost_button,
)
from app.version import APP_DISPLAY_NAME, APP_NAME, ORG_NAME, __version__
from app.worker import CaptureWorker, RecordingWorker

SAMPLE_URLS = """https://example.com
https://www.wikipedia.org
https://news.ycombinator.com
"""


def _make_qsettings() -> QSettings:
    """Create the profile store. Overridden in tests for isolation."""
    return QSettings(ORG_NAME, APP_NAME)


def _default_profiles_base() -> Path:
    """Where profile JSON files live. Overridden in tests for isolation."""
    from PyQt6.QtCore import QStandardPaths  # local import keeps the top light

    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
    return Path(location or ".")


class _FnThread(QThread):
    """Runs one callable off the GUI thread and emits its return value."""

    done = pyqtSignal(object)

    def __init__(self, fn, parent=None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:  # noqa: D102 - QThread entry point
        try:
            payload: object = self._fn()
        except Exception as exc:  # pragma: no cover - probe must never crash the app
            payload = exc
        self.done.emit(payload)


class MainWindow(QWidget):
    """The single window of the application."""

    #: Injected by tests to swap the engine for a fake one.
    engine_factory = staticmethod(CaptureEngine)

    SETTINGS_KEY = "capture/settings"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._worker: CaptureWorker | None = None
        self._thread: QThread | None = None
        self._probe: _FnThread | None = None
        self._running = False
        self._run_started_at: float | None = None
        self._last_summary: CaptureSummary | None = None
        self._ok_count = 0
        self._failed_count = 0
        self._last_failed: list[str] = []

        self._qsettings = _make_qsettings()
        self._profiles_base = _default_profiles_base()

        # Restore the persisted theme *before* any widget is painted.
        self._restore_theme()

        self._build_ui()
        self._connect_signals()

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)

        self._schedule_timer = QTimer(self)
        self._schedule_timer.setSingleShot(True)
        self._schedule_timer.timeout.connect(self._on_schedule_tick)

        # Widgets whose enabled-state depends on another widget must be put in
        # their correct initial state here, before any signal can fire.
        self._on_format_changed()
        self._load_settings()
        self._apply_language()
        self._refresh_url_summary()
        self._refresh_profiles()
        self._restore_queue()

        self._start_browser_probe()

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        self.setObjectName("MainWindow")
        # Dropping a storage_state.json on the window imports it (see dropEvent).
        self.setAcceptDrops(True)
        self.setWindowTitle(APP_DISPLAY_NAME)
        self.resize(1280, 820)
        self.setMinimumSize(QSize(1080, 700))

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(14)

        root.addLayout(self._build_header())

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setHandleWidth(14)
        splitter.addWidget(self._build_settings_column())
        splitter.addWidget(self._build_run_column())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([470, 760])
        root.addWidget(splitter, 1)

        root.addWidget(self._build_action_bar())

        self.status_bar = QStatusBar(self)
        self.status_bar.setSizeGripEnabled(False)
        self.status_bar.showMessage("Ready.")
        root.addWidget(self.status_bar)

    def _build_header(self) -> QHBoxLayout:
        header = QHBoxLayout()
        header.setSpacing(12)

        mark = QLabel("\U0001f4f8", self)
        mark.setStyleSheet("font-size: 26px; background: transparent; border: none;")
        header.addWidget(mark, 0, Qt.AlignmentFlag.AlignVCenter)

        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.app_title_label = QLabel(APP_NAME, self)
        self.app_title_label.setObjectName("AppTitle")
        self.app_subtitle_label = QLabel(i18n.tr("app_subtitle"), self)
        self.app_subtitle_label.setObjectName("AppSubtitle")
        titles.addWidget(self.app_title_label)
        titles.addWidget(self.app_subtitle_label)
        header.addLayout(titles)
        header.addStretch(1)

        self.browser_chip = QLabel("Checking browser...", self)
        self.browser_chip.setObjectName("MutedLabel")
        self._browser_status: BrowserStatus | None = None
        self._restyle_browser_chip()
        header.addWidget(self.browser_chip, 0, Qt.AlignmentFlag.AlignVCenter)

        self.install_browser_button = ghost_button(
            "Install browser", "Download the Playwright browser binaries"
        )
        self.install_browser_button.setVisible(False)
        header.addWidget(self.install_browser_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self.theme_button = ghost_button("Light mode", "Switch between the dark and light themes")
        self.theme_button.setObjectName("ThemeButton")
        self.theme_button.clicked.connect(self._toggle_theme)
        header.addWidget(self.theme_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self.update_button = ghost_button(
            "Check updates", "Check GitHub Releases for a newer version"
        )
        self.update_button.clicked.connect(self._check_updates)
        header.addWidget(self.update_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self.walk_button = ghost_button(
            "Screens", "See the last journey/crawl: every screen, its clicks and a thumbnail page"
        )
        self.walk_button.clicked.connect(self._open_walk_report)
        header.addWidget(self.walk_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self.history_button = ghost_button("History", "Show the per-site capture/change timeline")
        self.history_button.clicked.connect(self._open_history)
        header.addWidget(self.history_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self.language_button = ghost_button("Language", "Switch the interface language (EN / FA)")
        self.language_button.clicked.connect(self._toggle_language)
        header.addWidget(self.language_button, 0, Qt.AlignmentFlag.AlignVCenter)

        return header

    # -- left column ----------------------------------------------------- #
    def _build_settings_column(self) -> QWidget:
        scroll = QScrollArea(self)
        scroll.setObjectName("SettingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setObjectName("SettingsViewport")

        container = QWidget()
        container.setObjectName("SettingsContainer")
        column = QVBoxLayout(container)
        column.setContentsMargins(0, 0, 6, 0)
        column.setSpacing(12)

        self._recording = False
        self._cards = []
        for builder, key in (
            (self._build_urls_card, "urls_title"),
            (self._build_clicks_card, "clicks_title"),
            (self._build_profiles_card, "profiles_title"),
            (self._build_alerts_card, "alerts_title"),
            (self._build_network_card, "network_title"),
            (self._build_destination_card, "destination_title"),
            (self._build_capture_options_card, "options_title"),
            (self._build_credentials_card, "credentials_title"),
            (self._build_advanced_card, "advanced_title"),
        ):
            card = builder()
            card.title_key = key
            self._cards.append(card)
            column.addWidget(card)
        column.addStretch(1)

        self.settings_container = container
        scroll.setWidget(container)
        return scroll

    def _build_urls_card(self) -> Card:
        card = Card(
            "Target URLs",
            "One URL per line. Blank lines and lines starting with # are ignored.",
            badge="1",
        )

        self.url_input = QPlainTextEdit(card)
        self.url_input.setPlaceholderText(
            "https://example.com\nhttps://example.com/pricing\n# this line is a comment"
        )
        self.url_input.setMinimumHeight(150)
        self.url_input.setStyleSheet(
            "QPlainTextEdit { font-family: Cascadia Mono, Consolas, 'DejaVu Sans Mono', monospace; font-size: 12px; }"
        )
        card.addWidget(self.url_input)

        footer = QHBoxLayout()
        footer.setSpacing(6)
        self.url_summary = QLabel("0 URLs", card)
        self.url_summary.setObjectName("MutedLabel")
        footer.addWidget(self.url_summary)
        footer.addStretch(1)

        self.sample_button = ghost_button("Sample", "Insert a few demo URLs", card)
        self.paste_button = ghost_button(
            "Paste", "Replace the list with the clipboard contents", card
        )
        self.clear_urls_button = ghost_button("Clear", "Empty the list", card)
        for button in (self.sample_button, self.paste_button, self.clear_urls_button):
            footer.addWidget(button)
        card.addLayout(footer)
        return card

    def _build_network_card(self) -> Card:
        card = Card(
            "Network & session",
            "Route traffic through a proxy and reuse a saved login session.",
            badge="N",
        )
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)

        self.proxy_server = QLineEdit(card)
        self.proxy_server.setPlaceholderText("http://host:8080")
        self.storage_state_path = QLineEdit(card)
        self.storage_state_path.setPlaceholderText("path/to/storage_state.json")
        self.storage_browse = QPushButton("Browse...", card)
        self.storage_browse.setCursor(Qt.CursorShape.PointingHandCursor)
        self.session_import_button = QPushButton("Import session...", card)
        self.session_import_button.setObjectName("GhostButton")
        self.session_import_button.setToolTip(
            "Pick a storage_state.json (exported by Playwright or a browser "
            "extension): it is checked, copied into the app's own folder, and used "
            "for every run from now on. You can also drop the file on this window."
        )
        self.session_import_button.clicked.connect(self.import_session_file)
        self.session_status = QLabel("", card)
        self.session_status.setObjectName("CardSubtitle")
        self.session_status.setWordWrap(True)

        grid.addWidget(self._field_label("Proxy server", card), 0, 0)
        grid.addWidget(self.proxy_server, 0, 1)
        grid.addWidget(self._field_label("storage_state", card), 1, 0)
        grid.addWidget(self.storage_state_path, 1, 1)
        grid.addWidget(self.storage_browse, 1, 2)
        grid.addWidget(self.session_import_button, 2, 1)
        grid.addWidget(self.session_status, 3, 0, 1, 3)
        card.addLayout(grid)
        return card

    def _build_alerts_card(self) -> Card:
        card = Card(
            "Change alerts",
            "Notify a webhook or send an email when a monitored page changes.",
            badge="A",
        )
        self.alert_toggle = ToggleRow(
            "Send alerts on change",
            "After a run, alert for every page whose diff met the threshold.",
            card,
        )
        card.addWidget(self.alert_toggle)

        self.desktop_notify_toggle = ToggleRow(
            "Desktop notification",
            "Show an operating-system notification when a run finishes.",
            card,
        )
        card.addWidget(self.desktop_notify_toggle)

        self.auto_dashboard_toggle = ToggleRow(
            "Auto dashboard",
            "Regenerate dashboard.html in the output folder after every run.",
            card,
        )
        card.addWidget(self.auto_dashboard_toggle)

        grid = QGridLayout()
        grid.setColumnStretch(1, 1)

        self.alert_webhook_url = QLineEdit(card)
        self.alert_webhook_url.setPlaceholderText("https://hooks.example.com/...")
        self.alert_webhook_kind = QComboBox(card)
        self.alert_webhook_kind.addItem("Generic JSON", "generic")
        self.alert_webhook_kind.addItem("Slack", "slack")
        self.alert_webhook_kind.addItem("Teams", "teams")
        self.alert_webhook_kind.setToolTip("Payload shape the webhook URL expects.")
        self.alert_email_to = QLineEdit(card)
        self.alert_email_to.setPlaceholderText("you@company.com")
        self.smtp_host = QLineEdit(card)
        self.smtp_port = QSpinBox(card)
        self.smtp_port.setRange(1, 65535)
        self.smtp_port.setValue(587)
        self.smtp_user = QLineEdit(card)
        self.smtp_password = QLineEdit(card)
        self.smtp_password.setEchoMode(QLineEdit.EchoMode.Password)
        from app.core import secrets

        store = secrets.backend_name()
        self.smtp_password.setToolTip(
            f"The password is kept in the system keychain ({store}), not in the settings "
            "file, so the settings stay safe to copy or back up."
            if store
            else "No system keychain was found here, so this password is saved with the "
            "other settings. Install 'keyring' or set CAPTURE_SECRETS=memory to keep it out."
        )
        self.alert_threshold = QDoubleSpinBox(card)
        self.alert_threshold.setRange(0.0, 1.0)
        self.alert_threshold.setSingleStep(0.05)
        self.alert_threshold.setDecimals(2)
        self.alert_threshold.setValue(0.0)
        self.alert_threshold.setToolTip(
            "Only alert when a change is at least this large (0 = alert on any change)."
        )
        self.baseline_drift_alert = QDoubleSpinBox(card)
        self.baseline_drift_alert.setRange(0.0, 1.0)
        self.baseline_drift_alert.setSingleStep(0.05)
        self.baseline_drift_alert.setDecimals(2)
        self.baseline_drift_alert.setValue(0.0)
        self.baseline_drift_alert.setToolTip(
            "Alert when a capture drifts this far from its pinned baseline (0 = off)."
        )
        self.digest_days = QSpinBox(card)
        self.digest_days.setRange(1, 365)
        self.digest_days.setSuffix(" days")
        self.digest_days.setValue(7)
        self.digest_days.setToolTip("Window the periodic digest summarises (saved per profile).")
        self.digest_issue = QSpinBox(card)
        self.digest_issue.setRange(0, 999999)
        self.digest_issue.setValue(0)
        self.digest_issue.setToolTip(
            "Tracking issue for the digest sticky comment (0 = none; saved per profile)."
        )
        self.alert_cooldown = QSpinBox(card)
        self.alert_cooldown.setRange(0, 10080)
        self.alert_cooldown.setSuffix(" min")
        self.alert_cooldown.setValue(0)
        self.alert_cooldown.setToolTip(
            "Don't re-alert the same site more often than this (0 = no cooldown)."
        )
        self.alert_mute_urls = QLineEdit(card)
        self.alert_mute_urls.setPlaceholderText("staging, preview")
        self.alert_mute_urls.setToolTip(
            "Never alert about URLs (or labels) containing these fragments "
            "(comma-separated; empty = alert about everything)."
        )
        self.alert_quiet_hours = QLineEdit(card)
        self.alert_quiet_hours.setPlaceholderText("22:00-07:00, fri18:00-mon09:00")
        self.alert_quiet_hours.setToolTip(
            "Alerts inside these windows are queued and sent together afterwards. "
            "Several windows may be listed, and a window may span named days "
            "(empty = never hold them)."
        )

        self.alert_channels = QLineEdit(card)
        self.alert_channels.setPlaceholderText("channels.toml (one [table] per destination)")
        self.alert_channels.setToolTip(
            "A TOML file with one table per destination, each with its own url/to, "
            "match, quiet, mute and min_diff. Set it to replace the routing, quiet "
            "and mute fields below."
        )

        self.alert_quiet_urls = QLineEdit(card)
        self.alert_quiet_urls.setPlaceholderText("staging.example.com=fri18:00-mon09:00")
        self.alert_quiet_urls.setToolTip(
            "Per-URL quiet hours, semicolon-separated: 'fragment=windows'. "
            "An empty window list means the URL keeps alerting at any hour "
            "(empty = every URL uses Quiet hours above)."
        )

        self.alert_route_urls = QLineEdit(card)
        self.alert_route_urls.setPlaceholderText(
            "staging.example.com=https://hooks.example.com/staging"
        )
        self.alert_route_urls.setToolTip(
            "Send a host's changes to its own webhook: 'fragment=https://url' "
            "entries separated by semicolons ('*' matches every URL). Changes "
            "nobody claims go to the webhook URL above."
        )

        rows = [
            ("Webhook URL", self.alert_webhook_url),
            ("Route URLs", self.alert_route_urls),
            ("Webhook type", self.alert_webhook_kind),
            ("Email to", self.alert_email_to),
            ("SMTP host", self.smtp_host),
            ("SMTP port", self.smtp_port),
            ("SMTP user", self.smtp_user),
            ("SMTP password", self.smtp_password),
            ("Alert threshold", self.alert_threshold),
            ("Alert cooldown", self.alert_cooldown),
            ("Mute URLs", self.alert_mute_urls),
            ("Quiet hours", self.alert_quiet_hours),
            ("Quiet per URL", self.alert_quiet_urls),
            ("Channels file", self.alert_channels),
            ("Drift alert", self.baseline_drift_alert),
            ("Digest window", self.digest_days),
            ("Digest issue", self.digest_issue),
        ]
        for row, (label, widget) in enumerate(rows):
            grid.addWidget(self._field_label(label, card), row, 0)
            grid.addWidget(widget, row, 1)
        card.addLayout(grid)
        return card

    def _build_clicks_card(self) -> Card:
        """The click-path to walk before each screenshot, and the crawl switch.

        Most screens of a real site are *behind* something - a Sign-in button, a
        Filters panel, a tab. This card is where that is written down: one line
        per step, exactly what a person would do. "Crawl" is the other half of the
        same idea: nobody writes anything, and the bot finds the clickable things
        itself and photographs every screen it reaches.
        """
        card = Card(
            "Clicks & screens",
            "Walk the site before capturing: one step per line, in order. "
            'click "Sign in" · fill #email=me@x.com · wait 800 · capture dashboard',
            badge="2",
        )

        self.clicks_toggle = QCheckBox("Click through the site, screenshot each screen", card)
        self.clicks_toggle.setToolTip(
            "On: every URL below is opened, the steps run on it, and each 'capture' "
            "step writes its own full-page screenshot (journeys/<name>/)."
        )
        card.addWidget(self.clicks_toggle)

        self.journey_name = QLineEdit(card)
        self.journey_name.setPlaceholderText("journey name (optional, e.g. shop)")
        self.journey_name.setToolTip("Names the folder the screenshots go into.")
        card.addWidget(self.journey_name)

        self.clicks_steps = QPlainTextEdit(card)
        self.clicks_steps.setPlaceholderText(
            'click "Sign in"\nfill #email = ${SHOP_EMAIL:-me@example.com}\n'
            "wait 500\ncapture dashboard"
        )
        self.clicks_steps.setMinimumHeight(120)
        self.clicks_steps.setObjectName("ClicksSteps")
        card.addWidget(self.clicks_steps)

        edit_row = QHBoxLayout()
        edit_row.setSpacing(8)
        self.edit_steps_button = QPushButton("Edit as rows…", card)
        self.edit_steps_button.setObjectName("GhostButton")
        self.edit_steps_button.setToolTip(
            "Open the same steps as a table: one row per step, a drop-down for the "
            "action, a tick for 'optional' and a box for the timeout. Saving writes "
            "the table back into this box."
        )
        self.edit_steps_button.clicked.connect(self.edit_steps_as_rows)
        edit_row.addWidget(self.edit_steps_button)
        self.steps_status = QLabel("", card)
        self.steps_status.setObjectName("CardSubtitle")
        edit_row.addWidget(self.steps_status, 1)
        card.addLayout(edit_row)

        self.clicks_steps.textChanged.connect(self._refresh_steps_status)

        hint = QLabel(
            "click / fill / press / hover / select / check / wait / wait_for / scroll / "
            'back / reload / goto / capture. A step can say selector #id, text "Sign in" or '
            'role button name "Save"; add optional to survive a missing button.',
            card,
        )
        hint.setObjectName("CardSubtitle")
        hint.setWordWrap(True)
        card.addWidget(hint)

        self.crawl_toggle = QCheckBox("…or let the bot explore the site on its own", card)
        self.crawl_toggle.setToolTip(
            "Crawls instead of stepping: finds the buttons/tabs/links on the page, clicks "
            "them one by one and photographs every screen it reaches. Login/delete/pay "
            "buttons are skipped."
        )
        card.addWidget(self.crawl_toggle)

        crawl_row = QHBoxLayout()
        crawl_row.setSpacing(8)
        crawl_row.addWidget(self._field_label("Depth", card))
        self.crawl_depth = QSpinBox(card)
        self.crawl_depth.setRange(1, 5)
        self.crawl_depth.setValue(2)
        self.crawl_depth.setToolTip("How many clicks deep to explore (1 = only the landing page).")
        self.crawl_depth.setMaximumWidth(70)
        crawl_row.addWidget(self.crawl_mode)
        crawl_row.addWidget(self.crawl_depth)
        crawl_row.addWidget(self._field_label("Screens", card))
        self.crawl_states = QSpinBox(card)
        self.crawl_states.setRange(1, 500)
        self.crawl_states.setValue(25)
        self.crawl_states.setToolTip("Stop after this many screens.")
        self.crawl_states.setMaximumWidth(80)
        crawl_row.addWidget(self.crawl_states)
        crawl_row.addWidget(self._field_label("Ignore", card))
        self.crawl_ignore = QLineEdit(card)
        self.crawl_ignore.setPlaceholderText("pricing, blog")
        self.crawl_ignore.setToolTip("Never click anything whose label or link contains these.")
        crawl_row.addWidget(self.crawl_ignore, 1)
        card.addLayout(crawl_row)

        recipe_row = QHBoxLayout()
        recipe_row.setSpacing(8)
        self.recipe_combo = QComboBox(card)
        self.recipe_combo.setToolTip(
            "Ready-made click-paths for common pages: pick one, press Load, then edit it."
        )
        recipe_row.addWidget(self.recipe_combo, 1)
        self.recipe_button = QPushButton("Load recipe", card)
        self.recipe_button.setObjectName("GhostButton")
        self.recipe_button.clicked.connect(self._load_recipe)
        recipe_row.addWidget(self.recipe_button)
        self.save_recipe_button = QPushButton("Save as recipe", card)
        self.save_recipe_button.setObjectName("GhostButton")
        self.save_recipe_button.setToolTip(
            "Keep the steps in the box as a recipe of your own - it then shows up in "
            "the picker (and for 'journey --recipe NAME') on every run."
        )
        self.save_recipe_button.clicked.connect(self.save_current_recipe)
        recipe_row.addWidget(self.save_recipe_button)
        self.recipes_folder_button = QPushButton("Recipes folder…", card)
        self.recipes_folder_button.setObjectName("GhostButton")
        self.recipes_folder_button.setToolTip(
            "Read your own recipes from another folder - point it at a shared or "
            "git-backed one and the whole team edits the same click-paths."
        )
        self.recipes_folder_button.clicked.connect(self.pick_recipes_folder)
        recipe_row.addWidget(self.recipes_folder_button)
        card.addLayout(recipe_row)

        record_row = QHBoxLayout()
        record_row.setSpacing(8)
        self.record_button = QPushButton("Record a session", card)
        self.record_button.setObjectName("GhostButton")
        self.record_button.setToolTip(
            "Open a browser and watch you click around: every click and every typed "
            "value becomes a step, and the recording lands in the box above."
        )
        self.record_button.clicked.connect(self.toggle_recording)
        record_row.addWidget(self.record_button)
        self.record_hint = QLabel("", card)
        self.record_hint.setObjectName("CardSubtitle")
        self.record_hint.setWordWrap(True)
        record_row.addWidget(self.record_hint, 1)
        card.addLayout(record_row)

        self.sample_journey_button = QPushButton("Load sample steps", card)
        self.sample_journey_button.setObjectName("GhostButton")
        self.sample_journey_button.setToolTip(
            "Fill the box with a working example (Sign in -> dashboard) you can edit."
        )
        self.sample_journey_button.clicked.connect(self._insert_sample_steps)
        card.addWidget(self.sample_journey_button)
        self.fill_recipes()
        return card

    def toggle_recording(self) -> None:
        """Start watching a session - or stop and keep what was recorded.

        Recording is the fast path to a journey: nobody knows the CSS selector of
        a button before they have clicked it, but everybody can click it once.
        """
        if self._recording:
            self._recording = False
            self.record_button.setText("Stopping…")
            return

        first_url = next(
            (
                validate_url(line).normalized
                for line in self.url_lines()
                if validate_url(line).is_valid
            ),
            "",
        )
        if not first_url:
            QMessageBox.information(
                self, APP_NAME, "Add the page to record as the first URL, then press Record."
            )
            return

        self._recording = True
        self.record_button.setText("Stop recording")
        self.record_hint.setText(f"Recording {first_url} - click around in the browser.")
        self.append_log(LogLevel.INFO, f"Recording a session on {first_url}.")
        thread = QThread(self)
        worker = RecordingWorker(
            self.collect_settings(), first_url, engine_factory=self.engine_factory
        )

        def keep_stopping(recording, _seconds) -> bool:
            return not self._recording

        worker.should_stop = keep_stopping
        worker.recorded.connect(self._on_recording_done)
        worker.failed.connect(self._on_recording_failed)
        worker.finished.connect(self._on_recording_finished)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        self._recording_thread = thread
        self._recording_worker = worker
        thread.start()

    @pyqtSlot(object)
    def _on_recording_done(self, recording) -> None:
        """Put the recorded steps in the box (replacing it, after asking)."""
        from app.core import journey as journey_module

        text = recording.text()
        if not recording.found or not text:
            self.append_log(LogLevel.WARNING, "Nothing was recorded.")
            return
        if self.clicks_steps.toPlainText().strip():
            answer = QMessageBox.question(
                self,
                APP_NAME,
                "Replace the steps in the box with the recording?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.append_log(LogLevel.INFO, "Recording kept out of the box (it was not empty).")
                return
        self.clicks_steps.setPlainText(text)
        if not self.journey_name.text().strip():
            self.journey_name.setText("recorded")
        self.clicks_toggle.setChecked(True)
        self.append_log(LogLevel.SUCCESS, recording.summary())
        try:  # a recording is only worth keeping if it replays
            journey_module.journey_from_text("recorded", recording.url, text)
        except journey_module.JourneyError as exc:
            self.append_log(LogLevel.WARNING, f"The recording needs an edit: {exc}")

    @pyqtSlot(str)
    def _on_recording_failed(self, message: str) -> None:
        self.append_log(LogLevel.ERROR, message)
        QMessageBox.warning(self, APP_NAME, message)

    @pyqtSlot()
    def _on_recording_finished(self) -> None:
        self._recording = False
        self.record_button.setText("Record a session")
        self.record_hint.setText("")

    def recipes_folder(self) -> str:
        """The folder custom recipes are read from (empty = the default one)."""
        try:
            return str(self._qsettings.value("recipes_dir", "") or "").strip()
        except Exception:  # noqa: BLE001 - no settings store is not a crash
            return ""

    def pick_recipes_folder(self) -> None:
        """Choose the folder recipes come from, and remember it."""
        start = self.recipes_folder() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "The folder your recipes live in", start)
        if not chosen:
            return
        self._qsettings.setValue("recipes_dir", chosen)
        self.fill_recipes()
        count = self.recipe_combo.count()
        self.append_log(LogLevel.SUCCESS, f"Recipes folder: {chosen} ({count} recipe(s))")

    def fill_recipes(self) -> None:
        """List the click-paths in the picker (built-in first, then your own).

        A recipe of your own carries a short version: the file is shared, so two
        people looking at the same picker should be able to say *which* version
        they are running.
        """
        from app.core import journey as journey_module

        mine = {
            recipe.name
            for recipe in journey_module.load_custom_recipes(self.recipes_folder() or None)
        }
        self.recipe_combo.clear()
        for recipe in journey_module.all_recipes(self.recipes_folder() or None):
            if recipe.name in mine:
                label = f"{recipe.name} - {recipe.title} * v{recipe.version}"
            else:
                label = f"{recipe.name} - {recipe.title}"
            self.recipe_combo.addItem(label, recipe.name)

    def _refresh_steps_status(self) -> None:
        """Say how the box parses, right under it: 4 steps - 2 captures."""
        from app.core import steplist

        text = self.clicks_steps.toPlainText()
        if not text.strip():
            self.steps_status.setText("")
            return
        try:
            rows = steplist.parse_rows(text)
        except Exception as exc:  # noqa: BLE001 - a half-typed box is normal
            self.steps_status.setText(f"not finished yet: {exc}")
            return
        self.steps_status.setText(steplist.describe_rows(rows))

    def edit_steps_as_rows(self) -> bool:
        """Open the step editor on the box's text; keep what comes back."""
        from app.core import steplist
        from app.ui.step_editor import StepEditorDialog

        dialog = StepEditorDialog(self.clicks_steps.toPlainText(), self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return False
        text = dialog.steps_text()
        self.clicks_steps.setPlainText(text)
        self.steps_status.setText(steplist.describe_rows(steplist.parse_rows(text)))
        self.append_log(
            LogLevel.INFO, f"Steps updated from the row editor ({text.count(chr(10)) + 1})."
        )
        return True

    def save_current_recipe(self) -> None:
        """Keep what is in the box as a recipe of the user's own."""
        from app.core import journey as journey_module

        text = self.clicks_steps.toPlainText().strip()
        if not text:
            QMessageBox.information(self, APP_NAME, "There are no steps to save yet.")
            return
        suggested = self.journey_name.text().strip() or "my-recipe"
        name, accepted = QInputDialog.getText(
            self, APP_NAME, "Save these steps as a recipe called:", text=suggested
        )
        if not accepted or not name.strip():
            return
        try:
            path = journey_module.save_recipe(name.strip(), text, self.recipes_folder() or None)
        except journey_module.JourneyError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        self.fill_recipes()
        index = self.recipe_combo.findData(name.strip())
        if index >= 0:
            self.recipe_combo.setCurrentIndex(index)
        self.append_log(LogLevel.SUCCESS, f"Recipe saved to {path}")

    def _load_recipe(self) -> None:
        """Put the picked recipe in the box, ready to edit and run."""
        from app.core import journey as journey_module

        name = str(self.recipe_combo.currentData() or self.recipe_combo.currentText()).strip()
        if not name:
            return
        try:
            recipe = journey_module.recipe_for(name, self.recipes_folder() or None)
        except journey_module.JourneyError as exc:  # pragma: no cover - the list is ours
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        # A recipe of the user's own starts with a "# Title: …" header; that is
        # metadata for the picker, not a step, so it stays out of the box.
        lines = recipe.steps.splitlines()
        while lines and (not lines[0].strip() or lines[0].strip().startswith("#")):
            lines.pop(0)
        self.clicks_steps.setPlainText("\n".join(lines))
        if not self.journey_name.text().strip():
            self.journey_name.setText(recipe.name)
        self.clicks_toggle.setChecked(True)
        self.append_log(LogLevel.INFO, f"Loaded the '{recipe.name}' recipe: {recipe.note}")

    def build_journey_spec(self):
        """What the user typed in the Clicks card, as a runnable journey.

        Returns ``(journey, error)``: the error is a sentence for the log instead
        of an exception the run control would have to handle. ``None`` means "no
        clicking was requested", which is not an error.
        """
        from app.core import journey as journey_module

        text = self.journey_text()
        if not text:
            return None, ""
        first_url = next(
            (
                validate_url(line).normalized
                for line in self.url_lines()
                if validate_url(line).is_valid
            ),
            "",
        )
        if not first_url:
            return None, "Add at least one URL: the steps are walked on the first one."
        name = self.journey_name.text().strip() or "journey"
        try:
            return journey_module.journey_from_text(name, first_url, text), ""
        except journey_module.JourneyError as exc:
            return None, f"Step {exc}" if str(exc).startswith("line ") else str(exc)

    def _insert_sample_steps(self) -> None:
        """A starting point that is better than an empty box."""
        self.clicks_steps.setPlainText(
            "capture landing\n"
            'click "Sign in"\n'
            "fill #email = ${CAPTURE_EMAIL:-me@example.com}\n"
            "fill #password = ${CAPTURE_PASSWORD:-secret}\n"
            "click button[type=submit]\n"
            "wait_for .dashboard optional\n"
            "capture dashboard"
        )
        if not self.journey_name.text().strip():
            self.journey_name.setText("shop")
        self.clicks_toggle.setChecked(True)

    def journey_text(self) -> str:
        """The steps box, as text (empty when the user turned the feature off)."""
        if not self.clicks_toggle.isChecked():
            return ""
        return self.clicks_steps.toPlainText().strip()

    def _build_profiles_card(self) -> Card:
        card = Card(
            "Profiles",
            "Save and reuse complete per-project setups (settings + URL list).",
            badge="P",
        )

        row = QHBoxLayout()
        row.setSpacing(8)
        self.profile_combo = QComboBox(card)
        self.profile_combo.setMinimumWidth(140)
        row.addWidget(self.profile_combo, 1)

        self.profile_save = QPushButton("Save as...", card)
        self.profile_save.setCursor(Qt.CursorShape.PointingHandCursor)
        self.profile_apply = QPushButton("Apply", card)
        self.profile_apply.setCursor(Qt.CursorShape.PointingHandCursor)
        self.profile_delete = QPushButton("Delete", card)
        self.profile_delete.setCursor(Qt.CursorShape.PointingHandCursor)
        for button in (self.profile_save, self.profile_apply, self.profile_delete):
            row.addWidget(button)
        card.addLayout(row)
        return card

    def _build_destination_card(self) -> Card:
        card = Card("Destination", "Screenshots are written here, one PNG per URL.", badge="2")

        row = QHBoxLayout()
        row.setSpacing(8)
        self.output_dir_input = QLineEdit(card)
        self.output_dir_input.setPlaceholderText("C:\\Users\\you\\Pictures\\FullPageCaptures")
        self.browse_button = QPushButton("Browse...", card)
        self.browse_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.browse_button.setMinimumWidth(96)
        row.addWidget(self.output_dir_input, 1)
        row.addWidget(self.browse_button)
        card.addLayout(row)

        self.open_folder_toggle = ToggleRow(
            "Open the folder when the run finishes",
            "",
            card,
        )
        card.addWidget(self.open_folder_toggle)
        return card

    def _build_capture_options_card(self) -> Card:
        card = Card("Capture options", None, badge="3")

        self.headless_toggle = ToggleRow(
            "Headless mode",
            "On: the browser runs invisibly in the background (fast). Off: you can watch every page load.",
            card,
        )
        card.addWidget(self.headless_toggle)

        grid = QGridLayout()
        grid.setContentsMargins(0, 4, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        self.viewport_width = QSpinBox(card)
        self.viewport_width.setRange(320, 7680)
        self.viewport_width.setSingleStep(10)
        self.viewport_width.setSuffix(" px")
        self.viewport_width.setValue(1920)

        self.viewport_height = QSpinBox(card)
        self.viewport_height.setRange(200, 7680)
        self.viewport_height.setSingleStep(10)
        self.viewport_height.setSuffix(" px")
        self.viewport_height.setValue(1080)

        self.scale_factor = QDoubleSpinBox(card)
        self.scale_factor.setRange(0.5, 4.0)
        self.scale_factor.setSingleStep(0.25)
        self.scale_factor.setSuffix("x")
        self.scale_factor.setValue(2.0)
        self.scale_factor.setToolTip(
            "2x captures at retina sharpness - sharper text, larger files."
        )

        self.image_format = QComboBox(card)
        self.image_format.addItems(
            ["PNG (lossless)", "JPEG (smaller)", "WebP (compressed)", "AVIF (smallest)"]
        )

        self.navigation_timeout = QSpinBox(card)
        self.navigation_timeout.setRange(5, 600)
        self.navigation_timeout.setSuffix(" s")
        self.navigation_timeout.setValue(60)

        self.settle_delay = QSpinBox(card)
        self.settle_delay.setRange(0, 30_000)
        self.settle_delay.setSingleStep(250)
        self.settle_delay.setSuffix(" ms")
        self.settle_delay.setValue(1500)
        self.settle_delay.setToolTip("Extra pause after the page loads, so animations can settle.")

        labels = (
            ("Viewport width", self.viewport_width),
            ("Viewport height", self.viewport_height),
            ("Scale factor", self.scale_factor),
            ("Image format", self.image_format),
            ("Page timeout", self.navigation_timeout),
            ("Settle delay", self.settle_delay),
        )
        for index, (text, widget) in enumerate(labels):
            row_index, column_index = divmod(index, 2)
            label = QLabel(text, card)
            label.setObjectName("MutedLabel")
            grid.addWidget(label, row_index * 2, column_index)
            grid.addWidget(widget, row_index * 2 + 1, column_index)

        card.addLayout(grid)

        self.lazy_scroll_toggle = ToggleRow(
            "Scroll to load lazy content",
            "Walks down the page first so images that only load on scroll are captured.",
            card,
        )
        self.lazy_scroll_toggle.setChecked(True)
        card.addWidget(self.lazy_scroll_toggle)

        # --- scheduling ---------------------------------------------------
        schedule_row = QHBoxLayout()
        schedule_row.setSpacing(10)

        self.auto_repeat_toggle = ToggleRow(
            "Auto-repeat captures",
            "Re-run the whole batch automatically after each finished run.",
            card,
        )
        schedule_row.addWidget(self.auto_repeat_toggle, 1)

        schedule_row.addWidget(self._field_label("Every", card))
        self.auto_repeat_minutes = QSpinBox(card)
        self.auto_repeat_minutes.setRange(1, 1440)
        self.auto_repeat_minutes.setSingleStep(5)
        self.auto_repeat_minutes.setSuffix(" min")
        self.auto_repeat_minutes.setValue(30)
        self.auto_repeat_minutes.setMaximumWidth(110)
        schedule_row.addWidget(self.auto_repeat_minutes)
        card.addLayout(schedule_row)

        daily_row = QHBoxLayout()
        daily_row.setSpacing(10)
        self.schedule_daily_toggle = ToggleRow(
            "Run daily at a fixed time",
            "One run per day at the chosen time (takes priority over auto-repeat).",
            card,
        )
        daily_row.addWidget(self.schedule_daily_toggle, 1)

        daily_row.addWidget(self._field_label("At", card))
        self.schedule_time = QTimeEdit(card)
        self.schedule_time.setDisplayFormat("HH:mm")
        self.schedule_time.setTime(QTime(9, 0))
        self.schedule_time.setMaximumWidth(110)
        daily_row.addWidget(self.schedule_time)
        card.addLayout(daily_row)

        cron_row = QHBoxLayout()
        cron_row.setSpacing(10)
        self.schedule_cron_toggle = ToggleRow(
            "Run on a cron schedule",
            "Standard 5-field cron (min hour dom mon dow); takes priority over daily/interval.",
            card,
        )
        cron_row.addWidget(self.schedule_cron_toggle, 1)
        cron_row.addWidget(self._field_label("Cron", card))
        self.schedule_cron = QLineEdit(card)
        self.schedule_cron.setPlaceholderText("e.g. 0 9 * * 1-5")
        self.schedule_cron.setMaximumWidth(180)
        cron_row.addWidget(self.schedule_cron)
        card.addLayout(cron_row)

        return card

    def _build_credentials_card(self) -> Card:
        card = Card(
            "Login (optional)",
            "For pages that sit behind a sign-in form or HTTP basic auth.",
            badge="4",
        )

        self.auth_enabled_check = QCheckBox("Sign in before capturing", card)
        card.addWidget(self.auth_enabled_check)

        self.credentials_panel = QWidget(card)
        panel_layout = QGridLayout(self.credentials_panel)
        panel_layout.setContentsMargins(0, 4, 0, 0)
        panel_layout.setHorizontalSpacing(10)
        panel_layout.setVerticalSpacing(6)

        self.auth_mode = QComboBox(self.credentials_panel)
        self.auth_mode.addItem("HTML form (fill and submit)", AUTH_MODE_FORM)
        self.auth_mode.addItem("HTTP Basic / Digest", AUTH_MODE_HTTP)

        self.username_input = QLineEdit(self.credentials_panel)
        self.username_input.setPlaceholderText("Username or e-mail")
        self.password_input = PasswordField("Password", self.credentials_panel)

        panel_layout.addWidget(self._field_label("Method", card), 0, 0)
        panel_layout.addWidget(self.auth_mode, 0, 1)
        panel_layout.addWidget(self._field_label("Username", card), 1, 0)
        panel_layout.addWidget(self.username_input, 1, 1)
        panel_layout.addWidget(self._field_label("Password", card), 2, 0)
        panel_layout.addWidget(self.password_input, 2, 1)

        self.credentials_panel.setEnabled(False)
        card.addWidget(self.credentials_panel)
        return card

    @staticmethod
    def _field_label(text: str, parent: QWidget) -> QLabel:
        label = QLabel(text, parent)
        label.setObjectName("MutedLabel")
        return label

    def _build_advanced_card(self) -> CollapsibleSection:
        section = CollapsibleSection("Advanced", self)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        self.browser_engine = QComboBox(section)
        self.browser_engine.addItems(list(SUPPORTED_BROWSERS))

        self.retries = QSpinBox(section)
        self.retries.setRange(0, 5)
        self.retries.setValue(1)
        self.retries.setToolTip("Extra attempts for a URL that fails.")

        self.max_concurrency = QSpinBox(section)
        self.max_concurrency.setRange(1, 8)
        self.max_concurrency.setValue(1)
        self.max_concurrency.setToolTip("Number of pages captured in parallel (1 = serial).")

        self.watchdog_stale = QSpinBox(section)
        self.watchdog_stale.setRange(0, 10080)
        self.watchdog_stale.setSuffix(" min")
        self.watchdog_stale.setValue(0)
        self.watchdog_stale.setToolTip(
            "Warn at the end of a run when the newest capture is older than this "
            "(0 = off). Catches a bot that keeps running but stops capturing."
        )

        self.jpeg_quality = QSpinBox(section)
        self.jpeg_quality.setRange(10, 100)
        self.jpeg_quality.setValue(92)

        self.scroll_step = QSpinBox(section)
        self.scroll_step.setRange(100, 4000)
        self.scroll_step.setSingleStep(100)
        self.scroll_step.setSuffix(" px")
        self.scroll_step.setValue(800)

        self.filename_prefix = QLineEdit(section)
        self.filename_prefix.setPlaceholderText("e.g. client-a")

        self.user_agent = QLineEdit(section)
        self.user_agent.setPlaceholderText("Leave empty to use the browser default")

        self.locale_input = QLineEdit(section)
        self.locale_input.setPlaceholderText("e.g. en-US")

        self.timezone_input = QLineEdit(section)
        self.timezone_input.setPlaceholderText("e.g. Europe/Berlin")

        self.username_selector = QLineEdit(section)
        self.username_selector.setPlaceholderText("Auto-detect")
        self.password_selector = QLineEdit(section)
        self.password_selector.setPlaceholderText("Auto-detect")
        self.submit_selector = QLineEdit(section)
        self.submit_selector.setPlaceholderText("Auto-detect")

        self.extra_args = QLineEdit(section)
        self.extra_args.setPlaceholderText("--proxy-server=http://localhost:8080, --lang=en-GB")

        self.font_family = QLineEdit(section)
        self.font_family.setPlaceholderText("Leave empty for the system font")
        self.font_size = QSpinBox(section)
        self.font_size.setRange(0, 32)
        self.font_size.setValue(0)
        self.font_size.setSuffix(" pt")
        self.font_size.setToolTip("0 keeps the default size.")

        self.theme_selector = QComboBox(section)
        self.theme_selector.addItems(available_themes())
        self.theme_selector.setCurrentText(theme_name())

        fields = (
            ("Browser engine", self.browser_engine),
            ("Interface font", self.font_family),
            ("Font size", self.font_size),
            ("Theme", self.theme_selector),
            ("Retries per URL", self.retries),
            ("Parallel workers", self.max_concurrency),
            ("Stale warning", self.watchdog_stale),
            ("JPEG quality", self.jpeg_quality),
            ("Lazy-load scroll step", self.scroll_step),
            ("File name prefix", self.filename_prefix),
            ("User agent", self.user_agent),
            ("Locale", self.locale_input),
            ("Time zone", self.timezone_input),
            ("Login: username selector", self.username_selector),
            ("Login: password selector", self.password_selector),
            ("Login: submit selector", self.submit_selector),
            ("Extra browser flags", self.extra_args),
        )
        for row, (text, widget) in enumerate(fields):
            grid.addWidget(self._field_label(text, section), row, 0)
            grid.addWidget(widget, row, 1)
        grid.setColumnStretch(1, 1)
        section.addLayout(grid)

        self.ignore_https_toggle = ToggleRow("Ignore HTTPS certificate errors", "", section)
        self.ignore_https_toggle.setChecked(True)
        self.hide_scrollbars_toggle = ToggleRow("Hide scrollbars in screenshots", "", section)
        self.hide_scrollbars_toggle.setChecked(True)

        self.hide_selectors_label = QLabel("Hide these on every shot (CSS)", section)
        self.hide_selectors_label.setObjectName("FieldLabel")
        section.addWidget(self.hide_selectors_label)
        self.hide_selectors_input = QLineEdit(section)
        self.hide_selectors_input.setPlaceholderText(".cookie-banner, #ad-slot, .clock")
        self.hide_selectors_input.setToolTip(
            "Comma-separated CSS selectors hidden before every screenshot: the cookie "
            "banner, the ad slot, the clock that changes every minute. They stay out "
            "of the captures and out of the before/after diff."
        )
        section.addWidget(self.hide_selectors_input)
        self.stop_on_error_toggle = ToggleRow("Stop the batch on the first failure", "", section)
        self.continue_on_http_error_toggle = ToggleRow(
            "Capture pages that return HTTP 4xx/5xx", "", section
        )
        self.continue_on_http_error_toggle.setChecked(True)
        self.write_log_toggle = ToggleRow(
            "Write a capture log file into the destination folder", "", section
        )
        self.write_log_toggle.setChecked(True)
        self.write_report_toggle = ToggleRow("Write a CSV + JSON results report", "", section)
        self.write_report_toggle.setChecked(True)

        self.change_detection_toggle = ToggleRow(
            "Detect visual changes (monitoring)",
            "Compares each capture with the previous one and only saves a new "
            "file when the change crosses the threshold.",
            section,
        )
        self.change_threshold = QDoubleSpinBox(section)
        self.change_threshold.setRange(0.0, 1.0)
        self.change_threshold.setSingleStep(0.01)
        self.change_threshold.setDecimals(2)
        self.change_threshold.setValue(0.05)
        self.change_threshold.setToolTip("dHash difference (0..1) that counts as a real change.")
        self.change_threshold.setMaximumWidth(110)

        for toggle in (
            self.ignore_https_toggle,
            self.hide_scrollbars_toggle,
            self.stop_on_error_toggle,
            self.continue_on_http_error_toggle,
            self.write_log_toggle,
            self.write_report_toggle,
        ):
            section.addWidget(toggle)

        change_row = QHBoxLayout()
        change_row.setSpacing(10)
        change_row.addWidget(self.change_detection_toggle, 1)
        change_row.addWidget(self._field_label("Threshold", section))
        change_row.addWidget(self.change_threshold)
        section.addLayout(change_row)

        self.history_retention = QSpinBox(section)
        self.history_retention.setRange(0, 3650)
        self.history_retention.setSuffix(" days")
        self.history_retention.setValue(0)
        self.history_retention.setToolTip(
            "Drop SQLite history rows older than this after each run (0 = keep forever)."
        )
        self.history_retention.setMaximumWidth(130)
        retention_row = QHBoxLayout()
        retention_row.setSpacing(10)
        retention_row.addWidget(self._field_label("Keep history", section))
        retention_row.addWidget(self.history_retention)
        section.addLayout(retention_row)

        self.screenshot_retention = QSpinBox(section)
        self.screenshot_retention.setRange(0, 3650)
        self.screenshot_retention.setSuffix(" days")
        self.screenshot_retention.setValue(0)
        self.screenshot_retention.setToolTip(
            "Delete capture images older than this after each run (0 = keep forever)."
        )
        self.screenshot_retention.setMaximumWidth(130)
        screenshots_row = QHBoxLayout()
        screenshots_row.setSpacing(10)
        screenshots_row.addWidget(self._field_label("Keep screenshots", section))
        screenshots_row.addWidget(self.screenshot_retention)
        section.addLayout(screenshots_row)

        self.history_size_cap = QSpinBox(section)
        self.history_size_cap.setRange(0, 1_000_000)
        self.history_size_cap.setSuffix(" MB")
        self.history_size_cap.setValue(0)
        self.history_size_cap.setToolTip(
            "Keep the reports and the SQLite index under this by forgetting the "
            "oldest runs after each run (0 = no cap)."
        )
        self.history_size_cap.setMaximumWidth(130)
        history_row = QHBoxLayout()
        history_row.setSpacing(10)
        history_row.addWidget(self._field_label("History under", section))
        history_row.addWidget(self.history_size_cap)
        section.addLayout(history_row)

        self.history_archive_toggle = ToggleRow(
            "Archive instead of deleting",
            "Zip the runs the history cap drops into archive-YYYY-MM.zip first, "
            "so they can be restored later.",
            section,
        )
        section.addWidget(self.history_archive_toggle)

        self.screenshot_size_cap = QSpinBox(section)
        self.screenshot_size_cap.setRange(0, 1_000_000)
        self.screenshot_size_cap.setSuffix(" MB")
        self.screenshot_size_cap.setValue(0)
        self.screenshot_size_cap.setToolTip(
            "After each run, delete the oldest captures until the folder fits "
            "under this (0 = no cap)."
        )
        self.screenshot_size_cap.setMaximumWidth(130)
        size_row = QHBoxLayout()
        size_row.setSpacing(10)
        size_row.addWidget(self._field_label("Keep under", section))
        size_row.addWidget(self.screenshot_size_cap)
        section.addLayout(size_row)

        return section

    # -- right column ---------------------------------------------------- #
    def _build_run_column(self) -> QWidget:
        column = QWidget(self)
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        progress_card = Card("Progress", None, badge="\u25b6")
        self.progress_bar = QProgressBar(progress_card)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%v / %m")
        progress_card.addWidget(self.progress_bar)

        self.current_url_label = QLabel("Idle", progress_card)
        self.current_url_label.setObjectName("MutedLabel")
        self.current_url_label.setWordWrap(True)
        progress_card.addWidget(self.current_url_label)

        stats = QHBoxLayout()
        stats.setSpacing(8)
        self.stat_done = StatChip("Done", "accent", progress_card)
        self.stat_ok = StatChip("Captured", "success", progress_card)
        self.stat_failed = StatChip("Failed", "error", progress_card)
        self.stat_elapsed = StatChip("Elapsed", "neutral", progress_card)
        self.stat_elapsed.set_value("00:00")
        for chip in (self.stat_done, self.stat_ok, self.stat_failed, self.stat_elapsed):
            chip.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            stats.addWidget(chip)
        progress_card.addLayout(stats)
        layout.addWidget(progress_card)

        log_card = Card("Live log", None, badge="\u2261")
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        toolbar.addWidget(self._field_label("Verbosity", log_card))
        self.verbosity_combo = QComboBox(log_card)
        self.verbosity_combo.addItem("Everything", "DEBUG")
        self.verbosity_combo.addItem("Info and above", "INFO")
        self.verbosity_combo.addItem("Warnings and errors", "WARNING")
        self.verbosity_combo.addItem("Errors only", "ERROR")
        self.verbosity_combo.setCurrentIndex(0)
        self.verbosity_combo.setMinimumWidth(150)
        toolbar.addWidget(self.verbosity_combo)

        self.autoscroll_check = QCheckBox("Auto-scroll", log_card)
        self.autoscroll_check.setChecked(True)
        toolbar.addWidget(self.autoscroll_check)
        toolbar.addStretch(1)

        self.retry_failed_button = ghost_button(
            "Retry failed", "Put the failed URLs back into the list", log_card
        )
        self.retry_failed_button.setEnabled(False)
        self.copy_failed_button = ghost_button(
            "Copy failed", "Copy the failed URLs to the clipboard", log_card
        )
        self.copy_failed_button.setEnabled(False)
        self.save_log_button = ghost_button("Save log", "Export the log to a text file", log_card)
        self.clear_log_button = ghost_button("Clear", "Empty the log view", log_card)
        for button in (
            self.retry_failed_button,
            self.copy_failed_button,
            self.save_log_button,
            self.clear_log_button,
        ):
            toolbar.addWidget(button)

        log_card.addLayout(toolbar)

        self.log_console = LogConsole(log_card)
        self.log_console.setMinimumHeight(260)
        log_card.addWidget(self.log_console)

        layout.addWidget(log_card, 1)
        return column

    # -- bottom ---------------------------------------------------------- #
    def _build_action_bar(self) -> QWidget:
        bar = QFrame(self)
        bar.setObjectName("Card")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        self.action_hint = QLabel("Paste your URLs, pick a folder, then press Start Capture.", bar)
        self.action_hint.setObjectName("MutedLabel")
        layout.addWidget(self.action_hint, 1)

        self.open_output_button = QPushButton("Open folder", bar)
        self.open_output_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_output_button.setToolTip("Open the destination folder in Explorer")
        layout.addWidget(self.open_output_button)

        self.pdf_button = QPushButton("PDF report", bar)
        self.pdf_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pdf_button.setToolTip("Export the last run to a PDF report")
        self.pdf_button.setEnabled(False)
        layout.addWidget(self.pdf_button)

        self.pdf_only_changes = QCheckBox("Only changes", bar)
        self.pdf_only_changes.setToolTip("Include only pages that visually changed")
        layout.addWidget(self.pdf_only_changes)

        self.pdf_include_trend = QCheckBox("Trend page", bar)
        self.pdf_include_trend.setToolTip("Append a per-site trend page (table + sparklines)")
        layout.addWidget(self.pdf_include_trend)

        self.stop_button = QPushButton("Stop", bar)
        self.stop_button.setObjectName("DangerButton")
        self.stop_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_button.setEnabled(False)
        layout.addWidget(self.stop_button)

        self.start_button = QPushButton("Start Capture", bar)
        self.start_button.setObjectName("PrimaryButton")
        self.start_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_button.setMinimumWidth(180)
        self.start_button.setDefault(True)
        self.start_button.setShortcut("Ctrl+Return")
        self.start_button.setToolTip("Start capturing (Ctrl+Enter)")
        layout.addWidget(self.start_button)
        return bar

    # ------------------------------------------------------------------ #
    # wiring
    # ------------------------------------------------------------------ #
    def _connect_signals(self) -> None:
        self.start_button.clicked.connect(self.start_capture)
        self.stop_button.clicked.connect(self.stop_capture)
        self.browse_button.clicked.connect(self._browse_folder)
        self.open_output_button.clicked.connect(self._open_output_folder)
        self.pdf_button.clicked.connect(self._export_pdf_report)
        self.sample_button.clicked.connect(self._insert_sample_urls)
        self.paste_button.clicked.connect(self._paste_from_clipboard)
        self.clear_urls_button.clicked.connect(lambda: self.url_input.clear())
        self.url_input.textChanged.connect(self._refresh_url_summary)

        self.auth_enabled_check.toggled.connect(self.credentials_panel.setEnabled)
        self.image_format.currentIndexChanged.connect(self._on_format_changed)
        self.theme_selector.currentTextChanged.connect(self._on_theme_selected)
        self.verbosity_combo.currentIndexChanged.connect(self._on_verbosity_changed)
        self.autoscroll_check.toggled.connect(self._on_autoscroll_toggled)
        self.clear_log_button.clicked.connect(self.log_console.clear_console)
        self.save_log_button.clicked.connect(self._save_log_file)
        self.retry_failed_button.clicked.connect(self._retry_failed_urls)
        self.copy_failed_button.clicked.connect(self._copy_failed_urls)
        self.install_browser_button.clicked.connect(self._install_browser)

        self.auto_repeat_toggle.toggled.connect(self._on_auto_repeat_toggled)
        self.auto_repeat_minutes.valueChanged.connect(self._on_auto_repeat_interval_changed)
        self.schedule_daily_toggle.toggled.connect(self._on_schedule_changed)
        self.schedule_time.timeChanged.connect(self._on_schedule_changed)
        self.schedule_cron_toggle.toggled.connect(self._on_schedule_changed)
        self.schedule_cron.textChanged.connect(self._on_schedule_changed)
        self.profile_save.clicked.connect(self._save_profile_as)
        self.profile_apply.clicked.connect(self._apply_profile)
        self.profile_delete.clicked.connect(self._delete_profile)
        self.storage_browse.clicked.connect(self._browse_storage_state)

    def _start_browser_probe(self) -> None:
        """Ask Playwright about its browser without freezing start-up."""
        self._probe = _FnThread(lambda: detect_browser(self.browser_engine.currentText()), self)
        self._probe.done.connect(self._on_browser_status)
        self._probe.start()

    @pyqtSlot(object)
    def _on_browser_status(self, status: object) -> None:
        if not isinstance(status, BrowserStatus):
            self._browser_status = None
            self.browser_chip.setText("Browser check failed")
            self.browser_chip.setToolTip(str(status))
            self.install_browser_button.setVisible(True)
            self._restyle_browser_chip()
            return

        self._browser_status = status
        self.browser_chip.setText(status.message)
        self.browser_chip.setToolTip(status.executable_path or status.message)
        self._restyle_browser_chip()
        self.install_browser_button.setVisible(not status.browser_available)
        if not status.ready:
            self.append_log(LogLevel.WARNING, status.message)

    def _restyle_browser_chip(self) -> None:
        """(Re)apply the header chip colours for the active theme."""
        p = current()
        status = self._browser_status
        if status is None:
            border = text = p.text_muted
        elif status.ready:
            border = text = p.success
        else:
            border = text = p.warning
        self.browser_chip.setStyleSheet(
            f"background: {p.surface_alt}; border: 1px solid {border};"
            f"border-radius: 12px; padding: 5px 12px; font-size: 11px; font-weight: 600; color: {text};"
        )

    # ------------------------------------------------------------------ #
    # settings <-> widgets
    # ------------------------------------------------------------------ #
    def collect_settings(self) -> CaptureSettings:
        """Read every widget into a :class:`CaptureSettings` snapshot."""
        settings = CaptureSettings()

        profile_dir = Path.home() / ".capture-bot" / "profiles" / (settings.profile_name or "default")
        profile_dir.mkdir(parents=True, exist_ok=True)
        settings.output_dir = self.output_dir_input.text().strip() or str(profile_dir / "shots")
        settings.filename_prefix = self.filename_prefix.text().strip()
        settings.image_format = {0: "png", 1: "jpeg", 2: "webp", 3: "avif"}.get(
            self.image_format.currentIndex(), "png"
        )
        settings.jpeg_quality = int(self.jpeg_quality.value())
        settings.open_folder_when_done = self.open_folder_toggle.isChecked()

        settings.browser = self.browser_engine.currentText()
        settings.headless = self.headless_toggle.isChecked()
        settings.ignore_https_errors = self.ignore_https_toggle.isChecked()
        settings.extra_browser_args = self.extra_args.text().strip()

        settings.viewport_width = int(self.viewport_width.value())
        settings.viewport_height = int(self.viewport_height.value())
        settings.device_scale_factor = float(self.scale_factor.value())
        settings.user_agent = self.user_agent.text().strip()
        settings.locale = self.locale_input.text().strip()
        settings.timezone = self.timezone_input.text().strip()

        settings.navigation_timeout_ms = int(self.navigation_timeout.value()) * 1000
        settings.settle_delay_ms = int(self.settle_delay.value())
        settings.lazy_scroll_step_px = int(self.scroll_step.value())
        settings.scroll_to_load_lazy_content = self.lazy_scroll_toggle.isChecked()
        settings.hide_scrollbars = self.hide_scrollbars_toggle.isChecked()
        settings.hide_selectors = self.hide_selectors_input.text().strip()

        settings.auth_enabled = self.auth_enabled_check.isChecked()
        settings.auth_mode = self.auth_mode.currentData() or AUTH_MODE_FORM
        settings.username = self.username_input.text().strip()
        settings.password = self.password_input.text()
        settings.login_username_selector = self.username_selector.text().strip()
        settings.login_password_selector = self.password_selector.text().strip()
        settings.login_submit_selector = self.submit_selector.text().strip()

        settings.retries = int(self.retries.value())
        settings.max_concurrency = int(self.max_concurrency.value())
        settings.watchdog_stale_minutes = int(self.watchdog_stale.value())
        settings.font_family = self.font_family.text().strip()
        settings.font_size = int(self.font_size.value())
        settings.stop_on_first_error = self.stop_on_error_toggle.isChecked()
        settings.continue_on_http_error = self.continue_on_http_error_toggle.isChecked()
        settings.write_log_file = self.write_log_toggle.isChecked()
        settings.write_report = self.write_report_toggle.isChecked()
        settings.history_retention_days = int(self.history_retention.value())
        settings.screenshot_retention_days = int(self.screenshot_retention.value())
        settings.screenshot_retention_mb = int(self.screenshot_size_cap.value())
        settings.history_retention_mb = int(self.history_size_cap.value())
        settings.history_archive = self.history_archive_toggle.isChecked()
        settings.auto_repeat_enabled = self.auto_repeat_toggle.isChecked()
        settings.auto_repeat_minutes = int(self.auto_repeat_minutes.value())
        settings.schedule_daily_enabled = self.schedule_daily_toggle.isChecked()
        settings.schedule_time = self.schedule_time.time().toString("HH:mm")
        settings.schedule_cron_enabled = self.schedule_cron_toggle.isChecked()
        settings.schedule_cron = self.schedule_cron.text().strip()
        settings.alert_enabled = self.alert_toggle.isChecked()
        settings.alert_webhook_url = self.alert_webhook_url.text().strip()
        settings.alert_webhook_kind = self.alert_webhook_kind.currentData() or "generic"
        settings.alert_email_to = self.alert_email_to.text().strip()
        settings.smtp_host = self.smtp_host.text().strip()
        settings.smtp_port = int(self.smtp_port.value())
        settings.smtp_user = self.smtp_user.text().strip()
        from app.core import secrets

        typed = self.smtp_password.text()
        settings.smtp_password = "" if secrets.is_mask(typed) else typed
        settings.change_alert_threshold = float(self.alert_threshold.value())
        settings.alert_cooldown_minutes = int(self.alert_cooldown.value())
        settings.alert_quiet_hours = self.alert_quiet_hours.text().strip()
        settings.alert_quiet_urls = self.alert_quiet_urls.text().strip()
        settings.alert_channels = self.alert_channels.text().strip()
        settings.alert_route_urls = self.alert_route_urls.text().strip()
        settings.alert_mute_urls = self.alert_mute_urls.text().strip()
        settings.baseline_drift_alert_threshold = float(self.baseline_drift_alert.value())
        settings.digest_days = int(self.digest_days.value())
        settings.digest_issue_number = int(self.digest_issue.value())
        settings.proxy_server = self.proxy_server.text().strip()
        settings.storage_state_path = self.storage_state_path.text().strip()
        settings.desktop_notifications = self.desktop_notify_toggle.isChecked()
        settings.auto_dashboard = self.auto_dashboard_toggle.isChecked()
        settings.language = i18n.current_language()
        settings.change_detection_enabled = self.change_detection_toggle.isChecked()
        settings.change_threshold = float(self.change_threshold.value())
        return settings

    def apply_settings(self, settings: CaptureSettings) -> None:
        """Push a settings object back into the widgets."""
        self.output_dir_input.setText(settings.output_dir)
        self.filename_prefix.setText(settings.filename_prefix)
        self.image_format.setCurrentIndex(
            {"jpeg": 1, "webp": 2, "avif": 3}.get(settings.image_format, 0)
        )
        self.jpeg_quality.setValue(int(settings.jpeg_quality))
        self.open_folder_toggle.setChecked(settings.open_folder_when_done)

        index = self.browser_engine.findText(settings.browser)
        self.browser_engine.setCurrentIndex(max(0, index))
        self.headless_toggle.setChecked(settings.headless)
        self.ignore_https_toggle.setChecked(settings.ignore_https_errors)
        self.extra_args.setText(settings.extra_browser_args)

        self.viewport_width.setValue(int(settings.viewport_width))
        self.viewport_height.setValue(int(settings.viewport_height))
        self.scale_factor.setValue(float(settings.device_scale_factor))
        self.user_agent.setText(settings.user_agent)
        self.locale_input.setText(settings.locale)
        self.timezone_input.setText(settings.timezone)

        self.navigation_timeout.setValue(max(5, int(settings.navigation_timeout_ms) // 1000))
        self.settle_delay.setValue(int(settings.settle_delay_ms))
        self.scroll_step.setValue(int(settings.lazy_scroll_step_px))
        self.lazy_scroll_toggle.setChecked(settings.scroll_to_load_lazy_content)
        self.hide_scrollbars_toggle.setChecked(settings.hide_scrollbars)
        self.hide_selectors_input.setText(settings.hide_selectors)

        self.auth_enabled_check.setChecked(settings.auth_enabled)
        mode_index = self.auth_mode.findData(settings.auth_mode)
        self.auth_mode.setCurrentIndex(max(0, mode_index))
        self.username_input.setText(settings.username)
        self.password_input.setText(settings.password)
        self.username_selector.setText(settings.login_username_selector)
        self.password_selector.setText(settings.login_password_selector)
        self.submit_selector.setText(settings.login_submit_selector)

        self.retries.setValue(int(settings.retries))
        self.max_concurrency.setValue(int(settings.max_concurrency))
        self.watchdog_stale.setValue(int(settings.watchdog_stale_minutes))
        self.font_family.setText(settings.font_family)
        self.font_size.setValue(int(settings.font_size))
        self.stop_on_error_toggle.setChecked(settings.stop_on_first_error)
        self.continue_on_http_error_toggle.setChecked(settings.continue_on_http_error)
        self.write_log_toggle.setChecked(settings.write_log_file)
        self.write_report_toggle.setChecked(settings.write_report)
        self.history_retention.setValue(int(settings.history_retention_days))
        self.screenshot_retention.setValue(int(settings.screenshot_retention_days))
        self.screenshot_size_cap.setValue(int(settings.screenshot_retention_mb))
        self.history_size_cap.setValue(int(settings.history_retention_mb))
        self.history_archive_toggle.setChecked(settings.history_archive)
        self.auto_repeat_toggle.setChecked(settings.auto_repeat_enabled)
        self.auto_repeat_minutes.setValue(int(settings.auto_repeat_minutes))
        self.schedule_daily_toggle.setChecked(settings.schedule_daily_enabled)
        parsed_time = QTime.fromString(settings.schedule_time, "HH:mm")
        self.schedule_time.setTime(parsed_time if parsed_time.isValid() else QTime(9, 0))
        self.schedule_cron_toggle.setChecked(settings.schedule_cron_enabled)
        self.schedule_cron.setText(settings.schedule_cron)
        self.alert_toggle.setChecked(settings.alert_enabled)
        self.alert_webhook_url.setText(settings.alert_webhook_url)
        index = self.alert_webhook_kind.findData(settings.alert_webhook_kind)
        self.alert_webhook_kind.setCurrentIndex(max(0, index))
        self.alert_email_to.setText(settings.alert_email_to)
        self.smtp_host.setText(settings.smtp_host)
        self.smtp_port.setValue(int(settings.smtp_port))
        self.smtp_user.setText(settings.smtp_user)
        from app.core import secrets

        # Where the password comes from: the keychain when there is one (it is the
        # only place the app writes to now), the settings file otherwise - a
        # machine with no keychain still has to be able to send mail.
        stored_password = secrets.load() if secrets.available() else settings.smtp_password
        # Show a placeholder, never the password, and never leave it in a file.
        self.smtp_password.setText(secrets.MASK if stored_password else "")
        self.alert_threshold.setValue(float(settings.change_alert_threshold))
        self.alert_cooldown.setValue(int(settings.alert_cooldown_minutes))
        self.alert_quiet_hours.setText(settings.alert_quiet_hours)
        self.alert_quiet_urls.setText(settings.alert_quiet_urls)
        self.alert_channels.setText(settings.alert_channels)
        self.alert_route_urls.setText(settings.alert_route_urls)
        self.alert_mute_urls.setText(settings.alert_mute_urls)
        self.baseline_drift_alert.setValue(float(settings.baseline_drift_alert_threshold))
        self.digest_days.setValue(int(settings.digest_days))
        self.digest_issue.setValue(int(settings.digest_issue_number))
        self.proxy_server.setText(settings.proxy_server)
        self.storage_state_path.setText(settings.storage_state_path)
        self.desktop_notify_toggle.setChecked(settings.desktop_notifications)
        self.auto_dashboard_toggle.setChecked(settings.auto_dashboard)
        self.change_detection_toggle.setChecked(settings.change_detection_enabled)
        self.change_threshold.setValue(float(settings.change_threshold))
        self._on_format_changed()
        self._sync_theme_button()
        i18n.set_language(settings.language)
        self._retranslate()
        self._apply_font(settings)
        self.theme_selector.blockSignals(True)
        self.theme_selector.setCurrentText(theme_name())
        self.theme_selector.blockSignals(False)

    def _load_settings(self) -> None:
        stored = self._qsettings.value(self.SETTINGS_KEY)
        settings = (
            CaptureSettings.from_dict(stored) if isinstance(stored, dict) else default_settings()
        )
        if isinstance(stored, dict):
            # A saved profile may predate new fields; fill the gaps with defaults.
            fresh = default_settings()
            for key, value in fresh.to_dict().items():
                stored.setdefault(key, value)
            settings = CaptureSettings.from_dict(stored)
        self.apply_settings(settings)

    def _save_settings(self) -> None:
        try:
            from app.core import secrets

            settings = self.collect_settings()
            payload = settings.to_dict()
            payload.pop("password", None)  # never persist secrets
            # The SMTP password goes to the OS keychain (or nowhere) - a settings
            # file is copied, mailed and backed up, a keychain entry is not.
            typed = self.smtp_password.text()
            if typed and not secrets.is_mask(typed) and secrets.store(typed):
                # The keychain has it now, so the settings file does not have to.
                # A machine with no keychain keeps the old behaviour instead.
                payload["smtp_password"] = ""
            self._qsettings.setValue(self.SETTINGS_KEY, payload)
            self._qsettings.sync()
        except Exception as exc:  # pragma: no cover - settings are a convenience
            self.append_log(LogLevel.DEBUG, f"Could not save settings: {exc}")

    # ------------------------------------------------------------------ #
    # URL helpers
    # ------------------------------------------------------------------ #
    def url_lines(self) -> list[str]:
        return split_url_lines(self.url_input.toPlainText())

    def _refresh_url_summary(self) -> None:
        lines = self.url_lines()
        invalid = [line for line in lines if not validate_url(line).is_valid]
        valid = len(lines) - len(invalid)

        if not lines:
            text = "0 URLs"
        elif invalid:
            text = f"{valid} ready · {len(invalid)} invalid"
        else:
            text = f"{valid} URL{'s' if valid != 1 else ''} ready"
        self.url_summary.setText(text)
        p = current()
        self.url_summary.setStyleSheet(f"color: {p.error if invalid else p.text_muted};")
        self.start_button.setEnabled(bool(valid) and not self._running)

    def _insert_sample_urls(self) -> None:
        self.url_input.setPlainText(SAMPLE_URLS)

    def _paste_from_clipboard(self) -> None:
        from PyQt6.QtWidgets import QApplication  # local import keeps module import cheap

        clipboard = QApplication.clipboard()
        if clipboard is not None:
            text = clipboard.text()
            if text.strip():
                self.url_input.setPlainText(text)

    # ------------------------------------------------------------------ #
    # run control
    # ------------------------------------------------------------------ #
    @pyqtSlot()
    def start_capture(self) -> None:
        """Validate the form, then launch the worker thread."""
        if self._running:
            return
        self._last_walk_folder = ""  # a new run decides afresh where the screens go

        urls = [validate_url(line).normalized for line in self.url_lines()]
        urls = [url for url in urls if url]
        if not urls:
            QMessageBox.information(self, APP_NAME, "Add at least one valid URL to capture.")
            return

        settings = self.collect_settings()
        try:
            settings.validate()
        except SettingsError as exc:
            self.append_log(LogLevel.ERROR, str(exc))
            QMessageBox.warning(self, APP_NAME, str(exc))
            return

        self._last_summary = None
        self.pdf_button.setEnabled(False)
        self._set_running(True)
        self._reset_stats(len(urls))
        self.log_console.clear_console()
        self.append_log(LogLevel.INFO, f"{APP_DISPLAY_NAME} - run started")
        self._save_settings()
        queue_store.save_queue(self._profiles_base, urls)

        if self.clicks_toggle.isChecked() or self.crawl_toggle.isChecked():
            self._start_screen_walk(settings, urls)
            return

        self._worker = CaptureWorker(settings, urls, engine_factory=self.engine_factory)
        self._worker.log_message.connect(self.append_log_by_name)
        self._worker.progress_changed.connect(self._on_progress)
        self._worker.result_ready.connect(self._on_result)
        self._worker.batch_finished.connect(self._on_batch_finished)
        self._worker.fatal_error.connect(self._on_fatal_error)

        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._thread.start()

    def open_last_walk_folder(self) -> None:
        """Open the folder a journey/crawl wrote to (falls back to the output dir)."""
        target = getattr(self, "_last_walk_folder", "") or ""
        if target and Path(target).is_dir():
            self._open_folder(target)
            return
        self._open_output_folder()

    def _start_screen_walk(self, settings: CaptureSettings, urls: list[str]) -> None:
        """Run a journey (or a crawl) on the worker thread and report like a batch.

        The same thread/signal plumbing is reused on purpose: Stop, the log
        console, the progress chips and the "finished" handling all keep working
        unchanged, whatever the bot is doing inside a page.
        """
        from app.core import crawler as crawler_module
        from app.core import journey as journey_module

        crawl = self.crawl_toggle.isChecked()
        depth = int(self.crawl_depth.value())
        mode = str(self.crawl_mode.currentText()) if hasattr(self, "crawl_mode") else "main"
        states = int(self.crawl_states.value())
        ignore = tuple(item.strip() for item in self.crawl_ignore.text().split(",") if item.strip())
        journey_spec = None
        if not crawl:
            journey_spec, problem = self.build_journey_spec()
            if problem:
                self.append_log(LogLevel.ERROR, problem)
                QMessageBox.warning(self, APP_NAME, problem)
                self._set_running(False)
                return

        class ScreenWalkWorker(CaptureWorker):
            """A worker that photographs screens instead of whole pages."""

            def run(self) -> None:  # noqa: D102 - same contract as the parent
                started = time.monotonic()
                summary = CaptureSummary(output_dir=settings.output_dir)
                try:
                    engine = self._engine_factory(settings, log=self.log_message.emit)
                    self._engine = engine
                    if crawl:
                        options = crawler_module.CrawlOptions(
                            max_depth=depth, max_states=states, ignore=ignore
                        )
                        report = crawler_module.Crawler(engine, options).run(urls[0])
                        folder = crawler_module.crawl_folder(report)
                        if folder is not None:
                            crawler_module.write_crawl_report(report, folder)
                        self.walk_folder = str(folder) if folder is not None else ""
                        self._on_log(LogLevel.INFO, report.summary().replace("\n", " | "))
                        results = [
                            CaptureResult(
                                index=page.index,
                                url=page.url,
                                status=(CaptureStatus.SUCCESS if page.ok else CaptureStatus.FAILED),
                                message=page.error or (page.title or page.name),
                                file_path=page.file_path,
                                duration_ms=page.elapsed_ms,
                            )
                            for page in report.pages
                        ]
                        results.extend(
                            CaptureResult(
                                index=len(results) + offset,
                                url=urls[0],
                                status=CaptureStatus.FAILED,
                                message=message,
                            )
                            for offset, message in enumerate(report.errors, start=1)
                        )
                        self.walk_root = str(settings.output_dir)
                    else:
                        report = journey_module.JourneyRunner(engine).run([journey_spec])
                        self.walk_folder = str(Path(settings.output_dir) / "journeys")
                        self.walk_root = str(settings.output_dir)
                        self._on_log(LogLevel.INFO, report.summary().replace("\n", " | "))
                        results = []
                        for run in report.journeys:
                            for path in run.captures:
                                results.append(
                                    CaptureResult(
                                        index=len(results) + 1,
                                        url=run.url,
                                        status=CaptureStatus.SUCCESS,
                                        message=f"{run.name}: {Path(path).stem}",
                                        file_path=path,
                                    )
                                )
                            for step in run.failures():
                                results.append(
                                    CaptureResult(
                                        index=len(results) + 1,
                                        url=step.url or run.url,
                                        status=CaptureStatus.FAILED,
                                        message=f"{step.action}: {step.message}",
                                    )
                                )
                    summary = CaptureSummary(
                        results=results,
                        output_dir=settings.output_dir,
                        started_at=started,
                        ended_at=time.monotonic(),
                    )
                except BrowserNotInstalledError as exc:
                    self._on_log(LogLevel.ERROR, str(exc))
                    self.fatal_error.emit(str(exc))
                except Exception as exc:  # noqa: BLE001 - absolute safety net
                    message = f"Unexpected failure: {exc.__class__.__name__}: {exc}"
                    self._on_log(LogLevel.ERROR, message)
                    self.fatal_error.emit(message)
                finally:
                    self.batch_finished.emit(summary)
                    self._engine = None

        worker = ScreenWalkWorker(settings, urls, engine_factory=self.engine_factory)
        worker.walk_folder = ""
        worker.walk_root = str(settings.output_dir)
        self._worker = worker
        worker.log_message.connect(self.append_log_by_name)
        worker.progress_changed.connect(self._on_progress)
        worker.result_ready.connect(self._on_result)
        worker.batch_finished.connect(self._on_batch_finished)
        worker.fatal_error.connect(self._on_fatal_error)

        self._thread = QThread(self)
        worker.moveToThread(self._thread)
        self._thread.started.connect(worker.run)
        self._thread.start()

    @pyqtSlot()
    def stop_capture(self) -> None:
        if not self._running or self._worker is None:
            return
        self.append_log(
            LogLevel.WARNING, "Stop requested - finishing the current page, then stopping."
        )
        self._worker.request_stop()
        self.stop_button.setEnabled(False)
        self.action_hint.setText("Stopping after the current page...")

    def _set_running(self, running: bool) -> None:
        self._running = running
        self.start_button.setEnabled(not running and bool(self.url_lines()))
        self.stop_button.setEnabled(running)
        self.settings_container.setEnabled(not running)
        self.open_output_button.setEnabled(True)

        if running:
            self._run_started_at = datetime.now().timestamp()
            self._elapsed_timer.start()
            self.progress_bar.setRange(0, 100)
            self.action_hint.setText(
                "Capturing... press Stop to finish the current page and abort."
            )
        else:
            self._elapsed_timer.stop()
            self.action_hint.setText(
                "Finished."
                if self._last_summary is not None
                else "Paste your URLs, pick a folder, then press Start Capture."
            )

    def _reset_stats(self, total: int) -> None:
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat(f"%v / {total}")
        self._ok_count = 0
        self._failed_count = 0
        self.stat_done.set_value(0)
        self.stat_ok.set_value(0)
        self.stat_failed.set_value(0)
        self.stat_elapsed.set_value("00:00")
        self.current_url_label.setText("Starting...")
        self.retry_failed_button.setEnabled(False)
        self.copy_failed_button.setEnabled(False)

    def _tick_elapsed(self) -> None:
        if self._run_started_at is None:
            return
        seconds = int(datetime.now().timestamp() - self._run_started_at)
        self.stat_elapsed.set_value(f"{seconds // 60:02d}:{seconds % 60:02d}")

    # ------------------------------------------------------------------ #
    # worker slots
    # ------------------------------------------------------------------ #
    @pyqtSlot(str, str)
    def append_log_by_name(self, level: str, message: str) -> None:
        try:
            parsed = LogLevel(level)
        except ValueError:
            parsed = LogLevel.INFO
        self.append_log(parsed, message)

    def append_log(self, level: LogLevel, message: str) -> None:
        self.log_console.append_log(level.value, message)

    @pyqtSlot(int, int, str)
    def _on_progress(self, completed: int, total: int, current: str) -> None:
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(min(completed, total))
        self.progress_bar.setFormat(f"{completed} / {total}")
        self.stat_done.set_value(completed)
        self.current_url_label.setText(current)
        self.status_bar.showMessage(f"Capturing {completed}/{total}: {current}")

    @pyqtSlot(object)
    def _on_result(self, result: CaptureResult) -> None:
        if result.status is CaptureStatus.SUCCESS:
            self._ok_count += 1
            self.stat_ok.set_value(self._ok_count)
        elif result.status is CaptureStatus.FAILED:
            self._failed_count += 1
            self.stat_failed.set_value(self._failed_count)

    def _big_change_labels(self, summary: CaptureSummary) -> list[str]:
        """Labels of pages whose visual change met the alert threshold."""
        try:
            threshold = float(self.alert_threshold.value())
        except (AttributeError, ValueError):  # pragma: no cover - defensive
            threshold = 0.0
        labels: list[str] = []
        for result in summary.results:
            diff = getattr(result, "diff", None)
            if diff is None or getattr(result, "unchanged", False):
                continue
            if diff >= threshold:
                labels.append(build_url_label(result.url))
        return labels

    @pyqtSlot(object)
    def _on_batch_finished(self, summary: CaptureSummary) -> None:
        self._last_summary = summary
        folder = str(getattr(self._worker, "walk_folder", "") or "")
        if folder:
            self._last_walk_folder = folder
            self._last_walk_root = str(getattr(self._worker, "walk_root", "") or folder)
            self.append_log(LogLevel.SUCCESS, f"Screenshots in {folder}")
        self._set_running(False)
        self._arm_scheduler()
        self.pdf_button.setEnabled(summary.total > 0)
        queue_store.clear_queue(self._profiles_base)
        self._log_trend_summary(summary.output_dir)
        if self.desktop_notify_toggle.isChecked():
            from app.core import notify

            changed = self._big_change_labels(summary)
            if changed:
                headline = (
                    f"{changed[0]} changed significantly."
                    if len(changed) == 1
                    else f"{len(changed)} pages changed significantly ({changed[0]} +more)."
                )
                notify.send_notification(APP_NAME, headline)
            else:
                notify.send_notification(APP_NAME, summary.headline())

        if summary.total:
            self.progress_bar.setValue(summary.total)
            self.stat_done.set_value(summary.total)
        seconds = summary.elapsed_ms / 1000
        self.stat_elapsed.set_value(f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}")

        failed = [r.url for r in summary.results if r.status is CaptureStatus.FAILED]
        self._last_failed = failed
        self.retry_failed_button.setEnabled(bool(failed))
        self.copy_failed_button.setEnabled(bool(failed))

        self.current_url_label.setText(summary.headline() if summary.total else "Nothing to do.")
        self.status_bar.showMessage(f"Done - {summary.headline()} in {seconds:.1f}s")
        self.append_log(LogLevel.INFO, f"Output folder: {summary.output_dir}")

        if summary.succeeded and summary.output_dir and self.open_folder_toggle.isChecked():
            # After a walk this is the folder of screens, not the output root.
            self.open_last_walk_folder()

        self._teardown_worker()

    @pyqtSlot(str)
    def _on_fatal_error(self, message: str) -> None:
        self.status_bar.showMessage("Run aborted - see the log for details.")
        self.action_hint.setText("Something went wrong before capturing started.")
        QMessageBox.critical(self, APP_NAME, message)

    def _teardown_worker(self) -> None:
        thread, self._thread = self._thread, None
        worker, self._worker = self._worker, None

        if thread is not None:
            thread.quit()
            if not thread.wait(5000):  # pragma: no cover - defensive
                thread.terminate()
                thread.wait(1000)
            thread.deleteLater()
        if worker is not None:
            worker.deleteLater()

    # ------------------------------------------------------------------ #
    # misc actions
    # ------------------------------------------------------------------ #
    @pyqtSlot()
    def _browse_folder(self) -> None:
        start_dir = self.output_dir_input.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose where screenshots are saved", start_dir
        )
        if chosen:
            self.output_dir_input.setText(chosen)

    @pyqtSlot()
    def import_session_file(self, path: str = "") -> bool:
        """Import a storage_state file: check it, keep it, use it."""
        from app.core import sessionfile

        chosen = str(path or "").strip()
        if not chosen:
            chosen, _ = QFileDialog.getOpenFileName(
                self,
                "Choose the session file to import",
                str(Path.home()),
                "storage_state JSON (*.json);;All files (*)",
            )
        if not chosen:
            return False

        try:
            destination, info = sessionfile.import_session(chosen)
        except sessionfile.SessionFileError as exc:
            self.session_status.setText(f"Not a session file: {exc}")
            QMessageBox.warning(self, APP_NAME, str(exc))
            return False

        self.storage_state_path.setText(str(destination))
        note = sessionfile.expires_text(info)
        self.session_status.setText(f"{info.summary()} - until {note}")
        level = (
            LogLevel.WARNING
            if info.expired or sessionfile.expiring_soon(info)
            else LogLevel.SUCCESS
        )
        self.append_log(level, f"Imported the session {destination.name}: {info.summary()}")
        return True

    @pyqtSlot()
    def _browse_storage_state(self) -> None:
        start = self.storage_state_path.text().strip() or str(Path.home())
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Choose a storage_state file", start, "JSON files (*.json);;All files (*)"
        )
        if chosen:
            self.storage_state_path.setText(chosen)

    @pyqtSlot()
    def _open_output_folder(self) -> None:
        folder = self.output_dir_input.text().strip()
        if not folder:
            QMessageBox.information(self, APP_NAME, "No destination folder has been chosen yet.")
            return
        self._open_folder(folder)

    def _open_folder(self, folder: str) -> None:
        """Open a folder in the desktop's file manager, with a headless fallback."""
        path = Path(folder).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, APP_NAME, f"Cannot create the folder:\n{exc}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            # Headless/Linux fallback so the action never appears broken.
            webbrowser.open(path.as_uri())

    @pyqtSlot()
    def _export_pdf_report(self) -> None:
        if not self._last_summary:
            QMessageBox.information(self, APP_NAME, "Run a capture first.")
            return
        start = str(Path(self._last_summary.output_dir or ".") / "report.pdf")
        target, _ = QFileDialog.getSaveFileName(self, "Save PDF report", start, "PDF files (*.pdf)")
        if not target:
            return
        try:
            from app.core import pdfreport

            pdfreport.build_pdf_report(
                self._last_summary,
                target,
                only_changed=self.pdf_only_changes.isChecked(),
                include_trend=self.pdf_include_trend.isChecked(),
            )
        except Exception as exc:  # noqa: BLE001 - report failure must not crash the UI
            QMessageBox.warning(self, APP_NAME, f"Could not write the PDF report:\n{exc}")
            self.append_log(LogLevel.ERROR, f"PDF export failed: {exc}")
            return
        self.status_bar.showMessage(f"PDF report saved to {target}")
        self.append_log(LogLevel.INFO, f"PDF report saved to {target}")

    @pyqtSlot()
    def _save_log_file(self) -> None:
        default_name = f"capture-log-{datetime.now():%Y%m%d-%H%M%S}.txt"
        start = str(Path(self.output_dir_input.text().strip() or str(Path.home())) / default_name)
        target, _ = QFileDialog.getSaveFileName(self, "Save log", start, "Text files (*.txt)")
        if not target:
            return
        try:
            Path(target).write_text(self.log_console.plain_snapshot(), encoding="utf-8")
            self.status_bar.showMessage(f"Log saved to {target}")
        except OSError as exc:
            QMessageBox.warning(self, APP_NAME, f"Could not write the log file:\n{exc}")

    @pyqtSlot()
    def _retry_failed_urls(self) -> None:
        failed = list(self._last_failed)
        if failed:
            self.url_input.setPlainText("\n".join(failed))

    @pyqtSlot()
    def _copy_failed_urls(self) -> None:
        from PyQt6.QtWidgets import QApplication

        failed = list(self._last_failed)
        clipboard = QApplication.clipboard()
        if clipboard is not None and failed:
            clipboard.setText("\n".join(failed))
            self.status_bar.showMessage(f"{len(failed)} failed URL(s) copied to the clipboard.")

    @pyqtSlot()
    def _install_browser(self) -> None:
        """Download the browser binaries, streaming installer output into the log."""
        from app.core.runtime import install_browser

        engine_name = self.browser_engine.currentText()
        self.install_browser_button.setEnabled(False)
        self.browser_chip.setText(f"Installing {engine_name}...")
        self.append_log(LogLevel.INFO, f"Installing {engine_name} (this can take a few minutes)...")

        def _run() -> BrowserStatus:
            install_browser(engine_name, on_line=lambda line: self._installer_lines.append(line))
            return detect_browser(engine_name)

        self._installer_lines: list[str] = []

        self._probe = _FnThread(_run, self)
        self._probe.done.connect(self._on_install_finished)
        self._probe.start()

        # The installer writes into a list on the worker thread; drain it here.
        drain = QTimer(self)
        drain.setInterval(400)

        def _drain() -> None:
            while self._installer_lines:
                self.append_log(LogLevel.INFO, self._installer_lines.pop(0))
            if self._probe is None or not self._probe.isRunning():
                drain.stop()

        drain.timeout.connect(_drain)
        drain.start()
        self._installer_drain = drain

    @pyqtSlot(object)
    def _on_install_finished(self, status: object) -> None:
        self.install_browser_button.setEnabled(True)
        self._on_browser_status(status)

    @pyqtSlot()
    def _on_format_changed(self) -> None:
        uses_quality = self.image_format.currentIndex() in (1, 2, 3)
        self.jpeg_quality.setEnabled(uses_quality)

    @pyqtSlot()
    def _on_verbosity_changed(self) -> None:
        self.log_console.set_minimum_level(self.verbosity_combo.currentData() or "DEBUG")

    @pyqtSlot(bool)
    def _on_autoscroll_toggled(self, checked: bool) -> None:
        self.log_console.autoscroll = checked

    # ------------------------------------------------------------------ #
    # updates
    # ------------------------------------------------------------------ #
    @pyqtSlot()
    @pyqtSlot()
    def _open_walk_report(self) -> None:
        """Show the newest walk: the log gets the text, the browser the pictures.

        A journey or a crawl already wrote a report next to its screenshots; this
        turns that report into one page with the thumbnails, so "where did it go
        and what did it see?" is answerable without opening a file manager.
        """
        from app.core import walkview

        folder = getattr(self, "_last_walk_root", "") or self.output_dir_input.text().strip()
        if not folder:
            QMessageBox.information(
                self, APP_NAME, "Run a journey or a crawl first (or choose a folder)."
            )
            return
        walk = walkview.latest_walk(folder)
        if not walk.found:
            QMessageBox.information(
                self,
                APP_NAME,
                f"No walk (journey or crawl) has been run in {folder} yet.",
            )
            return
        self.append_log(LogLevel.INFO, walk.summary().replace("\n", " | "))
        target = walkview.build_walk_report(folder)
        if target is None:  # pragma: no cover - just checked above
            return
        self.append_log(LogLevel.INFO, f"Walk report: {target}")
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(target))):
            webbrowser.open(Path(target).as_uri())

    def _open_history(self) -> None:
        from app.ui.history_dialog import HistoryDialog

        dialog = HistoryDialog(self.output_dir_input.text().strip(), self)
        dialog.exec()

    @pyqtSlot()
    def _check_updates(self) -> None:
        """Probe GitHub Releases off the GUI thread; never blocks or crashes."""
        self.update_button.setEnabled(False)
        self.status_bar.showMessage("Checking for updates...")
        self._update_probe = _FnThread(lambda: updater.check_for_updates(__version__), self)
        self._update_probe.done.connect(self._on_update_result)
        self._update_probe.start()

    @pyqtSlot(object)
    def _on_update_result(self, payload: object) -> None:
        self.update_button.setEnabled(True)
        if not isinstance(payload, dict):
            self.status_bar.showMessage("Could not check for updates.")
            return

        if payload.get("latest") is None:
            self.status_bar.showMessage("Could not check for updates (offline?).")
            return

        if payload.get("available"):
            self.status_bar.showMessage(
                f"Update available: {payload['latest']} - download from {payload['url']}"
            )
            self.append_log(
                LogLevel.INFO, f"Update available: {payload['latest']} ({payload['url']})"
            )
        else:
            self.status_bar.showMessage(f"You are on the latest version (v{__version__}).")

    # ------------------------------------------------------------------ #
    # theming
    # ------------------------------------------------------------------ #
    def _restore_theme(self) -> None:
        """Apply the persisted theme before the first paint."""
        name = self._qsettings.value("ui/theme", "dark")
        set_current(LIGHT if name == "light" else DARK)

    def _sync_theme_button(self) -> None:
        is_light = current() is LIGHT
        self.theme_button.setText(i18n.tr("theme_dark") if is_light else i18n.tr("theme_light"))
        self.theme_button.setToolTip(
            "Switch to the " + ("dark" if is_light else "light") + " theme"
        )

    # ------------------------------------------------------------------ #
    # language
    # ------------------------------------------------------------------ #
    def _retranslate(self) -> None:
        """Apply the active language: direction + every tracked string."""
        rtl = i18n.is_rtl()
        self.setLayoutDirection(
            Qt.LayoutDirection.RightToLeft if rtl else Qt.LayoutDirection.LeftToRight
        )
        self.app_subtitle_label.setText(i18n.tr("app_subtitle"))
        self.install_browser_button.setText(i18n.tr("install_browser"))
        self.update_button.setText(i18n.tr("check_updates"))
        self.history_button.setText(i18n.tr("history"))
        self.walk_button.setText(i18n.tr("walk"))
        self.language_button.setText(i18n.tr("language"))
        self.start_button.setText(i18n.tr("start"))
        self.stop_button.setText(i18n.tr("stop"))
        self.open_output_button.setText(i18n.tr("open_folder"))
        self.pdf_button.setText(i18n.tr("pdf_report"))
        self.action_hint.setText(i18n.tr("ready_hint"))
        self._sync_theme_button()
        for card in getattr(self, "_cards", []):
            label = getattr(card, "title_label", None)
            key = getattr(card, "title_key", "")
            if label is not None and key:
                label.setText(i18n.tr(key))

    @pyqtSlot(str)
    def _on_theme_selected(self, name: str) -> None:
        if not name:
            return
        set_by_name(name)
        self._apply_current_theme()
        self._qsettings.setValue("ui/theme", name)
        self._qsettings.sync()

    def _apply_font(self, settings: CaptureSettings) -> None:
        from PyQt6.QtGui import QFont
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            return
        font = QFont(app.font())
        if settings.font_family:
            font.setFamily(settings.font_family)
        if int(settings.font_size) > 0:
            font.setPointSize(int(settings.font_size))
        app.setFont(font)

    def _apply_language(self) -> None:
        self._retranslate()

    @pyqtSlot()
    def _toggle_language(self) -> None:
        new_lang = "fa" if i18n.current_language() == "en" else "en"
        i18n.set_language(new_lang)
        self._qsettings.setValue("ui/language", new_lang)
        self._qsettings.sync()
        self._retranslate()
        self.status_bar.showMessage("FA" if new_lang == "fa" else "EN")

    def _toggle_theme(self) -> None:
        set_current(LIGHT if current() is DARK else DARK)
        self._apply_current_theme()
        self._qsettings.setValue("ui/theme", theme_name())
        self._qsettings.sync()

    def _apply_current_theme(self) -> None:
        """Re-apply palette + stylesheet and refresh every hand-painted widget."""
        from PyQt6.QtWidgets import QApplication  # local import keeps the top light

        apply_app_theme(QApplication.instance(), current())
        self._sync_theme_button()
        self._refresh_dynamic_colors()

    def _refresh_dynamic_colors(self) -> None:
        """Widgets that paint themselves must be nudged after a theme switch."""
        from app.ui.widgets import ToggleSwitch  # noqa: PLC0415

        self.stat_done.set_tone("accent")
        self.stat_ok.set_tone("success")
        self.stat_failed.set_tone("error")
        self.stat_elapsed.set_tone("neutral")
        self._restyle_browser_chip()
        self._refresh_url_summary()

        for switch in self.findChildren(ToggleSwitch):
            switch.update()
        self.update()

    # ------------------------------------------------------------------ #
    # scheduling (auto-repeat)
    # ------------------------------------------------------------------ #
    @pyqtSlot(bool)
    def _on_auto_repeat_toggled(self, enabled: bool) -> None:
        self.auto_repeat_minutes.setEnabled(bool(enabled))
        self._arm_scheduler()

    @pyqtSlot(int)
    def _on_auto_repeat_interval_changed(self, _value: int) -> None:
        if self._schedule_timer.isActive():
            self._arm_scheduler()

    def _on_schedule_changed(self, *_args) -> None:
        """Re-arm the scheduler when either the daily toggle or the time changes."""
        self._arm_scheduler()

    def _arm_scheduler(self) -> None:
        """(Re)start the one-shot scheduler timer, or stop it if disabled."""
        self._schedule_timer.stop()
        if self._running:
            return

        if self.schedule_cron_toggle.isChecked():
            from app.core import cron

            expr = self.schedule_cron.text().strip()
            try:
                seconds = cron.seconds_until(expr)
            except Exception:  # noqa: BLE001 - invalid expression
                seconds = None
            if seconds is None:
                self.status_bar.showMessage("Invalid cron expression.")
                return
            self._schedule_timer.start(max(1000, int(seconds) * 1000))
            message = f"Cron armed - next run in {int(seconds // 60)} min."
            self.status_bar.showMessage(message)
            self.append_log(LogLevel.INFO, message)
            return

        if self.schedule_daily_toggle.isChecked():
            hhmm = self.schedule_time.time().toString("HH:mm")
            seconds = int(schedule.seconds_until_daily(hhmm))
            self._schedule_timer.start(max(1000, seconds * 1000))
            message = f"Daily run armed - next {schedule.format_daily_label(hhmm)}."
            self.status_bar.showMessage(message)
            self.append_log(LogLevel.INFO, message)
            return

        if not self.auto_repeat_toggle.isChecked():
            return
        minutes = max(1, int(self.auto_repeat_minutes.value()))
        self._schedule_timer.start(minutes * 60_000)
        what = self.scheduled_work()
        self.status_bar.showMessage(f"Auto-repeat armed - next run in {minutes} min.")
        self.append_log(
            LogLevel.INFO,
            f"Scheduler armed: next {what} in {minutes} minute(s).",
        )

    def scheduled_work(self) -> str:
        """What a scheduled tick will run: a batch, a journey or a crawl.

        The card decides; the schedule only says *when*. That is what lets one
        machine keep photographing a whole click-path every morning without
        anyone pressing Start.
        """
        if self.crawl_toggle.isChecked():
            return "crawl"
        if self.journey_text():
            return "journey"
        return "capture run"

    @pyqtSlot()
    def _on_schedule_tick(self) -> None:
        if self._running:
            return
        self.append_log(LogLevel.INFO, "Scheduled run starting.")
        self.start_capture()

    # ------------------------------------------------------------------ #
    # profiles
    # ------------------------------------------------------------------ #
    def _log_trend_summary(self, output_dir: str) -> None:
        """Append a compact per-site trend report after each (scheduled) run."""
        if not output_dir:
            return
        try:
            summary_text = history.trend_summary(output_dir)
        except Exception:  # noqa: BLE001 - trending is best effort
            return
        self.append_log(LogLevel.INFO, "Trend | " + summary_text.replace("\n", " | "))

    def _restore_queue(self) -> None:
        """Reload an unfinished batch left by a previous (crashed/closed) session."""
        pending = queue_store.load_queue(self._profiles_base)
        if not pending:
            return
        self.url_input.setPlainText("\n".join(pending))
        self._refresh_url_summary()
        self.append_log(
            LogLevel.INFO,
            f"Resumed {len(pending)} pending URL(s) from a previous session.",
        )

    def _refresh_profiles(self) -> None:
        current = self.profile_combo.currentText()
        names = profiles.list_profiles(self._profiles_base)
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems(names)
        index = self.profile_combo.findText(current)
        self.profile_combo.setCurrentIndex(max(0, index))
        self.profile_combo.blockSignals(False)

        has_any = bool(names)
        self.profile_apply.setEnabled(has_any)
        self.profile_delete.setEnabled(has_any)

    @pyqtSlot()
    def _save_profile_as(self) -> None:
        name, ok = QInputDialog.getText(self, APP_NAME, "Profile name:")
        if not ok or not name.strip():
            return
        profiles.save_profile(
            self._profiles_base,
            name,
            self.collect_settings().to_dict(),
            self.url_input.toPlainText(),
        )
        self._refresh_profiles()
        select = self.profile_combo.findText(profiles.list_profiles(self._profiles_base)[-1])
        if select >= 0:
            self.profile_combo.setCurrentIndex(select)
        self.status_bar.showMessage(f"Profile '{name}' saved.")
        self.append_log(LogLevel.INFO, f"Saved profile '{name}'.")

    @pyqtSlot()
    def _apply_profile(self) -> None:
        name = self.profile_combo.currentText()
        data = profiles.load_profile(self._profiles_base, name)
        if not data:
            self.status_bar.showMessage(f"Profile '{name}' could not be read.")
            return
        if isinstance(data.get("settings"), dict):
            self.apply_settings(CaptureSettings.from_dict(data["settings"]))
        self.url_input.setPlainText(str(data.get("urls") or ""))
        self._refresh_url_summary()
        self.status_bar.showMessage(f"Profile '{name}' applied.")
        self.append_log(LogLevel.INFO, f"Applied profile '{name}'.")

    @pyqtSlot()
    def _delete_profile(self) -> None:
        name = self.profile_combo.currentText()
        if profiles.delete_profile(self._profiles_base, name):
            self._refresh_profiles()
            self.status_bar.showMessage(f"Profile '{name}' deleted.")
            self.append_log(LogLevel.INFO, f"Deleted profile '{name}'.")

    # ------------------------------------------------------------------ #
    # -- drag & drop: a session file dropped on the window ---------------
    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt's name
        """Accept the drag when it carries a JSON file (a session, usually)."""
        urls = event.mimeData().urls() if event.mimeData() else []
        if any(str(url.toLocalFile()).lower().endswith(".json") for url in urls):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt's name
        """Import the first dropped JSON file as the session."""
        urls = event.mimeData().urls() if event.mimeData() else []
        for url in urls:
            local = str(url.toLocalFile())
            if local.lower().endswith(".json"):
                event.acceptProposedAction()
                self.import_session_file(local)
                return

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming convention
        """Persist the profile and make sure the worker thread is stopped."""
        self._save_settings()
        self._schedule_timer.stop()
        self._elapsed_timer.stop()

        if self._worker is not None:
            self._worker.request_stop()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(4000)
        if self._probe is not None and self._probe.isRunning():
            self._probe.quit()
            self._probe.wait(1000)

        super().closeEvent(event)

    # ------------------------------------------------------------------ #
    # helpers used by tests
    # ------------------------------------------------------------------ #
    @property
    def log_text(self) -> str:
        return self.log_console.plain_snapshot()

    def set_window_icon_from(self, path: str) -> None:
        """Attach an application icon if the file exists (packaged builds)."""
        icon_path = Path(path)
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))


    def open_browser_plugin(self) -> None:
        from PyQt6.QtWidgets import QDialog
        dlg = QDialog(self)
        dlg.setWindowTitle("Embedded Step Editor")
        dlg.resize(900, 600)
        dlg.show()
