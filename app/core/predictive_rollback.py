"""Predict failure from crawl patterns."""
from __future__ import annotations
def risk_score(prev_failures: int, total: int) -> float:
    return prev_failures / max(total, 1)


def predict_failure(prev_failures: int, total: int) -> bool:
    return risk_score(prev_failures, total) > 0.1
