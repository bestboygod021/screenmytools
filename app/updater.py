"""Versioned auto-update helpers (Qt-free).

The app checks its GitHub repository's *latest release* and compares the tag to
the running version. Downloading/installing the new ``.exe`` is left to the user
(a single click on the Releases page), which keeps the updater simple, safe and
free of any elevated-privilege installer logic.

Only the standard library is used so both the GUI and the CLI can reuse it, and
the HTTP layer is a tiny injectable function for tests.
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Any

#: Repository that publishes release builds. Override for forks/tests.
UPDATE_REPO = "bestboygod021/screenmytools"

_API_LATEST = "https://api.github.com/repos/{repo}/releases/latest"
_RELEASES_PAGE = "https://github.com/{repo}/releases"

_VERSION_RE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def parse_version(text: str) -> tuple[int, ...]:
    """Parse ``v1.2.3`` / ``1.2`` / ``1`` into a comparable tuple."""
    match = _VERSION_RE.search(text or "")
    if not match:
        return (0, 0, 0)
    parts = [int(g) if g is not None else 0 for g in match.groups()]
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    """True when ``latest`` represents a strictly newer version than ``current``."""
    return parse_version(latest) > parse_version(current)


def _get_json(url: str, timeout: float = 10.0) -> Any:
    """Fetch and decode JSON from ``url``. Injectable for tests."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "FullPageCaptureBot-updater",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https host
        return json.loads(response.read().decode("utf-8"))


def latest_release_tag(repo: str = UPDATE_REPO) -> str | None:
    """Return the latest release tag (e.g. ``v1.2.0``) or ``None`` when offline."""
    try:
        data = _get_json(_API_LATEST.format(repo=repo))
    except Exception:
        return None
    tag = data.get("tag_name") if isinstance(data, dict) else None
    return str(tag) if tag else None


def releases_page(repo: str = UPDATE_REPO) -> str:
    """Where the user can download the newest build."""
    return _RELEASES_PAGE.format(repo=repo)


def check_for_updates(current: str, repo: str = UPDATE_REPO) -> dict:
    """High-level probe used by the UI. Never raises.

    Returns ``{"available": bool, "latest": str|None, "url": str}``.
    """
    tag = latest_release_tag(repo)
    if tag is None:
        return {"available": False, "latest": None, "url": releases_page(repo)}

    available = is_newer(tag, current)
    return {"available": available, "latest": tag, "url": releases_page(repo)}
