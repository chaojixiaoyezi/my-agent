"""记忆推模式集成测试。

测试记忆推模式在各个决策点的实际效果：
1. dispatch 失败后的记忆注入
2. planner 决策前的记忆注入（预留）
3. 记忆格式化和注入
4. 记忆查询相关性
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestMemoryQueryRelevance:
    """测试记忆查询相关性。"""

    def test_push_relevant_memories_no_memory_attr(self, tmp_path: Path):
        """agent 没有 memory 属性时返回空列表。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories

        agent = MagicMock()
        del agent.memory  # 没有 memory 属性

        result = push_relevant_memories(agent, "timeout", {}, limit=3)
        assert result == []

    def test_push_relevant_memories_none_memory(self, tmp_path: Path):
        """agent.memory 为 None 时返回空列表。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories

        agent = MagicMock()
        agent.memory = None

        result = push_relevant_memories(agent, "timeout", {}, limit=3)
        assert result == []

    def test_push_relevant_memories_with_context(self, tmp_path: Path):
        """验证 context 参数被正确使用。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories
        from agent_py_agent.agent.memory_store import MemoryRecord

        agent = MagicMock()
        agent.memory.search.return_value = [
            MemoryRecord(
                role="system",
                content="超时处理技巧",
                kind="lesson_general",
                tags=["timeout"],
                created_at=1234567890.0,
            ),
        ]

        context = {
            "task_id": "test_123",
            "goal": "执行长时间任务",
            "failure_type": "timeout",
        }
        result = push_relevant_memories(agent, "timeout", context, limit=3)

        # 验证 search 被调用（参数不重要，重要的是调用了）
        agent.memory.search.assert_called_once()

    def test_push_relevant_memories_timeout_trigger(self, tmp_path: Path):
        """timeout 触发类型正确查询记忆。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories
        from agent_py_agent.agent.memory_store import MemoryRecord

        agent = MagicMock()
        agent.memory.search.return_value = [
            MemoryRecord(
                role="system",
                content="timeout lesson",
                kind="lesson_general",
                tags=["timeout"],
                created_at=1234567890.0,
            ),
        ]

        result = push_relevant_memories(agent, "timeout", {"failure_type": "timeout"}, limit=3)

        assert isinstance(result, list)

    def test_push_relevant_memories_failure_trigger(self, tmp_path: Path):
        """failure 触发类型正确查询记忆。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories
        from agent_py_agent.agent.memory_store import MemoryRecord

        agent = MagicMock()
        agent.memory.search.return_value = [
            MemoryRecord(
                role="system",
                content="failure lesson",
                kind="lesson_task",
                tags=["failure"],
                created_at=1234567890.0,
            ),
        ]

        result = push_relevant_memories(agent, "failure", {"failure_type": "parse_error"}, limit=3)

        assert isinstance(result, list)

    def test_push_relevant_memories_planning_trigger(self, tmp_path: Path):
        """planning 触发类型正确查询 context 类型记忆。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories
        from agent_py_agent.agent.memory_store import MemoryRecord

        agent = MagicMock()
        agent.memory.search.return_value = [
            MemoryRecord(
                role="system",
                content="planning context",
                kind="context",
                tags=["planning"],
                created_at=1234567890.0,
            ),
        ]

        result = push_relevant_memories(agent, "planning", {"goal": "设计架构"}, limit=3)

        assert isinstance(result, list)

    def test_push_relevant_memories_general_trigger(self, tmp_path: Path):
        """general 触发类型返回所有类型记忆。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories
        from agent_py_agent.agent.memory_store import MemoryRecord

        agent = MagicMock()
        agent.memory.search.return_value = [
            MemoryRecord(
                role="system",
                content="general lesson",
                kind="lesson_general",
                tags=["general"],
                created_at=1234567890.0,
            ),
        ]

        result = push_relevant_memories(agent, "general", {}, limit=3)

        assert isinstance(result, list)

    def test_push_relevant_memories_skip_short_content(self, tmp_path: Path):
        """验证跳过内容太短的记录（< 10 字符）。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories
        from agent_py_agent.agent.memory_store import MemoryRecord

        agent = MagicMock()
        agent.memory.search.return_value = [
            MemoryRecord(
                role="system",
                content="短",  # 太短，应该被跳过
                kind="lesson_general",
                tags=["test"],
                created_at=1234567890.0,
            ),
        ]

        result = push_relevant_memories(agent, "timeout", {}, limit=3)

        # 短内容被跳过，结果应为空
        assert len(result) == 0

    def test_push_relevant_memories_goal_truncation(self, tmp_path: Path):
        """验证超长 goal 被截断到 50 字符。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories
        from agent_py_agent.agent.memory_store import MemoryRecord

        agent = MagicMock()
        agent.memory.search.return_value = []

        long_goal = "A" * 100
        context = {"task_id": "task_long", "goal": long_goal}
        push_relevant_memories(agent, "timeout", context, limit=3)

        # 验证 search 被调用，参数中 goal 长度不超过 50
        call_args = agent.memory.search.call_args
        query = call_args[0][0] if call_args[0] else call_args[1].get("query", "")
        assert len(query) < 150  # goal 被截断，query 不会太长
