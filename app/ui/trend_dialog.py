"""A per-site change-trend chart (Qt UI, no external charting dependency)."""

from __future__ import annotations

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.history_source import get_reader


class TrendChart(QWidget):
    """Draws a diff-over-time line for one site (diff is 0..1)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._points: list = []
        self.setMinimumHeight(220)

    def set_data(self, points: list) -> None:
        self._points = [p for p in points if p.get("diff") is not None]
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width, height = self.width(), self.height()
        margin = 34
        plot_w = max(10, width - 2 * margin)
        plot_h = max(10, height - 2 * margin)

        # axes
        painter.setPen(QPen(QColor("#888"), 1))
        painter.drawLine(margin, margin, margin, height - margin)
        painter.drawLine(margin, height - margin, width - margin, height - margin)
        painter.drawText(4, margin - 6, "1.0")
        painter.drawText(4, height - margin, "0.0")

        values = [float(p["diff"]) for p in self._points]
        if not values:
            painter.drawText(margin + 8, height // 2, "No change data for this site yet.")
            painter.end()
            return

        count = len(values)
        step_x = plot_w / count if count > 1 else 0.0

        def to_point(i: int, value: float) -> QPointF:
            x = margin + (i * step_x if count > 1 else plot_w / 2)
            y = (height - margin) - min(1.0, max(0.0, value)) * plot_h
            return QPointF(x, y)

        painter.setPen(QPen(QColor("#3B82F6"), 2))
        for i in range(count - 1):
            painter.drawLine(to_point(i, values[i]), to_point(i + 1, values[i + 1]))

        painter.setBrush(QColor("#3B82F6"))
        painter.setPen(Qt.PenStyle.NoPen)
        for i, value in enumerate(values):
            point = to_point(i, value)
            painter.drawEllipse(point, 3, 3)
        painter.end()


class TrendDialog(QDialog):
    """Shows the change trend for a chosen site."""

    def __init__(self, output_dir: str, parent=None) -> None:
        super().__init__(parent)
        self.output_dir = output_dir
        self.setWindowTitle("Change trend")
        self.resize(640, 420)
        self._build_ui()
        self._populate_sites()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Site:", self))
        self.site_combo = QComboBox(self)
        top.addWidget(self.site_combo, 1)
        layout.addLayout(top)

        self.chart = TrendChart(self)
        layout.addWidget(self.chart, 1)

        self.summary_label = QLabel("", self)
        layout.addWidget(self.summary_label)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.close_button = QPushButton("Close", self)
        self.close_button.clicked.connect(self.accept)
        bottom.addWidget(self.close_button)
        layout.addLayout(bottom)

        self.site_combo.currentIndexChanged.connect(self._on_site_changed)

    def _populate_sites(self) -> None:
        self.site_combo.blockSignals(True)
        self.site_combo.clear()
        with get_reader(self.output_dir) as reader:
            site_list = reader.sites()
        for url in site_list:
            self.site_combo.addItem(url, url)
        self.site_combo.blockSignals(False)
        self._on_site_changed()

    def _on_site_changed(self, *_args) -> None:
        url = self.site_combo.currentData()
        if not url:
            self.chart.set_data([])
            self.summary_label.setText("No captured sites yet.")
            return
        with get_reader(self.output_dir) as reader:
            trend = reader.trend_for_url(url)
        self.chart.set_data(trend)
        changes = sum(1 for point in trend if point["diff"] and point["diff"] > 0)
        self.summary_label.setText(f"{len(trend)} capture(s), {changes} change(s) recorded.")
