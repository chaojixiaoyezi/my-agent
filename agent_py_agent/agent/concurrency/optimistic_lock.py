# LLM: File-backed optimistic lock helper; task lock JSON shape must stay compatible with existing workspaces.
# 模块用途: 用每个任务目录里的 task.json.lock 记录版本号，帮助并发写入发现冲突。

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .exceptions import ConcurrencyConflictError

if TYPE_CHECKING:
    from ..settings.config import AgentConfig


# LLM: Owns version reads/writes for task lock files; callers rely on ConcurrencyConflictError for stale updates.
# 类用途: 管理任务的乐观锁版本号，写入前检查版本，成功写入后递增版本。
class OptimisticLock:

    # LLM: Captures the configured subagent workspace root; no lock file is written during construction.
    # 函数用途: 保存配置和任务工作区路径，后续所有锁文件都会从这里定位。
    def __init__(self, config: AgentConfig):
        self.config = config
        self._workspace = Path(config.subagent_workspace)

    # LLM: Centralizes lock-file path derivation so every method touches the same task-scoped file.
    # 函数用途: 根据 task_id 算出对应的 task.json.lock 路径。
    def _get_lock_path(self, task_id: str) -> Path:
        # 锁文件和任务文件在同一目录
        task_dir = self._workspace / task_id
        return task_dir / "task.json.lock"

    # LLM: Creates the task directory before writing lock state; keep path derivation paired with _get_lock_path.
    # 函数用途: 确保任务目录存在，给 release/set_version 写锁文件做准备。
    def _ensure_task_dir(self, task_id: str) -> Path:
        task_dir = self._workspace / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        return task_dir

    # LLM: Reads the current version and tolerates missing/corrupt lock files by returning the initial version.
    # 函数用途: 读取当前锁版本；没有锁文件或文件坏了时按初始版本 1 处理。
    def acquire(self, task_id: str) -> int:
        lock_path = self._get_lock_path(task_id)

        if lock_path.exists():
            try:
                data = json.loads(lock_path.read_text(encoding="utf-8"))
                return data.get("version", 1)
            except (json.JSONDecodeError, KeyError, OSError):
                pass

        # 初始版本号为 1
        return 1

    # LLM: Compares expected and current versions without mutating the lock file.
    # 函数用途: 判断调用方手里的版本是否还是最新版本。
    def check(self, task_id: str, expected_version: int) -> bool:
        current_version = self.acquire(task_id)
        return current_version == expected_version

    # LLM: Performs the compare-and-increment write; stale callers receive ConcurrencyConflictError.
    # 函数用途: 确认版本匹配后把锁版本加一并写回文件，版本不匹配就抛并发冲突。
    def release(self, task_id: str, expected_version: int) -> int:
        lock_path = self._get_lock_path(task_id)
        self._ensure_task_dir(task_id)

        current_version = self.acquire(task_id)
        if current_version != expected_version:
            raise ConcurrencyConflictError(
                task_id=task_id,
                expected_version=expected_version,
                actual_version=current_version,
            )

        new_version = expected_version + 1
        data = {
            "version": new_version,
            "updated_at": time.time(),
        }
        lock_path.write_text(json.dumps(data), encoding="utf-8")
        return new_version

    # LLM: Compatibility alias for acquire; keep return semantics identical.
    # 函数用途: 返回任务当前锁版本，方便调用方用更直白的名字读取版本。
    def get_version(self, task_id: str) -> int:
        return self.acquire(task_id)

    # LLM: Directly writes a version for tests/repair flows; preserve JSON keys used by acquire.
    # 函数用途: 手动设置任务锁版本，常用于初始化、修复或测试指定版本。
    def set_version(self, task_id: str, version: int) -> None:
        lock_path = self._get_lock_path(task_id)
        self._ensure_task_dir(task_id)

        data = {
            "version": version,
            "updated_at": time.time(),
        }
        lock_path.write_text(json.dumps(data), encoding="utf-8")

    # LLM: Best-effort cleanup helper; callers use the boolean to know whether a lock file existed.
    # 函数用途: 删除某个任务的锁文件，返回是否真的删到了文件。
    def remove_lock(self, task_id: str) -> bool:
        lock_path = self._get_lock_path(task_id)
        if lock_path.exists():
            lock_path.unlink()
            return True
        return False


__all__ = ["OptimisticLock"]
