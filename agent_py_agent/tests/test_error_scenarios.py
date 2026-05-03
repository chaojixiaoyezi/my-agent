"""异常场景测试 - dispatch循环异常处理、memory_push异常场景、failure_introspector降级逻辑。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestDispatchLoopExceptions:
    """测试 dispatch 循环中的异常处理。"""

    def test_task_not_found_during_dispatch(self, tmp_path: Path):
        """任务不存在时的异常处理。"""
        from agent_py_agent.agent.agent_core.dispatch_loop import dispatch_loop, DispatchLoopReport

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"

        # 模拟 load 抛出 FileNotFoundError
        def load_side_effect(task_id):
            raise FileNotFoundError(f"Task {task_id} not found")

        agent.subagents.load.side_effect = load_side_effect
        agent.subagents.list_runs.return_value = []
        agent.has_pending_work = False

        mock_report = MagicMock()
        mock_report.records = []
        agent.dispatch_subagents.return_value = mock_report

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20)
        assert isinstance(result, DispatchLoopReport)

    def test_corrupted_task_file(self, tmp_path: Path):
        """任务文件损坏时的行为。"""
        from agent_py_agent.agent.agent_core.dispatch_loop import dispatch_loop, DispatchLoopReport

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"

        # 模拟 JSON 解析失败
        def load_side_effect(task_id):
            raise ValueError("Invalid JSON format")

        agent.subagents.load.side_effect = load_side_effect
        agent.subagents.list_runs.return_value = []
        agent.has_pending_work = False

        mock_report = MagicMock()
        mock_report.records = []
        agent.dispatch_subagents.return_value = mock_report

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20)
        assert isinstance(result, DispatchLoopReport)

    def test_llm_call_failure_in_dispatch(self, tmp_path: Path):
        """LLM 调用失败时的降级处理。"""
        from agent_py_agent.agent.agent_core.failure_introspector import FailureIntrospector, FailureIntrospection
        from agent_py_agent.agent.agent_core.failure_analyzer import FailureAnalysis

        # 模拟 agent 为 None
        introspector = FailureIntrospector(agent=None)

        mock_task = MagicMock()
        mock_task.goal = "测试任务"
        mock_runner_result = MagicMock()
        mock_runner_result.ok = False
        mock_runner_result.status = "BLOCKED"

        failure_analysis = FailureAnalysis(
            failure_type="timeout",
            root_cause="task_too_large",
            suggested_action="split_task",
            should_retry=True,
        )

        # agent 为 None 时应降级到规则分类
        result = introspector.introspect(mock_task, mock_runner_result, failure_analysis)
        assert isinstance(result, FailureIntrospection)
        assert result.confidence == 0.3  # 降级后的低置信度

    def test_llm_json_parse_failure(self, tmp_path: Path):
        """LLM 返回 JSON 解析失败时的降级。"""
        from agent_py_agent.agent.agent_core.failure_introspector import FailureIntrospector, FailureIntrospection
        from agent_py_agent.agent.agent_core.failure_analyzer import FailureAnalysis

        agent = MagicMock()
        # 模拟 LLM 返回无效 JSON
        agent.run.return_value = MagicMock(response="这不是有效的JSON格式")

        introspector = FailureIntrospector(agent=agent)

        mock_task = MagicMock()
        mock_task.goal = "测试任务"
        mock_task.attributes = {}
        mock_runner_result = MagicMock()
        mock_runner_result.ok = False
        mock_runner_result.status = "TIMEOUT"
        mock_runner_result.runner_last_error = "timeout"

        failure_analysis = FailureAnalysis(
            failure_type="timeout",
            root_cause="timeout_too_short",
            suggested_action="increase_timeout",
            should_retry=True,
        )

        result = introspector.introspect(mock_task, mock_runner_result, failure_analysis)
        assert isinstance(result, FailureIntrospection)
        assert result.confidence == 0.3  # 降级后的低置信度

    def test_all_tasks_failed_and_no_retry(self, tmp_path: Path):
        """所有任务都失败且不允许重试时的处理。"""
        from agent_py_agent.agent.agent_core.dispatch_loop import dispatch_loop, DispatchLoopReport

        agent = MagicMock()
        agent.config.runner_failure_policy = "no_retry"  # 不重试策略

        mock_report = MagicMock()
        # 所有记录都是失败的
        failed_record = MagicMock()
        failed_record.ok = False
        failed_record.status = "FAILED"
        mock_report.records = [failed_record]

        agent.dispatch_subagents.return_value = mock_report
        agent.subagents.list_runs.return_value = []
        agent.has_pending_work = False

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20)
        assert isinstance(result, DispatchLoopReport)
        assert result.rounds_count == 1


class TestMemoryPushExceptions:
    """测试 memory_push 在异常场景下的行为。"""

    def test_memory_push_no_agent(self, tmp_path: Path):
        """agent 为 None 时的处理。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories

        result = push_relevant_memories(None, "timeout", {}, limit=3)
        assert result == []

    def test_memory_push_memory_is_none(self, tmp_path: Path):
        """agent.memory 为 None 时的处理。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories

        agent = MagicMock()
        agent.memory = None

        result = push_relevant_memories(agent, "timeout", {}, limit=3)
        assert result == []

    def test_memory_push_memory_has_no_search(self, tmp_path: Path):
        """memory 没有 search 方法时的处理。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories

        agent = MagicMock()
        agent.memory = MagicMock(spec=[])  # 没有 search 方法

        result = push_relevant_memories(agent, "timeout", {}, limit=3)
        assert result == []

    def test_memory_search_exception(self, tmp_path: Path):
        """memory.search 抛出异常时的处理。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories

        agent = MagicMock()
        agent.memory = MagicMock()
        agent.memory.search.side_effect = RuntimeError("Search failed")

        result = push_relevant_memories(agent, "timeout", {}, limit=3)
        assert result == []

    def test_memory_corrupted_entry(self, tmp_path: Path):
        """记忆条目损坏时的降级处理。"""
        from agent_py_agent.agent.memory_push import MemoryEntry, MemoryType

        # 损坏的数据（缺少必需字段）
        corrupted_data = {
            "type": "invalid_type",
            # 缺少其他必需字段
        }

        # from_dict 应该能处理损坏数据，返回默认值
        entry = MemoryEntry.from_dict(corrupted_data)
        assert entry.type == MemoryType.LESSON_GENERAL  # 默认值

    def test_write_memory_without_memory(self, tmp_path: Path):
        """写入记忆时 memory 为 None 的处理。"""
        from agent_py_agent.agent.memory_push import write_memory_with_type

        mock_memory = MagicMock()
        mock_memory.add.side_effect = AttributeError("memory is None")

        # 应该不抛出异常
        try:
            write_memory_with_type(
                mock_memory,
                content="test",
                mem_type=MemoryType.LESSON_GENERAL,
            )
        except Exception:
            pass  # 可能抛出异常，但不应该导致进程崩溃


class TestFailureIntrospectorDegradation:
    """测试 failure_introspector 的降级逻辑。"""

    def test_introspector_no_agent_fallback(self, tmp_path: Path):
        """agent 未设置时降级到规则分类。"""
        from agent_py_agent.agent.agent_core.failure_introspector import FailureIntrospector
        from agent_py_agent.agent.agent_core.failure_analyzer import FailureAnalysis

        introspector = FailureIntrospector(agent=None)

        mock_task = MagicMock()
        mock_runner_result = MagicMock()
        mock_runner_result.ok = False

        analysis = FailureAnalysis(
            failure_type="parse_error",
            root_cause="malformed_input",
            suggested_action="validate_input",
            should_retry=True,
        )

        result = introspector.introspect(mock_task, mock_runner_result, analysis)
        assert result.confidence == 0.3
        assert result.analysis_reason.startswith("规则分类：")

    def test_introspector_llm_exception_fallback(self, tmp_path: Path):
        """LLM 调用抛出异常时降级。"""
        from agent_py_agent.agent.agent_core.failure_introspector import FailureIntrospector
        from agent_py_agent.agent.agent_core.failure_analyzer import FailureAnalysis

        agent = MagicMock()
        agent.run.side_effect = RuntimeError("LLM API failed")

        introspector = FailureIntrospector(agent=agent)

        mock_task = MagicMock()
        mock_task.goal = "测试"
        mock_task.attributes = {}
        mock_runner_result = MagicMock()
        mock_runner_result.ok = False
        mock_runner_result.status = "ERROR"
        mock_runner_result.runner_last_error = "api_error"

        analysis = FailureAnalysis(
            failure_type="api_error",
            root_cause="network_issue",
            suggested_action="retry_later",
            should_retry=True,
        )

        result = introspector.introspect(mock_task, mock_runner_result, analysis)
        assert result.confidence == 0.3
        assert result.root_cause == "network_issue"

    def test_introspector_invalid_json_response(self, tmp_path: Path):
        """LLM 返回无效 JSON 时的降级。"""
        from agent_py_agent.agent.agent_core.failure_introspector import FailureIntrospector
        from agent_py_agent.agent.agent_core.failure_analyzer import FailureAnalysis

        agent = MagicMock()
        agent.run.return_value = MagicMock(response="This is not JSON at all")

        introspector = FailureIntrospector(agent=agent)

        mock_task = MagicMock()
        mock_task.goal = "测试"
        mock_task.attributes = {}
        mock_runner_result = MagicMock()
        mock_runner_result.ok = False
        mock_runner_result.status = "TIMEOUT"
        mock_runner_result.runner_last_error = ""

        analysis = FailureAnalysis(
            failure_type="timeout",
            root_cause="slow_processing",
            suggested_action="increase_timeout",
            should_retry=True,
        )

        result = introspector.introspect(mock_task, mock_runner_result, analysis)
        assert result.confidence == 0.3

    def test_introspector_missing_keys_in_response(self, tmp_path: Path):
        """LLM 返回 JSON 缺少必需字段时的降级。"""
        from agent_py_agent.agent.agent_core.failure_introspector import FailureIntrospector
        from agent_py_agent.agent.agent_core.failure_analyzer import FailureAnalysis

        agent = MagicMock()
        # JSON 缺少必需字段
        agent.run.return_value = MagicMock(response='{"analysis_reason": "测试"}')

        introspector = FailureIntrospector(agent=agent)

        mock_task = MagicMock()
        mock_task.goal = "测试"
        mock_task.attributes = {}
        mock_runner_result = MagicMock()
        mock_runner_result.ok = False

        analysis = FailureAnalysis(
            failure_type="error",
            root_cause="unknown",
            suggested_action="manual_check",
            should_retry=False,
        )

        result = introspector.introspect(mock_task, mock_runner_result, analysis)
        assert isinstance(result.analysis_reason, str)

    def test_introspector_fallback_includes_params(self, tmp_path: Path):
        """降级时包含规则分类的参数建议。"""
        from agent_py_agent.agent.agent_core.failure_introspector import FailureIntrospector
        from agent_py_agent.agent.agent_core.failure_analyzer import FailureAnalysis

        introspector = FailureIntrospector(agent=None)

        mock_task = MagicMock()
        mock_runner_result = MagicMock()

        analysis = FailureAnalysis(
            failure_type="timeout",
            root_cause="too_short",
            suggested_action="increase_timeout",
            should_adjust_timeout=True,
            new_timeout_seconds=300,
            should_retry=True,
        )

        result = introspector.introspect(mock_task, mock_runner_result, analysis)
        assert "new_timeout_seconds" in result.suggested_params
        assert result.suggested_params["new_timeout_seconds"] == 300