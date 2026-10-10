"""Side-by-side before/after comparison with a highlighted difference (Qt UI)."""

from __future__ import annotations

import io
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from app.core.imagediff import diff_ratio

try:  # Pillow is a runtime dependency; used to build the difference image.
    from PIL import Image, ImageChops
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    ImageChops = None  # type: ignore[assignment]

_PANEL_WIDTH = 320


def make_diff_image(a_path: str, b_path: str):
    """Return a PIL image highlighting pixel differences (amplified), or None."""
    if Image is None:  # pragma: no cover
        return None
    try:
        before = Image.open(a_path).convert("RGB")
        after = Image.open(b_path).convert("RGB")
    except Exception:  # noqa: BLE001 - unreadable image
        return None
    if after.size != before.size:
        after = after.resize(before.size)
    diff = ImageChops.difference(before, after)
    return diff.point(lambda pixel: min(255, pixel * 4))


def _pixmap(image) -> QPixmap:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    qimage = QImage.fromData(buffer.getvalue())
    return QPixmap.fromImage(qimage)


def _scaled_pixmap(path: str) -> QPixmap:
    pixmap = QPixmap(str(path))
    if pixmap.isNull():
        return pixmap
    return pixmap.scaledToWidth(_PANEL_WIDTH, Qt.TransformationMode.SmoothTransformation)


class CompareDialog(QDialog):
    """Shows two captures side by side plus their highlighted difference."""

    def __init__(
        self,
        before_path: str,
        after_path: str,
        parent=None,
        before_title: str = "Before",
        after_title: str = "After",
    ) -> None:
        super().__init__(parent)
        self.before_path = before_path
        self.after_path = after_path
        self.before_title = before_title
        self.after_title = after_title
        self.setWindowTitle("Compare captures")
        self._build_ui()
        self._populate()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        row = QHBoxLayout()
        self.before_label = QLabel(self)
        self.after_label = QLabel(self)
        self.diff_label = QLabel(self)
        self.before_heading = QLabel(self.before_title, self)
        self.after_heading = QLabel(self.after_title, self)
        self.diff_heading = QLabel("Difference", self)
        for heading, widget in (
            (self.before_heading, self.before_label),
            (self.after_heading, self.after_label),
            (self.diff_heading, self.diff_label),
        ):
            column = QVBoxLayout()
            heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
            widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
            widget.setMinimumSize(_PANEL_WIDTH, 200)
            column.addWidget(heading)
            column.addWidget(widget, 1)
            row.addLayout(column, 1)
        layout.addLayout(row, 1)

        self.info_label = QLabel("", self)
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.info_label)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.close_button = QPushButton("Close", self)
        self.close_button.clicked.connect(self.accept)
        bottom.addWidget(self.close_button)
        layout.addLayout(bottom)

    def _populate(self) -> None:
        before_ok = Path(self.before_path).exists()
        after_ok = Path(self.after_path).exists()

        if before_ok:
            self.before_label.setPixmap(_scaled_pixmap(self.before_path))
        else:
            self.before_label.setText("missing")
        if after_ok:
            self.after_label.setPixmap(_scaled_pixmap(self.after_path))
        else:
            self.after_label.setText("missing")

        ratio = None
        if before_ok and after_ok:
            try:
                ratio = diff_ratio(
                    Path(self.before_path).read_bytes(), Path(self.after_path).read_bytes()
                )
            except Exception:  # noqa: BLE001 - an undecodable pair simply has no ratio
                ratio = None
        diff_image = (
            make_diff_image(self.before_path, self.after_path) if (before_ok and after_ok) else None
        )
        if diff_image is not None:
            self.diff_label.setPixmap(
                _pixmap(diff_image).scaledToWidth(
                    _PANEL_WIDTH, Qt.TransformationMode.SmoothTransformation
                )
            )
        else:
            self.diff_label.setText("n/a")

        if ratio is None:
            self.info_label.setText("Could not compare (a file is missing).")
        elif self.before_title == "Before":
            self.info_label.setText(f"Visual difference: {ratio:.1%}")
        else:
            self.info_label.setText(
                f"Visual difference vs {self.before_title.lower()}: {ratio:.1%}"
            )
