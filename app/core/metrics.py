"""Session-level telemetry — written after every run, read by /metrics."""
from __future__ import annotations
from pathlib import Path
from typing import Any
import json, time

class SessionMetrics:
    def __init__(self, out: Path | str = ".") -> None:
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.file = self.out / "session_metrics.json"

    def alert_on_failures(self, min_failures: int = 3) -> str:
        data = []
        if self.file.exists():
            try: data = __import__('json').loads(self.file.read_text())
            except Exception: pass
        fails = sum(1 for e in data if e.get('status') != 'ok')
        return f'ALERT: {fails} failures (threshold {min_failures})' if fails >= min_failures else f'OK: {fails}'

    def record(self, url: str, duration_ms: float, status: str, steps: int, captures: int) -> None:
        entry = {
            "ts": time.time(), "url": url, "duration_ms": duration_ms,
            "status": status, "steps": steps, "captures": captures,
        }
        data = []
        if self.file.exists():
            try:
                data = json.loads(self.file.read_text(encoding="utf-8"))
            except Exception:
                pass
        data.append(entry)
        self.file.write_text(json.dumps(data[-500:]), encoding="utf-8")


def team_dashboard() -> dict:
    from app.core.team_dashboard import team_status
    return team_status(Path.home())
