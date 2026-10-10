"""Durable pending-URL queue (Qt-free).

The GUI writes the batch to ``<base>/queue.json`` when a run starts and clears
it on a clean finish. If the app is closed or crashes mid-run, the file survives
and the next launch can restore the unfinished URLs.
"""

from __future__ import annotations

import json
from pathlib import Path


def _path(base: str | Path) -> Path:
    return Path(base) / "queue.json"


def save_queue(base: str | Path, urls: list[str]) -> Path:
    path = _path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"urls": list(urls)}, ensure_ascii=False), encoding="utf-8")
    return path


def load_queue(base: str | Path) -> list[str]:
    path = _path(base)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    urls = data.get("urls") if isinstance(data, dict) else None
    if not isinstance(urls, list):
        return []
    return [u for u in urls if isinstance(u, str)]


def clear_queue(base: str | Path) -> bool:
    path = _path(base)
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError:  # pragma: no cover - best effort
        return False
