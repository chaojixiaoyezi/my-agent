# LLM: delivery repair guard keeps staged-delivery recovery actions actionable without relying on prompt prose as facts.
# 模块用途: 当 closeout 已经给出“先写 checkpoint / 先补非空结构化数据 / 先调 builder”这类结构化恢复动作时，阻止模型继续只读空转。

from __future__ import annotations

import json
from pathlib import Path

from ..backend import ModelResponse

_MAX_REPAIRS = 2
_WRITE_FIRST_ACTIONS = {
    "invoke_builder_tool",
    "materialize_checkpoint",
    "repair_evidence_refs",
    "repair_structured_checkpoint_json",
    "write_non_empty_structured_rows",
}
_PRODUCTIVE_TOOL_NAMES = {
    "append_file",
    "data_to_workbook",
    "file_write_session",
    "replace_in_file",
    "run_command",
    "write_file",
}
_INSPECTION_ONLY_TOOL_NAMES = {
    "fetch_url",
    "list_files",
    "list_tools",
    "read_file",
    "search",
}
_STRICT_REPAIR_PRODUCTIVE_TOOLS = {
    "append_file",
    "data_to_workbook",
    "file_write_session",
    "replace_in_file",
    "write_file",
}


# LLM: delivery_repair_context exposes required staged-repair actions from closeout.json as one structured prompt hint.
# 函数用途: 读取 .agent_delivery/closeout.json 中的结构化恢复动作；若当前必须先修阶段产物，则返回下一轮模型可见的纠偏合同。
def delivery_repair_context(agent: object, repairs: int) -> str:
    payload = _delivery_repair_payload(agent)
    if not payload or repairs >= _MAX_REPAIRS:
        return ""
    return "\n".join(
        [
            "[tool-system delivery-required-repair]",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            "当前仍处于阶段产物修复模式。下一轮必须优先执行能推进 required_actions 的真实工具调用，"
            "例如写出/修复 checkpoint、补齐非空结构化数据，或调用 builder tool。"
            "不要只重复 read_file、list_files、search、list_tools 这类检查动作。",
        ]
    )


# LLM: delivery_repair_block_response stops the loop once the model ignores required staged-repair actions multiple times.
# 函数用途: 模型连续忽略结构化阶段修复动作时，返回确定性阻断，避免在同一 no-progress 状态无限消耗轮次。
def delivery_repair_block_response(agent: object) -> ModelResponse | None:
    payload = _delivery_repair_payload(agent)
    if not payload:
        return None
    return ModelResponse(
        text=(
            "[DELIVERY_REQUIRED_REPAIR_BLOCKED] 结构化交付合同已经明确下一步必须先修阶段产物，"
            "但模型仍连续没有执行对应写入/构建动作，已停止本轮以避免继续空转。"
        ),
        backend=str(getattr(getattr(agent, "backend", None), "name", "") or ""),
    )


# LLM: has_required_delivery_repair returns True only when closeout recovery actions require a write-first step.
# 函数用途: 让工具循环只在“必须先写/修/构建”的阶段收紧；普通 acceptance failed 不会触发这里。
def has_required_delivery_repair(agent: object) -> bool:
    return bool(_delivery_repair_payload(agent))


# LLM: is_delivery_repair_productive_call checks whether at least one tool call can satisfy the current staged-repair contract.
# 函数用途: 只要本轮工具调用里包含 builder 或明显的写入/执行动作，就视为在推进 staged repair，不做拦截。
def is_delivery_repair_productive_call(agent: object, calls: list[dict[str, object]]) -> bool:
    payload = _delivery_repair_payload(agent)
    if not payload:
        return True
    required_actions = [item for item in payload.get("required_actions", []) if isinstance(item, dict)]
    builder_tools = {
        str(item.get("builder_tool") or "").strip()
        for item in required_actions
    }
    strict_write_required = bool(payload.get("strict_write_required"))
    productive_tools = _productive_tools(payload, builder_tools)
    inspection_only_tools = _inspection_only_tools(required_actions, strict_write_required)
    return any(_call_is_productive(call, productive_tools, inspection_only_tools, strict_write_required) for call in calls)


