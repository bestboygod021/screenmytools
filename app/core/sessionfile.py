"""Importing a saved browser session (``storage_state.json``).

Playwright writes a session as a small JSON file: the cookies, and the
``localStorage`` of a few origins. Exporting one is easy (Playwright's own
``context.storage_state(path=…)``, or a browser extension), and the app has always
been able to *use* one - what it could not do was tell whether the file was a
session at all, or whether the cookies in it had already expired.

That is the whole job of this module: read the file, say what is inside it, and
reject what is not a session. No Qt, no browser, nothing to time out - so the app
can refuse a bad file *before* a run starts, and a test can do the same.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class SessionFileError(ValueError):
    """A file that is not a usable storage state."""


@dataclass
class SessionInfo:
    """What a storage-state file holds, in numbers a person can act on."""

    path: str = ""
    cookies: int = 0
    origins: int = 0
    hosts: list[str] = field(default_factory=list)
    #: The soonest cookie expiry, as a UTC datetime (``None`` when nothing expires).
    expires_at: datetime | None = None
    local_storage_keys: int = 0

    @property
    def expired(self) -> bool:
        """True when every dated cookie in the file is in the past."""
        return self.expires_at is not None and self.expires_at <= datetime.now(UTC)

    def days_left(self) -> float | None:
        if self.expires_at is None:
            return None
        delta = self.expires_at - datetime.now(UTC)
        return delta.total_seconds() / 86_400

    def summary(self) -> str:
        """One line for the log: what was imported, and whether it still works."""
        if not self.cookies and not self.origins:
            return "The file holds no cookies and no local storage."
        hosts = ", ".join(self.hosts[:4]) + ("…" if len(self.hosts) > 4 else "")
        parts = [f"{self.cookies} cookie(s)"]
        if self.origins:
            parts.append(f"{self.origins} origin(s)")
        if self.local_storage_keys:
            parts.append(f"{self.local_storage_keys} stored value(s)")
        line = f"{', '.join(parts)} for {hosts or 'no host'}"
        left = self.days_left()
        if left is None:
            return line + " - no expiry date (they last until the site says otherwise)"
        if left < 0:
            return line + " - every cookie in it has expired: sign in again"
        return line + f" - the first cookie expires in {left:.1f} day(s)"


def _expiry(cookie: dict[str, Any]) -> datetime | None:
    """A cookie's expiry as a UTC datetime (Playwright writes seconds since 1970)."""
    raw = cookie.get("expires")
    if raw in (None, "", -1, 0):
        return None
    try:
        return datetime.fromtimestamp(float(raw), tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def read_session(path: str | Path, payload: Any = None) -> SessionInfo:
    """Read and check a storage-state file; raises :class:`SessionFileError`.

    ``payload`` skips the file read (used by the tests and by drag & drop, where
    the bytes are already in memory).
    """
    target = Path(path) if path else None
    if payload is None:
        try:
            text = target.read_text(encoding="utf-8") if target else ""
        except OSError as exc:
            raise SessionFileError(f"Could not read {target}: {exc}") from exc
        if not text.strip():
            raise SessionFileError(f"{target} is empty.")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SessionFileError(f"{target} is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise SessionFileError("A session file is a JSON object with 'cookies' and 'origins'.")
    if "cookies" not in payload and "origins" not in payload:
        raise SessionFileError(
            "This is JSON, but it is not a storage state: it needs a 'cookies' or 'origins' key."
        )

    cookies = payload.get("cookies") or []
    origins = payload.get("origins") or []
    if not isinstance(cookies, list) or not isinstance(origins, list):
        raise SessionFileError("'cookies' and 'origins' must both be lists.")

    hosts: list[str] = []
    dates: list[datetime] = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise SessionFileError("Every cookie has to be an object.")
        domain = str(cookie.get("domain") or "").strip().lstrip(".")
        if domain and domain not in hosts:
            hosts.append(domain)
        when = _expiry(cookie)
        if when is not None:
            dates.append(when)

    stored = 0
    for origin in origins:
        if not isinstance(origin, dict):
            raise SessionFileError("Every origin has to be an object.")
        name = str(origin.get("origin") or "").strip()
        if name and name not in hosts:
            hosts.append(name)
        values = origin.get("localStorage") or []
        if not isinstance(values, list):
            raise SessionFileError("'localStorage' has to be a list.")
        stored += len(values)

    return SessionInfo(
        path=str(target or ""),
        cookies=len(cookies),
        origins=len(origins),
        hosts=hosts,
        expires_at=min(dates) if dates else None,
        local_storage_keys=stored,
    )


def default_session_folder() -> Path:
    """Where an imported session is kept when the user does not say otherwise."""
    return Path.home() / ".capture-bot" / "sessions"


def import_session(path: str | Path, folder: str | Path | None = None) -> tuple[Path, SessionInfo]:
    """Check ``path`` and copy it into the app's own session folder.

    Copying matters more than it looks: a session file is usually exported into
    ``Downloads``, where the next cleanup pass deletes it - and a run six weeks
    later should not fail because of that. The copy is validated first, so a file
    that is not a session never lands.
    """
    source = Path(path)
    info = read_session(source)
    destination_dir = Path(folder) if folder else default_session_folder()
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / source.name
    if source.resolve() != destination.resolve():
        destination.write_bytes(source.read_bytes())
    info.path = str(destination)
    return destination, info


def rotate_session(path: str | Path, out_dir: str | Path | None = None) -> Path:
    frozen = freeze_session(path, out_dir)
    # Rotate: keep only last 3 frozen copies
    folder = frozen.parent
    copies = sorted(folder.glob('session-*.json'), key=lambda p: p.stat().st_mtime)
    for old_copy in copies[:-3]:
        old_copy.unlink(missing_ok=True)
    return frozen

def inherit_session(child_path: str | Path, parent_path: str | Path) -> Path:
    from shutil import copy2
    copy2(str(parent_path), child_path)
    return Path(child_path)


def freeze_session(path: str | Path, out_dir: str | Path | None = None) -> Path:
    """Freeze a session file into a dated copy (for version-controlled backups)."""
    source = Path(path)
    read_session(source)  # validate first
    folder = Path(out_dir) if out_dir else default_session_folder() / "frozen"
    folder.mkdir(parents=True, exist_ok=True)
    frozen = folder / f"{source.stem}-{int(__import__('time').time())}{source.suffix}"
    frozen.write_bytes(source.read_bytes())
    return frozen


def expiring_soon(info: SessionInfo, days: float = 3.0) -> bool:
    """True when the session has less than ``days`` left (or none at all)."""
    left = info.days_left()
    if left is None:
        return False
    return left < float(days)


def expires_text(info: SessionInfo) -> str:
    """The expiry as a date, for a label under the field."""
    if info.expires_at is None:
        return "no expiry date"
    stamp = info.expires_at.astimezone(UTC) + timedelta(0)
    return stamp.strftime("%Y-%m-%d %H:%M UTC")


def audit_hash(path: str | Path) -> str:
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
