"""Replay a crawl result without a browser — for CI / audit."""
from __future__ import annotations
from pathlib import Path
import json

class ReplayResult:
    def __init__(self, path: Path) -> None:
        self.data = json.loads(path.read_text(encoding="utf-8"))
    def screens(self) -> list[dict]:
        return self.data.get("screens") or []
    def url(self) -> str:
        return self.data.get("url") or ""

def render_view(path: str | Path) -> str:
    r = replay(path)
    return f'<h1>Offline replay — {r.url()}</h1><p>{len(r.screens())} screen(s)</p>'

def replay(path: str | Path) -> ReplayResult:
    return ReplayResult(Path(path))
