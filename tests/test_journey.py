"""Journeys: walk a site, press the buttons, photograph every screen on the way."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core import journey
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


def shop_site() -> FakeSite:
    """A tiny shop: home -> products -> filters (a modal), plus a login page."""
    return FakeSite(
        pages={
            "/": (
                "Home",
                [
                    FakeWidget("Products", kind="link", link="https://example.com/products"),
                    FakeWidget("Sign in", kind="link", link="https://example.com/login"),
                ],
            ),
            "/products": (
                "Products",
                [
                    FakeWidget("Filters", opens=FakeModal("Filters", selector=".filters")),
                    FakeWidget("Sort by price"),
                ],
            ),
            "/login": (
                "Sign in",
                [FakeWidget("Forgot password", kind="link", link="https://example.com/reset")],
            ),
            "/reset": ("Reset password", []),
        }
    )


def make_engine(tmp_path, script: PageScript | None = None, **settings_kwargs):
    """An engine wired to a fake browser (and, by default, the fake shop)."""
    calls = CallLog()
    if script is None:
        site = SiteScript.from_site(shop_site())
        site.inputs = {"#q", "#email", "#password"}
        script = PageScript(page_width=1200, page_height=900, site=site)
    settings = CaptureSettings(
        output_dir=str(tmp_path / "shots"),
        settle_delay_ms=0,
        network_idle_timeout_ms=0,
        **settings_kwargs,
    )
    settings.validate()
    engine = CaptureEngine(
        settings, playwright_factory=make_factory(script, calls), log=lambda level, message: None
    )
    return engine, settings, Path(settings.output_dir), calls


@pytest.fixture
def shop(tmp_path):
    """``(engine, settings, output folder, call log)`` wired to the fake shop."""
    return make_engine(tmp_path)


class TestParsing:
    """The journey file: what a person writes, and what it has to mean."""

    def test_a_plain_journey(self):
        doc = {
            "shop": {
                "url": "https://example.com",
                "steps": [
                    {"action": "capture", "name": "landing"},
                    {"action": "click", "text": "Products"},
                ],
            }
        }
        parsed = journey.parse_journeys(doc)[0]
        assert parsed.name == "shop"
        assert parsed.url == "https://example.com"
        assert [step.action for step in parsed.steps] == ["capture", "click"]
        assert parsed.steps[0].label == "landing"  # a capture's name is its label
        assert parsed.steps[1].text == "Products"

    def test_every_action_is_understood(self):
        for action in journey.ACTIONS:
            table: dict = {"action": action}
            if action in (
                "click",
                "fill",
                "press",
                "hover",
                "select",
                "check",
                "download",
                "ensure_login",
                "wait_for",
            ):
                table["selector"] = "#thing"
            if action == "fill":
                table["value"] = "text"
            if action == "goto":
                table["url"] = "https://example.com/other"
            journey.parse_step("j", 1, table)

    def test_an_unknown_action_names_the_alternatives(self):
        with pytest.raises(journey.JourneyError) as excinfo:
            journey.parse_step("j", 3, {"action": "explode"})
        assert "unknown action 'explode'" in str(excinfo.value)
        assert "click" in str(excinfo.value)

    def test_an_unknown_key_is_a_typo_worth_reporting(self):
        with pytest.raises(journey.JourneyError) as excinfo:
            journey.parse_step("j", 1, {"action": "click", "selector": "#a", "txet": "x"})
        assert "txet" in str(excinfo.value)

    def test_click_needs_something_to_click(self):
        with pytest.raises(journey.JourneyError):
            journey.parse_step("j", 1, {"action": "click"})
        with pytest.raises(journey.JourneyError):
            journey.parse_step("j", 1, {"action": "fill", "selector": "#a"})
        with pytest.raises(journey.JourneyError):
            journey.parse_step("j", 1, {"action": "goto"})

    def test_a_journey_without_a_capture_is_refused(self):
        with pytest.raises(journey.JourneyError) as excinfo:
            journey.parse_journey(
                "j", {"url": "https://a", "steps": [{"action": "click", "text": "x"}]}
            )
        assert "no capture step" in str(excinfo.value)

    def test_a_journey_without_a_url_is_refused(self):
        with pytest.raises(journey.JourneyError):
            journey.parse_journey("j", {"steps": [{"action": "capture"}]})

    def test_an_empty_file_is_refused(self):
        with pytest.raises(journey.JourneyError):
            journey.parse_journeys({})

    def test_press_defaults_to_enter(self):
        step = journey.parse_step("j", 1, {"action": "press", "selector": "#q"})
        assert step.key == "Enter"

    def test_a_nameless_capture_gets_a_numbered_label(self):
        step = journey.parse_step("j", 4, {"action": "capture"})
        assert step.label == "screen-4"

    def test_name_alone_names_the_element(self):
        step = journey.parse_step("j", 1, {"action": "click", "name": "Sign in"})
        assert step.text == "Sign in"

    def test_optional_and_timeout_travel(self):
        step = journey.parse_step(
            "j", 1, {"action": "click", "text": "Cookie", "optional": True, "timeout": 500}
        )
        assert step.optional is True and step.timeout == 500

    # -- variables ---------------------------------------------------------
    def test_a_variable_comes_from_the_environment(self):
        assert journey.expand_variables("${SHOP_USER}", {"SHOP_USER": "ali"}) == "ali"

    def test_a_variable_may_carry_a_fallback(self):
        assert journey.expand_variables("${NOPE:-demo@example.com}", {}) == "demo@example.com"

    def test_an_undefined_variable_is_reported(self):
        assert journey.missing_variables("${WHAT_IS_THIS}", {}) == ["WHAT_IS_THIS"]
        assert journey.missing_variables("${OK:-x}", {}) == []

    def test_a_file_with_a_missing_variable_is_refused(self, tmp_path):
        path = tmp_path / "j.toml"
        path.write_text(
            '[one]\nurl = "https://a"\n[[one.steps]]\naction = "fill"\nselector = "#a"\n'
            'value = "${NOT_SET_ANYWHERE}"\n[[one.steps]]\naction = "capture"\n',
            encoding="utf-8",
        )
        with pytest.raises(journey.JourneyError) as excinfo:
            journey.load_journeys(path)
        assert "NOT_SET_ANYWHERE" in str(excinfo.value)

    def test_a_file_that_is_not_toml_says_so(self, tmp_path):
        path = tmp_path / "j.toml"
        path.write_text("this is not toml at all", encoding="utf-8")
        with pytest.raises(journey.JourneyError) as excinfo:
            journey.load_journeys(path)
        assert "not valid TOML" in str(excinfo.value)

    def test_a_missing_file_says_so(self, tmp_path):
        with pytest.raises(journey.JourneyError):
            journey.load_journeys(tmp_path / "nope.toml")

    def test_the_sample_file_parses(self, tmp_path):
        path = tmp_path / "sample.toml"
        path.write_text(journey.sample_journey(), encoding="utf-8")
        journeys = journey.load_journeys(path, env={})
        assert [item.name for item in journeys] == ["home", "account"]
        assert all(item.captures() for item in journeys)

    def test_selectors_try_the_label_before_giving_up(self):
        step = journey.parse_step("j", 1, {"action": "click", "text": "Sign in"})
        candidates = step.selectors()
        assert candidates[0] == 'text="Sign in"'
        assert any("button:has-text" in item for item in candidates)

    def test_role_and_name_make_a_selector(self):
        step = journey.parse_step("j", 1, {"action": "click", "role": "button", "name": "Save"})
        assert any('[role="button"]' in item for item in step.selectors())


class TestStepLines:
    """The short one-line steps the app's Clicks box (and ``--steps``) accepts."""

    def test_actions_parse_into_step_tables(self):
        assert journey.parse_step_line('click "Sign in"') == {"action": "click", "text": "Sign in"}
        assert journey.parse_step_line("click #login") == {
            "action": "click",
            "selector": "#login",
        }
        assert journey.parse_step_line("hover .menu") == {"action": "hover", "selector": ".menu"}
        assert journey.parse_step_line("check #tos") == {"action": "check", "selector": "#tos"}
        assert journey.parse_step_line("goto /pricing") == {"action": "goto", "url": "/pricing"}
        assert journey.parse_step_line("wait 800") == {"action": "wait", "ms": 800}
        assert journey.parse_step_line("capture dashboard") == {
            "action": "capture",
            "label": "dashboard",
        }
        assert journey.parse_step_line("back") == {"action": "back"}
        assert journey.parse_step_line("reload") == {"action": "reload"}
        assert journey.parse_step_line("press Enter") == {"action": "press", "key": "Enter"}

    def test_a_selector_written_with_an_equals_sign_stays_a_selector(self):
        step = journey.parse_step_line('fill input[name="email"] = me@x.com')
        assert step["selector"] == 'input[name="email"]'
        assert step["value"] == "me@x.com"

    def test_positions_can_be_named_by_role_and_name(self):
        step = journey.parse_step_line('click role=button name="Save changes"')
        assert step == {"action": "click", "role": "button", "name": "Save changes"}

    def test_trailing_flags_are_peeled_off(self):
        step = journey.parse_step_line("wait_for .dashboard optional timeout=8000")
        assert step == {
            "action": "wait_for",
            "selector": ".dashboard",
            "optional": True,
            "timeout": 8000,
        }

    def test_a_bad_line_names_the_problem(self):
        with pytest.raises(journey.JourneyError, match="unknown action 'clik'"):
            journey.parse_step_line('clik "Sign in"')
        with pytest.raises(journey.JourneyError, match="needs a selector"):
            journey.parse_step_line("click")
        with pytest.raises(journey.JourneyError, match="needs <selector> = <value>"):
            journey.parse_step_line("fill #email")
        with pytest.raises(journey.JourneyError, match="milliseconds"):
            journey.parse_step_line("wait soon")

    def test_a_block_skips_comments_and_numbers_the_errors(self):
        steps = journey.parse_steps_text(
            '# what I do on the shop\n\nclick "Sign in"\ncapture dashboard\n'
        )
        assert [step["action"] for step in steps] == ["click", "capture"]
        with pytest.raises(journey.JourneyError, match="line 3"):
            journey.parse_steps_text('click "Sign in"\n# a comment\nwait soon\n')

    def test_missing_variables_are_reported_with_the_line(self):
        with pytest.raises(journey.JourneyError, match="SHOP_EMAIL"):
            journey.parse_steps_text("fill #email = ${SHOP_EMAIL}")

    def test_text_becomes_a_runnable_journey(self):
        spec = journey.journey_from_text(
            "shop", "https://example.com", 'click "Sign in"\ncapture dashboard'
        )
        assert spec.name == "shop"
        assert spec.url == "https://example.com"
        assert [step.action for step in spec.steps] == ["click", "capture"]
        assert spec.steps[-1].label == "dashboard"

    def test_a_capture_step_is_added_when_it_was_forgotten(self):
        spec = journey.journey_from_text("quick", "https://example.com", 'click "Sign in"')
        assert [step.action for step in spec.steps] == ["click", "capture"]
        assert spec.captures()[0].label == "screen"

    def test_the_written_journey_really_runs(self, shop):
        engine, _settings, folder, _calls = shop
        spec = journey.journey_from_text(
            "shop", "https://example.com", 'click "Sign in"\nwait 10\ncapture login'
        )
        report = journey.JourneyRunner(engine).run([spec])
        assert report.captures == 1
        saved = sorted(item.name for item in (folder / "journeys" / "shop").iterdir())
        assert saved == ["01-login.png"]


