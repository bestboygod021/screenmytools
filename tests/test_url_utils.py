"""Unit tests for URL parsing, validation and file naming."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.url_utils import (
    build_filename,
    build_url_label,
    normalize_url,
    sanitize_component,
    split_url_lines,
    unique_path,
    validate_url,
)


class TestSplitUrlLines:
    def test_ignores_blanks_and_comments(self):
        text = "\n".join(
            [
                "https://a.com",
                "",
                "   ",
                "# a comment",
                "// another comment",
                "https://b.com",
            ]
        )
        assert split_url_lines(text) == ["https://a.com", "https://b.com"]

    def test_strips_markdown_wrappers_and_trailing_punctuation(self):
        # One URL per line - commas inside a URL are legal, so lines are
        # never split on them; wrappers are peeled per line instead.
        text = "\n".join(["<https://a.com>", '"https://b.com",', "'https://c.com';"])
        assert split_url_lines(text) == ["https://a.com", "https://b.com", "https://c.com"]

    def test_comma_inside_a_query_string_survives(self):
        assert split_url_lines("https://a.com/s?ids=1,2,3") == ["https://a.com/s?ids=1,2,3"]

    def test_deduplicates_case_insensitively_but_keeps_order(self):
        text = "https://B.com/x\nhttps://a.com\nHTTPS://b.com/X"
        assert split_url_lines(text) == ["https://B.com/x", "https://a.com"]

    def test_windows_line_endings(self):
        assert split_url_lines("https://a.com\r\nhttps://b.com\r\n") == [
            "https://a.com",
            "https://b.com",
        ]


class TestNormalizeUrl:
    def test_adds_https_when_missing(self):
        assert normalize_url("example.com/login") == "https://example.com/login"

    def test_lowercases_scheme_and_host_only(self):
        assert normalize_url("HTTP://Example.COM/Path") == "http://example.com/Path"

    def test_root_path_is_preserved(self):
        assert normalize_url("https://example.com") == "https://example.com"

    def test_protocol_relative(self):
        assert normalize_url("//example.com/a") == "https://example.com/a"

    def test_query_string_is_kept_and_encoded(self):
        result = normalize_url("https://example.com/s?q=hello world&page=2")
        assert result == "https://example.com/s?q=hello%20world&page=2"

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "ftp://example.com/file",
            "https://",
            "https:// /path",
            "https://nope",
            "https://-bad.com",
        ],
    )
    def test_rejects_invalid_input(self, raw):
        with pytest.raises(ValueError):
            normalize_url(raw)

    def test_accepts_localhost_and_ports(self):
        assert normalize_url("http://localhost:8080/admin") == "http://localhost:8080/admin"


class TestValidateUrl:
    def test_valid(self):
        parsed = validate_url("example.com/dashboard")
        assert parsed.is_valid
        assert parsed.normalized == "https://example.com/dashboard"
        assert parsed.host == "example.com"
        assert parsed.label == "example_com_dashboard"

    def test_invalid_captures_reason_without_raising(self):
        parsed = validate_url("ftp://files.example.com/x")
        assert not parsed.is_valid
        assert parsed.error
        assert parsed.normalized == ""
        assert bool(parsed) is False


class TestSanitizeComponent:
    def test_removes_windows_illegal_characters(self):
        assert sanitize_component('a<b>c:d"e|f?g*h') == "a_b_c_d_e_f_g_h"

    def test_collapses_and_trims_underscores(self):
        assert sanitize_component("  Hello   World!! ") == "hello_world"

    def test_reserved_device_names_are_suffixed(self):
        assert sanitize_component("CON") == "con_"
        assert sanitize_component("nul.txt") == "nul_txt"

    def test_length_cap(self):
        assert len(sanitize_component("x" * 500, max_length=40)) <= 40

    def test_empty_falls_back(self):
        assert sanitize_component("///") == "page"


class TestBuildFilename:
    def test_shape(self):
        name = build_filename(3, "https://www.example.com/pricing", "png", "20261007-135012")
        assert name == "003_www_example_com_pricing_20261007-135012.png"

    def test_prefix_is_included_and_sanitised(self):
        name = build_filename(1, "https://a.com", "jpeg", "20261007-135012", prefix="Client A/B")
        assert name.startswith("001_client_a_b_a_com_")
        assert name.endswith(".jpg") or name.endswith(".jpeg")

    def test_extension_dot_is_tolerated(self):
        assert build_filename(1, "https://a.com", ".PNG", "").endswith(".png")


class TestBuildUrlLabel:
    def test_uses_first_path_segments(self):
        assert build_url_label("https://shop.example.com/a/b/c/d") == "shop_example_com_a_b_c"

    def test_no_path(self):
        assert build_url_label("https://example.com") == "example_com"


class TestUniquePath:
    def test_returns_input_when_free(self, tmp_path: Path):
        assert unique_path(tmp_path, "a.png") == tmp_path / "a.png"

    def test_increments_on_collision(self, tmp_path: Path):
        (tmp_path / "a.png").write_bytes(b"x")
        (tmp_path / "a_2.png").write_bytes(b"x")
        assert unique_path(tmp_path, "a.png") == tmp_path / "a_3.png"
