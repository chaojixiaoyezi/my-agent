"""乐观锁实现。

基于版本号的并发控制机制：
- 每个任务有一个版本号
- 保存时检查版本号是否匹配
- 版本号不匹配则抛出 ConcurrencyConflictError
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .exceptions import ConcurrencyConflictError

if TYPE_CHECKING:
    from ..settings.config import AgentConfig


class OptimisticLock:
    """乐观锁管理器。

    使用版本号实现乐观锁：
    - 版本号存储在 task.json.lock 文件中
    - acquire() 获取当前版本号
    - check() 验证版本号是否匹配
    - release() 递增版本号
    """

    def __init__(self, config: AgentConfig):
        """初始化乐观锁。

        Args:
            config: 智能体配置对象
        """
        self.config = config
        self._workspace = Path(config.subagent_workspace)

    def _get_lock_path(self, task_id: str) -> Path:
        """获取锁文件路径。

        Args:
            task_id: 任务 ID

        Returns:
            锁文件路径
        """
        # 锁文件和任务文件在同一目录
        task_dir = self._workspace / task_id
        return task_dir / "task.json.lock"

    def _ensure_task_dir(self, task_id: str) -> Path:
        """确保任务目录存在。

        Args:
            task_id: 任务 ID

        Returns:
            任务目录路径
        """
        task_dir = self._workspace / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        return task_dir

    def acquire(self, task_id: str) -> int:
        """获取当前版本号。

        Args:
            task_id: 任务 ID

        Returns:
            当前版本号
        """
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
        """检查版本号是否匹配。

        Args:
            task_id: 任务 ID
            expected_version: 期望的版本号

        Returns:
            版本号是否匹配
        """
        current_version = self.acquire(task_id)
        return current_version == expected_version

    def release(self, task_id: str, expected_version: int) -> int:
        """释放锁并递增版本号。

        Args:
            task_id: 任务 ID
            expected_version: 期望的版本号（用于验证）

        Returns:
            新版本号

        Raises:
            ConcurrencyConflictError: 版本号不匹配
        """
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
        """获取当前版本号（不带验证）。

        Args:
            task_id: 任务 ID

        Returns:
            当前版本号
        """
        return self.acquire(task_id)

    def set_version(self, task_id: str, version: int) -> None:
        """设置版本号（用于初始化或修复）。

        Args:
            task_id: 任务 ID
            version: 版本号
        """
        lock_path = self._get_lock_path(task_id)
        self._ensure_task_dir(task_id)

        data = {
            "version": version,
            "updated_at": time.time(),
        }
        lock_path.write_text(json.dumps(data), encoding="utf-8")

    def remove_lock(self, task_id: str) -> bool:
        """删除锁文件。

        Args:
            task_id: 任务 ID

        Returns:
            是否成功删除
        """
        lock_path = self._get_lock_path(task_id)
        if lock_path.exists():
            lock_path.unlink()
            return True
        return False


__all__ = ["OptimisticLock"]
