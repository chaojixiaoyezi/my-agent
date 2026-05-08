# LLM: Compact resume completion prompts guide users to add missing work-state facts safely.
# 模块用途: 当 compact resume 缺验收、约束或测试字段时，生成半自动补全模板；不写文件、不猜事实。

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


# LLM: CompactCompletionPromptRequest bundles work-state gaps and apply identity for prompt rendering.
# 类用途: 汇总缺失字段、apply_id 和 plan_id，生成可复制的半自动补全提示。
@dataclass(frozen=True)
class CompactCompletionPromptRequest:
    apply_id: str
    plan_id: str
    work_state: dict[str, Any]


# LLM: build_compact_completion_prompt returns guidance only; it never edits runtime facts.
# 函数用途: 根据 missing_fields 生成补全模板，让用户用明确标签补齐 compact work_state。
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


# LLM: _missing_fields normalizes work-state missing_fields into stable strings.
# 函数用途: 提取缺失字段列表，过滤空值并保持原始顺序。
def _missing_fields(work_state: dict[str, Any]) -> list[str]:
    value = work_state.get("missing_fields", [])
    return [str(item) for item in value if str(item).strip()] if isinstance(value, list) else []


# LLM: _field_payload explains one missing compact work-state field for humans and future UI.
# 函数用途: 返回字段名、展示标签和补写提示，方便 CLI/GUI 生成同一补全界面。
def _field_payload(field: str) -> dict[str, str]:
    return {
        "field": field,
        "label": _FIELD_LABELS.get(field, field),
        "hint": _FIELD_HINTS.get(field, "补充明确、可核对的事实"),
    }


# LLM: _prompt_template is copyable user input, not an instruction to auto-write facts.
# 函数用途: 生成用户可复制到下一次 run 的标签化模板，缺什么字段就显示什么字段。
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


# LLM: _suggested_commands keeps semi-auto recovery explicit and copyable without writing facts itself.
# 函数用途: 根据 compact scope 生成 memory-fact-write 和重新 resume 的建议命令；缺字段时才返回。
def _suggested_commands(request: CompactCompletionPromptRequest, missing: list[str]) -> list[str]:
    if not missing:
        return []
    fact_id = _fact_id_from_scope(request.work_state)
    fact_flag = f" --fact-id {fact_id}" if fact_id else ""
    return [
        f"my-agent memory-fact-write{fact_flag} --from-compact {request.apply_id} "
        '--acceptance "..." --constraint "..." --latest-test "..."',
        "my-agent memory-compact --apply  # rerun with the same request/session/task/run scope",
        "my-agent memory-resume --from-compact <new_apply_id> --compact-resume-mode auto",
    ]


# LLM: _fact_id_from_scope chooses a stable runtime_facts directory from the compact work-state scope.
# 函数用途: 优先 request/session/task/run id，保证补全事实能被下一次同 scope compact apply 读到。
def _fact_id_from_scope(work_state: dict[str, Any]) -> str:
    scope = work_state.get("scope", {}) if isinstance(work_state.get("scope"), dict) else {}
    for key in ("request_id", "session_id", "task_id", "run_id"):
        if value := str(scope.get(key) or "").strip():
            return value
    return ""


__all__ = ["CompactCompletionPromptRequest", "build_compact_completion_prompt"]
