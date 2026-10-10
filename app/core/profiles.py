"""Named configuration profiles (Qt-free).

A profile bundles a full :class:`~app.core.settings.CaptureSettings` snapshot
plus the URL list, so one machine can hold independent per-project setups
(e.g. "client-a daily", "staging monitors"). Profiles live as JSON files in
``<base>/profiles/<name>.json`` where ``base`` is the app config directory
(the GUI passes the QSettings folder; tests pass a temp dir).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

_SAFE_RE = re.compile(r"[^a-z0-9-_]+")


def default_base() -> Path:
    """The folder profiles live in when the GUI did not pass one (Qt-free).

    Mirrors ``QStandardPaths::AppConfigLocation`` (``~/.config/<Org>/<App>`` on
    Linux, ``%APPDATA%/<Org>/<App>`` on Windows) so the headless CLI finds the
    same profiles the desktop app wrote.
    """
    import os

    from app.version import APP_NAME, ORG_NAME

    app_dir = APP_NAME.replace(" ", "") or "FullPageCaptureBot"
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        root = os.environ.get("APPDATA") or os.path.expanduser("~")
        return Path(root) / ORG_NAME / app_dir
    if sys.platform == "darwin":  # pragma: no cover - exercised on macOS only
        return Path(os.path.expanduser("~/Library/Application Support")) / ORG_NAME / app_dir
    root = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(root) / ORG_NAME / app_dir


def digest_plan(base: Path, fallback_days: int = 7) -> list[dict[str, Any]]:
    """Per-profile digest settings: window (days) and tracking issue.

    A profile that never chose a window falls back to ``fallback_days``, so the
    plan is always usable. ``issue`` is ``0`` when the profile has none.
    """
    plan: list[dict[str, Any]] = []
    for name in list_profiles(base):
        data = load_profile(base, name) or {}
        settings = data.get("settings") or {}
        try:
            days = int(settings.get("digest_days") or fallback_days)
        except (TypeError, ValueError):
            days = fallback_days
        try:
            issue = int(settings.get("digest_issue_number") or 0)
        except (TypeError, ValueError):
            issue = 0
        plan.append(
            {
                "name": name,
                "days": max(1, days),
                "issue": max(0, issue),
                "output_dir": str(settings.get("output_dir") or ""),
            }
        )
    return plan


def profiles_dir(base: Path) -> Path:
    return Path(base) / "profiles"


def _safe_name(name: str) -> str:
    cleaned = _SAFE_RE.sub("-", (name or "").strip().lower()).strip("-.")
    return cleaned or "profile"


def _path_for(base: Path, name: str) -> Path:
    return profiles_dir(base) / f"{_safe_name(name)}.json"


def save_profile(base: Path, name: str, settings: dict, urls: str) -> Path:
    """Persist one profile; returns the file that was written."""
    path = _path_for(base, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"name": name, "settings": settings, "urls": urls}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_profile(base: Path, name: str) -> dict[str, Any] | None:
    """Return a stored profile or ``None`` when it does not exist / is corrupt."""
    path = _path_for(base, name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def list_profiles(base: Path) -> list[str]:
    directory = profiles_dir(base)
    if not directory.exists():
        return []
    return sorted(p.stem for p in directory.glob("*.json"))


def delete_profile(base: Path, name: str) -> bool:
    path = _path_for(base, name)
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False
