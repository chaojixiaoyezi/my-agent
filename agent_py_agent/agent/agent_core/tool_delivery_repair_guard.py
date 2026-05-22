# LLM: delivery repair guard keeps staged-delivery recovery actions actionable without relying on prompt prose as facts.
# 模块用途: 当 closeout 已经给出“先写 checkpoint / 先补非空结构化数据 / 先调 builder”这类结构化恢复动作时，阻止模型继续只读空转。

from __future__ import annotations

import json

from ..backend import ModelResponse
from .tool_delivery_repair_declared_reads import (
    declared_repair_reads_exhausted,
    reset_declared_read_allowance,
)
from .tool_delivery_repair_evidence_shape import violates_evidence_repair_shape
from .tool_delivery_repair_paths import (
    call_command,
    call_path,
    command_references_ref,
    repair_target_values,
    same_path_ref,
)
from .tool_delivery_repair_payload import delivery_repair_payload
from .tool_delivery_repair_productivity import (
    EVIDENCE_GATHERING_TOOL_NAMES,
    DeliveryRepairProductivityContext,
    inspection_only_tools,
    productive_tools,
)
from .tool_delivery_repair_rejection_context import render_delivery_repair_rejection_context
from .tool_shell_command_classifier import command_has_local_mutation

_MAX_REPAIRS = 2
_RUN_COMMAND_INSPECTION_PREFIXES = ("cat ", "curl ", "find ", "ls", "pwd", "rg ", "wget ")
_PARAMS_UNSET = object()


# LLM: delivery_repair_context exposes required staged-repair actions from closeout.json as one structured prompt hint.
# 函数用途: 读取 .agent_delivery/closeout.json 中的结构化恢复动作；若当前必须先修阶段产物，则返回下一轮模型可见的纠偏合同。
def delivery_repair_context(agent: object, repairs: int, runtime_params: object = _PARAMS_UNSET) -> str:
    payload = _delivery_repair_payload(agent, runtime_params)
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


# LLM: delivery_repair_rejection_context turns a blocked inspection turn into structured feedback.
# 函数用途: 当模型在必修阶段产物时调用了无进展工具，把被拒绝调用和下一步 required_tool_calls 一起反馈给下一轮。
def delivery_repair_rejection_context(
    agent: object,
    calls: list[dict[str, object]],
    repairs: int,
    runtime_params: object = _PARAMS_UNSET,
) -> str:
    return render_delivery_repair_rejection_context(
        _delivery_repair_payload(agent, runtime_params),
        calls,
        repairs,
        _MAX_REPAIRS,
    )


# LLM: delivery_repair_block_response stops the loop once the model ignores required staged-repair actions multiple times.
# 函数用途: 模型连续忽略结构化阶段修复动作时，返回确定性阻断，避免在同一 no-progress 状态无限消耗轮次。
def delivery_repair_block_response(agent: object, runtime_params: object = _PARAMS_UNSET) -> ModelResponse | None:
    payload = _delivery_repair_payload(agent, runtime_params)
    if not payload:
        return None
    return ModelResponse(
        text=(
            "[DELIVERY_REQUIRED_REPAIR_BLOCKED] 结构化交付合同已经明确下一步必须先修阶段产物，"
            "但模型仍连续没有执行对应写入/构建动作，已停止本轮以避免继续空转。"
        ),
        backend=str(getattr(getattr(agent, "backend", None), "name", "") or ""),
        runtime_status="blocked",
        runtime_reason="DELIVERY_REQUIRED_REPAIR",
    )


# LLM: has_required_delivery_repair returns True only when closeout recovery actions require a write-first step.
# 函数用途: 让工具循环只在“必须先写/修/构建”的阶段收紧；普通 acceptance failed 不会触发这里。
def has_required_delivery_repair(agent: object, runtime_params: object = _PARAMS_UNSET) -> bool:
    return bool(_delivery_repair_payload(agent, runtime_params))


# LLM: is_delivery_repair_productive_call checks whether at least one tool call can satisfy the current staged-repair contract.
# 函数用途: 只要本轮工具调用里包含 builder 或明显的写入/执行动作，就视为在推进 staged repair，不做拦截。
def is_delivery_repair_productive_call(
    agent: object,
    calls: list[dict[str, object]],
    runtime_params: object = _PARAMS_UNSET,
) -> bool:
    payload = _delivery_repair_payload(agent, runtime_params)
    if not payload:
        return True
    required_actions = [item for item in payload.get("required_actions", []) if isinstance(item, dict)]
    action_tools = {
        str(item.get(key) or "").strip()
        for item in required_actions
        for key in ("builder_tool", "writer_tool")
    }
    action_tools.update(
        str(tool).strip()
        for item in required_actions
        for tool in item.get("write_tools", [])
        if isinstance(item.get("write_tools"), list)
    )
    strict_write_required = bool(payload.get("strict_write_required"))
    context = DeliveryRepairProductivityContext(
        productive_tools=productive_tools(payload, action_tools),
        inspection_only_tools=inspection_only_tools(required_actions, strict_write_required),
        strict_write_required=strict_write_required,
        required_actions=required_actions,
    )
    if any(_call_resets_declared_read_allowance(call, context) for call in calls):
        reset_declared_read_allowance(agent)
    if declared_repair_reads_exhausted(agent, payload, calls, required_actions):
        return False
    return any(_call_is_productive(call, context) for call in calls)


