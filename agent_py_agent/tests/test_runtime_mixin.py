from __future__ import annotations

"""LLM: tests for runtime_mixin.

给人看的解释：
测试 SimpleAgentRuntimeMixin 的 run() 方法、memory 压缩、token 估算等功能。
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import pytest


def test_run_exit_code_rejects_old_success_alias() -> None:
    from agent_py_agent.cli.run_output import run_exit_code

    assert run_exit_code(SimpleNamespace(runtime_status="ok")) == 0
    assert run_exit_code(SimpleNamespace(runtime_status="")) == 0
    assert run_exit_code(SimpleNamespace(runtime_status="succeeded")) == 2


def test_print_run_result_reports_typed_conversation_degradation(capsys) -> None:
    from agent_py_agent.cli.run_output import print_run_result

    result = SimpleNamespace(
        response="实际最终回复",
        prompt="最终 prompt",
        backend="test",
        used_memories=0,
        tool_rounds=0,
        memory_route_matches=0,
        prompt_token_estimate=10,
        runtime_injection_token_estimate=0,
        archive_events=0,
        memory_resume_context_injected=False,
        memory_resume_context_token_estimate=0,
        memory_compact_suggested=False,
        conversation_persist_degraded=True,
        conversation_persist_error="assistant transcript append failed",
    )

    print_run_result(result, show_prompt=False)

    output = capsys.readouterr().out
    assert "实际最终回复" in output
    assert "conversation_persist_degraded" in output
    assert "assistant transcript append failed" in output


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
        """旧 note 入口必须拒绝，不能恢复绕过统一候选链的兼容语义。"""
        from agent_py_agent.agent.agent_core.runtime_mixin import SimpleAgentRuntimeMixin

        mixin = SimpleAgentRuntimeMixin()

        with pytest.raises(ValueError, match="fact/event/project"):
            mixin.remember("测试记忆内容", kind="note")

    def test_remember_default_kind(self) -> None:
        """测试 remember 默认 kind。"""
        from agent_py_agent.agent.agent_core.runtime_mixin import SimpleAgentRuntimeMixin

        mixin = SimpleAgentRuntimeMixin()
        mock_memory = MagicMock()
        record = SimpleNamespace(entry_id="entry-1")
        mock_memory.all.return_value = [record]
        mixin.memory = mock_memory
        pending = SimpleNamespace(candidate_id="candidate-1", status="pending_review")
        approved = SimpleNamespace(candidate_id="candidate-1", status="approved")
        mixin.memory_candidates = MagicMock()
        mixin.memory_candidates.observe.return_value = pending
        mixin.memory_promotion = MagicMock()
        mixin.memory_promotion.review.return_value = approved
        mixin.memory_promotion.promote.return_value = SimpleNamespace(
            promoted=True,
            promotion_ref="memory:long_term#entry-1",
            reason_code="",
        )

        result = mixin.remember("内容")

        observation = mixin.memory_candidates.observe.call_args.args[0]
        assert observation.candidate_type == "long_term_fact"
        assert observation.promotion_target == "long_term"
        mixin.memory_promotion.review.assert_called_once()
        mixin.memory_promotion.promote.assert_called_once_with(
            "candidate-1",
            reviewer="local-user-explicit",
            confirmed=True,
        )
        mock_memory.add.assert_not_called()
        assert result is record

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
