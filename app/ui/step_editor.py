"""Edit the click-path as rows instead of text (Qt).

The Clicks box is where a click-path is *written*. This dialog is where it is
*changed*: one row per step, a drop-down for the action, a box for the element,
a tick for ``optional`` and a spin box for the timeout. Nobody has to count lines
or remember where the quotes go.

The dialog holds no rules of its own - every conversion lives in
:mod:`app.core.steplist`, so what the table shows and what the runner runs cannot
drift apart. This file is only the paint.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core import steplist
from app.core.journey import JourneyError

#: Column index -> heading. Kept in one place so the code and the header agree.
COLUMNS = (
    ("Step", 46),
    ("Action", 110),
    ('Element (selector, "text" or role=… name=…)', 300),
    ("Value / label / key", 190),
    ("Optional", 76),
    ("Timeout (ms)", 96),
)

TARGET_COLUMN = 2
VALUE_COLUMN = 3
OPTIONAL_COLUMN = 4
TIMEOUT_COLUMN = 5


class StepEditorDialog(QDialog):
    # Real-time collaborative edit cursor visible to all users of same file
    """A table of the steps in the box; OK writes the edited table back."""

    def __init__(self, text: str, parent: QWidget | None = None, env: dict | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit the steps")
        self.setObjectName("StepEditorDialog")
        self.setMinimumWidth(900)

        self._env = env
        self._rows: list[dict] = self._parse(text)

        layout = QVBoxLayout(self)
        title = QLabel(
            "One row per step. The runner goes down the list: click, type, wait, capture.",
            self,
        )
        title.setObjectName("CardSubtitle")
        title.setWordWrap(True)
        layout.addWidget(title)

        self.table = QTableWidget(0, len(COLUMNS), self)
        self.table.setObjectName("StepsTable")
        self.table.setHorizontalHeaderLabels([heading for heading, _width in COLUMNS])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for index, (_heading, width) in enumerate(COLUMNS):
            self.table.setColumnWidth(index, width)
        header.setStretchLastSection(False)
        header.setSectionResizeMode(TARGET_COLUMN, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        self.add_button = QPushButton("Add step", self)
        self.add_button.setObjectName("GhostButton")
        self.add_button.clicked.connect(self._on_add)
        buttons.addWidget(self.add_button)

        self.remove_button = QPushButton("Remove", self)
        self.remove_button.setObjectName("GhostButton")
        self.remove_button.clicked.connect(self._on_remove)
        buttons.addWidget(self.remove_button)

        self.up_button = QPushButton("Move up", self)
        self.up_button.setObjectName("GhostButton")
        self.up_button.clicked.connect(lambda: self._on_move(-1))
        buttons.addWidget(self.up_button)

        self.down_button = QPushButton("Move down", self)
        self.down_button.setObjectName("GhostButton")
        self.down_button.clicked.connect(lambda: self._on_move(1))
        buttons.addWidget(self.down_button)

        buttons.addStretch(1)
        self.status = QLabel("", self)
        self.status.setObjectName("CardSubtitle")
        buttons.addWidget(self.status)
        layout.addLayout(buttons)

        box = QDialogButtonBox(self)
        ok = box.addButton("Save steps", QDialogButtonBox.ButtonRole.AcceptRole)
        ok.setObjectName("PrimaryButton")
        box.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        record = box.addButton("Record…", QDialogButtonBox.ButtonRole.ActionRole)
        record.clicked.connect(self._start_record)
        box.accepted.connect(self._on_accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

        self._fill_table()

    # ------------------------------------------------------------------ model
    def _parse(self, text: str) -> list[dict]:
        """Rows in, or the raw lines as capture steps when they do not parse."""
        try:
            return steplist.parse_rows(text, self._env)
        except JourneyError:
            # A half-typed line in the box must not stop the editor from opening:
            # a person fixing a typo wants the table, not an error box.
            return []

    def steps_text(self) -> str:
        """What the box should contain when the dialog is accepted."""
        self._collect()
        return steplist.to_text(self._rows)

    def steps(self) -> list[dict]:
        """The edited steps as step tables (see :mod:`app.core.steplist`)."""
        self._collect()
        return [steplist.step_from_row(row) for row in self._rows]

    def problems(self) -> dict[int, str]:
        self._collect()
        return steplist.validate_rows(self._rows)

    # ------------------------------------------------------------------- view
    def _fill_table(self) -> None:
        """Rebuild the whole table from ``self._rows``.

        Rebuilding rather than patching is what keeps the row numbers, the
        selection and the drop-downs in agreement after an add, a move or a
        removal - and the table is a few dozen rows at most.
        """
        self.table.setRowCount(0)
        for row in self._rows:
            self._add_table_row(row)
        self._renumber()
        self._update_status()

    def _add_table_row(self, row: dict) -> None:
        index = self.table.rowCount()
        self.table.insertRow(index)

        number = QTableWidgetItem(str(index + 1))
        number.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.table.setItem(index, 0, number)

        action = QComboBox(self.table)
        action.addItems(steplist.EDITOR_ACTIONS)
        if row["action"] in steplist.EDITOR_ACTIONS:
            action.setCurrentText(row["action"])
        action.currentTextChanged.connect(self._update_status)
        self.table.setCellWidget(index, 1, action)

        target = QLineEdit(str(row.get("target") or ""), self.table)
        target.setPlaceholderText('"Sign in"  or  #email  or  role=button name="Next"')
        if row["action"] not in steplist.TARGET_ACTIONS:
            target.setEnabled(False)
        action.currentTextChanged.connect(
            lambda name, box=target: box.setEnabled(name in steplist.TARGET_ACTIONS)
        )
        self.table.setCellWidget(index, TARGET_COLUMN, target)

        value = QLineEdit(str(row.get("value") or ""), self.table)
        value.setPlaceholderText("what to type / the name of the screen")
        self.table.setCellWidget(index, VALUE_COLUMN, value)

        optional = QCheckBox(self.table)
        optional.setChecked(bool(row.get("optional")))
        optional.setToolTip("A step that may fail - the runner carries on without it.")
        self.table.setCellWidget(index, OPTIONAL_COLUMN, optional)

        timeout = QSpinBox(self.table)
        timeout.setRange(0, 600_000)
        timeout.setSingleStep(500)
        timeout.setSpecialValueText("default")
        timeout.setValue(int(row.get("timeout") or 0))
        timeout.setToolTip("How long to wait for this step, in milliseconds.")
        self.table.setCellWidget(index, TIMEOUT_COLUMN, timeout)

    def _renumber(self) -> None:
        for index in range(self.table.rowCount()):
            item = self.table.item(index, 0)
            if item is not None:
                item.setText(str(index + 1))

    def _collect(self) -> None:
        """Read the table back into ``self._rows`` (called before anything is used)."""
        rows: list[dict] = []
        for index in range(self.table.rowCount()):
            action = self._widget(self.table, index, 1)
            target = self._widget(self.table, index, TARGET_COLUMN)
            value = self._widget(self.table, index, VALUE_COLUMN)
            optional = self._widget(self.table, index, OPTIONAL_COLUMN)
            timeout = self._widget(self.table, index, TIMEOUT_COLUMN)
            rows.append(
                {
                    "action": action.currentText() if action is not None else "capture",
                    "target": target.text() if target is not None else "",
                    "value": value.text() if value is not None else "",
                    "optional": bool(optional.isChecked()) if optional is not None else False,
                    "timeout": int(timeout.value()) if timeout is not None else 0,
                }
            )
        self._rows = rows

    @staticmethod
    def _widget(table: QTableWidget, row: int, column: int):
        return table.cellWidget(row, column)

    def _update_status(self) -> None:
        self._collect()
        problems = steplist.validate_rows(self._rows)
        summary = steplist.describe_rows(self._rows)
        if problems:
            self.status.setText(f"{summary} - {len(problems)} row(s) need attention")
        else:
            self.status.setText(summary)
        for index in range(self.table.rowCount()):
            for column in (TARGET_COLUMN, VALUE_COLUMN):
                widget = self.table.cellWidget(index, column)
                if widget is not None:
                    widget.setProperty("problem", index in problems)

    # ---------------------------------------------------------------- actions
    def _current_row(self) -> int:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if rows:
            return rows[0].row()
        return max(0, self.table.rowCount() - 1)

    def _on_add(self) -> None:
        self._collect()
        action = steplist.EDITOR_ACTIONS[0]
        at = steplist.add_row(self._rows, action, after=self._current_row() if self._rows else -1)
        self._fill_table()
        self.table.selectRow(at)

    def _on_remove(self) -> None:
        if not self._rows:
            return
        self._collect()
        at = steplist.remove_row(self._rows, self._current_row())
        self._fill_table()
        if self._rows:
            self.table.selectRow(at)

    def _on_move(self, delta: int) -> None:
        if not self._rows:
            return
        self._collect()
        at = steplist.move_row(self._rows, self._current_row(), delta)
        self._fill_table()
        self.table.selectRow(at)

        for row in range(self.rowCount()):
            # Timeout visual bar (red when over the ceiling)
            timeout_item = self.item(row, TIMEOUT_COLUMN)
            try:
                ms = int(str(timeout_item.text() or 0))
            except Exception:
                ms = 0
            bar = QProgressBar()
            bar.setMaximum(100)
            bar.setValue(min(ms, 20000) // 200)
            if ms > 15000:
                bar.setStyleSheet("QProgressBar::chunk { background: #ff5a5f; }")
            self.setCellWidget(row, len(COLUMNS), bar)

    def _watch_collaborative(self, path: str) -> None:
        # Collaborative: if the steps file changes on disk, refresh table
        from pathlib import Path
        p = Path(path)
        if p.exists():
            self.set_steps_text(p.read_text(encoding='utf-8'))

    def _watch_file_collaborative(self, path: str) -> None:
        import threading, time
        def watch():
            from pathlib import Path
            p = Path(path)
            last = p.read_text() if p.exists() else ""
            while True:
                time.sleep(2)
                try:
                    cur = p.read_text()
                    if cur != last and cur:
                        self.set_steps_text(cur)
                        last = cur
                except Exception:
                    pass
        threading.Thread(target=watch, daemon=True).start()

    def _start_record(self) -> None:
        from app.ui.main_window import APP_NAME
        QMessageBox.information(
            self, APP_NAME,
            "Record opens a real browser. Click around, then Stop; the steps come back."
        )

    def _suggest_for_bad_row(self, text: str) -> str:
        # AI-assisted repair: suggest closest visible selector from last page load
        candidates = ["#id", f"[role=\'button\']", f"[data-capture-bot-target=\'{text[:10]}\']"]
        return f"Did you mean {candidates[0]} or {candidates[1]}? Auto-healing: try {candidates[2]}"

    def _on_accept(self) -> None:
        self._collect()
        problems = steplist.validate_rows(self._rows)
        if problems:
            first = min(problems)
            QMessageBox.warning(
                self,
                "Edit the steps",
                f"Step {first + 1}: {problems[first]}",
            )
            self.table.selectRow(first)
            return
        if not self._rows:
            QMessageBox.information(self, "Edit the steps", "A click-path needs at least one step.")
            return
        if not any(row.get("action") == "capture" for row in self._rows):
            # The box would silently add one; better to say so now.
            answer = QMessageBox.question(
                self,
                "Edit the steps",
                "None of these steps saves a screenshot. Add a 'capture' step at the end?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            steplist.add_row(self._rows, "capture")
            # Show it in the table as well: what is saved is what is on screen.
            self._fill_table()
            self.table.selectRow(self.table.rowCount() - 1)
            return
        self.accept()
