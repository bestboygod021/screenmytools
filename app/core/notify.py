"""Best-effort desktop notifications (Qt-free).

Tries a few backends in order and returns True if one accepted the message.
It never raises - a notification failure must not disturb a capture run.
"""

from __future__ import annotations

import json
import shutil
import subprocess


def _plyer(title: str, message: str) -> bool:
    try:
        from plyer import notification  # type: ignore
    except Exception:  # noqa: BLE001 - optional dependency
        return False
    try:
        notification.notify(title=title, message=message, timeout=5)
        return True
    except Exception:  # noqa: BLE001
        return False


def _notify_send(title: str, message: str) -> bool:
    if shutil.which("notify-send") is None:
        return False
    try:
        subprocess.run(["notify-send", title, message], check=False, timeout=5)
        return True
    except Exception:  # noqa: BLE001
        return False


def _osascript(title: str, message: str) -> bool:
    if shutil.which("osascript") is None:
        return False
    script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=5)
        return True
    except Exception:  # noqa: BLE001
        return False


def send_notification(title: str, message: str) -> bool:
    """Show a desktop notification; True if some backend handled it."""
    # Resolved at call time so the backends stay individually patchable.
    for backend in (_plyer, _notify_send, _osascript):
        try:
            if backend(title, message):
                return True
        except Exception:  # noqa: BLE001 - keep trying the next backend
            continue
    return False
