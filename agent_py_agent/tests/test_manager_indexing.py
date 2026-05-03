"""Tests for SubAgentIndexingMixin: _select_runs(), _index_task(), and DISPATCH_INELIGIBLE_STATUSES filtering.

给人看的解释：
测试子代理索引模块：_select_runs() 状态过滤、_index_task() 索引写入、
DISPATCH_INELIGIBLE_STATUSES 过滤。
"""
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagents.models import DISPATCH_INELIGIBLE_STATUSES


class TestDispatchIneligibleStatuses:
    """测试 DISPATCH_INELIGIBLE_STATUSES 常量。"""

    def test_dispatch_ineligible_contains_paused(self):
        """验证 PAUSED 状态不可调度。"""
        assert "PAUSED" in DISPATCH_INELIGIBLE_STATUSES

    def test_dispatch_ineligible_contains_abandoned(self):
        """验证 ABANDONED 状态不可调度。"""
        assert "ABANDONED" in DISPATCH_INELIGIBLE_STATUSES

    def test_dispatch_ineligible_contains_completed(self):
        """验证 COMPLETED 状态不可调度。"""
        assert "COMPLETED" in DISPATCH_INELIGIBLE_STATUSES

    def test_dispatch_ineligible_contains_failed(self):
        """验证 FAILED 状态不可调度。"""
        assert "FAILED" in DISPATCH_INELIGIBLE_STATUSES

    def test_dispatch_eligible_contains_planning(self):
        """验证 PLANNING 状态可调度。"""
        assert "PLANNING" not in DISPATCH_INELIGIBLE_STATUSES

    def test_dispatch_eligible_contains_running(self):
        """验证 RUNNING 状态可调度。"""
        assert "RUNNING" not in DISPATCH_INELIGIBLE_STATUSES

    def test_dispatch_eligible_contains_blocked(self):
        """验证 BLOCKED 状态可调度（blocked 只是暂时受阻，不是终态）。"""
        assert "BLOCKED" not in DISPATCH_INELIGIBLE_STATUSES


class TestSelectRuns:
    """测试 _select_runs 方法。"""

    def test_select_runs_none_returns_all_eligible(self):
        """验证 run_ids=None 时返回所有非 ineligible 状态的任务。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            # 创建多个任务（默认状态是 PLANNING）
            task1 = agent.subagents.create_run(
                goal="任务1",
                thought="思考任务1",
                plan=["step1"],
            )
            task2 = agent.subagents.create_run(
                goal="任务2",
                thought="思考任务2",
                plan=["step1"],
            )

            selected = agent.subagents.select_runs(None)

            selected_ids = [t.id for t in selected]
            assert task1.id in selected_ids
            assert task2.id in selected_ids

    def test_select_runs_empty_list(self):
        """验证 run_ids=[] 时返回空列表。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            selected = agent.subagents.select_runs([])

            assert selected == []

    def test_select_runs_filters_specific_ids(self):
        """验证指定 run_ids 时只返回存在的任务。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            task1 = agent.subagents.create_run(
                goal="任务1",
                thought="思考任务1",
                plan=["step1"],
            )
            task2 = agent.subagents.create_run(
                goal="任务2",
                thought="思考任务2",
                plan=["step1"],
            )
            task3 = agent.subagents.create_run(
                goal="任务3",
                thought="思考任务3",
                plan=["step1"],
            )

            selected = agent.subagents.select_runs([task1.id, task2.id, task3.id])

            assert len(selected) == 3
            selected_ids = [t.id for t in selected]
            assert task1.id in selected_ids
            assert task2.id in selected_ids
            assert task3.id in selected_ids

    def test_select_runs_skips_missing_tasks(self):
        """验证跳过不存在的任务 ID。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            task1 = agent.subagents.create_run(
                goal="任务1",
                thought="思考任务1",
                plan=["step1"],
            )

            # 使用一个不存在的 ID
            selected = agent.subagents.select_runs([task1.id, "non-existent-id"])

            assert len(selected) == 1
            assert selected[0].id == task1.id


