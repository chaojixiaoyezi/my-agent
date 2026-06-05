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
    "evidence_count",
    "participant_count",
    "decision_count",
    "case_window",
    "collection_result",
    "requires_main_agent",
    "allowed_tools",
    "subagent_workspace",
    "completion_status",
    "completion_risk",
    "blocking_run_ids",
    "repair_advice",
    "created_run_ids",
    "planned_count",
    "runner_selection_recovery",
    "quality_advice",
    "current_turn_run_state",
    "next_action",
)


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