class TestDownloadsAndKeys:
    """Two steps that are not about a screen: a file, and a key for the page."""

    @staticmethod
    def _reports_engine(tmp_path, name="August Report.pdf"):
        from tests.fakes import PageScript, SiteScript

        script = PageScript(
            page_width=1200, page_height=900, site=SiteScript.from_site(shop_site())
        )
        script.download_name = name
        return make_engine(tmp_path, script=script)

    def test_a_download_step_saves_the_file_the_site_sent(self, tmp_path):
        engine, _settings, folder, calls = self._reports_engine(tmp_path)
        journeys = journey.parse_journeys(
            {
                "reports": {
                    "url": "https://example.com/",
                    "steps": [
                        {"action": "click", "text": "Products"},
                        {"action": "download", "text": "Sort by price"},
                        {"action": "capture", "name": "reports"},
                    ],
                }
            }
        )

        report = journey.JourneyRunner(engine).run(journeys)

        assert report.ok, [step.message for step in report.journeys[0].steps]
        saved = list((folder / "downloads").glob("*.pdf"))
        assert len(saved) == 1
        assert saved[0].read_bytes() == b"%PDF-fake"
        assert saved[0].name == "august_report.pdf", "the site's name, made safe"
        assert saved[0].suffix == ".pdf", "a download keeps its type"
        assert calls.contexts[0]["accept_downloads"] is True, "the browser keeps files"

    def test_two_downloads_with_the_same_name_do_not_overwrite_each_other(self, tmp_path):
        engine, _settings, folder, _calls = self._reports_engine(tmp_path, name="report.pdf")
        journeys = journey.parse_journeys(
            {
                "reports": {
                    "url": "https://example.com/",
                    "steps": [
                        {"action": "click", "text": "Products"},
                        {"action": "download", "text": "Sort by price"},
                        {"action": "download", "text": "Sort by price"},
                        {"action": "capture", "name": "done"},
                    ],
                }
            }
        )

        journey.JourneyRunner(engine).run(journeys)

        assert sorted(item.name for item in (folder / "downloads").iterdir()) == [
            "report.pdf",
            "report_2.pdf",
        ]

    def test_a_download_step_that_matches_nothing_says_so(self, tmp_path):
        engine, _settings, _folder, _calls = self._reports_engine(tmp_path)
        journeys = journey.parse_journeys(
            {
                "reports": {
                    "url": "https://example.com/",
                    "steps": [
                        {"action": "download", "text": "Nothing like this"},
                        {"action": "capture", "name": "reports"},
                    ],
                }
            }
        )

        report = journey.JourneyRunner(engine).run(journeys)

        assert report.ok is False
        assert "nothing matching" in report.journeys[0].steps[0].message

    def test_a_key_without_a_target_goes_to_the_page(self, shop):
        engine, _settings, folder, calls = shop
        journeys = journey.parse_journeys(
            {
                "shortcut": {
                    "url": "https://example.com/",
                    "steps": [
                        {"action": "press", "key": "Control+K"},
                        {"action": "capture", "name": "search"},
                    ],
                }
            }
        )

        report = journey.JourneyRunner(engine).run(journeys)

        assert report.ok, report.summary()
        assert ("key", "", "Control+K") in calls.actions
        assert (folder / "journeys" / "shortcut" / "01-search.png").exists()

    def test_the_step_line_for_a_download_survives_a_round_trip(self):
        spec = journey.journey_from_text(
            "reports", "https://example.com", "download #pdf\ncapture reports"
        )
        assert [step.action for step in spec.steps] == ["download", "capture"]
        assert spec.steps[0].selector == "#pdf"


