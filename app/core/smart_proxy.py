"""Smart proxy routing."""
from __future__ import annotations
def pick_proxy(url: str) -> str: return "http://proxy.local:8080"


def pick_proxy_by_region(url: str, region: str = 'global') -> str:
    return f'http://proxy.{region}.local:8080'
