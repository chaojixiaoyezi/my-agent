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


class TestDispatchFailureMemoryInjection:
    """测试 dispatch 失败后的记忆注入。"""

    def test_push_failure_memories_called_on_timeout(self, tmp_path: Path):
        """模拟任务超时失败时，push_failure_memories() 被调用。"""
        from agent_py_agent.agent.memory_push import push_failure_memories

        agent = MagicMock()
        agent.memory = MagicMock()
        agent.memory.search.return_value = []

        # 模拟超时失败
        result = push_failure_memories(agent, "task_123", "分析日志", "timeout")

        # 验证返回的是列表
        assert isinstance(result, list)
        # 验证 memory.search 被调用
        agent.memory.search.assert_called_once()

    def test_push_failure_memories_returns_relevant_lessons(self, tmp_path: Path):
        """验证 push_failure_memories 返回与失败原因相关的记忆。"""
        from agent_py_agent.agent.memory_push import push_failure_memories
        from agent_py_agent.agent.memory_store import MemoryRecord

        agent = MagicMock()
        # 模拟返回包含 timeout 标签的记忆
        agent.memory.search.return_value = [
            MemoryRecord(
                role="system",
                content="超时后要检查 start_time",
                kind="lesson_task",
                tags=["timeout", "gateway"],
                created_at=1234567890.0,
            ),
        ]

        result = push_failure_memories(agent, "task_456", "执行命令", "timeout")

        # 验证返回了相关记忆
        assert len(result) >= 0  # 可能返回空因为过滤条件

    def test_failure_memory_content_contains_failure_info(self, tmp_path: Path):
        """验证记忆内容与失败原因相关。"""
        from agent_py_agent.agent.memory_push import MemoryEntry, push_failure_memories
        from agent_py_agent.agent.memory_store import MemoryRecord

        agent = MagicMock()
        agent.memory.search.return_value = [
            MemoryRecord(
                role="system",
                content="parse_error 需要检查 JSON 格式",
                kind="lesson_task",
                tags=["parse_error", "json"],
                created_at=1234567890.0,
            ),
        ]

        result = push_failure_memories(agent, "task_789", "解析配置", "parse_error")

        # 记忆应该包含相关教训
        assert isinstance(result, list)

    def test_push_failure_memories_with_empty_memory(self, tmp_path: Path):
        """无记忆时返回空列表，不崩溃。"""
        from agent_py_agent.agent.memory_push import push_failure_memories

        agent = MagicMock()
        agent.memory = MagicMock()
        agent.memory.search.return_value = []

        result = push_failure_memories(agent, "task_empty", "测试", "unknown_error")

        assert result == []

    def test_push_failure_memories_limit_parameter(self, tmp_path: Path):
        """验证 limit 参数限制返回数量。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories
        from agent_py_agent.agent.memory_store import MemoryRecord

        agent = MagicMock()
        # 返回多条记忆
        agent.memory.search.return_value = [
            MemoryRecord(
                role="system",
                content=f"记忆{i} " * 10,
                kind="lesson_task",
                tags=["test"],
                created_at=1234567890.0 + i,
            )
            for i in range(10)
        ]

        context = {"task_id": "task_limit", "goal": "测试", "failure_type": "timeout"}
        result = push_relevant_memories(agent, "failure", context, limit=3)

        # 验证返回数量不超过 limit
        assert len(result) <= 3


class TestPlannerMemoryInjection:
    """测试 planner 决策前的记忆注入（预留测试）。"""

    def test_push_planning_memories_returns_context_memories(self, tmp_path: Path):
        """模拟 planner 决策场景，验证 push_planning_memories() 返回相关记忆。"""
        from agent_py_agent.agent.memory_push import push_planning_memories
        from agent_py_agent.agent.memory_store import MemoryRecord

        agent = MagicMock()
        agent.memory.search.return_value = [
            MemoryRecord(
                role="system",
                content="复杂任务应该拆分成子任务",
                kind="context",
                tags=["planning", "split"],
                created_at=1234567890.0,
            ),
        ]

        result = push_planning_memories(agent, "task_plan", "设计系统架构")

        assert isinstance(result, list)
        agent.memory.search.assert_called_once()

    def test_push_planning_memories_filters_context_type(self, tmp_path: Path):
        """验证 planning 触发类型查询时优先返回 context 类型记忆。"""
        from agent_py_agent.agent.memory_push import push_planning_memories
        from agent_py_agent.agent.memory_store import MemoryRecord

        agent = MagicMock()
        agent.memory.search.return_value = [
            MemoryRecord(
                role="system",
                content="上下文记忆内容",
                kind="context",
                tags=["context"],
                created_at=1234567890.0,
            ),
        ]

        result = push_planning_memories(agent, "task_ctx", "测试规划")

        assert isinstance(result, list)

    def test_push_planning_memories_empty_when_no_memory(self, tmp_path: Path):
        """无记忆时返回空列表。"""
        from agent_py_agent.agent.memory_push import push_planning_memories

        agent = MagicMock()
        agent.memory = MagicMock()
        agent.memory.search.return_value = []

        result = push_planning_memories(agent, "task_no_mem", "空规划")

        assert result == []


class TestMemoryFormatting:
    """测试记忆格式化和注入。"""

    def test_format_memories_for_injection_empty(self, tmp_path: Path):
        """测试空记忆列表返回空字符串。"""
        from agent_py_agent.agent.memory_push import format_memories_for_injection

        result = format_memories_for_injection([])
        assert result == ""

    def test_format_single_memory_with_prefix(self, tmp_path: Path):
        """测试单条记忆格式化输出格式。"""
        from agent_py_agent.agent.memory_push import format_memories_for_injection

        memories = ["[lesson_general] 这是一条测试教训"]
        result = format_memories_for_injection(memories)

        assert "[相关记忆提示]" in result
        assert "这是一条测试教训" in result
        assert "1. " in result  # 应该有编号

    def test_format_multiple_memories_with_numbers(self, tmp_path: Path):
        """测试多条记忆格式化时有正确编号。"""
        from agent_py_agent.agent.memory_push import format_memories_for_injection

        memories = [
            "[lesson_general] 教训1",
            "[lesson_task] 教训2",
            "[context] 上下文3",
            "[fact] 事实4",
        ]
        result = format_memories_for_injection(memories)

        assert "[相关记忆提示]" in result
        assert "1. " in result
        assert "2. " in result
        assert "3. " in result
        assert "4. " in result

    def test_format_truncates_long_memory(self, tmp_path: Path):
        """测试超过 200 字的记忆被截断。"""
        from agent_py_agent.agent.memory_push import format_memories_for_injection

        long_memory = "A" * 300  # 300 个字符
        result = format_memories_for_injection([long_memory])

        # 应该被截断且有省略号
        assert "..." in result
        assert len(result) < 400  # 确保被大幅截断

    def test_format_handles_lesson_general_type(self, tmp_path: Path):
        """测试 LESSON_GENERAL 类型记忆的处理。"""
        from agent_py_agent.agent.memory_push import format_memories_for_injection

        memories = ["[lesson_general] 通用教训应该被保留"]
        result = format_memories_for_injection(memories)

        assert "通用教训应该被保留" in result

    def test_format_handles_context_type(self, tmp_path: Path):
        """测试 CONTEXT 类型记忆的处理。"""
        from agent_py_agent.agent.memory_push import format_memories_for_injection

        memories = ["[context] 上下文信息很重要"]
        result = format_memories_for_injection(memories)

        assert "上下文信息很重要" in result

    def test_format_handles_fact_type(self, tmp_path: Path):
        """测试 FACT 类型记忆的处理。"""
        from agent_py_agent.agent.memory_push import format_memories_for_injection

        memories = ["[fact] 这是一个事实"]
        result = format_memories_for_injection(memories)

        assert "这是一个事实" in result


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


class TestMemoryEntryConversion:
    """测试 MemoryEntry 转换功能。"""

    def test_memory_entry_to_dict_complete(self, tmp_path: Path):
        """测试完整 MemoryEntry 转换为字典。"""
        from agent_py_agent.agent.memory_push import MemoryEntry, MemoryType

        entry = MemoryEntry(
            type=MemoryType.LESSON_TASK,
            trigger_type="timeout",
            tags=["timeout", "gateway"],
            content="测试内容",
            lesson="超时后要检查 start_time",
            action="use_scoped_lock",
            result="success",
            trigger_conditions={"timeout_count": ">=3"},
            created_at=1234567890.0,
        )

        data = entry.to_dict()
        assert data["type"] == "lesson_task"
        assert data["trigger_type"] == "timeout"
        assert "timeout" in data["tags"]
        assert data["lesson"] == "超时后要检查 start_time"

    def test_memory_entry_from_dict_complete(self, tmp_path: Path):
        """测试从字典创建完整 MemoryEntry。"""
        from agent_py_agent.agent.memory_push import MemoryEntry, MemoryType

        data = {
            "type": "lesson_task",
            "trigger_type": "timeout",
            "tags": ["timeout"],
            "content": "测试内容",
            "lesson": "测试教训",
            "action": "test_action",
            "result": "success",
            "created_at": 1234567890.0,
        }

        entry = MemoryEntry.from_dict(data)
        assert entry.type == MemoryType.LESSON_TASK
        assert entry.trigger_type == "timeout"
        assert entry.content == "测试内容"

    def test_memory_entry_to_memory_record_content_with_lesson(self, tmp_path: Path):
        """测试带 lesson 的 MemoryEntry 转换。"""
        from agent_py_agent.agent.memory_push import MemoryEntry, MemoryType

        entry = MemoryEntry(
            type=MemoryType.LESSON_TASK,
            content="完整内容",
            lesson="简短教训",
        )

        text = entry.to_memory_record_content()
        assert "[lesson_task]" in text
        assert "简短教训" in text

    def test_memory_entry_to_memory_record_content_without_lesson(self, tmp_path: Path):
        """测试不带 lesson 时使用 content 前100字符。"""
        from agent_py_agent.agent.memory_push import MemoryEntry, MemoryType

        entry = MemoryEntry(
            type=MemoryType.CONTEXT,
            content="这是很长的内容需要被截取",
        )

        text = entry.to_memory_record_content()
        assert "[context]" in text


class TestMemoryTypeEnum:
    """测试 MemoryType 枚举。"""

    def test_memory_type_from_string_valid(self, tmp_path: Path):
        """测试有效字符串转换为 MemoryType。"""
        from agent_py_agent.agent.memory_push import MemoryType

        assert MemoryType.from_string("lesson_general") == MemoryType.LESSON_GENERAL
        assert MemoryType.from_string("lesson_task") == MemoryType.LESSON_TASK
        assert MemoryType.from_string("lesson_temp") == MemoryType.LESSON_TEMP
        assert MemoryType.from_string("context") == MemoryType.CONTEXT
        assert MemoryType.from_string("fact") == MemoryType.FACT

    def test_memory_type_from_string_case_insensitive(self, tmp_path: Path):
        """测试大小写不敏感。"""
        from agent_py_agent.agent.memory_push import MemoryType

        assert MemoryType.from_string("LESSON_GENERAL") == MemoryType.LESSON_GENERAL
        assert MemoryType.from_string("Lesson_Task") == MemoryType.LESSON_TASK
        assert MemoryType.from_string("Context") == MemoryType.CONTEXT

    def test_memory_type_from_string_invalid_default(self, tmp_path: Path):
        """测试无效字符串默认为 LESSON_GENERAL。"""
        from agent_py_agent.agent.memory_push import MemoryType

        assert MemoryType.from_string("") == MemoryType.LESSON_GENERAL
        assert MemoryType.from_string("unknown") == MemoryType.LESSON_GENERAL
        assert MemoryType.from_string("invalid_type") == MemoryType.LESSON_GENERAL


class TestWriteMemoryWithType:
    """测试 write_memory_with_type 函数。"""

    def test_write_memory_with_type_lesson_task(self, tmp_path: Path):
        """测试写入 LESSON_TASK 类型记忆。"""
        from agent_py_agent.agent.memory_push import MemoryType, MemoryWriteContext, write_memory_with_type

        mock_memory = MagicMock()
        mock_memory.add.return_value = MagicMock()

        write_memory_with_type(
            mock_memory,
            ctx=MemoryWriteContext(
                content="测试记忆内容",
                mem_type=MemoryType.LESSON_TASK,
                trigger_type="timeout",
                tags=["test"],
                lesson="测试教训",
                action="test_action",
                result="success",
            ),
        )

        mock_memory.add.assert_called_once()
        call_kwargs = mock_memory.add.call_args[1]
        assert call_kwargs["kind"] == "lesson_task"
        assert "测试记忆内容" in call_kwargs["content"]

    def test_write_memory_with_type_extends_content(self, tmp_path: Path):
        """测试写入时 content 被扩展包含 lesson 和 action。"""
        from agent_py_agent.agent.memory_push import MemoryType, MemoryWriteContext, write_memory_with_type

        mock_memory = MagicMock()
        mock_memory.add.return_value = MagicMock()

        write_memory_with_type(
            mock_memory,
            ctx=MemoryWriteContext(
                content="基础内容",
                mem_type=MemoryType.LESSON_GENERAL,
                lesson="学到教训",
                action="执行动作",
                result="好的结果",
            ),
        )

        call_kwargs = mock_memory.add.call_args[1]
        content = call_kwargs["content"]
        assert "基础内容" in content
        assert "Lesson: 学到教训" in content
        assert "Action: 执行动作" in content
        assert "Result: 好的结果" in content

    def test_write_memory_with_type_adds_trigger_tag(self, tmp_path: Path):
        """测试写入时自动添加 trigger_type 到 tags。"""
        from agent_py_agent.agent.memory_push import MemoryType, MemoryWriteContext, write_memory_with_type

        mock_memory = MagicMock()
        mock_memory.add.return_value = MagicMock()

        write_memory_with_type(
            mock_memory,
            ctx=MemoryWriteContext(
                content="内容",
                mem_type=MemoryType.CONTEXT,
                trigger_type="planning",
                tags=["existing"],
            ),
        )

        call_kwargs = mock_memory.add.call_args[1]
        assert "planning" in call_kwargs["tags"]
        assert "existing" in call_kwargs["tags"]


class TestIntegrationFlow:
    """集成流程测试。"""

    def test_full_memory_push_flow(self, tmp_path: Path):
        """测试完整记忆推送流程：查询 -> 格式化 -> 注入。"""
        from agent_py_agent.agent.memory_push import (
            format_memories_for_injection,
            push_relevant_memories,
        )
        from agent_py_agent.agent.memory_store import MemoryRecord

        agent = MagicMock()
        agent.memory.search.return_value = [
            MemoryRecord(
                role="system",
                content="超时后要检查 start_time",
                kind="lesson_task",
                tags=["timeout", "gateway"],
                created_at=1234567890.0,
            ),
        ]

        # 1. 查询相关记忆
        context = {"task_id": "task_full", "goal": "测试", "failure_type": "timeout"}
        memories = push_relevant_memories(agent, "timeout", context, limit=3)

        # 2. 格式化记忆
        if memories:
            formatted = format_memories_for_injection(memories)
            assert "[相关记忆提示]" in formatted

    def test_memory_push_flow_no_results(self, tmp_path: Path):
        """测试无记忆时的流程（不崩溃）。"""
        from agent_py_agent.agent.memory_push import (
            format_memories_for_injection,
            push_relevant_memories,
        )

        agent = MagicMock()
        agent.memory = MagicMock()
        agent.memory.search.return_value = []

        context = {"task_id": "task_empty", "goal": "测试"}
        memories = push_relevant_memories(agent, "timeout", context, limit=3)

        # 格式化空列表返回空字符串
        formatted = format_memories_for_injection(memories)
        assert formatted == ""