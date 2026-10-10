"""Zero-config first run."""
from __future__ import annotations
from app.core.settings import CaptureSettings
def first_run() -> CaptureSettings:
    s = CaptureSettings()
    s.output_dir = "~/captures/first"
    s.validate()
    return s
