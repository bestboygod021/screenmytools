"""The step list as editable rows: parse, edit, write back (no Qt in here).

The "Clicks & screens" box is a text box because text is the shortest way to
*write* a click-path. It is a poor way to *edit* one: adding ``optional`` to the
fourth step means counting lines and hoping the quoting is right.

So the app shows the same steps as a table - one row per step, one column per
switch - and this module is the bridge between the two views. It is deliberately
Qt-free: the dialog is paint, this is arithmetic, and arithmetic is what the
tests can pin down.

A row is the step table plus one extra key, ``target``, holding the element part
("``#id``", "``"Sign in"``", "``role=button name="Next"``") exactly as the compact
language spells it. Round-tripping is the contract::

    rows = parse_rows(text)          # text  -> rows  (for the table)
    assert to_text(rows) == text     # rows  -> text  (back to the box)
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.core.journey import JourneyError, parse_step_line, steps_to_text

#: The actions the editor offers, in the order they appear in the drop-down.
EDITOR_ACTIONS: tuple[str, ...] = (
    "capture",
    "click",
    "download",
    "ensure_login",
    "fill",
    "wait_for",
    "wait",
    "scroll",
    "press",
    "goto",
    "select",
    "check",
    "hover",
    "back",
    "reload",
)

#: Actions whose row uses the second (value) column.
VALUE_ACTIONS: frozenset[str] = frozenset({"fill", "select"})

#: Actions whose row uses the target column at all.
TARGET_ACTIONS: frozenset[str] = frozenset(EDITOR_ACTIONS) - {"wait", "back", "reload"}


def step_target(step: dict[str, Any]) -> str:
    """The element part of a step, spelled the way the box spells it."""
    selector = str(step.get("selector") or "").strip()
    text = str(step.get("text") or "").strip()
    role = str(step.get("role") or "").strip()
    name = str(step.get("name") or "").strip()
    if selector:
        return selector
    if role:
        return f'role={role} name="{name}"' if name else f"role={role}"
    if text:
        return f'"{text}"'
    return ""


def step_value(step: dict[str, Any]) -> str:
    """The second column: what a fill types, what a press presses, a wait's ms."""
    action = str(step.get("action") or "")
    if action in VALUE_ACTIONS:
        return str(step.get("value") or "")
    if action == "capture":
        return str(step.get("label") or "")
    if action == "wait":
        return str(int(step.get("ms") or 0))
    if action == "press":
        return str(step.get("key") or "Enter")
    if action == "scroll":
        return str(step.get("value") or "")
    if action == "goto":
        return str(step.get("url") or "")
    return ""


def row_from_step(step: dict[str, Any]) -> dict[str, Any]:
    """One parsed step as one editable row."""
    return {
        "action": str(step.get("action") or "click"),
        "target": step_target(step),
        "value": step_value(step),
        "optional": bool(step.get("optional")),
        "timeout": int(step.get("timeout") or 0),
        "hint": str(step.get("_hint") or ""),
    }


def step_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """One edited row back as a step table (raises :class:`JourneyError`).

    The row is turned into *text* and parsed again, rather than assembled field by
    field. That sounds roundabout and is exactly the point: there is one grammar in
    the program, so a row the editor accepts is a step the runner understands, and
    a typo gets the same error message the text box would give.
    """
    action = str(row.get("action") or "").strip().lower()
    if action not in EDITOR_ACTIONS:
        raise JourneyError(f"'{action}' is not a step this app knows")
    target = str(row.get("target") or "").strip()
    value = str(row.get("value") or "").strip()
    optional = bool(row.get("optional"))
    timeout = int(row.get("timeout") or 0)

    body = target
    if action in VALUE_ACTIONS:
        if not target:
            raise JourneyError(f"'{action}' needs the field (for example #email)")
        if not value:
            # A fill with nothing in it is a mistake, not a step: say so here.
            raise JourneyError(f"'{action}' needs a value to type into {target}")
        body = f"{target} = {value}"
    elif action == "wait":
        body = value or "1000"
        if not body.isdigit():
            raise JourneyError("'wait' needs a number of milliseconds")
    elif action == "capture":
        body = value or "screen"
    elif action == "goto":
        if not value:
            raise JourneyError("'goto' needs a url")
        body = value
    elif action == "scroll":
        body = value or "1"
    elif action == "press":
        if target:
            body = f"{target} {value or 'Enter'}"
        else:
            body = value or "Enter"
    elif action in ("back", "reload"):
        body = ""

    line = f"{action} {body}".strip()
    if optional:
        line += " optional"
    if timeout > 0:
        line += f" timeout={timeout}"
    step = parse_step_line(line)
    step.pop("optional", None)
    step["optional"] = optional
    return step


