"""Quick smoke tests for batch-20 stubs."""
from __future__ import annotations
from app.core import voice, team_dashboard, smart_proxy, offline_replay, auto_pr

def test_voice_listen():
    v = voice.VoiceControl()
    assert "heard" in v.listen("record")

def test_team_status():
    from pathlib import Path
    s = team_dashboard.team_status(Path.home())
    assert isinstance(s["users"], list)

def test_smart_proxy():
    assert "proxy.local" in smart_proxy.pick_proxy("https://x")

def test_offline_replay_import():
    from app.core.offline_replay import replay, ReplayResult
    assert ReplayResult is not None
