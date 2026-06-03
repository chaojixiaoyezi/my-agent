
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .exceptions import ConcurrencyConflictError

if TYPE_CHECKING:
    from ..settings.config import AgentConfig


class OptimisticLock:

    def __init__(self, config: AgentConfig):
        self.config = config
        self._workspace = Path(config.subagent_workspace)

    def _get_lock_path(self, task_id: str) -> Path:
        # 锁文件和任务文件在同一目录
        task_dir = self._workspace / task_id
        return task_dir / "task.json.lock"

    def _ensure_task_dir(self, task_id: str) -> Path:
        task_dir = self._workspace / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        return task_dir

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

    def check(self, task_id: str, expected_version: int) -> bool:
        current_version = self.acquire(task_id)
        return current_version == expected_version

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

    def get_version(self, task_id: str) -> int:
        return self.acquire(task_id)

    def set_version(self, task_id: str, version: int) -> None:
        lock_path = self._get_lock_path(task_id)
        self._ensure_task_dir(task_id)

        data = {
            "version": version,
            "updated_at": time.time(),
        }
        lock_path.write_text(json.dumps(data), encoding="utf-8")

    def remove_lock(self, task_id: str) -> bool:
        lock_path = self._get_lock_path(task_id)
        if lock_path.exists():
            lock_path.unlink()
            return True
        return False


__all__ = ["OptimisticLock"]
