"""Dark, Fluent-inspired theme: colour tokens, QPalette and the global stylesheet.

Every colour lives in :class:`Palette` so the whole application can be
re-skinned (or a light theme added later) by changing one object.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass

from PyQt6.QtCore import QDir, QPoint, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPalette, QPen
from PyQt6.QtWidgets import QApplication

_CHECKMARK_ASSET: str | None = None


@dataclass(frozen=True)
class Palette:
    """Colour tokens for one theme (dark or light)."""

    window: str = "#15171C"  # application background
    surface: str = "#1D2027"  # cards
    surface_alt: str = "#232732"  # raised inner areas, inputs
    surface_hover: str = "#2A2F3C"
    border: str = "#2E333F"
    border_strong: str = "#3C4250"

    text: str = "#E8EAF0"
    text_muted: str = "#9AA3B4"
    text_disabled: str = "#626B7C"

    accent: str = "#3B82F6"
    accent_hover: str = "#4F90FA"
    accent_pressed: str = "#2F6FDB"
    accent_soft: str = "#1E2C44"

    success: str = "#3FB950"
    warning: str = "#D29922"
    error: str = "#F85149"
    info: str = "#58A6FF"

    danger: str = "#DA3633"
    danger_hover: str = "#F0473F"

    console: str = "#101216"  # log terminal background

    font_family: str = "Segoe UI"
    mono_family: str = "Cascadia Mono, Consolas, 'DejaVu Sans Mono', monospace"

    radius: int = 10
    radius_sm: int = 6


DARK = Palette()

LIGHT = Palette(
    window="#EEF1F6",
    surface="#FFFFFF",
    surface_alt="#F7F8FB",
    surface_hover="#EAEFF5",
    border="#D9DFE8",
    border_strong="#C2CAD6",
    text="#1B1F27",
    text_muted="#5B6572",
    text_disabled="#9AA3AF",
    accent="#2563EB",
    accent_hover="#1D4FD7",
    accent_pressed="#1E40AF",
    accent_soft="#E1EAFD",
    success="#15803D",
    warning="#B45309",
    error="#DC2626",
    info="#2563EB",
    danger="#DC2626",
    danger_hover="#B91C1C",
    console="#FFFFFF",
)

MIDNIGHT = Palette(
    window="#0B1020",
    surface="#121832",
    surface_alt="#182042",
    surface_hover="#1E2752",
    border="#26305A",
    border_strong="#33406F",
    text="#DCE3FF",
    text_muted="#8E9BD0",
    text_disabled="#5A6699",
    accent="#7C8CFF",
    accent_hover="#93A1FF",
    accent_pressed="#6474E8",
    accent_soft="#232C55",
    success="#37D67A",
    warning="#E0B341",
    error="#FF6B6B",
    info="#63B3FF",
    danger="#FF5C5C",
    danger_hover="#FF7B7B",
    console="#080C18",
)

HIGH_CONTRAST = Palette(
    window="#000000",
    surface="#0A0A0A",
    surface_alt="#141414",
    surface_hover="#1F1F1F",
    border="#FFFFFF",
    border_strong="#FFFFFF",
    text="#FFFFFF",
    text_muted="#E6E6E6",
    text_disabled="#A6A6A6",
    accent="#FFFF00",
    accent_hover="#FFFF66",
    accent_pressed="#E6E600",
    accent_soft="#333300",
    success="#00FF7F",
    warning="#FFD400",
    error="#FF4D4D",
    info="#4DC3FF",
    danger="#FF4D4D",
    danger_hover="#FF6B6B",
    console="#000000",
)

#: Named theme registry (order = menu order).
THEMES = {
    "dark": DARK,
    "light": LIGHT,
    "midnight": MIDNIGHT,
    "high-contrast": HIGH_CONTRAST,
}


def available_themes() -> list:
    return list(THEMES.keys())


def palette_by_name(name: str) -> Palette:
    return THEMES.get(name, DARK)


def set_by_name(name: str) -> None:
    set_current(palette_by_name(name))


#: Backwards/forwards-compatible handle for the *default* (dark) palette.
PALETTE = DARK

# The theme currently in effect. Widgets read ``current()`` at draw time so a
# runtime switch restyles everything that paints itself.
_current = {"palette": DARK}


def current() -> Palette:
    """Return the palette currently in effect."""
    return _current["palette"]


def set_current(palette: Palette) -> None:
    """Switch the active palette (callers then re-apply the stylesheet)."""
    _current["palette"] = palette


def theme_name() -> str:
    for name, palette in THEMES.items():
        if current() is palette:
            return name
    return "dark"


def checkmark_asset() -> str:
    """Render a white tick PNG once and return its path for use in QSS.

    Qt's stylesheet engine has no vector primitives, so a checked checkbox
    needs a real image file. Drawing it at start-up keeps the app free of
    binary resources while still showing a proper tick mark.
    """
    global _CHECKMARK_ASSET
    if _CHECKMARK_ASSET:
        return _CHECKMARK_ASSET

    from PyQt6.QtGui import QImage  # noqa: PLC0415 - needs a QGuiApplication

    size = 32
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor("#FFFFFF"), 4)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPolyline([QPoint(8, 17), QPoint(14, 23), QPoint(25, 10)])
    painter.end()

    directory = QDir(os.path.join(tempfile.gettempdir(), "fullpage-capture-bot"))
    directory.mkpath(".")
    path = directory.filePath("check.png")
    image.save(path, "PNG")

    _CHECKMARK_ASSET = path.replace("\\", "/")
    return _CHECKMARK_ASSET


def build_qpalette(p: Palette = PALETTE) -> QPalette:
    """A matching QPalette so native dialogs inherit the dark colours."""
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(p.window))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(p.text))
    palette.setColor(QPalette.ColorRole.Base, QColor(p.surface_alt))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(p.surface))
    palette.setColor(QPalette.ColorRole.Text, QColor(p.text))
    palette.setColor(QPalette.ColorRole.Button, QColor(p.surface))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(p.text))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(p.surface_alt))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(p.text))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(p.accent))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    palette.setColor(QPalette.ColorRole.Link, QColor(p.info))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(p.text_muted))
    for role in (QPalette.ColorRole.Light, QPalette.ColorRole.Midlight):
        palette.setColor(role, QColor(p.surface_hover))
    palette.setColor(QPalette.ColorRole.Mid, QColor(p.border))
    palette.setColor(QPalette.ColorRole.Dark, QColor(p.border))
    palette.setColor(QPalette.ColorRole.Shadow, QColor("#0B0C0F"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(p.text_disabled))
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(p.text_disabled)
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(p.text_disabled)
    )
    return palette


def build_stylesheet(p: Palette | None = None, check_asset: str = "") -> str:
    """Return the application-wide QSS string for ``p`` (default: active theme)."""
    p = p or current()
    r = p.radius
    rs = p.radius_sm
    tick = f'url("{check_asset}")' if check_asset else "none"

    return f"""
