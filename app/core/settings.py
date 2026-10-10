"""Capture configuration.

:class:`CaptureSettings` is a plain dataclass so it can be created in a unit
test, serialised into ``QSettings`` for persistence, or read from a future
CLI/config-file front end without any change.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from app.core import schedule

#: Browser engines supported by the bundled Playwright driver.
SUPPORTED_BROWSERS = ("chromium", "firefox", "webkit")

#: Chromium (and WebKit) cannot rasterise a single canvas taller than this in
#: *device* pixels. Taller pages are captured in segments and stitched.
MAX_CANVAS_PX = 16_384

#: Conservative per-segment height used when a page is too tall for one shot.
SEGMENT_PX = 8_192

AUTH_MODE_HTTP = "http"  # HTTP Basic / Digest handled by the browser itself
AUTH_MODE_FORM = "form"  # HTML login form filled in by the bot
AUTH_MODES = (AUTH_MODE_HTTP, AUTH_MODE_FORM)

#: Sensible CSS selectors tried in order when auto-detecting a login form.
DEFAULT_USERNAME_SELECTORS = (
    "input[type='email']",
    "input[name='username']",
    "input[name='user']",
    "input[name='login']",
    "input[name='email']",
    "input[id='username']",
    "input[id='email']",
    "input[id='user']",
    "input[type='text']",
)

DEFAULT_PASSWORD_SELECTORS = (
    "input[type='password']",
    "input[name='password']",
    "input[id='password']",
)

DEFAULT_SUBMIT_SELECTORS = (
    "button[type='submit']",
    "input[type='submit']",
    "form button",
    "button",
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class SettingsError(ValueError):
    """Raised when a :class:`CaptureSettings` instance is not usable."""


@dataclass
class CaptureSettings:
    """Everything the engine needs to capture a batch of URLs."""

    # --- output -----------------------------------------------------------
    output_dir: str = ""
    filename_prefix: str = ""
    image_format: str = "png"  # "png" | "jpeg" | "webp" | "avif"
    jpeg_quality: int = 92
    open_folder_when_done: bool = False

    # --- browser ----------------------------------------------------------
    browser: str = "chromium"
    headless: bool = True
    ignore_https_errors: bool = True
    extra_browser_args: str = ""  # comma separated, advanced users only

    # --- viewport / quality ----------------------------------------------
    viewport_width: int = 1920
    viewport_height: int = 1080
    device_scale_factor: float = 2.0  # 2.0 == "retina" sharpness
    user_agent: str = ""  # empty -> browser default
    locale: str = ""  # empty -> browser default
    timezone: str = ""  # empty -> browser default

    # --- timing -----------------------------------------------------------
    navigation_timeout_ms: int = 60_000
    network_idle_timeout_ms: int = 15_000
    settle_delay_ms: int = 1_500  # extra wait after load, before shot
    per_url_timeout_ms: int = 180_000

    # --- content ----------------------------------------------------------
    scroll_to_load_lazy_content: bool = True
    lazy_scroll_step_px: int = 800
    hide_scrollbars: bool = True
    #: CSS selectors hidden on every shot, comma or newline separated - for the
    #: things every page has and nobody wants in a diff: cookie banners, ad
    #: slots, a clock that changes every minute.
    hide_selectors: str = ""
    smart_proxy: bool = False  # auto-select proxy by geo
    profile_name: str = "default"
    profile_name: str = "default"  # multi-tenant isolation folder

    # --- authentication (optional) ---------------------------------------
    auth_enabled: bool = False
    auth_mode: str = AUTH_MODE_FORM
    username: str = ""
    password: str = ""
    login_username_selector: str = ""  # empty -> auto-detect
    login_password_selector: str = ""
    login_submit_selector: str = ""
    login_wait_ms: int = 4_000

    # --- resilience -------------------------------------------------------
    retries: int = 1  # extra attempts per URL
    retry_backoff_ms: int = 500  # base delay before the 1st retry
    retry_backoff_max_ms: int = 8_000  # cap for the exponential backoff
    watchdog_enabled: bool = True  # relaunch the browser if it disconnects
    # Warn when the newest capture is older than this many minutes, which means
    # the bot has been failing or not running at all (0 = off).
    watchdog_stale_minutes: int = 0
    language: str = "en"  # UI language: 'en' or 'fa'
    request_delay_ms: int = 0  # politeness delay between URLs
    respect_robots: bool = False  # skip URLs disallowed by robots.txt
    desktop_notifications: bool = False  # OS notification when a run finishes
    save_har: bool = False  # record a .har network log per URL
    max_per_host: int = 0  # parallel: cap simultaneous URLs per host (0=off)
    font_family: str = ""  # UI font family ('' = system)
    font_size: int = 0  # UI font point size (0 = default)
    stop_on_first_error: bool = False
    continue_on_http_error: bool = True
    max_concurrency: int = 1  # parallel capture workers (1 = serial)

    # --- change detection (monitoring) -----------------------------------
    change_detection_enabled: bool = False
    change_threshold: float = 0.05  # dHash ratio (0..1) that counts as "changed"

    # --- misc -------------------------------------------------------------
    write_log_file: bool = True
    write_report: bool = True  # CSV + JSON summary of the batch
    auto_dashboard: bool = False  # regenerate dashboard.html after each run

    # --- scheduling (UI-driven; ignored by the engine) --------------------
    auto_repeat_enabled: bool = False
    auto_repeat_minutes: int = 30
    schedule_daily_enabled: bool = False
    schedule_time: str = "09:00"  # HH:MM, used when daily mode is on
    schedule_cron_enabled: bool = False
    schedule_cron: str = ""  # 5-field cron, used when cron mode is on

    # --- change alerts -----------------------------------------------------
    alert_enabled: bool = False
    alert_webhook_url: str = ""
    # Payload shape the webhook expects: generic | slack | teams.
    alert_webhook_kind: str = "generic"
    alert_email_to: str = ""
    # Never alert about URLs (or labels) containing these fragments, separated by
    # commas or spaces - e.g. "staging, preview" (empty = alert about everything).
    alert_mute_urls: str = ""
    # Only alert when a change is at least this large (0 = alert on any change).
    change_alert_threshold: float = 0.0
    # Drop SQLite history rows older than this after each run (0 = keep forever).
    history_retention_days: int = 0
    # Delete capture images older than this after each run (0 = keep forever).
    screenshot_retention_days: int = 0
    # Also delete the oldest captures until the folder fits under this (0 = off).
    screenshot_retention_mb: int = 0
    # Keep the history itself (reports + SQLite index) under this many MB by
    # forgetting the oldest runs (0 = no cap, keep everything forever).
    history_retention_mb: int = 0
    # A size budget per site: "news.example.com=500, *=1000" (MB), where "*" is
    # every site without one of its own. The chatty host is what fills a disk, so
    # this trims that host instead of everything at once (empty = off).
    site_caps: str = ""
    # Zip the runs the size cap drops into archive-YYYY-MM.zip instead of losing
    # them for good (the archives are not counted by the cap, so it still bites).
    history_archive: bool = False
    # Warn when a pinned baseline is older than this (0 = never warn).
    baseline_max_age_days: int = 0

    # --- digest (per profile when saved) ----------------------------------
    digest_days: int = 7  # window the digest summarises
    digest_issue_number: int = 0  # tracking issue for the sticky comment (0 = none)
    # Alert when a capture drifts this far from its pinned baseline (0 = off).
    baseline_drift_alert_threshold: float = 0.0
    # Don't re-alert the same site more often than this (0 = no cooldown).
    alert_cooldown_minutes: int = 0
    # Hold alerts inside these windows and send them together afterwards, e.g.
    # "22:00-07:00" or "22:00-07:00, fri18:00-mon09:00" (empty = any hour).
    alert_quiet_hours: str = ""
    # Per-URL quiet hours: "fragment=windows; fragment=windows" (semicolon
    # separated). The first matching fragment wins; an empty window list means
    # "never quiet for this URL" (empty = every URL follows alert_quiet_hours).
    alert_quiet_urls: str = ""
    # Per-URL webhook routing: "fragment=https://target; fragment=https://target"
    # (semicolon separated, "*" matches everything). The first matching fragment
    # wins; changes nobody claims go to alert_webhook_url (empty = no routing).
    alert_route_urls: str = ""
    # A TOML file with one table per destination ([ops] kind/url/match/quiet/
    # mute/min_diff) - the readable version of the route/quiet/mute/threshold
    # settings above. When set it replaces them (empty = use the settings).
    alert_channels: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""

    # --- network & session -------------------------------------------------
    proxy_server: str = ""  # e.g. http://host:8080 (per-run)
    storage_state_path: str = ""  # Playwright storage_state JSON (cookies)

    def clone(self) -> CaptureSettings:
        """Return an independent copy (used to hand a snapshot to the worker)."""
        return CaptureSettings(**asdict(self))

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------
    def validate(self) -> None:
        """Raise :class:`SettingsError` describing the first problem found."""
        if not str(self.output_dir).strip():
            raise SettingsError("Choose a destination folder before starting.")

        if self.browser not in SUPPORTED_BROWSERS:
            raise SettingsError(
                f"Unknown browser engine '{self.browser}'. Use one of: {', '.join(SUPPORTED_BROWSERS)}."
            )

        if self.image_format not in ("png", "jpeg", "webp", "avif"):
            raise SettingsError("Image format must be 'png', 'jpeg', 'webp' or 'avif'.")

        if not 10 <= int(self.jpeg_quality) <= 100:
            raise SettingsError("JPEG quality must be between 10 and 100.")

        if not 320 <= int(self.viewport_width) <= 7_680:
            raise SettingsError("Viewport width must be between 320 and 7680 px.")

        if not 200 <= int(self.viewport_height) <= 7_680:
            raise SettingsError("Viewport height must be between 200 and 7680 px.")

        if not 0.5 <= float(self.device_scale_factor) <= 4.0:
            raise SettingsError("Device scale factor must be between 0.5 and 4.0.")

        if int(self.navigation_timeout_ms) < 1_000:
            raise SettingsError("Navigation timeout must be at least 1 000 ms.")

        if int(self.network_idle_timeout_ms) < 0:
            raise SettingsError("Network-idle timeout cannot be negative.")

        if int(self.settle_delay_ms) < 0:
            raise SettingsError("Settle delay cannot be negative.")

        if not 0 <= int(self.retries) <= 5:
            raise SettingsError("Retries must be between 0 and 5.")

        if int(self.retry_backoff_ms) < 0:
            raise SettingsError("Retry backoff cannot be negative.")

        if int(self.request_delay_ms) < 0:
            raise SettingsError("Request delay cannot be negative.")

        if int(self.max_per_host) < 0:
            raise SettingsError("Per-host limit cannot be negative.")

        if int(self.font_size) < 0:
            raise SettingsError("Font size cannot be negative.")

        if int(self.retry_backoff_max_ms) < int(self.retry_backoff_ms):
            raise SettingsError("Retry backoff cap must be >= the base delay.")

        if not 1 <= int(self.max_concurrency) <= 8:
            raise SettingsError("Parallel workers must be between 1 and 8.")

        if not 0.0 <= float(self.change_threshold) <= 1.0:
            raise SettingsError("Change threshold must be between 0 and 1.")

        if self.schedule_daily_enabled and schedule.parse_hhmm(self.schedule_time) is None:
            raise SettingsError("Daily schedule time must be in HH:MM format (e.g. 09:00).")

        if self.schedule_cron_enabled:
            from app.core import cron

            try:
                cron.parse(self.schedule_cron)
            except Exception as exc:
                raise SettingsError(f"Invalid cron expression: {exc}") from exc

        if not 1 <= int(self.smtp_port) <= 65535:
            raise SettingsError("SMTP port must be between 1 and 65535.")

        if self.alert_enabled and not (
            self.alert_webhook_url or (self.alert_email_to and self.smtp_host)
        ):
            raise SettingsError("Alerts need a webhook URL, or an email + SMTP host.")

        if str(self.alert_webhook_kind).lower() not in ("generic", "slack", "teams"):
            raise SettingsError("Webhook type must be generic, slack or teams.")

        if not 0.0 <= float(self.change_alert_threshold) <= 1.0:
            raise SettingsError("Change alert threshold must be between 0 and 1.")

        if int(self.alert_cooldown_minutes) < 0:
            raise SettingsError("Alert cooldown cannot be negative.")

        if str(self.alert_quiet_hours).strip():
            from app.core import quiet

            broken = quiet.invalid_windows(self.alert_quiet_hours)
            if broken:
                raise SettingsError(
                    f"Quiet hours must look like 22:00-07:00 or fri18:00-mon09:00 "
                    f"(got '{broken[0]}')."
                )

        if str(self.alert_quiet_urls).strip():
            from app.core import quiet

            broken_rules = quiet.invalid_rules(self.alert_quiet_urls)
            if broken_rules:
                raise SettingsError(
                    "Per-URL quiet hours must look like "
                    "staging.example.com=22:00-07:00; news.example.com= "
                    f"(got '{broken_rules[0]}')."
                )

        if str(self.alert_route_urls).strip():
            from app.core import alerts

            broken_routes = alerts.invalid_routes(self.alert_route_urls)
            if broken_routes:
                raise SettingsError(
                    "Alert routes must look like "
                    "staging.example.com=https://hooks.example.com/staging; "
                    f"*=https://hooks.example.com/all (got '{broken_routes[0]}')."
                )

        if str(self.alert_channels).strip():
            from app.core import channels

            try:
                channels.load_channels(self.alert_channels)
            except channels.ChannelError as exc:
                raise SettingsError(str(exc)) from exc

        if int(self.history_retention_days) < 0:
            raise SettingsError("History retention cannot be negative.")

        if int(self.screenshot_retention_days) < 0:
            raise SettingsError("Screenshot retention cannot be negative.")

        if int(self.screenshot_retention_mb) < 0:
            raise SettingsError("Screenshot size cap cannot be negative.")

        if int(self.history_retention_mb) < 0:
            raise SettingsError("History size cap cannot be negative.")

        if str(self.site_caps or "").strip():
            from app.core.retention import SiteCapError, parse_site_caps

            try:
                parse_site_caps(self.site_caps)
            except SiteCapError as exc:
                raise SettingsError(
                    f"site_caps expects 'host=MB' pairs separated by commas: {exc}"
                ) from None

        if int(self.watchdog_stale_minutes) < 0:
            raise SettingsError("Watchdog age cannot be negative.")

        if int(self.baseline_max_age_days) < 0:
            raise SettingsError("Baseline max age cannot be negative.")

        if int(self.digest_days) < 1:
            raise SettingsError("Digest window must be at least one day.")

        if int(self.digest_issue_number) < 0:
            raise SettingsError("Digest issue number cannot be negative.")

        if not 0.0 <= float(self.baseline_drift_alert_threshold) <= 1.0:
            raise SettingsError("Baseline drift threshold must be between 0 and 1.")

        if self.proxy_server.strip() and "://" not in self.proxy_server:
            raise SettingsError("Proxy must include a scheme, e.g. http://host:8080")

        from app.core import i18n

        if self.language not in i18n.SUPPORTED:
            raise SettingsError("Language must be one of: " + ", ".join(i18n.SUPPORTED))

        if int(self.lazy_scroll_step_px) < 100:
            raise SettingsError("Lazy-load scroll step must be at least 100 px.")

        if not 1 <= int(self.auto_repeat_minutes) <= 1440:
            raise SettingsError("Auto-repeat interval must be between 1 and 1440 minutes.")

        if self.auth_enabled:
            if self.auth_mode not in AUTH_MODES:
                raise SettingsError(f"Authentication mode must be one of: {', '.join(AUTH_MODES)}.")
            if not str(self.username).strip():
                raise SettingsError("Login is enabled but no username was supplied.")

    # ------------------------------------------------------------------
    # derived helpers
    # ------------------------------------------------------------------
    @property
    def output_path(self) -> Path:
        return Path(self.output_dir).expanduser()

    @property
    def browser_args(self) -> list[str]:
        """Extra Chromium switches, with the quality defaults always applied."""
        args = [
            "--force-color-profile=srgb",
            "--font-render-hinting=none",
            "--disable-lcd-text",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
        ]
        if self.hide_scrollbars:
            args.append("--hide-scrollbars")
        for chunk in (self.extra_browser_args or "").split(","):
            item = chunk.strip()
            if item:
                args.append(item)
        return args

    @property
    def effective_device_scale_factor(self) -> float:
        """Device scale factor actually usable for a one-shot capture.

        A 20 000 px page at scale 2 would need a 40 000 px canvas, which the
        GPU refuses. The engine falls back to segmented capture in that case,
        but keeping a safe factor avoids pointless first attempts.
        """
        return max(0.5, float(self.device_scale_factor))

    def file_extension(self) -> str:
        return {"jpeg": "jpg", "webp": "webp", "avif": "avif"}.get(self.image_format, "png")

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CaptureSettings:
        """Build settings from a mapping, ignoring unknown keys.

        Values coming from ``QSettings`` are strings, so every known field is
        coerced back to its declared type. Unknown keys are skipped silently
        so that a config written by a newer build never breaks an older one.
        """
        kwargs: dict[str, Any] = {}
        for spec in fields(cls):
            if spec.name not in data:
                continue
            value = data[spec.name]
            if value is None:
                continue
            kwargs[spec.name] = _coerce(value, spec.type)
        return cls(**kwargs)


def _coerce(value: Any, type_name: Any) -> Any:
    """Best-effort conversion of ``value`` to the type named by ``type_name``."""
    name = (
        type_name if isinstance(type_name, str) else getattr(type_name, "__name__", str(type_name))
    )
    if name == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if name == "int":
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return 0
    if name == "float":
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return 1.0
    return str(value)


def default_settings() -> CaptureSettings:
    """The settings the application shows on a brand-new install."""
    return CaptureSettings(
        output_dir=str(Path.home() / "Pictures" / "FullPageCaptures"),
        user_agent="",
    )
