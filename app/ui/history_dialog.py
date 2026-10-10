"""A dialog that shows the per-site capture/change history (Qt UI only)."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.core.history_source import get_reader

#: A preview longer than this is shown shortened; "Save as" always writes it all.
PREVIEW_LIMIT = 200_000


class HistoryDialog(QDialog):
    """Lists every captured URL over time, newest first, with an optional filter."""

    def __init__(self, output_dir: str, parent=None) -> None:
        super().__init__(parent)
        self.output_dir = output_dir
        self.setWindowTitle("Capture history")
        self.resize(760, 480)
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Site:", self))
        self.site_combo = QComboBox(self)
        self.site_combo.addItem("All sites", "")
        top.addWidget(self.site_combo, 1)
        self.refresh_button = QPushButton("Refresh", self)
        top.addWidget(self.refresh_button)
        self.compare_button = QPushButton("Compare last two", self)
        top.addWidget(self.compare_button)
        self.compare_baseline_button = QPushButton("Compare to baseline", self)
        self.compare_baseline_button.setToolTip(
            "Show the pinned baseline next to this site's latest capture."
        )
        top.addWidget(self.compare_baseline_button)
        self.trend_button = QPushButton("Trend", self)
        top.addWidget(self.trend_button)
        self.pin_button = QPushButton("Pin baseline", self)
        self.pin_button.setToolTip(
            "Mark the selected site's latest capture as the known-good baseline."
        )
        top.addWidget(self.pin_button)
        layout.addLayout(top)

        self.table = QTableWidget(self)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["When", "Site", "Status", "Diff"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table, 1)

        self.export_preview = QPlainTextEdit(self)
        self.export_preview.setReadOnly(True)
        self.export_preview.setVisible(False)
        self.export_preview.setMaximumHeight(180)
        self.export_preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.export_preview)

        bottom = QHBoxLayout()
        bottom.addWidget(QLabel("Export:", self))
        self.export_format = QComboBox(self)
        self.export_format.addItem("CSV", "csv")
        self.export_format.addItem("JSON", "json")
        self.export_format.setToolTip(
            "Download format - the same bytes /api/export and history --export produce."
        )
        bottom.addWidget(self.export_format)
        self.preview_button = QPushButton("Preview", self)
        self.preview_button.setToolTip("Show the download here before saving it.")
        bottom.addWidget(self.preview_button)
        self.save_export_button = QPushButton("Save as...", self)
        bottom.addWidget(self.save_export_button)
        self.copy_export_button = QPushButton("Copy", self)
        self.copy_export_button.setToolTip("Put the download on the clipboard.")
        bottom.addWidget(self.copy_export_button)
        self.export_status = QLabel("", self)
        bottom.addWidget(self.export_status, 1)
        self.close_button = QPushButton("Close", self)
        bottom.addWidget(self.close_button)
        layout.addLayout(bottom)

        self.refresh_button.clicked.connect(self.refresh)
        self.compare_button.clicked.connect(self._compare)
        self.compare_baseline_button.clicked.connect(self._compare_baseline)
        self.trend_button.clicked.connect(self._open_trend)
        self.pin_button.clicked.connect(self._pin_baseline)
        self.preview_button.clicked.connect(self.preview_export)
        self.save_export_button.clicked.connect(self.save_export)
        self.copy_export_button.clicked.connect(self.copy_export)
        self.export_format.currentIndexChanged.connect(self._export_format_changed)
        self.close_button.clicked.connect(self.accept)
        self.site_combo.currentIndexChanged.connect(self.refresh)

    # -- export (the offline twin of /api/export) -------------------------- #
    def export_download(self) -> tuple[bytes, str, int]:
        """``(body, filename, rows)`` for the current site filter and format.

        The bytes come from the same builder the API and the CLI use, so what is
        previewed here is exactly what a script would download.
        """
        from app.core import api

        fmt = self.export_format.currentData() or "csv"
        page = api.history_page(
            self.output_dir, limit=0, offset=0, url=self.site_combo.currentData() or ""
        )
        body, _content_type, filename = api.format_export(page, fmt)
        return body, filename, int(page.get("count", 0))

    def preview_export(self) -> None:
        """Fill the preview area with the file the buttons would write."""
        body, filename, rows = self.export_download()
        text = body.decode("utf-8", errors="replace")
        truncated = len(text) > PREVIEW_LIMIT
        if truncated:
            text = text[:PREVIEW_LIMIT] + "\n... (preview truncated; 'Save as' writes it all)\n"
        elif not text.endswith("\n"):
            text += "\n"
        self.export_preview.setPlainText(text)
        self.export_preview.setVisible(True)
        self.export_status.setText(f"{filename}: {rows} row(s), {len(body) / 1024:.1f} KB")

    def save_export(self) -> None:
        """Ask for a path and write exactly the bytes the preview showed."""
        body, filename, _rows = self.export_download()
        target = self._ask_save_path(filename)
        if not target:
            return
        try:
            Path(target).write_bytes(body)
        except OSError as exc:
            QMessageBox.warning(self, "Save history", f"Could not write {target}: {exc}")
            return
        self.export_status.setText(f"Saved {Path(target).name}")

    def copy_export(self) -> None:
        """Put the download on the clipboard (handy for pasting into a ticket)."""
        body, filename, _rows = self.export_download()
        QApplication.clipboard().setText(body.decode("utf-8", errors="replace"))
        self.export_status.setText(f"{filename} copied")

    def _ask_save_path(self, filename: str) -> str:
        """The path the user picked, or an empty string when they cancelled."""
        suggested = str(Path(self.output_dir) / filename) if self.output_dir else filename
        target, _selected = QFileDialog.getSaveFileName(
            self, "Save history", suggested, "All files (*)"
        )
        return target

    def _export_format_changed(self) -> None:
        """Keep the preview honest when the format changes.

        ``isHidden`` (rather than ``isVisible``) asks whether the user opened the
        preview at all - a widget inside a window that is not on screen is not
        "visible", but it is not hidden either.
        """
        if not self.export_preview.isHidden():
            self.preview_export()

    def _open_trend(self) -> None:
        from app.ui.trend_dialog import TrendDialog

        TrendDialog(self.output_dir, self).exec()

    def _pin_baseline(self) -> None:
        from app.core import baseline

        url = self.site_combo.currentData()
        if not url:
            QMessageBox.information(
                self, "Pin baseline", "Select a single site first (not 'All sites')."
            )
            return
        pinned = baseline.pin_baseline(self.output_dir, url)
        if pinned is None:
            QMessageBox.warning(
                self, "Pin baseline", "No capture to pin yet - run a capture first."
            )
            return
        QMessageBox.information(self, "Pin baseline", f"Baseline pinned: {pinned.name}")

    def _compare_baseline(self) -> None:
        """Show the pinned baseline next to this site's newest capture."""
        from app.core import baseline
        from app.ui.compare_dialog import CompareDialog

        url = self.site_combo.currentData()
        if not url:
            QMessageBox.information(
                self, "Compare to baseline", "Select a single site first (not 'All sites')."
            )
            return
        pinned = baseline.find_baseline(self.output_dir, url)
        if pinned is None:
            QMessageBox.information(
                self, "Compare to baseline", "This site has no pinned baseline yet."
            )
            return
        with get_reader(self.output_dir) as reader:
            rows = [row for row in reader.flat_rows() if row["url"] == url]
        files = [row["file"] for row in rows if row.get("file") and Path(row["file"]).exists()]
        if not files:
            QMessageBox.information(
                self, "Compare to baseline", "No capture to compare against the baseline yet."
            )
            return
        CompareDialog(
            str(pinned), files[0], self, before_title="Baseline", after_title="Latest"
        ).exec()

    def _compare(self) -> None:
        with get_reader(self.output_dir) as reader:
            rows = reader.flat_rows()
        selected = self.site_combo.currentData()
        if selected:
            rows = [row for row in rows if row["url"] == selected]
        files = [row["file"] for row in rows if row.get("file") and Path(row["file"]).exists()]
        if len(files) < 2:
            QMessageBox.information(self, "Compare", "Need at least two captures to compare.")
            return
        from app.ui.compare_dialog import CompareDialog

        # files are newest-first: [0] is the latest (after), [1] the previous (before).
        CompareDialog(files[1], files[0], self).exec()

    def refresh(self, *_args) -> None:
        with get_reader(self.output_dir) as reader:
            rows = reader.flat_rows()
            site_list = reader.sites()

        current = self.site_combo.currentData()
        self.site_combo.blockSignals(True)
        self.site_combo.clear()
        self.site_combo.addItem("All sites", "")
        for url in site_list:
            self.site_combo.addItem(url, url)
        index = self.site_combo.findData(current)
        self.site_combo.setCurrentIndex(max(0, index))
        self.site_combo.blockSignals(False)

        selected = self.site_combo.currentData()
        if selected:
            rows = [row for row in rows if row["url"] == selected]

        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            diff = row["diff"]
            self.table.setItem(i, 0, QTableWidgetItem(str(row["timestamp"])))
            self.table.setItem(i, 1, QTableWidgetItem(str(row["label"])))
            self.table.setItem(i, 2, QTableWidgetItem(str(row["status"])))
            self.table.setItem(i, 3, QTableWidgetItem("-" if diff is None else f"{diff:.3f}"))
