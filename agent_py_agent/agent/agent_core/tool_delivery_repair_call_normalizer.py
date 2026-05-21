# LLM: Delivery repair call normalization applies machine recovery contracts before tool execution.
# 模块用途: 在工具执行前修正可安全归一化的 staged-repair 调用，避免模型遗漏参数导致覆盖已有产物。

from __future__ import annotations

from .tool_delivery_repair_paths import call_path, same_path_ref
from .tool_delivery_repair_payload import delivery_repair_payload
from .tool_delivery_repair_productivity import (
    EVIDENCE_GATHERING_TOOL_NAMES,
    INSPECTION_ONLY_TOOL_NAMES,
)

_PARAMS_UNSET = object()


# LLM: normalize_delivery_repair_calls returns executor-ready calls derived from structured closeout facts.
# 函数用途: 只根据 closeout recovery_actions 的机器字段修正工具参数，不读取用户 prompt 或自然语言报告正文。
def normalize_delivery_repair_calls(
    agent: object,
    calls: list[dict[str, object]],
    runtime_params: object = _PARAMS_UNSET,
) -> list[dict[str, object]]:
    payload = _delivery_repair_payload(agent, runtime_params)
    if not payload:
        return calls
    required_actions = [item for item in payload.get("required_actions", []) if isinstance(item, dict)]
    if not required_actions:
        return calls
    normalized = [_normalize_call(call, required_actions) for call in calls]
    if builder_call := _deterministic_builder_call_for_inspection(normalized, payload, required_actions):
        return [builder_call]
    return normalized


# LLM: _delivery_repair_payload scopes closeout facts to the active delivery contract when params are available.
# 函数用途: 复用 delivery repair 的范围匹配规则，避免旧 closeout 影响后续不相关任务。
def _delivery_repair_payload(agent: object, params: object) -> dict[str, object]:
    if params is _PARAMS_UNSET:
        return delivery_repair_payload(agent)
    return delivery_repair_payload(
        agent,
        _current_delivery_contract(params),
        enforce_contract_scope=True,
    )


# LLM: _current_delivery_contract extracts the active machine contract from runtime params.
# 函数用途: 从 ToolLoopExecuteParams 或 task_attributes 读取结构化 delivery_contract。
def _current_delivery_contract(params: object) -> dict[str, object] | None:
    value = getattr(params, "delivery_contract", None)
    if isinstance(value, dict):
        return value
    attrs = getattr(params, "task_attributes", None)
    if isinstance(attrs, dict) and isinstance(attrs.get("delivery_contract"), dict):
        return attrs["delivery_contract"]
    return None


# LLM: _normalize_call keeps non-matching tool calls unchanged.
# 函数用途: 只对匹配 repair_evidence_refs 的 write_structured_json 调用设置 merge_existing。
def _normalize_call(call: dict[str, object], required_actions: list[dict[str, object]]) -> dict[str, object]:
    if not _matches_evidence_metadata_merge(call, required_actions):
        return call
    normalized = dict(call)
    normalized["merge_existing"] = True
    return normalized


# LLM: _deterministic_builder_call_for_inspection turns inspection loops into the required builder call.
# 函数用途: 当合同已声明 source/output/builder 且本轮只有检查类调用时，直接执行 required_tool_calls 中的 builder。
def _deterministic_builder_call_for_inspection(
    calls: list[dict[str, object]],
    payload: dict[str, object],
    required_actions: list[dict[str, object]],
) -> dict[str, object]:
    if not calls or not _all_calls_are_inspection(calls):
        return {}
    builder_tools = _ready_builder_tools(required_actions)
    if not builder_tools:
        return {}
    for call in payload.get("required_tool_calls", []):
        if isinstance(call, dict) and str(call.get("tool") or "").strip() in builder_tools:
            return dict(call)
    return {}


# LLM: _all_calls_are_inspection classifies calls by tool name, not by assistant prose.
# 函数用途: 只有模型本轮完全没有写入/构建动作时，才允许系统替换成 deterministic builder 调用。
def _all_calls_are_inspection(calls: list[dict[str, object]]) -> bool:
    inspection_tools = INSPECTION_ONLY_TOOL_NAMES | EVIDENCE_GATHERING_TOOL_NAMES
    return all(str(call.get("tool") or "").strip() in inspection_tools for call in calls)


# LLM: _ready_builder_tools reads builder tools from invoke_builder_tool recovery actions.
# 函数用途: 从 required_actions 的机器字段提取可自动执行的 builder_tool 集合。
def _ready_builder_tools(required_actions: list[dict[str, object]]) -> set[str]:
    return {
        tool
        for action in required_actions
        if str(action.get("recommended_action") or "") == "invoke_builder_tool"
        for tool in [str(action.get("builder_tool") or "").strip()]
        if tool
    }


# LLM: _matches_evidence_metadata_merge checks structured tool/path/action fields only.
# 函数用途: 判断当前调用是否是对指定 checkpoint_ref 的证据元数据补写。
def _matches_evidence_metadata_merge(
    call: dict[str, object],
    required_actions: list[dict[str, object]],
) -> bool:
    if str(call.get("tool") or "").strip() != "write_structured_json":
        return False
    path = call_path(call)
    if not path:
        return False
    return any(_is_matching_evidence_action(action, path) for action in required_actions)


# LLM: _is_matching_evidence_action matches one recovery action without reading prose.
# 函数用途: 用 recommended_action、writer_tool 和 checkpoint_ref 判定是否应做 merge 归一化。
def _is_matching_evidence_action(action: dict[str, object], path: str) -> bool:
    return (
        str(action.get("recommended_action") or "") == "repair_evidence_refs"
        and str(action.get("writer_tool") or "") == "write_structured_json"
        and same_path_ref(path, str(action.get("checkpoint_ref") or ""))
    )


__all__ = ["normalize_delivery_repair_calls"]
