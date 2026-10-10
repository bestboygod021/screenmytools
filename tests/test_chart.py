"""Tests for the PNG drift chart (app.core.chart)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from PIL import Image

from app.core import chart

_seq = {"n": 0}


def _seed(directory, url, drift, days_ago=1):
    """Write one report with a recorded drift value (unique filename each call)."""
    when = (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds")
    _seq["n"] += 1
    name = when.replace(":", "").replace("-", "").replace("T", "-") + f"-{_seq['n']:03d}"
    (directory / f"capture-report-{name}.json").write_text(
        json.dumps(
            {
                "generated_at": when,
                "results": [
                    {"url": url, "status": "success", "diff": 0.2, "drift": drift, "file_path": ""}
                ],
            }
        ),
        encoding="utf-8",
    )


class TestDriftSeries:
    def test_an_empty_folder_has_no_series(self, tmp_path):
        assert chart.drift_series(tmp_path) == {}

    def test_values_are_oldest_first(self, tmp_path):
        _seed(tmp_path, "https://a.com", 0.1, days_ago=3)
        _seed(tmp_path, "https://a.com", 0.4, days_ago=1)
        assert chart.drift_series(tmp_path)["https://a.com"] == [0.1, 0.4]

    def test_sites_without_drift_are_skipped(self, tmp_path):
        _seed(tmp_path, "https://a.com", None)
        assert chart.drift_series(tmp_path) == {}

    def test_the_window_filters_old_points(self, tmp_path):
        _seed(tmp_path, "https://a.com", 0.1, days_ago=30)
        _seed(tmp_path, "https://a.com", 0.4, days_ago=1)
        assert chart.drift_series(tmp_path, days=7)["https://a.com"] == [0.4]


class TestRenderDriftChart:
    def test_no_data_means_no_chart(self, tmp_path):
        assert chart.render_drift_chart(tmp_path) is None

    def test_the_png_is_loadable_and_has_the_requested_size(self, tmp_path):
        _seed(tmp_path, "https://a.com", 0.2)
        _seed(tmp_path, "https://a.com", 0.3)
        png = chart.render_drift_chart(tmp_path, width=400, height=200)
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        import io

        with Image.open(io.BytesIO(png)) as image:
            assert image.size == (400, 200)
            assert image.convert("RGB").getcolors(maxcolors=100000)  # something was drawn

    def test_a_single_point_still_draws(self, tmp_path):
        _seed(tmp_path, "https://a.com", 0.5)
        assert chart.render_drift_chart(tmp_path) is not None

    def test_a_flat_zero_series_does_not_crash(self, tmp_path):
        _seed(tmp_path, "https://a.com", 0.0)
        _seed(tmp_path, "https://a.com", 0.0)
        assert chart.render_drift_chart(tmp_path) is not None

    def test_more_sites_than_lines_are_capped(self, tmp_path):
        for index in range(9):
            _seed(tmp_path, f"https://site{index}.com", 0.1 * (index + 1))
        assert chart.render_drift_chart(tmp_path, max_sites=3) is not None


class TestHelpers:
    def test_the_axis_is_friendly_but_never_zero(self):
        assert chart._scale([0.0]) == 0.1
        assert chart._scale([0.03, 0.4]) == 0.5
        assert chart._scale([0.9]) == 1.0

    def test_long_labels_are_trimmed_from_the_left(self):
        trimmed = chart._short("https://a-very-long-hostname.example.com/page")
        assert trimmed == "...hostname.example.com/page" and len(trimmed) == 28
        assert chart._short("https://a.com") == "a.com"