/* -------------------------------------------------------------- globals */
* {{
    font-family: "{p.font_family}", "Helvetica Neue", Arial, sans-serif;
    font-size: 13px;
    outline: none;
}}

QWidget {{
    color: {p.text};
    background: transparent;
}}

QMainWindow, QDialog, QWidget#MainWindow {{
    background: {p.window};
}}

QScrollArea#SettingsScroll {{
    background: transparent;
    border: none;
}}
QWidget#SettingsViewport, QWidget#SettingsContainer {{
    background: transparent;
    border: none;
}}

QToolTip {{
    background: {p.surface_alt};
    color: {p.text};
    border: 1px solid {p.border_strong};
    padding: 6px 8px;
    border-radius: {rs}px;
}}

/* --------------------------------------------------------------- cards */
QFrame#Card {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: {r}px;
}}

QLabel#CardTitle {{
    font-size: 13px;
    font-weight: 700;
    color: {p.text};
    background: transparent;
    border: none;
}}

QLabel#CardSubtitle, QLabel#MutedLabel {{
    color: {p.text_muted};
    background: transparent;
    border: none;
}}

QLabel#StepBadge {{
    background: {p.accent_soft};
    color: {p.accent_hover};
    border: none;
    border-radius: 9px;
    font-weight: 700;
    font-size: 11px;
    padding: 2px 0;
}}

QLabel#AppTitle {{
    font-size: 20px;
    font-weight: 700;
    color: {p.text};
    background: transparent;
    border: none;
}}

QLabel#AppSubtitle {{
    color: {p.text_muted};
    font-size: 12px;
    background: transparent;
    border: none;
}}

QLabel#StatValue {{
    font-size: 18px;
    font-weight: 700;
    background: transparent;
    border: none;
}}

QLabel#StatLabel {{
    color: {p.text_muted};
    font-size: 10px;
    font-weight: 600;
    background: transparent;
    border: none;
}}

QFrame#StatChip {{
    background: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: {rs}px;
}}

/* -------------------------------------------------------------- inputs */
QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: {rs}px;
    padding: 7px 9px;
    selection-background-color: {p.accent};
    selection-color: #FFFFFF;
}}

QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {p.accent};
}}

QLineEdit:disabled, QPlainTextEdit:disabled, QSpinBox:disabled,
QDoubleSpinBox:disabled, QComboBox:disabled {{
    color: {p.text_disabled};
    background: {p.surface};
}}

