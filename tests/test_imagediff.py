"""Unit tests for the dHash-based visual change detector."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from app.core.imagediff import diff_ratio, hamming


def _png(draw_left: bool) -> bytes:
    """A 64x64 greyscale image with a dark block on one side."""
    image = Image.new("L", (64, 64), 200)
    painter = ImageDraw.Draw(image)
    if draw_left:
        painter.rectangle([0, 0, 31, 63], fill=20)
    else:
        painter.rectangle([32, 0, 63, 63], fill=20)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class TestDiffRatio:
    def test_identical_images_have_zero_diff(self):
        assert diff_ratio(_png(True), _png(True)) == 0.0

    def test_mirrored_layout_is_a_big_change(self):
        assert diff_ratio(_png(True), _png(False)) > 0.2

    def test_ratio_is_normalised(self):
        ratio = diff_ratio(_png(True), _png(False))
        assert 0.0 <= ratio <= 1.0

    def test_small_brightness_jitter_is_ignored(self):
        a = _png(True)
        # Re-encode the same picture slightly brighter: dHash should not move.
        with Image.open(io.BytesIO(a)) as image:
            bright = image.point(lambda v: min(255, v + 3))
            buffer = io.BytesIO()
            bright.save(buffer, format="PNG")
        assert diff_ratio(a, buffer.getvalue()) < 0.05


class TestHamming:
    def test_counts_differences(self):
        assert hamming([1, 0, 1, 0], [1, 1, 1, 0]) == 1
        assert hamming([0] * 64, [1] * 64) == 64
        assert hamming([], []) == 0