class TestIndexTask:
    """测试 _index_task 方法。"""

    def test_index_task_basic(self):
        """验证基本任务索引写入。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            task = agent.subagents.create_run(
                goal="测试索引任务",
                thought="这是测试任务",
                plan=["步骤1", "步骤2"],
                acceptance_checks=["检查项1", "检查项2"],
            )

            # 不应该抛出异常
            agent.subagents.index_task(task)

    def test_index_task_without_local_store(self):
        """验证没有 LocalStore 时不崩溃。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)
            agent.subagents.local_store = None

            task = agent.subagents.create_run(
                goal="无 LocalStore 测试",
                thought="测试",
                plan=["step1"],
            )

            # 不应该抛出异常
            agent.subagents.index_task(task)


class TestIndexDispatchRecord:
    """测试 _index_dispatch_record 方法。"""

    def test_index_dispatch_record_basic(self):
        """验证基本 dispatch 索引记录。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            record = agent.subagents.make_dispatch_record(
                step="test",
                action="test",
                run_id="test-run",
                ok=True,
            )

            # 不应该抛出异常
            agent.subagents.index_dispatch_record(record)


class TestIndexDispatchWatchRecord:
    """测试 _index_dispatch_watch_record 方法。"""

    def test_index_dispatch_watch_record_basic(self):
        """验证基本 dispatch watch 索引记录。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            record = agent.subagents.make_dispatch_watch_record(
                cycle=1,
                dry_run=True,
                ok=True,
                message="test",
                dispatch_record_count=0,
            )

            # 不应该抛出异常
            agent.subagents.index_dispatch_watch_record(record)


class TestIndexParentPlannerRecord:
    """测试 _index_parent_planner_record 方法。"""

    def test_index_parent_planner_record_basic(self):
        """验证基本 parent planner 索引记录。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            record = agent.subagents.make_parent_planner_record(
                dry_run=True,
                triggered=True,
                ok=True,
                decision="PLAN",
                message="测试",
            )

            # 不应该抛出异常
            agent.subagents.index_parent_planner_record(record)


class TestIndexExecutionContext:
    """测试 _index_execution_context 方法。"""

    def test_index_execution_context_basic(self):
        """验证基本 execution context 索引记录。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            task = agent.subagents.create_run(
                goal="测试执行上下文索引",
                thought="思考",
                plan=["step1"],
            )

            context = agent.subagents.write_execution_context(task.id)

            # 不应该抛出异常
            agent.subagents.index_execution_context(context)


class TestLogLocalRecord:
    """测试 _log_local_record 方法的异常处理。"""

    def test_log_local_record_without_local_store(self):
        """验证没有 LocalStore 时 _log_local_record 正常返回。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)
            agent.subagents.local_store = None

            # 不应该抛出异常
            agent.subagents.log_local_record(
                source_type="test",
                source_id="test-id",
                title="Test Title",
                content="Test Content",
                event_type="test_event",
            )

    def test_log_local_record_with_local_store_failure(self):
        """验证 LocalStore.log_record 失败时不影响主流程。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            if agent.subagents.local_store:
                # 模拟 log_record 失败
                original = agent.subagents.local_store.log_record

                def failing_log_record(**kwargs):
                    raise RuntimeError("LocalStore unavailable")

                agent.subagents.local_store.log_record = failing_log_record

                # 不应该抛出异常
                agent.subagents.log_local_record(
                    source_type="test",
                    source_id="test-id",
                    title="Test Title",
                    content="Test Content",
                    event_type="test_event",
                )


class TestIndexReport:
    """测试 _index_report 方法。"""

    def test_index_report_without_local_store(self):
        """验证没有 LocalStore 时 _index_report 正常返回。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)
            agent.subagents.local_store = None

            report = agent.subagents.build_dispatch_report([], dry_run=True)

            # 不应该抛出异常
            agent.subagents._index_report(
                source_type="test_report",
                source_id="test-id",
                title="Test Report",
                report=report,
                event_type="test_event",
            )
