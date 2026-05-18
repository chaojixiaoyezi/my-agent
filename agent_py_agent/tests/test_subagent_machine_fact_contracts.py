"""Subagent machine-fact architecture contracts."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


# LLM: legacy acceptance helper must stay a compatibility shim around the service implementation.
# 函数用途: 防止验收证据逻辑再次分叉成 helper/service 双轨，导致一边修自然语言事实源、一边漏。
def test_legacy_acceptance_evidence_helper_stays_service_shim() -> None:
    """Legacy evidence helper must not own independent machine-fact logic."""

    path = REPO_ROOT / "agent_py_agent/agent/subagents/acceptance_helpers/evidence_acceptance_findings.py"
    text = path.read_text(encoding="utf-8")
    assert "..services.acceptance_evidence_findings" in text
    assert "def _build_presence_findings" not in text
    assert "def _build_tool_requirement_findings" not in text
    assert "def _required_tool_evidence" not in text


# LLM: create idempotency must never fall back to natural-language goal matching.
# 函数用途: 保证重复创建复用只来自 repair/idempotency 合同，不重新把 goal 文本当机器事实。
def test_create_subagents_idempotency_does_not_compare_goal_text() -> None:
    """Create/schedule idempotency must use structured contracts, not goal text."""

    for relative_path in (
        "agent_py_agent/agent/agent_core/orchestration_create_idempotency.py",
        "agent_py_agent/agent/subagents/services/hierarchy_schedule_idempotency.py",
    ):
        text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        forbidden_markers = [
            "_normalized_goal",
            "goal_output_refs(",
            "params.goal",
            "request.goal == task.goal",
            "task.goal == request.goal",
        ]
        assert [marker for marker in forbidden_markers if marker in text] == []
