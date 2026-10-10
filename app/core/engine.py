"""Playwright capture engine (Qt-free).

The engine is deliberately structured as *one* class with small, single-purpose
helpers so that every step can be tested with a fake browser:

    CaptureEngine.run(urls)
        -> _launch()                    start the browser once per batch
        -> for each url: _capture_one()
              goto -> network-idle -> login -> settle -> lazy scroll
              -> measure -> screenshot (or stitch segments) -> write file
        -> CaptureSummary

No exception is allowed to escape :meth:`CaptureEngine.run` except
:class:`BrowserNotInstalledError`, which the caller must surface because no
further work is possible without a browser binary.
"""

from __future__ import annotations

import csv
import io
import json
import queue
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from app.core import alerts, history
from app.core.baseline import baseline_age_days, baseline_drift, find_baseline, stale_baselines
from app.core.imagediff import diff_ratio
from app.core.retention import (
    HistoryPrune,
    apply_site_caps,
    prune_history,
    prune_screenshots,
    prune_screenshots_by_size,
)
from app.core.settings import (
    AUTH_MODE_FORM,
    AUTH_MODE_HTTP,
    DEFAULT_PASSWORD_SELECTORS,
    DEFAULT_SUBMIT_SELECTORS,
    DEFAULT_USERNAME_SELECTORS,
    MAX_CANVAS_PX,
    SEGMENT_PX,
    CaptureSettings,
)
from app.core.status import STALE, verdict
from app.core.url_utils import (
    build_filename,
    build_url_label,
    unique_path,
    validate_url,
)


class LogLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


class CaptureStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class EngineError(RuntimeError):
    """Base class for engine-level failures."""


class BrowserNotInstalledError(EngineError):
    """The Playwright browser binary is missing.

    Carries the exact command the user has to run, because this is by far the
    most common first-run problem with a packaged Playwright application.
    """

    def __init__(self, browser: str) -> None:
        self.browser = browser
        super().__init__(
            f"The {browser} browser binary required by Playwright is not installed.\n"
            f"Fix it with:  python -m playwright install {browser}"
        )


@dataclass
class CaptureResult:
    """Outcome of a single URL."""

    index: int
    url: str
    status: CaptureStatus
    message: str = ""
    file_path: str = ""
    duration_ms: int = 0
    http_status: int | None = None
    page_width_px: int = 0
    page_height_px: int = 0
    stitched: bool = False
    attempts: int = 1
    unchanged: bool = False  # change detection: below the threshold
    diff: float | None = None  # normalised visual difference (0..1)
    console_errors: list = field(default_factory=list)
    page_errors: list = field(default_factory=list)
    failed_requests: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status is CaptureStatus.SUCCESS


@dataclass
class CaptureSummary:
    """Aggregated outcome of a batch run."""

    results: list[CaptureResult] = field(default_factory=list)
    output_dir: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    stopped_by_user: bool = False

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.results if r.status is CaptureStatus.SUCCESS)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status is CaptureStatus.FAILED)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status is CaptureStatus.SKIPPED)

    @property
    def elapsed_ms(self) -> int:
        return max(0, int((self.ended_at - self.started_at) * 1000))

    def headline(self) -> str:
        return (
            f"{self.succeeded} captured, {self.failed} failed, "
            f"{self.skipped} skipped of {self.total}"
        )


#: Signature of the log callback: ``(LogLevel, str) -> None``.
LogCallback = Callable[[LogLevel, str], None]
#: Signature of the progress callback: ``(done, total, label) -> None``.
ProgressCallback = Callable[[int, int, str], None]
#: Signature of the per-URL callback: ``(CaptureResult) -> None``.
ResultCallback = Callable[[CaptureResult], None]


def _default_playwright_factory():
    """Import lazily so that the module loads even without Playwright present."""
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    return sync_playwright()