def _row_target_for_insert(action: str) -> str:
    """A friendly starting point, so a new row is runnable after two keystrokes."""
    if action in ("click", "hover", "check", "download", "wait_for"):
        return '"Sign in"'
    if action == "ensure_login":
        return ".dashboard"
    if action in VALUE_ACTIONS:
        return "#email"
    if action == "press":
        return ""
    if action == "capture":
        return ""
    return ""


def new_row(action: str = "capture") -> dict[str, Any]:
    """A fresh row: the action is chosen, everything else is a sensible default."""
    action = action if action in EDITOR_ACTIONS else "capture"
    row = {
        "action": action,
        "target": _row_target_for_insert(action),
        "value": "",
        "optional": action in ("click", "wait_for", "press"),
        "timeout": 0,
        "hint": "",
    }
    if action == "capture":
        row["value"] = "screen"
    if action == "wait":
        row["value"] = "1000"
    return row


def parse_rows(text: str, env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """The box's text as editable rows (an empty box is simply no rows)."""
    lines = [
        line.strip()
        for line in str(text or "").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not lines:
        return []
    from app.core.journey import parse_step_rows

    return [row_from_step(step) for step in parse_step_rows("\n".join(lines), env)]


def to_text(rows: Sequence[dict[str, Any]]) -> str:
    """The rows as the compact text the box (and a recipe) holds."""
    steps = []
    for index, row in enumerate(rows, start=1):
        try:
            steps.append(step_from_row(row))
        except JourneyError as exc:
            raise JourneyError(f"step {index}: {exc}") from exc
    if not steps:
        return ""
    return steps_to_text(steps)


def move_row(rows: list[dict[str, Any]], index: int, delta: int) -> int:
    """Move one row up (-1) or down (+1); returns where it ended up."""
    if not rows:
        return 0
    target = max(0, min(len(rows) - 1, int(index) + int(delta)))
    if target != index:
        rows.insert(target, rows.pop(index))
    return target


def add_row(rows: list[dict[str, Any]], action: str = "capture", after: int = -1) -> int:
    """Insert a new row after ``after`` (or at the end) and return its index."""
    row = new_row(action)
    at = len(rows) if after < 0 or after >= len(rows) else int(after) + 1
    rows.insert(at, row)
    return at


def remove_row(rows: list[dict[str, Any]], index: int) -> int:
    """Drop a row; returns the index that should stay selected."""
    if not rows:
        return 0
    at = max(0, min(len(rows) - 1, int(index)))
    rows.pop(at)
    return max(0, min(len(rows) - 1, at)) if rows else 0


def describe_rows(rows: Sequence[dict[str, Any]]) -> str:
    """A one-line summary for the dialog's footer: ``5 steps, 2 captures``."""
    captures = sum(1 for row in rows if row.get("action") == "capture")
    optional = sum(1 for row in rows if row.get("optional"))
    parts = [f"{len(rows)} step(s)", f"{captures} capture(s)"]
    if optional:
        parts.append(f"{optional} optional")
    return " - ".join(parts)


def validate_rows(rows: Sequence[dict[str, Any]]) -> dict[int, str]:
    """Every row's problem, keyed by index: ``{2: "'fill' needs a value ..."}``."""
    problems: dict[int, str] = {}
    for index, row in enumerate(rows):
        try:
            step_from_row(row)
        except JourneyError as exc:
            problems[index] = str(exc)
    return problems
