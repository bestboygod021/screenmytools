"""Explore a site the way a curious visitor does: click everything, photograph every screen.

A URL list can only photograph the pages someone already knows about. The
screens that actually matter - the filter panel behind a button, the tab that
switches a table, the dialog a menu opens - only exist *after* a click. The
crawler finds those clickable things on the page, clicks them one by one, and
saves a full-page screenshot of every new screen it reaches::

    python -m app.cli crawl https://shop.example.com --out ./shots --max-depth 2

For each screen the crawler

1. tags everything clickable in the DOM (``app.core.journey.DISCOVERY_SCRIPT``),
2. clicks one of them on a **fresh page** that first replays the click-path that
   led there (so a screen two clicks deep is reached the same way a person would),
3. waits for the page to settle, fingerprints it (URL + title + visible text) and
   compares it with the screens already captured,
4. saves the new ones as ``crawl-<stamp>/001-home.png`` ... and goes back to the
   next candidate.

It is a breadth-first walk with budgets, not a search spider: ``--max-states``
screens, ``--max-depth`` clicks deep, ``--max-clicks`` clicks in total, and an
ignore list for the parts of a site that must not be poked. Buttons that say
"Log out", "Delete", "Pay" and their relatives are skipped unless
``--allow-dangerous`` says otherwise, off-site links stay off-site unless
``--allow-external``, and ``--list`` prints what would be clicked without
touching anything - the review step before letting it loose on a live site.
"""

from __future__ import annotations

import csv
import json
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from app.core.engine import BrowserNotInstalledError, LogLevel
from app.core.journey import (
    ELEMENT_ATTR,
    clear_element_tags,
    describe_elements,
    page_signature,
    slug_for_text,
)
from app.core.url_utils import build_url_label, sanitize_component, unique_path

#: What the crawler is willing to click, in page order.
DEFAULT_SELECTORS: tuple[str, ...] = (
    "a[href]",
    "button",
    "[role=button]",
    "[role=tab]",
    "[role=menuitem]",
    "summary",
)

#: Words that mean "this click has consequences". Lower-case substrings.
DANGEROUS_WORDS: tuple[str, ...] = (
    "log out",
    "logout",
    "sign out",
    "signout",
    "delete",
    "remove",
    "unsubscribe",
    "deactivate",
    "close account",
    "cancel subscription",
    "cancel account",
    "cancel order",
    "buy now",
    "checkout",
    "place order",
    "pay now",
    "purchase",
    "confirm payment",
    "withdraw",
    "transfer",
    "deactivate account",
    "shutdown",
    "shut down",
    "purge",
)

#: Hrefs that are never worth clicking (and would leave the browser).
SKIPPED_SCHEMES = ("mailto:", "tel:", "javascript:", "sms:", "whatsapp:", "ftp:")
#: Extensions that mean "a download", not "a page".
SKIPPED_SUFFIXES = (
    ".pdf",
    ".zip",
    ".rar",
    ".7z",
    ".gz",
    ".csv",
    ".xlsx",
    ".xls",
    ".doc",
    ".docx",
    ".pptx",
    ".dmg",
    ".exe",
    ".msi",
    ".apk",
    ".mp3",
    ".mp4",
    ".mov",
    ".avi",
)


def is_dangerous(text: str, extra: Sequence[str] = ()) -> bool:
    """Whether a label reads like "this will change something permanent"."""
    haystack = str(text or "").lower()
    return any(word in haystack for word in (*DANGEROUS_WORDS, *(w.lower() for w in extra)))


def resume_from_walk(path: str | Path, settings: CaptureSettings | None = None) -> tuple[str, int]:
    """Return (url, remaining_depth) from the last crawl file."""
    import json
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    screens = data.get("screens") or []
    last_url = screens[-1]["url"] if screens else data.get("url", "")
    done_depth = len(screens)
    max_d = getattr(settings, "max_depth", 2) if settings else 2
    return str(last_url or ""), max(0, max_d - done_depth)


