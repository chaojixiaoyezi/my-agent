# LLM: Quality advice payload rendering is shared by schedule and dispatch tools.
# 模块用途: 把 QA advice dataclass 转成 refs-only JSON，避免多个工具各自复制字段。

from __future__ import annotations


# LLM: quality_advice_payload keeps QA suggestions compact and copyable for the next tool call.
# 函数用途: 把 QA advice 转成模型可读 JSON，不读取任何业务产物正文。
def quality_advice_payload(advice) -> dict[str, object]:
    return {
        "phase": advice.phase,
        "llm_next_step": advice.llm_next_step,
        "guardrails": list(advice.guardrails),
        "suggested_roles": list(advice.suggested_roles),
        "ready_work_refs": list(advice.ready_work_refs),
        "suggested_children": [_quality_child_payload(item) for item in advice.suggested_children],
    }


# LLM: _quality_child_payload keeps suggested QA specs refs-only and safe for prompt reuse.
# 函数用途: 输出候选 QA child 的最小字段，LLM 可复制后按 scope/work_group_id 自行调整。
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
