"""Walk a site the way a person does: click, fill, and capture each screen.

A **journey** is the click-path a person takes through a site - open the home
page, press "Sign in", open the Filters panel, switch to the Invoices tab - with
a screenshot for every screen that matters. One journey is a list of steps in a
TOML file::

    [login]
    url = "https://shop.example.com"

    [[login.steps]]
    action = "click"
    text = "Sign in"                 # or selector = "#login", or role/name

    [[login.steps]]
    action = "fill"
    selector = "#email"
    value = "${SHOP_EMAIL}"          # read from the environment, not the file

    [[login.steps]]
    action = "click"
    selector = "button[type=submit]"

    [[login.steps]]
    action = "capture"
    name = "dashboard"               # -> journeys/login/03-dashboard.png

The steps exist so that a capture can happen *after* something happened. The
whole reason to click a button is that the screen behind it is a different screen
- that is the picture the user wanted, and it is the one a plain URL list can
never take.

Actions
-------
``goto``      open a URL (``url``)
``click``     click an element (``selector`` or ``text`` or ``role``/``name``)
``fill``      type into a field (``value``)
``press``     press a key in an element, e.g. ``key = "Enter"``
``hover``     move the pointer over an element
``select``    choose an option in a ``<select>`` (``value``)
``check``     tick a checkbox/radio
``wait``      wait ``ms`` milliseconds
``wait_for``  wait for an element (``state = "visible"`` or ``"hidden"``)
``capture``   save a full-page screenshot (``name``, ``full = false`` for the fold)
``scroll``    scroll the page (``ms`` is ignored; ``value`` = pixels, default 1 view)
``back``      go back one history entry
``reload``    reload the page

Every step may carry ``optional = true`` (a missing "Cookie" button must not
fail the journey) and ``timeout`` (milliseconds).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import time
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.core.engine import BrowserNotInstalledError, LogLevel
from app.core.url_utils import download_name, sanitize_component, unique_path

#: The attribute :func:`describe_elements` uses to tag what it found. It is
#: removed again afterwards, so a captured page never shows it.
ELEMENT_ATTR = "data-capture-bot-target"

#: The JavaScript that finds the things a person could click. It is also used by
#: the crawler (``app/core/crawler.py``), which is why it lives here.
DISCOVERY_SCRIPT = """
() => {
  const selectors = [
    "a[href]", "button", "[role=button]", "[role=tab]", "[role=menuitem]",
    "[role=link]", "[role=checkbox]", "[role=switch]", "summary",
    "input[type=button]", "input[type=submit]", "[onclick]"
  ];
  const seen = new Set();
  const found = [];
  let index = 0;
  for (const selector of selectors) {
    for (const el of Array.from(document.querySelectorAll(selector))) {
      if (seen.has(el)) continue;
      seen.add(el);
      const rect = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);
      if (style.visibility === "hidden" || style.display === "none") continue;
      if (rect.width < 2 || rect.height < 2) continue;
      if (el.disabled) continue;
      const text = (el.innerText || el.value || el.getAttribute("aria-label") || "")
        .replace(/\\s+/g, " ").trim().slice(0, 120);
      const href = el.getAttribute("href") || "";
      const marker = "c" + index++;
      el.setAttribute("data-capture-bot-target", marker);
      found.push({
        marker: marker,
        tag: el.tagName.toLowerCase(),
        role: (el.getAttribute("role") || "").toLowerCase(),
        kind: el.getAttribute("role") === "tab" ? "tab" : (el.tagName.toLowerCase() === "a" ? "link" : "button"),
        text: text,
        href: href,
        in_viewport: rect.top < (window.innerHeight || 0) * 3,
      });
    }
  }
  return found;
}
"""

#: A cheap fingerprint of *what the page looks like right now* - used by the
#: crawler to notice that two clicks led to the same screen.
SIGNATURE_SCRIPT = """
() => {
  const text = (document.body ? document.body.innerText : "").replace(/\\s+/g, " ").trim();
  return {
    url: location.href,
    title: document.title || "",
    text: text.slice(0, 4000),
    __capture_bot_signature__: true,
  };
}
"""

ACTIONS = (
    "goto",
    "click",
    "download",
    "ensure_login",
    "fill",
    "press",
    "hover",
    "select",
    "check",
    "wait",
    "wait_for",
    "capture",
    "scroll",
    "back",
    "reload",
)

#: Keys a step table may use. Anything else is a typo worth reporting.
STEP_KEYS = frozenset(
    {
        "action",
        "selector",
        "text",
        "role",
        "name",
        "value",
        "key",
        "url",
        "state",
        "ms",
        "label",
        "full",
        "optional",
        "timeout",
    }
)

#: Keys a journey table may use in addition to ``url``/``steps``.
JOURNEY_KEYS = frozenset(
    {"url", "steps", "enabled", "note", "settle_ms", "timeout", "share_session"}
)

#: ``${NAME}`` from the environment (``${NAME:-fallback}``), so a journey file
#: can be committed without carrying a password.
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class JourneyError(RuntimeError):
    """Raised when a journey step fails; bilingual message."""
    def __str__(self) -> str:
        msg = super().__str__()
        return f"{msg} | خطای سفر: {msg}"


def expand_variables(value: str, env: dict[str, str] | None = None) -> str:
    """Replace ``${NAME}`` (and ``${NAME:-fallback}``) from the environment.

    An unknown variable with no fallback is left as-is and reported by
    :func:`missing_variables`, so a typo is visible instead of becoming an empty
    login field.
    """
    source = os.environ if env is None else env

    def replace(match: re.Match[str]) -> str:
        name, fallback = match.group(1), match.group(2)
        if name in source:
            return source[name]
        return fallback if fallback is not None else match.group(0)

    return _VARIABLE.sub(replace, str(value or ""))


def missing_variables(text: str, env: dict[str, str] | None = None) -> list[str]:
    """Names that ``text`` refers to and the environment does not define."""
    source = os.environ if env is None else env
    return sorted(
        {
            match.group(1)
            for match in _VARIABLE.finditer(str(text or ""))
            if match.group(1) not in source and match.group(2) is None
        }
    )


@dataclass(frozen=True)
class Step:
    """One click, one keystroke, one screenshot."""

    action: str
    selector: str = ""
    text: str = ""
    role: str = ""
    name: str = ""
    value: str = ""
    key: str = ""
    url: str = ""
    state: str = ""
    ms: int = 0
    label: str = ""
    full: bool = True
    optional: bool = False
    timeout: int = 0

    def describe(self) -> str:
        """``'click "Sign in"'``-ish: what the report and the log say."""
        if self.action in ("capture",):
            return f"capture {self.label or '(unnamed)'}"
        if self.action == "wait":
            return f"wait {self.ms}ms"
        if self.action == "wait_for":
            return f"wait_for {self.target or '(nothing)'} {self.state or 'visible'}"
        if self.action == "ensure_login":
            return f"ensure_login {self.target or '(nothing)'}"
        if self.action in ("goto",):
            return f"goto {self.url}"
        if self.action in ("back", "reload"):
            return self.action
        if self.action == "scroll":
            return f"scroll {self.value or '1 view'}"
        if self.action == "fill":
            return f'fill {self.target} with "{self.value}"'
        if self.action == "press":
            return f"press {self.key} in {self.target}"
        return f"{self.action} {self.target}"

    @property
    def target(self) -> str:
        """How this step names its element, for logs and matching."""
        if self.selector:
            return self.selector
        if self.role and self.name:
            return f"role={self.role}[name={self.name}]"
        if self.text:
            return f'"{self.text}"'
        if self.name:
            return f'"{self.name}"'
        return ""

    def selectors(self) -> list[str]:
        """Candidate Playwright selectors, best first.

        A journey author may name an element by CSS, by its visible text, by its
        accessible role and name, or by any mix - and ``_first_visible`` then
        picks whichever actually exists on the page, so one journey keeps working
        when someone renames a CSS class.
        """
        candidates: list[str] = []
        if self.selector:
            candidates.append(self.selector)
        if self.role and self.name:
            quoted = _escape_text(self.name)
            role = self.role
            candidates.extend(
                [
                    f'[role="{role}"][aria-label={quoted}]',
                    f"{role}[aria-label={quoted}]",
                    f'[role="{role}"]:has-text({quoted})',
                    f"{role}:has-text({quoted})",
                ]
            )
            if role in ("button",):
                candidates.append(f"button:has-text({quoted})")
                candidates.append(f"input[type=submit][value={quoted}]")
        for value in (self.text, self.name):
            if not value:
                continue
            quoted = _escape_text(value)
            candidates.extend(
                [
                    f"text={quoted}",
                    f"a:has-text({quoted})",
                    f"button:has-text({quoted})",
                    f"[role=button]:has-text({quoted})",
                    f"[role=tab]:has-text({quoted})",
                    f"[aria-label={quoted}]",
                    f"[title={quoted}]",
                ]
            )
        return [item for item in candidates if item]

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"action": self.action}
        for key in (
            "selector",
            "text",
            "role",
            "name",
            "value",
            "key",
            "url",
            "state",
            "ms",
            "label",
        ):
            value = getattr(self, key)
            if value not in ("", 0):
                data[key] = value
        if self.action == "capture":
            data["full"] = bool(self.full)
        if self.optional:
            data["optional"] = True
        if self.timeout:
            data["timeout"] = int(self.timeout)
        return data


def _escape_text(value: str) -> str:
    """A Playwright text matcher for ``value``, quotes included."""
    cleaned = str(value or "").replace('"', '\\"')
    return f'"{cleaned}"'


@dataclass(frozen=True)
class Journey:
    """A named click-path through one site."""

    name: str
    url: str
    steps: tuple[Step, ...] = ()
    note: str = ""
    enabled: bool = True
    settle_ms: int = 0
    timeout: int = 0
    #: Ride in the browser session of the journeys before it instead of opening a
    #: fresh context. This is what an SSO click-path needs: the cookie the identity
    #: provider set on *its* host is the thing the app's host is waiting for, and a
    #: new context would have thrown it away.
    share_session: bool = False

    @property
    def slug(self) -> str:
        return sanitize_component(self.name, max_length=40) or "journey"

    def captures(self) -> list[Step]:
        return [step for step in self.steps if step.action == "capture"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "note": self.note,
            "enabled": self.enabled,
            "share_session": self.share_session,
            "steps": [step.to_dict() for step in self.steps],
        }


def parse_journey(name: str, table: dict[str, Any], env: dict[str, str] | None = None) -> Journey:
    """Build a :class:`Journey` from one TOML table."""
    if not isinstance(table, dict):
        raise JourneyError(f"[{name}] is not a table.")
    unknown = sorted(set(table) - JOURNEY_KEYS)
    if unknown:
        raise JourneyError(f"[{name}] has unknown key(s): {', '.join(unknown)}.")

    url = expand_variables(str(table.get("url") or ""), env).strip()
    if not url:
        raise JourneyError(f"[{name}] needs a url (the page the journey starts on).")

    raw_steps = table.get("steps") or []
    if not isinstance(raw_steps, list) or not raw_steps:
        raise JourneyError(f"[{name}] needs at least one [[{name}.steps]] entry.")

    steps: list[Step] = []
    for index, raw in enumerate(raw_steps, start=1):
        steps.append(parse_step(name, index, raw, env))

    if not any(step.action == "capture" for step in steps):
        raise JourneyError(
            f'[{name}] has no capture step - add {{ action = "capture", name = "screen" }}.'
        )

    return Journey(
        name=str(name).strip(),
        url=url,
        steps=tuple(steps),
        note=str(table.get("note") or ""),
        enabled=bool(table.get("enabled", True)),
        settle_ms=max(0, int(table.get("settle_ms") or 0)),
        timeout=max(0, int(table.get("timeout") or 0)),
        share_session=bool(table.get("share_session", False)),
    )


def parse_step(
    journey: str, index: int, table: dict[str, Any], env: dict[str, str] | None = None
) -> Step:
    """Build a :class:`Step` from one step table."""
    if not isinstance(table, dict):
        raise JourneyError(f"[{journey}] step {index} is not a table.")
    unknown = sorted(set(table) - STEP_KEYS)
    if unknown:
        raise JourneyError(f"[{journey}] step {index} has unknown key(s): {', '.join(unknown)}.")

    action = str(table.get("action") or "").strip().lower()
    if action not in ACTIONS:
        raise JourneyError(
            f"[{journey}] step {index}: unknown action '{action}' "
            f"(use one of: {', '.join(ACTIONS)})."
        )

    def text_value(key: str) -> str:
        return expand_variables(str(table.get(key) or ""), env).strip()

    step = Step(
        action=action,
        selector=text_value("selector"),
        text=text_value("text"),
        role=text_value("role"),
        name=text_value("name"),
        value=expand_variables(str(table.get("value") or ""), env),
        key=text_value("key"),
        url=text_value("url"),
        state=text_value("state"),
        ms=max(0, int(table.get("ms") or 0)),
        label=text_value("label"),
        full=bool(table.get("full", True)),
        optional=bool(table.get("optional", False)),
        timeout=max(0, int(table.get("timeout") or 0)),
    )

    # `press Enter` is legitimate on its own: the key goes to the page, not to a
    # field - and that is exactly the step a recording produces for a form submit.
    needs_target = step.action in (
        "click",
        "fill",
        "hover",
        "select",
        "check",
        "download",
        "ensure_login",
    ) or (step.action == "press" and not step.key)
    if needs_target and not step.selectors():
        raise JourneyError(
            f"[{journey}] step {index}: '{action}' needs selector=, text= or role=/name=."
        )
    if step.action == "goto" and not step.url:
        raise JourneyError(f"[{journey}] step {index}: goto needs url=.")
    if step.action == "fill" and not step.value:
        raise JourneyError(f"[{journey}] step {index}: fill needs value=.")
    if step.action == "press" and not step.key:
        step = Step(**{**step.__dict__, "key": "Enter"})
    if step.action == "capture":
        # On a capture step ``name`` is what the file should be called - there is
        # no element to name, so there is nothing for it to be ambiguous with.
        label = step.label or step.name or f"screen-{index}"
        step = Step(**{**step.__dict__, "label": label, "name": ""})
    elif step.name and not step.role and not step.text and not step.selector:
        # A step may name its element by the visible label alone.
        return Step(**{**step.__dict__, "text": step.name})
    return step


def parse_journeys(data: dict[str, Any], env: dict[str, str] | None = None) -> list[Journey]:
    """Every journey in a parsed TOML document, in file order."""
    if not isinstance(data, dict) or not data:
        raise JourneyError("The journey file is empty.")
    journeys: list[Journey] = []
    for name, table in data.items():
        if isinstance(table, dict) and "steps" in table:
            journeys.append(parse_journey(str(name), table, env))
    if not journeys:
        raise JourneyError(
            "No journeys found: each one is a table with 'url' and [[name.steps]] entries."
        )
    return journeys


def load_journeys(path: str | Path, env: dict[str, str] | None = None) -> list[Journey]:
    """Read a journey TOML file. Raises :class:`JourneyError` when it is not one."""
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise JourneyError(f"Could not read {target}: {exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise JourneyError(f"{target} is not valid TOML: {exc}") from exc
    journeys = parse_journeys(data, env)
    # The check runs over the parsed *values*: a comment that explains
    # ``${VARIABLES}`` must not look like a missing one.
    values = "\n".join(
        [
            step.value + step.url + step.selector + step.text
            for item in journeys
            for step in item.steps
        ]
        + [item.url for item in journeys]
    )
    missing = missing_variables(values, env)
    if missing:
        raise JourneyError(
            f"{target} refers to undefined variable(s): {', '.join(missing)}. "
            "Set them in the environment or give a fallback (${NAME:-default})."
        )
    return journeys


SAMPLE_JOURNEY = """# One table per journey: the click-path a person takes through the site.
# Copy it next to your captures and edit it - or write one from the app.
#
#   python -m app.cli journey --file journey.toml --out ./shots --list
#   python -m app.cli journey --file journey.toml --out ./shots
#
# An element can be named three ways; the runner tries them in order, so a
# journey survives a renamed CSS class as long as the button still says the same
# thing. Use whichever you can see in the browser:
#   selector = "#login"          CSS
#   text     = "Sign in"         what the user reads
#   role = "button", name = "Sign in"    what a screen reader announces
#
# ${VARIABLES} are read from the environment, so a password never lands in this
# file.