class CaptureEngine:
    """Runs a batch of full-page screenshots.

    Parameters
    ----------
    settings:
        Validated :class:`~app.core.settings.CaptureSettings`.
    playwright_factory:
        Zero-argument callable returning a Playwright context manager.
        Injected in tests; defaults to ``playwright.sync_api.sync_playwright``.
    """

    def __init__(
        self,
        settings: CaptureSettings,
        playwright_factory: Callable[[], Any] | None = None,
        log: LogCallback | None = None,
        progress: ProgressCallback | None = None,
        on_result: ResultCallback | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.settings = settings
        self._playwright_factory = playwright_factory or _default_playwright_factory
        self._log_cb = log or (lambda level, message: None)
        self._progress_cb = progress or (lambda done, total, label: None)
        self._result_cb = on_result or (lambda result: None)
        self._clock = clock
        self._now = now  # wall clock for quiet hours / cooldowns (injected in tests)

        self._stop = threading.Event()
        self._log_lines: list[str] = []
        self._robots_cache: dict[str, Any] = {}
        self._host_sems: dict[str, Any] = {}
        self._host_sem_lock = threading.Lock()
        self._image_format: str = settings.image_format
        self._drift_cache: dict[str, float | None] = {}

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def request_stop(self) -> None:
        """Ask the engine to finish the current URL and stop the batch."""
        self._stop.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    @property
    def log_lines(self) -> list[str]:
        """Plain-text copy of everything logged during the run (for the log file)."""
        return list(self._log_lines)

    def log(self, level: LogLevel, message: str) -> None:
        self._log_lines.append(f"[{level.value.upper()}] {message}")
        try:
            self._log_cb(level, message)
        except Exception:  # pragma: no cover - a broken UI sink must not kill the run
            pass

    def run(self, urls: Sequence[str]) -> CaptureSummary:
        """Capture every URL in ``urls``; returns a summary, never raises.

        The only exception that escapes is :class:`BrowserNotInstalledError`.
        """
        settings = self.settings
        summary = CaptureSummary(output_dir=settings.output_dir, started_at=time.time())
        total = len(urls)

        self.log(LogLevel.INFO, f"Starting batch: {total} URL(s) -> {settings.output_dir}")
        self._drift_cache.clear()
        self._resolve_image_format()
        if not settings.headless:
            self.log(LogLevel.INFO, "Headed mode: a real browser window will open.")

        if total == 0:
            self.log(LogLevel.WARNING, "No URLs to capture.")
            summary.ended_at = time.time()
            return summary

        output_dir = None
        log_file = None
        playwright_ctx = None
        browser = None
        try:
            output_dir = self._prepare_output_dir()
            if settings.write_log_file:
                log_file = self._open_log_file(output_dir)

            if settings.max_concurrency > 1 and total > 1:
                self._run_parallel(list(urls), summary, output_dir, total)
            else:
                playwright_ctx = self._playwright_factory()
                playwright = playwright_ctx.__enter__()
                browser = self._launch(playwright)
                self.log(
                    LogLevel.INFO,
                    f"Browser ready: {settings.browser} "
                    f"({'headless' if settings.headless else 'headed'}, "
                    f"{settings.viewport_width}x{settings.viewport_height}"
                    f" @{settings.effective_device_scale_factor:g}x)",
                )

                for _priority, index, raw_url in self._ordered_urls(urls):
                    if self._stop.is_set():
                        result = CaptureResult(
                            index=index,
                            url=raw_url,
                            status=CaptureStatus.SKIPPED,
                            message="Skipped because the run was stopped.",
                        )
                        self._finish_result(summary, result, total, index)
                        continue

                    self._rate_limit_sleep()
                    if settings.watchdog_enabled:
                        browser = self._ensure_healthy_browser(browser, playwright)
                    result = self._capture_with_retries(browser, raw_url, index, output_dir)
                    self._finish_result(summary, result, total, index)

                    if result.status is CaptureStatus.FAILED and settings.stop_on_first_error:
                        self.log(
                            LogLevel.ERROR, "Stopping early: 'stop on first error' is enabled."
                        )
                        self.request_stop()

        except BrowserNotInstalledError:
            # Nothing else can be done - re-raise so the UI can explain it.
            self._close_browser(browser)
            self._close_context(playwright_ctx)
            raise
        except Exception as exc:  # pragma: no cover - unexpected driver failure
            self.log(LogLevel.ERROR, f"Fatal engine error: {exc.__class__.__name__}: {exc}")
        finally:
            self._close_browser(browser)
            self._close_context(playwright_ctx)
            summary.ended_at = time.time()
            summary.stopped_by_user = self._stop.is_set()
            self.log(
                LogLevel.INFO, f"Finished. {summary.headline()} in {summary.elapsed_ms / 1000:.1f}s"
            )
            if settings.write_report and output_dir is not None:
                self._write_reports(summary, output_dir)
                self._warn_stale_baselines([r.url for r in summary.results], output_dir)
            if output_dir is not None:
                self._prune_old_screenshots(output_dir)
                self._prune_history(output_dir)
                self._watch_history(output_dir)
            self._close_log_file(log_file)

        summary.results.sort(key=lambda result: result.index)
        self._maybe_send_alerts(summary.results)
        self._maybe_send_heartbeats()
        return summary

    def _maybe_send_alerts(self, results: list[CaptureResult]) -> None:
        """Fire change alerts (webhook/email) for pages that changed.

        A page counts as changed when change detection produced a non-None diff
        at or above the threshold (i.e. a baseline existed and it moved), or when
        its drift against a pinned baseline meets the drift threshold. The alert
        layer never raises, so a broken endpoint can't fail the run.
        """
        if not self.settings.alert_enabled:
            return
        # Only pages whose change is at least this large should page someone
        # (0 = alert on any detected change).
        min_diff = float(getattr(self.settings, "change_alert_threshold", 0.0) or 0.0)
        changed = [
            {
                "label": build_url_label(r.url),
                "url": r.url,
                "diff": r.diff,
                "file": r.file_path,
            }
            for r in results
            if r.status is CaptureStatus.SUCCESS
            and r.diff is not None
            and not r.unchanged
            and r.diff >= min_diff
        ]
        # Second gate: drift against a pinned baseline, even when change
        # detection is off (then no per-run diff exists at all).
        changed.extend(self._drift_alert_items(results, exclude={item["url"] for item in changed}))

        channels_file = str(getattr(self.settings, "alert_channels", "") or "").strip()
        if channels_file:
            # The channels file owns the destination rules (match/mute/quiet/
            # min_diff), so the mute list and the global quiet hours below are
            # skipped: routing one change to four destinations with one schedule
            # is the whole point of naming them.
            changed = [*self._release_channel_queue(channels_file), *changed]
        else:
            # Sites the user never wants to be paged about (staging hosts, previews).
            changed, muted = alerts.split_muted(
                changed, str(getattr(self.settings, "alert_mute_urls", "") or "")
            )
            if muted:
                self.log(LogLevel.INFO, f"{len(muted)} change(s) muted by the alert mute list.")

            # Quiet hours run before the "nothing changed" exit: a silent morning
            # run still has to empty last night's queue.
            quiet_hours = str(getattr(self.settings, "alert_quiet_hours", "") or "")
            if quiet_hours:
                ready = self._handle_quiet_hours(changed, quiet_hours)
                if ready is None:
                    return
                changed = ready

        if not changed:
            return

        cooldown = int(getattr(self.settings, "alert_cooldown_minutes", 0) or 0)
        if cooldown > 0:
            changed = self._filter_by_cooldown(changed, cooldown)
            if not changed:
                self.log(LogLevel.INFO, "Change(s) suppressed by the alert cooldown.")
                return

        for channel, ok in self._dispatch_alerts(changed):
            self.log(
                LogLevel.INFO if ok else LogLevel.WARNING,
                f"Alert ({channel}) {'sent' if ok else 'failed'}.",
            )

        if cooldown > 0:
            self._record_alerts([item["url"] for item in changed])

    def _dispatch_alerts(self, changed: list[dict]) -> list[tuple[str, bool]]:
        """Send one batch of changes: the channels file, else the settings.

        The file's owner gets the engine's clock, so a quiet window is decided
        against the same ``now`` the queue was released with (and a test can fix
        the time of day instead of waiting for the night).
        """
        channels_file = str(getattr(self.settings, "alert_channels", "") or "").strip()
        if not channels_file:
            return alerts.notify(self.settings, changed)
        from app.core import channels as channels_module

        try:
            return channels_module.notify(self.settings, changed, moment=self._now())
        except Exception:  # noqa: BLE001 - alerting must never break a run
            return [("channels", False)]

    def _release_channel_queue(self, channels_file: str) -> list[dict]:
        """Take back the queued alerts whose channel is not quiet any more.

        Each queued item remembers the channel that held it, so the window that
        ends at 07:00 releases its own alerts without waking a channel that is
        still sleeping. A file that stopped parsing releases nothing (the run
        logs a warning and carries on).
        """
        from app.core import channels as channels_module
        from app.core import quiet

        try:
            parsed = channels_module.load_channels(channels_file)
        except channels_module.ChannelError as exc:
            self.log(LogLevel.WARNING, f"Channels file ignored: {exc}")
            return []

        now = self._now()
        path = quiet.queue_path(self.settings.output_dir or ".")
        due: list[dict] = []
        still: list[dict] = []
        for item in quiet.take(path):
            tags = item.get("channels") or []
            holding = [
                channel
                for channel in parsed
                if channel.name in tags and channel.quiet and quiet.in_window(now, channel.quiet)
            ]
            if holding:
                still.append(item)
            else:
                due.append(item)
        if still:
            quiet.append(path, still)
        if due:
            names = sorted({tag for item in due for tag in (item.get("channels") or [])})
            self.log(
                LogLevel.INFO,
                f"Quiet hours over for {len(due)} queued alert(s) "
                f"({', '.join(names) or 'unsorted'}).",
            )
        return due

    def _maybe_send_heartbeats(self) -> None:
        """Say "still here" for any channel whose heartbeat moment came and went.

        This runs even when nothing changed: the whole point of a heartbeat is the
        week where *nothing* happened, which would otherwise be indistinguishable
        from a webhook that stopped working.
        """
        if not self.settings.alert_enabled:
            return
        if not str(getattr(self.settings, "alert_channels", "") or "").strip():
            return
        from app.core import channels as channels_module

        try:
            results = channels_module.run_heartbeats(self.settings, moment=self._now())
        except Exception:  # noqa: BLE001 - a heartbeat must never break a run
            self.log(LogLevel.DEBUG, "Heartbeat check skipped.")
            return
        for label, ok in results:
            self.log(
                LogLevel.INFO if ok else LogLevel.WARNING,
                f"Alert ({label}) {'sent' if ok else 'failed'}.",
            )

    def _handle_quiet_hours(self, changed: list[dict], quiet_hours: str) -> list[dict] | None:
        """Queue alerts inside quiet hours, flush the queue outside them.

        Returns the items to send, or ``None`` when this run must stay silent.
        The decision is per URL: a per-URL rule (``alert_quiet_urls``) can hold
        one host on its own schedule, so a staging site can sleep on the weekend
        while a newsletter page keeps paging. Anything still queued is merged
        in, so the first free run sends one message for the whole night.
        """
        from app.core import quiet

        now = self._now()
        path = quiet.queue_path(self.settings.output_dir or ".")
        rules = quiet.compile_rules(str(getattr(self.settings, "alert_quiet_urls", "") or ""))

        def held_back(item: dict) -> bool:
            return quiet.silent_now(now, item.get("url", ""), quiet_hours, rules)

        held = [item for item in changed if held_back(item)]
        live = [item for item in changed if not held_back(item)]
        if held:
            waiting = quiet.append(path, held)
            suffix = ""
            if not rules:  # a single schedule: the end of the window is the answer
                until = quiet.window_end(now, quiet_hours)
                suffix = f" (until {until.strftime('%H:%M')})" if until else ""
            self.log(
                LogLevel.INFO,
                f"Quiet hours{suffix}: {len(held)} alert(s) queued, {waiting} waiting.",
            )

        due: list[dict] = []
        still: list[dict] = []
        for item in quiet.take(path):
            (still if held_back(item) else due).append(item)
        if still:
            quiet.append(path, still)
        ready = [*due, *live]
        if due:
            self.log(
                LogLevel.INFO,
                f"Quiet hours over: {len(due)} queued alert(s) sent with this run.",
            )
        return quiet.merge(ready) if ready else None

    def _drift_for(self, output_dir: Path, url: str) -> float | None:
        """Drift vs the pinned baseline for ``url``, computed once per run.

        ``None`` when the site has no pinned baseline (or the images cannot be
        compared), so this is cheap for sites that were never pinned.
        """
        if url not in self._drift_cache:
            try:
                self._drift_cache[url] = baseline_drift(output_dir, url)
            except Exception:  # noqa: BLE001 - a broken baseline must not break a run
                self._drift_cache[url] = None
        return self._drift_cache[url]

    def _drift_alert_items(self, results: list[CaptureResult], exclude: set[str]) -> list[dict]:
        """Items whose drift vs a pinned baseline meets the drift threshold.

        ``exclude`` holds the URLs already reported as changed in this run, so a
        page is never paged twice for the same capture.
        """
        threshold = float(getattr(self.settings, "baseline_drift_alert_threshold", 0.0) or 0.0)
        if threshold <= 0:
            return []
        output_dir = Path(self.settings.output_dir or ".")
        items: list[dict] = []
        for result in results:
            if result.status is not CaptureStatus.SUCCESS or result.url in exclude:
                continue
            drift = self._drift_for(output_dir, result.url)
            if drift is None or drift < threshold:
                continue
            self.log(
                LogLevel.WARNING,
                f"[baseline] {build_url_label(result.url)} drifted {drift:.0%} from its "
                f"pinned baseline (>= {threshold:.0%}).",
            )
            items.append(
                {
                    "label": build_url_label(result.url),
                    "url": result.url,
                    "diff": drift,
                    "file": result.file_path,
                    "reason": "baseline_drift",
                }
            )
        return items

    def _alert_db(self) -> Path:
        return Path(self.settings.output_dir or ".") / "history.sqlite3"

    def _filter_by_cooldown(self, changed: list[dict], cooldown_minutes: int) -> list[dict]:
        """Drop sites that were already alerted within the cooldown window."""
        from app.core.store import HistoryStore

        now = self._now()
        try:
            with HistoryStore(self._alert_db()) as store:
                allowed = []
                for item in changed:
                    last = store.last_alert_at(item["url"])
                    if last:
                        try:
                            elapsed = now - datetime.fromisoformat(last)
                        except ValueError:
                            elapsed = None
                        if elapsed is not None and elapsed < timedelta(minutes=cooldown_minutes):
                            continue
                    allowed.append(item)
                return allowed
        except Exception:  # noqa: BLE001 - cooldown is best-effort
            return changed

    def _record_alerts(self, urls: list[str]) -> None:
        """Remember that we just alerted these sites (for the cooldown window)."""
        from app.core.store import HistoryStore

        stamp = datetime.now().isoformat(timespec="seconds")
        try:
            with HistoryStore(self._alert_db()) as store:
                for url in urls:
                    store.record_alert(url, stamp)
        except Exception:  # noqa: BLE001 - cooldown is best-effort
            pass

    # ------------------------------------------------------------------
    # parallel capture
    # ------------------------------------------------------------------
    def _run_parallel(
        self,
        urls: list[str],
        summary: CaptureSummary,
        output_dir: Path,
        total: int,
    ) -> None:
        """Capture ``urls`` using a pool of worker threads.

        Playwright's synchronous API is not thread-safe on a shared connection,
        so every worker owns its *own* browser instance. Results are collected
        under a lock and re-sorted by index at the end, so the summary (and the
        CSV/JSON reports) keep a stable, human order regardless of completion
        order.
        """
        settings = self.settings
        workers = max(1, min(int(settings.max_concurrency), total))
        self.log(LogLevel.INFO, f"Parallel mode: {workers} worker(s) for {total} URL(s).")

        work: queue.Queue = queue.PriorityQueue()
        for priority, index, url in self._normalize_urls(urls):
            work.put((-priority, index, url))

        lock = threading.Lock()
        state = {"completed": 0, "fatal": None}

        def worker_thread() -> None:
            playwright_ctx = None
            browser = None
            try:
                playwright_ctx = self._playwright_factory()
                playwright = playwright_ctx.__enter__()
                browser = self._launch(playwright)

                while True:
                    try:
                        _priority, index, url = work.get_nowait()
                    except queue.Empty:
                        break

                    if self._stop.is_set() or state["fatal"] is not None:
                        result = CaptureResult(
                            index=index,
                            url=url,
                            status=CaptureStatus.SKIPPED,
                            message="Skipped because the run was stopped.",
                        )
                    else:
                        self._rate_limit_sleep()
                        if settings.watchdog_enabled:
                            browser = self._ensure_healthy_browser(browser, playwright)
                        if settings.max_per_host > 0:
                            from urllib.parse import urlparse

                            sem = self._host_semaphore(urlparse(url).netloc)
                            sem.acquire()
                            try:
                                result = self._capture_with_retries(browser, url, index, output_dir)
                            finally:
                                sem.release()
                        else:
                            result = self._capture_with_retries(browser, url, index, output_dir)

                    self._record_parallel(summary, result, state, lock, total)

                    if result.status is CaptureStatus.FAILED and settings.stop_on_first_error:
                        self.log(
                            LogLevel.ERROR, "Stopping early: 'stop on first error' is enabled."
                        )
                        self.request_stop()
            except BrowserNotInstalledError as exc:
                with lock:
                    state["fatal"] = exc
            except Exception as exc:  # pragma: no cover - a worker must not kill the batch
                self.log(LogLevel.ERROR, f"Worker error: {exc.__class__.__name__}: {exc}")
            finally:
                self._close_browser(browser)
                self._close_context(playwright_ctx)

        threads = [
            threading.Thread(target=worker_thread, name=f"capture-{n}", daemon=True)
            for n in range(workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        if state["fatal"] is not None:
            raise state["fatal"]

        summary.results.sort(key=lambda result: result.index)

    def _record_parallel(
        self,
        summary: CaptureSummary,
        result: CaptureResult,
        state: dict,
        lock: threading.Lock,
        total: int,
    ) -> None:
        """Append one result thread-safely and publish progress outside the lock."""
        with lock:
            summary.results.append(result)
            state["completed"] += 1
            done = state["completed"]
        current = result.url

        try:
            self._result_cb(result)
        except Exception:  # pragma: no cover
            pass
        self._progress_cb(done, total, current)

    # ------------------------------------------------------------------
    # batch plumbing
    # ------------------------------------------------------------------
    def _finish_result(
        self,
        summary: CaptureSummary,
        result: CaptureResult,
        total: int,
        index: int,
    ) -> None:
        summary.results.append(result)
        try:
            self._result_cb(result)
        except Exception:  # pragma: no cover
            pass
        self._progress_cb(index, total, result.url)

    def _prepare_output_dir(self) -> Path:
        output_dir = self.settings.output_path
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            self.log(LogLevel.DEBUG, f"Output folder: {output_dir}")
        except OSError as exc:
            raise EngineError(f"Cannot create the destination folder {output_dir}: {exc}") from exc
        return output_dir

    def _open_log_file(self, output_dir: Path):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = output_dir / f"capture-log-{stamp}.txt"
        try:
            handle = path.open("a", encoding="utf-8")
            self.log(LogLevel.DEBUG, f"Writing log file: {path.name}")
            return handle
        except OSError as exc:
            self.log(
                LogLevel.WARNING, f"Could not open the log file ({exc}); continuing without it."
            )
            return None

    def _close_log_file(self, handle) -> None:
        if handle is None:
            return
        try:
            handle.write("\n".join(self._log_lines) + "\n")
            handle.flush()
            handle.close()
        except OSError:  # pragma: no cover
            pass

    # ------------------------------------------------------------------
    # reporting (CSV + JSON)
    # ------------------------------------------------------------------
    def _write_reports(self, summary: CaptureSummary, output_dir: Path) -> None:
        """Write a machine-readable summary of the batch next to the images.

        Two files are produced, ``capture-report-<stamp>.json`` and
        ``capture-report-<stamp>.csv``, so results can be consumed by scripts
        or opened directly in Excel.
        """
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        rows = [self._result_row(result) for result in summary.results]
        # Record how far each pinned site has wandered, so the dashboard can plot
        # the drift over time instead of only showing today's value.
        for row in rows:
            if row["status"] == CaptureStatus.SUCCESS.value:
                row["drift"] = self._drift_for(output_dir, row["url"])

        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "output_dir": str(output_dir),
            "totals": {
                "total": summary.total,
                "succeeded": summary.succeeded,
                "failed": summary.failed,
                "skipped": summary.skipped,
                "elapsed_ms": summary.elapsed_ms,
                "stopped_by_user": summary.stopped_by_user,
            },
            "results": rows,
        }

        json_path = output_dir / f"capture-report-{stamp}.json"
        csv_path = output_dir / f"capture-report-{stamp}.csv"

        try:
            json_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            self.log(LogLevel.DEBUG, f"Report written: {json_path.name}")
        except OSError as exc:
            self.log(LogLevel.WARNING, f"Could not write the JSON report: {exc}")

        try:
            with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(self._REPORT_COLUMNS))
                writer.writeheader()
                writer.writerows(rows)
            self.log(LogLevel.DEBUG, f"Report written: {csv_path.name}")
        except OSError as exc:
            self.log(LogLevel.WARNING, f"Could not write the CSV report: {exc}")

        # Best-effort SQLite index so trend queries stay fast as history grows.
        try:
            from app.core import dashboard
            from app.core.store import HistoryStore

            with HistoryStore(output_dir / "history.sqlite3") as store:
                store.add_run(payload["generated_at"], rows)
                # One row per run describing the *folder* (not the pages): the
                # storage trend and its forecast are then measured, not guessed.
                store.add_storage_sample(
                    payload["generated_at"],
                    dashboard.storage_stats(output_dir, self._storage_caps()),
                )
                retention = int(getattr(self.settings, "history_retention_days", 0) or 0)
                if retention > 0:
                    removed = store.prune(retention)
                    if removed:
                        store.vacuum()
                        self.log(
                            LogLevel.INFO,
                            f"History index pruned: {removed} row(s) older than {retention} day(s).",
                        )
        except Exception:  # noqa: BLE001 - the index is best-effort, never fatal
            self.log(LogLevel.DEBUG, "History index unavailable; skipped.")

        # Optionally refresh the static HTML dashboard so it is always current.
        if self.settings.auto_dashboard:
            try:
                from app.core.dashboard import build_dashboard

                build_dashboard(
                    output_dir,
                    output_dir / "dashboard.html",
                    baseline_max_age_days=int(
                        getattr(self.settings, "baseline_max_age_days", 0) or 0
                    ),
                    caps=self._storage_caps(),
                )
                self.log(LogLevel.DEBUG, "Dashboard written: dashboard.html")
            except Exception:  # noqa: BLE001 - the dashboard is best-effort
                self.log(LogLevel.DEBUG, "Dashboard generation skipped.")

    def _storage_caps(self) -> dict[str, int]:
        """The configured size caps, in the shape the storage panel expects."""
        return {
            "screenshots_mb": int(getattr(self.settings, "screenshot_retention_mb", 0) or 0),
            "history_mb": int(getattr(self.settings, "history_retention_mb", 0) or 0),
            "site_caps": str(getattr(self.settings, "site_caps", "") or ""),
        }

    _REPORT_COLUMNS = (
        "index",
        "url",
        "status",
        "drift",
        "file_path",
        "message",
        "duration_ms",
        "http_status",
        "width_px",
        "height_px",
        "attempts",
        "unchanged",
        "diff",
        "console_errors",
        "page_errors",
        "failed_requests",
    )

    @staticmethod
    def _result_row(result: CaptureResult) -> dict:
        return {
            "index": result.index,
            "url": result.url,
            "status": result.status.value,
            "file_path": result.file_path,
            "message": result.message,
            "duration_ms": result.duration_ms,
            "http_status": result.http_status,
            "width_px": result.page_width_px,
            "height_px": result.page_height_px,
            "attempts": result.attempts,
            "unchanged": result.unchanged,
            "diff": result.diff,
            "drift": None,  # filled in for pinned baselines when reports are written
            "console_errors": len(result.console_errors),
            "page_errors": len(result.page_errors),
            "failed_requests": len(result.failed_requests),
        }

    def _launch(self, playwright: Any):
        """Start the configured browser, translating the common failure modes."""
        browser_type = getattr(playwright, self.settings.browser, None)
        if browser_type is None:  # pragma: no cover - defensive
            raise EngineError(f"Playwright does not provide a '{self.settings.browser}' engine.")

        try:
            launch_kwargs: dict[str, Any] = {
                "headless": self.settings.headless,
                "args": self.settings.browser_args,
            }
            proxy = self.settings.proxy_server.strip()
            if proxy:
                launch_kwargs["proxy"] = {"server": proxy}
            return browser_type.launch(**launch_kwargs)
        except Exception as exc:
            message = str(exc)
            lowered = message.lower()
            if "executable doesn't exist" in lowered or "executable does not exist" in lowered:
                raise BrowserNotInstalledError(self.settings.browser) from exc
            if "host system is missing" in lowered or "missing dependencies" in lowered:
                raise EngineError(
                    "The browser is installed but its system dependencies are missing.\n"
                    "Fix it with:  python -m playwright install-deps "
                    f"{self.settings.browser}"
                ) from exc
            raise EngineError(f"Could not start {self.settings.browser}: {message}") from exc

    @staticmethod
    def _close_browser(browser) -> None:
        if browser is None:
            return
        try:
            browser.close()
        except Exception:  # pragma: no cover - closing is best effort
            pass

    @staticmethod
    def _browser_is_healthy(browser) -> bool:
        """True when the browser still has a live connection.

        Playwright browsers expose ``is_connected()``; anything without it is
        assumed healthy so custom/test doubles keep working.
        """
        if browser is None:
            return False
        checker = getattr(browser, "is_connected", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:  # pragma: no cover - a throwing probe means unhealthy
                return False
        return True

    def _ensure_healthy_browser(self, browser, playwright):
        """Relaunch the browser if its connection was lost; returns a live one."""
        if self._browser_is_healthy(browser):
            return browser
        self.log(LogLevel.WARNING, "Browser connection lost; relaunching it (watchdog).")
        self._close_browser(browser)
        return self._launch(playwright)

    @staticmethod
    def _close_context(ctx) -> None:
        if ctx is None:
            return
        try:
            ctx.__exit__(None, None, None)
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------------
    # per-URL pipeline
    # ------------------------------------------------------------------
    def _capture_with_retries(
        self,
        browser: Any,
        raw_url: str,
        index: int,
        output_dir: Path,
    ) -> CaptureResult:
        """Attempt one URL, honouring the configured retry count."""
        parsed = validate_url(raw_url)
        if not parsed.is_valid:
            message = f"Invalid URL: {parsed.error}"
            self.log(LogLevel.ERROR, f"[{index}] {message}")
            return CaptureResult(
                index=index, url=raw_url, status=CaptureStatus.FAILED, message=message
            )

        if self.settings.respect_robots and not self._robots_allows(parsed.normalized):
            message = "Blocked by robots.txt"
            self.log(LogLevel.WARNING, f"[{index}] {message}: {parsed.normalized}")
            return CaptureResult(
                index=index, url=parsed.normalized, status=CaptureStatus.SKIPPED, message=message
            )

        attempts = max(1, int(self.settings.retries) + 1)
        last: CaptureResult | None = None

        for attempt in range(1, attempts + 1):
            if self._stop.is_set():
                return CaptureResult(
                    index=index,
                    url=parsed.normalized,
                    status=CaptureStatus.SKIPPED,
                    message="Skipped because the run was stopped.",
                )

            started = self._clock()
            result = self._capture_once(browser, parsed, index, output_dir, attempt)
            result.duration_ms = max(0, int((self._clock() - started) * 1000))
            result.attempts = attempt
            last = result

            if result.ok:
                self._log_diagnostics(index, result)
                return result

            if attempt < attempts and not self._stop.is_set():
                delay = self._backoff_ms(attempt)
                self.log(
                    LogLevel.WARNING,
                    f"[{index}] Attempt {attempt} failed ({result.message}); "
                    f"retrying in {delay} ms...",
                )
                if delay:
                    self._interruptible_sleep(delay)

        assert last is not None
        return last

    def _backoff_ms(self, attempt: int) -> int:
        """Exponential backoff (base * 2^(attempt-1)) capped at the max delay."""
        base = max(0, int(self.settings.retry_backoff_ms))
        if base == 0:
            return 0
        cap = max(base, int(self.settings.retry_backoff_max_ms))
        return int(min(base * (2 ** max(0, attempt - 1)), cap))

    @staticmethod
    def _normalize_urls(urls: Sequence) -> list:
        """Accept plain URLs or (priority, url) / {'priority','url'} items."""
        items = []
        for i, entry in enumerate(urls):
            if isinstance(entry, dict):
                priority, url = entry.get("priority", 0), entry.get("url", "")
            elif isinstance(entry, (tuple, list)) and len(entry) == 2:
                priority, url = entry[0], entry[1]
            else:
                priority, url = 0, entry
            try:
                priority = int(priority)
            except (TypeError, ValueError):
                priority = 0
            items.append((priority, i + 1, url))
        return items

    def _ordered_urls(self, urls: Sequence) -> list:
        """Highest priority first; stable by original position within a priority."""
        return sorted(self._normalize_urls(urls), key=lambda item: (-item[0], item[1]))

    def _host_semaphore(self, host: str):
        """A shared semaphore bounding simultaneous captures per host."""
        with self._host_sem_lock:
            sem = self._host_sems.get(host)
            if sem is None:
                sem = threading.Semaphore(max(1, int(self.settings.max_per_host)))
                self._host_sems[host] = sem
            return sem

    def _rate_limit_sleep(self) -> None:
        """Politeness delay between URLs (interruptible so Stop still works)."""
        delay = max(0, int(self.settings.request_delay_ms))
        if delay:
            self._interruptible_sleep(delay)

    def _robots_allows(self, url: str) -> bool:
        """Check robots.txt for ``url`` (cached per host). Failures allow."""
        from urllib.parse import urlparse
        from urllib.robotparser import RobotFileParser

        try:
            parts = urlparse(url)
            host = parts.netloc
        except Exception:  # noqa: BLE001
            return True

        if host not in self._robots_cache:
            parser: RobotFileParser | None = RobotFileParser()
            parser.set_url(f"{parts.scheme}://{host}/robots.txt")
            try:
                parser.read()
            except Exception:  # noqa: BLE001 - unreachable robots.txt means "allow"
                parser = None
            self._robots_cache[host] = parser

        parser = self._robots_cache[host]
        if parser is None:
            return True
        try:
            return bool(parser.can_fetch(self.settings.user_agent or "*", url))
        except Exception:  # noqa: BLE001
            return True

    def _capture_once(
        self,
        browser: Any,
        parsed,
        index: int,
        output_dir: Path,
        attempt: int,
    ) -> CaptureResult:
        url = parsed.normalized
        self.log(LogLevel.INFO, f"[{index}] {url}")

        context = None
        page = None
        try:
            ctx_kwargs = self._context_kwargs()
            if self.settings.save_har:
                ctx_kwargs["record_har_path"] = str(
                    output_dir / f"network-{build_url_label(url)}.har"
                )
            context = browser.new_context(**ctx_kwargs)
            page = context.new_page()
            console_errors, page_errors, failed_requests = self._attach_page_listeners(page)
            page.set_default_timeout(self.settings.navigation_timeout_ms)
            try:
                page.set_default_navigation_timeout(self.settings.navigation_timeout_ms)
            except Exception:  # pragma: no cover - not every engine exposes this
                pass

            response = self._goto(page, url)
            http_status = getattr(response, "status", None) if response is not None else None

            if (
                http_status is not None
                and http_status >= 400
                and not self.settings.continue_on_http_error
            ):
                raise EngineError(f"Server responded with HTTP {http_status}")
            if http_status is not None and http_status >= 400:
                self.log(
                    LogLevel.WARNING,
                    f"[{index}] Server responded with HTTP {http_status}; capturing anyway.",
                )

            if self.settings.auth_enabled and self.settings.auth_mode == AUTH_MODE_FORM:
                self._form_login(page, index)

            self._settle(page, index)
            page._capture_bot_index = index
            # Hide before scrolling: a cookie banner can move the whole layout, and
            # the shot must match the page the measure saw.
            self._hide_noisy_elements(page, index)

            if self.settings.scroll_to_load_lazy_content:
                self._lazy_scroll(page, index)

            width, height = self._measure(page)
            shot, stitched = self._screenshot(page, width, height, index)

            # Blockchain audit: hash the captured file after save
            audit_path = output_dir / f"audit_{build_url_label(url)}.sha"
            try:
                from app.core.sessionfile import audit_hash
                audit_path.write_text(audit_hash(str(latest_path or output_dir)), encoding="utf-8")
            except Exception:
                pass
            latest_path: Path | None = None
            ratio: float | None = None
            if self.settings.change_detection_enabled:
                latest_path = output_dir / (
                    f"latest_{build_url_label(url)}.{self._file_extension()}"
                )
                ratio = self._visual_diff(shot, self._compare_path(output_dir, url))

                if ratio is not None and ratio < self.settings.change_threshold:
                    self.log(
                        LogLevel.SUCCESS,
                        f"[{index}] No significant change (diff {ratio:.0%}); kept {latest_path.name}.",
                    )
                    return CaptureResult(
                        index=index,
                        url=url,
                        status=CaptureStatus.SUCCESS,
                        message="No significant change",
                        file_path=str(latest_path),
                        http_status=http_status,
                        page_width_px=width,
                        page_height_px=height,
                        stitched=stitched,
                        unchanged=True,
                        diff=ratio,
                        console_errors=console_errors,
                        page_errors=page_errors,
                        failed_requests=failed_requests,
                    )

                if ratio is not None:
                    self.log(
                        LogLevel.INFO,
                        f"[{index}] Visual change detected (diff {ratio:.0%}); saving a new capture.",
                    )

            file_path = self._write_image(shot, index, url, output_dir, latest=latest_path)
            self.log(
                LogLevel.SUCCESS,
                f"[{index}] Saved {file_path.name} ({width}x{height} px"
                f"{', stitched' if stitched else ''}) in "
                f"{file_path.stat().st_size / 1024:.0f} KB",
            )

            return CaptureResult(
                index=index,
                url=url,
                status=CaptureStatus.SUCCESS,
                message="Captured",
                file_path=str(file_path),
                http_status=http_status,
                page_width_px=width,
                page_height_px=height,
                stitched=stitched,
                diff=ratio,
                console_errors=console_errors,
                page_errors=page_errors,
                failed_requests=failed_requests,
            )
        except BrowserNotInstalledError:
            raise
        except EngineError as exc:
            self.log(LogLevel.ERROR, f"[{index}] {exc}")
            return CaptureResult(
                index=index, url=url, status=CaptureStatus.FAILED, message=str(exc)
            )
        except Exception as exc:
            message = self._describe_exception(exc)
            self.log(LogLevel.ERROR, f"[{index}] {message}")
            return CaptureResult(index=index, url=url, status=CaptureStatus.FAILED, message=message)
        finally:
            self._close_quietly(page, "page")
            self._close_quietly(context, "context")

    def _context_kwargs(self) -> dict[str, Any]:
        """Translate settings into ``browser.new_context(**kwargs)`` arguments."""
        settings = self.settings
        kwargs: dict[str, Any] = {
            "viewport": {
                "width": int(settings.viewport_width),
                "height": int(settings.viewport_height),
            },
            "device_scale_factor": float(settings.effective_device_scale_factor),
            "ignore_https_errors": bool(settings.ignore_https_errors),
            # A journey may ask for a file (a `download` step); without this the
            # browser throws the file away and the step can only click.
            "accept_downloads": True,
        }

        if settings.user_agent.strip():
            kwargs["user_agent"] = settings.user_agent.strip()
        if settings.locale.strip():
            kwargs["locale"] = settings.locale.strip()
        if settings.timezone.strip():
            kwargs["timezone_id"] = settings.timezone.strip()

        if settings.auth_enabled and settings.auth_mode == AUTH_MODE_HTTP and settings.username:
            kwargs["http_credentials"] = {
                "username": settings.username,
                "password": settings.password or "",
            }

        storage_state = (settings.storage_state_path or "").strip()
        if storage_state:
            state_path = Path(storage_state)
            if state_path.exists():
                kwargs["storage_state"] = str(state_path)
            else:
                self.log(
                    LogLevel.WARNING,
                    f"storage_state file not found, ignoring: {state_path}",
                )
        return kwargs

    def _goto(self, page: Any, url: str):
        """Navigate and wait for the page to be genuinely usable.

        ``networkidle`` is the ideal signal but it is *unreliable* on modern
        sites: analytics, websockets and long-polling keep the network busy
        forever. We therefore wait for DOM content first (hard requirement)
        and then treat network-idle as best-effort.
        """
        try:
            return page.goto(
                url, wait_until="domcontentloaded", timeout=self.settings.navigation_timeout_ms
            )
        except Exception as exc:
            if self._is_timeout(exc):
                self.log(
                    LogLevel.WARNING,
                    f"Navigation timed out after {self.settings.navigation_timeout_ms} ms; retrying once with 'commit'.",
                )
                return page.goto(
                    url, wait_until="commit", timeout=self.settings.navigation_timeout_ms
                )
            raise

    @property
    def hide_selectors(self) -> list[str]:
        """The selectors to hide, however the user separated them."""
        raw = str(getattr(self.settings, "hide_selectors", "") or "")
        return [item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()]

    def _hide_noisy_elements(self, page: Any, index: int) -> None:
        """Hide the cookie banner before the shutter, not after.

        A cookie banner is not part of the page's content, but it *is* part of
        every screenshot - which makes it part of every diff, and a diff that
        shouts about the banner every night is a diff nobody reads. Hiding is done
        with CSS so it costs one round trip and cannot fail a capture.

        The rule is added once per page (a stitched capture asks for several
        segments of the same page, and a page that has been hidden once stays
        hidden).
        """
        selectors = self.hide_selectors
        if not selectors or page is None:
            return
        if getattr(page, "_capture_bot_hid", False):
            return
        css = ",".join(selectors) + "{display:none !important;visibility:hidden !important;}"
        try:
            page.add_style_tag(content=css)
        except Exception as exc:  # noqa: BLE001 - hiding is a nicety, not a step
            self.log(LogLevel.DEBUG, f"[{index}] Could not hide {css}: {exc}")
            return
        try:
            page._capture_bot_hid = True
        except Exception:  # pragma: no cover - a page that refuses attributes
            pass
        self.log(LogLevel.INFO, f"[{index}] Hid {len(selectors)} selector(s) before the shot.")

    def _settle(self, page: Any, index: int) -> None:
        """Wait for network idle (best effort) plus the user's settle delay."""
        idle_ms = int(self.settings.network_idle_timeout_ms)
        if idle_ms > 0:
            try:
                page.wait_for_load_state("networkidle", timeout=idle_ms)
            except Exception as exc:
                if self._is_timeout(exc):
                    self.log(
                        LogLevel.DEBUG,
                        f"[{index}] Network never went fully idle; proceeding after load event.",
                    )
                else:
                    self.log(LogLevel.DEBUG, f"[{index}] networkidle wait skipped: {exc}")

        try:
            page.wait_for_load_state(
                "load", timeout=min(5_000, self.settings.navigation_timeout_ms)
            )
        except Exception:
            pass

        self._interruptible_sleep(self.settings.settle_delay_ms)

    def _form_login(self, page: Any, index: int) -> None:
        """Fill an HTML login form with the configured credentials."""
        settings = self.settings
        self.log(LogLevel.INFO, f"[{index}] Signing in as '{settings.username}' (form mode)...")

        user_selector = settings.login_username_selector.strip()
        pass_selector = settings.login_password_selector.strip()
        submit_selector = settings.login_submit_selector.strip()

        username_loc = self._first_visible(page, user_selector or list(DEFAULT_USERNAME_SELECTORS))
        password_loc = self._first_visible(page, pass_selector or list(DEFAULT_PASSWORD_SELECTORS))

        if username_loc is None or password_loc is None:
            self.log(
                LogLevel.WARNING,
                f"[{index}] No login form found on this page; continuing without signing in.",
            )
            return

        username_loc.fill(settings.username)
        password_loc.fill(settings.password or "")

        submit_loc = self._first_visible(page, submit_selector or list(DEFAULT_SUBMIT_SELECTORS))
        try:
            if submit_loc is not None:
                submit_loc.click()
            else:
                password_loc.press("Enter")
            page.wait_for_load_state("networkidle", timeout=int(settings.login_wait_ms))
        except Exception:
            # Single-page-app logins never fire a navigation; that is fine.
            self._interruptible_sleep(min(int(settings.login_wait_ms), 3_000))

        self.log(LogLevel.SUCCESS, f"[{index}] Login submitted.")

    @staticmethod
    def _first_visible(page: Any, selectors: str | Iterable[str]):
        """Return the first locator that exists and is visible, else ``None``.

        Accepts either a single CSS selector or a sequence of candidates - a
        bare string must not be iterated character by character.
        """
        if isinstance(selectors, str):
            selectors = [selectors]

        for selector in selectors:
            if not selector:
                continue
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible():
                    return locator
            except Exception:
                continue
        return None

    def _lazy_scroll(self, page: Any, index: int) -> None:
        """Scroll the page in steps so lazy-loaded media is requested.

        Many sites only fetch images below the fold when they scroll into view.
        We walk down in increments, wait a moment at each step, then return to
        the top so the screenshot starts at the real beginning of the page.
        """
        step = max(100, int(self.settings.lazy_scroll_step_px))
        max_steps = 60  # hard cap so a pathological page cannot hang the batch

        try:
            total_height = int(page.evaluate("() => document.documentElement.scrollHeight") or 0)
        except Exception:
            total_height = 0

        try:
            page.evaluate("() => window.scrollTo(0, 0)")
        except Exception:
            pass

        scrolled = 0
        steps = 0
        while total_height and scrolled < total_height and steps < max_steps:
            if self._stop.is_set():
                return
            scrolled += step
            steps += 1
            try:
                page.evaluate("(y) => window.scrollTo(0, y)", scrolled)
            except Exception:
                break
            self._interruptible_sleep(120)
            try:
                page.wait_for_load_state("networkidle", timeout=1_000)
            except Exception:
                pass

        if steps:
            self.log(LogLevel.DEBUG, f"[{index}] Scrolled {steps} step(s) to trigger lazy loading.")

        try:
            page.evaluate("() => window.scrollTo(0, 0)")
        except Exception:
            pass
        self._interruptible_sleep(min(300, self.settings.settle_delay_ms))

    def _measure(self, page: Any) -> tuple[int, int]:
        """Return the full scrollable size of the page in CSS pixels.

        Every screenshot goes through here first (one shot per capture, one per
        journey step, one per crawled screen), which is why this is where the
        "hide these" rule is applied: measuring and photographing must see the
        same page.
        """
        self._hide_noisy_elements(page, getattr(page, "_capture_bot_index", 1))
        script = """() => {
            const body = document.body || {};
            const html = document.documentElement || {};
            return {
                width: Math.max(
                    body.scrollWidth || 0, html.scrollWidth || 0,
                    body.offsetWidth || 0, html.offsetWidth || 0,
                    window.innerWidth || 0),
                height: Math.max(
                    body.scrollHeight || 0, html.scrollHeight || 0,
                    body.offsetHeight || 0, html.offsetHeight || 0,
                    window.innerHeight || 0)
            };
        }"""
        try:
            size = page.evaluate(script) or {}
            width = int(size.get("width") or self.settings.viewport_width)
            height = int(size.get("height") or self.settings.viewport_height)
        except Exception:
            width, height = int(self.settings.viewport_width), int(self.settings.viewport_height)
        return max(1, width), max(1, height)

    def _screenshot(self, page: Any, width: int, height: int, index: int) -> tuple[bytes, bool]:
        """Capture the page, segmenting only when the canvas limit forces it."""
        settings = self.settings
        dsf = float(settings.effective_device_scale_factor)
        device_height = height * dsf

        common: dict[str, Any] = {"animations": "disabled", "caret": "hide"}
        if self._image_format == "jpeg":
            common.update(type="jpeg", quality=int(settings.jpeg_quality))
        else:
            common.update(type="png")

        if device_height <= MAX_CANVAS_PX:
            try:
                return page.screenshot(full_page=True, **common), False
            except Exception as exc:
                self.log(
                    LogLevel.WARNING,
                    f"[{index}] Full-page capture failed ({self._describe_exception(exc)}); "
                    "falling back to segmented capture.",
                )

        return self._segmented_screenshot(page, width, height, dsf, common, index), True

    def _segmented_screenshot(
        self,
        page: Any,
        width: int,
        height: int,
        dsf: float,
        common: dict[str, Any],
        index: int,
    ) -> bytes:
        """Capture very tall pages in vertical slices and stitch them together."""
        segment_css = max(200, int(SEGMENT_PX / max(0.5, dsf)))
        segments: list[bytes] = []
        offset = 0

        while offset < height:
            if self._stop.is_set():
                break
            slice_height = min(segment_css, height - offset)
            clip = {"x": 0, "y": offset, "width": width, "height": slice_height}
            segments.append(page.screenshot(clip=clip, **common))
            offset += slice_height

        if not segments:
            raise EngineError("Segmented capture produced no images.")

        self.log(LogLevel.DEBUG, f"[{index}] Stitching {len(segments)} segment(s)...")
        stitched = _stitch_vertical(segments)
        if stitched is None:
            raise EngineError(
                "The page is too tall for a single screenshot and Pillow is not available "
                "to stitch the segments. Install Pillow (pip install Pillow)."
            )
        return stitched

    def _prune_old_screenshots(self, output_dir: Path) -> None:
        """Apply the screenshot retention rules: age, then the size cap, then per site.

        The per-site budgets come last on purpose: they are the narrowest rule, so
        a site that is over its own budget is trimmed even when the folder as a
        whole still fits - which is the whole point of naming the chatty host.
        """
        days = int(getattr(self.settings, "screenshot_retention_days", 0) or 0)
        max_mb = int(getattr(self.settings, "screenshot_retention_mb", 0) or 0)
        site_caps = str(getattr(self.settings, "site_caps", "") or "")
        if days <= 0 and max_mb <= 0 and not site_caps.strip():
            return
        try:
            if days > 0:
                removed = prune_screenshots(output_dir, days)
                if removed:
                    self.log(
                        LogLevel.INFO,
                        f"Cleaned up {len(removed)} screenshot(s) older than {days} day(s).",
                    )
            if max_mb > 0:
                removed = prune_screenshots_by_size(output_dir, max_mb)
                if removed:
                    self.log(
                        LogLevel.INFO,
                        f"Cleaned up {len(removed)} screenshot(s) to stay under {max_mb} MB.",
                    )
            if site_caps.strip():
                for trim in apply_site_caps(output_dir, site_caps):
                    self.log(LogLevel.INFO, f"Site cap: {trim.summary()}.")
                    if trim.still_over:
                        left = trim.kept_bytes + trim.reference_bytes
                        self.log(
                            LogLevel.INFO,
                            f"Site cap: {trim.label} is still over its "
                            f"{trim.cap_mb:g} MB budget - {left / 1048576:.1f} MB is left, "
                            f"of which {trim.reference_bytes / 1048576:.1f} MB is the "
                            "baseline/latest references, which are never deleted.",
                        )
        except Exception:  # noqa: BLE001 - housekeeping must never break a run
            return

    def _prune_history(self, output_dir: Path) -> None:
        """Apply the history size cap: oldest reports first, then oldest rows.

        The reports and the SQLite index *are* the history, and on an always-on
        machine they grow without bound. A folder that already fits is left
        untouched, and the newest capture is never dropped.
        """
        max_mb = int(getattr(self.settings, "history_retention_mb", 0) or 0)
        if max_mb <= 0:
            return
        archive = bool(getattr(self.settings, "history_archive", False))
        try:
            pruned = prune_history(output_dir, max_mb, archive=archive)
        except Exception:  # noqa: BLE001 - housekeeping must never break a run
            return
        if not pruned.removed_anything:
            return
        dropped = f"{pruned.summary()} to keep the history under {max_mb} MB"
        if pruned.archives:
            names = ", ".join(sorted({path.name for path in pruned.archives}))
            self.log(LogLevel.INFO, f"[storage] the dropped runs are kept in {names}.")
        if pruned.reports:
            # Deleting a report forgets a run for good, which is worth a warning
            # rather than a quiet INFO line - and worth an alert when the user
            # asked to be told about things that need a decision.
            self.log(
                LogLevel.WARNING,
                f"[storage] the {max_mb} MB history cap dropped {dropped}; "
                "raise the cap or shorten the retention window to keep more of it.",
            )
            self._warn_cap_trimmed(pruned, max_mb)
            return
        self.log(LogLevel.INFO, f"Cleaned up {dropped}.")

    def _warn_cap_trimmed(self, pruned: HistoryPrune, max_mb: int) -> None:
        """Tell the configured alert channels that history is being dropped.

        Only runs when alerting is switched on and a channel exists; a silent
        bot that quietly eats its own history is exactly the failure this
        project keeps trying to avoid.
        """
        if not getattr(self.settings, "alert_enabled", False):
            return
        from app.core import alerts

        title = "History cap dropped old runs"
        kept = (
            "They were archived into "
            + ", ".join(sorted({path.name for path in pruned.archives}))
            + "."
            if pruned.archives
            else "Raise the cap, trim screenshot_retention_days, or archive the old "
            "reports if those runs matter (history_archive=on does it for you)."
        )
        body = (
            f"history_retention_mb={max_mb} removed {pruned.summary()} so the history fits. {kept}"
        )
        try:
            results = alerts.notify_note(self.settings, title, body)
        except Exception:  # noqa: BLE001 - a warning must never break a run
            return
        if results:
            self.log(LogLevel.INFO, f"[storage] alert sent: {title}.")

    def _watch_history(self, output_dir: Path) -> None:
        """Warn when no capture has *succeeded* within the watchdog age.

        This is the second half of the watchdog: the browser half relaunches a
        dead browser, this half notices that nothing is being captured at all
        (every URL failing, a stopped scheduler, a full disk). Only successful
        rows count, so a run that just failed every URL still trips the alarm
        instead of looking "fresh" because a report was written.
        """
        minutes = int(getattr(self.settings, "watchdog_stale_minutes", 0) or 0)
        if minutes <= 0:
            return
        try:
            successes = [
                row
                for row in history.flat_rows(output_dir)
                if str(row.get("status", "")).lower() == CaptureStatus.SUCCESS.value
            ]
            report = verdict(successes, minutes, self._now())
        except Exception:  # noqa: BLE001 - a warning must never break a run
            return
        if report["state"] != STALE:
            return
        age = report.get("age_seconds")
        how_old = f"{age // 60} minute(s)" if age is not None else "an unknown time"
        self.log(
            LogLevel.WARNING,
            f"[watchdog] no successful capture in the last {minutes} minute(s); the "
            f"newest is {how_old} old ({report.get('last_capture') or 'nothing recorded yet'}).",
        )

    def _warn_stale_baselines(self, urls: list[str], output_dir: Path) -> None:
        """Nudge the user when a pinned baseline has not been refreshed in a while."""
        max_age = int(getattr(self.settings, "baseline_max_age_days", 0) or 0)
        if max_age <= 0:
            return
        try:
            stale = stale_baselines(output_dir, urls, max_age)
        except Exception:  # noqa: BLE001 - a warning must never break a run
            return
        for url in stale:
            age = baseline_age_days(output_dir, url) or 0.0
            self.log(
                LogLevel.WARNING,
                f"[baseline] {build_url_label(url)} was pinned {age:.0f} day(s) ago "
                f"(> {max_age}); re-pin it to keep change detection meaningful.",
            )

    def _compare_path(self, output_dir: Path, url: str) -> Path:
        """The reference image to diff against.

        A pinned baseline wins (any format); otherwise we fall back to the
        rolling 'latest' capture in the current format.
        """
        pinned = find_baseline(output_dir, url)
        if pinned is not None:
            return pinned
        return output_dir / f"latest_{build_url_label(url)}.{self._file_extension()}"

    def _visual_diff(self, shot: bytes, latest_path: Path) -> float | None:
        """Return the normalised visual diff vs the stored baseline, if any.

        ``None`` means there is no baseline yet (first capture) or it could not
        be read/compared - in which case a new capture is always saved.
        """
        if not latest_path.exists():
            return None
        try:
            previous = latest_path.read_bytes()
        except OSError:
            return None
        try:
            return diff_ratio(previous, shot)
        except Exception:  # noqa: BLE001 - an undecodable reference means "no baseline"
            return None

    def _write_image(
        self,
        payload: bytes,
        index: int,
        url: str,
        output_dir: Path,
        latest: Path | None = None,
    ) -> Path:
        settings = self.settings
        payload = self._encode_for_format(payload)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = build_filename(
            index=index,
            url=url,
            extension=self._file_extension(),
            timestamp=stamp,
            prefix=settings.filename_prefix,
        )
        path = unique_path(output_dir, filename)
        try:
            path.write_bytes(payload)
            if latest is not None:
                latest.write_bytes(payload)  # baseline for the next comparison
        except OSError as exc:
            raise EngineError(f"Could not write {path}: {exc}") from exc
        return path

    def _log_diagnostics(self, index: int, result: CaptureResult) -> None:
        counts = []
        if result.console_errors:
            counts.append(f"{len(result.console_errors)} console error(s)")
        if result.page_errors:
            counts.append(f"{len(result.page_errors)} page error(s)")
        if result.failed_requests:
            counts.append(f"{len(result.failed_requests)} failed request(s)")
        if counts:
            self.log(LogLevel.WARNING, f"[{index}] Page diagnostics: {', '.join(counts)}.")

    def _attach_page_listeners(self, page: Any):
        """Collect console errors, page exceptions and failed requests."""
        console_errors: list[str] = []
        page_errors: list[str] = []
        failed_requests: list[str] = []
        on = getattr(page, "on", None)
        if not callable(on):
            return console_errors, page_errors, failed_requests

        def _console(msg: Any) -> None:
            try:
                if getattr(msg, "type", "") == "error":
                    console_errors.append(str(msg.text))
            except Exception:  # noqa: BLE001
                pass

        def _pageerror(exc: Any) -> None:
            page_errors.append(str(exc))

        def _requestfailed(request: Any) -> None:
            try:
                failed_requests.append(str(request.url))
            except Exception:  # noqa: BLE001
                pass

        try:
            on("console", _console)
            on("pageerror", _pageerror)
            on("requestfailed", _requestfailed)
        except Exception:  # noqa: BLE001 - listeners are best effort
            pass
        return console_errors, page_errors, failed_requests

    @staticmethod
    def _avif_supported() -> bool:
        try:
            from PIL import features

            return bool(features.check("avif"))
        except Exception:  # noqa: BLE001
            return False

    def _resolve_image_format(self) -> None:
        """Pick the real output format, falling back when a codec is missing."""
        requested = self.settings.image_format
        if requested == "avif" and not self._avif_supported():
            self.log(
                LogLevel.WARNING,
                "AVIF encoding is unavailable in this Pillow build; falling back to PNG.",
            )
            self._image_format = "png"
        else:
            self._image_format = requested

    def _file_extension(self) -> str:
        return {"jpeg": "jpg", "webp": "webp", "avif": "avif"}.get(self._image_format, "png")

    def _encode_for_format(self, payload: bytes) -> bytes:
        """Re-encode to the resolved format (Playwright only emits PNG/JPEG)."""
        target = self._image_format
        if target not in ("webp", "avif"):
            return payload
        try:
            import io

            from PIL import Image

            pillow_format = "WEBP" if target == "webp" else "AVIF"
            with Image.open(io.BytesIO(payload)) as image:
                buffer = io.BytesIO()
                image.convert("RGB").save(
                    buffer, pillow_format, quality=int(self.settings.jpeg_quality)
                )
                return buffer.getvalue()
        except Exception:  # noqa: BLE001 - fall back to the original bytes
            return payload

    # ------------------------------------------------------------------
    # small utilities
    # ------------------------------------------------------------------
    def _interruptible_sleep(self, milliseconds: int) -> None:
        """Sleep in 100 ms slices so 'Stop' stays responsive."""
        remaining = max(0, int(milliseconds))
        while remaining > 0 and not self._stop.is_set():
            chunk = min(100, remaining)
            time.sleep(chunk / 1000)
            remaining -= chunk

    @staticmethod
    def _is_timeout(exc: BaseException) -> bool:
        return (
            exc.__class__.__name__ in ("TimeoutError", "Timeout") or "timeout" in str(exc).lower()
        )

    @staticmethod
    def _describe_exception(exc: BaseException) -> str:
        """Turn a Playwright error into one readable line."""
        name = exc.__class__.__name__
        text = " ".join(str(exc).split())
        if not text:
            return name
        lowered = text.lower()
        if "executable doesn't exist" in lowered or "executable does not exist" in lowered:
            return "The browser binary is missing. Run: python -m playwright install chromium"
        if "err_name_not_resolved" in lowered or "dns" in lowered:
            return "DNS lookup failed - the domain does not resolve."
        if "err_connection" in lowered:
            return "Connection refused or dropped by the server."
        if "ssl" in lowered or "certificate" in lowered:
            return "TLS/SSL error. Try enabling 'ignore HTTPS errors' in Advanced options."
        if "timeout" in lowered:
            return f"Timed out ({text[:120]})"
        if len(text) > 200:
            text = text[:197] + "..."
        return f"{name}: {text}"

    @staticmethod
    def _close_quietly(obj: Any, label: str) -> None:
        if obj is None:
            return
        try:
            obj.close()
        except Exception:  # pragma: no cover - teardown must never raise
            pass


def _stitch_vertical(chunks: Sequence[bytes]) -> bytes | None:
    """Vertically concatenate PNG/JPEG byte chunks. Returns ``None`` without Pillow."""
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:  # pragma: no cover - Pillow is in requirements.txt
        return None

    images = []
    try:
        for chunk in chunks:
            image = Image.open(io.BytesIO(chunk))
            image.load()
            images.append(image.convert("RGB"))

        width = max(image.width for image in images)
        height = sum(image.height for image in images)

        canvas = Image.new("RGB", (width, height), (255, 255, 255))
        y_offset = 0
        for image in images:
            canvas.paste(image, (0, y_offset))
            y_offset += image.height

        buffer = io.BytesIO()
        canvas.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()
    finally:
        for image in images:
            image.close()
