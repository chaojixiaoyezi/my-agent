"""模型助手 Code 风格的思考指示器。

在模型生成响应时显示动画 spinner，包含随机中文短语和已用时间。
收到第一个文本 chunk 后自动停止。

用法：
    spinner = ThinkingSpinner()
    spinner.start()
    # ... 模型生成中 ...
    spinner.stop()  # 收到第一个 chunk 时调用

    # 或者用 context manager：
    with ThinkingSpinner() as spinner:
        result = agent.run(prompt, on_chunk=make_chunk_handler(spinner))
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable

from .thinking_phrases import random_phrase

# LLM: Braille spinner characters for smooth animation.
_SPINNER_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class ThinkingSpinner:
    """模型助手 Code 风格的思考指示器。

    人在看的解释：
    模型思考时，终端会显示一行不断变化的文字和动画符号，
    比如「╭ 蛐蛐人：努力工作中... ⠋ 3.2s」。
    等模型开始输出内容后，这行会自动消失。
    """

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
        """启动后台线程，开始显示动画。"""
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
        """停止动画并清除 spinner 行。线程安全，可重复调用。"""
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
        """后台线程：每 0.12 秒刷新一帧，每 25 帧换一次短语。"""
        idx = 0
        while True:
            with self._lock:
                if not self._running:
                    break
            # LLM: rotate phrase every ~3 seconds (25 frames * 0.12s).
            if idx % 25 == 0:
                self._phrase = random_phrase()
            elapsed = time.perf_counter() - self._start_time
            char = _SPINNER_CHARS[idx % len(_SPINNER_CHARS)]
            frame = f"\r╭ 蛐蛐人：{self._phrase}... {char} {elapsed:.1f}s"
            if self._on_update is not None:
                self._on_update(frame.lstrip("\r"))
            else:
                sys.stdout.write(frame)
                sys.stdout.flush()
            idx += 1
            time.sleep(0.12)

    def __enter__(self) -> ThinkingSpinner:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()
