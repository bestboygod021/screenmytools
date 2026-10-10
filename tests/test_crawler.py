"""The crawler: find the buttons, click them, photograph every screen it reaches."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core import crawler
from app.core.engine import CaptureEngine
from app.core.settings import CaptureSettings
from tests.fakes import (
    CallLog,
    FakeModal,
    FakeSite,
    FakeWidget,
    PageScript,
    SiteScript,
    make_factory,
)


def demo_site() -> FakeSite:
    """A site with everything the crawler has to tell apart."""
    return FakeSite(
        pages={
            "/": (
                "Home",
                [
                    FakeWidget("Products", kind="link", link="https://example.com/products"),
                    FakeWidget("Pricing", kind="link", link="https://example.com/pricing"),
                    FakeWidget("Sign in", kind="link", link="https://example.com/login"),
                    FakeWidget("Blog", kind="link", link="https://blog.example.org/"),
                    FakeWidget(
                        "Delete account",
                        kind="button",
                        opens=FakeModal("Confirm delete", selector=".confirm"),
                    ),
                    FakeWidget("Our brochure", kind="link", href="/brochure.pdf"),
                    FakeWidget("Newsletter", kind="link", href="https://example.com/newsletter"),
                ],
            ),
            "/products": (
                "Products",
                [
                    FakeWidget("Filters", opens=FakeModal("Filters", selector=".filters")),
                    FakeWidget("Sort by price"),
                ],
            ),
            "/pricing": ("Pricing", [FakeWidget("Buy now")]),
            "/login": (
                "Sign in",
                [FakeWidget("Forgot password", kind="link", link="https://example.com/reset")],
            ),
            "/reset": ("Reset password", []),
            "/newsletter": ("Newsletter", []),
        }
    )


def make_crawler(tmp_path, options: crawler.CrawlOptions | None = None):
    """``(crawler, output folder, call log)`` wired to the demo site."""
    calls = CallLog()
    script = PageScript(page_width=1200, page_height=900, site=SiteScript.from_site(demo_site()))
    settings = CaptureSettings(
        output_dir=str(tmp_path / "shots"), settle_delay_ms=0, network_idle_timeout_ms=0
    )
    settings.validate()
    engine = CaptureEngine(
        settings, playwright_factory=make_factory(script, calls), log=lambda level, message: None
    )
    options = options or crawler.CrawlOptions(max_depth=2, max_states=10, max_clicks=20, delay_ms=0)
    return crawler.Crawler(engine, options), settings, script


def make_report(entries, url="https://example.com/", folder="/tmp/old"):
    """A crawl report with the pages described as ``(name, url)`` pairs."""
    report = crawler.CrawlReport(url=url, output_dir=folder)
    for index, (name, page_url) in enumerate(entries, start=1):
        report.pages.append(crawler.CrawledPage(index=index, name=name, url=page_url))
    return report


class TestDiff:
    """Two walks, compared: what is new, what is gone, what changed name."""

    def test_a_new_screen_is_reported_as_added(self):
        previous = make_report([("home", "https://example.com/")])
        current = make_report(
            [("home", "https://example.com/"), ("pricing", "https://example.com/pricing")]
        )
        diff = crawler.compare_reports(previous, current, changed_threshold=None)
        assert [page.name for page in diff.added] == ["pricing"]
        assert diff.removed == []
        assert diff.unchanged == 1
        assert "+ pricing" in diff.summary()

    def test_a_screen_that_vanished_is_reported_as_gone(self):
        previous = make_report(
            [("home", "https://example.com/"), ("careers", "https://example.com/careers")]
        )
        current = make_report([("home", "https://example.com/")])
        diff = crawler.compare_reports(previous, current, changed_threshold=None)
        assert diff.added == []
        assert [item["name"] for item in diff.removed] == ["careers"]
        assert "- careers" in diff.summary()

    def test_a_renamed_button_is_a_rename_not_two_changes(self):
        previous = make_report([("buy-now", "https://example.com/checkout")])
        current = make_report([("complete-order", "https://example.com/checkout")])
        diff = crawler.compare_reports(previous, current, changed_threshold=None)
        assert diff.added == [] and diff.removed == []
        assert diff.renamed == [
            {"url": "https://example.com/checkout", "was": "buy-now", "now": "complete-order"}
        ]
        assert "is now 'complete-order'" in diff.summary()

    def test_two_identical_walks_compare_clean(self):
        entries = [("home", "https://example.com/"), ("login", "https://example.com/login")]
        diff = crawler.compare_reports(
            make_report(entries), make_report(entries), changed_threshold=None
        )
        assert diff.to_dict()["added"] == []
        assert "No changes" in diff.summary()

    def test_a_report_round_trips_through_json(self, tmp_path):
        report = make_report([("home", "https://example.com/")], folder=str(tmp_path))
        report.pages[0].path = ["Sign in"]
        report.pages[0].depth = 1
        folder = tmp_path / "crawl-example_com-20260101-000000"
        folder.mkdir()
        crawler.write_crawl_report(report, folder)

        again = crawler.load_report(folder / "report.json")

        assert again.url == report.url
        assert [page.name for page in again.pages] == ["home"]
        assert again.pages[0].path == ["Sign in"]
        assert again.pages[0].depth == 1

    def test_the_newest_earlier_folder_wins(self, tmp_path):
        older = tmp_path / "crawl-example_com-20260101-000000"
        newer = tmp_path / "crawl-example_com-20260201-000000"
        for folder in (older, newer):
            folder.mkdir()
            crawler.write_crawl_report(make_report([("home", "https://a/")]), folder)
        current = tmp_path / "crawl-example_com-20260301-000000"
        current.mkdir()
        crawler.write_crawl_report(make_report([("home", "https://a/")]), current)

        found = crawler.find_previous_report(tmp_path, exclude=current)

        assert found == newer / "report.json"

    def test_without_an_earlier_walk_there_is_nothing_to_compare(self, tmp_path):
        current = tmp_path / "crawl-example_com-20260301-000000"
        current.mkdir()
        crawler.write_crawl_report(make_report([("home", "https://a/")]), current)
        assert crawler.find_previous_report(tmp_path, exclude=current) is None


class TestDiffWithPixels:
    """The same screen, two walks: did its content move?"""

    @staticmethod
    def shot(folder, name, flipped=False):
        """A tiny PNG with a real gradient in it (dHash needs structure)."""
        from PIL import Image, ImageDraw

        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        bright, dark = (255, 255, 255), (0, 0, 0)
        image = Image.new("RGB", (60, 60), dark if not flipped else bright)
        draw = ImageDraw.Draw(image)
        left = [0, 0, 29, 59]
        draw.rectangle(left, fill=bright if not flipped else dark)
        image.save(path)
        return str(path)

    def _pair(self, tmp_path, before, after):
        previous = crawler.CrawlReport(url="https://example.com/")
        previous.pages.append(
            crawler.CrawledPage(
                index=1,
                name="home",
                url="https://example.com/",
                file_path=self.shot(tmp_path / "old", "001-home.png", before),
            )
        )
        current = crawler.CrawlReport(url="https://example.com/")
        current.pages.append(
            crawler.CrawledPage(
                index=1,
                name="home",
                url="https://example.com/",
                file_path=self.shot(tmp_path / "new", "001-home.png", after),
            )
        )
        return previous, current

    def test_an_unchanged_screen_is_not_reported(self, tmp_path):
        previous, current = self._pair(tmp_path, False, False)
        diff = crawler.compare_reports(previous, current)
        assert diff.changed == []
        assert diff.unchanged == 1
        assert diff.unknown == 0
        assert "No changes" in diff.summary()

    def test_a_repainted_screen_is_reported_as_changed(self, tmp_path):
        previous, current = self._pair(tmp_path, False, True)
        diff = crawler.compare_reports(previous, current)
        assert [item["name"] for item in diff.changed] == ["home"]
        assert diff.changed[0]["diff"] > crawler.DEFAULT_CHANGED_THRESHOLD
        assert diff.changed[0]["before"].endswith("001-home.png")
        assert diff.changed[0]["after"].endswith("001-home.png")
        assert "changed (" in diff.summary()
        assert diff.to_dict()["changed"][0]["name"] == "home"

    def test_the_threshold_decides_what_counts_as_changed(self, tmp_path):
        previous, current = self._pair(tmp_path, False, True)
        diff = crawler.compare_reports(previous, current, changed_threshold=0.99)
        assert diff.changed == []
        assert diff.unchanged == 1

    def test_a_missing_screenshot_is_counted_separately(self, tmp_path):
        previous, current = self._pair(tmp_path, False, False)
        previous.pages[0].file_path = str(tmp_path / "gone.png")
        diff = crawler.compare_reports(previous, current)
        assert diff.changed == []
        assert diff.unknown == 1
        assert "could not be compared" in diff.summary()

    def test_an_address_only_diff_ignores_the_pixels(self, tmp_path):
        previous, current = self._pair(tmp_path, False, True)
        diff = crawler.compare_reports(previous, current, changed_threshold=None)
        assert diff.changed == [] and diff.unknown == 0
        assert diff.unchanged == 1


class TestRules:
    """What may be clicked, and what never is."""

    @staticmethod
    def _candidate(text="", href="", kind="button"):
        return crawler.Candidate(marker="c0", kind=kind, text=text, href=href)

    def test_a_normal_button_is_kept(self):
        keep, why = crawler.qualifies(
            self._candidate("Products"),
            "https://example.com/",
            "https://example.com/",
            crawler.CrawlOptions(),
        )
        assert keep is True and why == ""

    @pytest.mark.parametrize(
        "label",
        ["Log out", "Delete account", "Unsubscribe", "Buy now", "Place order", "Withdraw funds"],
    )
    def test_dangerous_labels_are_skipped(self, label):
        keep, why = crawler.qualifies(
            self._candidate(label),
            "https://example.com/",
            "https://example.com/",
            crawler.CrawlOptions(),
        )
        assert keep is False
        assert why == "looks dangerous"

    def test_a_dangerous_label_may_be_allowed_on_purpose(self):
        keep, _why = crawler.qualifies(
            self._candidate("Delete account"),
            "https://example.com/",
            "https://example.com/",
            crawler.CrawlOptions(allow_dangerous=True),
        )
        assert keep is True

    def test_extra_dangerous_words_can_be_added(self):
        options = crawler.CrawlOptions(dangerous_words=("subscribe",))
        keep, why = crawler.qualifies(
            self._candidate("Subscribe to the plan"),
            "https://example.com/",
            "https://example.com/",
            options,
        )
        assert keep is False and why == "looks dangerous"

    def test_downloads_are_not_pages(self):
        keep, why = crawler.qualifies(
            self._candidate("Our brochure", href="/brochure.pdf"),
            "https://example.com/",
            "https://example.com/",
            crawler.CrawlOptions(),
        )
        assert keep is False and why == "a download, not a page"

    @pytest.mark.parametrize("href", ["mailto:hi@example.com", "tel:+123", "javascript:void(0)"])
    def test_non_page_schemes_are_skipped(self, href):
        keep, why = crawler.qualifies(
            self._candidate("Contact", href=href),
            "https://example.com/",
            "https://example.com/",
            crawler.CrawlOptions(),
        )
        assert keep is False and why == "not a page link"

    def test_off_site_links_are_skipped_by_default(self):
        keep, why = crawler.qualifies(
            self._candidate("Blog", href="https://blog.example.org/"),
            "https://example.com/",
            "https://example.com/",
            crawler.CrawlOptions(),
        )
        assert keep is False and why == "off-site"

    def test_off_site_links_can_be_allowed(self):
        keep, _why = crawler.qualifies(
            self._candidate("Blog", href="https://blog.example.org/"),
            "https://example.com/",
            "https://example.com/",
            crawler.CrawlOptions(allow_external=True),
        )
        assert keep is True

    def test_www_does_not_make_it_another_site(self):
        assert crawler.same_site("https://example.com/", "https://www.example.com/x") is True
        assert crawler.same_site("https://example.com/", "https://other.example/x") is False
        assert crawler.same_site("https://example.com/", "/relative") is True

    def test_an_ignore_list_wins(self):
        keep, why = crawler.qualifies(
            self._candidate("Products"),
            "https://example.com/",
            "https://example.com/",
            crawler.CrawlOptions(ignore=("products",)),
        )
        assert keep is False and why == "ignored"

    def test_an_include_list_narrows_the_walk(self):
        options = crawler.CrawlOptions(include=("pric",))
        assert (
            crawler.qualifies(
                self._candidate("Pricing"), "https://example.com/", "https://example.com/", options
            )[0]
            is True
        )
        assert (
            crawler.qualifies(
                self._candidate("Products"), "https://example.com/", "https://example.com/", options
            )[0]
            is False
        )

    def test_the_page_top_is_not_a_screen(self):
        keep, why = crawler.qualifies(
            self._candidate("Top", href="#"),
            "https://example.com/",
            "https://example.com/",
            crawler.CrawlOptions(),
        )
        assert keep is False and why == "the page top"

    def test_an_icon_with_no_label_is_left_alone(self):
        options = crawler.CrawlOptions(min_text_length=2)
        keep, why = crawler.qualifies(
            self._candidate("x"), "https://example.com/", "https://example.com/", options
        )
        assert keep is False and why == "no label"


class TestWalking:
    """The walk itself: which screens are reached, and what is written down."""

    def test_the_landing_page_and_what_is_behind_its_buttons(self, tmp_path):
        walker, _settings, _script = make_crawler(tmp_path)
        report = walker.run("https://example.com/")
        names = [page.name for page in report.pages]
        assert names[0] == "home"
        assert "products" in names
        assert "pricing" in names
        assert "sign-in" in names
        assert report.clicks >= 4  # products, pricing, sign in, then what they show

    def test_two_clicks_deep_is_reached(self, tmp_path):
        walker, _settings, _script = make_crawler(tmp_path)
        report = walker.run("https://example.com/")
        deep = [page for page in report.pages if page.depth == 2]
        assert deep, "a screen behind two clicks should be found"
        assert any("Forgot password" in page.path for page in deep)

    def test_depth_one_stops_at_the_landing_page(self, tmp_path):
        options = crawler.CrawlOptions(max_depth=1, max_states=10, max_clicks=20, delay_ms=0)
        walker, _settings, _script = make_crawler(tmp_path, options)
        report = walker.run("https://example.com/")
        assert [page.depth for page in report.pages] == [0] + [1] * (len(report.pages) - 1)
        assert "reset" not in [page.name for page in report.pages]

    def test_screenshots_land_in_a_crawl_folder(self, tmp_path):
        walker, settings, _script = make_crawler(tmp_path)
        report = walker.run("https://example.com/")
        folder = crawler.crawl_folder(report)
        assert folder is not None
        assert folder.name.startswith("crawl-example_com-")
        assert folder.parent == Path(settings.output_dir)
        assert sorted(path.name for path in folder.iterdir())[0] == "001-home.png"
        assert len(list(folder.glob("*.png"))) == len(report.pages)

    def test_the_dangerous_button_is_not_clicked_and_is_explained(self, tmp_path):
        walker, _settings, _script = make_crawler(tmp_path)
        report = walker.run("https://example.com/")
        reasons = {item["reason"] for item in report.skipped}
        assert "looks dangerous" in reasons
        assert "a download, not a page" in reasons
        assert "off-site" in reasons
        assert "delete-account" not in [page.name for page in report.pages]
        # Once per candidate, not once per time its screen is replayed.
        assert len([item for item in report.skipped if item["text"] == "Delete account"]) == 1

    def test_the_buy_now_button_inside_pricing_is_skipped_too(self, tmp_path):
        walker, _settings, _script = make_crawler(tmp_path)
        report = walker.run("https://example.com/")
        assert "buy-now" not in [page.name for page in report.pages]
        assert any(item["text"] == "Buy now" for item in report.skipped)

    def test_allow_dangerous_clicks_it(self, tmp_path):
        options = crawler.CrawlOptions(
            max_depth=2, max_states=10, max_clicks=20, delay_ms=0, allow_dangerous=True
        )
        walker, _settings, _script = make_crawler(tmp_path, options)
        report = walker.run("https://example.com/")
        assert "delete-account" in [page.name for page in report.pages]

    def test_the_state_budget_stops_the_walk(self, tmp_path):
        options = crawler.CrawlOptions(max_depth=3, max_states=3, max_clicks=20, delay_ms=0)
        walker, _settings, _script = make_crawler(tmp_path, options)
        report = walker.run("https://example.com/")
        assert len(report.pages) == 3
        assert report.stopped is True

    def test_the_click_budget_stops_the_walk(self, tmp_path):
        options = crawler.CrawlOptions(max_depth=3, max_states=20, max_clicks=2, delay_ms=0)
        walker, _settings, _script = make_crawler(tmp_path, options)
        report = walker.run("https://example.com/")
        assert report.clicks <= 3  # the budget, plus the click that hit it
        assert report.stopped is True

    def test_the_clock_budget_stops_the_walk(self, tmp_path):
        walker, _settings, _script = make_crawler(tmp_path)
        walker.options = crawler.CrawlOptions(
            max_depth=3, max_states=20, max_clicks=20, delay_ms=0, max_seconds=0.0001
        )
        report = walker.run("https://example.com/")
        assert report.stopped is True

    def test_the_same_screen_is_not_photographed_twice(self, tmp_path):
        site = FakeSite(
            pages={
                "/": (
                    "Home",
                    [
                        FakeWidget("Products", kind="link", link="https://example.com/products"),
                        FakeWidget("Catalogue", kind="link", link="https://example.com/products"),
                    ],
                ),
                "/products": ("Products", []),
            }
        )
        calls = CallLog()
        script = PageScript(page_width=800, page_height=600, site=SiteScript.from_site(site))
        settings = CaptureSettings(
            output_dir=str(tmp_path / "shots"), settle_delay_ms=0, network_idle_timeout_ms=0
        )
        settings.validate()
        engine = CaptureEngine(settings, playwright_factory=make_factory(script, calls))
        report = crawler.Crawler(
            engine, crawler.CrawlOptions(max_depth=2, max_states=10, max_clicks=10, delay_ms=0)
        ).run("https://example.com/")
        products = [page for page in report.pages if page.url.endswith("/products")]
        assert len(products) == 1  # two buttons, one screen

    def test_a_base_capture_can_be_turned_off(self, tmp_path):
        options = crawler.CrawlOptions(max_depth=1, max_states=5, capture_base=False, delay_ms=0)
        walker, _settings, _script = make_crawler(tmp_path, options)
        report = walker.run("https://example.com/")
        assert all(page.depth == 1 for page in report.pages)

    def test_the_report_says_what_was_seen(self, tmp_path):
        walker, _settings, _script = make_crawler(tmp_path)
        report = walker.run("https://example.com/")
        text = report.summary()
        assert "screen(s) captured from https://example.com/" in text
        assert "landing page" in text
        assert "->" in text  # a click-path

    def test_the_json_report_is_written_next_to_the_screens(self, tmp_path):
        walker, _settings, _script = make_crawler(tmp_path)
        report = walker.run("https://example.com/")
        folder = crawler.crawl_folder(report)
        crawler.write_crawl_report(report, folder)
        data = json.loads((folder / "report.json").read_text(encoding="utf-8"))
        assert data["ok"] is True
        assert data["pages"][0]["name"] == "home"
        assert data["options"]["max_depth"] == 2
        assert (folder / "report.csv").exists()
        csv_text = (folder / "report.csv").read_text(encoding="utf-8")
        assert csv_text.splitlines()[0].startswith("index,name,url")
        assert "001-home.png" in csv_text

    def test_the_descriptions_serialise(self, tmp_path):
        walker, _settings, _script = make_crawler(tmp_path)
        data = walker.run("https://example.com/").to_dict()
        assert set(data) >= {"generated_at", "url", "ok", "clicks", "pages", "skipped", "errors"}
        assert data["pages"][0]["path"] == []

    def test_listing_finds_candidates_without_clicking(self, tmp_path):
        walker, _settings, script = make_crawler(tmp_path)
        found = walker.list_clicks("https://example.com/")
        assert found
        texts = [item["text"] for item in found]
        assert "Products" in texts and "Delete account" in texts
        kept = {item["text"]: item["kept"] for item in found}
        assert kept["Products"] is True
        assert kept["Delete account"] is False
        assert kept["Our brochure"] is False
        assert all(item["reason"] == "" for item in found if item["kept"])

    def test_a_candidate_has_a_stable_signature(self):
        one = crawler.Candidate(marker="c1", kind="link", text="Sign in", href="/login")
        two = crawler.Candidate(marker="c7", kind="link", text="Sign in", href="/login")
        three = crawler.Candidate(marker="c8", kind="link", text="Sign up", href="/signup")
        assert one.signature == two.signature
        assert one.signature != three.signature

    def test_is_dangerous_reads_a_label(self):
        assert crawler.is_dangerous("Delete my account") is True
        assert crawler.is_dangerous("Products") is False
        assert crawler.is_dangerous("Purge cache", ("purge",)) is True

    def test_options_serialise(self):
        data = crawler.CrawlOptions(ignore=("news",), allow_external=True).to_dict()
        assert data["ignore"] == ["news"]
        assert data["allow_external"] is True
        assert data["max_depth"] == 2
