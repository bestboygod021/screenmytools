"""Shared pytest configuration.

The repository root is added to ``sys.path`` so ``import app...`` works
regardless of the directory pytest is invoked from, and Qt is forced onto the
``offscreen`` platform so the UI tests run on machines without a display
server (CI containers, headless VMs).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Must be set before Qt is imported.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", "")

import pytest  # noqa: E402

from app.core.settings import CaptureSettings  # noqa: E402


@pytest.fixture
def tmp_output(tmp_path: Path) -> Path:
    folder = tmp_path / "shots"
    folder.mkdir()
    return folder


@pytest.fixture
def settings(tmp_output: Path) -> CaptureSettings:
    """Fast, deterministic settings used by most engine tests."""
    return CaptureSettings(
        output_dir=str(tmp_output),
        headless=True,
        settle_delay_ms=0,
        network_idle_timeout_ms=0,
        retries=0,
        scroll_to_load_lazy_content=False,
        write_log_file=False,
        device_scale_factor=1.0,
    )