class TestEnsureLogin:
    """``ensure_login``: use the saved session, and sign in again when it is dead."""

    def test_a_live_session_makes_the_step_a_no_op(self, tmp_path):
        from tests.fakes import PageScript

        script = PageScript(page_width=1200, page_height=900)
        script.has_login_form = True
        engine, _settings, folder, calls = make_engine(tmp_path, script=script)
        journeys = journey.parse_journeys(
            {
                "app": {
                    "url": "https://example.com/",
                    "steps": [
                        {"action": "ensure_login", "selector": ".dashboard"},
                        {"action": "capture", "name": "dashboard"},
                    ],
                }
            }
        )

        # The fake reports every selector as visible when has_login_form is set, so
        # the session is "alive": nothing may be filled in.
        report = journey.JourneyRunner(engine).run(journeys)

        assert report.ok, report.summary()
        assert calls.fills == [], "no credentials typed when the session is alive"
        assert "still signed in" in report.journeys[0].steps[0].detail

    def test_a_dead_session_signs_in_again(self, tmp_path):
        from tests.fakes import PageScript

        # A page with a login form, and a dashboard that only exists once the form
        # has been submitted: exactly what an expired session looks like.
        script = PageScript(
            page_width=1200,
            page_height=900,
            has_login_form=True,
            login_gated_selectors="#app-shell",
        )
        engine, settings, _folder, calls = make_engine(
            tmp_path,
            script=script,
            auth_enabled=True,
            username="demo@example.com",
            password="hunter2",
        )

        journeys = journey.parse_journeys(
            {
                "app": {
                    "url": "https://example.com/",
                    "steps": [
                        {"action": "ensure_login", "selector": "#app-shell"},
                        {"action": "capture", "name": "shell"},
                    ],
                }
            }
        )

        report = journey.JourneyRunner(engine).run(journeys)

        assert report.ok, [step.message for step in report.journeys[0].steps]
        assert [value for _selector, value in calls.fills] == ["demo@example.com", "hunter2"]
        assert settings.auth_enabled is True
        assert "signed in again" in report.journeys[0].steps[0].detail
        assert (Path(settings.output_dir) / "journeys" / "app" / "01-shell.png").exists()

    def test_a_dead_session_without_credentials_explains_itself(self, tmp_path):
        from tests.fakes import PageScript

        script = PageScript(login_gated_selectors=".dashboard")
        engine, _settings, _folder, calls = make_engine(tmp_path, script=script)
        journeys = journey.parse_journeys(
            {
                "app": {
                    "url": "https://example.com/",
                    "steps": [
                        {"action": "ensure_login", "selector": ".dashboard"},
                        {"action": "capture", "name": "dashboard"},
                    ],
                }
            }
        )

        report = journey.JourneyRunner(engine).run(journeys)

        assert report.ok is False
        assert "no login is configured" in report.journeys[0].steps[0].message
        assert calls.fills == []

    def test_the_step_line_round_trips(self):
        spec = journey.journey_from_text(
            "app", "https://example.com", "ensure_login .dashboard optional timeout=20000"
        )
        step = spec.steps[0]
        assert (step.action, step.selector, step.optional, step.timeout) == (
            "ensure_login",
            ".dashboard",
            True,
            20000,
        )
        assert journey.step_line(step.to_dict()) == (
            "ensure_login .dashboard optional timeout=20000"
        )


