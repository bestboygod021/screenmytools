"""AI crawl time budget."""
from __future__ import annotations
def budget_for(page_element_count: int, base: int = 30_000) -> int:
    return max(base, int(base * (1 + page_element_count / 50)))

from datetime import datetime, timezone
def budget_for_local(index: int) -> int:
    return budget_for(index) + (3600 if datetime.now(timezone.utc).hour > 12 else 0)
