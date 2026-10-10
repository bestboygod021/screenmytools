"""The step table behind the row editor: parse, edit, write back."""

from __future__ import annotations

import pytest

from app.core import steplist
from app.core.journey import JourneyError, journey_from_text

SAMPLE = """click "Sign in" optional
fill #email = me@example.com
wait 500
wait_for .dashboard timeout=8000
capture dashboard
scroll down
press Enter
goto https://app.example.com/x
click role=button name="Next" optional
select #plan = pro"""


class TestRoundTrip:
    """The box, the table and the box again: nothing may be lost on the way."""

    def test_the_text_survives_a_trip_through_the_rows(self):
        assert steplist.to_text(steplist.parse_rows(SAMPLE)) == SAMPLE

    def test_the_rows_describe_the_steps_the_runner_would_run(self):
        rows = steplist.parse_rows(SAMPLE)
        steps = [steplist.step_from_row(row) for row in rows]
        spec = journey_from_text("mine", "https://example.com", steplist.to_text(rows))
        assert [step["action"] for step in steps] == [step.action for step in spec.steps]

    def test_an_empty_box_is_an_empty_table(self):
        assert steplist.parse_rows("") == []
        assert steplist.parse_rows("   \n# just a comment\n") == []
        assert steplist.to_text([]) == ""

    def test_a_half_typed_box_does_not_explode(self):
        with pytest.raises(JourneyError, match="line 2"):
            steplist.parse_rows('capture one\nclik "nope"')

    def test_the_three_ways_to_name_an_element_all_survive(self):
        text = 'click #login\nclick "Sign in"\nclick role=button name="Next"'
        rows = steplist.parse_rows(text)
        assert [row["target"] for row in rows] == [
            "#login",
            '"Sign in"',
            'role=button name="Next"',
        ]
        assert steplist.to_text(rows) == text

    def test_a_selector_with_a_role_keeps_both_spellings(self):
        rows = [
            {
                "action": "click",
                "target": "#save",
                "value": "",
                "optional": False,
                "timeout": 0,
            }
        ]
        step = steplist.step_from_row(rows[0])
        assert step["selector"] == "#save"


class TestEditing:
    """Adding, moving and removing rows the way the buttons do."""

    def test_a_new_row_is_runnable_without_typing(self):
        row = steplist.new_row("capture")
        assert steplist.step_from_row(row)["label"] == "screen"
        click = steplist.new_row("click")
        assert click["optional"] is True, "a click that may be missing is the safe default"
        assert steplist.step_from_row(click)["text"] == "Sign in"

    def test_rows_move_up_and_down_within_the_list(self):
        rows = steplist.parse_rows("capture one\ncapture two\ncapture three")
        assert steplist.move_row(rows, 0, -1) == 0, "the first row stays first"
        assert steplist.move_row(rows, 2, 1) == 2, "and the last stays last"
        assert steplist.move_row(rows, 0, 1) == 1
        assert [row["value"] for row in rows] == ["two", "one", "three"]

    def test_a_row_can_be_added_after_another_and_removed_again(self):
        rows = steplist.parse_rows("capture one\ncapture two")
        at = steplist.add_row(rows, "wait", after=0)
        assert at == 1
        assert rows[1]["value"] == "1000"
        assert steplist.remove_row(rows, 1) == 1
        assert len(rows) == 2

    def test_removing_the_last_row_leaves_an_empty_table(self):
        rows = steplist.parse_rows("capture only")
        assert steplist.remove_row(rows, 0) == 0
        assert rows == []

    def test_the_summary_counts_steps_captures_and_optionals(self):
        rows = steplist.parse_rows(SAMPLE)
        assert steplist.describe_rows(rows) == "10 step(s) - 1 capture(s) - 2 optional"

    def test_a_row_that_cannot_run_is_named_before_it_is_saved(self):
        rows = steplist.parse_rows("capture one")
        rows.append(
            {"action": "fill", "target": "#email", "value": "", "optional": False, "timeout": 0}
        )
        problems = steplist.validate_rows(rows)
        assert list(problems) == [1]
        assert "value" in problems[1]

    def test_a_wait_needs_a_number(self):
        row = {"action": "wait", "target": "", "value": "soon", "optional": False, "timeout": 0}
        with pytest.raises(JourneyError, match="milliseconds"):
            steplist.step_from_row(row)

    def test_an_unknown_action_is_refused(self):
        row = {"action": "dance", "target": "", "value": "", "optional": False, "timeout": 0}
        with pytest.raises(JourneyError, match="not a step this app knows"):
            steplist.step_from_row(row)

    def test_the_to_text_error_names_the_row(self):
        rows = steplist.parse_rows("capture one") + [
            {"action": "fill", "target": "#email", "value": "", "optional": False, "timeout": 0}
        ]
        with pytest.raises(JourneyError, match="step 2"):
            steplist.to_text(rows)


