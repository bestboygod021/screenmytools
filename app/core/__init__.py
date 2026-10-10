"""Qt-free automation core.

Nothing in this sub-package may import PyQt6. That rule is what makes the
capture pipeline unit-testable without a display server or a real browser.
"""

from __future__ import annotations

__all__ = ["engine", "settings", "url_utils", "runtime"]
