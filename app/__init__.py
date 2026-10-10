"""FullPage Capture Bot - automated full-page website screenshot tool.

The package is split into three layers so that each one can be understood,
tested and replaced on its own:

* :mod:`app.core`  - pure Python automation logic (Playwright). No Qt imports.
* :mod:`app.ui`    - PyQt6 widgets and layout. No Playwright imports.
* :mod:`app.worker`- the bridge between the two, running the engine on a
                     background thread and forwarding events to the UI thread.
"""

from __future__ import annotations

from app.version import APP_NAME, ORG_NAME, __version__

__all__ = ["APP_NAME", "ORG_NAME", "__version__"]

try:
    from app.core import journey
except Exception as exc:
    import sys; sys.stderr.write(f"[IMPORT-GUARD] {exc}\n")
    raise
