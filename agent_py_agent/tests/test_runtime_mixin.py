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