class TestRecipes:
    """The ready-made click-paths: they have to be valid, and they have to run."""

    def test_every_recipe_is_runnable_text(self):
        assert journey.RECIPE_NAMES == (
            "shop",
            "login-dashboard",
            "tabs",
            "wizard",
            "sso-login",
            "pricing",
        )
        for recipe in journey.RECIPES:
            spec = journey.journey_from_text(recipe.name, "https://example.com", recipe.steps)
            assert spec.captures(), f"{recipe.name} captures nothing"
            assert all(step.action for step in spec.steps)
            assert recipe.title and recipe.note

    def test_a_recipe_can_be_found_by_name_or_prefix(self):
        assert journey.recipe_for("tabs").name == "tabs"
        assert journey.recipe_for("TABS").name == "tabs"
        assert journey.recipe_for("login").name == "login-dashboard"
        with pytest.raises(journey.JourneyError, match="unknown recipe 'nope'"):
            journey.recipe_for("nope")

    def test_the_listing_names_every_recipe(self):
        listing = journey.recipes_text()
        for recipe in journey.RECIPES:
            assert recipe.name in listing
        assert "Shop: landing, products, filters" in listing

    def test_the_shop_recipe_photographs_the_fake_shop(self, shop):
        engine, _settings, folder, _calls = shop
        recipe = journey.recipe_for("shop")
        spec = journey.journey_from_text(recipe.name, "https://example.com", recipe.steps)
        report = journey.JourneyRunner(engine).run([spec])
        # landing -> products -> the filters panel -> the sorted list. The login
        # page has no "Products" button and the shop has no other pages, so the
        # optionals do their job and nothing fails.
        assert report.captures == 4
        assert report.failures() == []
        saved = sorted(item.name for item in (folder / "journeys" / "shop").iterdir())
        assert saved == [
            "01-landing.png",
            "02-products.png",
            "03-filters-open.png",
            "04-sorted.png",
        ]

    def test_the_login_recipe_types_the_environment_values(self, shop):
        engine, _settings, folder, _calls = shop
        spec = journey.journey_from_text(
            "login",
            "https://example.com/login",
            'fill #email = ${TEST_EMAIL:-me@example.com}\nclick "Forgot password"\ncapture forgot',
        )
        report = journey.JourneyRunner(engine).run([spec])
        assert report.captures == 1
        assert (folder / "journeys" / "login" / "01-forgot.png").exists()


