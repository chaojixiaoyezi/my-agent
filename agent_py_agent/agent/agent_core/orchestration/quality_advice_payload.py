from __future__ import annotations


def quality_advice_payload(advice) -> dict[str, object]:
    return {
        "phase": advice.phase,
        "llm_next_step": advice.llm_next_step,
        "guardrails": list(advice.guardrails),
        "suggested_roles": list(advice.suggested_roles),
        "ready_work_refs": list(advice.ready_work_refs),
        "suggested_children": [_quality_child_payload(item) for item in advice.suggested_children],
    }


def _quality_child_payload(item) -> dict[str, object]:
    return {
        "goal": item.goal,
        "agent_name": item.agent_name,
        "role": item.role,
        "acceptance_checks": list(item.acceptance_checks),
        "source_run_ids": list(item.source_run_ids),
        "source_artifact_refs": list(item.source_artifact_refs),
        "source_output_refs": list(item.source_output_refs),
        "quality_scope": item.quality_scope,
    }


__all__ = ["quality_advice_payload"]
