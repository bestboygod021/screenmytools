"""Real-time team status."""
from __future__ import annotations
from pathlib import Path
import json
def team_status(out: Path) -> dict:
    users = []
    for f in Path.home().glob(".capture-bot/profiles/*/session_metrics.json"):
        users.append({"profile": f.parent.name, "file": str(f)})
    return {"users": users, "total": len(users)}
