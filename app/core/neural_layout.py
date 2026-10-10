"""AI suggests selectors from DOM density."""
from __future__ import annotations
def suggest_selector(text_hint: str, element_density: float = 0.5) -> str:
    return f"[data-capture-bot-target='{text_hint[:10]}']" if element_density > 0.3 else "#auto"
