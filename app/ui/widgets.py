"""Reusable custom widgets that give the application its professional feel.

Every widget here is self-contained: they own their painting and expose a
Qt-native API (``toggled`` signal, ``isChecked()``/``setChecked()``) so the
main window never has to know how they are drawn.
"""

from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import QAction, QColor, QKeyEvent, QPainter, QPen
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.ui.theme import Palette, current


# --------------------------------------------------------------------------- #
# Toggle switch
# --------------------------------------------------------------------------- #
class ToggleSwitch(QWidget):
    """A real sliding toggle switch (not a plain checkbox).

    Emits :pyattr:`toggled` exactly like ``QCheckBox`` and animates the knob
    between the off and on positions. Fully keyboard accessible.
    """

    toggled = pyqtSignal(bool)

    _TRACK_W = 44
    _TRACK_H = 24
    _KNOB_D = 18
    _PAD = 3

    def __init__(
        self, text: str = "", parent: QWidget | None = None, palette: Palette | None = None
    ) -> None:
        super().__init__(parent)
        self._palette = palette
        self._checked = False
        self._position = 0.0  # 0.0 = off, 1.0 = on (animated)

        self._animation = QPropertyAnimation(self, b"knobPosition", self)
        self._animation.setDuration(160)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutQuad)

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAccessibleName(text or "Toggle switch")
        self.setToolTip(text)

        self.setFixedHeight(self._TRACK_H)
        self.setFixedWidth(self._TRACK_W)

    # -- Qt property driving the animation --------------------------------
    def _get_knob_position(self) -> float:
        return self._position

    def _set_knob_position(self, value: float) -> None:
        self._position = float(value)
        self.update()

    knobPosition = pyqtProperty(float, fget=_get_knob_position, fset=_set_knob_position)

    # -- public API --------------------------------------------------------
    def isChecked(self) -> bool:  # noqa: N802 - Qt naming convention
        return self._checked

    def setChecked(self, checked: bool, animate: bool = True) -> None:  # noqa: N802
        checked = bool(checked)
        if checked == self._checked and abs(self._position - (1.0 if checked else 0.0)) < 0.01:
            self._checked = checked
            return

        self._checked = checked
        target = 1.0 if checked else 0.0
        self._animation.stop()
        if animate:
            self._animation.setStartValue(float(self._position))
            self._animation.setEndValue(target)
            self._animation.start()
        else:
            self._position = target
            self.update()
        self.toggled.emit(checked)

    def toggle(self) -> None:
        self.setChecked(not self._checked)

    # -- interaction -------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.toggle()
            event.accept()
            return
        super().keyPressEvent(event)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._TRACK_W, self._TRACK_H)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    # -- painting ----------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p = self._palette or current()

        off_color = QColor(p.border_strong)
        on_color = QColor(p.accent)
        track = _blend(off_color, on_color, self._position)

        if self.isEnabled():
            painter.setPen(QPen(QColor(p.border), 1))
        else:
            track = _blend(track, QColor(p.surface), 0.55)
            painter.setPen(QPen(QColor(p.border), 1))

        track_rect = QRectF(0.5, 0.5, self._TRACK_W - 1, self._TRACK_H - 1)
        painter.setBrush(track)
        painter.drawRoundedRect(track_rect, track_rect.height() / 2, track_rect.height() / 2)

        travel = self._TRACK_W - self._KNOB_D - (2 * self._PAD)
        knob_x = self._PAD + travel * self._position
        knob_rect = QRectF(knob_x, self._PAD, self._KNOB_D, self._KNOB_D)

        knob_color = QColor("#FFFFFF") if self._position > 0.5 else QColor(p.text_muted)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(knob_color)
        painter.drawEllipse(knob_rect)
        painter.end()


def _blend(first: QColor, second: QColor, factor: float) -> QColor:
    """Linear colour blend used for the toggle track."""
    factor = max(0.0, min(1.0, factor))
    return QColor(
        int(first.red() + (second.red() - first.red()) * factor),
        int(first.green() + (second.green() - first.green()) * factor),
        int(first.blue() + (second.blue() - first.blue()) * factor),
    )