class CrawlOptions:
    """How far the crawler may go, and what it must keep its hands off."""

    max_depth: int = 2
    max_states: int = 25
    max_clicks: int = 40
    max_seconds: float = 0.0  # 0 = no wall-clock budget
    same_origin: bool = True
    allow_external: bool = False
    allow_dangerous: bool = False
    ignore: tuple[str, ...] = ()
    include: tuple[str, ...] = ()
    dangerous_words: tuple[str, ...] = ()
    selectors: tuple[str, ...] = DEFAULT_SELECTORS
    delay_ms: int = 300  # polite pause between clicks
    settle_ms: int = 0  # extra wait after a click (0 = the settings' settle)
    min_text_length: int = 1  # ignore icon-only hit targets
    capture_base: bool = True  # photograph the landing page itself
    signature_guard: bool = True  # skip screens that look like one we have

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_depth": self.max_depth,
            "max_states": self.max_states,
            "max_clicks": self.max_clicks,
            "max_seconds": self.max_seconds,
            "same_origin": self.same_origin,
            "allow_external": self.allow_external,
            "allow_dangerous": self.allow_dangerous,
            "ignore": list(self.ignore),
            "include": list(self.include),
            "delay_ms": self.delay_ms,
            "settle_ms": self.settle_ms,
            "capture_base": self.capture_base,
        }


@dataclass(frozen=True)
class Candidate:
    """One thing on the page that could be clicked."""

    marker: str = ""
    kind: str = ""
    text: str = ""
    href: str = ""
    tag: str = ""
    role: str = ""
    in_viewport: bool = True

    @property
    def signature(self) -> str:
        """Identity across pages: the same "Sign in" link is clicked once."""
        return "|".join(
            [
                self.kind,
                slug_for_text(self.text, fallback=""),
                slug_for_text(self.href, fallback=""),
            ]
        )

    def describe(self) -> str:
        label = self.text or self.href or self.tag or "element"
        return f"{self.kind or 'element'} '{label}'"

    def slug(self) -> str:
        return slug_for_text(self.text or self.href or self.tag or "click", fallback="click")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "href": self.href,
            "tag": self.tag,
            "role": self.role,
            "marker": self.marker,
        }


def qualifies(
    candidate: Candidate,
    base_url: str,
    page_url: str,
    options: CrawlOptions,
) -> tuple[bool, str]:
    """``(keep, why not)`` for one candidate against the crawler's rules."""
    text = candidate.text.strip()
    href = (candidate.href or "").strip()
    lowered = href.lower()

    if len(text) < max(0, int(options.min_text_length)) and not href:
        return False, "no label"
    if lowered.startswith(SKIPPED_SCHEMES):
        return False, "not a page link"
    if any(lowered.split("?", 1)[0].endswith(suffix) for suffix in SKIPPED_SUFFIXES):
        return False, "a download, not a page"
    if lowered.startswith("#") and len(lowered) <= 1:
        return False, "the page top"
    if options.ignore and any(
        needle.lower() in f"{text} {href}".lower() for needle in options.ignore
    ):
        return False, "ignored"
    if not options.allow_dangerous and is_dangerous(f"{text} {href}", options.dangerous_words):
        return False, "looks dangerous"
    if href and not options.allow_external:
        target = urljoin(page_url, href)
        if options.same_origin and not same_site(base_url, target):
            return False, "off-site"
    if options.include and not any(
        needle.lower() in f"{text} {href}".lower() for needle in options.include
    ):
        return False, "not in --include"
    return True, ""


def same_site(base: str, target: str) -> bool:
    """Whether ``target`` lives on the same host (ignoring ``www.``)."""
    left, right = urlparse(base), urlparse(target)
    if not right.scheme and not right.netloc:
        return True  # a relative link stays on the site by definition
    return _host(left) == _host(right)


def _host(parsed) -> str:
    host = (parsed.netloc or "").lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


@dataclass
class CrawledPage:
    """One screen the crawler reached and photographed."""

    index: int = 0
    name: str = ""
    url: str = ""
    title: str = ""
    file_path: str = ""
    depth: int = 0
    path: list[str] = field(default_factory=list)  # the clicks that led here
    signature: str = ""
    elapsed_ms: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "index": self.index,
            "name": self.name,
            "url": self.url,
            "title": self.title,
            "file": self.file_path,
            "depth": self.depth,
            "path": list(self.path),
            "signature": self.signature,
            "elapsed_ms": self.elapsed_ms,
        }
        if self.error:
            data["error"] = self.error
        return data


