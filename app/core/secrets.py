"""Secrets that should not sit in a settings file.

A bot's ``QSettings`` (or a profile JSON) is exactly the kind of file people copy
to a new machine, paste into a ticket, or sync through a folder they share: the
SMTP password does not belong there. This module keeps it in whatever secret
store the operating system already has - Windows Credential Locker (DPAPI),
macOS Keychain, a Linux Secret Service - through :mod:`keyring`, and hands the
rest of the app one function to read it back.

Nothing here is mandatory. Without a usable backend (a bare container, a
headless box with no session bus, keyring not installed) :func:`available`
returns ``False``, :func:`load` returns an empty string, and the app falls back
to the old behaviour instead of refusing to send mail.
"""

from __future__ import annotations

import os
from typing import Any

#: The in-process store behind ``CAPTURE_SECRETS=memory``.
_MEMORY: dict[str, str] = {}

#: Keyring service name: everything this app stores lives under it.
SERVICE = "fullpage-capture-bot"

#: The account names we use.
SMTP_PASSWORD = "smtp_password"

#: What the UI shows in place of a stored secret.
MASK = "********"


def is_mask(value: str) -> bool:
    """Whether a field's text is just the placeholder we put there."""
    return str(value or "").strip() == MASK


def _keyring() -> Any:
    """The keyring module, or ``None`` when it is not installed."""
    try:
        import keyring  # noqa: PLC0415 - optional dependency, imported on demand
    except Exception:  # noqa: BLE001 - any import problem means "no keyring"
        return None
    return keyring


def _env_override() -> str:
    """``CAPTURE_SECRETS=memory`` (or ``off``) for tests and odd servers.

    ``memory`` swaps in keyring's in-process backend, which keeps the code path
    identical on a machine where the real store is unavailable; ``off`` makes the
    whole module behave as if keyring were not installed.
    """
    return str(os.environ.get("CAPTURE_SECRETS", "")).strip().lower()


def _memory_backend() -> Any:
    """An in-process keyring, for tests and for ``CAPTURE_SECRETS=memory``.

    It installs itself as the active backend, so every call below walks exactly
    the same path a real store would - which is what makes the tests worth having
    on a machine (like a container) that has no OS keychain at all.
    """
    from keyring.backend import KeyringBackend  # noqa: PLC0415

    class MemoryBackend(KeyringBackend):  # type: ignore[misc]
        priority = 100  # type: ignore[assignment]

        def get_password(self, service: str, username: str) -> str | None:
            return _MEMORY.get(f"{service}:{username}")

        def set_password(self, service: str, username: str, password: str) -> None:
            _MEMORY[f"{service}:{username}"] = password

        def delete_password(self, service: str, username: str) -> None:
            _MEMORY.pop(f"{service}:{username}", None)

    return MemoryBackend()


def _active() -> Any:
    """The keyring module with an appropriate backend, or ``None``."""
    override = _env_override()
    if override in ("off", "none", "0", "false"):
        return None
    module = _keyring()
    if module is None:
        return None
    if override in ("memory", "fake"):
        module.set_keyring(_memory_backend())
    return module


def available() -> bool:
    """Whether a working secret store is reachable right now."""
    module = _active()
    if module is None:
        return False
    try:
        backend = module.get_keyring()
    except Exception:  # noqa: BLE001 - a broken backend must not break a run
        return False
    if backend is None:
        return False
    # keyring's "fail" backend is how it says "there is no OS keychain here".
    return not backend.__class__.__module__.endswith("backends.fail")


#: Module -> the name a Windows or macOS user actually recognises.
_FRIENDLY = {
    "keyring.backends.Windows": "Windows Credential Locker",
    "keyring.backends.macOS": "macOS Keychain",
    "keyring.backends.SecretService": "Secret Service (GNOME Keyring / KWallet)",
    "keyring.backends.kwallet": "KWallet",
    "keyring.backends.chainer": "the chained system keychains",
    "keyring.backends.libsecret": "libsecret",
}


def backend_name() -> str:
    """A human name for the store ("Windows Credential Locker", ...), else ``""``."""
    if _env_override() in ("memory", "fake"):
        return "the in-process test keychain"
    module = _active() if available() else None
    if module is None:
        return ""
    try:
        backend = module.get_keyring()
    except Exception:  # noqa: BLE001
        return ""
    owner = backend.__class__.__module__
    if owner in _FRIENDLY:
        return _FRIENDLY[owner]
    return str(getattr(backend, "name", "") or backend.__class__.__name__)


def store(value: str, account: str = SMTP_PASSWORD) -> bool:
    """Save ``value`` under ``account``. Returns whether it is now stored."""
    if not str(value or "").strip():
        return forget(account)
    if not available():
        return False
    module = _keyring()
    assert module is not None
    try:
        module.set_password(SERVICE, account, value)
    except Exception:  # noqa: BLE001
        return False
    return True


def load(account: str = SMTP_PASSWORD) -> str:
    """The stored secret, or ``""`` when there is none (or no store)."""
    if not available():
        return ""
    module = _keyring()
    assert module is not None
    try:
        return str(module.get_password(SERVICE, account) or "")
    except Exception:  # noqa: BLE001
        return ""


def forget(account: str = SMTP_PASSWORD) -> bool:
    """Remove the stored secret (``False`` when there was nothing to remove)."""
    if not available():
        return False
    module = _keyring()
    assert module is not None
    try:
        existed = module.get_password(SERVICE, account) is not None
    except Exception:  # noqa: BLE001 - a backend that cannot be read: try to delete anyway
        existed = True
    if not existed:
        return False
    try:
        module.delete_password(SERVICE, account)
    except Exception:  # noqa: BLE001 - "no such entry" is not an error here
        return False
    return True


def smtp_password(settings: Any) -> str:
    """The password to send mail with: the settings first, the store second.

    A settings value wins, so a CLI flag or a per-profile override still works
    for a one-off; anything left empty falls back to the store, which is what the
    desktop app now writes.
    """
    direct = str(getattr(settings, "smtp_password", "") or "")
    if direct and not is_mask(direct):
        return direct
    return load(SMTP_PASSWORD)


def resolve(settings: Any) -> Any:
    """``settings`` with ``smtp_password`` filled in from the store.

    Returns the same object (mutated) so callers keep their other fields; this is
    the one call an email sender has to make to stop caring where the password
    lives.
    """
    if not str(getattr(settings, "smtp_password", "") or "") and available():
        found = load(SMTP_PASSWORD)
        if found:
            try:
                settings.smtp_password = found
            except Exception:  # noqa: BLE001 - a frozen settings object is fine
                pass
    return settings
