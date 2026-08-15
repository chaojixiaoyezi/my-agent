
from __future__ import annotations


def format_activity_text(thinking: str, started_at: float, running: bool, *, now: float) -> str:
    star = _format_activity_star(started_at, running, now=now)
    return f"{star} {_format_thinking_status(thinking, started_at, running, now=now)}"


def _format_activity_star(started_at: float, running: bool, *, now: float) -> str:
    if not running or not started_at:
        return "✦"
    frames = ("✦", "✧", "✶", "✷")
    elapsed = max(0.0, now - started_at)
    return frames[int(elapsed * 4) % len(frames)]


def _format_thinking_status(thinking: str, started_at: float, running: bool, *, now: float) -> str:
    if not running or not started_at:
        return thinking
    elapsed = max(0.0, now - started_at)
    return f"{thinking} {elapsed:.1f}s"
