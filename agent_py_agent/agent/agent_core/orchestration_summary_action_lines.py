# LLM: Orchestration summary action lines keep bulky tool outputs refs-first.
# 模块用途: 集中维护调度类工具需要保留在 live prompt 的顶层状态字段。

from __future__ import annotations

import json
from typing import Any

_TOP_LEVEL_ACTION_KEYS = (
    "blocked",
    "reason",
    "created",
    "case_id",
    "request_id",
    "case_ref",
    "request_ref",
    "target_agent_ids",
    "required_capabilities",
    "ids",
    "request_count",
    "request_history_count",
    "pending_request_count",
    "blocked_request_count",
    "completed_request_count",
    "declined_request_count",
    "evidence_count",
    "participant_count",
    "decision_count",
    "missing_evidence_request_ids",
    "ready_for_main_agent",
    "requires_main_agent",
    "rework_targets",
    "rework",
    "allowed_tools",
    "subagent_workspace",
    "completion_status",
    "must_not_report_done",
    "blocking_run_ids",
    "repair_advice",
    "created_run_ids",
    "planned_count",
    "runner_selection_recovery",
    "quality_advice",
    "current_turn_run_state",
    "next_action",
)


# LLM: top_level_action_lines keeps schedule outputs useful even without direct_children.
# 函数用途: 提取 schedule_child_subagents 等顶层字段中的新建 run、警告和建议。
def top_level_action_lines(payload: dict[str, Any]) -> list[str]:
    return [
        f"- {key}: {_json_inline(payload.get(key))}"
        for key in _TOP_LEVEL_ACTION_KEYS
        if key in payload and payload.get(key) not in (None, "", [], {})
    ]


def _json_inline(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)
