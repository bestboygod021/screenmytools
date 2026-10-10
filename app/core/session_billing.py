"""Charge per session."""
from __future__ import annotations
from pathlib import Path
def charge_for(sessions: int, rate: float = 0.05) -> float:
    return sessions * rate


def profile_charge(profile_name: str, session_count: int) -> float:
    return charge_for(session_count, 0.05)


def charge_localized(sessions: int, currency: str = 'USD') -> str:
    amount = charge_for(sessions)
    return f'{amount:.2f} {currency}'
