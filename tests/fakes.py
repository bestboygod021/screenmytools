"""A miniature stand-in for Playwright's sync API.

The fake records every call the engine makes, so tests can assert on the
*sequence of automation steps* (goto -> networkidle -> login -> scroll ->
screenshot) without launching a real browser.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # Pillow is a runtime dependency; the fakes need it to build real images.
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]


class FakeTimeoutError(Exception):
    """Mimics ``playwright._impl._errors.TimeoutError`` by class *name*."""


# ``CaptureEngine._is_timeout`` looks at the class name, so give it the real one.
FakeTimeoutError.__name__ = "TimeoutError"


def png_bytes(width: int, height: int, color: tuple[int, int, int] = (30, 60, 120)) -> bytes:
    """Create a genuine PNG of the requested size."""
    if Image is None:  # pragma: no cover
        return b"\x89PNG\r\n\x1a\nfake"
    image = Image.new("RGB", (max(1, width), max(1, height)), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@dataclass
class PageScript:
    """Per-URL behaviour the fake page should simulate."""

    http_status: int = 200
    page_width: int = 1440
    page_height: int = 3000
    goto_error: Exception | None = None
    goto_timeout_first_attempt: bool = False
    networkidle_error: Exception | None = None
    has_login_form: bool = False
    full_page_error: Exception | None = None
    #: Raised by ``add_style_tag`` (a page whose CSP refuses injected CSS).
    style_error: Exception | None = None
    #: The file name ``expect_download`` hands over (empty uses the default).
    download_name: str = ""
    #: Selectors that only exist *after* a login form has been submitted. The fake
    #: uses it to play "the session expired, sign in and the page appears".
    login_gated_selectors: str = ""
    #: Set when a click lands on a page that has a login form (the fake's stand-in
    #: for "the form was submitted").
    logged_in: bool = False
    #: Every download the fake browser handed over, in order.
    downloads: list[Any] = field(default_factory=list)
    #: Events the injected recorder script "saw": returned by the drain call, and
    #: the queue is emptied afterwards, exactly like the real page.
    recorded_events: list[dict[str, Any]] = field(default_factory=list)
    #: Raised by every ``evaluate`` whose script mentions this text (a closed
    #: window, a dead browser); empty means "never fail".
    evaluate_error_for: str = ""
    evaluate_error: Exception | None = None
    #: Fail the first N ``goto`` calls with a non-timeout error, then succeed.
    #: A timeout is deliberately avoided here: the engine legitimately retries a
    #: navigation timeout once with ``wait_until='commit'``.
    fail_first_n_gotos: int = 0
    #: Drop the browser connection once this many screenshots have been taken
    #: (-1 disables). Used to exercise the health watchdog / auto-restart.
    disconnect_after: int = -1
    #: When set, the page behaves like a real (tiny) site: buttons navigate,
    #: modals open, and the discovery script finds them. Used by the journey and
    #: crawler tests; the plain ``has_login_form`` behaviour is used otherwise.
    site: SiteScript | None = None
    _goto_calls: int = 0


@dataclass
class FakeModal:
    """A dialog a click can open: it shows up in the DOM and hides the page."""

    title: str
    selector: str = ".modal"
    body: str = "Modal body"


@dataclass
class FakeWidget:
    """One clickable thing the discovery script can find on a fake page."""

    text: str
    kind: str = "button"
    href: str = ""
    opens: FakeModal | None = None
    link: str = ""  # navigating here after the click
    dangerous: bool = False  # only the crawler cares; kept for realism

    def to_element(self, marker: str) -> dict[str, Any]:
        return {
            "marker": marker,
            "tag": "a" if self.kind == "link" else "button",
            "role": "tab" if self.kind == "tab" else "",
            "kind": self.kind,
            "text": self.text,
            "href": self.href or self.link,  # a link has an href in a real DOM
            "in_viewport": True,
        }

    @property
    def selector(self) -> str:
        return f'button:has-text("{self.text}")'


@dataclass
class FakeSite:
    """A tiny site model: pages made of widgets, so a crawl has somewhere to go.

    ``pages`` maps a URL (or path) to ``(title, [widgets], body)``. A widget may
    open a modal, navigate to another page, or do nothing but change the body -
    exactly the three things the crawler has to tell apart.
    """

    pages: dict[str, tuple[str, list[FakeWidget]]] = field(default_factory=dict)
    body: str = "A fake site"

    def resolve(self, href: str) -> str:
        """A click's href as a page key (``/pricing`` stays ``/pricing``)."""
        return href.rstrip("/") or "/"

    def widgets_for(self, url: str) -> list[FakeWidget]:
        for key, (_title, widgets) in self.pages.items():
            if url.endswith(key) or (key == "/" and url.rstrip("/").endswith("example.com")):
                return widgets
        return []

    def title_for(self, url: str) -> str:
        for key, (title, _widgets) in self.pages.items():
            if url.endswith(key) or (key == "/" and url.rstrip("/").endswith("example.com")):
                return title
        return "Untitled"


