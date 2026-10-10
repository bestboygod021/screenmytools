"""Render the real main window to ``docs/ui-preview.png`` (documentation aid).

On Linux/CI without a display it uses Qt's ``offscreen`` platform; on Windows
it simply opens and grabs the window. This is how the screenshot in the README
was produced - it is a genuine render, not a mockup.

Run with::

    python tools/render_ui_preview.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import app.ui.main_window as mw  # noqa: E402
from app.core.runtime import BrowserStatus  # noqa: E402
from app.ui.theme import apply_theme  # noqa: E402
from app.version import APP_NAME, ORG_NAME  # noqa: E402

# A canned, friendly run for the screenshot - no real browser needed.
mw.detect_browser = lambda name="chromium": BrowserStatus(True, True, "/fake", "Chromium ready")
mw.MainWindow._load_settings = lambda self: None

SAMPLE_LOG = [
    ("info", "FullPage Capture Bot v1.0.0 - run started"),
    ("info", "Browser ready: chromium (headless, 1920x1080 @2x)"),
    ("info", "[1] https://www.notion.so/pricing"),
    (
        "success",
        "[1] Saved 001_www_notion_so_pricing_20261007-140211.png (1920x6840 px, stitched) in 2431 KB",
    ),
    ("info", "[2] https://stripe.com/docs/payments"),
    ("warning", "[2] Server responded with HTTP 403; capturing anyway."),
    (
        "success",
        "[2] Saved 002_stripe_com_docs_payments_20261007-140224.png (1920x4120 px) in 1188 KB",
    ),
    ("info", "[3] https://news.ycombinator.com"),
    ("error", "[3] Timed out (Timeout 60000ms exceeded while waiting for navigation)"),
    ("success", "[3] Saved 003_news_ycombinator_com_20261007-140259.png (1920x2280 px) in 604 KB"),
]


def main() -> None:
    theme_name = sys.argv[1] if len(sys.argv) > 1 else "dark"

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setStyle("Fusion")
    apply_theme(app)

    window = mw.MainWindow()
    window.resize(1320, 880)
    window.output_dir_input.setText(r"C:\Users\alex\Pictures\FullPageCaptures")
    window.url_input.setPlainText(
        "\n".join(
            [
                "https://www.notion.so/pricing",
                "https://stripe.com/docs/payments",
                "https://news.ycombinator.com",
                "https://github.com/microsoft/playwright-python",
                "# internal dashboards",
                "https://dashboard.internal.example.com/reports",
            ]
        )
    )
    for level, message in SAMPLE_LOG:
        window.log_console.append_log(level, message)

    window.progress_bar.setRange(0, 5)
    window.progress_bar.setValue(3)
    window.progress_bar.setFormat("3 / 5")
    window.stat_done.set_value(3)
    window.stat_ok.set_value(2)
    window.stat_failed.set_value(1)
    window.stat_elapsed.set_value("01:47")
    window.current_url_label.setText("https://github.com/microsoft/playwright-python")
    window.action_hint.setText("Capturing... press Stop to finish the current page and abort.")
    window.stop_button.setEnabled(True)

    if theme_name == "light":
        from app.ui import theme as _theme

        _theme.set_current(_theme.LIGHT)
        window._apply_current_theme()

    window.show()
    app.processEvents()
    QTimer.singleShot(300, app.quit)
    app.exec()

    suffix = "-light" if theme_name == "light" else ""
    out = ROOT / "docs" / f"ui-preview{suffix}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    window.grab().save(str(out))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