QPlainTextEdit#LogConsole {{
    background: {p.console};
    border: 1px solid {p.border};
    border-radius: {rs}px;
    font-family: {p.mono_family};
    font-size: 12px;
    padding: 8px;
}}

QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    background: {p.surface_hover};
    border: none;
    width: 16px;
}}

QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: none; border-left: 4px solid transparent;
    border-right: 4px solid transparent; border-bottom: 5px solid {p.text_muted};
    width: 0; height: 0;
}}

QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: none; border-left: 4px solid transparent;
    border-right: 4px solid transparent; border-top: 5px solid {p.text_muted};
    width: 0; height: 0;
}}

QComboBox::drop-down {{
    border: none;
    width: 22px;
}}

QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {p.text_muted};
    width: 0; height: 0;
    margin-right: 8px;
}}

QComboBox QAbstractItemView {{
    background: {p.surface_alt};
    border: 1px solid {p.border_strong};
    border-radius: {rs}px;
    selection-background-color: {p.accent};
    selection-color: #FFFFFF;
    padding: 4px;
}}

/* ------------------------------------------------------------ buttons */
QPushButton {{
    background: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: {rs}px;
    padding: 8px 14px;
    font-weight: 600;
    color: {p.text};
}}

QPushButton:hover {{ background: {p.surface_hover}; border-color: {p.border_strong}; }}
QPushButton:pressed {{ background: {p.border}; }}

QPushButton:disabled {{
    color: {p.text_disabled};
    background: {p.surface};
    border-color: {p.border};
}}

QPushButton#PrimaryButton {{
    background: {p.accent};
    border: 1px solid {p.accent};
    color: #FFFFFF;
    font-size: 14px;
    font-weight: 700;
    padding: 12px 20px;
    border-radius: 8px;
}}
QPushButton#PrimaryButton:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}
QPushButton#PrimaryButton:pressed {{ background: {p.accent_pressed}; }}
QPushButton#PrimaryButton:disabled {{
    background: {p.surface_alt}; border-color: {p.border}; color: {p.text_disabled};
}}

QPushButton#DangerButton {{
    background: transparent;
    border: 1px solid {p.danger};
    color: {p.danger_hover};
}}
QPushButton#DangerButton:hover {{ background: {p.danger}; color: #FFFFFF; }}
QPushButton#DangerButton:disabled {{ border-color: {p.border}; color: {p.text_disabled}; background: transparent; }}

QPushButton#GhostButton {{
    background: transparent;
    border: 1px solid transparent;
    color: {p.text_muted};
    padding: 6px 10px;
}}
QPushButton#GhostButton:hover {{ background: {p.surface_alt}; color: {p.text}; }}

QToolButton#DisclosureButton {{
    background: transparent;
    border: none;
    color: {p.text_muted};
    text-align: left;
    padding: 4px 0;
    font-weight: 600;
}}
QToolButton#DisclosureButton:hover {{ color: {p.text}; }}

/* ----------------------------------------------------------- controls */
QCheckBox {{ spacing: 8px; color: {p.text}; background: transparent; }}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {p.border_strong};
    border-radius: 4px;
    background: {p.surface_alt};
}}
QCheckBox::indicator:hover {{ border-color: {p.accent}; }}
QCheckBox::indicator:checked {{
    background: {p.accent};
    border-color: {p.accent};
    image: {tick};
}}

QProgressBar {{
    background: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: 7px;
    height: 14px;
    text-align: center;
    color: {p.text};
    font-size: 11px;
    font-weight: 600;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 {p.accent_pressed}, stop:1 {p.accent_hover});
    border-radius: 6px;
}}

QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {p.border_strong}; border-radius: 4px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.text_disabled}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}

QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {p.border_strong}; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}

QStatusBar {{
    background: {p.surface};
    color: {p.text_muted};
    border-top: 1px solid {p.border};
}}
QStatusBar::item {{ border: none; }}

QMenuBar {{ background: {p.surface}; border-bottom: 1px solid {p.border}; color: {p.text}; }}
QMenuBar::item:selected {{ background: {p.surface_hover}; }}
QMenu {{ background: {p.surface_alt}; border: 1px solid {p.border_strong}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px; border-radius: {rs}px; }}
QMenu::item:selected {{ background: {p.accent}; color: #FFFFFF; }}

QSplitter::handle {{ background: transparent; }}
"""


def apply_theme(app: QApplication, palette: Palette | None = None) -> None:
    """Apply the given (or active) theme to the whole application."""
    palette = palette or current()
    app.setPalette(build_qpalette(palette))
    try:
        asset = checkmark_asset()
    except Exception:  # pragma: no cover - rendering the tick is cosmetic
        asset = ""
    app.setStyleSheet(build_stylesheet(palette, asset))

    base_font = QFont(palette.font_family, 10)
    base_font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    app.setFont(base_font)
