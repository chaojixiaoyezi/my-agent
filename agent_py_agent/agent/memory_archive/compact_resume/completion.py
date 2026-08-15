
from __future__ import annotations

"""semi-auto completion prompt helpers for compact resume."""

from dataclasses import dataclass
from typing import Any

_FIELD_LABELS = {
    "goal": "目标",
    "next_step": "下一步",
    "acceptance": "验收条件",
    "constraints": "约束",
    "latest_tests": "测试",
}

_FIELD_HINTS = {
    "goal": "一句话说明当前任务真实目标",
    "next_step": "下一步准备做什么",
    "acceptance": "完成后必须满足的可验证条件",
    "constraints": "不能触碰、不能覆盖或必须遵守的限制",
    "latest_tests": "最近已跑或必须跑的测试命令和结果",
}


@dataclass(frozen=True)
class CompactCompletionPromptRequest:
    apply_id: str
    plan_id: str
    work_state: dict[str, Any]


def build_compact_completion_prompt(request: CompactCompletionPromptRequest) -> dict[str, Any]:
    missing = _missing_fields(request.work_state)
    fields = [_field_payload(field) for field in missing]
    return {
        "status": "needs_user_input" if fields else "complete",
        "apply_id": request.apply_id,
        "plan_id": request.plan_id,
        "missing_fields": missing,
        "fields": fields,
        "prompt_template": _prompt_template(fields),
        "suggested_commands": _suggested_commands(request, missing),
        "runtime_fact_source_hint": "Use a future run prompt with these explicit labels, or write an approved task.json fact source.",
        "automatic_write": False,
    }


def _missing_fields(work_state: dict[str, Any]) -> list[str]:
    value = work_state.get("missing_fields", [])
    return [str(item) for item in value if str(item).strip()] if isinstance(value, list) else []


def _field_payload(field: str) -> dict[str, str]:
    return {
        "field": field,
        "label": _FIELD_LABELS.get(field, field),
        "hint": _FIELD_HINTS.get(field, "补充明确、可核对的事实"),
    }


def _prompt_template(fields: list[dict[str, str]]) -> str:
    if not fields:
        return ""
    lines = [
        "请把下面信息作为明确事实源记录，用于 compact resume：",
        "",
    ]
    for field in fields:
        lines.extend([f"{field['label']}:", f"- <{field['hint']}>", ""])
    return "\n".join(lines).rstrip()


def _suggested_commands(request: CompactCompletionPromptRequest, missing: list[str]) -> list[str]:
    if not missing:
        return []
    fact_id = _fact_id_from_scope(request.work_state)
    fact_flag = f" --fact-id {fact_id}" if fact_id else ""
    scope_flags = _scope_flags(request.work_state)
    return [
        f"my-agent memory-fact-write{fact_flag} --from-compact {request.apply_id} "
        '--acceptance "..." --constraint "..." --latest-test "..."',
        f"my-agent memory-compact --apply{scope_flags}",
        "my-agent memory-resume --from-compact <new_apply_id> --compact-resume-mode auto",
    ]


def _fact_id_from_scope(work_state: dict[str, Any]) -> str:
    scope = work_state.get("scope", {}) if isinstance(work_state.get("scope"), dict) else {}
    for key in ("request_id", "session_id", "task_id", "run_id"):
        if value := str(scope.get(key) or "").strip():
            return value
    return ""


def _scope_flags(work_state: dict[str, Any]) -> str:
    scope = work_state.get("scope", {}) if isinstance(work_state.get("scope"), dict) else {}
    flags = []
    for key, flag in (
        ("session_id", "--session-id"),
        ("request_id", "--request-id"),
        ("task_id", "--task-id"),
        ("run_id", "--run-id"),
    ):
        value = str(scope.get(key) or "").strip()
        if value:
            flags.append(f"{flag} {_quote_shell(value)}")
    return f" {' '.join(flags)}" if flags else ""


def _quote_shell(value: str) -> str:
    if value and all(char.isalnum() or char in "._:/=-" for char in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


__all__ = ["CompactCompletionPromptRequest", "build_compact_completion_prompt"]
