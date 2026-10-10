"""Thread bridge between the Qt-free capture engine and the PyQt6 interface.

``CaptureWorker`` lives on its own :class:`QThread`. The engine calls back into
it from that thread; every callback only *emits a signal*, and Qt delivers
those signals to the GUI thread through a queued connection. That single rule
is what keeps the UI responsive and crash-free.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from app.core.engine import (
    BrowserNotInstalledError,
    CaptureEngine,
    CaptureResult,
    CaptureSummary,
    EngineError,
    LogLevel,
)
from app.core.settings import CaptureSettings


class CaptureWorker(QObject):
    """Runs :class:`CaptureEngine` off the GUI thread and publishes its events."""

    #: ``(level, message)``
    log_message = pyqtSignal(str, str)
    #: ``(completed, total, current_url)``
    progress_changed = pyqtSignal(int, int, str)
    #: ``(CaptureResult)``
    result_ready = pyqtSignal(object)
    #: ``(CaptureSummary)`` - always emitted exactly once per run
    batch_finished = pyqtSignal(object)
    #: ``(message)`` - fatal problems that prevented any capture
    fatal_error = pyqtSignal(str)

    def __init__(
        self,
        settings: CaptureSettings,
        urls: Sequence[str],
        engine_factory: Callable[..., CaptureEngine] = CaptureEngine,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._urls = list(urls)
        self._engine_factory = engine_factory
        self._engine: CaptureEngine | None = None

    # ------------------------------------------------------------------
    def request_stop(self) -> None:
        """Ask the running engine to stop after the current URL."""
        engine = self._engine
        if engine is not None:
            engine.request_stop()

    @property
    def urls(self) -> list[str]:
        return list(self._urls)

    # ------------------------------------------------------------------
    @pyqtSlot()
    def run(self) -> None:
        """Entry point for the worker thread."""
        summary: CaptureSummary | None = None
        try:
            self._engine = self._engine_factory(
                self._settings,
                log=self._on_log,
                progress=self._on_progress,
                on_result=self._on_result,
            )
            summary = self._engine.run(self._urls)
        except BrowserNotInstalledError as exc:
            self._on_log(LogLevel.ERROR, str(exc))
            self.fatal_error.emit(str(exc))
        except EngineError as exc:
            self._on_log(LogLevel.ERROR, str(exc))
            self.fatal_error.emit(str(exc))
        except Exception as exc:  # pragma: no cover - absolute safety net
            message = f"Unexpected failure: {exc.__class__.__name__}: {exc}"
            self._on_log(LogLevel.ERROR, message)
            self.fatal_error.emit(message)
        finally:
            self.batch_finished.emit(
                summary
                if summary is not None
                else CaptureSummary(
                    output_dir=self._settings.output_dir,
                )
            )
            self._engine = None

    # ------------------------------------------------------------------
    # engine callbacks (executed on the worker thread)
    # ------------------------------------------------------------------
    def _on_log(self, level, message: str) -> None:
        level_name = level.value if isinstance(level, LogLevel) else str(level)
        self.log_message.emit(level_name, message)

    def _on_progress(self, completed: int, total: int, current: str) -> None:
        self.progress_changed.emit(int(completed), int(total), str(current))

    def _on_result(self, result: CaptureResult) -> None:
        self.result_ready.emit(result)


class RecordingWorker(QObject):
    """Runs a recording session off the GUI thread.

    A recording is a capture that never ends by itself: it watches a real browser
    until the user says stop. So it borrows the engine (same user-agent, proxy,
    login and viewport the replay will use) and publishes the finished steps.
    """

    #: ``(Recording)`` - the session, with its steps
    recorded = pyqtSignal(object)
    #: ``(message)`` - the browser could not start, or the session broke
    failed = pyqtSignal(str)
    #: ``(level, message)``
    log_message = pyqtSignal(str, str)
    #: always emitted exactly once, whatever happened
    finished = pyqtSignal()

    def __init__(
        self,
        settings: CaptureSettings,
        url: str,
        engine_factory: Callable[..., CaptureEngine] = CaptureEngine,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._url = url
        self._engine_factory = engine_factory
        self._engine: CaptureEngine | None = None
        #: called with ``(recording, seconds)``; returning True stops the session
        self.should_stop: Callable[[object, float], bool] | None = None

    def request_stop(self) -> None:
        """Ask the session to stop after the current poll."""
        engine = self._engine
        if engine is not None:
            engine.request_stop()

    def run(self) -> None:
        """Open the page, watch it, and report the steps."""
        from app.core.recorder import Recorder

        try:
            settings = self._settings
            settings.headless = False  # the whole point is that a human clicks
            engine = self._engine_factory(settings, log=self._log)
            self._engine = engine
            recording = Recorder(engine).record(self._url, on_tick=self._tick)
            self.recorded.emit(recording)
        except BrowserNotInstalledError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - the worker must never crash the app
            self.failed.emit(f"Recording failed: {exc}")
        finally:
            self._engine = None
            self.finished.emit()

    # -- callbacks from the engine ---------------------------------------
    def _log(self, level: LogLevel, message: str) -> None:
        self.log_message.emit(getattr(level, "name", str(level)), message)

    def _tick(self, recording: object, seconds: float) -> bool:
        callback = self.should_stop
        return bool(callback(recording, seconds)) if callback is not None else False