@dataclass
class SiteScript:
    """The state of every fake page in one browser session (shared by contexts)."""

    site: FakeSite
    url: str = "https://example.com/"
    title: str = "Home"
    widgets: list[FakeWidget] = field(default_factory=list)
    marker: str = ""  # the widget the next marker will belong to
    open_modal: FakeModal | None = None
    dom: list[tuple[str, int, int]] = field(default_factory=list)  # (marker, w, h)
    links: dict[str, str] = field(default_factory=dict)  # selector -> url
    modal_selector: str = ""
    modal_title: str = ""
    sign_in: bool = False
    discover: int = 0
    evaluations: list[str] = field(default_factory=list)
    #: Selectors that exist as fillable fields (``#q``, ``#email``, ...), so a
    #: journey's ``fill``/``press`` steps have something to type into.
    inputs: set[str] = field(default_factory=set)

    # -- building a page ----------------------------------------------------
    @classmethod
    def from_site(cls, site: FakeSite, url: str = "https://example.com/") -> SiteScript:
        script = cls(site=site, url=url)
        script.go(url)
        return script

    def go(self, url: str) -> None:
        """Move to a page of the site (a click or a goto lands here)."""
        self.url = url
        self.widgets = list(self.site.widgets_for(url))
        self.title = self.site.title_for(url)
        self.open_modal = None
        self.modal_selector = ""
        self.modal_title = ""
        self.sign_in = False
        self.dom = []
        self.links = {}
        self.marker = ""
        for index, widget in enumerate(self.widgets):
            marker = f"c{index}"
            self.dom.append((marker, 120, 40))
            self.links[widget.selector] = marker
            if widget.link:
                self.links[f'text="{widget.text}"'] = marker

    # -- what a click does --------------------------------------------------
    def click_selector(self, selector: str) -> None:
        wanted = selector.strip()
        marker = self.links.get(selector) or self.links.get(wanted) or self.marker_of(wanted)
        widget = self.widget_for_marker(marker)
        if widget is None:
            return
        if widget.opens is not None:
            self.open_modal = widget.opens
            self.modal_selector = widget.opens.selector
            self.modal_title = widget.opens.title
        elif widget.link:
            self.go(widget.link)
        elif widget.text.lower() in ("sign in", "login", "log in"):
            self.sign_in = True

    def marker_of(self, selector: str) -> str:
        """The marker a click selector refers to (``[data-...="c1"]`` -> ``c1``)."""
        if 'data-capture-bot-target="' not in selector:
            return ""
        return selector.split('data-capture-bot-target="', 1)[1].split('"', 1)[0]

    def widget_for_marker(self, marker: str) -> FakeWidget | None:
        for index, (candidate, _w, _h) in enumerate(self.dom):
            if candidate == marker and index < len(self.widgets):
                return self.widgets[index]
        return None

    def signature_body(self) -> str:
        parts = [
            self.body if hasattr(self, "body") else "",
            self.title,
            " ".join(w.text for w in self.widgets),
        ]
        if self.open_modal is not None:
            parts.append(self.open_modal.title)
        if self.sign_in:
            parts.append("signed in dashboard")
        return " ".join(parts)


