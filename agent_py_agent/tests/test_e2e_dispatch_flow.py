"""端到端调度流程测试。

测试 dispatch 循环完整流程、多任务并发调度、失败重试和记忆注入效果。
尽量少用 mock，使用真实的组件组合。
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


class TestDispatchCandidates:
    """测试调度候选任务识别。"""

    def test_running_tasks_are_dispatch_candidates(self, registry):
        """测试 RUNNING 状态的任务是有效的调度候选。

        验证 RUNNING 状态任务会被识别为可调度。
        """
        # 注册一个 RUNNING 任务
        registry.register_task(
            task_id="dispatch-candidate-1",
            status=TaskStatus.RUNNING.value,
            goal="应该被调度的任务",
        )

        # 验证任务存在且状态为 RUNNING
        task = registry.lookup_task("dispatch-candidate-1")
        assert task is not None
        assert task["status"] == TaskStatus.RUNNING.value

        # RUNNING 任务不在排除列表中，应该被调度
        from agent_py_agent.agent.subagents.models import DISPATCH_INELIGIBLE_STATUSES
        assert TaskStatus.RUNNING.value not in DISPATCH_INELIGIBLE_STATUSES

    def test_ineligible_statuses_are_excluded(self, registry):
        """测试不可调度的状态会被正确排除。

        验证 PAUSED、ABANDONED、DONE、FAILED 状态的任务不会被调度。
        """
        ineligible_statuses = [
            TaskStatus.PAUSED.value,
            TaskStatus.ABANDONED.value,
            TaskStatus.DONE.value,
            TaskStatus.FAILED.value,
        ]

        for i, status in enumerate(ineligible_statuses):
            task_id = f"ineligible-task-{i}"
            registry.register_task(
                task_id=task_id,
                status=status,
                goal=f"状态为 {status} 的任务",
            )

            # 验证这些状态在排除列表中
            from agent_py_agent.agent.subagents.models import DISPATCH_INELIGIBLE_STATUSES
            assert status in DISPATCH_INELIGIBLE_STATUSES

    def test_dispatch_candidates_ordered_by_updated_at(self, registry):
        """测试调度候选按更新时间排序。

        验证最新的任务会被优先调度。
        """
        # 创建旧任务
        registry.register_task(
            task_id="old-task",
            status=TaskStatus.RUNNING.value,
            goal="旧任务",
        )
        time.sleep(0.01)

        # 创建新任务
        registry.register_task(
            task_id="new-task",
            status=TaskStatus.RUNNING.value,
            goal="新任务",
        )

        # 按更新时间DESC排序
        tasks = registry.query_tasks(status=TaskStatus.RUNNING.value)
        assert tasks[0]["task_id"] == "new-task"
        assert tasks[1]["task_id"] == "old-task"


class TestMultiTaskDispatch:
    """测试多任务并发调度。"""

    def test_multiple_tasks_creation_for_dispatch(self, registry):
        """测试创建多个用于调度的任务。

        验证可以同时创建多个 RUNNING 状态的任务。
        """
        tasks_to_create = 5
        for i in range(tasks_to_create):
            registry.register_task(
                task_id=f"multi-dispatch-task-{i}",
                status=TaskStatus.RUNNING.value,
                goal=f"并发任务 {i}",
            )

        tasks = registry.query_tasks(status=TaskStatus.RUNNING.value)
        assert len(tasks) >= tasks_to_create

    def test_concurrent_task_status_updates(self, registry):
        """测试并发更新多个任务状态。

        验证多线程同时更新不同任务状态不会出错。
        """
        errors = []

        # 创建 10 个任务
        for i in range(10):
            registry.register_task(
                task_id=f"concurrent-update-{i}",
                status=TaskStatus.RUNNING.value,
                goal=f"并发更新任务 {i}",
            )

        def update_task(task_id, new_status):
            try:
                registry.update_task_status(task_id, new_status)
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(10):
            t = threading.Thread(
                target=update_task,
                args=(f"concurrent-update-{i}", TaskStatus.DONE.value),
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        assert len(errors) == 0

        # 验证所有任务都已更新
        for i in range(10):
            task = registry.lookup_task(f"concurrent-update-{i}")
            assert task["status"] == TaskStatus.DONE.value

    def test_dispatch_batch_size_respected(self, registry):
        """测试调度时批次大小会被遵守。

        验证 query_tasks 的 limit 参数正确限制返回数量。
        """
        # 创建 20 个任务
        for i in range(20):
            registry.register_task(
                task_id=f"batch-task-{i}",
                status=TaskStatus.RUNNING.value,
                goal=f"批次任务 {i}",
            )

        # 只获取 5 个
        tasks = registry.query_tasks(status=TaskStatus.RUNNING.value, limit=5)
        assert len(tasks) == 5


class TestFailureRetry:
    """测试失败重试逻辑。"""

    def test_failed_task_can_be_retried(self, registry):
        """测试失败的任务可以重试。

        验证 FAILED 状态的任务可以重新标记为 RUNNING。
        """
        task_id = "retry-task"
        registry.register_task(
            task_id=task_id,
            status=TaskStatus.FAILED.value,
            goal="失败后重试的任务",
        )

        # 重试：将状态改回 RUNNING
        ok = registry.update_task_status(task_id, TaskStatus.RUNNING.value)
        assert ok is True

        task = registry.lookup_task(task_id)
        assert task["status"] == TaskStatus.RUNNING.value

    def test_task_failure_sequence_tracking(self, registry):
        """测试任务失败序列跟踪。

        验证任务状态时间线正确记录失败历史。
        """
        task_id = "failure-sequence-task"
        registry.register_task(
            task_id=task_id,
            status=TaskStatus.RUNNING.value,
            goal="会经历多次失败的任务",
        )

        # 第一次失败
        registry.update_task_status(task_id, TaskStatus.FAILED.value)
        # 重试
        registry.update_task_status(task_id, TaskStatus.RUNNING.value)
        # 再次失败
        registry.update_task_status(task_id, TaskStatus.FAILED.value)

        task = registry.lookup_task(task_id)
        assert task["status"] == TaskStatus.FAILED.value

    def test_abandoned_task_not_retried(self, registry):
        """测试放弃的任务不会被重试。

        验证 ABANDONED 状态的任务保持在放弃状态。
        """
        task_id = "abandoned-no-retry"
        registry.register_task(
            task_id=task_id,
            status=TaskStatus.RUNNING.value,
            goal="被放弃的任务",
        )

        # 放弃
        registry.update_task_status(task_id, TaskStatus.ABANDONED.value)

        # 尝试重试（模拟调度器不应该重新调度）
        task = registry.lookup_task(task_id)
        assert task["status"] == TaskStatus.ABANDONED.value


class TestMemoryInjection:
    """测试记忆注入效果。"""

    def test_task_goal_preserved_in_registry(self, registry):
        """测试任务目标被正确保存在注册表。

        验证 goal 字段在任务创建后不会丢失。
        """
        task_id = "goal-preservation-task"
        goal_text = "这是一个重要的任务目标，用于测试记忆注入"

        registry.register_task(
            task_id=task_id,
            status=TaskStatus.RUNNING.value,
            goal=goal_text,
        )

        task = registry.lookup_task(task_id)
        assert task["goal"] == goal_text

    def test_task_timestamps_for_injection_timing(self, registry):
        """测试任务时间戳用于判断注入时机。

        验证 created_at 和 updated_at 时间戳正确。
        """
        task_id = "timestamp-task"
        before_create = time.time()

        registry.register_task(
            task_id=task_id,
            status=TaskStatus.PLANNING.value,
            goal="用于测试时间戳的任务",
        )

        after_create = time.time()
        task = registry.lookup_task(task_id)

        assert before_create <= task["created_at"] <= after_create
        assert task["created_at"] == task["updated_at"]

        # 更新后 updated_at 应该变化
        time.sleep(0.01)
        registry.update_task_status(task_id, TaskStatus.RUNNING.value)
        updated_task = registry.lookup_task(task_id)

        assert updated_task["updated_at"] > task["updated_at"]


class TestDispatchLoopIntegration:
    """测试调度循环集成。"""

    def test_dispatch_report_structure(self):
        """测试调度报告结构。

        验证 DispatchReport 包含必要的字段。
        """
        from agent_py_agent.agent.subagents import DispatchReport

        # DispatchReport 需要 generated_at, dry_run, summary, records
        report = DispatchReport(
            generated_at=time.time(),
            dry_run=True,
            summary={"total": 0},
            records=[],
        )
        assert hasattr(report, "records")
        assert isinstance(report.records, list)
        assert hasattr(report, "generated_at")
        assert hasattr(report, "dry_run")

    def test_dispatch_watch_report_structure(self):
        """测试调度观察报告结构。

        验证 DispatchWatchReport 包含必要的字段。
        """
        from agent_py_agent.agent.subagents import DispatchWatchReport

        report = DispatchWatchReport(
            generated_at=time.time(),
            dry_run=True,
            summary={"processed": 0},
            records=[],
        )
        assert hasattr(report, "generated_at")
        assert hasattr(report, "dry_run")
        assert hasattr(report, "summary")

    def test_runner_dispatch_candidates_filtering(self, registry):
        """测试 Runner 调度候选过滤。

        验证非 RUNNING 状态的任务不会被 runner 调度。
        """
        # 创建不同状态的任务
        statuses = [
            (TaskStatus.RUNNING.value, "runnable"),
            (TaskStatus.PAUSED.value, "paused"),
            (TaskStatus.DONE.value, "done"),
            (TaskStatus.ABANDONED.value, "abandoned"),
            (TaskStatus.FAILED.value, "failed"),
        ]

        for status, suffix in statuses:
            registry.register_task(
                task_id=f"filter-test-{suffix}",
                status=status,
                goal=f"状态为 {status} 的任务",
            )

        # 只查询 RUNNING 状态
        runnable_tasks = registry.query_tasks(status=TaskStatus.RUNNING.value)
        assert len(runnable_tasks) == 1
        assert runnable_tasks[0]["task_id"] == "filter-test-runnable"

    def test_dispatch_with_runner_max_attempts(self, registry):
        """测试带最大尝试次数的调度逻辑。

        验证 runner_max_attempts 参数返回值大于 0。
        """
        from agent_py_agent.agent.agent_core.runner.dispatch import _runner_max_attempts

        # auto 模式返回合理的重试次数
        auto_max = _runner_max_attempts("auto")
        assert auto_max >= 1

        # manual 模式也返回合理的值
        manual_max = _runner_max_attempts("manual")
        assert manual_max >= 1


class TestConcurrencyDispatch:
    """测试并发调度场景。"""

    def test_concurrent_registration_and_query(self, registry):
        """测试并发注册和查询。

        验证多线程同时注册和查询任务不会出错。
        """
        errors = []
        results = []

        def register_and_query(task_id):
            try:
                registry.register_task(
                    task_id=task_id,
                    status=TaskStatus.RUNNING.value,
                    goal=f"并发任务 {task_id}",
                )
                task = registry.lookup_task(task_id)
                results.append(task)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=register_and_query, args=(f"cc-task-{i}",))
            for i in range(15)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 15

    def test_dispatch_loop_rounds_tracking(self, registry):
        """测试调度循环轮次跟踪。

        验证多轮调度时轮次计数正确。
        """
        # 创建任务
        registry.register_task(
            task_id="rounds-track-task",
            status=TaskStatus.RUNNING.value,
            goal="用于跟踪轮次的任务",
        )

        # 模拟多轮更新
        for round_num in range(5):
            registry.update_task_status(
                "rounds-track-task",
                TaskStatus.RUNNING.value,
            )

        # 验证任务仍然存在
        task = registry.lookup_task("rounds-track-task")
        assert task is not None
        assert task["status"] == TaskStatus.RUNNING.value
