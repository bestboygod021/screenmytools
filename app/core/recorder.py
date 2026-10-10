"""Recording: turn a real browsing session into a journey.

Writing the steps by hand means knowing the selectors before you start, which is
backwards - you know the *page*, not its HTML. So the app can watch a browser
window instead: every click, every typed value and every navigation becomes one
line of the same short language the Clicks box accepts, and the recording ends
with a `capture` step for each screen you looked at.

Qt-free on purpose: the browser side is injected (the tests pass a fake), the
translation is pure functions, so both halves are testable without a display.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.engine import BrowserNotInstalledError
from app.core.journey import slug_for_text

#: The console function the app injects. It must keep a queue of the events it
#: saw and return them to the caller; a list works, and so does anything with the
#: same three methods.
RECORDER_SCRIPT = """
() => {
  if (window.__captureBotRecorder) return true;
  const queue = [];
  const push = (event) => queue.push(Object.assign({ t: Date.now() }, event));
  const label = (el) => (
    (el.getAttribute('aria-label') || el.innerText || el.value || el.name || el.tagName || '')
      .trim().replace(/\\s+/g, ' ').slice(0, 80)
  );
  const path = (el) => {
    if (!el || el.nodeType !== 1) return '';
    if (el.id) return '#' + el.id;
    const attr = el.getAttribute('data-testid') || el.getAttribute('name');
    if (attr) return el.tagName.toLowerCase() + '[' + (el.getAttribute('data-testid') ? 'data-testid' : 'name') + '="' + attr + '"]';
    return el.tagName.toLowerCase();
  };
  document.addEventListener('click', (event) => {
    const el = event.target.closest('a, button, [role=button], [role=tab], input, summary, label') || event.target;
    push({ kind: 'click', tag: (el.tagName || '').toLowerCase(), selector: path(el), text: label(el) });
  }, true);
  document.addEventListener('change', (event) => {
    const el = event.target;
    if (!el || !('value' in el)) return;
    const kind = el.tagName === 'SELECT' ? 'select' : (el.type === 'checkbox' ? 'check' : 'fill');
    push({ kind: kind, tag: 'input', selector: path(el), text: label(el), value: el.value });
  }, true);
  document.addEventListener('submit', () => push({ kind: 'submit', tag: 'form', selector: 'form', text: '' }), true);

  // Scrolling and pausing are part of a session on a modern page: a list only
  // renders when it is scrolled into view, and a "wait for it to appear" pause is
  // something the person did on purpose. Both are recorded so the replay can be
  // the same session, not just the same clicks.
  let lastY = window.scrollY;
  let lastScroll = 0;
  const onScroll = () => {
    const now = Date.now();
    if (now - lastScroll < 400) return;  // one step per pause, not per pixel
    lastScroll = now;
    const direction = window.scrollY >= lastY ? 'down' : 'up';
    const moved = Math.abs(window.scrollY - lastY);
    lastY = window.scrollY;
    if (moved > 40) push({ kind: 'scroll', tag: 'window', value: direction });
  };
  window.addEventListener('scroll', onScroll, true);
  // Keys worth replaying: Enter outside a form (a search box, a dialog), Escape and
  // Tab, and any shortcut with Ctrl/Alt/Meta held - Ctrl+K, Alt+Left. Typing is
  // already recorded as a fill; a bare letter is not a step.
  const combo = (event) => {
    const parts = [];
    if (event.ctrlKey) parts.push('Control');
    if (event.metaKey) parts.push('Meta');
    if (event.altKey) parts.push('Alt');
    if (event.shiftKey && parts.length) parts.push('Shift');
    return parts.length ? parts.concat([event.key.toUpperCase()]).join('+') : '';
  };
  window.addEventListener('keydown', (event) => {
    const shortcut = combo(event);
    if (shortcut) { push({ kind: 'press', key: shortcut, text: shortcut }); return; }
    if ((event.key === 'Enter' || event.key === 'Escape' || event.key === 'Tab')
        && !event.target.closest('form') && event.key !== 'Tab') {
      push({ kind: 'press', key: event.key });
    }
  }, true);
  // A click on a link that downloads a file leaves no other trace in the DOM.
  document.addEventListener('click', (event) => {
    const link = event.target.closest('a[download], a[href$=".pdf"], a[href$=".csv"], a[href$=".zip"]');
    if (link) push({ kind: 'download', selector: path(link), text: label(link) });
  }, true);
  window.__captureBotRecorder = {
    drain: () => queue.splice(0, queue.length),
    stop: () => { window.__captureBotRecorder = null; return true; }
  };
  return true;
}
"""


@dataclass
class Event:
    """One thing the person did, as the page saw it."""

    #: click / fill / select / check / submit / scroll / press / goto / capture
    kind: str = ""
    tag: str = ""
    selector: str = ""
    text: str = ""
    value: str = ""
    url: str = ""
    key: str = ""
    #: milliseconds since the epoch, as the page saw it (``t`` in the event)
    t: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in (
                ("kind", self.kind),
                ("tag", self.tag),
                ("selector", self.selector),
                ("text", self.text),
                ("value", self.value),
                ("url", self.url),
                ("key", self.key),
                ("t", self.t),
            )
            if value
        }


@dataclass
class Recording:
    """A session the user walked through: the events, and the steps they imply."""

    url: str = ""
    events: list[Event] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    captures: int = 0
    #: Files the session downloaded, saved during the recording (full paths).
    downloads: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.events)

    def text(self) -> str:
        """The recording as the step language, ready to paste into the app."""
        return "\n".join(self.steps)

    def summary(self) -> str:
        if not self.found:
            return "Nothing was recorded."
        saved = f", {len(self.downloads)} download(s) saved" if self.downloads else ""
        return (
            f"Recorded {len(self.events)} action(s) on {self.url}: "
            f"{len(self.steps)} step(s), {self.captures} capture(s){saved}."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "steps": list(self.steps),
            "captures": self.captures,
            "downloads": list(self.downloads),
            "events": [event.to_dict() for event in self.events],
        }


# --------------------------------------------------------------------------- #
# translating what the page saw into the step language
# --------------------------------------------------------------------------- #
#: Words that make an element a nicer reference than a generated CSS path.
_WORD = re.compile(r"^[A-Za-z\u0600-\u06FF][A-Za-z0-9\u0600-\u06FF _-]{1,40}$")

#: Labels that are structural rather than meaningful, so text beats them.
_NOISE = ("nav", "div", "span", "a", "p", "li", "section")

#: A pause shorter than this is just the time between two clicks, not a decision.
WAIT_FLOOR_MS = 700
#: A pause longer than this is the person reading their inbox, not the page.
WAIT_CEILING_MS = 20_000


def _quote(text: str) -> str:
    return '"' + str(text).replace('"', '\\"') + '"'


def reference(event: Event) -> str:
    """How the step should name the element it acted on.

    A stable-looking CSS selector wins (an id or a ``name``/``data-testid``
    attribute survives a redesign); otherwise the visible label is used, because
    "click \\"Sign in\\"" keeps working long after a generated path like
    ``div > div:nth-child(3) > button`` has rotted.
    """
    selector = (event.selector or "").strip()
    text = (event.text or "").strip()
    if selector.startswith("#") or "[" in selector:
        return selector
    if text and _WORD.match(text):
        return _quote(text)
    if selector and selector.split(" ")[0].lower() not in _NOISE and selector != event.tag:
        return selector
    if selector:
        return selector
    return _quote(text) if text else ""


def _nice_hint(hint: str) -> str:
    """A file-name-friendly name for the screen a step landed on.

    ``#filters`` is the filters screen, ``input[name="go"]`` is the go screen: the
    interesting part of a reference is the word a person chose, not the syntax
    around it.
    """
    text = str(hint or "").strip()
    if '="' in text:
        text = text.split('="', 1)[1].rsplit('"', 1)[0]
    return text.lstrip("#.").strip()


def _pause_before(previous: Event, event: Event) -> str:
    """``wait <ms>`` when the person paused between two actions, else ``""``.

    Only real gaps count: the floor keeps the noise of ordinary typing out of the
    recording, the ceiling keeps a coffee break from becoming a 40-minute wait.
    """
    try:
        gap = int(event.t) - int(previous.t)
    except (TypeError, ValueError):
        return ""
    if gap < WAIT_FLOOR_MS:
        return ""
    gap = min(gap, WAIT_CEILING_MS) - (min(gap, WAIT_CEILING_MS) % 100)
    return f"wait {gap}"


def steps_for(events: list[Event], capture_each: bool = True) -> list[str]:
    """Translate the recorded events into lines of the step language.

    ``capture_each`` adds a screenshot after every navigation or submitted form,
    which is what "photograph each screen I looked at" means in practice: the
    recording is the moment the user was *on* those screens.
    """
    steps: list[str] = []
    seen_labels: dict[str, int] = {}
    hint = ""  # the last thing that was touched, for the capture names
    last_click = ""  # the last *button*: the best name for a screen after a submit

    def add_capture(hint: str) -> None:
        if not capture_each:
            return
        slug = slug_for_text(_nice_hint(hint), "screen")
        seen_labels[slug] = seen_labels.get(slug, 0) + 1
        count = seen_labels[slug]
        name = slug if count == 1 else f"{slug}-{count}"
        steps.append(f"capture {name}")

    for position, event in enumerate(events):
        kind = event.kind
        hint = (event.text or event.selector or event.value or hint).strip() or hint
        pause = _pause_before(events[position - 1], event) if position else ""
        if pause and steps and not steps[-1].startswith("wait"):
            steps.append(pause)
        if kind == "click":
            target = reference(event)
            if not target:
                continue
            steps.append(f"click {target}")
            last_click = (event.text or event.selector or "").strip() or last_click
        elif kind == "fill":
            target = reference(event)
            if not target or not event.value:
                continue
            steps.append(f"fill {target} = {event.value}")
        elif kind == "select":
            target = reference(event)
            if not target or not event.value:
                continue
            steps.append(f"select {target} = {event.value} optional")
        elif kind == "check":
            target = reference(event)
            if target:
                steps.append(f"check {target} optional")
        elif kind == "scroll":
            direction = (event.value or "down").strip().lower()
            steps.append(
                f"scroll {direction}" if direction in ("down", "up", "bottom") else "scroll down"
            )
        elif kind == "press":
            steps.append(f"press {event.key or 'Enter'}")
        elif kind == "download":
            target = reference(event)
            if target:
                # The page reports the click *and* the download for the same link.
                # Keep the interesting one: the click fetched a file, so the step
                # that saves the file is what the replay needs.
                if steps and steps[-1] == f"click {target}":
                    steps.pop()
                steps.append(f"download {target}")
        elif kind == "new_tab":
            # A tab is not a step in this language: the honest replay of "opened a
            # link in another tab and looked at it" is to open that page.
            steps.append(f"goto {event.url}")
        elif kind == "submit":
            steps.append("press Enter optional")
            add_capture(last_click or hint or "form")
        elif kind == "goto":
            if not steps:
                add_capture(event.url or "landing")
            else:
                steps.append(f"goto {event.url}")
                add_capture(last_click or hint or "screen")
    while steps and steps[-1].startswith("wait"):
        steps.pop()  # a pause after the last action tells nobody anything
    if steps and not steps[-1].startswith("capture"):
        # The recording ends where the person stopped looking, so that screen is
        # worth a screenshot too - unless the last step already took one. An empty
        # recording stays empty: there is no screen to photograph.
        add_capture(last_click or hint or "final")
    return steps


def recording_from_events(
    url: str, events: list[Event], downloads: list[str] | None = None
) -> Recording:
    """Wrap the raw events into a :class:`Recording` with its steps."""
    from copy import deepcopy

    recording = Recording(url=url, events=deepcopy(list(events)))
    recording.downloads = list(downloads or [])
    recording.steps = steps_for(recording.events)
    recording.captures = sum(1 for step in recording.steps if step.startswith("capture"))
    return recording


# --------------------------------------------------------------------------- #
# the browser half
# --------------------------------------------------------------------------- #
class Recorder:
    """Drives a real browser window and collects what the page reports back.

    The engine is the same one a capture uses (so the user-agent, proxy, login
    and viewport are the ones the recording will be replayed with), and the
    console bridge is the only coupling: the injected script queues events and
    ``drain`` reads them, which keeps the Python side free of Playwright types.
    """

    def __init__(self, engine: Any, poll_seconds: float = 0.4, max_seconds: float = 900.0):
        self.engine = engine
        self.settings = engine.settings
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.max_seconds = max(1.0, float(max_seconds))

    def log(self, level, message: str) -> None:  # pragma: no cover - pass-through
        self.engine.log(level, message)

    @staticmethod
    def _collect(raw: Any, events: list[Event]) -> None:
        """Turn what the page queued into :class:`Event`s (ignoring strangers)."""
        for item in raw or []:
            if isinstance(item, dict):
                events.append(
                    Event(
                        **{
                            key: str(value)
                            for key, value in item.items()
                            if key in Event.__annotations__
                        }
                    )
                )

    def _adopt_new_tabs(
        self, context: Any, watched: list[Any], events: list[Event], downloads: list[str]
    ) -> None:
        """Notice tabs the person opened and start watching them too.

        A click that opens a tab is the one action whose screen is not in the tab
        you clicked in. Playwright keeps every page the context has, so a tab is
        noticed by comparing that list with the ones we already watch - and each
        new tab is reported once, as ``new_tab``, with its address.
        """
        try:
            pages = list(getattr(context, "pages", []) or [])
        except Exception:  # noqa: BLE001 - a context that will not list its tabs
            return
        for tab in pages:
            if tab in watched:
                continue
            watched.append(tab)
            try:
                tab.evaluate(RECORDER_SCRIPT)
            except Exception:  # noqa: BLE001 - a tab that is already gone
                continue
            self._watch_downloads(tab, downloads)
            events.append(Event(kind="new_tab", tag="tab", url=str(getattr(tab, "url", "") or "")))

    def _watch_downloads(self, page: Any, sink: list[str]) -> None:
        """Save whatever this tab downloads, into ``<out>/downloads/``.

        The step is written from what the page saw (a click on a download link);
        this is the file itself, which the replay would otherwise throw away.
        """
        listen = getattr(page, "on", None)
        if listen is None:  # pragma: no cover - a page without events
            return

        from app.core.engine import LogLevel
        from app.core.url_utils import download_name, unique_path

        def save(download: Any) -> None:
            name = str(getattr(download, "suggested_filename", "") or "") or "download"
            folder = Path(str(self.settings.output_dir or ".")) / "downloads"
            try:
                folder.mkdir(parents=True, exist_ok=True)
                target = unique_path(folder, download_name(name))
                download.save_as(str(target))
            except Exception as exc:  # noqa: BLE001 - a file we could not keep
                self.log(LogLevel.WARNING, f"The download '{name}' could not be saved: {exc}")
                return
            sink.append(str(target))
            self.log(LogLevel.SUCCESS, f"Recording: saved the download {target.name}")

        try:
            listen("download", save)
        except Exception:  # pragma: no cover - an engine without download events
            pass

    def record(self, url: str, on_tick: Any | None = None) -> Recording:
        """Open ``url`` headed, watch until stopped, and return the events seen.

        ``on_tick`` is called with ``(recording, seconds)`` every poll, so the app
        can show progress; returning ``True`` from it stops the recording, and so
        does closing the browser window or the time budget running out.
        """
        import time

        from app.core.engine import LogLevel

        events: list[Event] = []
        downloads: list[str] = []
        started = time.monotonic()
        playwright = browser = context = page = None
        watched: list[Any] = []  # every tab we have installed the script on
        try:
            playwright = self.engine._playwright_factory()
            playwright = playwright.__enter__()
            browser = self.engine._launch(playwright)
            context = browser.new_context(**self.engine._context_kwargs())
            page = context.new_page()
            timeout = int(self.settings.navigation_timeout_ms)
            page.set_default_timeout(timeout)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            page.evaluate(RECORDER_SCRIPT)
            self._watch_downloads(page, downloads)
            watched.append(page)
            self.log(LogLevel.INFO, "Recording: click around the page, then press Stop.")

            while True:
                if self.engine.stop_requested or (time.monotonic() - started) > self.max_seconds:
                    break
                self._adopt_new_tabs(context, watched, events, downloads)
                for tab in list(watched):
                    try:
                        raw = tab.evaluate(
                            "() => (window.__captureBotRecorder "
                            "? window.__captureBotRecorder.drain() : [])"
                        )
                    except Exception:  # noqa: BLE001 - a closed window is a stop
                        continue
                    self._collect(raw, events)
                if on_tick is not None:
                    if on_tick(
                        recording_from_events(url, events, downloads),
                        time.monotonic() - started,
                    ):
                        break
                time.sleep(self.poll_seconds)
        except BrowserNotInstalledError:
            # Not a recording problem: nothing can run until the browser is there.
            raise
        except Exception as exc:  # noqa: BLE001 - report it as a failed recording
            self.log(LogLevel.ERROR, f"Recording stopped: {self.engine._describe_exception(exc)}")
        finally:
            self.engine._close_context(context)
            self.engine._close_browser(browser)
            if playwright is not None:
                try:
                    playwright.__exit__(None, None, None)
                except Exception:  # pragma: no cover - closing is best effort
                    pass
        return recording_from_events(url, events, downloads)