# --------------------------------------------------------------------------- #
# Toggle row (switch + title + description)
# --------------------------------------------------------------------------- #
class ToggleRow(QWidget):
    """A switch with a bold title and a muted explanation underneath."""

    toggled = pyqtSignal(bool)

    def __init__(
        self,
        title: str,
        description: str = "",
        parent: QWidget | None = None,
        palette: Palette | None = None,
    ) -> None:
        super().__init__(parent)
        self.switch = ToggleSwitch("", self, palette)
        self.switch.toggled.connect(self.toggled.emit)

        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(2)

        self.title_label = QLabel(title, self)
        self.title_label.setStyleSheet("font-weight: 600; background: transparent; border: none;")
        text_column.addWidget(self.title_label)

        if description:
            self.description_label = QLabel(description, self)
            self.description_label.setObjectName("MutedLabel")
            self.description_label.setWordWrap(True)
            self.description_label.setStyleSheet("font-size: 11px;")
            text_column.addWidget(self.description_label)
        else:
            self.description_label = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addLayout(text_column, 1)
        layout.addWidget(self.switch, 0, Qt.AlignmentFlag.AlignTop)

    def isChecked(self) -> bool:  # noqa: N802
        return self.switch.isChecked()

    def setChecked(self, checked: bool) -> None:  # noqa: N802
        self.switch.setChecked(checked)


# --------------------------------------------------------------------------- #
# Card
# --------------------------------------------------------------------------- #
class Card(QFrame):
    """A titled container used to group related controls."""

    def __init__(
        self,
        title: str = "",
        subtitle: str = "",
        badge: str = "",
        parent: QWidget | None = None,
        title_key: str = "",
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.title_key = title_key
        self.title_label: QLabel | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 16)
        root.setSpacing(10)

        if title:
            header = QHBoxLayout()
            header.setSpacing(8)

            if badge:
                badge_label = QLabel(badge, self)
                badge_label.setObjectName("StepBadge")
                badge_label.setFixedSize(20, 18)
                badge_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                header.addWidget(badge_label)

            title_label = QLabel(title, self)
            title_label.setObjectName("CardTitle")
            self.title_label = title_label
            header.addWidget(title_label)
            header.addStretch(1)
            self.header_layout = header
            root.addLayout(header)

            if subtitle:
                subtitle_label = QLabel(subtitle, self)
                subtitle_label.setObjectName("CardSubtitle")
                subtitle_label.setWordWrap(True)
                root.addWidget(subtitle_label)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(8)
        root.addLayout(self.body, 1)

    def addWidget(self, widget: QWidget) -> None:  # noqa: N802
        self.body.addWidget(widget)

    def addLayout(self, layout) -> None:  # noqa: N802
        self.body.addLayout(layout)