# LLM: _delivery_repair_payload scopes persisted closeout facts to the current run params when available.
# 函数用途: 真实工具循环传入 params 时，旧 closeout 必须匹配当前 delivery_contract 才能成为硬修复事实。
def _delivery_repair_payload(agent: object, params: object) -> dict[str, object]:
    if params is _PARAMS_UNSET:
        return delivery_repair_payload(agent)
    return delivery_repair_payload(
        agent,
        _current_delivery_contract(params),
        enforce_contract_scope=True,
    )


# LLM: _current_delivery_contract mirrors closeout contract lookup without reading rendered prompt text.
# 函数用途: 从 ToolLoopExecuteParams 或 task_attributes 中提取当前运行的机器交付合同。
def _current_delivery_contract(params: object) -> dict[str, object] | None:
    value = getattr(params, "delivery_contract", None)
    if isinstance(value, dict):
        return value
    attrs = getattr(params, "task_attributes", None)
    if isinstance(attrs, dict) and isinstance(attrs.get("delivery_contract"), dict):
        return attrs["delivery_contract"]
    return None


# LLM: _call_is_productive treats pure inspection loops as non-productive but leaves real data gathering or execution actions alone.
# 函数用途: 只有明确的只读检查工具才会被 staged repair guard 拦下；抓取数据、执行命令和构建产物都算推进。
def _call_is_productive(
    call: dict[str, object],
    context: DeliveryRepairProductivityContext,
) -> bool:
    tool = str(call.get("tool") or "").strip()
    if _is_declared_repair_target_read(call, context.required_actions):
        return True
    if _is_checkpoint_repair_read(call, context.required_actions):
        return True
    if _is_evidence_repair_gathering(call, context.required_actions):
        return True
    if _violates_declared_writer_tool(call, context.required_actions):
        return False
    if violates_evidence_repair_shape(
        call,
        context.required_actions,
        path_matches=same_path_ref,
        call_path=call_path,
    ):
        return False
    if _violates_non_empty_rows_repair(call, context.required_actions):
        return False
    if tool == "run_command":
        return _run_command_is_productive(call, context)
    if tool in context.productive_tools:
        return True
    if context.strict_write_required:
        return False
    return tool not in context.inspection_only_tools


# LLM: _violates_declared_writer_tool keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _violates_declared_writer_tool(call: dict[str, object], required_actions: list[dict[str, object]]) -> bool:
    tool = str(call.get("tool") or "").strip()
    path = call_path(call)
    if not tool or not path:
        return False
    for action in required_actions:
        writer_tool = str(action.get("writer_tool") or "").strip()
        allowed_tools = _declared_writer_tools(action, writer_tool)
        checkpoint_ref = str(action.get("checkpoint_ref") or "").strip()
        if allowed_tools and checkpoint_ref and tool not in allowed_tools and same_path_ref(path, checkpoint_ref):
            return True
    return False


# LLM: _declared_writer_tools lets one checkpoint have multiple structured writer tools.
# 函数用途: 将 writer_tool 和 write_tools 合并成允许集合，避免 API 来源 writer 被单一 writer_tool 误拦。
def _declared_writer_tools(action: dict[str, object], writer_tool: str) -> set[str]:
    tools = {writer_tool} if writer_tool else set()
    raw = action.get("write_tools")
    if isinstance(raw, list):
        tools.update(str(item).strip() for item in raw if str(item).strip())
    return tools


# LLM: STAGED_JSON_NO_ROWS repair requires actual structured rows, not just touching the checkpoint path.
# 函数用途: 对 write_non_empty_structured_rows 恢复动作做机器级校验，空 sheets/rows 不能算有效修复推进。
def _violates_non_empty_rows_repair(call: dict[str, object], required_actions: list[dict[str, object]]) -> bool:
    if str(call.get("tool") or "").strip() != "write_structured_json":
        return False
    path = call_path(call)
    if not path:
        return False
    for action in required_actions:
        if not _requires_non_empty_checkpoint_write(action):
            continue
        if same_path_ref(path, str(action.get("checkpoint_ref") or "")):
            return not _has_non_empty_structured_rows(call)
    return False