@dataclass
class CallLog:
    """Everything the fake browser was asked to do."""

    launched: list[dict[str, Any]] = field(default_factory=list)
    contexts: list[dict[str, Any]] = field(default_factory=list)
    gotos: list[dict[str, Any]] = field(default_factory=list)
    wait_states: list[tuple[str, int]] = field(default_factory=list)
    evaluates: list[str] = field(default_factory=list)
    fills: list[tuple[str, str]] = field(default_factory=list)
    clicks: list[str] = field(default_factory=list)
    actions: list[tuple[str, str, str]] = field(default_factory=list)
    screenshots: list[dict[str, Any]] = field(default_factory=list)
    closed_pages: int = 0
    closed_contexts: int = 0
    style_tags: list[str] = field(default_factory=list)
    #: The live fake browsers, so a test can drive the tabs it created.
    browsers: list[Any] = field(default_factory=list)
    browser_closed: bool = False
    saved_states: list[str | None] = field(default_factory=list)


class FakeLocator:
    """One element. With a :class:`SiteScript` it also *does* things."""

    def __init__(
        self,
        selector: str,
        visible: bool,
        calls: CallLog,
        site: SiteScript | None = None,
        script: PageScript | None = None,
    ) -> None:
        self.selector = selector
        self._visible = visible
        self._calls = calls
        self._site = site
        self._script = script

    def count(self) -> int:
        return 1 if self._visible else 0

    def is_visible(self) -> bool:
        return self._visible

    def _act(self, name: str, *args) -> None:
        if self._site is None:
            return
        if not self._visible:
            raise FakeTimeoutError(f"{name}: {self.selector} is not visible")

    def fill(self, value: str, timeout: float = 0) -> None:
        self._calls.fills.append((self.selector, value))
        self._calls.actions.append(("fill", self.selector, value))
        self._act("fill", value)

    def click(self, timeout: float = 0) -> None:
        self._calls.clicks.append(self.selector)
        self._calls.actions.append(("click", self.selector, ""))
        if self._site is None:
            if self._script is not None and self._script.has_login_form:
                # A page with a login form: clicking the submit button signs in.
                self._script.logged_in = True
            return
        if not self._visible:
            raise FakeTimeoutError(f"click: {self.selector} is not visible")
        self._site.click_selector(self.selector)

    def press(self, key: str, timeout: float = 0) -> None:
        self._calls.clicks.append(f"{self.selector}:{key}")
        self._calls.actions.append(("press", self.selector, key))
        self._act("press", key)

    def hover(self, timeout: float = 0) -> None:
        self._calls.actions.append(("hover", self.selector, ""))

    def select_option(self, value: str, timeout: float = 0) -> None:
        self._calls.actions.append(("select", self.selector, value))
        self._act("select", value)

    def check(self, timeout: float = 0) -> None:
        self._calls.actions.append(("check", self.selector, ""))
        self._act("check")

    def wait_for(self, state: str = "visible", timeout: float = 0) -> None:
        self._calls.actions.append(("wait_for", self.selector, state))
        if state == "hidden":
            if self._visible:
                raise FakeTimeoutError(f"wait_for(hidden): {self.selector} is still visible")
        elif not self._visible:
            raise FakeTimeoutError(f"wait_for({state}): {self.selector} never appeared")


class FakeLocatorSet:
    """Returned by ``page.locator(selector)``; exposes ``.first``."""

    def __init__(
        self,
        selector: str,
        visible: bool,
        calls: CallLog,
        site: SiteScript | None = None,
        script: PageScript | None = None,
    ) -> None:
        self.first = FakeLocator(selector, visible, calls, site, script)


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class FakeMouse:
    """``page.mouse.wheel(...)``: recorded, never surprising."""

    def __init__(self, calls: CallLog) -> None:
        self._calls = calls

    def wheel(self, dx: float, dy: float) -> None:
        self._calls.actions.append(("wheel", f"{dx:.0f}", f"{dy:.0f}"))