# --------------------------------------------------------------------------- #
# Stat chip
# --------------------------------------------------------------------------- #
class StatChip(QFrame):
    """Compact metric read-out used on the progress dashboard."""

    def __init__(self, label: str, tone: str = "neutral", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("StatChip")
        self._tone = tone

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(0)

        self.value_label = QLabel("0", self)
        self.value_label.setObjectName("StatValue")
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.label_label = QLabel(label.upper(), self)
        self.label_label.setObjectName("StatLabel")
        self.label_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(self.value_label)
        layout.addWidget(self.label_label)
        self.set_tone(tone)

    def set_value(self, value: str | int) -> None:
        self.value_label.setText(str(value))

    def set_tone(self, tone: str) -> None:
        """``tone`` is one of neutral, accent, success, warning, error."""
        p = current()
        color = {
            "neutral": p.text,
            "accent": p.info,
            "success": p.success,
            "warning": p.warning,
            "error": p.error,
        }.get(tone, p.text)
        self._tone = tone
        self.value_label.setStyleSheet(f"color: {color}; font-size: 18px; font-weight: 700;")


# --------------------------------------------------------------------------- #
# Collapsible section
# --------------------------------------------------------------------------- #
class CollapsibleSection(QWidget):
    """A disclosure triangle that shows or hides a block of advanced options."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.button = QToolButton(self)
        self.button.setObjectName("DisclosureButton")
        self.button.setText(title)
        self.button.setCheckable(True)
        self.button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.button.setArrowType(Qt.ArrowType.RightArrow)
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.toggled.connect(self._on_toggled)

        self.content = QWidget(self)
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 6, 0, 0)
        self.content_layout.setSpacing(8)
        self.content.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.button)
        layout.addWidget(self.content)

    def _on_toggled(self, checked: bool) -> None:
        self.content.setVisible(checked)
        self.button.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)

    def addWidget(self, widget: QWidget) -> None:  # noqa: N802
        self.content_layout.addWidget(widget)

    def addLayout(self, layout) -> None:  # noqa: N802
        self.content_layout.addLayout(layout)


# --------------------------------------------------------------------------- #
# Password field with reveal button
# --------------------------------------------------------------------------- #
class PasswordField(QLineEdit):
    """A password input with a trailing eye button that reveals the text."""

    def __init__(self, placeholder: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setEchoMode(QLineEdit.EchoMode.Password)

        # PyQt6 has no QLineEdit.addAction(str, position) overload - the text
        # has to travel inside a QAction.
        self.reveal_action = QAction("show", self)
        self.reveal_action.setToolTip("Show or hide the password")
        self.reveal_action.triggered.connect(self._toggle_reveal)
        self.addAction(self.reveal_action, QLineEdit.ActionPosition.TrailingPosition)

    def _toggle_reveal(self) -> None:
        if self.echoMode() == QLineEdit.EchoMode.Password:
            self.setEchoMode(QLineEdit.EchoMode.Normal)
            self.reveal_action.setText("hide")
        else:
            self.setEchoMode(QLineEdit.EchoMode.Password)
            self.reveal_action.setText("show")


# --------------------------------------------------------------------------- #
# Log console
# --------------------------------------------------------------------------- #
class LogConsole(QPlainTextEdit):
    """A terminal-style log view with level colouring and filtering."""

    MAX_LINES = 3_000

    LEVEL_ORDER = ("DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR")

    def _colors(self) -> dict:
        """Level colours for the *active* theme (read at draw time)."""
        p = current()
        return {
            "DEBUG": p.text_muted,
            "INFO": p.text,
            "SUCCESS": p.success,
            "WARNING": p.warning,
            "ERROR": p.error,
        }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("LogConsole")
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setMaximumBlockCount(self.MAX_LINES)
        self.setUndoRedoEnabled(False)

        self.autoscroll = True
        self._minimum_level = 0  # index into LEVEL_ORDER

    # -- API ---------------------------------------------------------------
    def set_minimum_level(self, level: str) -> None:
        """Hide everything below ``level`` (used by the verbosity combo box)."""
        try:
            self._minimum_level = self.LEVEL_ORDER.index(level.upper())
        except ValueError:
            self._minimum_level = 0

    def append_log(self, level: str, message: str, timestamp: datetime | None = None) -> None:
        """Append one coloured, HTML-escaped line and keep the buffer bounded."""
        key = level.upper()
        if key not in self.LEVEL_ORDER:
            key = "INFO"
        if self.LEVEL_ORDER.index(key) < self._minimum_level:
            return

        stamp = (timestamp or datetime.now()).strftime("%H:%M:%S")
        color = self._colors().get(key, current().text)
        weight = "600" if key in ("ERROR", "SUCCESS", "WARNING") else "400"
        safe_message = _escape_html(message)

        self.appendHtml(
            f'<span style="color:{current().text_disabled};">{stamp}</span> '
            f'<span style="color:{color};font-weight:{weight};">[{key:<7}]</span> '
            f'<span style="color:{color};">{safe_message}</span>'
        )

        if self.autoscroll:
            scrollbar = self.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    def plain_snapshot(self) -> str:
        """Plain text of the visible buffer - used by 'Save log'."""
        return self.toPlainText()

    def clear_console(self) -> None:
        self.clear()


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def muted_label(text: str, parent: QWidget | None = None) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName("MutedLabel")
    label.setWordWrap(True)
    return label


def ghost_button(text: str, tooltip: str = "", parent: QWidget | None = None) -> QPushButton:
    button = QPushButton(text, parent)
    button.setObjectName("GhostButton")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if tooltip:
        button.setToolTip(tooltip)
    return button