class TestCustomRecipes:
    """Recipes of the user's own: written as plain text, read from one folder."""

    def test_a_text_file_becomes_a_recipe(self, tmp_path, monkeypatch):
        monkeypatch.setenv(journey.RECIPES_ENV, str(tmp_path))
        (tmp_path / "checkout.txt").write_text(
            "# Title: The checkout funnel\n"
            "# Note: what sales asks for on Mondays\n"
            'click "Cart"\n'
            "capture cart\n",
            encoding="utf-8",
        )

        found = journey.load_custom_recipes()

        assert [recipe.name for recipe in found] == ["checkout"]
        assert found[0].title == "The checkout funnel"
        assert found[0].note == "what sales asks for on Mondays"
        spec = journey.journey_from_text("checkout", "https://example.com", found[0].steps)
        assert [step.action for step in spec.steps] == ["click", "capture"]

    def test_your_own_recipe_is_listed_and_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv(journey.RECIPES_ENV, str(tmp_path))
        (tmp_path / "shop.txt").write_text("capture home\n", encoding="utf-8")

        names = [recipe.name for recipe in journey.all_recipes()]

        assert names.count("shop") == 1, "the custom shop must replace the built-in one"
        assert names[0] == "login-dashboard", "built-ins keep coming first"
        assert "Your own recipes live in" in journey.recipes_text()
        assert "(your own, v" in journey.recipes_text()
        assert journey.recipe_for("shop").steps.strip() == "capture home"

    def test_saving_a_recipe_writes_a_file_the_loader_reads(self, tmp_path, monkeypatch):
        monkeypatch.setenv(journey.RECIPES_ENV, str(tmp_path / "recipes"))

        path = journey.save_recipe(
            "monday report",
            'click "Reports"\ncapture reports',
            title="The Monday report",
            note="every week, same clicks",
        )

        assert path == tmp_path / "recipes" / "monday-report.txt"
        body = path.read_text(encoding="utf-8")
        assert body.startswith("# Title: The Monday report\n# Note: every week")
        recipe = journey.recipe_for("monday")
        assert recipe.title == "The Monday report"

    def test_saving_steps_that_do_not_parse_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv(journey.RECIPES_ENV, str(tmp_path))

        with pytest.raises(journey.JourneyError, match="cannot be saved"):
            journey.save_recipe("broken", "dance wildly")

        assert not (tmp_path / "broken.txt").exists(), "nothing half-written on disk"

    def test_a_missing_name_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv(journey.RECIPES_ENV, str(tmp_path))
        with pytest.raises(journey.JourneyError, match="needs a name"):
            journey.save_recipe("   ", "capture home")

    def test_a_broken_file_does_not_hide_the_others(self, tmp_path, monkeypatch):
        monkeypatch.setenv(journey.RECIPES_ENV, str(tmp_path))
        (tmp_path / "good.txt").write_text("capture home\n", encoding="utf-8")
        (tmp_path / "ignored.md").write_text("not a recipe", encoding="utf-8")

        assert [recipe.name for recipe in journey.load_custom_recipes()] == ["good"]

    def test_a_custom_recipe_really_runs(self, shop, tmp_path, monkeypatch):
        engine, _settings, folder, _calls = shop
        monkeypatch.setenv(journey.RECIPES_ENV, str(tmp_path))
        journey.save_recipe(
            "shop-like",
            'click "Products"\ncapture products',
        )

        recipe = journey.recipe_for("shop-like")
        spec = journey.journey_from_text(recipe.name, "https://example.com/", recipe.steps)
        report = journey.JourneyRunner(engine).run([spec])

        assert report.captures == 1
        saved = sorted(item.name for item in (folder / "journeys" / "shop-like").iterdir())
        assert saved == ["01-products.png"]


