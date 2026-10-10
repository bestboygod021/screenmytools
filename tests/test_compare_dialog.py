"""Tests for the before/after comparison dialog and history compare action."""

from __future__ import annotations

import json

from PIL import Image


def _png(path, color, size=(30, 30)):
    Image.new("RGB", size, color).save(path)
    return path


class TestCompareDialog:
    def test_shows_pixmaps_and_ratio(self, tmp_path, qapp):
        from app.ui.compare_dialog import CompareDialog

        a = _png(tmp_path / "a.png", (255, 0, 0))
        b = _png(tmp_path / "b.png", (0, 0, 255))
        dialog = CompareDialog(str(a), str(b))
        assert not dialog.before_label.pixmap().isNull()
        assert not dialog.after_label.pixmap().isNull()
        assert "%" in dialog.info_label.text()

    def test_missing_file_message(self, tmp_path, qapp):
        from app.ui.compare_dialog import CompareDialog

        a = _png(tmp_path / "a.png", (0, 0, 0), size=(10, 10))
        dialog = CompareDialog(str(a), str(tmp_path / "ghost.png"))
        assert "Could not compare" in dialog.info_label.text()

    def test_make_diff_image_size(self, tmp_path):
        from app.ui.compare_dialog import make_diff_image

        a = _png(tmp_path / "a.png", (0, 0, 0), size=(12, 8))
        b = _png(tmp_path / "b.png", (255, 255, 255), size=(12, 8))
        diff = make_diff_image(str(a), str(b))
        assert diff is not None
        assert diff.size == (12, 8)


