# LLM: Gateway service module; keep file-queue, daemon, HTTP, and audit contracts stable.
# 模块用途: 拆分 gateway 请求队列、守护进程、HTTP 处理和响应渲染逻辑。

from __future__ import annotations

"""Chunk file handling for streaming gateway output.

This module provides utilities for writing chunk files that CLI polls for streaming display.
"""

import json
import time
from pathlib import Path


# LLM: open_chunk_stream 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理开放chunk流式相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def open_chunk_stream(chunk_path: Path) -> tuple[Path, float]:
    chunk_path.parent.mkdir(parents=True, exist_ok=True)
    return chunk_path, time.time()


# LLM: write_chunk 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 写入chunk的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
def write_chunk(chunk_path: Path, text: str) -> None:
    try:
        line = json.dumps({"t": time.time(), "text": text}, ensure_ascii=False)
        with open(chunk_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# LLM: close_chunk_stream 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理closechunk流式相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def close_chunk_stream(chunk_path: Path) -> None:
    # Keep the chunk file after completion so clients that observe the final
    # response first can still drain the last streamed tokens.
    return