[home]
url = "https://example.com"
note = "The landing page, and what is behind its main buttons"

[[home.steps]]
action = "capture"
name = "landing"

[[home.steps]]
action = "click"
text = "Products"

[[home.steps]]
action = "capture"
name = "products"

[[home.steps]]
action = "click"
text = "Filters"

[[home.steps]]
action = "capture"
name = "filters-open"

[account]
url = "https://example.com/login"
note = "Sign in, then photograph the dashboard"

[[account.steps]]
action = "fill"
selector = "#email"
value = "${CAPTURE_DEMO_EMAIL:-demo@example.com}"

[[account.steps]]
action = "fill"
selector = "#password"
value = "${CAPTURE_DEMO_PASSWORD:-hunter2}"

[[account.steps]]
action = "click"
selector = "button[type=submit]"

[[account.steps]]
action = "wait_for"
selector = ".dashboard"
optional = true

[[account.steps]]
action = "capture"
name = "dashboard"
"""


@dataclass
class StepOutcome:
    """What one step did."""

    index: int = 0
    action: str = ""
    detail: str = ""
    ok: bool = True
    skipped: bool = False
    message: str = ""
    file_path: str = ""
    url: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "index": self.index,
            "action": self.action,
            "detail": self.detail,
            "ok": self.ok,
        }
        if self.skipped:
            data["skipped"] = True
        if self.message:
            data["message"] = self.message
        if self.file_path:
            data["file"] = self.file_path
        if self.url:
            data["url"] = self.url
        return data


@dataclass
class JourneyRun:
    """What happened to one journey."""

    name: str = ""
    url: str = ""
    steps: list[StepOutcome] = field(default_factory=list)
    captures: list[str] = field(default_factory=list)
    ok: bool = True
    message: str = ""
    elapsed_ms: int = 0

    def failures(self) -> list[StepOutcome]:
        return [step for step in self.steps if not step.ok]

    def summary(self) -> str:
        mark = "ok" if self.ok else "FAILED"
        detail = f"{len(self.captures)} capture(s)"
        if self.message:
            detail = f"{detail}, {self.message}"
        return f"{self.name}: {mark} - {detail}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "ok": self.ok,
            "message": self.message,
            "elapsed_ms": self.elapsed_ms,
            "captures": list(self.captures),
            "steps": [step.to_dict() for step in self.steps],
        }


@dataclass
class JourneyReport:
    """The whole run: one :class:`JourneyRun` per journey."""

    output_dir: str = ""
    files: list[str] = field(default_factory=list)
    journeys: list[JourneyRun] = field(default_factory=list)
    generated_at: str = ""

    @property
    def ok(self) -> bool:
        return all(run.ok for run in self.journeys) and bool(self.journeys)

    @property
    def captures(self) -> int:
        return sum(len(run.captures) for run in self.journeys)

    def failures(self) -> list[tuple[str, StepOutcome]]:
        return [(run.name, step) for run in self.journeys for step in run.failures()]

    def summary(self) -> str:
        if not self.journeys:
            return "No journey ran."
        lines = [run.summary() for run in self.journeys]
        lines.append(
            f"{len(self.journeys)} journey(s), {self.captures} capture(s), "
            f"{len(self.failures())} failed step(s)."
        )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "output_dir": self.output_dir,
            "ok": self.ok,
            "captures": self.captures,
            "files": list(self.files),
            "journeys": [run.to_dict() for run in self.journeys],
        }


class JourneyRunner:
    """Drives a browser through journeys and keeps the screenshots.

    The runner borrows the browser session from a
    :class:`~app.core.engine.CaptureEngine` (its user-agent, proxy, storage state,
    timeouts and screenshot logic), which is why it takes an engine rather than a
    settings object: one source of truth for *how* a page is photographed, and a
    second vocabulary for *when*.
    """

    def __init__(
        self,
        engine: Any,
        clock: Callable[[], float] = time.monotonic,
        force_shared_session: bool = False,
    ) -> None:
        self.engine = engine
        self.settings = engine.settings
        self._clock = clock
        self._index = 0  # running counter for the engine's log/screenshot helpers
        self._saved = 0  # screenshots written so far, for the file names
        #: ``--share-session``: run *every* journey in the one context.
        self.force_shared_session = force_shared_session

    def _prepare_page(self, page: Any) -> None:
        """Timeouts on a fresh tab, wherever the tab came from."""
        timeout = self.settings.navigation_timeout_ms
        page.set_default_timeout(timeout)
        try:
            page.set_default_navigation_timeout(timeout)
        except Exception:  # pragma: no cover - not every engine exposes this
            pass

    def carry_session_over(self, context: Any, journeys: Sequence[Journey]) -> None:
        """Save the shared session, so the *next* run starts signed in.

        Only when a storage state is configured at all: writing one unasked would
        leave a cookie jar on disk that the user never asked for. This is the
        second half of SSO - the first half signs in, this half remembers it.
        """
        storage_state = str(getattr(self.settings, "storage_state_path", "") or "").strip()
        if not storage_state:
            return
        named = ", ".join(journey.name for journey in journeys if journey.share_session)
        try:
            context.storage_state(path=storage_state)
        except Exception as exc:  # noqa: BLE001 - remember-me is a bonus, not a step
            self.log(LogLevel.WARNING, f"Could not save the shared session: {exc}")
            return
        self.log(
            LogLevel.SUCCESS,
            f"Saved the session from {named or 'the shared journeys'} to {storage_state}",
        )

    # ------------------------------------------------------------------ helpers
    def log(self, level: LogLevel, message: str) -> None:
        self.engine.log(level, message)

    def _wait(self, ms: int) -> None:
        if ms > 0:
            self.engine._interruptible_sleep(int(ms))

    def _pause(self, journey: Journey) -> None:
        """Wait the settle delay (the journey may ask for a longer one)."""
        self._wait(max(int(self.settings.settle_delay_ms), int(journey.settle_ms)))

    def capture(self, page: Any, journey: Journey, step: Step) -> tuple[bytes, int, int]:
        """A full-page (or viewport) screenshot, in the configured format."""
        self._index += 1
        if step.full and self.settings.scroll_to_load_lazy_content:
            self.engine._lazy_scroll(page, self._index)
        page._capture_bot_index = self._index
        width, height = self.engine._measure(page)
        payload, _stitched = self.engine._screenshot(page, width, height, self._index)
        return self.engine._encode_for_format(payload), width, height

    def _save(
        self, payload: bytes, output_dir: Path, journey: Journey, order: int, label: str
    ) -> Path:
        extension = self.engine._file_extension()
        folder = output_dir / "journeys" / journey.slug
        folder.mkdir(parents=True, exist_ok=True)
        filename = f"{order:02d}-{sanitize_component(label, max_length=40) or 'screen'}.{extension}"
        path = unique_path(folder, filename)
        try:
            path.write_bytes(payload)
        except OSError as exc:  # pragma: no cover - a full disk is the user's problem
            raise JourneyError(f"Could not write {path}: {exc}") from exc
        return path

    # -------------------------------------------------------------------- steps
    def _click(self, page: Any, step: Step, timeout: int) -> str:
        locator = self.engine._first_visible(page, step.selectors())
        if locator is None:
            raise JourneyError(f"nothing matching {step.target} was visible")
        locator.click(timeout=timeout)
        return step.target

    def _fill(self, page: Any, step: Step, timeout: int) -> str:
        locator = self.engine._first_visible(page, step.selectors())
        if locator is None:
            raise JourneyError(f"no field matching {step.target} was visible")
        locator.fill(step.value, timeout=timeout)
        return f"{step.target} = {step.value}"

    def _press(self, page: Any, step: Step, timeout: int) -> str:
        key = step.key or "Enter"
        selectors = step.selectors()
        if not selectors:
            # ``press Enter`` with nothing to aim at is a key for the page - the
            # step a recording writes for a form submit or a keyboard shortcut.
            keyboard = getattr(page, "keyboard", None)
            if keyboard is None:
                raise JourneyError(f"this browser cannot press {key} without a target")
            keyboard.press(key, timeout=timeout)
            return f"pressed {key}"
        locator = self.engine._first_visible(page, selectors)
        if locator is None:
            raise JourneyError(f"nothing matching {step.target} was visible")
        locator.press(key, timeout=timeout)
        return f"{key} in {step.target}"

    def _ensure_login(self, page: Any, step: Step, timeout: int) -> str:
        """Sign in again if the saved session has expired.

        A storage state is a cookie with a date on it: the run that works today
        finds a login form tomorrow. ``ensure_login <selector>`` names something
        only a signed-in page has - if it is already there the step is free, and if
        it is not, the engine's configured login form is filled in and the step
        waits for the selector to show up.
        """
        selectors = step.selectors()
        if not selectors:
            raise JourneyError("ensure_login needs a selector that signed-in pages show")
        if self.engine._first_visible(page, selectors) is not None:
            return f"still signed in ({step.target})"

        settings = self.settings
        if not (settings.auth_enabled and settings.username):
            raise JourneyError(
                f"the session looks signed out ({step.target} is missing) and no login "
                "is configured - set a username/password or import a fresh session"
            )
        self.log(
            LogLevel.INFO,
            f"  {step.target} is missing: signing in again as '{settings.username}'.",
        )
        self.engine._form_login(page, self._index or 1)
        try:
            self._wait_for(page, step, timeout)
        except Exception as exc:  # noqa: BLE001 - still not there after signing in
            raise JourneyError(
                f"signed in, but {step.target} never appeared: "
                f"{self.engine._describe_exception(exc)}"
            ) from exc
        return f"signed in again ({step.target})"

    def _download(self, page: Any, step: Step, timeout: int) -> str:
        """Click something that saves a file, and keep the file.

        A download is the one click whose *result* is not a screen: the person
        wanted the file, and a replayed journey that merely clicks again throws it
        away. The file lands in ``<out>/downloads/`` next to the screenshots, with
        the name the site proposed.
        """
        locator = self.engine._first_visible(page, step.selectors())
        if locator is None:
            raise JourneyError(f"nothing matching {step.target} was visible")
        folder = Path(self.settings.output_dir) / "downloads"
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - a read-only output folder
            raise JourneyError(f"could not create {folder}: {exc}") from exc

        expect = getattr(page, "expect_download", None)
        if expect is None:
            # An engine without download support still clicks - losing the file is
            # better than failing the journey.
            locator.click(timeout=timeout)
            return f"clicked {step.target} (downloads not supported)"
        try:
            with expect(timeout=timeout) as info:
                locator.click(timeout=timeout)
            download = getattr(info, "value", None) or info
            suggested = str(getattr(download, "suggested_filename", "") or "") or "download"
            target = unique_path(folder, download_name(suggested))
            download.save_as(str(target))
        except JourneyError:
            raise
        except Exception as exc:  # noqa: BLE001 - no file arrived
            raise JourneyError(
                f"no download started: {self.engine._describe_exception(exc)}"
            ) from exc
        self.log(LogLevel.SUCCESS, f"  saved the download {target.name}")
        return f"downloaded {target.name}"

    def _hover(self, page: Any, step: Step, timeout: int) -> str:
        locator = self.engine._first_visible(page, step.selectors())
        if locator is None:
            raise JourneyError(f"nothing matching {step.target} was visible")
        locator.hover(timeout=timeout)
        return step.target

    def _select(self, page: Any, step: Step, timeout: int) -> str:
        locator = self.engine._first_visible(page, step.selectors())
        if locator is None:
            raise JourneyError(f"no <select> matching {step.target} was visible")
        locator.select_option(step.value, timeout=timeout)
        return f"{step.target} = {step.value}"

    def _check(self, page: Any, step: Step, timeout: int) -> str:
        locator = self.engine._first_visible(page, step.selectors())
        if locator is None:
            raise JourneyError(f"nothing matching {step.target} was visible")
        locator.check(timeout=timeout)
        return step.target

    def _wait_for(self, page: Any, step: Step, timeout: int) -> str:
        wanted = (step.state or "visible").strip().lower()
        locator = page.locator(step.selector or step.target).first
        if wanted == "hidden":
            locator.wait_for(state="hidden", timeout=timeout)
        elif wanted in ("attached", "detached", "visible"):
            locator.wait_for(state=wanted, timeout=timeout)
        else:
            raise JourneyError(f"unknown state '{step.state}' (visible/hidden/attached/detached)")
        return f"{step.target or step.selector} became {wanted}"

    def _scroll(self, page: Any) -> str:
        try:
            page.evaluate(
                "() => { window.scrollTo(0, document.body ? document.body.scrollHeight : 0); }"
            )
        except Exception:  # pragma: no cover - scrolling is never worth a failure
            try:
                page.mouse.wheel(0, 4000)
            except Exception:  # noqa: BLE001
                pass
        return "page scrolled"

    def _back(self, page: Any, timeout: int) -> str:
        page.go_back(timeout=timeout)
        return "went back"

    def _reload(self, page: Any, timeout: int) -> str:
        page.reload(timeout=timeout)
        return "reloaded"

    def _goto(self, page: Any, step: Step) -> str:
        target = step.url
        here = ""
        try:
            here = urlparse(str(page.url or "")).hostname or ""
        except Exception:  # noqa: BLE001 - a page that will not name itself
            here = ""
        there = urlparse(str(target or "")).hostname or ""
        if here and there and here != there:
            # Leaving the host is the interesting part of an SSO step: say so, so a
            # screenshot of the other site is never a surprise in the report.
            self.log(LogLevel.INFO, f"Leaving {here} for {there} (cross-site step)")
        self.engine._goto(page, target)
        return f"opened {target}"

    def run_step(self, page: Any, journey: Journey, step: Step, order: int) -> StepOutcome:
        """Run one step; a failure is returned, never raised (except stop)."""
        timeout = int(step.timeout or journey.timeout or self.settings.navigation_timeout_ms)
        outcome = StepOutcome(index=order, action=step.action, detail=step.describe())
        try:
            if step.action == "goto":
                outcome.detail = self._goto(page, step)
            elif step.action == "click":
                outcome.detail = self._click(page, step, timeout)
            elif step.action == "download":
                outcome.detail = self._download(page, step, timeout)
            elif step.action == "ensure_login":
                outcome.detail = self._ensure_login(page, step, timeout)
            elif step.action == "fill":
                outcome.detail = self._fill(page, step, timeout)
            elif step.action == "press":
                outcome.detail = self._press(page, step, timeout)
            elif step.action == "hover":
                outcome.detail = self._hover(page, step, timeout)
            elif step.action == "select":
                outcome.detail = self._select(page, step, timeout)
            elif step.action == "check":
                outcome.detail = self._check(page, step, timeout)
            elif step.action == "wait":
                self._wait(step.ms)
                outcome.detail = f"waited {step.ms}ms"
            elif step.action == "wait_for":
                outcome.detail = self._wait_for(page, step, timeout)
            elif step.action == "scroll":
                outcome.detail = self._scroll(page)
            elif step.action == "back":
                outcome.detail = self._back(page, timeout)
            elif step.action == "reload":
                outcome.detail = self._reload(page, timeout)
            elif step.action == "capture":
                self._pause(journey)
                payload, width, height = self.capture(page, journey, step)
                self._saved += 1
                path = self._save(
                    payload, Path(self.settings.output_dir), journey, self._saved, step.label
                )
                outcome.file_path = str(path)
                outcome.detail = f"{step.label} ({width}x{height})"
            else:  # pragma: no cover - parse_step rejects anything else
                raise JourneyError(f"unsupported action '{step.action}'")
            outcome.ok = True
        except Exception as exc:  # noqa: BLE001 - one bad step must not kill the app
            message = self.engine._describe_exception(exc)
            if step.optional:
                outcome.ok = True
                outcome.skipped = True
                outcome.message = f"skipped: {message}"
                self.log(
                    LogLevel.DEBUG,
                    f"{journey.name}: step {order} ({step.describe()}) skipped: {message}",
                )
            else:
                outcome.ok = False
                outcome.message = message
        try:
            outcome.url = str(page.url)
        except Exception:  # pragma: no cover - a closed page has no url
            outcome.url = ""
        return outcome

    # ------------------------------------------------------------------ journeys
    def run_journey(self, page: Any, journey: Journey) -> JourneyRun:
        """Run every step of one journey on an already-open page."""
        run = JourneyRun(name=journey.name, url=journey.url)
        started = self._clock()
        self.log(
            LogLevel.INFO,
            f"Journey '{journey.name}': {len(journey.steps)} step(s) from {journey.url}",
        )
        try:
            self.engine._goto(page, journey.url)
        except Exception as exc:  # noqa: BLE001
            run.ok = False
            run.message = f"could not open {journey.url}: {self.engine._describe_exception(exc)}"
            run.elapsed_ms = int((self._clock() - started) * 1000)
            self.log(LogLevel.ERROR, f"Journey '{journey.name}': {run.message}")
            return run

        for order, step in enumerate(journey.steps, start=1):
            if self.engine.stop_requested:
                run.message = "stopped"
                self.log(LogLevel.WARNING, f"Journey '{journey.name}': stopped.")
                break
            outcome = self.run_step(page, journey, step, order)
            run.steps.append(outcome)
            if outcome.file_path:
                run.captures.append(outcome.file_path)
                self.log(
                    LogLevel.SUCCESS,
                    f"  {journey.name} [{order}] captured {Path(outcome.file_path).name}",
                )
            elif outcome.skipped:
                self.log(LogLevel.DEBUG, f"  {journey.name} [{order}] {outcome.message}")
            elif outcome.ok:
                self.log(LogLevel.INFO, f"  {journey.name} [{order}] {outcome.detail}")
            else:
                run.ok = False
                run.message = f"step {order} failed"
                self.log(
                    LogLevel.ERROR,
                    f"  {journey.name} [{order}] {step.describe()} failed: {outcome.message}",
                )
                break

        run.elapsed_ms = int((self._clock() - started) * 1000)
        if run.ok and not run.message:
            run.message = f"{len(run.captures)} capture(s)"
        return run

    def run(self, journeys: Sequence[Journey]) -> JourneyReport:
        """Run every enabled journey, each in its own fresh browser context."""
        settings = self.settings
        report = JourneyReport(
            output_dir=str(settings.output_dir),
            generated_at=datetime.now().isoformat(timespec="seconds"),
        )
        wanted = [journey for journey in journeys if journey.enabled]
        if not wanted:
            report.generated_at = ""
            return report

        output_dir = Path(settings.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        playwright = None
        browser = None
        shared_context = None
        shared_page = None
        try:
            playwright = self.engine._playwright_factory()
            playwright = playwright.__enter__()
            browser = self.engine._launch(playwright)
            for journey in wanted:
                if self.engine.stop_requested:
                    break
                share = bool(journey.share_session) or bool(self.force_shared_session)
                context = None
                page = None
                try:
                    if share:
                        # The whole point of a shared session is that a cookie from
                        # host A is still there when the journey moves to host B.
                        if shared_context is None:
                            shared_context = browser.new_context(**self.engine._context_kwargs())
                            shared_page = shared_context.new_page()
                            self._prepare_page(shared_page)
                            self.log(
                                LogLevel.INFO,
                                "Sharing one browser session across journeys "
                                "(cross-site / SSO steps).",
                            )
                        page = shared_context.new_page()
                        self._prepare_page(page)
                    else:
                        context = browser.new_context(**self.engine._context_kwargs())
                        page = context.new_page()
                        self._prepare_page(page)
                    report.journeys.append(self.run_journey(page, journey))
                except Exception as exc:  # noqa: BLE001 - one journey must not kill the rest
                    broken = JourneyRun(
                        name=journey.name,
                        url=journey.url,
                        ok=False,
                        message=self.engine._describe_exception(exc),
                    )
                    report.journeys.append(broken)
                    self.log(LogLevel.ERROR, f"Journey '{journey.name}' failed: {broken.message}")
                finally:
                    if share:
                        # The tab goes; the session stays for the next journey.
                        try:
                            if page is not None:
                                page.close()
                        except Exception:  # noqa: BLE001 - a tab that will not close
                            pass
                    else:
                        self.engine._close_context(context)
            if shared_context is not None:
                self.carry_session_over(shared_context, wanted)
                self.engine._close_context(shared_context)
                shared_context = None
        except BrowserNotInstalledError:
            # Not a page that failed: nothing can run until Playwright's browser is
            # installed, and the CLI turns this into exit code 3.
            raise
        except Exception as exc:  # noqa: BLE001
            report.journeys.append(
                JourneyRun(name="(browser)", ok=False, message=self.engine._describe_exception(exc))
            )
            self.log(LogLevel.ERROR, f"Could not drive the browser: {exc}")
        finally:
            self.engine._close_browser(browser)
            if playwright is not None:
                try:
                    playwright.__exit__(None, None, None)
                except Exception:  # pragma: no cover
                    pass

        report.files = sorted(str(path) for run in report.journeys for path in run.captures)
        if want_report(settings):
            path = write_report(report, output_dir)
            if path is not None:
                self.log(LogLevel.INFO, f"Journey report written to {path.name}")
        return report


def want_report(settings: Any) -> bool:
    """Whether a journey run should leave a report behind (it always should)."""
    return bool(getattr(settings, "write_report", True))


def write_report(report: JourneyReport, output_dir: Path) -> Path | None:
    """Write ``journey-report-<stamp>.json`` next to the captures."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = output_dir / f"journey-report-{stamp}.json"
    try:
        path.write_text(
            json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:  # pragma: no cover - the report is a convenience
        return None
    return path


def describe_elements(page: Any) -> list[dict[str, Any]]:
    """Tag and describe everything clickable on the page (see DISCOVERY_SCRIPT)."""
    try:
        found = page.evaluate(DISCOVERY_SCRIPT)
    except Exception:  # noqa: BLE001 - a page that refuses the script simply has nothing
        return []
    if not isinstance(found, list):
        return []
    return [item for item in found if isinstance(item, dict)]


def page_signature(page: Any) -> str:
    """A short fingerprint of the screen: URL, title and the visible text.

    Two clicks that end on the same screen produce the same fingerprint, which is
    how the crawler avoids photographing the same page five times.
    """
    try:
        data = page.evaluate(SIGNATURE_SCRIPT)
    except Exception:  # noqa: BLE001
        data = None
    if isinstance(data, dict):
        url = str(data.get("url") or "")
        title = str(data.get("title") or "")
        body = str(data.get("text") or "")
    else:
        url, title, body = str(getattr(page, "url", "")), "", ""
    digest = hashlib.sha1(f"{url}|{title}|{body}".encode("utf-8", "replace")).hexdigest()
    return digest[:12]


def clear_element_tags(page: Any) -> None:
    """Remove the crawler's marker attributes so a capture shows a clean page."""
    try:
        page.evaluate(
            f"() => document.querySelectorAll('[{ELEMENT_ATTR}]').forEach(el => el.removeAttribute('{ELEMENT_ATTR}'))"
        )
    except Exception:  # noqa: BLE001 - cosmetic only
        pass


def slug_for_text(text: str, fallback: str = "screen") -> str:
    """A file-name-friendly version of a button's label."""
    cleaned = re.sub(r"[^A-Za-z0-9\u0600-\u06FF]+", "-", str(text or "")).strip("-")
    return (cleaned[:40] or fallback).lower()


_LINE_ACTIONS = (
    "goto",
    "click",
    "download",
    "ensure_login",
    "fill",
    "press",
    "hover",
    "select",
    "check",
    "wait",
    "wait_for",
    "capture",
    "scroll",
    "back",
    "reload",
)

_FLAG_KEYS = ("timeout", "state", "key", "value", "url", "label")


def _unquote(token: str) -> str:
    """``"Sign in"`` -> ``Sign in``; anything else is returned as it is."""
    token = str(token or "").strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def _split_flags(rest: str) -> tuple[str, dict[str, Any]]:
    """Peel ``optional`` and ``key=value`` flags off the end of a step line."""
    flags: dict[str, Any] = {}
    tokens = rest.split()
    while tokens:
        last = tokens[-1]
        lowered = last.lower()
        if lowered == "optional":
            flags["optional"] = True
        elif "=" in last:
            key, _, value = last.partition("=")
            key = key.strip().lower()
            if key not in _FLAG_KEYS or not value:
                break
            flags[key] = int(value) if key == "timeout" and value.isdigit() else _unquote(value)
        else:
            break
        tokens.pop()
    return " ".join(tokens), flags


def _parse_target(rest: str) -> dict[str, Any]:
    """Turn ``#id`` / ``"Sign in"`` / ``selector=#id`` / ``role=button name="Go"`` into keys."""
    rest = rest.strip()
    if not rest:
        return {}
    lowered = rest.lower()
    if lowered.startswith("selector="):
        return {"selector": _unquote(rest.split("=", 1)[1])}
    if lowered.startswith("text="):
        return {"text": _unquote(rest.split("=", 1)[1])}
    if lowered.startswith("role="):
        role, _, tail = rest.split("=", 1)[1].partition(" ")
        target: dict[str, Any] = {"role": _unquote(role)}
        tail = tail.strip()
        if tail.lower().startswith("name="):
            target["name"] = _unquote(tail.split("=", 1)[1])
        elif tail:
            target["name"] = _unquote(tail)
        return target
    if rest[0] in "\"'":
        return {"text": _unquote(rest)}
    # A CSS-ish token (#id, .class, div[role=tab]) is a selector; plain words are text.
    if rest[0] in "#.[" or "[" in rest or rest.startswith("role:") or ">" in rest:
        return {"selector": rest}
    if "=" in rest:
        return {"selector": rest}
    return {"text": rest}


def parse_step_line(line: str) -> dict[str, Any]:
    """Parse one line of the compact step language into a step table.

    The GUI's "Clicks & screens" box accepts exactly this, and so does the
    ``--steps-file`` shortcut. It is the same vocabulary as a TOML step, only
    flattened onto one line, because that is what a person can type quickly::

        click "Sign in"
        fill #email = ${SHOP_EMAIL:-me@example.com}
        wait 500
        wait_for .dashboard optional timeout=8000
        capture dashboard

    Raises :class:`JourneyError` with the offending word, so the app can say
    "Step 3: unknown action 'clik'" instead of failing somewhere inside a page.
    """
    stripped = str(line or "").strip()
    if not stripped or stripped.startswith("#"):
        raise JourneyError("empty step line")

    head, _, tail = stripped.partition(" ")
    action = head.strip().lower()
    if action not in _LINE_ACTIONS:
        raise JourneyError(f"unknown action '{action}' (use one of: {', '.join(_LINE_ACTIONS)})")

    rest, flags = _split_flags(tail.strip())
    step: dict[str, Any] = {"action": action}
    step.update(flags)
    rest = rest.strip()

    if action in ("back", "reload"):
        if rest:
            raise JourneyError(f"'{action}' takes no arguments")
        return step
    if action in ("click", "hover", "check", "download"):
        target = _parse_target(rest)
        if not target:
            raise JourneyError(f"'{action}' needs a selector, a \"text\" or role=/name=")
        step.update(target)
        return step
    if action == "ensure_login":
        target = _parse_target(rest)
        if not target:
            raise JourneyError(
                "'ensure_login' needs something only a signed-in page shows "
                "(for example .dashboard)"
            )
        step.update(target)
        step["state"] = "visible"
        return step
    if action in ("fill", "select"):
        selector, separator, value = rest.partition(" = ")
        if not separator:
            selector, separator, value = rest.partition("=")
        if not separator or not value.strip():
            raise JourneyError(f"'{action}' needs <selector> = <value>")
        step.update(_parse_target(selector.strip()))
        step["value"] = _unquote(value.strip())
        return step
    if action == "press":
        if not rest:
            step["key"] = "Enter"
            return step
        first, _, remainder = rest.partition(" ")
        if remainder.strip():
            step.update(_parse_target(first))
            step["key"] = _unquote(remainder)
        else:
            step["key"] = _unquote(first)
        return step
    if action == "wait":
        if not rest:
            raise JourneyError("'wait' needs a number of milliseconds")
        digits = rest.split()[0]
        if not digits.isdigit():
            raise JourneyError(f"'wait' needs a number of milliseconds, not '{digits}'")
        step["ms"] = int(digits)
        return step
    if action == "wait_for":
        target = _parse_target(rest)
        if not target:
            raise JourneyError("'wait_for' needs a selector, a \"text\" or role=/name=")
        step.update(target)
        return step
    if action == "capture":
        step["label"] = _unquote(rest) or "screen"
        return step
    if action == "scroll":
        step["value"] = rest or "1"
        return step
    if action == "goto":
        if not rest:
            raise JourneyError("'goto' needs a url")
        step["url"] = _unquote(rest)
        return step
    raise JourneyError(f"unknown action '{action}'")  # pragma: no cover - guarded above


def step_line(step: dict[str, Any]) -> str:
    """One step table as one line of the compact language (the inverse of parse).

    The app's step editor is a table of rows: action, target, and the three
    switches that matter (optional, timeout, an alternative way to name the
    element). Editing a row is easy; editing *text* is what people get wrong, so
    the editor keeps rows and this function is how it writes them back. Parsing
    the result must give the same step table back - a round trip, by design.
    """
    action = str(step.get("action") or "").strip().lower()
    if not action:
        raise JourneyError("a step needs an action")

    selector = str(step.get("selector") or "").strip()
    text = str(step.get("text") or "").strip()
    role = str(step.get("role") or "").strip()
    name = str(step.get("name") or "").strip() if action != "capture" else ""
    value = str(step.get("value") or "")
    target = ""
    if selector:
        target = selector
    elif role:
        target = f"role={role}"
    elif text:
        target = f'"{text}"'
    if role and name:
        target = f'{target} name="{name}"' if target.startswith("role=") else target

    if action in ("back", "reload"):
        body = ""
    elif action in ("click", "hover", "check", "download", "ensure_login", "wait_for"):
        body = target
    elif action in ("fill", "select"):
        body = f"{target} = {value}"
    elif action == "press":
        body = f"{target} {step.get('key') or 'Enter'}".strip()
    elif action == "wait":
        body = str(int(step.get("ms") or 0))
    elif action == "capture":
        body = str(step.get("label") or "screen")
    elif action == "scroll":
        body = str(step.get("value") or "1")
    elif action == "goto":
        body = str(step.get("url") or "")
    else:  # pragma: no cover - parse_step_line refuses anything else
        body = target

    flags = []
    if bool(step.get("optional")):
        flags.append("optional")
    timeout = int(step.get("timeout") or 0)
    if timeout > 0:
        flags.append(f"timeout={timeout}")
    if selector and role:
        # The selector wins on the way back in, so keep the other spelling alive.
        flags.append(f"role={role}")
    if selector and text:
        flags.append(f'text="{text}"')
    if selector and name and not role:
        flags.append(f'name="{name}"')
    line = " ".join(part for part in (action, body) if part)
    return f"{line} {' '.join(flags)}" if flags else line


def steps_to_text(steps: Sequence[dict[str, Any]]) -> str:
    """A list of step tables as the compact text the box (and a recipe) holds."""
    return "\n".join(step_line(step) for step in steps)


def parse_step_rows(text: str, env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Steps as editable rows: the parsed table plus a hint for the first column."""
    rows = []
    for step in parse_steps_text(text, env):
        row = dict(step)
        row["_hint"] = _row_hint(row)
        rows.append(row)
    return rows


def _row_hint(step: dict[str, Any]) -> str:
    """The human half of a row: what this step is *for*, in a few words."""
    action = str(step.get("action") or "")
    if action == "capture":
        return f"save a screenshot called '{step.get('label') or 'screen'}'"
    if action == "wait":
        return f"pause for {int(step.get('ms') or 0)} ms"
    if action == "goto":
        return f"open {step.get('url')}"
    if action in ("fill", "select"):
        return f"type into {step.get('selector') or step.get('text') or '?'}"
    if action == "press":
        return f"press {step.get('key') or 'Enter'}"
    if action == "scroll":
        return f"scroll {step.get('value') or '1'}"
    return f"{action} {step.get('selector') or step.get('text') or step.get('role') or ''}".strip()


def parse_steps_text(text: str, env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Parse a whole block of :func:`parse_step_line` lines.

    Blank lines and ``#`` comments are skipped, and variables are expanded here
    rather than later so a missing ``${VAR}`` is reported with its line number.
    """
    steps: list[dict[str, Any]] = []
    for number, line in enumerate(str(text or "").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            step = parse_step_line(stripped)
        except JourneyError as exc:
            raise JourneyError(f"line {number}: {exc}") from exc
        for key, value in list(step.items()):
            if isinstance(value, str) and "${" in value:
                step[key] = expand_variables(value, env)
        missing = missing_variables(
            " ".join(str(v) for v in step.values() if isinstance(v, str)), env
        )
        if missing:
            raise JourneyError(f"line {number}: undefined variable(s): {', '.join(missing)}")
        steps.append(step)
    if not steps:
        raise JourneyError("no steps were given")
    return steps


def journey_from_text(name: str, url: str, text: str, env: dict[str, str] | None = None) -> Journey:
    """A runnable :class:`Journey` from the compact step text (used by the GUI)."""
    steps = parse_steps_text(text, env)
    if not any(step.get("action") == "capture" for step in steps):
        # A person who forgot the capture step still wants the screens.
        steps.append({"action": "capture", "label": "screen"})
    return parse_journey(name or "journey", {"url": url, "steps": steps}, env)


@dataclass(frozen=True)
class Recipe:
    """A ready-made click-path for a common kind of page.

    Recipes are the answer to an empty box: nobody wants to learn a step language
    before their first screenshot, but almost everybody's site is *some* kind of
    shop, login page, wizard, tab bar or pricing table. Each recipe is plain text
    in the same language the Clicks box accepts, with fallbacks baked in so it
    runs as-is on a first try and can then be edited in place.
    """

    name: str
    title: str
    note: str
    steps: str
    #: A short hash of the file a custom recipe was read from (empty for the
    #: built-ins): enough to tell two versions of the same recipe apart in the
    #: picker, and to know that a shared folder moved on.
    version: str = ""

    def lines(self) -> list[str]:
        return [
            line.strip()
            for line in self.steps.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]


RECIPES: tuple[Recipe, ...] = (
    Recipe(
        name="shop",
        title="Shop: landing, products, filters",
        note="Walks into the product list and opens the filters panel.",
        steps="""capture landing
click "Products" optional
capture products
click "Filters" optional timeout=8000
capture filters-open
click "Sort by price" optional
capture sorted""",
    ),
    Recipe(
        name="login-dashboard",
        title="Sign in, then the dashboard",
        note="Types the credentials from the environment (demo values as fallback).",
        steps="""capture login
fill #email = ${CAPTURE_EMAIL:-demo@example.com}
fill #password = ${CAPTURE_PASSWORD:-hunter2}
click button[type=submit] optional
click "Sign in" optional
wait_for .dashboard optional timeout=15000
capture dashboard
click "Settings" optional
capture settings""",
    ),
    Recipe(
        name="tabs",
        title="Every tab of a tabbed page",
        note="Clicks each tab by its accessible role and photographs the panel.",
        steps="""capture first-tab
click role=tab name="Overview" optional
capture overview
click role=tab name="Details" optional
capture details
click role=tab name="Reviews" optional
capture reviews""",
    ),
    Recipe(
        name="wizard",
        title="A multi-step form, one screen per step",
        note="Fills each step and photographs it before moving on.",
        steps="""capture step-1
fill #name = ${CAPTURE_NAME:-Demo Person}
fill #email = ${CAPTURE_EMAIL:-demo@example.com}
click "Next" optional
capture step-2
select #country = ${CAPTURE_COUNTRY:-NL} optional
click "Next" optional
capture step-3
check #terms optional
click "Finish" optional
capture done""",
    ),
    Recipe(
        name="sso-login",
        title="Sign in on the identity provider, land back on the app",
        note="A journey may cross hosts: the same browser session carries the login.",
        steps="""capture sso-login
fill #username = ${SSO_USER:-demo@example.com}
fill #password = ${SSO_PASSWORD:-hunter2}
click button[type=submit] optional
wait_for .app optional timeout=15000
capture app-after-sso
goto ${SSO_APP_URL:-https://app.example.com/dashboard}
capture dashboard""",
    ),
    Recipe(
        name="pricing",
        title="Pricing page: monthly, yearly, plan detail",
        note="Toggles the billing switch and opens one plan, if those exist.",
        steps="""capture pricing
click "Yearly" optional
capture yearly
click "Monthly" optional
capture monthly
click "Compare all features" optional
capture comparison""",
    ),
)

RECIPE_NAMES: tuple[str, ...] = tuple(recipe.name for recipe in RECIPES)

#: Where a user's own recipes live (``CAPTURE_BOT_RECIPES`` overrides it, which
#: is what the tests and a portable install both want).
RECIPES_ENV = "CAPTURE_BOT_RECIPES"


def recipes_dir(folder: str | Path | None = None) -> Path:
    """The folder custom recipes are read from (created on save, not on read)."""
    if folder is not None and str(folder).strip():
        return Path(folder)
    override = os.environ.get(RECIPES_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    base = os.environ.get("APPDATA", "").strip() if os.name == "nt" else ""
    home = Path(base).expanduser() if base else Path.home() / ".config"
    return home / "capture-bot" / "recipes"


def load_custom_recipes(folder: str | Path | None = None) -> tuple[Recipe, ...]:
    """Every ``*.txt`` in the recipes folder, as recipes.

    The file *is* the step text (comments allowed), and its first two comment
    lines may name it::

        # Title: The checkout funnel
        # Note: what the sales team asks for every Monday
        capture cart
        click "Checkout"
        capture address

    A broken file is skipped rather than allowed to break the picker: a file of
    steps someone is halfway through writing should not stop the app from listing
    the rest.
    """
    root = recipes_dir(folder)
    if not root.is_dir():
        return ()
    found: list[Recipe] = []
    for path in sorted(root.glob("*.txt")):
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - unreadable file
            continue
        title = ""
        note = ""
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if not stripped.startswith("#"):
                break
            text_line = stripped.lstrip("#").strip()
            lowered = text_line.lower()
            if lowered.startswith("title:"):
                title = text_line.split(":", 1)[1].strip()
            elif lowered.startswith("note:"):
                note = text_line.split(":", 1)[1].strip()
        found.append(
            Recipe(
                name=path.stem.strip(),
                title=title or path.stem,
                note=note or f"your own recipe ({path.name})",
                steps=body,
                version=hashlib.sha1(body.encode("utf-8")).hexdigest()[:6],
            )
        )
    return tuple(item for item in found if item.name)


def all_recipes(folder: str | Path | None = None) -> tuple[Recipe, ...]:
    """The built-in recipes, then the user's own (which win on a name clash)."""
    custom = load_custom_recipes(folder)
    taken = {recipe.name for recipe in custom}
    return tuple(recipe for recipe in RECIPES if recipe.name not in taken) + custom


def parse_sets(pairs: Any) -> dict[str, str]:
    """``--set KEY=VALUE`` pairs as the variables one run should see.

    A recipe is written once and run for many sites, so the differences belong on
    the command line rather than in the file: ``--set coupon=SAVE10`` fills every
    ``${coupon}`` in the steps for that run only, and leaves the environment (and
    the file) alone.
    """
    values: dict[str, str] = {}
    for item in pairs or []:
        key, separator, value = str(item).partition("=")
        key = key.strip()
        if not separator or not key:
            raise JourneyError(f"--set wants KEY=VALUE, not '{item}'")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise JourneyError(f"'{key}' is not a variable name (letters, digits and _)")
        values[key] = value
    return values


def save_recipe(
    name: str,
    steps: str,
    folder: str | Path | None = None,
    title: str = "",
    note: str = "",
) -> Path:
    """Write ``steps`` to ``<recipes>/<name>.txt`` and return that path."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "").strip()).strip("-")
    if not safe:
        raise JourneyError("a recipe needs a name (letters, digits, - and _).")
    body = ""
    if title or note:
        body += f"# Title: {title or safe}\n"
        if note:
            body += f"# Note: {note}\n"
    body += str(steps or "").rstrip() + "\n"
    try:
        # Validate before saving: a file that does not parse helps nobody.
        parse_steps_text(body)
    except JourneyError as exc:
        raise JourneyError(f"these steps cannot be saved: {exc}") from exc
    root = recipes_dir(folder)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{safe}.txt"
    path.write_text(body, encoding="utf-8")
    return path


def recipe_for(name: str, folder: str | Path | None = None) -> Recipe:
    """The recipe called ``name`` (case-insensitive; a prefix will do)."""
    wanted = str(name or "").strip().lower()
    available = all_recipes(folder)
    for recipe in available:
        if recipe.name == wanted:
            return recipe
    for recipe in available:
        if recipe.name.startswith(wanted) and wanted:
            return recipe
    raise JourneyError(
        f"unknown recipe '{name}' (known: {', '.join(item.name for item in available)}). "
        "'journey --recipes' lists them."
    )


def recipes_text(folder: str | Path | None = None) -> str:
    """The ``--recipes`` listing: name, description and the first few lines."""
    mine = {recipe.name for recipe in load_custom_recipes(folder)}
    lines = []
    for recipe in all_recipes(folder):
        mark = f"  (your own, v{recipe.version})" if recipe.name in mine else ""
        lines.append(f"{recipe.name:<16} {recipe.title}{mark}")
        lines.append(f"{'':<16} {recipe.note}")
        for step in recipe.lines()[:3]:
            lines.append(f"{'':<18} {step}")
        if len(recipe.lines()) > 3:
            lines.append(f"{'':<18} ... {len(recipe.lines()) - 3} more step(s)")
    lines.append(f"Your own recipes live in {recipes_dir(folder)}")
    return "\n".join(lines)


def compare_recipe(name: str, folder: str | Path | None = None) -> dict[str, Any]:
    """Compare a loaded recipe to the file that produced it.

    Returns {"same": bool, "before": str, "after": str, "file": str} so a CLI
    can print the diff and a test can assert the contract.
    """
    file_path = None
    # Find the file by name
    for root in [recipes_dir(folder) if folder else recipes_dir()] if folder else [recipes_dir()]:
        if isinstance(root, str):
            root = Path(root)
        for f in root.glob("*.txt"):
            if f.stem == name:
                file_path = f
                break
        if file_path:
            break
    try:
        recipe = recipe_for(name, folder)
    except JourneyError:
        return {"same": False, "before": "(missing)", "after": "(missing)", "file": str(file_path or "")}
    current = recipe.steps
    file_text = file_path.read_text(encoding="utf-8") if file_path else ""
    # Strip header lines for comparison (Title/Note comments)
    file_lines = [line for line in file_text.splitlines() if not line.startswith("# ")]
    before = "\n".join(file_lines).strip()
    same = current.strip() == before.strip()
    return {"same": same, "before": before, "after": current.strip(), "file": str(file_path or "")}


def sample_journey() -> str:
    """The commented starter file ``journey --sample`` prints."""
    return SAMPLE_JOURNEY


def shell_hint(path: str | Path) -> str:
    """A copy-pasteable command for the journey file at ``path``."""
    return f"python -m app.cli journey --file {shlex.quote(str(path))} --out ./shots"
