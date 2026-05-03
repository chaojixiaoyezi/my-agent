from __future__ import annotations

"""LLM: tests for runtime_mixin.

给人看的解释：
测试 SimpleAgentRuntimeMixin 的 run() 方法、memory 压缩、token 估算等功能。
"""

from unittest.mock import MagicMock, PropertyMock, patch

import pytest


class TestRuntimeMixinCompress:
    """测试 memory 压缩相关方法。"""

    def test_compress_memories_no_op_when_small(self) -> None:
        """测试记忆少于 keep_recent 时不压缩。"""
        from agent_py_agent.agent.agent_core.runtime_mixin import SimpleAgentRuntimeMixin

        mixin = SimpleAgentRuntimeMixin()
        memories = [MagicMock(), MagicMock()]
        result = mixin._compress_memories(memories, keep_recent=3)
        assert len(result) == 2

    def test_compress_memories_single_summary(self) -> None:
        """测试压缩时生成单个摘要。"""
        from agent_py_agent.agent.agent_core.runtime_mixin import SimpleAgentRuntimeMixin

        mixin = SimpleAgentRuntimeMixin()
        memories = [MagicMock() for _ in range(10)]
        for i, m in enumerate(memories):
            m.role = "user" if i % 2 == 0 else "agent"
            m.content = f"memory {i}"

        result = mixin._compress_memories(memories, keep_recent=2)
        assert len(result) == 3  # 1 summary + 2 recent

    def test_compress_memories_keeps_recent(self) -> None:
        """测试压缩保留最近 N 条。"""
        from agent_py_agent.agent.agent_core.runtime_mixin import SimpleAgentRuntimeMixin

        mixin = SimpleAgentRuntimeMixin()
        memories = [MagicMock() for _ in range(15)]
        for i, m in enumerate(memories):
            m.role = "user" if i % 2 == 0 else "agent"
            m.content = f"content {i}"

        result = mixin._compress_memories(memories, keep_recent=5)
        assert len(result) == 6

    def test_compress_memories_edge_case_exact(self) -> None:
        """测试刚好等于 keep_recent 时不压缩。"""
        from agent_py_agent.agent.agent_core.runtime_mixin import SimpleAgentRuntimeMixin

        mixin = SimpleAgentRuntimeMixin()
        memories = [MagicMock(), MagicMock(), MagicMock()]
        result = mixin._compress_memories(memories, keep_recent=3)
        assert len(result) == 3


class TestRuntimeMixinBuildSnapshot:
    """测试 compression snapshot 构建。"""

    def test_build_compression_snapshot_content(self) -> None:
        """测试快照内容构建。"""
        from agent_py_agent.agent.agent_core.runtime_mixin import SimpleAgentRuntimeMixin

        mixin = SimpleAgentRuntimeMixin()

        mock_routed = MagicMock()
        mock_routed.required_read_paths = ["/path/1", "/path/2"]
        mock_routed.candidate_paths = ["/candidate/1"]

        result = mixin._build_compression_snapshot_content(
            user_prompt="test prompt",
            memories=[],
            runtime_injections=["inject 1", "inject 2"],
            routed_context=mock_routed,
            resume_context_section="",
        )

        assert "user_prompt=test prompt" in result
        assert "routed_required=" in result
        assert "runtime_injections=" in result

    def test_build_compression_snapshot_with_resume(self) -> None:
        """测试带 resume context 的快照。"""
        from agent_py_agent.agent.agent_core.runtime_mixin import SimpleAgentRuntimeMixin

        mixin = SimpleAgentRuntimeMixin()

        mock_routed = MagicMock()
        mock_routed.required_read_paths = []
        mock_routed.candidate_paths = []

        result = mixin._build_compression_snapshot_content(
            user_prompt="test",
            memories=[],
            runtime_injections=[],
            routed_context=mock_routed,
            resume_context_section="### Auto Recovery Context\nrecovery content here",
        )

        assert "resume_context=" in result
        assert "recovery content here" in result

    def test_build_compression_snapshot_empty_memories(self) -> None:
        """测试空 memories 列表。"""
        from agent_py_agent.agent.agent_core.runtime_mixin import SimpleAgentRuntimeMixin

        mixin = SimpleAgentRuntimeMixin()
        mock_routed = MagicMock()
        mock_routed.required_read_paths = []
        mock_routed.candidate_paths = []

        result = mixin._build_compression_snapshot_content(
            user_prompt="test",
            memories=[],
            runtime_injections=[],
            routed_context=mock_routed,
            resume_context_section="",
        )

        assert "user_prompt=test" in result

    def test_build_compression_snapshot_many_memories(self) -> None:
        """测试大量 memories 的快照。"""
        from agent_py_agent.agent.agent_core.runtime_mixin import SimpleAgentRuntimeMixin

        mixin = SimpleAgentRuntimeMixin()
        memories = [MagicMock() for _ in range(20)]
        for i, m in enumerate(memories):
            m.role = "user" if i % 2 == 0 else "agent"
            m.content = f"content {i}"
        mock_routed = MagicMock()
        mock_routed.required_read_paths = ["/p1"]
        mock_routed.candidate_paths = ["/c1"]

        result = mixin._build_compression_snapshot_content(
            user_prompt="test",
            memories=memories,
            runtime_injections=[],
            routed_context=mock_routed,
            resume_context_section="",
        )

        assert "recent_memories=" in result


class TestRuntimeMixinMemoryMethods:
    """测试 memory 相关方法。"""

    def test_remember(self) -> None:
        """测试 remember 方法。"""
        from agent_py_agent.agent.agent_core.runtime_mixin import SimpleAgentRuntimeMixin

        mixin = SimpleAgentRuntimeMixin()
        mock_memory = MagicMock()
        mock_memory.add.return_value = None
        mixin.memory = mock_memory

        mixin.remember("测试记忆内容", kind="note")
        mock_memory.add.assert_called_once_with("user", "测试记忆内容", kind="note")

    def test_remember_default_kind(self) -> None:
        """测试 remember 默认 kind。"""
        from agent_py_agent.agent.agent_core.runtime_mixin import SimpleAgentRuntimeMixin

        mixin = SimpleAgentRuntimeMixin()
        mock_memory = MagicMock()
        mock_memory.add.return_value = None
        mixin.memory = mock_memory

        mixin.remember("内容")
        mock_memory.add.assert_called_once_with("user", "内容", kind="note")

    def test_recall(self) -> None:
        """测试 recall 方法。"""
        from agent_py_agent.agent.agent_core.runtime_mixin import SimpleAgentRuntimeMixin

        mixin = SimpleAgentRuntimeMixin()
        mock_memory = MagicMock()
        mock_memory.search.return_value = [MagicMock(), MagicMock()]
        mock_memory.top_k = 5
        mixin.memory = mock_memory
        mixin.config = MagicMock()
        mixin.config.memory_top_k = 5

        results = mixin.recall("查询内容")
        assert len(results) == 2

    def test_recall_with_top_k(self) -> None:
        """测试带 top_k 的 recall。"""
        from agent_py_agent.agent.agent_core.runtime_mixin import SimpleAgentRuntimeMixin

        mixin = SimpleAgentRuntimeMixin()
        mock_memory = MagicMock()
        mock_memory.search.return_value = [MagicMock()]
        mixin.memory = mock_memory

        results = mixin.recall("query", top_k=3)
        mock_memory.search.assert_called_once_with("query", 3)