class TestSharedSession:
    """Cross-site journeys: one browser session, two hosts, one sign-in."""

    def test_share_session_is_read_from_the_toml(self):
        journeys = journey.parse_journeys(
            {
                "idp": {
                    "url": "https://sso.example.org/login",
                    "share_session": True,
                    "steps": [{"action": "capture", "name": "login"}],
                },
                "app": {
                    "url": "https://app.example.com/",
                    "steps": [{"action": "capture", "name": "home"}],
                },
            }
        )
        assert journeys[0].share_session is True
        assert journeys[1].share_session is False
        assert journeys[0].to_dict()["share_session"] is True

    def test_journeys_that_share_a_session_share_one_context(self, shop):
        engine, _settings, _folder, calls = shop
        journeys = journey.parse_journeys(
            {
                "idp": {
                    "url": "https://sso.example.org/login",
                    "share_session": True,
                    "steps": [{"action": "capture", "name": "login"}],
                },
                "app": {
                    "url": "https://example.com/",
                    "share_session": True,
                    "steps": [{"action": "capture", "name": "home"}],
                },
                "solo": {
                    "url": "https://example.com/",
                    "steps": [{"action": "capture", "name": "alone"}],
                },
            }
        )

        report = journey.JourneyRunner(engine).run(journeys)

        assert report.ok
        assert len(calls.contexts) == 2, "one shared context plus one of its own"
        assert calls.closed_contexts == 2, "the runner closes both when it is done"
        assert len(report.journeys) == 3

    def test_force_shared_session_puts_every_journey_in_one_context(self, shop):
        engine, _settings, _folder, calls = shop
        journeys = journey.parse_journeys(
            {
                "one": {
                    "url": "https://example.com/",
                    "steps": [{"action": "capture", "name": "home"}],
                },
                "two": {
                    "url": "https://example.com/",
                    "steps": [{"action": "capture", "name": "other"}],
                },
            }
        )

        report = journey.JourneyRunner(engine, force_shared_session=True).run(journeys)

        assert report.ok
        assert len(calls.contexts) == 1, "both journeys rode in the same context"
        assert calls.closed_contexts == 1

    def test_the_shared_session_is_saved_when_storage_state_is_configured(self, tmp_path):
        state = tmp_path / "session.json"
        engine, _settings, _folder, calls = make_engine(tmp_path, storage_state_path=str(state))
        journeys = journey.parse_journeys(
            {
                "idp": {
                    "url": "https://example.com/",
                    "share_session": True,
                    "steps": [{"action": "capture", "name": "login"}],
                }
            }
        )

        report = journey.JourneyRunner(engine).run(journeys)

        assert report.ok
        assert calls.saved_states == [str(state)]
        assert state.exists(), "the next run can start signed in"

    def test_without_a_storage_state_nothing_is_written(self, shop):
        engine, _settings, _folder, calls = shop
        journeys = journey.parse_journeys(
            {
                "idp": {
                    "url": "https://example.com/",
                    "share_session": True,
                    "steps": [{"action": "capture", "name": "login"}],
                }
            }
        )

        journey.JourneyRunner(engine).run(journeys)

        assert calls.saved_states == [], "no cookie jar unless one was asked for"

    def test_moving_to_another_host_is_logged(self, tmp_path):
        lines: list[str] = []
        site = SiteScript.from_site(shop_site())
        script = PageScript(page_width=1200, page_height=900, site=site)
        engine, _settings, _folder, _calls = make_engine(tmp_path, script=script)
        engine.log = lambda level, message: lines.append(f"{level}: {message}")
        journeys = journey.parse_journeys(
            {
                "sso": {
                    "url": "https://example.com/",
                    "steps": [
                        {"action": "goto", "url": "https://sso.example.org/login"},
                        {"action": "capture", "name": "idp"},
                    ],
                }
            }
        )

        journey.JourneyRunner(engine).run(journeys)

        assert any("cross-site step" in line for line in lines), lines

    def test_the_sso_recipe_is_a_cross_site_journey(self):
        recipe = journey.recipe_for("sso-login")
        spec = journey.journey_from_text(recipe.name, "https://sso.example.org/login", recipe.steps)

        actions = [step.action for step in spec.steps]
        assert actions.count("goto") == 1
        target = next(step for step in spec.steps if step.action == "goto")
        assert target.url.startswith("https://app.example.com")
        assert len(spec.captures()) == 3


