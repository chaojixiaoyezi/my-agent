# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。


from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable

from .thinking_phrases import random_phrase

_SPINNER_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


# LLM: ThinkingSpinner 是CLI 命令层的数据契约；字段名会被调用方和测试读取。
# 类用途: 定义本模块对外传递的数据字段，字段名需要和调用方保持一致。
class ThinkingSpinner:

    # LLM: __init__ 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
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

    # LLM: start 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
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

    # LLM: stop 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
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
        sys.stdout.write("\r" + " " * 80 + "\r\n")
        sys.stdout.flush()

    # LLM: _animate 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def _animate(self) -> None:
        idx = 0
        while self._is_running():
            if idx % 25 == 0:
                self._phrase = random_phrase()
            self._emit_frame(idx)
            idx += 1
            time.sleep(0.12)

    # LLM: _is_running 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
    # 函数用途: 判断输入或环境是否满足规则，结果会影响分支、告警或阻断。
    def _is_running(self) -> bool:
        with self._lock:
            return self._running

    # LLM: _emit_frame 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def _emit_frame(self, idx: int) -> None:
        elapsed = time.perf_counter() - self._start_time
        char = _SPINNER_CHARS[idx % len(_SPINNER_CHARS)]
        frame = f"\r╭ 蛐蛐人：{self._phrase}... {char} {elapsed:.1f}s"
        if self._on_update is not None:
            self._on_update(frame.lstrip("\r"))
            return
        sys.stdout.write(frame)
        sys.stdout.flush()

    # LLM: __enter__ 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def __enter__(self) -> ThinkingSpinner:
        self.start()
        return self

    # LLM: __exit__ 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def __exit__(self, *_exc: object) -> None:
        self.stop()
