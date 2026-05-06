from __future__ import annotations

import threading
import time
from typing import Any

from .renderer import strip_ansi

MAX_TRANSCRIPT_CHARS = 200_000
APP_REDRAW_INTERVAL_SECONDS = 1 / 30


class TuiTranscriptStore:
    def __init__(self, output_area: Any, follow_ref: list[bool], app_ref: list[Any]) -> None:
        self.output_area = output_area
        self.follow_ref = follow_ref
        self.app_ref = app_ref
        self.history = ""
        self.live_stream = ""
        self.lock = threading.Lock()
        self.last_render_at = 0.0
        self.render_timer: threading.Timer | None = None

    def append_history(self, text: str) -> None:
        cleaned = strip_ansi(text)
        with self.lock:
            self._commit_live_stream_locked()
            self._append_history_locked(cleaned)
            self._render_locked(force=True)

    def append_stream(self, text: str) -> None:
        cleaned = strip_ansi(text)
        with self.lock:
            was_empty = not self.live_stream
            self.live_stream += cleaned
            self._render_locked(force=was_empty)

    def finish_stream(self) -> None:
        with self.lock:
            self._commit_live_stream_locked()
            self._render_locked(force=True)

    def _append_history_locked(self, text: str) -> None:
        self.history += text
        if len(self.history) > MAX_TRANSCRIPT_CHARS:
            self.history = self.history[-MAX_TRANSCRIPT_CHARS:]

    def _commit_live_stream_locked(self) -> None:
        if not self.live_stream:
            return
        self._append_history_locked(self.live_stream)
        self.live_stream = ""

    def _render_locked(self, *, force: bool) -> None:
        now = time.monotonic()
        if force or now - self.last_render_at >= APP_REDRAW_INTERVAL_SECONDS:
            self._cancel_timer_locked()
            self._render_now_locked(now)
            return
        if self.render_timer is None:
            delay = max(0.0, APP_REDRAW_INTERVAL_SECONDS - (now - self.last_render_at))
            self.render_timer = threading.Timer(delay, self._render_from_timer)
            self.render_timer.daemon = True
            self.render_timer.start()

    def _render_from_timer(self) -> None:
        with self.lock:
            self.render_timer = None
            self._render_now_locked(time.monotonic())

    def _render_now_locked(self, now: float) -> None:
        text = self.history + self.live_stream
        self.output_area.text = text
        if self.follow_ref[0]:
            self.output_area.buffer.cursor_position = len(text)
        self.last_render_at = now
        if self.app_ref[0] is not None:
            self.app_ref[0].invalidate()

    def _cancel_timer_locked(self) -> None:
        if self.render_timer is not None:
            self.render_timer.cancel()
            self.render_timer = None
