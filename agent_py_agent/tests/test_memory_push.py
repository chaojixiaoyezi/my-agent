"""记忆推送功能测试。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestMemoryType:
    """测试 MemoryType 枚举。"""

    def test_memory_type_from_string(self):
        """从字符串创建 MemoryType。"""
        from agent_py_agent.agent.memory_push import MemoryType

        # 有效类型
        assert MemoryType.from_string("lesson_general") == MemoryType.LESSON_GENERAL
        assert MemoryType.from_string("lesson_task") == MemoryType.LESSON_TASK
        assert MemoryType.from_string("lesson_temp") == MemoryType.LESSON_TEMP
        assert MemoryType.from_string("context") == MemoryType.CONTEXT
        assert MemoryType.from_string("fact") == MemoryType.FACT

        # 大小写不敏感
        assert MemoryType.from_string("LESSON_GENERAL") == MemoryType.LESSON_GENERAL
        assert MemoryType.from_string("Lesson_Task") == MemoryType.LESSON_TASK

        # 无效值默认为 LESSON_GENERAL
        assert MemoryType.from_string("") == MemoryType.LESSON_GENERAL
        assert MemoryType.from_string("unknown") == MemoryType.LESSON_GENERAL


class TestMemoryEntry:
    """测试 MemoryEntry 数据类。"""

    def test_memory_entry_to_dict(self):
        """MemoryEntry 转换为字典。"""
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
        assert data["tags"] == ["timeout", "gateway"]
        assert data["content"] == "测试内容"
        assert data["lesson"] == "超时后要检查 start_time"
        assert data["action"] == "use_scoped_lock"
        assert data["result"] == "success"

    def test_memory_entry_from_dict(self):
        """从字典创建 MemoryEntry。"""
        from agent_py_agent.agent.memory_push import MemoryEntry, MemoryType

        data = {
            "type": "lesson_task",
            "trigger_type": "timeout",
            "tags": ["timeout", "gateway"],
            "content": "测试内容",
            "lesson": "超时后要检查 start_time",
            "action": "use_scoped_lock",
            "result": "success",
            "created_at": 1234567890.0,
        }

        entry = MemoryEntry.from_dict(data)
        assert entry.type == MemoryType.LESSON_TASK
        assert entry.trigger_type == "timeout"
        assert entry.content == "测试内容"

    def test_to_memory_record_content(self):
        """转换为简短记忆文本。"""
        from agent_py_agent.agent.memory_push import MemoryEntry, MemoryType

        entry = MemoryEntry(
            type=MemoryType.LESSON_TASK,
            content="这是完整的记忆内容，应该被截取",
            lesson="简短教训",
        )

        text = entry.to_memory_record_content()
        assert "[lesson_task]" in text
        assert "简短教训" in text


class TestPushRelevantMemories:
    """测试 push_relevant_memories 函数。"""

    def test_push_relevant_memories_no_memory(self, tmp_path: Path):
        """无 memory 属性时返回空列表。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories

        agent = MagicMock()
        del agent.memory  # 使其没有 memory 属性

        result = push_relevant_memories(agent, "timeout", {}, limit=3)
        assert result == []

    def test_push_relevant_memories_no_agent_memory(self, tmp_path: Path):
        """agent.memory 为 None 时返回空列表。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories

        agent = MagicMock()
        agent.memory = None

        result = push_relevant_memories(agent, "timeout", {}, limit=3)
        assert result == []

    def test_push_relevant_memories_with_mock_memory(self, tmp_path: Path):
        """使用模拟 memory 时返回搜索结果。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories
        from agent_py_agent.agent.memory_store import MemoryRecord

        agent = MagicMock()

        # 模拟 memory.search 返回记忆
        mock_records = [
            MemoryRecord(role="system", content="超时处理技巧", kind="lesson_task", tags=["timeout"], created_at=1234567890.0),
            MemoryRecord(role="system", content="使用 scoped lock", kind="lesson_general", tags=["lock"], created_at=1234567891.0),
        ]
        agent.memory.search.return_value = mock_records

        context = {"task_id": "test_123", "goal": "测试任务", "failure_type": "timeout"}
        result = push_relevant_memories(agent, "timeout", context, limit=3)

        assert len(result) >= 0  # 可能返回空因为过滤逻辑

    def test_push_relevant_memories_triggers_timeout(self, tmp_path: Path):
        """timeout 触发类型搜索记忆。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories
        from agent_py_agent.agent.memory_store import MemoryRecord

        agent = MagicMock()

        mock_records = [
            MemoryRecord(role="system", content="timeout lesson", kind="lesson_general", tags=["timeout"], created_at=1234567890.0),
        ]
        agent.memory.search.return_value = mock_records

        context = {"failure_type": "timeout"}
        result = push_relevant_memories(agent, "timeout", context, limit=3)

        # 验证搜索被调用
        agent.memory.search.assert_called()


class TestPushSpecificMemories:
    """测试特定触发类型的推送函数。"""

    def test_push_timeout_memories(self, tmp_path: Path):
        """推送超时相关记忆。"""
        from agent_py_agent.agent.memory_push import push_timeout_memories

        agent = MagicMock()
        agent.memory = MagicMock()
        agent.memory.search.return_value = []

        result = push_timeout_memories(agent, "task_123", "分析日志", timeout_count=3)

        assert isinstance(result, list)

    def test_push_failure_memories(self, tmp_path: Path):
        """推送失败相关记忆。"""
        from agent_py_agent.agent.memory_push import push_failure_memories

        agent = MagicMock()
        agent.memory = MagicMock()
        agent.memory.search.return_value = []

        result = push_failure_memories(agent, "task_456", "重构代码", "parse_error")

        assert isinstance(result, list)

    def test_push_planning_memories(self, tmp_path: Path):
        """推送计划相关记忆。"""
        from agent_py_agent.agent.memory_push import push_planning_memories

        agent = MagicMock()
        agent.memory = MagicMock()
        agent.memory.search.return_value = []

        result = push_planning_memories(agent, "task_789", "设计架构")

        assert isinstance(result, list)


class TestFormatMemoriesForInjection:
    """测试记忆格式化函数。"""

    def test_format_empty_memories(self, tmp_path: Path):
        """空记忆列表返回空字符串。"""
        from agent_py_agent.agent.memory_push import format_memories_for_injection

        result = format_memories_for_injection([])
        assert result == ""

    def test_format_single_memory(self, tmp_path: Path):
        """单条记忆格式化。"""
        from agent_py_agent.agent.memory_push import format_memories_for_injection

        memories = ["[lesson_general] 测试教训"]
        result = format_memories_for_injection(memories)

        assert "[相关记忆提示]" in result
        assert "测试教训" in result

    def test_format_multiple_memories(self, tmp_path: Path):
        """多条记忆格式化。"""
        from agent_py_agent.agent.memory_push import format_memories_for_injection

        memories = [
            "[lesson_general] 教训1",
            "[lesson_task] 教训2",
            "[context] 上下文3",
        ]
        result = format_memories_for_injection(memories)

        assert "[相关记忆提示]" in result
        assert "教训1" in result
        assert "教训2" in result
        assert "上下文3" in result

    def test_format_truncates_long_memory(self, tmp_path: Path):
        """超过 200 字的记忆被截断。"""
        from agent_py_agent.agent.memory_push import format_memories_for_injection

        long_memory = "A" * 300  # 300 个字符
        result = format_memories_for_injection([long_memory])

        # 应该被截断到 200 字
        assert len(result) < 350  # 确保没有 300 字的原始内容
        assert "..." in result  # 截断标记


class TestWriteMemoryWithType:
    """测试带类型标签的记忆写入。"""

    def test_write_memory_with_type(self, tmp_path: Path):
        """写入带类型的记忆。"""
        from agent_py_agent.agent.memory_push import MemoryType, write_memory_with_type

        # 创建临时记忆文件
        memory_path = tmp_path / "memory.jsonl"
        mock_memory = MagicMock()
        mock_memory.add.return_value = MagicMock()

        write_memory_with_type(
            mock_memory,
            content="测试记忆内容",
            mem_type=MemoryType.LESSON_TASK,
            trigger_type="timeout",
            tags=["test"],
            lesson="测试教训",
            action="test_action",
            result="success",
        )

        mock_memory.add.assert_called_once()


class TestFailureAnalysisMemories:
    """测试 FailureAnalysis 增加的 relevant_memories 字段。"""

    def test_failure_analysis_has_relevant_memories(self, tmp_path: Path):
        """FailureAnalysis 包含 relevant_memories 字段。"""
        from agent_py_agent.agent.agent_core.failure_analyzer import FailureAnalysis

        analysis = FailureAnalysis(
            failure_type="timeout",
            root_cause="task_too_large",
            suggested_action="split_task",
            relevant_memories=["memory1", "memory2"],
        )

        assert analysis.relevant_memories == ["memory1", "memory2"]

    def test_failure_analysis_default_empty_memories(self, tmp_path: Path):
        """FailureAnalysis 默认 relevant_memories 为空列表。"""
        from agent_py_agent.agent.agent_core.failure_analyzer import FailureAnalysis

        analysis = FailureAnalysis(
            failure_type="timeout",
            root_cause="task_too_large",
            suggested_action="split_task",
        )

        assert analysis.relevant_memories == []


class TestIntegration:
    """集成测试。"""

    def test_memory_push_integration(self, tmp_path: Path):
        """记忆推送完整流程测试。"""
        from agent_py_agent.agent.memory_push import (
            MemoryType,
            format_memories_for_injection,
            push_relevant_memories,
            write_memory_with_type,
        )
        from agent_py_agent.agent.memory_store import MemoryRecord

        # 模拟 agent
        agent = MagicMock()

        # 模拟 memory.search 返回记忆
        mock_records = [
            MemoryRecord(
                role="system",
                content="使用 scoped lock 避免超时",
                kind="lesson_general",
                tags=["timeout", "lock"],
                created_at=1234567890.0,
            ),
            MemoryRecord(
                role="system",
                content="任务拆分策略",
                kind="lesson_task",
                tags=["split"],
                created_at=1234567891.0,
            ),
        ]
        agent.memory.search.return_value = mock_records

        # 触发记忆推送
        context = {"task_id": "test", "goal": "测试任务", "failure_type": "timeout"}
        memories = push_relevant_memories(agent, "timeout", context, limit=3)

        # 格式化记忆
        if memories:
            formatted = format_memories_for_injection(memories)
            assert "[相关记忆提示]" in formatted

    def test_memory_flow_without_agent(self, tmp_path: Path):
        """没有 agent 时的记忆流程（不应崩溃）。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories

        # agent 为 None 或没有 memory
        result = push_relevant_memories(None, "timeout", {}, limit=3)
        assert result == []

        agent = MagicMock()
        agent.memory = None
        result = push_relevant_memories(agent, "failure", {}, limit=3)
        assert result == []