class FakeKeyboard:
    """``page.keyboard``: the keys a journey sends to the page itself."""

    def __init__(self, calls: CallLog) -> None:
        self._calls = calls

    def press(self, key: str, timeout: float = 0) -> None:
        self._calls.actions.append(("key", "", key))


class FakeDownload:
    """A file Playwright handed over (``expect_download`` / the download event)."""

    def __init__(self, filename: str = "report.pdf", payload: bytes = b"%PDF-fake") -> None:
        self.suggested_filename = filename
        self.payload = payload
        self.saved_as: str | None = None
        self.url = f"https://example.com/files/{filename}"
        self.failure: str | None = None

    def save_as(self, path: str) -> None:
        if self.failure:
            raise RuntimeError(self.failure)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(self.payload)
        self.saved_as = path


class FakeDownloadInfo:
    """What ``with page.expect_download() as info`` yields."""

    def __init__(self, download: FakeDownload) -> None:
        self.value = download

    def __enter__(self) -> FakeDownloadInfo:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakePage:
    def __init__(
        self, context: FakeContext, script: PageScript, calls: CallLog, attempt: int
    ) -> None:
        self._context = context
        self._script = script
        self._calls = calls
        self._attempt = attempt
        self.default_timeout = 0
        self.closed = False
        self._handlers: dict[str, list] = {}
        self.mouse = FakeMouse(calls)
        self.keyboard = FakeKeyboard(calls)
        # A fake site is shared between the contexts of one browser session, the
        # way a real site is: the state lives in the server, not in the tab.
        self._site = script.site
        self._url = "about:blank"
        self._history: list[str] = []

    @property
    def url(self) -> str:
        """The tab's address: on a fake site this mirrors the shared site state."""
        if self._site is not None:
            return self._site.url
        return self._url

    # -- event listeners (console/pageerror/requestfailed) ----------------
    def on(self, event: str, handler) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def emit(self, event: str, arg=None) -> None:
        for handler in self._handlers.get(event, []):
            handler(arg)

    # -- navigation -------------------------------------------------------
    def keyboard_press(self, key: str) -> None:
        """``page.keyboard.press`` (also reachable as ``page.keyboard.press``)."""
        self._calls.actions.append(("key", "", key))

    def expect_download(self, timeout: float = 0) -> FakeDownloadInfo:
        """``with page.expect_download() as info:`` - hands over what the site sends."""
        self._calls.actions.append(("expect_download", "", str(timeout)))
        download = FakeDownload(self._script.download_name or "report.pdf")
        self._script.downloads.append(download)
        return FakeDownloadInfo(download)

    def add_style_tag(self, content: str = "", path: str | None = None) -> None:
        """``page.add_style_tag``: the engine uses it to hide noisy elements."""
        if self._script.style_error is not None:
            raise self._script.style_error
        self._calls.style_tags.append(content)
        if path:
            Path(path).read_text(encoding="utf-8")  # a real file, or an error

    def set_default_timeout(self, timeout: float) -> None:
        self.default_timeout = timeout

    def set_default_navigation_timeout(self, timeout: float) -> None:
        self.default_timeout = timeout

    def goto(self, url: str, wait_until: str = "load", timeout: float = 0) -> FakeResponse:
        self._calls.gotos.append({"url": url, "wait_until": wait_until, "timeout": timeout})

        if (
            self._script.goto_timeout_first_attempt
            and self._attempt == 1
            and wait_until != "commit"
        ):
            raise FakeTimeoutError("Timeout 60000ms exceeded while navigating")
        self._script._goto_calls += 1
        if self._script._goto_calls <= self._script.fail_first_n_gotos:
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED at https://broken.example")
        if self._script.goto_error is not None:
            raise self._script.goto_error
        self._navigate(url)
        return FakeResponse(self._script.http_status)

    # -- page identity / history -------------------------------------------
    def _navigate(self, url: str) -> None:
        """Follow a link or a goto: this is what changes *what is on the page*."""
        self._history.append(self.url)
        self._url = str(url)
        if self._site is not None:
            self._site.go(self._url)

    def title(self) -> str:
        if self._site is not None:
            return self._site.title
        return "Fake page"

    def go_back(self, timeout: float = 0) -> None:
        self._calls.actions.append(("back", "", ""))
        if self._history:
            previous = self._history.pop()
            self._url = previous
            if self._site is not None:
                self._site.go(previous)

    def reload(self, timeout: float = 0) -> None:
        self._calls.actions.append(("reload", "", ""))
        if self._site is not None:
            self._site.go(self._site.url)

    def wait_for_load_state(self, state: str = "load", timeout: float = 0) -> None:
        self._calls.wait_states.append((state, int(timeout)))
        if state == "networkidle" and self._script.networkidle_error is not None:
            raise self._script.networkidle_error

    def wait_for_timeout(self, timeout: float) -> None:
        return None

    # -- DOM --------------------------------------------------------------
    def evaluate(self, script: str, arg: Any = None) -> Any:
        self._calls.evaluates.append(script)
        if (
            self._script.evaluate_error is not None
            and self._script.evaluate_error_for
            and self._script.evaluate_error_for in script
        ):
            raise self._script.evaluate_error
        if "Math.max" in script:
            return {"width": self._script.page_width, "height": self._script.page_height}
        if ".drain()" in script:  # the recorder's read call, not its installer
            events = list(self._script.recorded_events)
            self._script.recorded_events = []  # drained
            return events
        if "scrollHeight" in script:
            return self._script.page_height
        if self._site is not None:
            self._site.evaluations.append(script)
            if "__capture_bot_signature__" in script:
                return {
                    "url": self.url,
                    "title": self._site.title,
                    "text": self._site.signature_body(),
                    "__capture_bot_signature__": True,
                }
            if "data-capture-bot-target" in script and "querySelectorAll" in script:
                if "removeAttribute" in script:  # clear_element_tags
                    return None
                return [
                    self._site.widget_for_marker(marker).to_element(marker)
                    for marker, _width, _height in self._site.dom
                    if self._site.widget_for_marker(marker) is not None
                ]
            if "scrollTo" in script:
                return None
        return None

    def locator(self, selector: str) -> FakeLocatorSet:
        return FakeLocatorSet(
            selector, self._is_visible(selector), self._calls, self._site, self._script
        )

    def _is_visible(self, selector: str) -> bool:
        """Which selector exists *right now* (the site decides when there is one)."""
        gated = {
            item.strip()
            for item in str(self._script.login_gated_selectors or "").split(",")
            if item.strip()
        }
        if selector.strip() in gated:
            return bool(self._script.logged_in)
        if self._site is None:
            return bool(self._script.has_login_form)
        current = selector.strip()
        if not current:
            return False
        if current in self._site.links:
            return True
        if current.startswith("button:has-text(") or current.startswith("text="):
            wanted = current.split('"')[1].lower() if '"' in current else ""
            return any(w.text.lower() == wanted for w in self._site.widgets)
        marker = self._site.marker_of(current)
        if marker:
            return any(candidate == marker for candidate, _w, _h in self._site.dom)
        if current in self._site.inputs:
            return True
        if self._site.open_modal is not None:
            return current in (self._site.open_modal.selector, self._site.modal_selector)
        if self._script.has_login_form:
            return True  # the engine's login selectors keep working inside a site
        return False

    # -- capture ----------------------------------------------------------
    def screenshot(self, full_page: bool = False, clip: dict | None = None, **kwargs) -> bytes:
        self._calls.screenshots.append({"full_page": full_page, "clip": clip, **kwargs})

        if (
            self._script.disconnect_after >= 0
            and len(self._calls.screenshots) >= self._script.disconnect_after
        ):
            self._context._browser.disconnect()

        scale = float(self._context.kwargs.get("device_scale_factor", 1))
        if clip is not None:
            return png_bytes(int(clip["width"] * scale), int(clip["height"] * scale))

        if full_page:
            if self._script.full_page_error is not None:
                raise self._script.full_page_error
            return png_bytes(
                int(self._script.page_width * scale), int(self._script.page_height * scale)
            )

        viewport = self._context.kwargs.get("viewport", {"width": 1280, "height": 720})
        return png_bytes(int(viewport["width"] * scale), int(viewport["height"] * scale))

    def close(self) -> None:
        self.closed = True
        self._calls.closed_pages += 1


