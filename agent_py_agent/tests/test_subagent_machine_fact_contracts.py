"""Subagent machine-fact architecture contracts."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_create_subagents_idempotency_does_not_compare_goal_text() -> None:
    """Create/schedule idempotency must use structured contracts, not goal text."""

    for relative_path in (
        "agent_py_agent/agent/agent_core/orchestration/create_idempotency.py",
        "agent_py_agent/agent/subagents/services/hierarchy/schedule_idempotency.py",
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
