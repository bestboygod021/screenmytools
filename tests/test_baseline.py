"""Tests for pinned baselines (app.core.baseline)."""

from __future__ import annotations

from app.core import baseline


class TestBaselinePaths:
    def test_latest_and_baseline_names(self, tmp_path):
        latest = baseline.latest_path(tmp_path, "https://a.example.com")
        pinned = baseline.baseline_path(tmp_path, "https://a.example.com")
        assert latest.name.startswith("latest_") and latest.suffix == ".png"
        assert pinned.name.startswith("baseline_") and pinned.suffix == ".png"

    def test_find_helpers_return_none_when_absent(self, tmp_path):
        assert baseline.find_latest(tmp_path, "https://a.example.com") is None
        assert baseline.find_baseline(tmp_path, "https://a.example.com") is None
        assert baseline.has_baseline(tmp_path, "https://a.example.com") is False


class TestPin:
    def test_pin_copies_the_latest_capture(self, tmp_path):
        baseline.latest_path(tmp_path, "https://a.example.com").write_bytes(b"png-bytes")
        pinned = baseline.pin_baseline(tmp_path, "https://a.example.com")
        assert pinned is not None and pinned.exists()
        assert pinned.read_bytes() == b"png-bytes"
        assert baseline.has_baseline(tmp_path, "https://a.example.com") is True

    def test_pin_without_latest_returns_none(self, tmp_path):
        assert baseline.pin_baseline(tmp_path, "https://a.example.com") is None

    def test_pin_preserves_the_webp_format(self, tmp_path):
        baseline.latest_path(tmp_path, "https://a.example.com", "webp").write_bytes(b"webp")
        pinned = baseline.pin_baseline(tmp_path, "https://a.example.com")
        assert pinned is not None and pinned.suffix == ".webp"
        assert baseline.find_baseline(tmp_path, "https://a.example.com") == pinned


class TestClear:
    def test_clear_removes_the_pin(self, tmp_path):
        baseline.latest_path(tmp_path, "https://a.example.com").write_bytes(b"x")
        baseline.pin_baseline(tmp_path, "https://a.example.com")
        assert baseline.clear_baseline(tmp_path, "https://a.example.com") is True
        assert baseline.has_baseline(tmp_path, "https://a.example.com") is False

    def test_clear_without_a_pin_is_false(self, tmp_path):
        assert baseline.clear_baseline(tmp_path, "https://a.example.com") is False


class TestStaleness:
    def _pin(self, tmp_path, url="https://a.example.com", age_days=10.0):
        import os
        import time

        baseline.latest_path(tmp_path, url).write_bytes(b"png")
        pinned = baseline.pin_baseline(tmp_path, url)
        assert pinned is not None
        when = time.time() - age_days * 86400
        os.utime(pinned, (when, when))
        return pinned

    def test_age_is_none_without_a_baseline(self, tmp_path):
        assert baseline.baseline_age_days(tmp_path, "https://a.example.com") is None

    def test_age_reflects_the_pin_time(self, tmp_path):
        self._pin(tmp_path, age_days=10.0)
        age = baseline.baseline_age_days(tmp_path, "https://a.example.com")
        assert age is not None and 9.5 < age < 10.5

    def test_fresh_baseline_is_not_stale(self, tmp_path):
        self._pin(tmp_path, age_days=1.0)
        assert baseline.is_baseline_stale(tmp_path, "https://a.example.com", 7) is False

    def test_old_baseline_is_stale(self, tmp_path):
        self._pin(tmp_path, age_days=30.0)
        assert baseline.is_baseline_stale(tmp_path, "https://a.example.com", 7) is True

    def test_check_disabled_when_max_age_is_zero(self, tmp_path):
        self._pin(tmp_path, age_days=999.0)
        assert baseline.is_baseline_stale(tmp_path, "https://a.example.com", 0) is False

    def test_missing_baseline_is_never_stale(self, tmp_path):
        assert baseline.is_baseline_stale(tmp_path, "https://a.example.com", 7) is False

    def test_stale_baselines_filters_the_list(self, tmp_path):
        self._pin(tmp_path, "https://old.example.com", age_days=40.0)
        self._pin(tmp_path, "https://new.example.com", age_days=1.0)
        stale = baseline.stale_baselines(
            tmp_path,
            ["https://old.example.com", "https://new.example.com", "https://none.example.com"],
            7,
        )
        assert stale == ["https://old.example.com"]


def _gradient(path, reverse=False):
    """A left-to-right gradient; ``reverse`` flips it so every dHash bit differs."""
    from PIL import Image

    image = Image.new("L", (40, 40))
    for x in range(40):
        value = int(255 * x / 39)
        if reverse:
            value = 255 - value
        for y in range(40):
            image.putpixel((x, y), value)
    image.convert("RGB").save(path)
    return path


class TestBaselineDrift:
    def _images(self, tmp_path, reverse=False):
        url = "https://a.example.com"
        _gradient(baseline.latest_path(tmp_path, url))
        pinned = baseline.pin_baseline(tmp_path, url)
        _gradient(baseline.latest_path(tmp_path, url), reverse=reverse)
        return url, pinned

    def test_none_without_a_baseline(self, tmp_path):
        assert baseline.baseline_drift(tmp_path, "https://a.example.com") is None

    def test_identical_images_have_no_drift(self, tmp_path):
        url, _ = self._images(tmp_path)
        assert baseline.baseline_drift(tmp_path, url) == 0.0

    def test_a_different_image_drifts(self, tmp_path):
        url, _ = self._images(tmp_path, reverse=True)
        drift = baseline.baseline_drift(tmp_path, url)
        assert drift is not None and drift > 0.5

    def test_undecodable_pair_returns_none(self, tmp_path):
        url = "https://a.example.com"
        baseline.latest_path(tmp_path, url).write_bytes(b"not-an-image")
        baseline.pin_baseline(tmp_path, url)
        assert baseline.baseline_drift(tmp_path, url) is None
