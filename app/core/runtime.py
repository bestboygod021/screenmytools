"""Playwright runtime discovery.

Answers two questions the UI needs before a run starts:

1. Is the Playwright package importable at all?
2. Is the requested browser binary actually present on disk?

It can also trigger ``playwright install <browser>`` from inside the packaged
application, where there is no Python interpreter on ``PATH`` to fall back on.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BrowserStatus:
    """Result of probing the local Playwright installation."""

    playwright_available: bool
    browser_available: bool
    executable_path: str = ""
    message: str = ""

    @property
    def ready(self) -> bool:
        return self.playwright_available and self.browser_available


def playwright_available() -> bool:
    """True when ``import playwright`` works in the current interpreter."""
    try:
        import playwright  # noqa: F401, PLC0415
    except Exception:
        return False
    return True


def detect_browser(browser: str = "chromium", timeout_s: float = 25.0) -> BrowserStatus:
    """Ask Playwright where its browser binary lives and whether it exists.

    Never raises: any failure is reported through :attr:`BrowserStatus.message`
    so the UI can render a helpful hint instead of crashing on start-up.
    """
    if not playwright_available():
        return BrowserStatus(
            playwright_available=False,
            browser_available=False,
            message="Playwright is not installed. Run: pip install playwright",
        )

    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        with sync_playwright() as driver:
            browser_type = getattr(driver, browser, None)
            if browser_type is None:
                return BrowserStatus(
                    playwright_available=True,
                    browser_available=False,
                    message=f"Unknown browser engine '{browser}'.",
                )

            executable = str(getattr(browser_type, "executable_path", "") or "")
    except Exception as exc:  # driver could not start
        return BrowserStatus(
            playwright_available=True,
            browser_available=False,
            message=f"Could not query Playwright: {exc}",
        )

    if executable and Path(executable).exists():
        return BrowserStatus(
            playwright_available=True,
            browser_available=True,
            executable_path=executable,
            message=f"{browser.capitalize()} ready",
        )

    return BrowserStatus(
        playwright_available=True,
        browser_available=False,
        executable_path=executable,
        message=(
            f"{browser.capitalize()} is not installed. Run: python -m playwright install {browser}"
        ),
    )


def browsers_root() -> Path:
    """Where Playwright stores downloaded browsers."""
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path.home() / ".cache"
    return base / "ms-playwright"


def _driver_command(browser: str) -> list[str] | None:
    """Build the command that runs ``playwright install`` for this interpreter.

    A frozen (PyInstaller) application has no ``python`` on PATH, so the
    bundled Node.js driver is invoked directly.
    """
    if getattr(sys, "frozen", False):
        try:
            from playwright._impl._driver import compute_driver_executable  # noqa: PLC0415

            node, cli, *_ = compute_driver_executable()
            return [str(node), str(cli), "install", browser]
        except Exception:
            return None

    return [sys.executable, "-m", "playwright", "install", browser]


def install_browser(
    browser: str = "chromium",
    on_line: Callable[[str], None] | None = None,
    timeout_s: float = 900.0,
) -> bool:
    """Download the browser binaries, streaming progress into ``on_line``.

    Returns ``True`` when Playwright reports success. Safe to call repeatedly:
    an already-installed browser makes this a no-op.
    """
    command: Sequence[str] | None = _driver_command(browser)
    if command is None:
        if on_line:
            on_line(
                "Cannot start the installer from a frozen build; "
                "run 'python -m playwright install chromium' from a terminal."
            )
        return False

    if on_line:
        on_line(f"Running: {' '.join(command)}")

    try:
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        if on_line:
            on_line(f"Could not start the installer: {exc}")
        return False

    assert process.stdout is not None
    try:
        for line in process.stdout:
            stripped = line.rstrip()
            if stripped and on_line:
                on_line(stripped)
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        process.kill()
        if on_line:
            on_line("The browser download timed out.")
        return False

    return process.returncode == 0
