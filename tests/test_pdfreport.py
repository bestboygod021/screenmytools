"""Unit tests for the PDF report builder."""

from __future__ import annotations

from PIL import Image

from app.core.engine import CaptureResult, CaptureStatus, CaptureSummary
from app.core.pdfreport import build_pdf_report


def _make_png(path, color):
    Image.new("RGB", (60, 80), color).save(path)


class TestPdfReport:
    def test_writes_a_real_pdf_with_thumbnails(self, tmp_path):
        img1 = tmp_path / "a.png"
        img2 = tmp_path / "b.png"
        _make_png(img1, (200, 10, 10))
        _make_png(img2, (10, 200, 10))

        results = [
            CaptureResult(
                index=0,
                url="https://a.example.com",
                status=CaptureStatus.SUCCESS,
                file_path=str(img1),
                diff=0.12,
            ),
            CaptureResult(
                index=1,
                url="https://b.example.com",
                status=CaptureStatus.FAILED,
                file_path=str(img2),
            ),
        ]
        summary = CaptureSummary(results=results, output_dir=str(tmp_path))

        out = build_pdf_report(summary, tmp_path / "report.pdf")
        assert out.exists()
        assert out.read_bytes()[:4] == b"%PDF"
        assert out.stat().st_size > 1000  # cover + thumbnails

    def test_writes_pdf_even_without_images(self, tmp_path):
        summary = CaptureSummary(
            results=[
                CaptureResult(index=0, url="https://x.example.com", status=CaptureStatus.FAILED)
            ],
            output_dir=str(tmp_path),
        )
        out = build_pdf_report(summary, tmp_path / "r.pdf")
        assert out.read_bytes()[:4] == b"%PDF"

    def test_missing_image_files_are_skipped(self, tmp_path):
        summary = CaptureSummary(
            results=[
                CaptureResult(
                    index=0,
                    url="https://x.example.com",
                    status=CaptureStatus.SUCCESS,
                    file_path=str(tmp_path / "ghost.png"),
                )
            ],
            output_dir=str(tmp_path),
        )
        out = build_pdf_report(summary, tmp_path / "r.pdf")
        assert out.read_bytes()[:4] == b"%PDF"


class TestOnlyChanged:
    def _summary(self, tmp_path):
        from app.core.engine import CaptureResult, CaptureStatus, CaptureSummary

        img = tmp_path / "a.png"
        _make_png(img, (1, 2, 3))
        results = [
            CaptureResult(
                index=0,
                url="https://a.example.com",
                status=CaptureStatus.SUCCESS,
                file_path=str(img),
                diff=0.5,
            ),
            CaptureResult(
                index=1,
                url="https://b.example.com",
                status=CaptureStatus.SUCCESS,
                file_path=str(img),
                diff=0.0,
                unchanged=True,
            ),
            CaptureResult(
                index=2,
                url="https://c.example.com",
                status=CaptureStatus.SUCCESS,
                file_path=str(img),
                diff=None,
            ),
        ]
        return CaptureSummary(results=results, output_dir=str(tmp_path))

    def test_changed_results_filter(self, tmp_path):
        from app.core.pdfreport import _changed_results

        changed = _changed_results(self._summary(tmp_path))
        assert [r.url for r in changed] == ["https://a.example.com"]

    def test_only_changed_pdf_builds(self, tmp_path):
        out = build_pdf_report(self._summary(tmp_path), tmp_path / "changes.pdf", only_changed=True)
        assert out.read_bytes()[:4] == b"%PDF"