def _requires_non_empty_checkpoint_write(action: dict[str, object]) -> bool:
    recommended = str(action.get("recommended_action") or "")
    if recommended == "write_non_empty_structured_rows":
        return True
    return (
        recommended == "materialize_checkpoint"
        and str(action.get("checkpoint_materialization_mode") or "") == "source_evidence_first"
    )


# LLM: _has_non_empty_structured_rows checks table payload shape without reading prose.
# 函数用途: 在 write_structured_json 参数中识别 rows/sheets/data 的非空行，支持分批写入和嵌套 data 包装。
def _has_non_empty_structured_rows(value: object) -> bool:
    if isinstance(value, list):
        return bool(value)
    if not isinstance(value, dict):
        return False
    generated_rows = value.get("generated_rows")
    if isinstance(generated_rows, list):
        return bool(generated_rows)
    if isinstance(generated_rows, dict) and _positive_generated_row_count(generated_rows):
        return True
    rows = value.get("rows")
    if isinstance(rows, list) and rows:
        return True
    sheets = value.get("sheets")
    if isinstance(sheets, list) and any(_has_non_empty_structured_rows(item) for item in sheets):
        return True
    data = value.get("data")
    if isinstance(data, (dict, list)):
        return _has_non_empty_structured_rows(data)
    return False


def _positive_generated_row_count(value: dict[str, object]) -> bool:
    try:
        return int(value.get("count") or 0) > 0
    except (TypeError, ValueError):
        return False


# LLM: _is_checkpoint_repair_read allows one structured checkpoint inspection before rewriting it.
# 函数用途: 修复结构化 JSON checkpoint 时，读取同一 checkpoint_ref 是获取机器数据，不是验收产物空转。
def _is_checkpoint_repair_read(call: dict[str, object], required_actions: list[dict[str, object]]) -> bool:
    if str(call.get("tool") or "").strip() != "read_file":
        return False
    path = call_path(call)
    if not path:
        return False
    repair_actions = {
        "repair_collection_item_values",
        "repair_evidence_refs",
        "repair_structured_checkpoint_json",
        "write_non_empty_structured_rows",
    }
    return any(
        str(action.get("recommended_action") or "") in repair_actions
        and same_path_ref(path, str(action.get("checkpoint_ref") or ""))
        for action in required_actions
    )


# LLM: declared repair targets create a bounded read allowance before a mutation.
# 函数用途: 只允许读取 closeout 机器字段列出的 repair_targets/checkpoint_ref/artifact_path，避免修复前检查退化成随意探索。
def _is_declared_repair_target_read(call: dict[str, object], required_actions: list[dict[str, object]]) -> bool:
    if str(call.get("tool") or "").strip() != "read_file":
        return False
    path = call_path(call)
    if not path:
        return False
    return any(same_path_ref(path, target) for target in repair_target_values(required_actions))


# LLM: _is_evidence_repair_gathering allows source collection when claims/source_refs are the failed contract.
# 函数用途: 证据修复可以先调用抓取/检索工具补 source_refs，但仍由后续 verifier 要求写入结构化 claims。
def _is_evidence_repair_gathering(call: dict[str, object], required_actions: list[dict[str, object]]) -> bool:
    tool = str(call.get("tool") or "").strip()
    if tool not in EVIDENCE_GATHERING_TOOL_NAMES:
        return False
    return any(
        str(action.get("recommended_action") or "") == "repair_evidence_refs"
        and bool(action.get("required_fields"))
        for action in required_actions
    )


def _call_resets_declared_read_allowance(
    call: dict[str, object],
    context: DeliveryRepairProductivityContext,
) -> bool:
    tool = str(call.get("tool") or "").strip()
    if tool in context.productive_tools and tool not in context.inspection_only_tools and tool not in EVIDENCE_GATHERING_TOOL_NAMES:
        return True
    if tool != "run_command":
        return False
    return _run_command_is_productive(call, context)


# LLM: _run_command_is_productive keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _run_command_is_productive(call: dict[str, object], context: DeliveryRepairProductivityContext) -> bool:
    command = call_command(call)
    if not command:
        return False
    if context.strict_write_required:
        return False
    if command_has_local_mutation(command):
        writer_refs = _declared_writer_refs(context.required_actions)
        if writer_refs:
            return any(command_references_ref(command, ref) for ref in writer_refs)
        return True
    if ">" in command:
        return True
    return not any(command.startswith(prefix) for prefix in _RUN_COMMAND_INSPECTION_PREFIXES)


# LLM: _declared_writer_refs keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _declared_writer_refs(required_actions: list[dict[str, object]]) -> list[str]:
    return [
        str(action.get("checkpoint_ref") or "").strip()
        for action in required_actions
        if str(action.get("writer_tool") or "").strip() and str(action.get("checkpoint_ref") or "").strip()
    ]
