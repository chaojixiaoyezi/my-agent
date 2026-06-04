"""端到端任务生命周期测试。

测试完整任务生命周期：创建 → 执行 → 完成/失败，以及状态转换。
尽量少用 mock，使用真实的 LocalStore 和任务注册表。
"""
from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.subagents.models import TaskStatus
from agent_py_agent.agent.task_registry import TaskRegistry


@pytest.fixture
def temp_store():
    """创建临时 LocalStore 实例。"""
    with tempfile.TemporaryDirectory() as td:
        store = LocalStore(Path(td) / "test.db")
        yield store


@pytest.fixture
def registry(temp_store):
    """创建 TaskRegistry 实例。"""
    return temp_store.task_registry


class TestTaskLifecycle:
    """测试任务完整生命周期。"""

    def test_task_creation_and_lookup(self, registry):
        """测试任务创建和查询。

        验证任务创建后可以正确查询到。
        """
        task_id = "task-001"
        registry.register_task(
            task_id=task_id,
            status=TaskStatus.PLANNING.value,
            goal="测试任务创建",
            session_id="sess-1",
            user_id="user-1",
        )

        result = registry.lookup_task(task_id)
        assert result is not None
        assert result["task_id"] == task_id
        assert result["status"] == TaskStatus.PLANNING.value
        assert result["goal"] == "测试任务创建"

    def test_task_planning_to_running_transition(self, registry):
        """测试任务从 PLANNING 到 RUNNING 的转换。

        验证状态可以正确更新。
        """
        task_id = "task-002"
        registry.register_task(
            task_id=task_id,
            status=TaskStatus.PLANNING.value,
            goal="计划中任务",
        )

        ok = registry.update_task_status(task_id, TaskStatus.RUNNING.value)
        assert ok is True

        result = registry.lookup_task(task_id)
        assert result["status"] == TaskStatus.RUNNING.value

    def test_task_running_to_completed_transition(self, registry):
        """测试任务从 RUNNING 到 COMPLETED 的转换。

        验证正常完成流程。
        """
        task_id = "task-003"
        registry.register_task(
            task_id=task_id,
            status=TaskStatus.RUNNING.value,
            goal="运行中任务",
        )

        ok = registry.update_task_status(task_id, TaskStatus.COMPLETED.value)
        assert ok is True

        result = registry.lookup_task(task_id)
        assert result["status"] == TaskStatus.COMPLETED.value

    def test_task_running_to_failed_transition(self, registry):
        """测试任务从 RUNNING 到 FAILED 的转换。

        验证失败流程。
        """
        task_id = "task-004"
        registry.register_task(
            task_id=task_id,
            status=TaskStatus.RUNNING.value,
            goal="会失败的任务",
        )

        ok = registry.update_task_status(task_id, TaskStatus.FAILED.value)
        assert ok is True

        result = registry.lookup_task(task_id)
        assert result["status"] == TaskStatus.FAILED.value

    def test_task_pause_and_resume_cycle(self, registry):
        """测试任务暂停和恢复循环。

        验证 RUNNING → PAUSED → RUNNING 转换正确。
        """
        task_id = "task-005"
        registry.register_task(
            task_id=task_id,
            status=TaskStatus.RUNNING.value,
            goal="可暂停的任务",
        )

        # 暂停
        ok = registry.update_task_status(task_id, TaskStatus.PAUSED.value)
        assert ok is True
        result = registry.lookup_task(task_id)
        assert result["status"] == TaskStatus.PAUSED.value

        # 恢复
        ok = registry.update_task_status(task_id, TaskStatus.RUNNING.value)
        assert ok is True
        result = registry.lookup_task(task_id)
        assert result["status"] == TaskStatus.RUNNING.value

    def test_task_abandon_from_running(self, registry):
        """测试从 RUNNING 放弃任务。

        验证 RUNNING → ABANDONED 转换。
        """
        task_id = "task-006"
        registry.register_task(
            task_id=task_id,
            status=TaskStatus.RUNNING.value,
            goal="要放弃的任务",
        )

        ok = registry.update_task_status(task_id, TaskStatus.ABANDONED.value)
        assert ok is True

        result = registry.lookup_task(task_id)
        assert result["status"] == TaskStatus.ABANDONED.value

    def test_task_multiple_status_updates(self, registry):
        """测试任务多次状态更新。

        验证状态时间线正确：PLANNING → RUNNING → PAUSED → RUNNING → COMPLETED。
        """
        task_id = "task-007"
        registry.register_task(
            task_id=task_id,
            status=TaskStatus.PLANNING.value,
            goal="多次状态更新任务",
        )

        # PLANNING → RUNNING
        registry.update_task_status(task_id, TaskStatus.RUNNING.value)
        # RUNNING → PAUSED
        registry.update_task_status(task_id, TaskStatus.PAUSED.value)
        # PAUSED → RUNNING
        registry.update_task_status(task_id, TaskStatus.RUNNING.value)
        # RUNNING → COMPLETED
        registry.update_task_status(task_id, TaskStatus.COMPLETED.value)

        result = registry.lookup_task(task_id)
        assert result["status"] == TaskStatus.COMPLETED.value