class FakeContext:
    def __init__(
        self, browser: FakeBrowser, kwargs: dict[str, Any], script: PageScript, calls: CallLog
    ) -> None:
        self.kwargs = kwargs
        self._browser = browser
        self._script = script
        self._calls = calls
        self.pages: list[FakePage] = []

    def new_page(self) -> FakePage:
        page = FakePage(self, self._script, self._calls, attempt=len(self.pages) + 1)
        self.pages.append(page)
        return page

    def close(self) -> None:
        self._calls.closed_contexts += 1

    def __enter__(self) -> FakeContext:
        return self

    def __exit__(self, *exc: object) -> None:
        # Playwright's sync contexts are context managers too; the engine closes
        # them either way, so the fake has to support both.
        self.close()

    def storage_state(self, path: str | None = None) -> dict[str, Any]:
        """Remember-me: the fake session is a cookie and a file name."""
        state = {"cookies": [{"name": "session", "value": "fake"}], "origins": []}
        self._calls.saved_states.append(path)
        if path:
            Path(path).write_text(json.dumps(state), encoding="utf-8")
        return state


class FakeBrowser:
    def __init__(self, script: PageScript, calls: CallLog) -> None:
        self._script = script
        self._calls = calls
        self.contexts: list[FakeContext] = []
        self._connected = True

    def is_connected(self) -> bool:
        return self._connected

    def disconnect(self) -> None:
        """Simulate the browser process dying / losing its connection."""
        self._connected = False

    def new_context(self, **kwargs) -> FakeContext:
        self._calls.contexts.append(kwargs)
        context = FakeContext(self, kwargs, self._script, self._calls)
        self.contexts.append(context)
        return context

    def close(self) -> None:
        self._calls.browser_closed = True


