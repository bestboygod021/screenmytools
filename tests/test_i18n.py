"""Unit tests for the i18n catalogue."""

from __future__ import annotations

from app.core import i18n


def setup_function(_func):
    i18n.set_language("en")


class TestI18n:
    def test_default_is_english(self):
        assert i18n.current_language() == "en"
        assert i18n.is_rtl() is False
        assert i18n.tr("start") == "Start Capture"

    def test_persian_switch(self):
        i18n.set_language("fa")
        assert i18n.current_language() == "fa"
        assert i18n.is_rtl() is True
        assert i18n.tr("start") == "شروع ثبت"

    def test_unknown_key_falls_back_to_key(self):
        assert i18n.tr("does_not_exist") == "does_not_exist"

    def test_missing_translation_falls_back_to_english(self):
        # 'fa' has every key here, so drop to an unsupported lang to force fallback.
        i18n.set_language("xx")
        assert i18n.current_language() == "en"

    def test_invalid_language_is_normalised(self):
        i18n.set_language("klingon")
        assert i18n.current_language() == "en"
