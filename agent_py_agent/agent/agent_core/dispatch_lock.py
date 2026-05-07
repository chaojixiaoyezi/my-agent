# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""File lock for dispatch watch loops."""

import json
import os
import time
import uuid
from pathlib import Path

from ..gateway_parts.process_control import is_pid_alive


# LLM: _DispatchWatchLock 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 封装调度监控锁相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
class _DispatchWatchLock:

    # LLM: __init__ 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def __init__(self, path: Path, *, force: bool = False):
        self.path = path
        self.force = force
        self.token = uuid.uuid4().hex
        self.acquired = False

    # LLM: __enter__ 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理enter相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def __enter__(self) -> _DispatchWatchLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prepare_existing_lock()
        payload = self._lock_payload()
        self._write_lock_file(payload)
        self.acquired = True
        return self

    # LLM: __exit__ 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理exit相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def __exit__(self, exc_type, exc, tb) -> None:
        if not self.acquired or not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if payload.get("token") == self.token:
            self.path.unlink()

    # LLM: _prepare_existing_lock 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理prepareexisting锁相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def _prepare_existing_lock(self) -> None:
        if self.force and self.path.exists():
            self.path.unlink()
            return
        if self.path.exists():
            self._remove_stale_lock()

    # LLM: _lock_payload 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理锁载荷相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def _lock_payload(self) -> dict[str, object]:
        return {"token": self.token, "pid": os.getpid(), "created_at": time.time()}

    # LLM: _write_lock_file 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 写入锁文件的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
    def _write_lock_file(self, payload: dict[str, object]) -> None:
        try:
            self._write_lock_file_exclusive(payload)
        except FileExistsError as exc:
            self._retry_write_after_stale_lock(payload, exc)

    # LLM: _write_lock_file_exclusive 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 写入锁文件exclusive的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
    def _write_lock_file_exclusive(self, payload: dict[str, object]) -> None:
        with self.path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2))

    # LLM: _retry_write_after_stale_lock 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理retrywriteafterstale锁相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
    def _retry_write_after_stale_lock(self, payload: dict[str, object], exc: FileExistsError) -> None:
        if not self._remove_stale_lock():
            raise RuntimeError(
                f"dispatch watch lock already exists: {self.path}; "
                "confirm no parent agent is running, then use --force-lock."
            ) from exc
        self._write_lock_file_exclusive(payload)

    # LLM: _remove_stale_lock 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理removestale锁相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def _remove_stale_lock(self) -> bool:
        pid = self._existing_lock_pid()
        if pid is None or is_pid_alive(pid):
            return False
        try:
            self.path.unlink()
        except OSError:
            return False
        return True

    # LLM: _existing_lock_pid 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理existing锁pid相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def _existing_lock_pid(self) -> int | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return int(payload.get("pid", 0))
        except (TypeError, ValueError):
            return None
