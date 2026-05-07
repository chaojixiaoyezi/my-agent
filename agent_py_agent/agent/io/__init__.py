# LLM: Low-level IO module; keep atomic/locked file-write behavior stable across platforms.
# 模块用途: 提供 JSONL 和加锁文件写入等底层文件能力。

from __future__ import annotations

"""public API for low-level local file IO primitives.

给人看的解释：
这里放'很底层、无业务含义'的本地文件读写能力。比如带锁追加 JSONL。
只要函数开始有业务语义，就应该放回对应业务目录，不要把这里变成杂物间。
"""

from .jsonl import append_jsonl, append_line_locked

__all__ = ["append_jsonl", "append_line_locked"]
