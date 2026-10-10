"""Voice control stub."""
from __future__ import annotations
class VoiceControl:
    def listen(self, command: str) -> str: return f"heard: {command}"


def start_recording() -> VoiceControl:
    return VoiceControl()