class TestTrendPage:
    def _seed(self, directory):
        import json

        (directory / "capture-report-20260101-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "results": [
                        {
                            "url": "https://a.example.com",
                            "status": "success",
                            "diff": 0.0,
                            "file_path": "",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (directory / "capture-report-20260102-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-02T00:00:00",
                    "results": [
                        {
                            "url": "https://a.example.com",
                            "status": "success",
                            "diff": 0.5,
                            "file_path": "",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_trend_page_none_when_empty(self, tmp_path):
        from app.core.pdfreport import _trend_page

        assert _trend_page(str(tmp_path / "none")) is None

    def test_trend_page_rendered_when_history_exists(self, tmp_path):
        from app.core.pdfreport import _trend_page

        self._seed(tmp_path)
        page = _trend_page(str(tmp_path))
        assert isinstance(page, Image.Image)

    def test_build_with_include_trend_writes_pdf(self, tmp_path):
        self._seed(tmp_path)
        summary = CaptureSummary(
            results=[
                CaptureResult(
                    index=0, url="https://a.example.com", status=CaptureStatus.SUCCESS, diff=0.5
                )
            ],
            output_dir=str(tmp_path),
        )
        out = build_pdf_report(summary, tmp_path / "trend.pdf", include_trend=True)
        assert out.exists() and out.stat().st_size > 0


class TestDriftChart:
    def _seed(self, directory, drift_points):
        import json

        for index, drift in enumerate(drift_points):
            name = f"2026010{index + 1}-000000"
            (directory / f"capture-report-{name}.json").write_text(
                json.dumps(
                    {
                        "generated_at": f"2026-01-0{index + 1}T00:00:00",
                        "results": [
                            {
                                "url": "https://a.example.com",
                                "status": "success",
                                "diff": 0.2,
                                "drift": drift,
                                "file_path": "",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

    def test_trend_page_renders_with_drift_history(self, tmp_path):
        from app.core.pdfreport import _trend_page

        self._seed(tmp_path, [0.1, 0.3, 0.5])
        page = _trend_page(str(tmp_path))
        assert isinstance(page, Image.Image)

    def test_single_drift_point_still_renders(self, tmp_path):
        from app.core.pdfreport import _trend_page

        self._seed(tmp_path, [0.4])
        assert isinstance(_trend_page(str(tmp_path)), Image.Image)

    def test_drift_value_appears_in_the_summary_pdf(self, tmp_path):
        from app.core.engine import CaptureResult, CaptureSummary
        from app.core.pdfreport import build_pdf_report

        self._seed(tmp_path, [0.1, 0.6])
        summary = CaptureSummary(
            results=[
                CaptureResult(index=0, url="https://a.example.com", status=CaptureStatus.SUCCESS)
            ],
            output_dir=str(tmp_path),
        )
        out = build_pdf_report(summary, tmp_path / "drift.pdf", include_trend=True)
        assert out.exists() and out.stat().st_size > 0

    def test_sites_without_drift_are_unchanged(self, tmp_path):
        import json

        from app.core.pdfreport import _trend_page

        (tmp_path / "capture-report-20260101-000000.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "results": [
                        {
                            "url": "https://b.example.com",
                            "status": "success",
                            "diff": 0.1,
                            "drift": None,
                            "file_path": "",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        assert isinstance(_trend_page(str(tmp_path)), Image.Image)

    def test_row_heights_fit_the_page_for_many_sites(self, tmp_path):
        import json

        from app.core.pdfreport import PAGE_H, _trend_page

        for index in range(60):
            (tmp_path / f"capture-report-2026010{index % 9 + 1}-000{index:03d}.json").write_text(
                json.dumps(
                    {
                        "generated_at": f"2026-01-01T00:00:{index:02d}",
                        "results": [
                            {
                                "url": f"https://site{index}.example.com",
                                "status": "success",
                                "diff": 0.1,
                                "drift": 0.2 + index / 100,
                                "file_path": "",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
        page = _trend_page(str(tmp_path))
        assert page is not None and page.size[1] == PAGE_H  # never overflows the page


class TestDriftChartPixels:
    def _seed(self, directory, drift_points):
        import json

        for index, drift in enumerate(drift_points):
            (directory / f"capture-report-2026010{index + 1}-000000.json").write_text(
                json.dumps(
                    {
                        "generated_at": f"2026-01-0{index + 1}T00:00:00",
                        "results": [
                            {
                                "url": "https://a.example.com",
                                "status": "success",
                                "diff": 0.2,
                                "drift": drift,
                                "file_path": "",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

    def _colours(self, page):
        return {rgb for _count, rgb in page.getcolors(maxcolors=1_000_000)}

    def test_amber_line_appears_when_drift_is_recorded(self, tmp_path):
        from app.core.pdfreport import _trend_page

        self._seed(tmp_path, [0.1, 0.6])
        page = _trend_page(str(tmp_path))
        assert (224, 158, 60) in self._colours(page)  # the drift sparkline
        assert (74, 158, 255) in self._colours(page)  # the diff sparkline

    def test_no_amber_line_without_drift_history(self, tmp_path):
        from app.core.pdfreport import _trend_page

        self._seed(tmp_path, [0.5])  # a single point is not charted
        page = _trend_page(str(tmp_path))
        assert (224, 158, 60) not in self._colours(page)