class TestTaskQuery:
    """测试任务查询功能。"""

    def test_query_tasks_by_user_id(self, registry):
        """测试按用户 ID 查询任务。

        验证可以正确过滤特定用户的任务。
        """
        # 创建多个用户的任务
        registry.register_task(
            task_id="task-u1-1",
            status=TaskStatus.RUNNING.value,
            goal="用户1的任务",
            user_id="user-1",
        )
        registry.register_task(
            task_id="task-u1-2",
            status=TaskStatus.COMPLETED.value,
            goal="用户1的另一个任务",
            user_id="user-1",
        )
        registry.register_task(
            task_id="task-u2-1",
            status=TaskStatus.RUNNING.value,
            goal="用户2的任务",
            user_id="user-2",
        )

        # 查询用户1的任务
        results = registry.query_tasks(user_id="user-1")
        assert len(results) == 2
        assert all(r["user_id"] == "user-1" for r in results)

    def test_query_tasks_by_status(self, registry):
        """测试按状态查询任务。

        验证可以正确过滤特定状态的任务。
        """
        registry.register_task(task_id="task-r1", status=TaskStatus.RUNNING.value, goal="运行中")
        registry.register_task(task_id="task-r2", status=TaskStatus.RUNNING.value, goal="也是运行中")
        registry.register_task(task_id="task-p1", status=TaskStatus.PAUSED.value, goal="暂停中")

        results = registry.query_tasks(status=TaskStatus.RUNNING.value)
        assert len(results) == 2
        assert all(r["status"] == TaskStatus.RUNNING.value for r in results)

    def test_query_tasks_with_limit(self, registry):
        """测试查询任务数量限制。

        验证 limit 参数正确限制返回数量。
        """
        # 创建 10 个任务
        for i in range(10):
            registry.register_task(
                task_id=f"task-limit-{i}",
                status=TaskStatus.RUNNING.value,
                goal=f"任务 {i}",
            )

        results = registry.query_tasks(limit=5)
        assert len(results) == 5

    def test_query_tasks_order_by_updated_at(self, registry):
        """测试任务按更新时间排序。

        验证最新更新的任务排在前面。
        """
        registry.register_task(
            task_id="task-old",
            status=TaskStatus.RUNNING.value,
            goal="旧任务",
        )
        time.sleep(0.01)  # 确保时间戳不同
        registry.register_task(
            task_id="task-new",
            status=TaskStatus.RUNNING.value,
            goal="新任务",
        )

        results = registry.query_tasks()
        assert results[0]["task_id"] == "task-new"
        assert results[1]["task_id"] == "task-old"


class TestTaskRemoval:
    """测试任务删除功能。"""

    def test_remove_existing_task(self, registry):
        """测试删除存在的任务。

        验证任务可以被正确删除。
        """
        task_id = "task-to-delete"
        registry.register_task(
            task_id=task_id,
            status=TaskStatus.RUNNING.value,
            goal="要删除的任务",
        )

        registry.remove_task(task_id)
        result = registry.lookup_task(task_id)
        assert result is None

    def test_remove_nonexistent_task_no_error(self, registry):
        """测试删除不存在的任务不报错。

        验证删除不存在的任务不会抛出异常。
        """
        # 不应该抛出异常
        registry.remove_task("nonexistent-task")
        assert True


class TestConcurrentTaskOperations:
    """测试并发任务操作。"""

    def test_concurrent_task_registration(self, registry):
        """测试并发创建任务。

        验证多线程同时创建任务不会出错。
        """
        errors = []

        def create_task(task_id):
            try:
                registry.register_task(
                    task_id=task_id,
                    status=TaskStatus.RUNNING.value,
                    goal=f"并发任务 {task_id}",
                )
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=create_task, args=(f"concurrent-task-{i}",))
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        results = registry.query_tasks(limit=100)
        assert len(results) == 20

    def test_concurrent_status_updates(self, registry):
        """测试并发状态更新。

        验证多线程同时更新同一任务状态不会出错。
        """
        task_id = "concurrent-status-task"
        registry.register_task(
            task_id=task_id,
            status=TaskStatus.RUNNING.value,
            goal="并发更新状态",
        )

        update_results = []

        def update_status(status):
            ok = registry.update_task_status(task_id, status)
            update_results.append((status, ok))

        threads = [
            threading.Thread(target=update_status, args=(TaskStatus.PAUSED.value,)),
            threading.Thread(target=update_status, args=(TaskStatus.RUNNING.value,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 至少有一个更新成功
        assert any(ok for _, ok in update_results)


class TestTaskStatusTransitionsValidation:
    """测试状态转换验证。"""

    def test_update_nonexistent_task_returns_false(self, registry):
        """测试更新不存在的任务返回 False。

        验证对不存在的任务更新状态会返回 False 而不是崩溃。
        """
        ok = registry.update_task_status("nonexistent-task", TaskStatus.COMPLETED.value)
        assert ok is False

    def test_task_timestamps_updated(self, registry):
        """测试任务时间戳正确更新。

        验证 updated_at 在每次更新时都会变化。
        """
        task_id = "timestamp-task"
        registry.register_task(
            task_id=task_id,
            status=TaskStatus.PLANNING.value,
            goal="时间戳测试",
        )

        first_update = registry.lookup_task(task_id)["updated_at"]
        time.sleep(0.01)
        registry.update_task_status(task_id, TaskStatus.RUNNING.value)
        second_update = registry.lookup_task(task_id)["updated_at"]

        assert second_update > first_update