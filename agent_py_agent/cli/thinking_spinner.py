
from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable

from .thinking_phrases import random_phrase

# LLM: Braille spinner characters for smooth animation.
_SPINNER_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class ThinkingSpinner:

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        on_update: Callable[[str], None] | None = None,
        on_stop: Callable[[], None] | None = None,
    ):
        if enabled is None:
            enabled = bool(getattr(sys.stdout, "isatty", lambda: False)())
        self._enabled = enabled
        self._on_update = on_update
        self._on_stop = on_stop
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._phrase = random_phrase()
        self._start_time = 0.0

    def start(self) -> None:
        if not self._enabled:
            return
        with self._lock:
            if self._running:
                return
            self._running = True
            self._start_time = time.perf_counter()
            self._phrase = random_phrase()
            self._thread = threading.Thread(target=self._animate, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        if not self._enabled:
            return
        with self._lock:
            if not self._running:
                return
            self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._on_stop is not None:
            self._on_stop()
            return
        # LLM: clear the spinner line and move to next line.
        sys.stdout.write("\r" + " " * 80 + "\r\n")
        sys.stdout.flush()

    def _animate(self) -> None:
        idx = 0
        while self._is_running():
            # LLM: rotate phrase every ~3 seconds (25 frames * 0.12s).
            if idx % 25 == 0:
                self._phrase = random_phrase()
            self._emit_frame(idx)
            idx += 1
            time.sleep(0.12)

    def _is_running(self) -> bool:
        with self._lock:
            return self._running

    def _emit_frame(self, idx: int) -> None:
        elapsed = time.perf_counter() - self._start_time
        char = _SPINNER_CHARS[idx % len(_SPINNER_CHARS)]
        frame = f"\r╭ 蛐蛐人：{self._phrase}... {char} {elapsed:.1f}s"
        if self._on_update is not None:
            self._on_update(frame.lstrip("\r"))
            return
        sys.stdout.write(frame)
        sys.stdout.flush()

    def __enter__(self) -> ThinkingSpinner:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()
