"""记忆推送功能测试。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestMemoryPushMutationCoverage:
    """Tests to cover mutation-prone logic in memory_push.py."""

    def test_memory_limit_uses_gte_not_gt(self):
        """Memory limit check should use >= not >.

        Mutation: if len(memories_text) > limit (changed >= to >)
        This would return one extra memory item.
        """
        from unittest.mock import MagicMock

        from agent_py_agent.agent.memory_push import MemoryEntry, MemoryType, push_relevant_memories

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

        from agent_py_agent.agent.memory_push import MemoryType, push_relevant_memories

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
