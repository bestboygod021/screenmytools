"""Application entry point.

Run directly during development::

    python main.py

The same file is what PyInstaller packages into ``FullPageCaptureBot.exe``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from app.ui.main_window import MainWindow
from app.ui.theme import apply_theme
from app.version import APP_NAME, ORG_NAME, __version__


def resource_root() -> Path:
    """Directory that holds bundled assets.

    PyInstaller unpacks data files into ``sys._MEIPASS``; during development
    the assets simply live next to this file.
    """
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root)
    return Path(__file__).resolve().parent


def find_icon() -> QIcon | None:
    """Load ``assets/icon.ico`` when it is available."""
    candidates = (
        resource_root() / "assets" / "icon.ico",
        resource_root() / "assets" / "icon.png",
    )
    for candidate in candidates:
        if candidate.exists():
            icon = QIcon(str(candidate))
            if not icon.isNull():
                return icon
    return None


def build_application() -> QApplication:
    """Create and theme the QApplication (kept separate for testability)."""
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    application = QApplication(sys.argv)
    application.setApplicationName(APP_NAME)
    application.setApplicationDisplayName(APP_NAME)
    application.setOrganizationName(ORG_NAME)
    application.setOrganizationDomain("fullpagecapture.local")
    application.setApplicationVersion(__version__)

    # Fusion gives a predictable, platform-neutral base for the stylesheet.
    application.setStyle("Fusion")
    apply_theme(application)

    icon = find_icon()
    if icon is not None:
        application.setWindowIcon(icon)
    return application


def main() -> int:
    application = build_application()

    window = MainWindow()
    icon = find_icon()
    if icon is not None:
        window.setWindowIcon(icon)
    window.show()

    return application.exec()


if __name__ == "__main__":  # pragma: no cover - manual entry point
    sys.exit(main())