class FakeBrowserType:
    def __init__(
        self, name: str, script: PageScript, calls: CallLog, launch_error: Exception | None = None
    ) -> None:
        self.name = name
        self.executable_path = "/fake/ms-playwright/chromium/chrome"
        self._script = script
        self._calls = calls
        self._launch_error = launch_error

    def launch(
        self,
        headless: bool = True,
        args: list[str] | None = None,
        proxy: dict[str, Any] | None = None,
    ) -> FakeBrowser:
        self._calls.launched.append(
            {"browser": self.name, "headless": headless, "args": list(args or []), "proxy": proxy}
        )
        if self._launch_error is not None:
            raise self._launch_error
        browser = FakeBrowser(self._script, self._calls)
        self._calls.browsers.append(browser)
        return browser


class FakePlaywright:
    """Stands in for the object returned by ``sync_playwright()``."""

    def __init__(
        self,
        script: PageScript | None = None,
        calls: CallLog | None = None,
        launch_error: Exception | None = None,
    ) -> None:
        self.script = script or PageScript()
        self.calls = calls or CallLog()
        self.chromium = FakeBrowserType("chromium", self.script, self.calls, launch_error)
        self.firefox = FakeBrowserType("firefox", self.script, self.calls, launch_error)
        self.webkit = FakeBrowserType("webkit", self.script, self.calls, launch_error)

    def __enter__(self) -> FakePlaywright:
        return self

    def __exit__(self, *exc_info) -> None:
        return None


def make_factory(
    script: PageScript | None = None,
    calls: CallLog | None = None,
    launch_error: Exception | None = None,
):
    """Return a zero-argument callable usable as ``CaptureEngine(playwright_factory=...)``."""
    instance = FakePlaywright(script, calls, launch_error)

    def factory():
        return instance

    factory.instance = instance  # type: ignore[attr-defined]
    return factory
