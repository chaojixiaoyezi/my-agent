# LLM: Delivery repair call normalization applies machine recovery contracts before tool execution.
# 模块用途: 在工具执行前修正可安全归一化的 staged-repair 调用，避免模型遗漏参数导致覆盖已有产物。

from __future__ import annotations

import json
import shlex

from .tool_delivery_repair_paths import call_command, call_path, same_path_ref
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
    if writer_call := _deterministic_source_writer_call_for_inspection(normalized, payload, required_actions):
        return [writer_call]
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
# 函数用途: 只按 required_actions 机器字段修正工具调用，不读取普通自然语言作为事实。
def _normalize_call(call: dict[str, object], required_actions: list[dict[str, object]]) -> dict[str, object]:
    if normalized := _normalize_write_file_json_checkpoint(call, required_actions):
        return normalized
    if not _matches_evidence_metadata_merge(call, required_actions):
        return call
    normalized = dict(call)
    normalized["merge_existing"] = True
    return normalized


# LLM: write_file JSON checkpoint repair is upgraded to the structured writer declared by the contract.
# 函数用途: 当模型把结构化 checkpoint 作为 JSON 字符串写入时，在执行前转为 write_structured_json。
def _normalize_write_file_json_checkpoint(
    call: dict[str, object],
    required_actions: list[dict[str, object]],
) -> dict[str, object]:
    if str(call.get("tool") or "").strip() != "write_file":
        return {}
    path = call_path(call)
    if not path or not _allows_structured_checkpoint_write(path, required_actions):
        return {}
    data = _json_content(call)
    if data is _JSON_UNSET:
        return {}
    normalized: dict[str, object] = {
        "tool": "write_structured_json",
        "path": path,
        "data": data,
    }
    if isinstance(call.get("merge_existing"), bool):
        normalized["merge_existing"] = bool(call["merge_existing"])
    return normalized


# LLM: _allows_structured_checkpoint_write reads only writer_tool/write_tools/checkpoint_ref fields.
# 函数用途: 确认当前路径是合同声明 checkpoint，且 write_structured_json 是允许 writer。
def _allows_structured_checkpoint_write(path: str, required_actions: list[dict[str, object]]) -> bool:
    return any(
        _declared_writer_allows_structured_json(action)
        and same_path_ref(path, str(action.get("checkpoint_ref") or ""))
        for action in required_actions
    )


def _declared_writer_allows_structured_json(action: dict[str, object]) -> bool:
    tools = {str(action.get("writer_tool") or "").strip()}
    raw_tools = action.get("write_tools")
    if isinstance(raw_tools, list):
        tools.update(str(tool).strip() for tool in raw_tools if str(tool).strip())
    return "write_structured_json" in tools


_JSON_UNSET = object()


# LLM: _json_content parses only explicit write_file content fields.
# 函数用途: 只有 content/text/body 是有效 JSON 对象或数组时才做结构化写入转换。
def _json_content(call: dict[str, object]) -> object:
    raw = _raw_file_content(call)
    if not isinstance(raw, str):
        return _JSON_UNSET
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _JSON_UNSET
    return data if isinstance(data, (dict, list)) else _JSON_UNSET


def _raw_file_content(call: dict[str, object]) -> object:
    for key in ("content", "text", "body"):
        if key in call:
            return call.get(key)
    return None


# LLM: _deterministic_builder_call_for_inspection turns inspection loops into the required builder call.
# 函数用途: 当合同已声明 source/output/builder 且本轮只有检查类调用时，直接执行 required_tool_calls 中的 builder。
def _deterministic_builder_call_for_inspection(
    calls: list[dict[str, object]],
    payload: dict[str, object],
    required_actions: list[dict[str, object]],
) -> dict[str, object]:
    if not calls or not _all_calls_are_inspection_or_setup(calls):
        return {}
    builder_tools = _ready_builder_tools(required_actions)
    if not builder_tools:
        return {}
    for call in payload.get("required_tool_calls", []):
        if isinstance(call, dict) and str(call.get("tool") or "").strip() in builder_tools:
            return dict(call)
    return {}


# LLM: source-backed required writers replace inspection loops once source artifacts already exist.
# 函数用途: 资料来源已经外置为 artifact 时，继续 read/list 不算推进；直接执行 required_tool_calls 里的 writer。
def _deterministic_source_writer_call_for_inspection(
    calls: list[dict[str, object]],
    payload: dict[str, object],
    required_actions: list[dict[str, object]],
) -> dict[str, object]:
    if not calls or not _all_calls_are_inspection_or_setup(calls):
        return {}
    writer_tools = _ready_writer_tools(required_actions)
    if not writer_tools:
        return {}
    for call in payload.get("required_tool_calls", []):
        if not isinstance(call, dict):
            continue
        tool = str(call.get("tool") or "").strip()
        if tool in writer_tools and _has_source_backing(call):
            return dict(call)
    return {}


# LLM: _all_calls_are_inspection classifies calls by tool name, not by assistant prose.
# 函数用途: 只有模型本轮完全没有写入/构建动作时，才允许系统替换成 deterministic builder 调用。
def _all_calls_are_inspection_or_setup(calls: list[dict[str, object]]) -> bool:
    inspection_tools = INSPECTION_ONLY_TOOL_NAMES | EVIDENCE_GATHERING_TOOL_NAMES
    return all(
        str(call.get("tool") or "").strip() in inspection_tools
        or _is_directory_setup_command(call)
        for call in calls
    )


def _is_directory_setup_command(call: dict[str, object]) -> bool:
    if str(call.get("tool") or "").strip() != "run_command":
        return False
    command = call_command(call)
    if not command:
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if not parts or parts[0] != "mkdir":
        return False
    return bool(parts[1:]) and all(part == "-p" or not part.startswith("-") for part in parts[1:])


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


def _ready_writer_tools(required_actions: list[dict[str, object]]) -> set[str]:
    return {
        tool
        for action in required_actions
        for tool in _declared_writer_tool_values(action)
    }


def _declared_writer_tool_values(action: dict[str, object]) -> set[str]:
    values = {str(action.get("writer_tool") or "").strip()}
    raw = action.get("write_tools")
    if isinstance(raw, list):
        values.update(str(item).strip() for item in raw if str(item).strip())
    return {value for value in values if value}


def _has_source_backing(call: dict[str, object]) -> bool:
    artifacts = call.get("source_artifacts")
    if isinstance(artifacts, list) and any(isinstance(item, dict) for item in artifacts):
        return True
    return bool(str(call.get("source_ref") or "").strip() and str(call.get("content") or "").strip())


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