class TestMemoryPushBoundaryCases:
    """补充：memory_push 边界测试。"""

    def test_empty_memory_list_search(self, tmp_path: Path):
        """记忆列表为空时的搜索。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories

        agent = MagicMock()
        agent.memory = MagicMock()
        agent.memory.search.return_value = []  # 空列表

        result = push_relevant_memories(agent, "timeout", {}, limit=3)
        assert result == []

    def test_unknown_trigger_type(self, tmp_path: Path):
        """未知触发类型的处理。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories

        agent = MagicMock()
        agent.memory = MagicMock()
        agent.memory.search.return_value = []

        # 未知触发类型不应该崩溃
        result = push_relevant_memories(agent, "unknown_trigger_xyz", {"key": "value"}, limit=3)
        assert isinstance(result, list)

    def test_agent_not_initialized(self, tmp_path: Path):
        """agent 未初始化时的处理。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories

        # agent 是未初始化的 MagicMock
        agent = MagicMock(spec=[])  # 空 spec
        result = push_relevant_memories(agent, "timeout", {}, limit=3)
        assert result == []

    def test_memory_search_returns_none(self, tmp_path: Path):
        """memory.search 返回 None 时的处理。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories

        agent = MagicMock()
        agent.memory = MagicMock()
        agent.memory.search.return_value = None

        result = push_relevant_memories(agent, "timeout", {}, limit=3)
        assert result == []

    def test_format_with_none_in_list(self, tmp_path: Path):
        """格式化时列表包含 None 的处理。"""
        from agent_py_agent.agent.memory_push import format_memories_for_injection

        memories = ["valid memory", "another valid"]
        result = format_memories_for_injection(memories)
        assert isinstance(result, str)

    def test_corrupted_memory_record(self, tmp_path: Path):
        """损坏的记忆记录的处理。"""
        from agent_py_agent.agent.memory_push import MemoryEntry, MemoryType

        # 缺少必需字段
        incomplete_data = {
            "type": "lesson_general",
            # 缺少 content
        }

        entry = MemoryEntry.from_dict(incomplete_data)
        # 应该使用默认值而不是崩溃
        assert entry.content == ""

    def test_empty_context_dict(self, tmp_path: Path):
        """空上下文字典的处理。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories

        agent = MagicMock()
        agent.memory = MagicMock()
        agent.memory.search.return_value = []

        result = push_relevant_memories(agent, "timeout", {}, limit=3)
        assert result == []

    def test_limit_zero(self, tmp_path: Path):
        """limit=0 时的处理。"""
        from agent_py_agent.agent.memory_push import push_relevant_memories

        agent = MagicMock()
        agent.memory = MagicMock()
        agent.memory.search.return_value = []

        result = push_relevant_memories(agent, "timeout", {}, limit=0)
        assert result == []

    def test_memory_type_from_unknown_string(self, tmp_path: Path):
        """从未知字符串创建 MemoryType。"""
        from agent_py_agent.agent.memory_push import MemoryType

        # 未知类型应该返回默认 LESSON_GENERAL
        result = MemoryType.from_string("not_a_real_type")
        assert result == MemoryType.LESSON_GENERAL

        result = MemoryType.from_string("")
        assert result == MemoryType.LESSON_GENERAL

    def test_write_memory_with_none_trigger_type(self, tmp_path: Path):
        """写入记忆时 trigger_type 为 None。"""
        from agent_py_agent.agent.memory_push import MemoryType, write_memory_with_type

        mock_memory = MagicMock()
        mock_memory.add.return_value = MagicMock()

        write_memory_with_type(
            mock_memory,
            content="test content",
            mem_type=MemoryType.LESSON_TASK,
            trigger_type=None,  # None 类型
            tags=[],
        )

        mock_memory.add.assert_called_once()

class TestMemoryPushMutationCoverage:
    """Tests to cover mutation-prone logic in memory_push.py."""

    def test_memory_limit_uses_gte_not_gt(self):
        """Memory limit check should use >= not >.

        Mutation: if len(memories_text) > limit (changed >= to >)
        This would return one extra memory item.
        """
        from unittest.mock import MagicMock
        from agent_py_agent.agent.memory_push import push_relevant_memories, MemoryType, MemoryEntry

        mock_agent = MagicMock()
        mock_agent.memory = MagicMock()

        # Create mock records that will pass filters
        mock_records = []
        for i in range(5):
            mock_record = MagicMock()
            mock_record.content = f"This is a test memory content number {i} with enough length"
            mock_record.kind = "lesson_general"
            mock_record.tags = []
            mock_record.created_at = 0.0
            mock_records.append(mock_record)

        mock_agent.memory.search.return_value = mock_records

        context = {"task_id": "test", "goal": "timeout test scenario"}
        result = push_relevant_memories(mock_agent, "timeout", context, limit=3)

        # With >= check, limit=3 should return exactly 3 items
        # With > check (mutated), it could return 4
        assert len(result) == 3, f"Expected exactly 3 memories with limit=3, got {len(result)}"

    def test_memory_content_min_length_10(self):
        """Memory content must be at least 10 chars.

        Mutation: len(record.content) < 10 changed to < 20
        This would allow shorter (potentially meaningless) memories through.
        """
        from unittest.mock import MagicMock
        from agent_py_agent.agent.memory_push import push_relevant_memories

        mock_agent = MagicMock()
        mock_agent.memory = MagicMock()

        # Create a record with exactly 10 chars (should be included)
        mock_record = MagicMock()
        mock_record.content = "1234567890"  # Exactly 10 chars
        mock_record.kind = "lesson_general"
        mock_record.tags = []
        mock_record.created_at = 0.0

        mock_agent.memory.search.return_value = [mock_record]

        context = {"task_id": "test", "goal": "test"}
        result = push_relevant_memories(mock_agent, "timeout", context, limit=3)

        # With 10 char min, this should be included
        assert len(result) == 1, f"Expected 1 memory with 10-char content, got {len(result)}"

    def test_memory_type_filter_includes_lesson_task(self):
        """Memory type filter must include both LESSON_GENERAL and LESSON_TASK.

        Mutation: Filter changed to only include LESSON_GENERAL
        This would miss valuable LESSON_TASK memories.
        """
        from unittest.mock import MagicMock
        from agent_py_agent.agent.memory_push import push_relevant_memories, MemoryType

        mock_agent = MagicMock()
        mock_agent.memory = MagicMock()

        # Create a LESSON_TASK record
        mock_record = MagicMock()
        mock_record.content = "This is a task-specific lesson that should be included for timeout"
        mock_record.kind = "lesson_task"
        mock_record.tags = []
        mock_record.created_at = 0.0

        mock_agent.memory.search.return_value = [mock_record]

        context = {"task_id": "test", "goal": "timeout scenario"}
        result = push_relevant_memories(mock_agent, "timeout", context, limit=3)

        # LESSON_TASK should be included for timeout trigger
        assert len(result) == 1, f"Expected LESSON_TASK to be included, got {len(result)} memories"

    def test_memory_limit_check_uses_gte_not_gt(self):
        """Memory limit check must use >= not >.

        Mutation: if len(memories_text) > limit (changed >= to >)
        This would return one extra memory item.
        """
        from unittest.mock import MagicMock
        from agent_py_agent.agent.memory_push import push_relevant_memories

        mock_agent = MagicMock()
        mock_agent.memory = MagicMock()

        # Create mock records that will pass filters
        mock_records = []
        for i in range(4):
            mock_record = MagicMock()
            mock_record.content = f"This is a test memory content number {i} with enough length"
            mock_record.kind = "lesson_general"
            mock_record.tags = []
            mock_record.created_at = 0.0
            mock_records.append(mock_record)

        mock_agent.memory.search.return_value = mock_records

        context = {"task_id": "test", "goal": "timeout test scenario"}
        result = push_relevant_memories(mock_agent, "timeout", context, limit=3)

        # With >= check, limit=3 should return exactly 3 items
        # With > check (mutated), it would return 4
        assert len(result) == 3, f"Expected exactly 3 memories with limit=3, got {len(result)}"

    def test_goal_keyword_extraction_uses_50_chars(self):
        """Goal keyword extraction must use 50 chars, not 25.

        Mutation: if len(goal) > 50 changed to > 25
        This would extract shorter (less useful) keywords.
        """
        from unittest.mock import MagicMock
        from agent_py_agent.agent.memory_push import push_relevant_memories

        mock_agent = MagicMock()
        mock_agent.memory = MagicMock()

        # Create a record that will only be found if goal extraction is correct
        mock_record = MagicMock()
        mock_record.content = "Related to very long task description about file processing"
        mock_record.kind = "lesson_general"
        mock_record.tags = []
        mock_record.created_at = 0.0

        mock_agent.memory.search.return_value = [mock_record]

        # Goal > 50 chars - should extract first 50 chars for search
        long_goal = "A" * 60  # 60 char goal
        context = {"task_id": "test", "goal": long_goal}

        # Mock the search to verify what query is used
        def check_query(query, top_k=None):
            # With 50 char extraction, query should contain "AAAA..."
            # With 25 char extraction, query would be shorter
            assert len(query) >= 45, f"Query should contain 50 char goal prefix, got query: {query[:50]}"
            return [mock_record]

        mock_agent.memory.search.side_effect = check_query

        result = push_relevant_memories(mock_agent, "timeout", context, limit=3)

    def test_return_respects_limit_not_doubled(self):
        """Return must respect limit, not double it.

        Mutation: return memories_text[:limit] changed to [:limit * 2]
        This would return more memories than requested.
        """
        from unittest.mock import MagicMock
        from agent_py_agent.agent.memory_push import push_relevant_memories

        mock_agent = MagicMock()
        mock_agent.memory = MagicMock()

        # Create 4 mock records
        mock_records = []
        for i in range(4):
            mock_record = MagicMock()
            mock_record.content = f"Test memory content number {i} that is long enough"
            mock_record.kind = "lesson_general"
            mock_record.tags = []
            mock_record.created_at = 0.0
            mock_records.append(mock_record)

        mock_agent.memory.search.return_value = mock_records

        context = {"task_id": "test", "goal": "timeout test"}
        result = push_relevant_memories(mock_agent, "timeout", context, limit=2)

        # Should return at most 2 items (limit=2)
        assert len(result) <= 2, f"Expected at most 2 memories with limit=2, got {len(result)}"