@dataclass
class CrawlReport:
    """The whole walk: every screen, every skip, every failure."""

    url: str = ""
    output_dir: str = ""
    generated_at: str = ""
    pages: list[CrawledPage] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    clicks: int = 0
    options: dict[str, Any] = field(default_factory=dict)
    stopped: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.pages) and not self.errors

    def summary(self) -> str:
        lines = [
            f"{len(self.pages)} screen(s) captured from {self.url} "
            f"({self.clicks} click(s) probed, {len(self.skipped)} skipped)."
        ]
        for page in self.pages:
            via = " -> ".join(page.path) if page.path else "landing page"
            lines.append(f"  {page.index:03d} {page.name}  [{via}]  {page.url}")
        if self.errors:
            lines.append(f"{len(self.errors)} problem(s):")
            lines.extend(f"  {error}" for error in self.errors[:10])
        if self.stopped:
            lines.append("Stopped early (budget or stop request).")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "url": self.url,
            "output_dir": self.output_dir,
            "ok": self.ok,
            "clicks": self.clicks,
            "stopped": self.stopped,
            "options": dict(self.options),
            "pages": [page.to_dict() for page in self.pages],
            "skipped": list(self.skipped),
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class _State:
    """A screen to reach: the click-path from the landing page, as descriptions."""

    steps: tuple[Candidate, ...] = ()
    depth: int = 0
    name: str = "home"

    @property
    def description(self) -> list[str]:
        return [step.text or step.href or step.kind or "click" for step in self.steps]


