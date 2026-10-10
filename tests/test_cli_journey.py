"""``journey`` and ``crawl`` from the command line."""

from __future__ import annotations

import json

from app import cli
from tests.fakes import CallLog, FakeSite, FakeWidget, PageScript, SiteScript, make_factory
from tests.test_crawler import demo_site


def fake_engine_factory(tmp_path, site: FakeSite | None = None):
    """An ``engine_factory`` the CLI can use instead of a real browser."""
    calls = CallLog()
    script = PageScript(
        page_width=1000, page_height=700, site=SiteScript.from_site(site or demo_site())
    )

    def factory(settings, **kwargs):
        from app.core.engine import CaptureEngine

        return CaptureEngine(settings, playwright_factory=make_factory(script, calls), **kwargs)

    return factory, calls


JOURNEY_TOML = """[shop]
url = "https://example.com"
note = "the shop, from the landing page to the filters"

[[shop.steps]]
action = "capture"
name = "landing"

[[shop.steps]]
action = "click"
text = "Products"

[[shop.steps]]
action = "capture"
name = "products"

[[shop.steps]]
action = "click"
text = "Filters"

[[shop.steps]]
action = "capture"
name = "filters-open"
"""


class TestJourneyCommand:
    """One file, a few clicks, a folder full of screens."""

    def _write(self, tmp_path, text=JOURNEY_TOML):
        path = tmp_path / "journey.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_it_writes_a_screenshot_per_screen(self, tmp_path, capsys):
        path = self._write(tmp_path)
        factory, calls = fake_engine_factory(tmp_path)
        code = cli.main(
            ["journey", "--file", str(path), "--out", str(tmp_path / "shots"), "--quiet"],
            engine_factory=factory,
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "shop: ok" in out
        assert "3 capture(s)" in out
        assert "Screenshots in" in out
        files = sorted(item.name for item in (tmp_path / "shots" / "journeys" / "shop").iterdir())
        assert files == ["01-landing.png", "02-products.png", "03-filters-open.png"]
        assert [action[0] for action in calls.actions] == ["click", "click"]

    def test_json_prints_the_report(self, tmp_path, capsys):
        path = self._write(tmp_path)
        factory, _calls = fake_engine_factory(tmp_path)
        code = cli.main(
            ["journey", "--file", str(path), "--out", str(tmp_path / "shots"), "--json", "--quiet"],
            engine_factory=factory,
        )
        data = json.loads(capsys.readouterr().out)
        assert code == 0
        assert data["ok"] is True
        assert data["captures"] == 3
        assert data["journeys"][0]["steps"][0]["action"] == "capture"

    def test_list_describes_the_steps_without_running_anything(self, tmp_path, capsys):
        path = self._write(tmp_path)
        code = cli.main(["journey", "--file", str(path), "--list"])
        out = capsys.readouterr().out
        assert code == 0
        assert "shop (enabled) - https://example.com - 5 step(s)" in out
        assert '2. click "Products"' in out
        assert "3 capture(s)" in out
        assert not (tmp_path / "shots").exists()  # nothing was opened

    def test_sample_prints_a_file_that_parses(self, tmp_path, capsys):
        assert cli.main(["journey", "--sample"]) == 0
        text = capsys.readouterr().out
        from app.core import journey

        sample = tmp_path / "sample.toml"
        sample.write_text(text, encoding="utf-8")
        assert [item.name for item in journey.load_journeys(sample, env={})] == ["home", "account"]

    def test_without_a_file_it_says_what_to_do(self, capsys):
        assert cli.main(["journey", "--list"]) == 2
        assert "journey --sample" in capsys.readouterr().err

    def test_a_broken_file_exits_two(self, tmp_path, capsys):
        """A file that cannot be read is refused before any browser starts."""
        path = tmp_path / "broken.toml"
        path.write_text(
            '[x]\nurl = "https://a"\n[[x.steps]]\naction = "explode"\n', encoding="utf-8"
        )
        assert cli.main(["journey", "--file", str(path), "--out", str(tmp_path / "shots")]) == 2
        err = capsys.readouterr().err
        assert "unknown action 'explode'" in err
        assert not (tmp_path / "shots").exists()

    def test_a_failed_step_exits_one(self, tmp_path, capsys):
        path = self._write(
            tmp_path,
            '[one]\nurl = "https://example.com"\n[[one.steps]]\naction = "click"\ntext = "Nowhere"\n'
            '[[one.steps]]\naction = "capture"\nname = "never"\n',
        )
        factory, _calls = fake_engine_factory(tmp_path)
        code = cli.main(
            ["journey", "--file", str(path), "--out", str(tmp_path / "shots"), "--quiet"],
            engine_factory=factory,
        )
        assert code == 1
        assert "one: FAILED" in capsys.readouterr().out

    def test_a_journey_file_can_be_passed_positionally(self, tmp_path, capsys):
        path = self._write(tmp_path)
        factory, _calls = fake_engine_factory(tmp_path)
        code = cli.main(
            ["journey", str(path), "--out", str(tmp_path / "shots"), "--quiet"],
            engine_factory=factory,
        )
        assert code == 0
        assert (tmp_path / "shots" / "journeys" / "shop" / "01-landing.png").exists()

    def test_the_viewport_and_format_flags_travel(self, tmp_path):
        path = self._write(tmp_path)
        factory, calls = fake_engine_factory(tmp_path)
        code = cli.main(
            [
                "journey",
                "--file",
                str(path),
                "--out",
                str(tmp_path / "shots"),
                "--viewport",
                "900x600",
                "--scale",
                "1",
                "--format",
                "jpeg",
                "--quiet",
            ],
            engine_factory=factory,
        )
        assert code == 0
        assert calls.contexts[0]["viewport"] == {"width": 900, "height": 600}
        assert calls.screenshots[0]["type"] == "jpeg"
        assert (tmp_path / "shots" / "journeys" / "shop" / "01-landing.jpg").exists()

    def test_recipes_are_listed(self, capsys):
        assert cli.main(["journey", "--recipes"]) == 0
        out = capsys.readouterr().out
        assert "login-dashboard" in out and "Shop: landing, products, filters" in out

    def test_recipe_runs_without_a_file(self, tmp_path, capsys):
        factory, _calls = fake_engine_factory(tmp_path)
        code = cli.main(
            [
                "journey",
                "--recipe",
                "shop",
                "--url",
                "https://example.com",
                "--out",
                str(tmp_path / "shots"),
                "--quiet",
            ],
            engine_factory=factory,
        )
        assert code == 0
        files = sorted(item.name for item in (tmp_path / "shots" / "journeys" / "shop").iterdir())
        assert files[:2] == ["01-landing.png", "02-products.png"]

    def test_a_recipe_can_be_saved_from_the_command_line(self, tmp_path, capsys, monkeypatch):
        from app.core import journey as journey_module

        monkeypatch.setenv(journey_module.RECIPES_ENV, str(tmp_path / "recipes"))

        code = cli.main(
            [
                "journey",
                "--save-recipe",
                "monday",
                "--steps",
                'click "Products" ; capture reports',
                "--recipe-title",
                "The Monday report",
            ]
        )

        assert code == 0
        out = capsys.readouterr().out
        assert "Saved the 'monday' recipe" in out
        assert "journey --recipe monday" in out
        saved = tmp_path / "recipes" / "monday.txt"
        assert saved.read_text(encoding="utf-8").startswith("# Title: The Monday report")
        # and it runs, with the fake shop behind it
        factory, _calls = fake_engine_factory(tmp_path)
        assert (
            cli.main(
                [
                    "journey",
                    "--recipe",
                    "monday",
                    "--url",
                    "https://example.com",
                    "--out",
                    str(tmp_path / "shots"),
                    "--quiet",
                ],
                engine_factory=factory,
            )
            == 0
        )
        assert (tmp_path / "shots" / "journeys" / "monday" / "01-reports.png").exists()

    def test_saving_a_recipe_without_steps_exits_two(self, tmp_path, capsys, monkeypatch):
        from app.core import journey as journey_module

        monkeypatch.setenv(journey_module.RECIPES_ENV, str(tmp_path / "recipes"))
        assert cli.main(["journey", "--save-recipe", "empty"]) == 2
        assert "--save-recipe needs the steps" in capsys.readouterr().err

    def test_saving_steps_that_do_not_parse_exits_two(self, tmp_path, capsys, monkeypatch):
        from app.core import journey as journey_module

        monkeypatch.setenv(journey_module.RECIPES_ENV, str(tmp_path / "recipes"))
        code = cli.main(["journey", "--save-recipe", "broken", "--steps", "dance wildly"])
        assert code == 2
        assert "cannot be saved" in capsys.readouterr().err

    def test_the_picker_lists_a_recipe_of_your_own(self, tmp_path, capsys, monkeypatch):
        from app.core import journey as journey_module

        monkeypatch.setenv(journey_module.RECIPES_ENV, str(tmp_path / "recipes"))
        journey_module.save_recipe("checkout", 'click "Cart"\ncapture cart')
        assert cli.main(["journey", "--recipes"]) == 0
        out = capsys.readouterr().out
        assert "checkout" in out and "(your own," in out

    def test_an_unknown_recipe_exits_two(self, tmp_path, capsys):
        factory, _calls = fake_engine_factory(tmp_path)
        code = cli.main(
            ["journey", "--recipe", "nope", "--url", "https://example.com"],
            engine_factory=factory,
        )
        assert code == 2
        assert "unknown recipe 'nope'" in capsys.readouterr().err

    def test_steps_on_the_command_line_need_no_toml(self, tmp_path, capsys):
        factory, _calls = fake_engine_factory(tmp_path)
        code = cli.main(
            [
                "journey",
                "--steps",
                'click "Products" ; capture products',
                "--url",
                "https://example.com",
                "--name",
                "quick",
                "--out",
                str(tmp_path / "shots"),
                "--quiet",
            ],
            engine_factory=factory,
        )
        assert code == 0
        assert "quick: ok" in capsys.readouterr().out
        files = sorted(item.name for item in (tmp_path / "shots" / "journeys" / "quick").iterdir())
        assert files == ["01-products.png"]

    def test_a_steps_file_is_read_from_disk(self, tmp_path, capsys):
        steps = tmp_path / "walk.txt"
        steps.write_text(
            "# only the products screen is interesting\n"
            'click "Products"\n'
            "wait 10\n"
            "capture products\n",
            encoding="utf-8",
        )
        factory, _calls = fake_engine_factory(tmp_path)
        code = cli.main(
            [
                "journey",
                "--steps-file",
                str(steps),
                "--url",
                "https://example.com",
                "--list",
            ],
            engine_factory=factory,
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "(enabled) - https://example.com - 3 step(s)" in out

    def test_bad_steps_and_missing_files_exit_two(self, tmp_path, capsys):
        factory, _calls = fake_engine_factory(tmp_path)
        assert (
            cli.main(
                ["journey", "--steps", 'clik "Products"', "--url", "https://example.com"],
                engine_factory=factory,
            )
            == 2
        )
        assert "unknown action 'clik'" in capsys.readouterr().err

        assert (
            cli.main(
                ["journey", "--steps-file", str(tmp_path / "nope.txt"), "--url", "https://a.com"],
                engine_factory=factory,
            )
            == 2
        )
        assert "Cannot read" in capsys.readouterr().err

    def test_steps_without_a_page_to_start_on_exit_two(self, tmp_path, capsys):
        factory, _calls = fake_engine_factory(tmp_path)
        code = cli.main(
            ["journey", "--steps", 'click "Products" ; capture products'],
            engine_factory=factory,
        )
        assert code == 2
        assert "--url" in capsys.readouterr().err

    def test_the_parsers_exist_in_the_subcommand_list(self):
        assert "journey" in cli.SUBCOMMANDS
        assert "crawl" in cli.SUBCOMMANDS
        args = cli.build_journey_parser().parse_args(["--file", "j.toml", "--out", "x"])
        assert args.file == "j.toml" and args.settle == 1500


class TestWalkCommand:
    """``walk``: what the last journey/crawl did, and the page with thumbnails."""

    def test_without_a_walk_it_says_so(self, tmp_path, capsys):
        assert cli.main(["walk", "--dir", str(tmp_path)]) == 1
        assert "No walk" in capsys.readouterr().err

    def test_after_a_crawl_it_lists_the_screens_and_writes_html(self, tmp_path, capsys):
        out = tmp_path / "shots"
        factory, _calls = fake_engine_factory(tmp_path)
        assert (
            cli.main(
                ["crawl", "https://example.com", "--out", str(out), "--max-depth", "1", "--quiet"],
                engine_factory=factory,
            )
            == 0
        )
        capsys.readouterr()

        code = cli.main(["walk", "--dir", str(out), "--html", str(out / "walk.html")])

        out_text = capsys.readouterr().out
        assert code == 0
        assert "crawl walk in" in out_text
        assert "[landing page]" in out_text
        assert (out / "walk.html").exists()
        assert "001-home.png" in (out / "walk.html").read_text(encoding="utf-8")

    def test_json_prints_the_walk(self, tmp_path, capsys):
        out = tmp_path / "shots"
        factory, _calls = fake_engine_factory(tmp_path)
        assert (
            cli.main(
                ["crawl", "https://example.com", "--out", str(out), "--max-depth", "1", "--quiet"],
                engine_factory=factory,
            )
            == 0
        )
        capsys.readouterr()

        assert cli.main(["walk", "--dir", str(out), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["kind"] == "crawl"
        assert payload["screens"][0]["name"] == "home"

    def test_after_a_journey_it_reads_the_journey_report(self, tmp_path, capsys):
        factory, _calls = fake_engine_factory(tmp_path)
        assert (
            cli.main(
                [
                    "journey",
                    "--steps",
                    'click "Products" ; capture products',
                    "--url",
                    "https://example.com",
                    "--name",
                    "quick",
                    "--out",
                    str(tmp_path / "shots"),
                    "--quiet",
                ],
                engine_factory=factory,
            )
            == 0
        )
        capsys.readouterr()

        assert cli.main(["walk", "--dir", str(tmp_path / "shots")]) == 0
        out_text = capsys.readouterr().out
        assert "journey walk in" in out_text
        assert "01-products" in out_text


class TestCrawlCommand:
    """One URL, a walk through the site, a folder full of screens."""

    def test_a_walk_writes_one_file_per_screen(self, tmp_path, capsys):
        factory, _calls = fake_engine_factory(tmp_path)
        code = cli.main(
            [
                "crawl",
                "https://example.com/",
                "--out",
                str(tmp_path / "shots"),
                "--max-states",
                "4",
                "--delay",
                "0",
                "--quiet",
            ],
            engine_factory=factory,
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "screen(s) captured from https://example.com/" in out
        assert "Screenshots in" in out
        folders = list((tmp_path / "shots").glob("crawl-example_com-*"))
        assert len(folders) == 1
        assert (folders[0] / "001-home.png").exists()
        assert (folders[0] / "report.json").exists()
        assert (folders[0] / "report.csv").exists()

    def test_json_prints_the_report(self, tmp_path, capsys):
        factory, _calls = fake_engine_factory(tmp_path)
        code = cli.main(
            [
                "crawl",
                "https://example.com/",
                "--out",
                str(tmp_path / "shots"),
                "--max-states",
                "2",
                "--delay",
                "0",
                "--json",
                "--quiet",
            ],
            engine_factory=factory,
        )
        data = json.loads(capsys.readouterr().out)
        assert code == 0
        assert data["pages"][0]["name"] == "home"
        assert data["ok"] is True

    def test_list_shows_what_would_be_clicked_and_why_not(self, tmp_path, capsys):
        factory, _calls = fake_engine_factory(tmp_path)
        code = cli.main(
            [
                "crawl",
                "https://example.com/",
                "--out",
                str(tmp_path / "shots"),
                "--list",
                "--quiet",
            ],
            engine_factory=factory,
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "Products" in out
        assert "skip (looks dangerous)" in out
        assert "skip (a download, not a page)" in out
        assert "candidate(s) would be clicked" in out

    def test_list_json_is_machine_readable(self, tmp_path, capsys):
        factory, _calls = fake_engine_factory(tmp_path)
        code = cli.main(
            [
                "crawl",
                "https://example.com/",
                "--out",
                str(tmp_path / "shots"),
                "--list",
                "--json",
                "--quiet",
            ],
            engine_factory=factory,
        )
        data = json.loads(capsys.readouterr().out)
        assert code == 0
        assert any(item["text"] == "Products" and item["kept"] for item in data)

    def test_the_flags_reach_the_options(self, tmp_path, capsys):
        factory, _calls = fake_engine_factory(tmp_path)
        code = cli.main(
            [
                "crawl",
                "https://example.com/",
                "--out",
                str(tmp_path / "shots"),
                "--max-depth",
                "1",
                "--max-states",
                "3",
                "--ignore",
                "pric, news",
                "--delay",
                "0",
                "--json",
                "--quiet",
            ],
            engine_factory=factory,
        )
        data = json.loads(capsys.readouterr().out)
        assert code == 0
        assert data["options"]["max_depth"] == 1
        assert data["options"]["ignore"] == ["pric", "news"]
        assert "pricing" not in [page["name"] for page in data["pages"]]

    def test_include_narrows_and_dangerous_widens(self, tmp_path, capsys):
        factory, _calls = fake_engine_factory(tmp_path)
        code = cli.main(
            [
                "crawl",
                "https://example.com/",
                "--out",
                str(tmp_path / "shots"),
                "--include",
                "delete",
                "--allow-dangerous",
                "--max-states",
                "2",
                "--delay",
                "0",
                "--json",
                "--quiet",
            ],
            engine_factory=factory,
        )
        data = json.loads(capsys.readouterr().out)
        assert code == 0
        assert any(page["name"] == "delete-account" for page in data["pages"])

    def test_the_click_selectors_can_be_replaced(self, tmp_path, capsys):
        factory, _calls = fake_engine_factory(tmp_path)
        code = cli.main(
            [
                "crawl",
                "https://example.com/",
                "--out",
                str(tmp_path / "shots"),
                "--click",
                "button",
                "--max-states",
                "3",
                "--delay",
                "0",
                "--json",
                "--quiet",
            ],
            engine_factory=factory,
        )
        data = json.loads(capsys.readouterr().out)
        assert code == 0
        assert data["pages"][0]["name"] == "home"

    @staticmethod
    def compare_site(pricing: bool) -> FakeSite:
        """The same shop twice: the second time with a Pricing page on it."""
        home = [
            FakeWidget("Products", kind="link", link="https://example.com/products"),
            FakeWidget("Sign in", kind="link", link="https://example.com/login"),
        ]
        pages = {
            "/": ("Home", home),
            "/products": ("Products", [FakeWidget("Filters")]),
            "/login": ("Sign in", []),
        }
        if pricing:
            home.append(FakeWidget("Pricing", kind="link", link="https://example.com/pricing"))
            pages["/pricing"] = ("Pricing", [])
        return FakeSite(pages=pages)

    def _walk(self, tmp_path, out, site, *extra: str, capsys=None) -> int:
        factory, _calls = fake_engine_factory(tmp_path, site)
        code = cli.main(
            [
                "crawl",
                "https://example.com",
                "--out",
                str(out),
                "--max-depth",
                "1",
                "--quiet",
                *extra,
            ],
            engine_factory=factory,
        )
        if capsys is not None:
            capsys.readouterr()
        return code

    def test_compare_reports_the_screens_that_appeared(self, tmp_path, capsys):
        """A second walk of the same site, with one page added to it."""
        out = tmp_path / "shots"
        assert self._walk(tmp_path, out, self.compare_site(False), capsys=capsys) == 0
        assert self._walk(tmp_path, out, self.compare_site(True), "--compare") == 0

        out_text = capsys.readouterr().out
        assert "Changes since the previous crawl" in out_text
        assert "+ pricing" in out_text
        assert "1 new" in out_text

    def test_compare_reports_a_screen_that_went_away(self, tmp_path, capsys):
        out = tmp_path / "shots"
        assert self._walk(tmp_path, out, self.compare_site(True), capsys=capsys) == 0
        assert self._walk(tmp_path, out, self.compare_site(False), "--compare") == 0

        out_text = capsys.readouterr().out
        assert "- pricing" in out_text
        assert "1 gone" in out_text

    def test_a_changed_screen_is_reported_and_can_fail_the_run(self, tmp_path, capsys):
        """A screen that looks different from the previous walk is called out."""
        out = tmp_path / "shots"
        site = self.compare_site(False)
        # a previous walk, with one patterned screenshot for the home screen
        from PIL import Image, ImageDraw

        previous = out / "crawl-example_com-20260101-000000"
        previous.mkdir(parents=True)
        shot = previous / "001-home.png"
        image = Image.new("RGB", (60, 60), (0, 0, 0))
        ImageDraw.Draw(image).rectangle([0, 0, 29, 59], fill=(255, 255, 255))
        image.save(shot)
        (previous / "report.json").write_text(
            json.dumps(
                {
                    "url": "https://example.com",
                    "generated_at": "2026-01-01T09:00:00",
                    "pages": [
                        {
                            "index": 1,
                            "name": "home",
                            "url": "https://example.com",
                            "file": str(shot),
                            "path": [],
                        }
                    ],
                    "skipped": [],
                    "errors": [],
                }
            ),
            encoding="utf-8",
        )

        factory, _calls = fake_engine_factory(tmp_path, site)
        code = cli.main(
            [
                "crawl",
                "https://example.com",
                "--out",
                str(out),
                "--max-depth",
                "0",
                "--compare",
                "--changed-threshold",
                "0.001",
                "--fail-on-change",
            ],
            engine_factory=factory,
        )

        out_text = capsys.readouterr().out
        assert code == 1, "a change with --fail-on-change must exit non-zero"
        assert "changed (" in out_text
        assert "1 changed" in out_text

    def test_without_fail_on_change_a_change_still_exits_zero(self, tmp_path, capsys):
        out = tmp_path / "shots"
        previous = out / "crawl-example_com-20260101-000000"
        previous.mkdir(parents=True)
        (previous / "report.json").write_text(
            json.dumps({"url": "https://example.com", "pages": [], "skipped": [], "errors": []}),
            encoding="utf-8",
        )
        assert self._walk(tmp_path, out, self.compare_site(False), "--compare") == 0

    def test_zero_threshold_skips_the_pixel_comparison(self, tmp_path, capsys):
        out = tmp_path / "shots"
        site = self.compare_site(True)
        assert self._walk(tmp_path, out, site, capsys=capsys) == 0
        assert self._walk(tmp_path, out, site, "--compare", "--changed-threshold", "0") == 0
        out_text = capsys.readouterr().out
        assert "No changes since the previous crawl" in out_text

    def test_compare_without_an_earlier_walk_says_so(self, tmp_path, capsys):
        code = self._walk(tmp_path, tmp_path / "shots", self.compare_site(True), "--compare")
        assert code == 0
        assert "No changes since the previous crawl" in capsys.readouterr().out

    def test_compare_writes_diff_json_and_lands_in_the_json_report(self, tmp_path, capsys):
        out = tmp_path / "shots"
        assert self._walk(tmp_path, out, self.compare_site(False), capsys=capsys) == 0
        assert self._walk(tmp_path, out, self.compare_site(True), "--compare", "--json") == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["diff"]["added"][0]["name"] == "pricing"
        folders = sorted(item for item in out.iterdir() if item.is_dir())
        assert (folders[-1] / "diff.json").exists()

    def test_the_crawl_parser_defaults(self):
        args = cli.build_crawl_parser().parse_args(["https://a.com"])
        assert args.max_depth == 2
        assert args.max_states == 25
        assert args.delay == 300
        assert args.out == "shots"