class TestHistoryCompare:
    def test_compare_opens_with_before_and_after(self, tmp_path, qapp, monkeypatch):
        import app.ui.compare_dialog as cd
        from app.ui.history_dialog import HistoryDialog

        a = _png(tmp_path / "a.png", (255, 0, 0), size=(20, 20))
        b = _png(tmp_path / "b.png", (0, 255, 0), size=(20, 20))
        (tmp_path / "capture-report-20260101-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "results": [
                        {
                            "url": "https://x.example.com",
                            "status": "success",
                            "diff": 0.0,
                            "file_path": str(a),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "capture-report-20260102-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-02T00:00:00",
                    "results": [
                        {
                            "url": "https://x.example.com",
                            "status": "success",
                            "diff": 0.5,
                            "file_path": str(b),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        captured = {}

        class FakeDialog:
            def __init__(self, before, after, parent=None):
                captured["before"] = before
                captured["after"] = after

            def exec(self):
                return 0

        monkeypatch.setattr(cd, "CompareDialog", FakeDialog)

        dialog = HistoryDialog(str(tmp_path))
        dialog.site_combo.setCurrentIndex(1)  # select the single site
        dialog.compare_button.click()

        assert captured["before"] == str(a)  # older
        assert captured["after"] == str(b)  # newer

    def test_compare_needs_two_captures(self, tmp_path, qapp, monkeypatch):
        from PyQt6.QtWidgets import QMessageBox

        from app.ui.history_dialog import HistoryDialog

        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        dialog = HistoryDialog(str(tmp_path / "empty"))
        dialog.compare_button.click()  # should not raise


class TestCompareDialogTitles:
    def test_custom_titles_and_info(self, tmp_path, qapp):
        from app.ui.compare_dialog import CompareDialog

        a = _png(tmp_path / "a.png", (255, 0, 0))
        b = _png(tmp_path / "b.png", (0, 0, 255))
        dialog = CompareDialog(str(a), str(b), None, before_title="Baseline", after_title="Latest")
        assert dialog.before_heading.text() == "Baseline"
        assert dialog.after_heading.text() == "Latest"
        assert "vs baseline" in dialog.info_label.text()

    def test_default_titles_are_unchanged(self, tmp_path, qapp):
        from app.ui.compare_dialog import CompareDialog

        a = _png(tmp_path / "a.png", (10, 10, 10))
        dialog = CompareDialog(str(a), str(a))
        assert dialog.before_heading.text() == "Before"
        assert "Visual difference:" in dialog.info_label.text()


class TestHistoryCompareBaseline:
    def _seed(self, tmp_path, with_baseline=True):
        a = _png(tmp_path / "a.png", (255, 0, 0), size=(20, 20))
        b = _png(tmp_path / "b.png", (0, 255, 0), size=(20, 20))
        url = "https://x.example.com"
        (tmp_path / "capture-report-20260101-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "results": [
                        {"url": url, "status": "success", "diff": 0.0, "file_path": str(a)}
                    ],
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "capture-report-20260102-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-02T00:00:00",
                    "results": [
                        {"url": url, "status": "success", "diff": 0.5, "file_path": str(b)}
                    ],
                }
            ),
            encoding="utf-8",
        )
        if with_baseline:
            from app.core import baseline

            baseline.latest_path(tmp_path, url).write_bytes(a.read_bytes())
            baseline.pin_baseline(tmp_path, url)
        return a, b

    def test_compares_the_baseline_to_the_newest_capture(self, tmp_path, qapp, monkeypatch):
        import app.ui.compare_dialog as cd
        from app.core import baseline
        from app.ui.history_dialog import HistoryDialog

        _a, b = self._seed(tmp_path)
        captured = {}

        class FakeDialog:
            def __init__(
                self, before, after, parent=None, before_title="Before", after_title="After"
            ):
                captured.update(before=before, after=after, bt=before_title, at=after_title)

            def exec(self):
                return 0

        monkeypatch.setattr(cd, "CompareDialog", FakeDialog)
        dialog = HistoryDialog(str(tmp_path))
        dialog.site_combo.setCurrentIndex(1)  # the only site
        dialog.compare_baseline_button.click()

        pinned = baseline.find_baseline(tmp_path, "https://x.example.com")
        assert captured["before"] == str(pinned)
        assert captured["after"] == str(b)  # newest capture is the 'after'
        assert captured["bt"] == "Baseline" and captured["at"] == "Latest"

    def test_warns_without_a_pinned_baseline(self, tmp_path, qapp, monkeypatch):
        from PyQt6.QtWidgets import QMessageBox

        from app.ui.history_dialog import HistoryDialog

        self._seed(tmp_path, with_baseline=False)
        shown = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
        dialog = HistoryDialog(str(tmp_path))
        dialog.site_combo.setCurrentIndex(1)
        dialog.compare_baseline_button.click()
        assert shown and "no pinned baseline" in shown[0]

    def test_warns_when_all_sites_is_selected(self, tmp_path, qapp, monkeypatch):
        from PyQt6.QtWidgets import QMessageBox

        from app.ui.history_dialog import HistoryDialog

        self._seed(tmp_path)
        shown = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
        dialog = HistoryDialog(str(tmp_path))
        dialog.compare_baseline_button.click()  # "All sites" is selected
        assert shown


class TestHistoryExportPreview:
    """The history dialog can preview and save the very bytes /api/export returns."""

    def _seed(self, tmp_path):
        url = "https://x.example.com"
        for stamp in ("20260101-000000", "20260102-000000"):
            (tmp_path / f"capture-report-{stamp}.json").write_text(
                json.dumps(
                    {
                        "generated_at": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T00:00:00",
                        "results": [{"url": url, "status": "success", "diff": 0.4}],
                    }
                ),
                encoding="utf-8",
            )
        return url

    def test_the_preview_shows_the_csv(self, tmp_path, qapp):
        from app.ui.history_dialog import HistoryDialog

        self._seed(tmp_path)
        dialog = HistoryDialog(str(tmp_path))
        dialog.preview_button.click()
        text = dialog.export_preview.toPlainText()
        assert not dialog.export_preview.isHidden()  # opened, whatever the window state
        assert text.startswith("timestamp,url,label,status,diff,drift,file")
        assert "x.example.com" in text
        assert "2 row(s)" in dialog.export_status.text()

    def test_the_preview_follows_the_chosen_format(self, tmp_path, qapp):
        from app.ui.history_dialog import HistoryDialog

        self._seed(tmp_path)
        dialog = HistoryDialog(str(tmp_path))
        dialog.export_format.setCurrentIndex(1)  # JSON
        dialog.preview_button.click()
        text = dialog.export_preview.toPlainText()
        assert text.lstrip().startswith("{")
        assert '"total": 2' in text
        assert "history.json" in dialog.export_status.text()

    def test_changing_the_format_refreshes_an_open_preview(self, tmp_path, qapp):
        from app.ui.history_dialog import HistoryDialog

        self._seed(tmp_path)
        dialog = HistoryDialog(str(tmp_path))
        dialog.preview_button.click()
        dialog.export_format.setCurrentIndex(1)
        assert dialog.export_preview.toPlainText().lstrip().startswith("{")

    def test_the_download_matches_history_export(self, tmp_path, qapp):
        from app.core import api
        from app.ui.history_dialog import HistoryDialog

        self._seed(tmp_path)
        dialog = HistoryDialog(str(tmp_path))
        body, filename, rows = dialog.export_download()
        page = api.history_page(str(tmp_path))
        expected, _type, _name = api.format_export(page, "csv")
        assert body == expected and filename == "history.csv" and rows == 2

    def test_the_site_filter_narrows_the_download(self, tmp_path, qapp):
        from app.ui.history_dialog import HistoryDialog

        self._seed(tmp_path)
        other = tmp_path / "capture-report-20260103-000000.json"
        other.write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-03T00:00:00",
                    "results": [{"url": "https://y.example.com", "status": "success", "diff": 0.1}],
                }
            ),
            encoding="utf-8",
        )
        dialog = HistoryDialog(str(tmp_path))
        dialog.site_combo.setCurrentIndex(dialog.site_combo.findData("https://y.example.com"))
        body, _filename, rows = dialog.export_download()
        assert rows == 1 and b"y.example.com" in body and b"x.example.com" not in body

    def test_copy_puts_the_download_on_the_clipboard(self, tmp_path, qapp):
        from PyQt6.QtWidgets import QApplication

        from app.ui.history_dialog import HistoryDialog

        self._seed(tmp_path)
        dialog = HistoryDialog(str(tmp_path))
        dialog.copy_export_button.click()
        assert "x.example.com" in QApplication.clipboard().text()
        assert "history.csv copied" in dialog.export_status.text()

    def test_save_as_writes_exactly_the_previewed_bytes(self, tmp_path, qapp, monkeypatch):
        from PyQt6.QtWidgets import QFileDialog

        from app.ui.history_dialog import HistoryDialog

        self._seed(tmp_path)
        target = tmp_path / "saved.csv"
        monkeypatch.setattr(
            QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), ""))
        )
        dialog = HistoryDialog(str(tmp_path))
        dialog.save_export_button.click()
        body, _filename, _rows = dialog.export_download()
        assert target.read_bytes() == body
        assert "Saved saved.csv" in dialog.export_status.text()

    def test_save_as_does_nothing_when_cancelled(self, tmp_path, qapp, monkeypatch):
        from PyQt6.QtWidgets import QFileDialog

        from app.ui.history_dialog import HistoryDialog

        self._seed(tmp_path)
        monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("", "")))
        dialog = HistoryDialog(str(tmp_path))
        dialog.save_export_button.click()
        assert dialog.export_status.text() == ""

    def test_a_huge_history_is_shown_shortened(self, tmp_path, qapp):
        from app.ui.history_dialog import PREVIEW_LIMIT, HistoryDialog

        url = "https://x.example.com/" + "a" * 4000
        (tmp_path / "capture-report-20260101-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "results": [
                        {"url": f"{url}{index}", "status": "success", "diff": 0.1}
                        for index in range(80)
                    ],
                }
            ),
            encoding="utf-8",
        )
        dialog = HistoryDialog(str(tmp_path))
        dialog.preview_button.click()
        text = dialog.export_preview.toPlainText()
        assert len(text) < PREVIEW_LIMIT + 500
        assert "preview truncated" in text
        assert len(dialog.export_download()[0]) > PREVIEW_LIMIT  # the file itself is whole
