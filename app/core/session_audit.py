"""Generate audit HTML from session imports."""
from __future__ import annotations
from pathlib import Path
import json

def generate_audit(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    reports = []
    for f in Path.home().glob(".capture-bot/sessions/*.json"):
        reports.append({"file": f.name, "exists": f.exists()})
    html = "<h1>Session Audit</h1>" + "".join(f"<p>{r}</p>" for r in reports)
    audit = out_dir / "audit.html"
    audit.write_text(html, encoding="utf-8")
    return audit