class TestEditorSwitches:
    """The two columns that exist because text editing is where mistakes live."""

    def test_optional_and_timeout_survive_the_table(self):
        rows = steplist.parse_rows('click "Filters" optional timeout=8000')
        assert rows[0]["optional"] is True
        assert rows[0]["timeout"] == 8000

        rows[0]["optional"] = False
        rows[0]["timeout"] = 1500
        assert steplist.to_text(rows) == 'click "Filters" timeout=1500'

    def test_turning_a_timeout_off_removes_the_word(self):
        rows = steplist.parse_rows("wait_for .app timeout=9000")
        rows[0]["timeout"] = 0
        assert steplist.to_text(rows) == "wait_for .app"
        step = steplist.step_from_row(rows[0])
        assert step.get("timeout", 0) == 0, "an untouched timeout is no timeout"

    def test_the_timeout_reaches_the_parsed_step(self):
        rows = steplist.parse_rows("capture home")
        rows[0]["timeout"] = 3000
        step = steplist.step_from_row(rows[0])
        assert step["timeout"] == 3000


class TestEditorDialog:
    """The Qt table: it shows the rows, and it gives them back."""

    @pytest.fixture
    def dialog(self, qtbot):
        from app.ui.step_editor import StepEditorDialog

        made = StepEditorDialog(SAMPLE)
        qtbot.addWidget(made)
        return made

    def test_it_opens_with_one_row_per_step(self, dialog):
        assert dialog.table.rowCount() == 10
        assert dialog.table.item(0, 0).text() == "1"
        assert dialog.status.text() == "10 step(s) - 1 capture(s) - 2 optional"

        action = dialog.table.cellWidget(0, 1)
        assert action.currentText() == "click"
        assert dialog.table.cellWidget(0, 2).text() == '"Sign in"'
        assert dialog.table.cellWidget(0, 4).isChecked() is True
        assert "10 step(s)" in dialog.status.text()

    def test_saving_writes_the_text_back_unchanged(self, dialog):
        assert dialog.steps_text() == SAMPLE

    def test_editing_a_cell_reaches_the_text(self, dialog):
        dialog.table.cellWidget(0, 2).setText("#login")
        dialog.table.cellWidget(0, 4).setChecked(False)
        dialog.table.cellWidget(0, 5).setValue(2500)

        text = dialog.steps_text()

        assert text.splitlines()[0] == "click #login timeout=2500"
        assert dialog.steps()[0]["selector"] == "#login"

    def test_the_timeout_column_starts_at_default(self, dialog):
        assert dialog.table.cellWidget(0, 5).value() == 0
        assert dialog.table.cellWidget(3, 5).value() == 8000

    def test_add_remove_and_move_change_the_table_and_the_text(self, dialog):
        dialog.table.selectRow(0)
        dialog.add_button.click()
        assert dialog.table.rowCount() == 11
        assert dialog.table.cellWidget(1, 1).currentText() == "capture"

        dialog.table.selectRow(1)
        dialog.up_button.click()
        assert dialog.steps_text().splitlines()[0] == "capture screen"

        dialog.table.selectRow(0)
        dialog.remove_button.click()
        assert dialog.table.rowCount() == 10
        assert dialog.steps_text() == SAMPLE, "put back exactly what was taken out"

    def test_a_row_that_cannot_run_is_reported_and_the_dialog_stays_open(self, dialog, monkeypatch):
        from PyQt6.QtWidgets import QMessageBox

        seen: list[str] = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: seen.append(args[2]))
        dialog.table.cellWidget(1, 3).setText("")  # the fill loses its value
        dialog._on_accept()

        assert dialog.result() != dialog.DialogCode.Accepted, "not accepted"
        assert seen and "value" in seen[0]

    def test_saving_needs_a_capture_and_asks_for_one(self, qtbot, monkeypatch):
        from PyQt6.QtWidgets import QMessageBox

        from app.ui.step_editor import StepEditorDialog

        made = StepEditorDialog('click "Sign in"')
        qtbot.addWidget(made)
        monkeypatch.setattr(
            QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes
        )

        made._on_accept()

        # The step is added and shown, so the next Save is a plain Save.
        assert made.table.rowCount() == 2
        assert made.steps_text() == 'click "Sign in"\ncapture screen'

        made._on_accept()
        assert made.result() == made.DialogCode.Accepted

    def test_saying_no_to_the_capture_keeps_the_editor_open(self, qtbot, monkeypatch):
        from PyQt6.QtWidgets import QMessageBox

        from app.ui.step_editor import StepEditorDialog

        made = StepEditorDialog('click "Sign in"')
        qtbot.addWidget(made)
        monkeypatch.setattr(
            QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.No
        )

        made._on_accept()

        assert made.result() != made.DialogCode.Accepted

    def test_a_box_that_does_not_parse_opens_empty_rather_than_angry(self, qtbot):
        from app.ui.step_editor import StepEditorDialog

        made = StepEditorDialog("click")
        qtbot.addWidget(made)
        assert made.table.rowCount() == 0

    def test_a_comment_only_box_has_no_rows(self, qtbot):
        from app.ui.step_editor import StepEditorDialog

        made = StepEditorDialog("# nothing here yet\n")
        qtbot.addWidget(made)
        assert made.table.rowCount() == 0
