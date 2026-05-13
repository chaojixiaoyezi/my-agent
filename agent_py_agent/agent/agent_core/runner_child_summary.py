# LLM: Runner child summary helpers keep dispatch payloads compact and refs-first.
# 模块用途: 汇总 runner 创建的下级代理状态，不读取产物正文，供父级调度记录判断是否需要继续。

from __future__ import annotations

from typing import Any

from ..subagent import SubAgentRunnerResult, SubAgentTask

_RUNNER_CHILD_FINAL_STATUSES = {"DONE", "FAILED", "TIMEOUT", "CHANNEL_ERROR", "TAKEN_OVER"}


# LLM: runner_child_summary_fields makes nested schedule_child_subagents visible to parent dispatch payloads.
# 函数用途: 从执行后的任务快照收集 child ids/roles 和 runner 摘要，避免上层模型把 dispatch 记录数当成孩子数。
def runner_child_summary_fields(agent: Any, after: SubAgentTask, result: SubAgentRunnerResult) -> dict[str, object]:
    child_ids = [str(item) for item in (after.child_ids or []) if str(item).strip()]
    child_states = _runner_child_states(agent, child_ids)
    return {
        "runner_summary": result.structured_summary,
        "runner_created_child_count": len(child_ids),
        "runner_created_child_ids": child_ids,
        "runner_created_roles": [item["role"] for item in child_states if item["role"]],
        "runner_child_status_counts": _runner_child_status_counts(child_states),
        "runner_unfinished_child_ids": _runner_unfinished_child_ids(child_states),
        "runner_partial_success": bool(child_ids and not result.ok),
    }


# LLM: _runner_child_states resolves child status/role from persisted refs only.
# 函数用途: 给 dispatch 报告附加轻量 child 状态；读失败时保留 id，避免调度记录写入失败。
def _runner_child_states(agent: Any, child_ids: list[str]) -> list[dict[str, str]]:
    states: list[dict[str, str]] = []
    for child_id in child_ids:
        try:
            child = agent.subagents.load(child_id)
        except Exception:
            states.append({"id": child_id, "role": "", "status": "UNKNOWN"})
            continue
        states.append({
            "id": child_id,
            "role": str(getattr(child, "role", "") or "").strip(),
            "status": str(getattr(child, "status", "") or "UNKNOWN").strip().upper(),
        })
    return states


# LLM: _runner_child_status_counts keeps partial-success records compact.
# 函数用途: 汇总 runner 已创建 child 的状态分布，不读取 child artifact 正文。
def _runner_child_status_counts(child_states: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in child_states:
        status = item["status"] or "UNKNOWN"
        counts[status] = counts.get(status, 0) + 1
    return counts


# LLM: _runner_unfinished_child_ids surfaces children that need another dispatch wave.
# 函数用途: 标出还没终态的 child ids，便于父级 timeout 后继续调度或接管。
def _runner_unfinished_child_ids(child_states: list[dict[str, str]]) -> list[str]:
    return [
        item["id"] for item in child_states
        if item["id"] and item["status"] not in _RUNNER_CHILD_FINAL_STATUSES
    ]
