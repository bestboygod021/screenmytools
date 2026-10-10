"""Headless command-line front end.

The CLI reuses the exact same Qt-free :class:`~app.core.engine.CaptureEngine`
as the GUI, so automation and CI get identical behaviour (waits, retries,
parallelism, change detection, reports) without a display.

Run it with::

    python -m app.cli --urls list.txt --out ./shots
    python -m app.cli https://a.com https://b.com --out ./shots --concurrency 4

Exit codes (for CI):
    0  every URL succeeded (or was unchanged)
    1  at least one URL failed
    2  bad arguments / configuration
    3  the Playwright browser is not installed
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from app.core.api import DEFAULT_HOST as DEFAULT_API_HOST
from app.core.api import DEFAULT_PORT as DEFAULT_API_PORT
from app.core.engine import (
    BrowserNotInstalledError,
    CaptureEngine,
    CaptureResult,
    CaptureStatus,
    LogLevel,
)
from app.core.settings import AUTH_MODES, SUPPORTED_BROWSERS, CaptureSettings, SettingsError
from app.core.url_utils import split_url_lines, validate_url
from app.version import APP_DISPLAY_NAME, __version__


def _add_capture_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("urls", nargs="*", help="URLs to capture (or use --urls FILE).")
    parser.add_argument("--urls-file", metavar="FILE", help="Read URLs from a file, one per line.")
    parser.add_argument("--out", "-o", required=True, metavar="DIR", help="Destination folder.")

    parser.add_argument("--headed", action="store_true", help="Show the browser window.")
    parser.add_argument("--browser", choices=list(SUPPORTED_BROWSERS), default="chromium")
    parser.add_argument("--concurrency", type=int, default=1, help="Parallel workers (1-8).")
    parser.add_argument("--retries", type=int, default=1, help="Extra attempts per URL.")
    parser.add_argument("--viewport", default="1920x1080", help="Viewport as WxH, e.g. 1440x900.")
    parser.add_argument("--scale", type=float, default=2.0, help="Device scale factor (0.5-4).")
    parser.add_argument("--format", choices=["png", "jpeg"], default="png")
    parser.add_argument("--jpeg-quality", type=int, default=92)
    parser.add_argument("--timeout", type=int, default=60, help="Navigation timeout in seconds.")
    parser.add_argument("--settle", type=int, default=1500, help="Extra wait after load (ms).")
    parser.add_argument("--no-lazy-scroll", action="store_true", help="Skip lazy-load scrolling.")
    parser.add_argument(
        "--hide-selector",
        dest="hide_selectors",
        action="append",
        default=[],
        metavar="CSS",
        help="Hide these elements on every shot (repeatable): a cookie banner, an ad "
        "slot, a clock. Hiding them keeps them out of the captures and out of the "
        "diffs.",
    )

    parser.add_argument("--auth-mode", choices=list(AUTH_MODES), default="form")
    parser.add_argument("--username", default="")
    parser.add_argument("--password", default="")

    parser.add_argument(
        "--change-detection", action="store_true", help="Compare to previous capture."
    )
    parser.add_argument("--threshold", type=float, default=0.05, help="Change threshold (0-1).")

    parser.add_argument(
        "--mute-urls",
        default=os.environ.get("CAPTURE_MUTE_URLS", ""),
        metavar="TEXT",
        help="Never alert about URLs containing these comma-separated fragments.",
    )
    parser.add_argument(
        "--quiet-hours",
        default=os.environ.get("CAPTURE_QUIET_HOURS", ""),
        metavar="HH:MM-HH:MM",
        help="Hold alerts inside this window and send them together after it.",
    )
    parser.add_argument(
        "--route-urls",
        default=os.environ.get("CAPTURE_ROUTE_URLS", ""),
        metavar="TEXT",
        help="Per-URL webhook routing, e.g. "
        "'staging.example.com=https://hooks/x; *=https://hooks/all'.",
    )
    parser.add_argument(
        "--quiet-urls",
        default=os.environ.get("CAPTURE_QUIET_URLS", ""),
        metavar="TEXT",
        help="Per-URL quiet hours, e.g. 'staging.example.com=22:00-07:00; news.example.com='.",
    )
    parser.add_argument(
        "--channels",
        default=os.environ.get("CAPTURE_CHANNELS", ""),
        metavar="FILE",
        help="A channels TOML file: one table per destination, each with its own "
        "url/to, match, quiet, mute and min_diff. Replaces the routing/quiet/mute "
        "settings; run 'fullpage-capture channels --file FILE' to check it first.",
    )
    parser.add_argument(
        "--watchdog-max-age",
        type=int,
        default=0,
        metavar="MINUTES",
        help="Warn at the end of a run when no capture has succeeded for this long (0 = off).",
    )
    parser.add_argument(
        "--site-caps",
        default=os.environ.get("CAPTURE_SITE_CAPS", ""),
        metavar="HOST=MB,...",
        help="A size budget per site, e.g. 'news.example.com=500,*=1000' (MB; '*' covers "
        "the rest). After each run the oldest captures of a site over its own budget are "
        "deleted - the chatty host is trimmed instead of the whole folder.",
    )
    parser.add_argument("--no-report", action="store_true", help="Skip CSV/JSON reports.")
    parser.add_argument("--json", action="store_true", help="Print a JSON summary to stdout.")
    parser.add_argument("--quiet", "-q", action="store_true", help="Only errors + summary.")
    parser.add_argument("--version", action="version", version=APP_DISPLAY_NAME)


def build_secrets_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fullpage-capture secrets",
        description="Keep the SMTP password in the system keychain instead of the settings.",
    )
    parser.add_argument(
        "action",
        choices=("status", "set", "show", "forget"),
        help="status: is a keychain here? set: store the password from --stdin/--value; "
        "show: print whether one is stored; forget: delete it.",
    )
    parser.add_argument(
        "--value",
        default="",
        metavar="TEXT",
        help="The password to store with 'set' (prefer --stdin or the prompt on a shared box).",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read the password from the first line of stdin (for scripts and CI).",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    return parser


def _journey_settings(args: argparse.Namespace) -> CaptureSettings:
    """The browser/session settings a journey or crawl needs (no output naming)."""
    width, _, height = args.viewport.partition("x")
    settings = CaptureSettings(output_dir=args.out)
    settings.viewport_width = int(width)
    settings.viewport_height = int(height or 1080)
    settings.headless = not args.headed
    settings.browser = args.browser
    settings.device_scale_factor = args.scale
    settings.image_format = args.format
    settings.jpeg_quality = args.jpeg_quality
    settings.navigation_timeout_ms = args.timeout * 1000
    settings.settle_delay_ms = getattr(args, "settle", 0)
    settings.scroll_to_load_lazy_content = not getattr(args, "no_lazy_scroll", False)
    settings.hide_selectors = ",".join(getattr(args, "hide_selectors", []) or [])
    settings.write_report = True
    settings.write_log_file = False
    settings.storage_state_path = getattr(args, "storage_state", "")
    settings.proxy_server = getattr(args, "proxy", "")
    settings.user_agent = getattr(args, "user_agent", "")
    settings.ignore_https_errors = bool(getattr(args, "ignore_https_errors", False))
    if getattr(args, "username", ""):
        settings.auth_enabled = True
        settings.auth_mode = args.auth_mode
        settings.username = args.username
        settings.password = args.password
    return settings


def _text_list(value: str) -> list[str]:
    """``"a, b;c"`` -> ``["a", "b", "c"]`` (the CLI's usual comma list)."""
    parts: list[str] = []
    for chunk in str(value or "").replace(";", ",").split(","):
        item = chunk.strip()
        if item:
            parts.append(item)
    return parts


def _open_engine(settings: CaptureSettings, engine_factory, quiet: bool, prefix: str):
    """A started engine, or ``(None, exit code)`` when the browser is missing."""
    factory = engine_factory or CaptureEngine

    def on_log(level: LogLevel, message: str) -> None:
        if quiet and level not in (LogLevel.ERROR, LogLevel.WARNING):
            return
        print(f"[{prefix}][{level.value.upper():<7}] {message}")

    try:
        return factory(settings, log=on_log), 0
    except BrowserNotInstalledError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return None, 3


def _run_journey(args: argparse.Namespace, engine_factory=None) -> int:
    """``journey``: click through the site and photograph each screen."""
    from app.core import journey as journey_module
from app.core import onboarding

    if args.sample:
        print(journey_module.sample_journey())
        return 0

    recipes_folder = str(getattr(args, "recipes_dir", "") or "").strip() or None
    try:
        overrides = journey_module.parse_sets(getattr(args, "sets", []) or [])
    except journey_module.JourneyError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    run_env = {**os.environ, **overrides} if overrides else None

    if args.recipes:
        print(journey_module.recipes_text(recipes_folder))
        return 0

    if args.save_recipe:
        body = str(getattr(args, "steps", "") or "")
        if not body and getattr(args, "steps_file", ""):
            try:
                body = Path(args.steps_file).read_text(encoding="utf-8")
            except OSError as exc:
                print(f"[ERROR] Cannot read {args.steps_file}: {exc}", file=sys.stderr)
                return 2
        if not body.strip():
            print(
                "[ERROR] --save-recipe needs the steps: pass --steps '…' or --steps-file file.txt.",
                file=sys.stderr,
            )
            return 2
        try:
            saved = journey_module.save_recipe(
                args.save_recipe,
                body.replace(" ; ", "\n"),
                recipes_folder,
                title=args.recipe_title,
                note=args.recipe_note,
            )
        except journey_module.JourneyError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 2
        print(f"Saved the '{args.save_recipe}' recipe to {saved}")
        print(f"Run it with: python -m app.cli journey --recipe {args.save_recipe} --url URL")
        return 0

    wanted: list[journey_module.Journey] = []
    step_text = str(getattr(args, "steps", "") or "")
    if getattr(args, "recipe", ""):
        try:
            chosen = journey_module.recipe_for(args.recipe, recipes_folder)
        except journey_module.JourneyError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 2
        step_text = chosen.steps
        if not getattr(args, "name", "") or args.name == "journey":
            args.name = chosen.name
    if not step_text and getattr(args, "steps_file", ""):
        try:
            step_text = Path(args.steps_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"[ERROR] Cannot read {args.steps_file}: {exc}", file=sys.stderr)
            return 2
    if step_text.strip():
        start_url = str(getattr(args, "url", "") or "").strip()
        if not start_url:
            start_url = next((item for item in args.urls if "://" in item), "")
        if not start_url:
            print("[ERROR] --steps needs a page to start on: pass --url.", file=sys.stderr)
            return 2
        try:
            wanted.append(
                journey_module.journey_from_text(
                    getattr(args, "name", "journey") or "journey",
                    start_url,
                    step_text.replace(" ; ", "\n"),
                    run_env,
                )
            )
        except journey_module.JourneyError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 2

    origin = Path(args.file).parent if args.file else Path()
    locations: list[Path] = []
    if args.file:
        locations.append(Path(args.file))
    for extra in args.urls:
        candidate = Path(extra)
        if candidate.is_file():
            locations.append(candidate)
        elif (origin / extra).is_file():
            locations.append(origin / extra)
    if not locations and not wanted:
        print(
            "[ERROR] Nothing to run: pass --file journey.toml, --steps-file steps.txt, "
            "--steps 'click \"Sign in\" ; capture dashboard' (or a journey file as an "
            "argument). 'journey --sample' prints an example.",
            file=sys.stderr,
        )
        return 2

    for location in locations:
        try:
            wanted.extend(journey_module.load_journeys(location, run_env))
        except journey_module.JourneyError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 2
    if not wanted:
        print("[ERROR] No journeys in the given file(s).", file=sys.stderr)
        return 2

    if args.list:
        for item in wanted:
            state = "enabled" if item.enabled else "disabled"
            print(f"{item.name} ({state}) - {item.url} - {len(item.steps)} step(s)")
            for index, step in enumerate(item.steps, start=1):
                print(f"  {index:2d}. {step.describe()}")
        print(
            f"{len(wanted)} journey(s), {sum(len(item.captures()) for item in wanted)} capture(s)."
        )
        return 0

    try:
        settings = _journey_settings(args)
        settings.validate()
    except (SettingsError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    engine, code = _open_engine(settings, engine_factory, args.quiet, "journey")
    if engine is None:
        return code

    try:
        report = journey_module.JourneyRunner(
            engine, force_shared_session=bool(getattr(args, "share_session", False))
        ).run(wanted)
    except BrowserNotInstalledError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 3
    if args.json:
        import json

        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print()
        print(report.summary())
        if report.output_dir:
            print(f"Screenshots in {Path(report.output_dir) / 'journeys'}")
    return 0 if report.ok else 1


def _run_crawl(args: argparse.Namespace, engine_factory=None) -> int:
    """``crawl``: find the clickable things, click them, capture every screen."""
    from app.core import crawler as crawler_module
from app.core.adaptive_budget import budget_for

    options = crawler_module.CrawlOptions(
        max_depth=max(0, int(args.max_depth)),
        max_states=max(1, int(args.max_states)),
        max_clicks=max(1, int(args.max_clicks)),
        max_seconds=max(0.0, float(args.max_seconds or 0)),
        allow_external=bool(args.allow_external),
        allow_dangerous=bool(args.allow_dangerous),
        ignore=tuple(_text_list(args.ignore)),
        include=tuple(_text_list(args.include)),
        dangerous_words=tuple(_text_list(args.dangerous)),
        selectors=tuple(_text_list(args.click_selectors)) or crawler_module.DEFAULT_SELECTORS,
        delay_ms=max(0, int(args.delay)),
        settle_ms=max(0, int(args.settle or 0)),
    )
    try:
        settings = _journey_settings(args)
        settings.validate()
    except (SettingsError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    engine, code = _open_engine(settings, engine_factory, args.quiet, "crawl")
    if engine is None:
        return code

    walker = crawler_module.Crawler(engine, options)
    if args.list:
        try:
            found = walker.list_clicks(args.url)
        except BrowserNotInstalledError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 3
        if args.json:
            import json

            print(json.dumps(found, indent=2, ensure_ascii=False))
            return 0
        if not found:
            print(f"Nothing clickable found on {args.url}.")
            return 0
        for item in found:
            mark = "click" if item["kept"] else f"skip ({item['reason']})"
            label = item["text"] or item["href"] or item["tag"]
            print(f"  [{mark:>24}] {item['kind']:<7} {label}")
        kept = sum(1 for item in found if item["kept"])
        print(f"{kept} of {len(found)} candidate(s) would be clicked.")
        return 0

    try:
        report = walker.run(args.url)
    except BrowserNotInstalledError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 3
    folder = crawler_module.crawl_folder(report)
    if folder is not None:
        crawler_module.write_crawl_report(report, folder)

    diff = None
    if args.compare:
        diff = _compare_with_previous(report, folder, float(args.changed_threshold or 0))

    if args.json:
        import json

        payload = report.to_dict()
        if diff is not None:
            payload["diff"] = diff.to_dict()
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print()
        print(report.summary())
        if diff is not None:
            print()
            print(diff.summary())
        if folder is not None:
            print(f"Screenshots in {folder}")
    if not report.pages:
        return 1
    if diff is not None and getattr(args, "fail_on_change", False) and diff.any:
        return 1
    return 0 if not report.errors else 1


def crawler_changed_threshold() -> float:
    """The crawl diff's default threshold, straight from the crawler module."""
    from app.core import crawler as crawler_module

    return float(crawler_module.DEFAULT_CHANGED_THRESHOLD)


def _compare_with_previous(report, folder, changed_threshold: float | None = None) -> Any:
    """Diff ``report`` against the newest earlier crawl, and keep the result.

    The comparison is written next to the crawl (``diff.json``) so the app - or a
    later report - can show it without re-running anything.
    """
    from app.core import crawler as crawler_module

    previous_path = crawler_module.find_previous_report(
        report.output_dir, exclude=folder if folder is not None else None
    )
    if previous_path is None:
        diff = crawler_module.CrawlDiff()
        diff.previous = ""
        return diff
    try:
        previous = crawler_module.load_report(previous_path)
    except (OSError, ValueError) as exc:
        diff = crawler_module.CrawlDiff()
        diff.previous = f"unreadable: {exc}"
        return diff
    diff = crawler_module.compare_reports(
        previous,
        report,
        changed_threshold=changed_threshold if changed_threshold else None,
    )
    diff.previous = str(previous_path.parent)
    if folder is not None:
        import json

        try:
            (Path(folder) / "diff.json").write_text(
                json.dumps(diff.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:  # pragma: no cover - the diff is a convenience
            pass
    return diff


def _run_secrets(args: argparse.Namespace) -> int:
    """Status, set, show or forget the SMTP password in the OS secret store."""
    from app.core import secrets

    action = args.action
    if action == "status":
        store = secrets.backend_name()
        payload = {
            "available": secrets.available(),
            "backend": store,
            "stored": bool(secrets.load(secrets.SMTP_PASSWORD)),
            "service": secrets.SERVICE,
        }
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 0
        if not payload["available"]:
            print(
                "No system keychain is reachable, so the SMTP password stays in the "
                "settings/profile file. Install 'keyring', start a desktop session, or "
                "set CAPTURE_SECRETS=memory to try the code path without one."
            )
            return 1
        print(f"Keychain: {store}")
        print(
            "An SMTP password is stored."
            if payload["stored"]
            else "No SMTP password is stored yet ('secrets set')."
        )
        if not payload["stored"]:
            print("Until one is stored, an empty smtp_password field falls back to the file.")
        return 0

    if action == "show":
        stored = bool(secrets.load(secrets.SMTP_PASSWORD))
        if args.json:
            print(
                json.dumps(
                    {"stored": stored, "backend": secrets.backend_name()},
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0
        # Never print the password: a terminal has scrollback and a CI log does not forget.
        print("An SMTP password is stored." if stored else "No SMTP password is stored.")
        return 0 if stored else 1

    if action == "set":
        value = args.value
        if not value and args.stdin:
            value = sys.stdin.readline().rstrip("\n")
        if not value and sys.stdin.isatty():
            import getpass  # noqa: PLC0415 - only needed for the interactive prompt

            value = getpass.getpass("SMTP password: ")
        if not value:
            print(
                "[ERROR] Nothing to store: pass --value, --stdin or type it at the prompt.",
                file=sys.stderr,
            )
            return 2
        if not secrets.available():
            print(
                "No system keychain is reachable, so the password was not stored. "
                "Put it in smtp_password (settings or profile) instead, or install 'keyring'.",
                file=sys.stderr,
            )
            return 2
        if not secrets.store(value):
            print("The keychain refused to store the password.", file=sys.stderr)
            return 2
        if args.json:
            print(
                json.dumps({"stored": True, "backend": secrets.backend_name()}, ensure_ascii=False)
            )
        else:
            print(f"SMTP password stored in {secrets.backend_name()}.")
        return 0

    # forget
    removed = secrets.forget(secrets.SMTP_PASSWORD)
    if args.json:
        print(json.dumps({"removed": removed}, ensure_ascii=False))
    else:
        print("SMTP password removed from the keychain." if removed else "Nothing was stored.")
    return 0


def build_journey_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fullpage-capture journey",
        description="Click through a site (or several) and photograph every screen on the way.",
    )
    parser.add_argument("urls", nargs="*", help="URLs/A journey file to read (or use --file).")
    parser.add_argument(
        "--file",
        "-f",
        metavar="FILE",
        help="A journey TOML file: one table per journey, each with steps. "
        "'journey --sample' prints a commented example.",
    )
    parser.add_argument(
        "--out",
        "-o",
        default="shots",
        metavar="DIR",
        help="Where the screenshots go (default: shots); each journey gets its own folder.",
    )
    parser.add_argument(
        "--recipe",
        metavar="NAME",
        help="Start from a ready-made click-path (shop, login-dashboard, tabs, "
        "wizard, pricing); 'journey --recipes' prints them.",
    )
    parser.add_argument(
        "--recipes",
        action="store_true",
        help="List the ready-made click-paths (and your own) and stop.",
    )
    parser.add_argument(
        "--save-recipe",
        metavar="NAME",
        help="Save the --steps/--steps-file text as a recipe of your own, then stop.",
    )
    parser.add_argument("--recipe-title", default="", help="Title stored with --save-recipe.")
    parser.add_argument(
        "--recipe-note", default="", help="One-line note stored with --save-recipe."
    )
    parser.add_argument(
        "--set",
        dest="sets",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Fill ${KEY} in the steps for this run only (repeatable). The recipe "
        "stays generic, the run does not: --set coupon=SAVE10.",
    )
    parser.add_argument(
        "--recipes-dir",
        default="",
        metavar="DIR",
        help="Read your own recipes from DIR instead of ~/.capture-bot/recipes "
        "(point it at a shared or git-backed folder).",
    )
    parser.add_argument(
        "--share-session",
        action="store_true",
        help="Run every journey in one browser session, so a sign-in on one host "
        "(an SSO provider) still applies on the next. Journeys with "
        "share_session = true do this on their own.",
    )
    parser.add_argument(
        "--steps-file",
        metavar="FILE",
        help="A plain-text file of steps (one per line) instead of a TOML journey - "
        "the same short lines the app's Clicks box accepts.",
    )
    parser.add_argument(
        "--steps",
        metavar="TEXT",
        help="Steps as one line, separated by ' ; ' (e.g. 'click \"Sign in\" ; capture dashboard').",
    )
    parser.add_argument(
        "--url",
        default="",
        metavar="URL",
        help="The page --steps/--steps-file should start on (default: the first url argument).",
    )
    parser.add_argument(
        "--name", default="journey", help="Journey name for --steps-file (default: journey)."
    )
    parser.add_argument(
        "--sample", action="store_true", help="Print a commented example journey file and stop."
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Describe what would be clicked and captured, then stop.",
    )
    parser.add_argument("--headed", action="store_true", help="Show the browser window.")
    parser.add_argument("--browser", choices=list(SUPPORTED_BROWSERS), default="chromium")
    parser.add_argument("--viewport", default="1920x1080", help="Viewport as WxH, e.g. 1440x900.")
    parser.add_argument("--scale", type=float, default=2.0, help="Device scale factor (0.5-4).")
    parser.add_argument("--format", choices=["png", "jpeg"], default="png")
    parser.add_argument("--jpeg-quality", type=int, default=92)
    parser.add_argument("--timeout", type=int, default=60, help="Navigation timeout in seconds.")
    parser.add_argument(
        "--settle",
        type=int,
        default=1500,
        help="Extra wait after a step before the screenshot, in ms (a journey may override it).",
    )
    parser.add_argument("--no-lazy-scroll", action="store_true", help="Skip lazy-load scrolling.")
    parser.add_argument("--username", default="", help="Login username for the whole session.")
    parser.add_argument("--password", default="")
    parser.add_argument("--auth-mode", choices=list(AUTH_MODES), default="form")
    parser.add_argument(
        "--storage-state", default="", metavar="FILE", help="Reuse a saved session."
    )
    parser.add_argument("--proxy", default="", metavar="URL")
    parser.add_argument("--user-agent", default="")
    parser.add_argument("--ignore-https-errors", action="store_true")
    parser.add_argument(
        "--hide-selector",
        dest="hide_selectors",
        action="append",
        default=[],
        metavar="CSS",
        help="Hide these elements on every shot (repeatable): a cookie banner, an ad "
        "slot, a clock. Hiding them keeps them out of the captures and out of the "
        "diffs.",
    )
    parser.add_argument("--quiet", "-q", action="store_true", help="Only warnings + the summary.")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON.")
    return parser


def build_crawl_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fullpage-capture crawl",
        description="Find the buttons, tabs and menus on a site, click them, and photograph "
        "every screen it reaches - the parts a URL list can never see.",
    )
    parser.add_argument("url", help="The page to start from, e.g. https://shop.example.com.")
    parser.add_argument("--out", "-o", default="shots", metavar="DIR", help="Destination folder.")
    parser.add_argument(
        "--max-depth",
        type=int,
        default=2,
        metavar="N",
        help="How many clicks deep to go (1 = only what the landing page shows).",
    )
    parser.add_argument(
        "--max-states", type=int, default=25, metavar="N", help="Stop after N screens."
    )
    parser.add_argument(
        "--max-clicks", type=int, default=40, metavar="N", help="Stop after N distinct clicks."
    )
    parser.add_argument(
        "--max-seconds", type=float, default=0, metavar="S", help="Wall-clock budget (0 = none)."
    )
    parser.add_argument(
        "--hide-selector",
        dest="hide_selectors",
        action="append",
        default=[],
        metavar="CSS",
        help="Hide these elements on every shot (repeatable): a cookie banner, an ad "
        "slot, a clock. Hiding them keeps them out of the captures and out of the "
        "diffs.",
    )
    parser.add_argument(
        "--click",
        dest="click_selectors",
        default="",
        metavar="CSS,...",
        help="What counts as clickable (default: a[href],button,[role=button],[role=tab],"
        "[role=menuitem],summary).",
    )
    parser.add_argument(
        "--ignore",
        default="",
        metavar="TEXT,...",
        help="Never click anything whose label or href contains one of these.",
    )
    parser.add_argument(
        "--include",
        default="",
        metavar="TEXT,...",
        help="Only click labels/hrefs containing one of these.",
    )
    parser.add_argument(
        "--dangerous",
        default="",
        metavar="TEXT,...",
        help="Extra words that mean 'do not click' on top of the built-in list "
        "(log out, delete, buy now, ...).",
    )
    parser.add_argument(
        "--allow-dangerous",
        action="store_true",
        help="Click even the labels that look destructive (use with --list first).",
    )
    parser.add_argument(
        "--allow-external",
        action="store_true",
        help="Follow links to other hosts too (default: stay on the site).",
    )
    parser.add_argument(
        "--delay",
        type=int,
        default=300,
        metavar="MS",
        help="Polite pause after each screenshot, before the next click.",
    )
    parser.add_argument(
        "--settle",
        type=int,
        default=0,
        metavar="MS",
        help="Extra wait after each click (0 = the capture settle delay).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print what would be clicked (and what is skipped, with the reason) and stop.",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="After the walk, list the screens that are new, gone, renamed or changed "
        "compared to the previous crawl in the same --out folder (writes diff.json "
        "next to it). Screens present in both are compared by their pixels.",
    )
    parser.add_argument(
        "--changed-threshold",
        type=float,
        default=crawler_changed_threshold(),
        metavar="RATIO",
        help="How different a screen has to look (0..1, dHash) to count as changed "
        "(default: 0.02; 0 disables the pixel comparison).",
    )
    parser.add_argument(
        "--fail-on-change",
        action="store_true",
        help="Exit 1 when --compare found anything new, gone or changed - for a cron "
        "job that should shout when a site moves.",
    )
    parser.add_argument("--mode", choices=["landing","main","tools","full"], default="main", help="Capture depth level (1-4)")
    parser.add_argument("--voice", action="store_true", help="Enable voice command recording")
    parser.add_argument("--headed", action="store_true", help="Show the browser window.")
    parser.add_argument("--browser", choices=list(SUPPORTED_BROWSERS), default="chromium")
    parser.add_argument("--viewport", default="1920x1080", help="Viewport as WxH, e.g. 1440x900.")
    parser.add_argument("--scale", type=float, default=2.0, help="Device scale factor (0.5-4).")
    parser.add_argument("--format", choices=["png", "jpeg"], default="png")
    parser.add_argument("--jpeg-quality", type=int, default=92)
    parser.add_argument("--timeout", type=int, default=60, help="Navigation timeout in seconds.")
    parser.add_argument(
        "--username", default="", help="Login before crawling (form or basic auth)."
    )
    parser.add_argument("--password", default="")
    parser.add_argument("--auth-mode", choices=list(AUTH_MODES), default="form")
    parser.add_argument(
        "--storage-state", default="", metavar="FILE", help="Reuse a saved session."
    )
    parser.add_argument("--proxy", default="", metavar="URL")
    parser.add_argument("--user-agent", default="")
    parser.add_argument("--ignore-https-errors", action="store_true")
    parser.add_argument("--quiet", "-q", action="store_true", help="Only warnings + the summary.")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON.")
    return parser


def build_channels_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fullpage-capture channels",
        description="Check a named-channels file: who hears about what, and when.",
    )
    parser.add_argument(
        "--file",
        "-f",
        default=os.environ.get("CAPTURE_CHANNELS", ""),
        metavar="FILE",
        help="The TOML file with one [table] per destination.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the parsed channels as JSON (for scripts), not a table.",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Print a commented example file and exit (handy first step).",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Send a real notification through every enabled channel, so the "
        "credentials are known to work before a page changes.",
    )
    parser.add_argument(
        "--heartbeat",
        action="store_true",
        help="Send the proof-of-life note of every channel whose heartbeat is due "
        '(heartbeat = "mon 09:00"); the bot does this after each run too.',
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --heartbeat: say which channels are due and what they would say.",
    )
    parser.add_argument(
        "--dir",
        "-d",
        default=".",
        metavar="FOLDER",
        help="Output folder: the heartbeat state lives in it and the summary counts "
        "its reports (default: the current folder).",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        metavar="N",
        help="With --heartbeat: how many days the summary covers (default 7).",
    )
    return parser


SAMPLE_CHANNELS = """# One table per destination. Save as channels.toml next to the bot, then either
# point the UI field "Channels file" at it or run the bot with --channels.
#
# Every field but the destination is optional:
#   match     substrings of the URL/label this channel wants (default: all)
#   quiet     windows it holds alerts in, e.g. "22:00-07:00, fri18:00-mon09:00"
#   mute      substrings it never wants to hear about
#   min_diff  only changes at least this large (0.02 = 2% of the page)
#   enabled   false keeps the table but stops the delivery

[ops]
kind = "webhook"
url = "https://hooks.example.com/ops"
match = "staging, preview"
quiet = "22:00-07:00"
# heartbeat = "mon 09:00"   # "still here" when a week passes with nothing to say

[shop]
kind = "webhook"
url = "https://hooks.example.com/checkout"
match = "shop.example.com"
min_diff = 0.01

[team]
kind = "email"
to = "team@example.com"
match = "*.example.com"

[pager]
kind = "command"
# Anything curl cannot reach: a PagerDuty/SMS CLI, an internal script. The
# payload arrives as JSON on stdin (and as CAPTURE_BOT_* variables).
exec = ["/usr/local/bin/page-oncall", "--team", "ops"]
timeout = 30
"""


def _run_channels(args: argparse.Namespace) -> int:
    """``channels``: check (or print a sample of) the named-channels file."""
    from app.core import channels

    if args.sample:
        sys.stdout.write(SAMPLE_CHANNELS)
        return 0

    if not str(args.file).strip():
        print(
            "Which channels file? Pass --file channels.toml (or set CAPTURE_CHANNELS). "
            "Use --sample to see what one looks like.",
            file=sys.stderr,
        )
        return 2
    try:
        parsed = channels.load_channels(args.file)
    except channels.ChannelError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(
            json.dumps(
                {
                    "file": str(args.file),
                    "channels": [channel.to_dict() for channel in parsed],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(channels.summary(parsed))

    if args.heartbeat:
        from app.core.settings import CaptureSettings

        settings = CaptureSettings(output_dir=str(args.dir or "."))
        settings.alert_channels = args.file
        try:
            results = channels.run_heartbeats(
                settings, dry_run=args.dry_run, days=max(1, int(args.days))
            )
        except channels.ChannelError as exc:  # pragma: no cover - the file was checked above
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 2
        if not results:
            print("No channel is due for a heartbeat.")
            return 0
        for label, ok in results:
            state = "would send" if args.dry_run else ("sent" if ok else "FAILED")
            print(f"  {label}: {state}")
        if args.dry_run:
            print(f"  note: {channels.heartbeat_summary(str(args.dir or '.'), int(args.days))}")
        return 0 if all(ok for _label, ok in results) else 1

    if not args.test:
        return 0

    from app.core.settings import CaptureSettings

    settings = CaptureSettings()
    settings.alert_channels = args.file
    note = (
        "FullPage Capture Bot: this is a test alert from 'channels --test'. "
        "Nothing has changed - the real ones arrive when a page does."
    )
    results = channels.test_notify(settings, "Capture Bot test", note)
    failures = [label for label, ok in results if not ok]
    for label, ok in results:
        print(f"  {label}: {'sent' if ok else 'FAILED'}")
    if not results:
        print("  No channel wanted the test notification (check 'match' and 'enabled').")
        return 2
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    """The capture parser (also usable as the ``capture`` subcommand)."""
    parser = argparse.ArgumentParser(
        prog="fullpage-capture",
        description="Automated full-page website screenshots (headless).",
    )
    _add_capture_args(parser)
    return parser


def build_schedule_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fullpage-capture schedule",
        description="Run the same capture batch repeatedly on an interval.",
    )
    _add_capture_args(parser)
    parser.add_argument("--runs", type=int, default=1, help="How many times to run (>=1).")
    parser.add_argument("--every", type=int, default=0, help="Seconds to wait between runs.")
    return parser


def build_history_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fullpage-capture history",
        description="Show the per-site capture/change history from the report files.",
    )
    parser.add_argument("--dir", "-d", required=True, help="Folder holding capture-report-*.json.")
    parser.add_argument("--site", default="", help="Filter the timeline to one URL.")
    parser.add_argument(
        "--trend",
        action="store_true",
        help="Print a per-site trend summary (captures/changes/last diff) instead of the timeline.",
    )
    parser.add_argument(
        "--prune-days",
        type=int,
        default=0,
        metavar="N",
        help="Also drop SQLite index rows older than N days (0 = keep everything).",
    )
    parser.add_argument(
        "--prune-screenshots",
        type=int,
        default=0,
        metavar="N",
        help="Delete capture images older than N days (0 = keep everything).",
    )
    parser.add_argument(
        "--prune-screenshots-mb",
        type=int,
        default=0,
        metavar="MB",
        help="Delete the oldest captures until the folder fits under MB (0 = off).",
    )
    parser.add_argument(
        "--prune-history-mb",
        type=int,
        default=0,
        metavar="MB",
        help="Delete the oldest reports/index rows until the history fits under MB (0 = off).",
    )
    parser.add_argument(
        "--site-caps",
        default="",
        metavar="HOST=MB,...",
        help="A size budget per site, e.g. 'news.example.com=500,*=1000' (MB; '*' covers "
        "the rest). The oldest captures of a site over its own budget are deleted after "
        "each run - the chatty host is trimmed, not the whole folder.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --prune-screenshots[-mb]/--prune-history-mb: list what would go and keep it.",
    )
    parser.add_argument(
        "--archive",
        action="store_true",
        help="Keep what would be deleted: with --prune-history-mb (and --older-than) zip it first.",
    )
    parser.add_argument(
        "--older-than",
        type=int,
        default=0,
        metavar="DAYS",
        help="With --archive: archive every run older than DAYS (0 = nothing by age).",
    )
    parser.add_argument(
        "--restore",
        default="",
        metavar="ZIP",
        help="Extract an archive-*.zip back into the history folder and reindex it.",
    )
    parser.add_argument(
        "--top-space",
        action="store_true",
        help="List the sites that cost the most disk (largest first).",
    )
    parser.add_argument(
        "--prune-site",
        default="",
        metavar="SITE",
        help="Delete the captures of one site (host or label fragment); references stay.",
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="Compact the SQLite index and report how much space was reclaimed.",
    )
    parser.add_argument(
        "--reindex",
        action="store_true",
        help="Rebuild the SQLite index from the report files (repairs a lost/corrupt index).",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Compare the SQLite index with the report files; exit 1 when they disagree "
        "(with --json for a machine-readable verdict).",
    )
    parser.add_argument(
        "--export",
        choices=("csv", "json"),
        default="",
        metavar="{csv,json}",
        help="Write the history as a download (the offline twin of /api/export).",
    )
    parser.add_argument(
        "--out",
        "-o",
        default="",
        metavar="FILE",
        help="With --export: where to write it (default: stdout).",
    )
    parser.add_argument(
        "--url",
        default="",
        metavar="TEXT",
        help="With --export: only rows whose URL/label contains TEXT.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="With --export/--top-space: at most N rows (0 = all).",
    )
    parser.add_argument(
        "--offset", type=int, default=0, metavar="N", help="With --export: skip the first N rows."
    )
    parser.add_argument(
        "--compare",
        nargs="+",
        default=[],
        metavar="PERIOD",
        help="Compare two periods of history per site. 'history --compare 30d:today' "
        "compares the last 30 days with the 30 before them; pass two periods "
        "('--compare 30d 60d') to choose both sides yourself. A period is "
        "START:END, and each side accepts today, YYYY-MM-DD, YYYY-MM-DDTHH:MM and "
        "30d/12h/45m ('ago'; the leading '-' also works when quoted as --compare=...).",
    )
    parser.add_argument(
        "--compare-pdf",
        default="",
        metavar="FILE",
        help="With --compare: also write the comparison as a one-page PDF.",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output.")
    return parser


def build_dashboard_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fullpage-capture dashboard",
        description="Write a static, self-contained HTML trend dashboard.",
    )
    parser.add_argument("--dir", "-d", required=True, help="Folder holding capture-report-*.json.")
    parser.add_argument("--out", "-o", default="dashboard.html", help="Output HTML path.")
    parser.add_argument(
        "--baseline-max-age",
        type=int,
        default=0,
        metavar="N",
        help="Mark baselines older than N days as stale (0 = don't check).",
    )
    parser.add_argument(
        "--url",
        default="",
        metavar="TEXT",
        help="Only include sites whose URL contains TEXT (e.g. one host).",
    )
    parser.add_argument(
        "--caps",
        default="",
        metavar="SCREENSHOTS,MB",
        help="Show the configured caps in the storage panel, e.g. --caps 500,50.",
    )
    parser.add_argument(
        "--storage-json",
        default="",
        metavar="FILE",
        help="Also write the storage report (what /api/storage returns) to FILE, "
        "so a cron job can collect one file per machine.",
    )
    parser.add_argument(
        "--storage-series",
        type=int,
        default=0,
        metavar="N",
        help="With --storage-json: also write the newest N recorded storage samples "
        "(0 = the numbers only; /api/storage serves the same series).",
    )
    return parser


def build_baseline_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fullpage-capture baseline",
        description="Pin, clear or list the known-good baseline captures of a folder.",
    )
    parser.add_argument("--dir", "-d", required=True, help="Output folder of the captures.")
    parser.add_argument(
        "--pin", metavar="URL", help="Promote the latest capture of URL to a baseline."
    )
    parser.add_argument("--clear", metavar="URL", help="Remove the pinned baseline of URL.")
    parser.add_argument(
        "--list", action="store_true", help="List each site and whether it has a baseline."
    )
    return parser


def build_digest_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fullpage-capture digest",
        description="Print (or email) a periodic summary of capture activity.",
    )
    parser.add_argument("--dir", "-d", required=True, help="Folder holding capture-report-*.json.")
    parser.add_argument("--days", type=int, default=7, help="Window size in days (default 7).")
    parser.add_argument(
        "--send",
        action="store_true",
        help="Email the digest through SMTP instead of printing it.",
    )
    parser.add_argument(
        "--email-to",
        default=os.environ.get("CAPTURE_DIGEST_TO", ""),
        help="Recipient address (or the CAPTURE_DIGEST_TO environment variable).",
    )
    parser.add_argument("--smtp-host", default=os.environ.get("CAPTURE_SMTP_HOST", ""))
    parser.add_argument(
        "--smtp-port", type=int, default=int(os.environ.get("CAPTURE_SMTP_PORT", "587"))
    )
    parser.add_argument("--smtp-user", default=os.environ.get("CAPTURE_SMTP_USER", ""))
    parser.add_argument("--smtp-password", default=os.environ.get("CAPTURE_SMTP_PASSWORD", ""))
    parser.add_argument(
        "--base",
        default="",
        metavar="DIR",
        help="Also compare against another history folder (e.g. the base branch).",
    )
    parser.add_argument(
        "--profiles",
        action="store_true",
        help="Send one digest per saved profile, using that profile's window and issue.",
    )
    parser.add_argument(
        "--profiles-base",
        default="",
        metavar="DIR",
        help="Override where the saved profiles live (default: the app config folder).",
    )
    parser.add_argument(
        "--attach",
        default="dashboard",
        metavar="KINDS",
        help="What to attach: dashboard, csv, drift, none - comma-separated for several.",
    )
    parser.add_argument(
        "--link-base",
        default=os.environ.get("CAPTURE_LINK_BASE", ""),
        metavar="URL",
        help="Add download links to a running 'serve' instance instead of attaching data.",
    )
    parser.add_argument(
        "--max-attachment-kb",
        type=int,
        default=int(os.environ.get("CAPTURE_DIGEST_MAX_KB", "0")),
        metavar="KB",
        help="Skip the attachment when it is larger than this (0 = no limit).",
    )
    parser.add_argument(
        "--skip-if-unchanged",
        action="store_true",
        help="Send nothing when no capture was recorded since the last digest.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="With --skip-if-unchanged: send anyway (e.g. for a manual re-send).",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output.")
    return parser


def build_metrics_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fullpage-capture metrics",
        description="Print the history health as Prometheus metrics (text exposition format).",
    )
    parser.add_argument("--dir", "-d", required=True, help="Folder holding capture-report-*.json.")
    parser.add_argument(
        "--stale-after",
        type=int,
        default=0,
        metavar="MINUTES",
        help="Report health=stale when nothing was captured for this long (0 = never stale).",
    )
    parser.add_argument(
        "--out",
        "-o",
        default="",
        metavar="FILE",
        help="Write the metrics to this file instead of stdout (node_exporter "
        "textfile collector / systemd timer).",
    )
    parser.add_argument(
        "--write-if-changed",
        action="store_true",
        help="With --out: leave the file untouched when the metrics did not change.",
    )
    parser.add_argument(
        "--caps",
        default=os.environ.get("CAPTURE_CAPS", ""),
        metavar="SCREENSHOTS,MB",
        help="Cap sizes for the storage gauges, e.g. --caps 500,50.",
    )
    parser.add_argument(
        "--site-caps",
        default=os.environ.get("CAPTURE_SITE_CAPS", ""),
        metavar="HOST=MB,...",
        help="Per-site budgets for the storage gauges, e.g. 'news.example.com=500,*=1000' "
        "(each one becomes capture_bot_site_cap_bytes{site=...}).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 when the last *successful* capture is older than --max-age "
        "(or --stale-after, or when there is none) - for a systemd OnFailure= or a cron job.",
    )
    parser.add_argument(
        "--max-age",
        type=int,
        default=0,
        metavar="MINUTES",
        help="With --check: how old the newest successful capture may be (0 = only "
        "require that one exists). --stale-after is the fallback.",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output.")
    return parser


def build_serve_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fullpage-capture serve",
        description="Serve a read-only JSON/HTML API over the capture history.",
    )
    parser.add_argument("--dir", "-d", required=True, help="Folder holding the capture history.")
    parser.add_argument("--host", default=DEFAULT_API_HOST, help="Bind address (default 0.0.0.0).")
    parser.add_argument("--port", "-p", type=int, default=DEFAULT_API_PORT, help="TCP port.")
    parser.add_argument(
        "--token",
        default=os.environ.get("CAPTURE_API_TOKEN", ""),
        help="Require 'Authorization: Bearer <token>' (or CAPTURE_API_TOKEN).",
    )
    parser.add_argument(
        "--cors",
        action="store_true",
        help="Allow cross-origin GETs so a browser dashboard can poll the API.",
    )
    parser.add_argument(
        "--public-dashboard",
        action="store_true",
        help="With --token: leave the read-only HTML page open, keep /api/* guarded.",
    )
    parser.add_argument(
        "--tls-cert",
        default=os.environ.get("CAPTURE_API_TLS_CERT", ""),
        metavar="PATH",
        help="PEM certificate; pair it with --tls-key to serve over HTTPS.",
    )
    parser.add_argument(
        "--tls-key",
        default=os.environ.get("CAPTURE_API_TLS_KEY", ""),
        metavar="PATH",
        help="PEM private key for --tls-cert.",
    )
    parser.add_argument(
        "--rate-limit",
        type=int,
        default=int(os.environ.get("CAPTURE_API_RATE_LIMIT", "0")),
        metavar="N",
        help="Cap each client at N requests per minute (0 = unlimited).",
    )
    parser.add_argument(
        "--stale-after",
        type=int,
        default=0,
        metavar="MINUTES",
        help="Default for /api/status: report health=stale when nothing was captured for this long.",
    )
    parser.add_argument(
        "--caps",
        default="",
        metavar="SCREENSHOTS,MB",
        help="Cap sizes for /metrics and /api/storage, e.g. --caps 500,50 (so the "
        "forecast gauges are real numbers instead of -1).",
    )
    parser.add_argument(
        "--site-caps",
        default="",
        metavar="HOST=MB,...",
        help="Per-site budgets for /metrics, e.g. 'news.example.com=500,*=1000' - each "
        "one becomes capture_bot_site_cap_bytes{site=...}.",
    )
    return parser


def _collect_urls(args: argparse.Namespace) -> list[str]:
    """Gather and normalise the URL list from --urls-file and positionals."""
    raw: list[str] = list(args.urls)
    if args.urls_file:
        path = Path(args.urls_file)
        if not path.exists():
            raise SettingsError(f"URLs file not found: {path}")
        raw.extend(path.read_text(encoding="utf-8").splitlines())

    normalized: list[str] = []
    for line in split_url_lines("\n".join(raw)):
        parsed = validate_url(line)
        if parsed.is_valid:
            normalized.append(parsed.normalized)
        else:
            print(f"[WARNING] Skipping invalid URL '{line}': {parsed.error}", file=sys.stderr)
    return normalized


def _settings_from_args(args: argparse.Namespace) -> CaptureSettings:
    settings = CaptureSettings()
    settings.output_dir = args.out

    width, _, height = args.viewport.partition("x")
    settings.viewport_width = int(width)
    settings.viewport_height = int(height or 1080)

    settings.headless = not args.headed
    settings.browser = args.browser
    settings.max_concurrency = args.concurrency
    settings.retries = args.retries
    settings.device_scale_factor = args.scale
    settings.image_format = args.format
    settings.jpeg_quality = args.jpeg_quality
    settings.navigation_timeout_ms = args.timeout * 1000
    settings.settle_delay_ms = args.settle
    settings.scroll_to_load_lazy_content = not args.no_lazy_scroll
    settings.hide_selectors = ",".join(getattr(args, "hide_selectors", []) or [])

    if args.username:
        settings.auth_enabled = True
        settings.auth_mode = args.auth_mode
        settings.username = args.username
        settings.password = args.password

    settings.alert_quiet_hours = args.quiet_hours
    settings.alert_quiet_urls = args.quiet_urls
    settings.alert_route_urls = args.route_urls
    settings.alert_channels = args.channels
    settings.alert_mute_urls = args.mute_urls
    settings.watchdog_stale_minutes = args.watchdog_max_age
    settings.site_caps = getattr(args, "site_caps", "")
    settings.change_detection_enabled = args.change_detection
    settings.change_threshold = args.threshold
    settings.write_report = not args.no_report
    settings.write_log_file = False  # the console already shows the log
    return settings


def _summary_dict(summary) -> dict:
    return {
        "output_dir": summary.output_dir,
        "total": summary.total,
        "succeeded": summary.succeeded,
        "failed": summary.failed,
        "skipped": summary.skipped,
        "elapsed_ms": summary.elapsed_ms,
        "results": [
            {
                "index": r.index,
                "url": r.url,
                "status": r.status.value,
                "file": r.file_path,
                "unchanged": r.unchanged,
                "diff": r.diff,
                "message": r.message,
                "console_errors": r.console_errors,
                "page_errors": r.page_errors,
                "failed_requests": r.failed_requests,
            }
            for r in summary.results
        ],
    }


def build_walk_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fullpage-capture walk",
        description="Show the newest walk (a journey or a crawl): every screen, the clicks "
        "that reached it, and an optional HTML page with the thumbnails.",
    )
    parser.add_argument(
        "--dir", "-d", default="shots", help="Folder holding the walk reports (default: shots)."
    )
    parser.add_argument(
        "--html",
        metavar="FILE",
        nargs="?",
        const="",
        default=None,
        help="Also write an HTML report with thumbnails (default: walk-report.html in the "
        "walk's own folder).",
    )
    parser.add_argument("--json", action="store_true", help="Print the walk as JSON.")
    return parser


def _run_walk(args: argparse.Namespace) -> int:
    """``walk``: what the last journey/crawl did, and where the screens went."""
    import json

    from app.core import walkview

    walk = walkview.latest_walk(args.dir)
    if not walk.found:
        print(
            f"No walk (journey or crawl) has been run in {args.dir} yet.",
            file=sys.stderr,
        )
        return 1

    written = ""
    if args.html is not None:
        target = args.html or ""
        path = walkview.build_walk_report(args.dir, target or None)
        written = str(path) if path is not None else ""

    if args.json:
        payload = walk.to_dict()
        if written:
            payload["html"] = written
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print(walk.summary())
    if written:
        print(f"Report: {written}")
    return 0


SUBCOMMANDS = (
    "capture",
    "schedule",
    "history",
    "dashboard",
    "baseline",
    "digest",
    "serve",
    "metrics",
    "channels",
    "secrets",
    "journey",
    "crawl",
    "walk",
)


def main(argv: list[str] | None = None, engine_factory=None) -> int:
    """CLI entry point. Returns a process exit code.

    Supports subcommands (``capture``/``schedule``/``history``). For backwards
    compatibility, invoking with bare capture flags (no subcommand) still works.
    """
    raw = list(sys.argv[1:] if argv is None else argv)

    if raw and raw[0] in SUBCOMMANDS:
        command, rest = raw[0], raw[1:]
        if command == "capture":
            return _run_capture(build_parser().parse_args(rest), engine_factory)
        if command == "schedule":
            return _run_schedule(build_schedule_parser().parse_args(rest), engine_factory)
        if command == "history":
            return _run_history(build_history_parser().parse_args(rest))
        if command == "dashboard":
            return _run_dashboard(build_dashboard_parser().parse_args(rest))
        if command == "baseline":
            return _run_baseline(build_baseline_parser().parse_args(rest))
        if command == "digest":
            return _run_digest(build_digest_parser().parse_args(rest))
        if command == "serve":
            return _run_serve(build_serve_parser().parse_args(rest))
        if command == "metrics":
            return _run_metrics(build_metrics_parser().parse_args(rest))
        if command == "channels":
            return _run_channels(build_channels_parser().parse_args(rest))
        if command == "secrets":
            return _run_secrets(build_secrets_parser().parse_args(rest))
        if command == "journey":
            return _run_journey(build_journey_parser().parse_args(rest), engine_factory)
        if command == "crawl":
            return _run_crawl(build_crawl_parser().parse_args(rest), engine_factory)
        if command == "walk":
            return _run_walk(build_walk_parser().parse_args(rest))

    return _run_capture(build_parser().parse_args(raw), engine_factory)


def _run_capture(args: argparse.Namespace, engine_factory=None) -> int:
    try:
        urls = _collect_urls(args)
        settings = _settings_from_args(args)
        settings.validate()
    except SettingsError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:  # malformed --viewport etc.
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    if not urls:
        print("[ERROR] No valid URLs to capture.", file=sys.stderr)
        return 2

    def on_log(level: LogLevel, message: str) -> None:
        if args.quiet and level not in (LogLevel.ERROR, LogLevel.WARNING):
            return
        print(f"[{level.value.upper():<7}] {message}")

    def on_result(result: CaptureResult) -> None:
        if args.quiet:
            return
        mark = {
            CaptureStatus.SUCCESS: "OK " if not result.unchanged else "SAME",
            CaptureStatus.FAILED: "FAIL",
            CaptureStatus.SKIPPED: "SKIP",
        }[result.status]
        print(f"  [{mark}] {result.url}")

    factory = engine_factory or CaptureEngine
    try:
        engine = factory(settings, log=on_log, on_result=on_result)
        summary = engine.run(urls)
    except BrowserNotInstalledError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 3

    if args.json:
        import json

        print(json.dumps(_summary_dict(summary), indent=2, ensure_ascii=False))
    else:
        print(
            f"\nDone: {summary.succeeded} captured, {summary.failed} failed, "
            f"{summary.skipped} skipped (of {summary.total}) in {summary.elapsed_ms / 1000:.1f}s"
        )

    return 0 if summary.failed == 0 else 1


def _run_schedule(args: argparse.Namespace, engine_factory=None) -> int:
    import time

    runs = max(1, int(args.runs))
    codes = []
    for i in range(runs):
        if i > 0 and args.every > 0:
            time.sleep(args.every)
        if not args.quiet:
            print(f"--- scheduled run {i + 1}/{runs} ---")
        codes.append(_run_capture(args, engine_factory))
    return 1 if any(code != 0 for code in codes) else 0


def _parse_caps(text: str) -> dict[str, Any] | None:
    """``--caps 500,50`` -> ``{"screenshots_mb": 500, "history_mb": 50}``.

    A ``host=MB`` entry is a per-site budget and goes to ``site_caps``, so
    ``--caps "500,50,news.example.com=200,*=1000"`` sets all three kinds at once.
    Returns ``None`` when a value is not a number, so the caller can exit 2 with
    the message already printed.
    """
    caps: dict[str, Any] = {}
    if not text:
        return caps
    numbers = ("screenshots_mb", "history_mb")
    per_site: list[str] = []
    index = 0
    for part in (chunk.strip() for chunk in str(text).split(",")):
        if not part:
            continue
        if "=" in part:
            per_site.append(part)
            continue
        try:
            caps[numbers[index]] = int(float(part))
        except (ValueError, IndexError):
            print(f"--caps expects MB numbers, got {part!r}.", file=sys.stderr)
            return None
        index += 1
    if per_site:
        from app.core.retention import SiteCapError, parse_site_caps

        try:
            caps["site_caps"] = parse_site_caps(", ".join(per_site))
        except SiteCapError as exc:
            print(f"--caps: {exc}", file=sys.stderr)
            return None
    return caps


def _run_dashboard(args: argparse.Namespace) -> int:
    from app.core.dashboard import build_dashboard

    caps = _parse_caps(args.caps)
    if caps is None:
        return 2
    try:
        path = build_dashboard(
            args.dir,
            args.out,
            baseline_max_age_days=args.baseline_max_age,
            url_filter=args.url,
            caps=caps,
        )
    except OSError as exc:
        print(f"Could not write the dashboard: {exc}", file=sys.stderr)
        return 2
    print(f"Dashboard written to {path}")

    if args.storage_json:
        from app.core.dashboard import storage_export

        try:
            body, _content_type, _filename = storage_export(
                args.dir, caps, "json", series=args.storage_series > 0, limit=args.storage_series
            )
            target = Path(args.storage_json)
            if target.parent:
                target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        except OSError as exc:
            print(f"Could not write the storage report: {exc}", file=sys.stderr)
            return 2
        print(f"Storage report written to {target}")
    return 0


def _prune_index(output_dir: str, days: int) -> int:
    """Drop rows older than ``days`` from our own SQLite index, if it exists."""
    from app.core.store import HistoryStore

    db = Path(output_dir) / "history.sqlite3"
    if not db.exists():
        return 0
    with HistoryStore(db) as store:
        removed = store.prune(days)
        if removed:
            store.vacuum()
    return removed


def _export_history(args: argparse.Namespace) -> int:
    """Write the history as a file (or stdout) the way ``/api/export`` would."""
    from app.core import api

    page = api.history_page(args.dir, max(0, int(args.limit)), max(0, int(args.offset)), args.url)
    body, _content_type, _filename = api.format_export(page, args.export)
    if not args.out:
        sys.stdout.write(body.decode("utf-8"))
        return 0
    try:
        target = Path(args.out)
        if target.parent and not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    except OSError as exc:
        print(f"Could not write {args.out}: {exc}", file=sys.stderr)
        return 2
    print(
        f"Exported {page['count']} of {page['total']} row(s) to {target} ({args.export}).",
        file=sys.stderr,
    )
    return 0


def _run_history(args: argparse.Namespace) -> int:

    from app.core import history

    if args.export:
        return _export_history(args)

    if args.verify:
        from app.core import verify as verify_module

        report = verify_module.verify_folder(args.dir)
        if args.json:
            print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
            return 1 if report.issues else 0
        print(report.summary())
        for issue in report.issues:
            print(f"  {issue.kind}: {issue.detail}")
        if report.issues:
            print(
                "The reports are the source of truth: 'history --dir DIR --reindex' repairs the index."
            )
        return 1 if report.issues else 0

    if args.reindex:
        from app.core.store import rebuild_index

        runs = len(history.load_runs(args.dir))
        try:
            rows = rebuild_index(args.dir)
        except Exception as exc:  # noqa: BLE001 - report the reason instead of a traceback
            print(f"Could not rebuild the index: {exc}", file=sys.stderr)
            return 2
        print(f"Index rebuilt: {rows} row(s) from {runs} report file(s).")

    if args.prune_days:
        removed = _prune_index(args.dir, args.prune_days)
        print(f"History index pruned: {removed} row(s) older than {args.prune_days} day(s).")

    if args.vacuum:
        from app.core.store import HistoryStore

        db = Path(args.dir) / "history.sqlite3"
        if not db.exists():
            print("No history index to compact.")
        else:
            with HistoryStore(db) as store:
                reclaimed = store.vacuum()
            print(f"Index compacted: {reclaimed / 1024:.1f} KB reclaimed.")

    if args.prune_screenshots:
        from app.core.retention import prune_screenshots

        affected = prune_screenshots(args.dir, args.prune_screenshots, dry_run=args.dry_run)
        if args.dry_run:
            print(
                f"Would delete {len(affected)} file(s) older than {args.prune_screenshots} day(s):"
            )
            for path in affected:
                print(f"  {Path(path).name}")
        else:
            print(
                f"Screenshots deleted: {len(affected)} file(s) older than "
                f"{args.prune_screenshots} day(s)."
            )

    if args.prune_screenshots_mb:
        from app.core.retention import prune_screenshots_by_size

        affected = prune_screenshots_by_size(
            args.dir, args.prune_screenshots_mb, dry_run=args.dry_run
        )
        if args.dry_run:
            print(
                f"Would delete {len(affected)} file(s) to get under {args.prune_screenshots_mb} MB:"
            )
            for path in affected:
                print(f"  {Path(path).name}")
        else:
            print(
                f"Screenshots deleted: {len(affected)} file(s) to stay under "
                f"{args.prune_screenshots_mb} MB."
            )

    if args.site_caps:
        from app.core.retention import SiteCapError, apply_site_caps, parse_site_caps

        try:
            wanted = parse_site_caps(args.site_caps)
        except SiteCapError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 2
        trims = apply_site_caps(args.dir, wanted, dry_run=args.dry_run, limit=args.limit)
        if args.json:
            print(json.dumps([trim.to_dict() for trim in trims], indent=2, ensure_ascii=False))
            return 0
        if not trims:
            print(
                f"Every site fits its budget ({', '.join(f'{k}={v:g} MB' for k, v in sorted(wanted.items()))})."
            )
            return 0
        for trim in trims:
            print(trim.summary())
            if trim.still_over:
                left = trim.kept_bytes + trim.reference_bytes
                print(
                    f"  {trim.label} is still over {trim.cap_mb:g} MB: "
                    f"{left / 1048576:.1f} MB is left, of which "
                    f"{trim.reference_bytes / 1048576:.1f} MB is the baseline/latest "
                    "references - and those are never deleted."
                )
        print(
            f"{len(trims)} site(s) over budget; "
            f"{sum(trim.bytes for trim in trims) / 1048576:.1f} MB "
            f"{'would be freed' if args.dry_run else 'freed'}."
        )
        return 0

    if args.compare:
        from app.core import compare as compare_module

        given = list(args.compare)[:2]
        try:
            if len(given) == 1:
                spec = given[0]
                if ":" not in spec:
                    spec = f"{spec}:"
                second = compare_module.parse_period(spec)
                first = compare_module.mirror_period(second)
            else:
                first = compare_module.parse_period(given[0])
                second = compare_module.parse_period(given[1])
        except compare_module.PeriodError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 2
        report = compare_module.compare_periods(args.dir, first, second)
        if args.json:
            print(
                json.dumps(
                    report.to_dict(limit=max(0, args.limit)),
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print(report.summary(limit=args.limit or 0))
        if args.compare_pdf:
            from app.core import pdfreport

            try:
                written = pdfreport.write_compare_pdf(report, args.compare_pdf)
            except OSError as exc:
                print(f"Could not write the comparison PDF: {exc}", file=sys.stderr)
                return 2
            print(f"Comparison PDF written to {written}")
        return 0

    if args.top_space:
        from app.core.retention import site_space

        spaces = site_space(args.dir, limit=args.limit)
        if args.json:
            print(json.dumps([space.to_dict() for space in spaces], indent=2, ensure_ascii=False))
            return 0
        if not spaces:
            print("No captures to measure.")
            return 0
        print(f"{'BYTES':>12}  {'FILES':>5}  SITE")
        for space in spaces:
            print(f"{space.bytes:>12}  {space.files:>5}  {space.label}")
        print(
            "References (latest_/baseline_ files) are counted here but never pruned by "
            "--prune-site."
        )
        return 0

    if args.prune_site:
        from app.core.retention import prune_site

        result = prune_site(args.dir, args.prune_site, dry_run=args.dry_run)
        verb = "Would delete" if args.dry_run else "Deleted"
        if not result.removed_anything:
            print(f"No captures found for '{args.prune_site}'.")
            return 0
        print(f"{verb} {result.summary()} ({result.bytes / 1024:.1f} KB).")
        if args.dry_run:
            for path in result.paths[: max(0, args.limit or len(result.paths))]:
                print(f"  {path.name}")
        return 0

    if args.prune_history_mb:
        from app.core.retention import prune_history

        report = prune_history(
            args.dir, args.prune_history_mb, dry_run=args.dry_run, archive=args.archive
        )
        kept = (
            f" (kept in {', '.join(sorted({Path(p).name for p in report.archives}))})"
            if report.archives
            else ""
        )
        if not report.removed_anything:
            print(f"Nothing to clean up (the history is already under {args.prune_history_mb} MB).")
        elif args.dry_run:
            verb = "archive" if args.archive else "delete"
            print(f"Would {verb} {report.summary()} to stay under {args.prune_history_mb} MB:")
            for path in report.reports:
                print(f"  {Path(path).name}")
        else:
            verb = "archived" if args.archive else "deleted"
            print(
                f"History cleaned up: {report.summary()} {verb} to stay under "
                f"{args.prune_history_mb} MB{kept}."
            )

    if args.restore:
        from app.core.retention import restore_archive

        # A bare "archive-2026-01.zip" names the file the user just saw in their
        # history folder, so look there before giving up on the relative path.
        archive = Path(args.restore)
        if not archive.is_file() and (Path(args.dir) / args.restore).is_file():
            archive = Path(args.dir) / args.restore
        try:
            restored = restore_archive(args.dir, archive, dry_run=args.dry_run)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if not restored:
            print(f"Nothing to restore from {archive.name} (every report is already there).")
        elif args.dry_run:
            print(f"Would restore {len(restored)} file(s) from {archive.name}:")
            for path in restored:
                print(f"  {Path(path).name}")
        else:
            print(f"Restored {len(restored)} file(s) from {archive.name}.")
            from app.core.store import rebuild_index

            try:
                rows = rebuild_index(args.dir)
            except Exception as exc:  # noqa: BLE001 - the files are back either way
                print(f"Could not rebuild the index: {exc}", file=sys.stderr)
            else:
                print(f"Index rebuilt: {rows} row(s).")

    if args.archive and args.older_than:
        from app.core.retention import archive_runs

        result = archive_runs(args.dir, args.older_than, dry_run=args.dry_run)
        if not result.removed_anything:
            print(f"Nothing older than {args.older_than} day(s) to archive.")
        elif args.dry_run:
            print(f"Would archive {result.summary()}:")
            for path in result.files:
                print(f"  {Path(path).name}")
        else:
            saved = result.bytes_before / 1024 - result.bytes_after / 1024
            print(
                f"Archived {result.summary()} ({result.bytes_before / 1024:.1f} KB -> "
                f"{result.bytes_after / 1024:.1f} KB, {saved:.1f} KB saved)."
            )

    if args.trend:
        summary_text = history.trend_summary(args.dir)
        if args.json:
            print(json.dumps({"trend": summary_text}, ensure_ascii=False))
        else:
            print(summary_text)
        return 0

    rows = history.flat_rows(args.dir)
    if args.site:
        rows = [row for row in rows if row["url"] == args.site]

    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0

    if not rows:
        print("No history found.")
        return 0
    for row in rows:
        diff = "" if row["diff"] is None else f"  diff {row['diff']:.2f}"
        print(f"{row['timestamp']}  {str(row['status']):<7}  {row['url']}{diff}")
    return 0


def _run_baseline(args: argparse.Namespace) -> int:
    from app.core import baseline, history

    if args.pin:
        path = baseline.pin_baseline(args.dir, args.pin)
        if path is None:
            print(
                f"No latest capture found for {args.pin} (run a capture first).",
                file=sys.stderr,
            )
            return 1
        print(f"Baseline pinned: {path.name}")
        return 0

    if args.clear:
        if baseline.clear_baseline(args.dir, args.clear):
            print("Baseline cleared.")
        else:
            print("No baseline to clear.")
        return 0

    if args.list:
        sites = history.sites(args.dir)
        if not sites:
            print("No history found.")
            return 0
        for url in sites:
            marker = "pinned" if baseline.has_baseline(args.dir, url) else "  --  "
            print(f"[{marker}] {url}")
        return 0

    print("Nothing to do: pass --pin URL, --clear URL or --list.", file=sys.stderr)
    return 2


def _run_digest(args: argparse.Namespace) -> int:

    from app.core import digest

    unknown = digest.unknown_kinds(args.attach)
    if unknown:
        print(
            "Unknown --attach value(s): "
            + ", ".join(unknown)
            + ". Use dashboard, csv, drift or none.",
            file=sys.stderr,
        )
        return 2

    if args.profiles:
        return _run_digest_profiles(args)

    comparison = ""
    if args.base:
        comparison = digest.build_comparison(args.dir, args.base, args.days)

    links = digest.link_lines(args.link_base, args.days)

    if args.send:
        from app.core.settings import CaptureSettings

        settings = CaptureSettings(
            output_dir=args.dir,
            smtp_host=args.smtp_host,
            smtp_port=args.smtp_port,
            smtp_user=args.smtp_user,
            smtp_password=args.smtp_password,
            alert_email_to=args.email_to,
        )
        if args.skip_if_unchanged and not args.force:
            skip, marker, _ = digest.should_skip_digest(args.dir)
            if skip:
                print(
                    "Digest skipped: the history has not changed since the last digest "
                    f"(marker {marker[:12]}); use --force to send it anyway."
                )
                return 0
        try:
            digest.send_digest(
                settings,
                args.dir,
                args.days,
                attach=args.attach,
                extra=comparison,
                max_attachment_bytes=max(0, int(args.max_attachment_kb)) * 1024,
                link_base=args.link_base,
            )
        except Exception as exc:  # noqa: BLE001 - report why it could not be sent
            print(f"Could not send the digest: {exc}", file=sys.stderr)
            return 2
        digest.record_marker(args.dir, args.days, digest.marker_row_count(args.dir))
        print(f"Digest emailed to {args.email_to}.")
        return 0

    if args.json:
        if args.base:
            print(
                json.dumps(
                    {
                        "head": digest.collect_stats(args.dir, args.days),
                        "base": digest.collect_stats(args.base, args.days),
                        "comparison": comparison.splitlines(),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print(
                json.dumps(digest.collect_stats(args.dir, args.days), indent=2, ensure_ascii=False)
            )
        return 0

    print(digest.build_digest(args.dir, args.days))
    if comparison:
        print()
        print(comparison)
    if links:
        print()
        print("\n".join(links))
    return 0


def _run_digest_profiles(args: argparse.Namespace) -> int:
    """One digest per saved profile: its own window, output folder and issue."""

    from app.core import digest
    from app.core import profiles as profiles_mod

    base = Path(args.profiles_base) if args.profiles_base else profiles_mod.default_base()
    plan = profiles_mod.digest_plan(base, args.days)
    if not plan:
        print(f"No profiles found in {base}.", file=sys.stderr)
        return 0

    from app.core.settings import CaptureSettings

    results: list[dict] = []
    failures = 0
    for entry in plan:
        profile = profiles_mod.load_profile(base, entry["name"]) or {}
        stored = profile.get("settings") or {}
        output_dir = entry["output_dir"] or args.dir
        window = entry["days"]
        header = f"{entry['name']}: last {window} day(s)"
        if entry["issue"]:
            header += f" (issue #{entry['issue']})"

        if args.send:
            settings = CaptureSettings(
                output_dir=output_dir,
                smtp_host=args.smtp_host or stored.get("smtp_host", ""),
                smtp_port=int(stored.get("smtp_port") or args.smtp_port),
                smtp_user=stored.get("smtp_user", "") or args.smtp_user,
                smtp_password=stored.get("smtp_password", "") or args.smtp_password,
                alert_email_to=stored.get("alert_email_to", "") or args.email_to,
            )
            if args.skip_if_unchanged and not args.force:
                skip, marker, _ = digest.should_skip_digest(output_dir)
                if skip:
                    print(f"{entry['name']}: digest skipped (history unchanged, {marker[:12]}).")
                    continue
            try:
                digest.send_digest(
                    settings,
                    output_dir,
                    window,
                    attach=args.attach,
                    max_attachment_bytes=max(0, int(args.max_attachment_kb)) * 1024,
                    link_base=args.link_base,
                )
            except Exception as exc:  # noqa: BLE001 - report which profile failed
                print(f"{entry['name']}: could not send the digest: {exc}", file=sys.stderr)
                failures += 1
                continue
            digest.record_marker(output_dir, window, digest.marker_row_count(output_dir))
            print(f"{entry['name']}: digest emailed to {settings.alert_email_to} ({window}d).")
            if entry["issue"]:
                print(f"{entry['name']}: tracking issue #{entry['issue']}.")
        else:
            stats = digest.collect_stats(output_dir, window)
            if args.json:
                results.append({"profile": entry["name"], **stats, "issue": entry["issue"]})
            else:
                print(header)
                print(digest.format_digest(stats))
                print()

    if args.json:
        print(json.dumps({"profiles": results}, indent=2, ensure_ascii=False))
    return 1 if failures else 0


def _caps_from_args(args: argparse.Namespace) -> dict[str, Any] | None:
    """``--caps 500,50`` plus ``--site-caps 'host=200'`` -> one mapping.

    Returns ``None`` when either spelling is malformed (the message is already
    printed), so the caller can exit 2.
    """
    caps = _parse_caps(getattr(args, "caps", ""))
    if caps is None:
        return None
    per_site = str(getattr(args, "site_caps", "") or "").strip()
    if per_site and "site_caps" not in caps:
        from app.core.retention import SiteCapError, parse_site_caps

        try:
            caps["site_caps"] = parse_site_caps(per_site)
        except SiteCapError as exc:
            print(f"--site-caps: {exc}", file=sys.stderr)
            return None
    return caps


def _run_metrics(args: argparse.Namespace) -> int:
    """Print the Prometheus exposition, write it to ``--out``, or check freshness.

    ``--write-if-changed`` keeps the file's timestamp when the numbers are
    identical, which is what a cron/timer job wants: the file then records the
    last *change*, not the last run.

    ``--check`` turns the same payload into an exit code - 0 when the bot is
    still capturing successfully, 1 when it is not, 2 when nothing could be
    written - so systemd's ``OnFailure=`` (or a cron job) can page someone.
    """
    from app.core import dashboard, status

    caps = _caps_from_args(args)
    if caps is None:
        return 2

    payload = status.status_payload(
        args.dir,
        "",
        max(0, int(args.stale_after)),
        storage=dashboard.storage_stats(args.dir, caps),
    )
    text = status.metrics_text(payload, version=__version__)
    if args.out:
        target = Path(args.out)
        try:
            unchanged = (
                args.write_if_changed
                and target.is_file()
                and target.read_text(encoding="utf-8") == text
            )
            if unchanged:
                print(f"Metrics unchanged in {target} ({len(text.encode('utf-8'))} bytes).")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
                print(f"Metrics written to {target} ({len(text.encode('utf-8'))} bytes).")
        except OSError as exc:
            print(f"Could not write the metrics: {exc}", file=sys.stderr)
            return 2
    elif not args.check:
        sys.stdout.write(text)
        return 0

    if not args.check:
        return 0

    limit = int(args.max_age or args.stale_after or 0)
    result = status.freshness(payload, limit)
    if args.json:
        print(
            json.dumps(
                {
                    **result,
                    "state": (payload.get("health") or {}).get("state", ""),
                    "last_success": payload.get("last_success"),
                    "checked_at": payload.get("generated_at", ""),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(("OK: " if result["ok"] else "FAILED: ") + str(result["detail"]))
    return 0 if result["ok"] else 1


def _run_serve(args: argparse.Namespace) -> int:
    from app.core import api

    if bool(args.tls_cert) != bool(args.tls_key):
        print("TLS needs both --tls-cert and --tls-key.", file=sys.stderr)
        return 2

    caps = _caps_from_args(args)
    if caps is None:
        return 2

    try:
        api.serve(
            args.dir,
            args.host,
            args.port,
            token=args.token,
            cors=args.cors,
            public_dashboard=args.public_dashboard,
            rate_limit=args.rate_limit,
            stale_after=args.stale_after,
            tls_cert=args.tls_cert,
            tls_key=args.tls_key,
            caps=caps,
        )
    except (OSError, ValueError) as exc:
        print(f"Could not start the server on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
