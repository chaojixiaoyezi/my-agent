from __future__ import annotations

import threading
import time
from typing import Any

from .renderer import strip_ansi

MAX_TRANSCRIPT_CHARS = 500_000
APP_REDRAW_INTERVAL_SECONDS = 1 / 30

# 当前激活的 transcript store(布局安装时设置, 供 Ctrl+L 清屏等按键直用)。
_ACTIVE_STORE: "TuiTranscriptStore | None" = None


def set_active_transcript_store(store: "TuiTranscriptStore | None") -> None:
    global _ACTIVE_STORE
    _ACTIVE_STORE = store


def clear_active_transcript() -> bool:
    if _ACTIVE_STORE is not None:
        _ACTIVE_STORE.clear()
        return True
    return False


class TuiTranscriptStore:
    """会话运行时 风格对话流: 纯文本存储 + 行前缀标记(> / ⏺ / ⟿), 样式由
    TranscriptLexer 按行前缀渲染——保留 TextArea 的滚动/跟随/光标能力。
    """

    def __init__(
        self,
        output_area: Any,
        follow_ref: list[bool],
        app_ref: list[Any],
        *,
        max_chars: int = MAX_TRANSCRIPT_CHARS,
    ) -> None:
        self.output_area = output_area
        self.follow_ref = follow_ref
        self.app_ref = app_ref
        self.max_chars = max(1, int(max_chars or MAX_TRANSCRIPT_CHARS))
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

    def append_styled(self, style: str, text: str) -> None:
        # 样式由行前缀决定(见 tui_lexer.py), 这里只需把带标记的文本落盘。
        del style
        self.append_history(text)

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

    def clear(self) -> None:
        with self.lock:
            self.history = ""
            self.live_stream = ""
            self._render_locked(force=True)

    def _append_history_locked(self, text: str) -> None:
        self.history += text
        if len(self.history) > self.max_chars:
            self.history = self.history[-self.max_chars:]

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