class TestRunning:
    """The runner: it really clicks, and it really photographs each screen."""

    def test_every_screen_is_saved_with_the_step_order(self, shop):
        engine, _settings, output_dir, calls = shop
        journeys = journey.parse_journeys(
            {
                "shop": {
                    "url": "https://example.com/",
                    "steps": [
                        {"action": "capture", "name": "landing"},
                        {"action": "click", "text": "Products"},
                        {"action": "capture", "name": "products"},
                        {"action": "click", "text": "Filters"},
                        {"action": "capture", "name": "filters-open"},
                    ],
                }
            }
        )
        report = journey.JourneyRunner(engine).run(journeys)
        assert report.ok
        assert report.captures == 3
        files = sorted(path.name for path in (output_dir / "journeys" / "shop").iterdir())
        assert files == ["01-landing.png", "02-products.png", "03-filters-open.png"]
        assert [action[0] for action in calls.actions] == ["click", "click"]

    def test_the_clicked_page_is_what_gets_photographed(self, shop):
        engine, _settings, output_dir, _calls = shop
        journeys = journey.parse_journeys(
            {
                "shop": {
                    "url": "https://example.com/",
                    "steps": [
                        {"action": "click", "text": "Products"},
                        {"action": "capture", "name": "products"},
                    ],
                }
            }
        )
        report = journey.JourneyRunner(engine).run(journeys)
        assert report.ok
        assert report.journeys[0].steps[1].url == "https://example.com/products"

    def test_a_journey_reaches_two_clicks_deep(self, shop):
        engine, _settings, _output, _calls = shop
        journeys = journey.parse_journeys(
            {
                "deep": {
                    "url": "https://example.com/",
                    "steps": [
                        {"action": "click", "text": "Sign in"},
                        {"action": "click", "text": "Forgot password"},
                        {"action": "capture", "name": "reset"},
                    ],
                }
            }
        )
        report = journey.JourneyRunner(engine).run(journeys)
        assert report.ok
        assert report.journeys[0].steps[-1].url == "https://example.com/reset"

    def test_a_step_can_fill_and_press(self, shop):
        engine, _settings, _output, calls = shop
        journeys = journey.parse_journeys(
            {
                "search": {
                    "url": "https://example.com/",
                    "steps": [
                        {"action": "fill", "selector": "#q", "value": "shoes"},
                        {"action": "press", "selector": "#q", "key": "Enter"},
                        {"action": "capture", "name": "results"},
                    ],
                }
            }
        )
        report = journey.JourneyRunner(engine).run(journeys)
        assert report.ok
        assert ("fill", "#q", "shoes") in calls.actions
        assert ("press", "#q", "Enter") in calls.actions

    def test_wait_for_and_wait_and_scroll_and_reload(self, shop):
        engine, _settings, _output, calls = shop
        journeys = journey.parse_journeys(
            {
                "chatty": {
                    "url": "https://example.com/",
                    "steps": [
                        {"action": "wait", "ms": 1},
                        {"action": "wait_for", "selector": 'button:has-text("Products")'},
                        {"action": "scroll"},
                        {"action": "reload"},
                        {"action": "capture", "name": "after"},
                    ],
                }
            }
        )
        report = journey.JourneyRunner(engine).run(journeys)
        assert report.ok
        kinds = [action[0] for action in calls.actions]
        assert "wait_for" in kinds and "reload" in kinds

    def test_back_returns_to_the_previous_screen(self, shop):
        engine, _settings, _output, _calls = shop
        journeys = journey.parse_journeys(
            {
                "there-and-back": {
                    "url": "https://example.com/",
                    "steps": [
                        {"action": "click", "text": "Products"},
                        {"action": "back"},
                        {"action": "capture", "name": "home-again"},
                    ],
                }
            }
        )
        report = journey.JourneyRunner(engine).run(journeys)
        assert report.ok
        assert report.journeys[0].steps[-1].url.endswith("example.com/")

    def test_a_missing_button_fails_the_journey_with_a_reason(self, shop):
        engine, _settings, _output, _calls = shop
        journeys = journey.parse_journeys(
            {
                "shop": {
                    "url": "https://example.com/",
                    "steps": [
                        {"action": "click", "text": "Nothing like this"},
                        {"action": "capture", "name": "never"},
                    ],
                }
            }
        )
        report = journey.JourneyRunner(engine).run(journeys)
        assert report.ok is False
        assert report.journeys[0].ok is False
        assert "step 1 failed" in report.journeys[0].message
        assert "nothing matching" in report.journeys[0].steps[0].message
        assert report.captures == 0  # the journey stopped at the failure

    def test_an_optional_step_is_skipped_not_failed(self, shop):
        engine, _settings, output_dir, _calls = shop
        journeys = journey.parse_journeys(
            {
                "shop": {
                    "url": "https://example.com/",
                    "steps": [
                        {"action": "click", "text": "Accept cookies", "optional": True},
                        {"action": "capture", "name": "landing"},
                    ],
                }
            }
        )
        report = journey.JourneyRunner(engine).run(journeys)
        assert report.ok is True
        assert report.journeys[0].steps[0].skipped is True
        assert "skipped" in report.journeys[0].steps[0].message
        # It is the second *step* but the first *capture*: files are numbered by
        # capture order, which is what a folder listing should read like.
        assert (output_dir / "journeys" / "shop" / "01-landing.png").exists()

    def test_one_broken_journey_does_not_stop_the_others(self, shop):
        engine, _settings, output_dir, _calls = shop
        journeys = journey.parse_journeys(
            {
                "broken": {
                    "url": "https://example.com/",
                    "steps": [
                        {"action": "click", "text": "Nowhere"},
                        {"action": "capture", "name": "never"},
                    ],
                },
                "fine": {
                    "url": "https://example.com/",
                    "steps": [{"action": "capture", "name": "landing"}],
                },
            }
        )
        report = journey.JourneyRunner(engine).run(journeys)
        assert report.ok is False
        assert [run.ok for run in report.journeys] == [False, True]
        assert (output_dir / "journeys" / "fine" / "01-landing.png").exists()

    def test_a_journey_that_cannot_open_is_reported(self, tmp_path):
        broken = PageScript(
            page_width=800, page_height=600, goto_error=RuntimeError("net::ERR_NAME_NOT_RESOLVED")
        )
        engine, _settings, _output, _calls = make_engine(tmp_path, broken)
        journeys = journey.parse_journeys(
            {"dead": {"url": "https://nothing.example/", "steps": [{"action": "capture"}]}}
        )
        report = journey.JourneyRunner(engine).run(journeys)
        assert report.ok is False
        assert report.journeys[0].steps == []  # never got past the navigation
        assert "could not open" in report.journeys[0].message

    def test_a_disabled_journey_is_left_alone(self, shop):
        engine, _settings, _output, _calls = shop
        journeys = journey.parse_journeys(
            {
                "off": {
                    "url": "https://example.com/",
                    "enabled": False,
                    "steps": [{"action": "capture", "name": "landing"}],
                }
            }
        )
        report = journey.JourneyRunner(engine).run(journeys)
        assert report.journeys == []
        assert report.ok is False  # nothing ran

    def test_an_empty_journey_list_is_not_an_error_to_run(self, shop):
        engine, _settings, _output, _calls = shop
        report = journey.JourneyRunner(engine).run([])
        assert report.journeys == []

    def test_a_stop_request_ends_the_journey(self, shop):
        engine, _settings, output_dir, _calls = shop
        journeys = journey.parse_journeys(
            {
                "long": {
                    "url": "https://example.com/",
                    "steps": [
                        {"action": "capture", "name": "one"},
                        {"action": "click", "text": "Products"},
                        {"action": "capture", "name": "two"},
                    ],
                }
            }
        )

        runner = journey.JourneyRunner(engine)
        original = runner.run_step

        def stopping_step(page, one_journey, step, order):
            engine.request_stop()
            return original(page, one_journey, step, order)

        runner.run_step = stopping_step  # type: ignore[method-assign]
        report = runner.run(journeys)
        assert (output_dir / "journeys" / "long" / "01-one.png").exists()
        assert report.journeys[0].steps  # the first step still ran

    def test_the_report_is_written_next_to_the_captures(self, shop):
        engine, settings, output_dir, _calls = shop
        journeys = journey.parse_journeys(
            {"shop": {"url": "https://example.com/", "steps": [{"action": "capture", "name": "x"}]}}
        )
        report = journey.JourneyRunner(engine).run(journeys)
        reports = sorted(output_dir.glob("journey-report-*.json"))
        assert len(reports) == 1
        data = json.loads(reports[0].read_text(encoding="utf-8"))
        assert data["ok"] is True
        assert data["captures"] == 1
        assert data["journeys"][0]["name"] == "shop"
        assert data["journeys"][0]["steps"][0]["action"] == "capture"
        assert report.output_dir == str(output_dir)

    def test_the_summary_says_what_happened(self, shop):
        engine, _settings, _output, _calls = shop
        journeys = journey.parse_journeys(
            {"shop": {"url": "https://example.com/", "steps": [{"action": "capture", "name": "x"}]}}
        )
        text = journey.JourneyRunner(engine).run(journeys).summary()
        assert "shop: ok" in text
        assert "1 capture(s)" in text

    def test_the_scale_and_format_travel_into_the_capture(self, tmp_path):
        engine, settings, _output, calls = make_engine(
            tmp_path, image_format="jpeg", device_scale_factor=1.0
        )
        journeys = journey.parse_journeys(
            {"shop": {"url": "https://example.com/", "steps": [{"action": "capture", "name": "x"}]}}
        )
        report = journey.JourneyRunner(engine).run(journeys)
        assert report.ok
        assert calls.screenshots[-1]["type"] == "jpeg"
        assert (Path(settings.output_dir) / "journeys" / "shop" / "01-x.jpg").exists()