# LLM: _delivery_repair_payload extracts only the active write-first recovery actions from closeout.json.
# 函数用途: 读取 closeout 报告并筛选 recommended_action 属于写入/构建优先级的恢复动作；不读取自然语言日志。
def _delivery_repair_payload(agent: object) -> dict[str, object]:
    report = _closeout_report(agent)
    if not report or report.get("ok") is True:
        return {}
    progress = report.get("delivery_progress")
    if not isinstance(progress, dict):
        return {}
    actions = progress.get("recovery_actions")
    if not isinstance(actions, list):
        return {}
    required_actions = [
        {
            "builder_tool": str(item.get("builder_tool") or ""),
            "category": str(item.get("category") or ""),
            "checkpoint_shape_hint": str(item.get("checkpoint_shape_hint") or ""),
            "checkpoint_ref": str(item.get("checkpoint_ref") or ""),
            "code": str(item.get("code") or ""),
            "missing_columns": str(item.get("missing_columns") or ""),
            "output_ref": str(item.get("output_ref") or ""),
            "recommended_action": str(item.get("recommended_action") or ""),
            "required_columns": item.get("required_columns") if isinstance(item.get("required_columns"), list) else [],
            "retryable": bool(item.get("retryable", True)),
            "source_ref": str(item.get("source_ref") or ""),
        }
        for item in actions
        if isinstance(item, dict)
        and str(item.get("recommended_action") or "").strip() in _WRITE_FIRST_ACTIONS
    ]
    if not required_actions:
        return {}
    pending_targets = progress.get("pending_materialization_targets")
    return {
        "pending_materialization_targets": pending_targets if isinstance(pending_targets, list) else [],
        "report_ref": str(report.get("report_ref") or ""),
        "required_actions": required_actions,
        "strict_write_required": _strict_write_required(progress),
    }


# LLM: _closeout_report keeps the repair guard grounded in the same machine report that closeout writes.
# 函数用途: 读取当前工作区 .agent_delivery/closeout.json；不存在或损坏时返回空对象，不让纠偏逻辑抛异常。
def _closeout_report(agent: object) -> dict[str, object]:
    path = Path(getattr(agent, "root", ".")).resolve() / ".agent_delivery" / "closeout.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


# LLM: _call_is_productive treats pure inspection loops as non-productive but leaves real data gathering or execution actions alone.
# 函数用途: 只有明确的只读检查工具才会被 staged repair guard 拦下；抓取数据、执行命令和构建产物都算推进。
def _call_is_productive(
    call: dict[str, object],
    productive_tools: set[str],
    inspection_only_tools: set[str],
    strict_write_required: bool,
) -> bool:
    tool = str(call.get("tool") or "").strip()
    if tool in productive_tools:
        return True
    if strict_write_required:
        return False
    return tool not in inspection_only_tools


def _productive_tools(payload: dict[str, object], builder_tools: set[str]) -> set[str]:
    if bool(payload.get("strict_write_required")):
        return {tool for tool in builder_tools if tool} | _STRICT_REPAIR_PRODUCTIVE_TOOLS
    return {tool for tool in builder_tools if tool} | _PRODUCTIVE_TOOL_NAMES


def _strict_write_required(progress: dict[str, object]) -> bool:
    try:
        unchanged = int(progress.get("unchanged_failure_count") or 0)
    except (TypeError, ValueError):
        unchanged = 0
    try:
        threshold = int(progress.get("no_progress_block_threshold") or 0)
    except (TypeError, ValueError):
        threshold = 0
    return threshold > 0 and unchanged >= threshold


# LLM: _inspection_only_tools tightens the redirect rule only when the contract has advanced to a builder-ready stage.
# 函数用途: 基础检查工具一直算 inspection-only；只有进入 invoke_builder_tool 阶段时，read_artifact 才一并视为拖延动作。
def _inspection_only_tools(required_actions: list[dict[str, object]], strict_write_required: bool) -> set[str]:
    tools = set(_INSPECTION_ONLY_TOOL_NAMES)
    if strict_write_required or any(str(item.get("recommended_action") or "") == "invoke_builder_tool" for item in required_actions):
        tools.add("read_artifact")
    return tools
