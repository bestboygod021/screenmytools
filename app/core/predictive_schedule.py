"""Suggest best crawl time from previous crawl duration."""
from __future__ import annotations

def best_time(duration_minutes: float) -> str:
    return "02:00" if duration_minutes > 10 else "04:00"