class Crawler:
    """Breadth-first click-and-capture, driven by a :class:`CaptureEngine` session."""

    def __init__(
        self,
        engine: Any,
        options: CrawlOptions | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.engine = engine
        self.settings = engine.settings
        self.options = options or CrawlOptions()
        self._clock = clock
        self._index = 0
        self._started = 0.0

    # ------------------------------------------------------------------ helpers
    def log(self, level: LogLevel, message: str) -> None:
        self.engine.log(level, message)

    def _out_of_time(self) -> bool:
        budget = float(self.options.max_seconds or 0)
        return bool(budget) and (self._clock() - self._started) > budget

    def _pause(self) -> None:
        delay = max(int(self.options.delay_ms), int(self.options.settle_ms))
        if delay > 0:
            self.engine._interruptible_sleep(delay)

    def _capture(self, page: Any) -> bytes:
        self._index += 1
        if self.settings.scroll_to_load_lazy_content:
            self.engine._lazy_scroll(page, self._index)
        page._capture_bot_index = self._index
        width, height = self.engine._measure(page)
        payload, _stitched = self.engine._screenshot(page, width, height, self._index)
        return self.engine._encode_for_format(payload)

    def _save(self, payload: bytes, folder: Path, order: int, name: str) -> Path:
        extension = self.engine._file_extension()
        filename = f"{order:03d}-{sanitize_component(name, max_length=40) or 'screen'}.{extension}"
        path = unique_path(folder, filename)
        try:
            path.write_bytes(payload)
        except OSError as exc:  # pragma: no cover - a full disk is the user's problem
            raise RuntimeError(f"Could not write {path}: {exc}") from exc
        return path

    def _open(self, browser: Any, url: str) -> Any:
        """A fresh page with the same context settings as a normal capture."""
        context = browser.new_context(**self.engine._context_kwargs())
        page = context.new_page()
        page.set_default_timeout(self.settings.navigation_timeout_ms)
        try:
            page.set_default_navigation_timeout(self.settings.navigation_timeout_ms)
        except Exception:  # pragma: no cover - not every engine exposes this
            pass
        return context, page

    def _close(self, context: Any) -> None:
        self.engine._close_context(context)

    def _candidates(self, page: Any) -> list[Candidate]:
        found: list[Candidate] = []
        for item in describe_elements(page):
            candidate = Candidate(
                marker=str(item.get("marker") or ""),
                kind=str(item.get("kind") or ""),
                text=str(item.get("text") or ""),
                href=str(item.get("href") or ""),
                tag=str(item.get("tag") or ""),
                role=str(item.get("role") or ""),
                in_viewport=bool(item.get("in_viewport", True)),
            )
            if candidate.marker:
                found.append(candidate)
        return found

    # ------------------------------------------------------------------- the walk
    def list_clicks(self, url: str) -> list[dict[str, Any]]:
        """``--list``: what the crawler would click, without clicking anything."""
        playwright = None
        browser = None
        found: list[dict[str, Any]] = []
        try:
            playwright = self.engine._playwright_factory()
            playwright = playwright.__enter__()
            browser = self.engine._launch(playwright)
            context, page = self._open(browser, url)
            try:
                self.engine._goto(page, url)
                self.engine._settle(page, 0)
                for candidate in self._candidates(page):
                    keep, why = qualifies(candidate, url, str(page.url), self.options)
                    found.append(
                        {
                            **candidate.to_dict(),
                            "kept": keep,
                            "reason": why,
                            "would_capture": keep,
                        }
                    )
            finally:
                self._close(context)
        except BrowserNotInstalledError:
            # No point walking anything: the CLI turns this into exit code 3.
            raise
        except Exception as exc:  # noqa: BLE001
            self.log(
                LogLevel.ERROR, f"Could not inspect {url}: {self.engine._describe_exception(exc)}"
            )
        finally:
            self.engine._close_browser(browser)
            if playwright is not None:
                try:
                    playwright.__exit__(None, None, None)
                except Exception:  # pragma: no cover
                    pass
        return found

    def _click_candidate(self, page: Any, candidate: Candidate, timeout: int) -> None:
        """Click a candidate by the marker the discovery script put on it."""
        selector = f'[{ELEMENT_ATTR}="{candidate.marker}"]'
        locator = self.engine._first_visible(page, selector)
        if locator is None:
            raise RuntimeError(f"{candidate.describe()} disappeared before it could be clicked")
        locator.click(timeout=timeout)

    def _reach(self, browser: Any, base: str, state: _State, timeout: int) -> tuple[Any, list[str]]:
        """Open a page and replay the state's click-path on it.

        Every state is reached on a *fresh* page: that is what keeps the walk
        honest (no leftover modal from the previous probe) and what makes a
        screen two clicks deep reproducible by a person following the report.
        """
        context, page = self._open(browser, base)
        reached: list[str] = []
        try:
            self.engine._goto(page, base)
            self.engine._settle(page, 0)
            for step in state.steps:
                live = self._match(page, step)
                if live is None:
                    raise RuntimeError(
                        f"'{step.text or step.href or step.kind}' is no longer on the page"
                    )
                self._click_candidate(page, live, timeout)
                reached.append(live.text or live.href or live.kind)
                self._pause()
                self.engine._settle(page, 0)
            return context, reached
        except Exception:
            self._close(context)
            raise

    def _match(self, page: Any, wanted: Candidate) -> Candidate | None:
        """The candidate on the *current* page that corresponds to ``wanted``.

        Markers are assigned in document order, so they survive a reload; matching
        on the description as well keeps the walk honest when the page changed
        between two visits (a banner appeared, a cookie bar went away).
        """
        for candidate in self._candidates(page):
            if candidate.marker == wanted.marker:
                return candidate
        for candidate in self._candidates(page):
            if candidate.signature == wanted.signature and wanted.signature.strip("|"):
                return candidate
        return None

    def run(self, url: str) -> CrawlReport:
        """Walk ``url`` breadth-first and photograph every screen it reaches."""
        options = self.options
        report = CrawlReport(
            url=url,
            output_dir=str(self.settings.output_dir),
            generated_at=datetime.now().isoformat(timespec="seconds"),
            options=options.to_dict(),
        )
        self._started = self._clock()

        label = build_url_label(url)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        folder = Path(self.settings.output_dir) / f"crawl-{label}-{stamp}"
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover
            report.errors.append(f"Could not create {folder}: {exc}")
            return report

        playwright = None
        browser = None
        seen: dict[str, str] = {}  # signature -> screen name
        clicked: set[str] = set()
        rejected: set[str] = set()  # candidates the rules turned down (said once)
        order = 0
        queue: deque[_State] = deque()
        if options.capture_base:
            queue.append(_State())

        try:
            playwright = self.engine._playwright_factory()
            playwright = playwright.__enter__()
            browser = self.engine._launch(playwright)

            while queue:
                if self.engine.stop_requested or self._out_of_time():
                    report.stopped = True
                    break
                if len(report.pages) >= max(1, int(options.max_states)):
                    report.stopped = True
                    break

                state = queue.popleft()
                timeout = self.settings.navigation_timeout_ms
                started = self._clock()
                context = None
                try:
                    context, reached = self._reach(browser, url, state, timeout)
                    page = context.pages[0]
                    clear_element_tags(page)
                    signature = page_signature(page)
                    if options.signature_guard and signature in seen:
                        self.log(
                            LogLevel.DEBUG,
                            f"  {state.name}: same screen as '{seen[signature]}', not captured again.",
                        )
                        if state.depth < options.max_depth:
                            self._enqueue(queue, state, page, url, clicked, rejected, report)
                        continue

                    order += 1
                    name = state.name if state.depth else "home"
                    payload = self._capture(page)
                    path = self._save(payload, folder, order, name)
                    title = ""
                    try:
                        title = str(page.title() or "")
                    except Exception:  # pragma: no cover - a page without a title is fine
                        title = ""
                    seen[signature] = name
                    report.pages.append(
                        CrawledPage(
                            index=order,
                            name=name,
                            url=str(page.url),
                            title=title,
                            file_path=str(path),
                            depth=state.depth,
                            path=reached,
                            signature=signature,
                            elapsed_ms=int((self._clock() - started) * 1000),
                        )
                    )
                    self.log(
                        LogLevel.SUCCESS,
                        f"  {order:03d} {name} [{'>'.join(reached) if reached else 'landing page'}] "
                        f"-> {path.name}",
                    )
                    if state.depth < options.max_depth:
                        self._enqueue(queue, state, page, url, clicked, rejected, report)
                except Exception as exc:  # noqa: BLE001 - one bad screen must not stop the walk
                    message = f"{state.name}: {self.engine._describe_exception(exc)}"
                    report.errors.append(message)
                    self.log(LogLevel.WARNING, f"  {state.name} could not be reached: {message}")
                finally:
                    self._close(context)
        except BrowserNotInstalledError:
            raise
        except Exception as exc:  # noqa: BLE001
            report.errors.append(self.engine._describe_exception(exc))
            self.log(LogLevel.ERROR, f"Crawl failed: {exc}")
        finally:
            self.engine._close_browser(browser)
            if playwright is not None:
                try:
                    playwright.__exit__(None, None, None)
                except Exception:  # pragma: no cover
                    pass

        report.clicks = len(clicked)
        return report

    def _enqueue(
        self,
        queue: deque[_State],
        state: _State,
        page: Any,
        base: str,
        clicked: set[str],
        rejected: set[str],
        report: CrawlReport,
    ) -> None:
        """Look at what is clickable here and queue the next screens.

        Every screen is reached on a fresh page, so the same candidates come round
        again on every replay; these two sets are what keep the walk - and the
        report - from repeating themselves.
        """
        options = self.options
        for candidate in self._candidates(page):
            if len(clicked) >= max(1, int(options.max_clicks)):
                report.stopped = True
                return
            if candidate.signature in clicked or candidate.signature in rejected:
                continue
            keep, why = qualifies(candidate, base, str(page.url), options)
            if not keep:
                rejected.add(candidate.signature)
                report.skipped.append(
                    {"text": candidate.text, "href": candidate.href, "reason": why}
                )
                continue
            clicked.add(candidate.signature)
            queue.append(
                _State(
                    steps=(*state.steps, candidate),
                    depth=state.depth + 1,
                    name=candidate.slug(),
                )
            )


def write_crawl_report(report: CrawlReport, folder: str | Path) -> Path | None:
    """Write ``report.json`` and ``report.csv`` into the crawl folder."""
    target = Path(folder)
    path = target / "report.json"
    try:
        path.write_text(
            json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:  # pragma: no cover - the report is a convenience
        return None
    try:
        with (target / "report.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["index", "name", "url", "title", "depth", "path", "file", "error"])
            for page in report.pages:
                writer.writerow(
                    [
                        page.index,
                        page.name,
                        page.url,
                        page.title,
                        page.depth,
                        " > ".join(page.path),
                        page.file_path,
                        page.error,
                    ]
                )
    except OSError:  # pragma: no cover
        pass
    return path


#: A screen whose appearance moved this far (dHash ratio) counts as changed.
DEFAULT_CHANGED_THRESHOLD = 0.02


@dataclass
class CrawlDiff:
    """What one crawl found that the previous one did not, and the reverse.

    Two walks of the same site are only interesting *against each other*: the
    screens that appeared are the ones somebody added, the ones that vanished are
    a deploy that removed something. Screens that exist in both are compared by
    their pixels - "the page is still there but its content moved" is the change a
    URL list can never see, and the reason to keep a crawl history at all.

    Everything is keyed on the address of the screen, so a renamed button shows up
    as a rename rather than as one screen leaving and another arriving.
    """

    previous: str = ""  # the folder the earlier report came from
    added: list[CrawledPage] = field(default_factory=list)
    removed: list[dict[str, str]] = field(default_factory=list)
    renamed: list[dict[str, str]] = field(default_factory=list)
    changed: list[dict[str, Any]] = field(default_factory=list)
    threshold: float = DEFAULT_CHANGED_THRESHOLD
    unchanged: int = 0
    #: screens whose two screenshots could not be compared (a pruned file, no Pillow)
    unknown: int = 0

    @property
    def any(self) -> bool:
        return bool(self.added or self.removed or self.renamed or self.changed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "previous": self.previous,
            "threshold": self.threshold,
            "added": [page.to_dict() for page in self.added],
            "removed": list(self.removed),
            "renamed": list(self.renamed),
            "changed": list(self.changed),
            "unchanged": self.unchanged,
            "unknown": self.unknown,
        }

    def summary(self) -> str:
        where = f" (against {self.previous})" if self.previous else ""
        if not self.any:
            tail = f" ({self.unknown} screen(s) could not be compared)" if self.unknown else ""
            return (
                f"No changes since the previous crawl{where}: "
                f"{self.unchanged} screen(s) unchanged{tail}."
            )
        lines = [f"Changes since the previous crawl{where}:"]
        for page in self.added:
            via = " -> ".join(page.path) if page.path else "landing page"
            lines.append(f"  + {page.name}  [{via}]  {page.url}")
        for item in self.removed:
            lines.append(f"  - {item['name']}  {item['url']}")
        for item in self.renamed:
            lines.append(f"  ~ {item['url']}: '{item['was']}' is now '{item['now']}'")
        for item in self.changed:
            percent = round(float(item.get("diff") or 0) * 100)
            lines.append(f"  ! {item['name']}  changed ({percent}% different)  {item['url']}")
        lines.append(
            f"{len(self.added)} new, {len(self.removed)} gone, {len(self.renamed)} renamed, "
            f"{len(self.changed)} changed, {self.unchanged} unchanged"
            + (f", {self.unknown} could not be compared." if self.unknown else ".")
        )
        return "\n".join(lines)


def _read_bytes(path: str) -> bytes | None:
    """The screenshot at ``path``, or ``None`` when it is gone or unreadable."""
    if not path:
        return None
    try:
        return Path(path).read_bytes()
    except OSError:  # pragma: no cover - a pruned folder is normal
        return None


def _same_screen(previous: CrawledPage, current: CrawledPage) -> tuple[float | None, bool]:
    """``(ratio, known)``: how different the two screenshots look.

    ``(None, False)`` means "cannot tell" (a missing file, no Pillow); the caller
    reports that as changed, because a false alarm beats a silent edit.
    """
    from app.core import imagediff

    before = _read_bytes(previous.file_path)
    after = _read_bytes(current.file_path)
    if before is None or after is None:
        return None, False
    try:
        return imagediff.diff_ratio(before, after), True
    except Exception:  # noqa: BLE001 - a broken image is not a crash
        return None, False


def compare_reports(
    previous: CrawlReport,
    current: CrawlReport,
    changed_threshold: float | None = DEFAULT_CHANGED_THRESHOLD,
) -> CrawlDiff:
    """Diff two crawl reports: what is new, gone, renamed or changed.

    ``changed_threshold`` is a dHash ratio (0..1) - the same measure the change
    detector uses; ``None`` skips the pixel comparison, which is what a caller
    without the images (a test, an API) wants.
    """
    diff = CrawlDiff(threshold=float(changed_threshold or 0.0))
    old_by_url: dict[str, CrawledPage] = {page.url: page for page in previous.pages}
    new_by_url = {page.url: page for page in current.pages}

    for url, page in new_by_url.items():
        if url not in old_by_url:
            diff.added.append(page)
    for url, old_page in old_by_url.items():
        if url not in new_by_url:
            diff.removed.append(
                {"name": old_page.name, "url": url, "folder": str(previous.output_dir)}
            )
            continue
        page = new_by_url[url]
        if old_page.name != page.name:
            diff.renamed.append({"url": url, "was": old_page.name, "now": page.name})
        if changed_threshold is None:
            # Address-only diff: the caller has the reports but not the pixels.
            diff.unchanged += 1
            continue
        ratio, known = _same_screen(old_page, page)
        if not known:
            # A pruned screenshot is not evidence of a change: say so instead of
            # crying wolf on every run after the retention window passes.
            diff.unknown += 1
            continue
        if float(ratio or 0) > float(changed_threshold):
            diff.changed.append(
                {
                    "url": url,
                    "name": page.name,
                    "diff": round(float(ratio or 0), 4),
                    "before": old_page.file_path,
                    "after": page.file_path,
                }
            )
        else:
            diff.unchanged += 1
    return diff


def load_report(path: str | Path) -> CrawlReport:
    """Read back a ``report.json`` written by :func:`write_crawl_report`."""
    import json

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a crawl report")
    report = CrawlReport(
        url=str(data.get("url") or ""),
        output_dir=str(data.get("output_dir") or ""),
        generated_at=str(data.get("generated_at") or ""),
        clicks=int(data.get("clicks") or 0),
        stopped=bool(data.get("stopped")),
        options=dict(data.get("options") or {}),
        skipped=list(data.get("skipped") or []),
        errors=[str(item) for item in (data.get("errors") or [])],
    )
    for raw in data.get("pages") or []:
        if not isinstance(raw, dict):
            continue
        report.pages.append(
            CrawledPage(
                index=int(raw.get("index") or 0),
                name=str(raw.get("name") or ""),
                url=str(raw.get("url") or ""),
                title=str(raw.get("title") or ""),
                file_path=str(raw.get("file") or ""),
                depth=int(raw.get("depth") or 0),
                path=[str(item) for item in (raw.get("path") or [])],
                signature=str(raw.get("signature") or ""),
                elapsed_ms=int(raw.get("elapsed_ms") or 0),
                error=str(raw.get("error") or ""),
            )
        )
    return report


def find_previous_report(output_dir: str | Path, exclude: str | Path | None = None) -> Path | None:
    """The newest ``crawl-*/report.json`` in ``output_dir``, minus the one just made.

    Newest is judged by the folder name, which carries the timestamp, so a folder
    that was copied or restored keeps its place in the order.
    """
    root = Path(output_dir)
    skip = Path(exclude).resolve() if exclude else None
    best: Path | None = None
    for candidate in sorted(root.glob("crawl-*/report.json")):
        if skip is not None and candidate.parent.resolve() == skip:
            continue
        if best is None or candidate.parent.name > best.parent.name:
            best = candidate
    return best


def adaptive_rate_limit(page_element_count: int) -> float:
    return 0.5 + (page_element_count / 100.0)

def crawl_folder(report: CrawlReport) -> Path | None:
    """The folder a report's screenshots live in (``None`` when nothing was saved)."""
    for page in report.pages:
        if page.file_path:
            return Path(page.file_path).parent
    return None
