
from __future__ import annotations

"""parsing and execution helpers for ToolRegistry.

ToolRegistry 本身保持'服务台'职责；这里集中放工具调用解析、授权检查和异常格式化，
避免注册表类继续变厚。
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..action_protocol import (
    RunScope,
    ToolCallEnvelope,
)
from ..contracts.gates.models import GateDecision
from .content_transport_policy import (
    RECOMMENDED_WRITE_CHUNK_CHARS,
    RECOVERY_WRITE_CHUNK_CHARS,
)
from .models import BaseTool, ToolExecutionResult, ToolRuntimeSnapshot
from .parser import parse_xmlish_tool_calls
from .registry_auth import (
    ToolAuthContext,
    allowed_tool_set,
    registry_auth_error,
    registry_auth_error_code,
)
from .registry_envelopes import (
    attach_result_envelope,
    payload_from_tool_call_envelope,
    payloads_to_tool_envelopes,
    tool_call_envelope_from_execution_payload,
)
from .registry_file_write_blocks import (
    malformed_file_write_raw_block_calls,
    parse_write_file_raw_blocks,
    write_file_raw_block_ranges,
)
from .registry_invoke import RegistryToolInvokeRequest, invoke_registry_tool
from .registry_payload_normalize import (
    ToolPayloadNormalizeLimits,
    normalize_tool_payload,
    parse_error_payload,
    parse_tool_block_payload,
)
from .registry_payload_normalize import (
    tool_name as normalize_tool_name,
)
from .registry_resilience import ResilientToolInvokeRequest, resilient_tool_invoke
from .registry_runtime_gate_pipeline import tool_call_gate_decision
from .tool_input_completion import ToolInputCompletionContext
from .tool_operation_coordinator import (
    ToolOperationExecutionRequest,
    execute_tool_operation,
)
from .tool_spec_schema import NormalizedToolPayload, normalize_tool_payload_for_spec

# 措辞同时适用文本协议与原生 tool_use：文本协议下重发 [TOOL_CALL]{json}[/TOOL_CALL] 块，
# 原生协议下直接发起结构化工具调用。绝不能只教文本 [TOOL_CALL] 写法——native 模型偶尔
# 因训练惯性写出残缺的文本 [TOOL_CALL] 才走到这条解析失败提示，再叫它写 [TOOL_CALL]
# 文本只会把它越带越偏（治根护栏：原生协议禁止退回文本协议）。
_PARSE_RETRY_HINT = (
    "请重新发起一个标准工具调用：参数放在一个 JSON 对象里，不要在 JSON 外追加正文，"
    "也不要混用未闭合的 XML 标签。"
    "若本会话用文本协议，写成 [TOOL_CALL] 后跟该 JSON 对象、再用 [/TOOL_CALL] 闭合；"
    "若已启用原生工具调用，直接用结构化工具调用发起，不要把调用写成正文里的文本块。"
)
_PROTECTED_MARKER_PAIRS = (
    ("[SUBAGENT_RESULT]", "[/SUBAGENT_RESULT]"),
    ("[PARENT_PLANNER_RESULT]", "[/PARENT_PLANNER_RESULT]"),
)
_TRUNCATED_PAYLOAD_HINT = (
    "如果上一轮工具参数太长导致截断，请缩短 goal/plan/acceptance_checks，"
    "只保留关键路径、必需文件名和硬约束；长说明交给后续 runner 自己展开。"
)
_TRUNCATED_WRITE_HINT = (
    "如果上一轮是 write_file 且 content 太长，不要重复输出完整 content；"
    "下一轮只输出 1 个完整机器写入块，优先用独立成行的 WRITE_FILE_RAW mode=\"append\" 原文块；"
    "如果继续用 JSON write_file，先用 mode=\"overwrite\" 写第一小块，再用同一路径 mode=\"append\" 逐块追加；"
    f"正常分块时单次 content 建议 {RECOMMENDED_WRITE_CHUNK_CHARS} 字符。"
    "如果已经连续解析失败，下一轮只发 1 个 write_file 工具调用，"
    f"content 降到不超过 {RECOVERY_WRITE_CHUNK_CHARS} 字符，等这个调用完整发出、拿到工具结果后再继续下一块。"
    "只有内容能完整闭合时才使用 WRITE_FILE_RAW 原文块；不要把 WRITE_FILE_RAW 当 JSON tool 名。"
)
_MALFORMED_OPENERS = ("[TOOL_CALL",)
_VALID_OPENERS = ("[TOOL_CALL]",)
_START_MARKERS = ("[TOOL_CALL]",)
_END_MARKERS = ("[/TOOL_CALL]",)
_WRITE_FILE_TOOL_RE = re.compile(r'"tool"\s*:\s*"write_file"')
_WRITE_FILE_PATH_RE = re.compile(r'"(?:path|target_path)"\s*:\s*"(?P<path>(?:\\.|[^"\\]){0,240})"')
_WRITE_FILE_CONTENT_RE = re.compile(r'"content"\s*:')


@dataclass(frozen=True)
class ExecuteRegistryCallParams:
    payload: object
    tools: dict[str, BaseTool]
    workspace_root: Path
    workspace_roots: list[Path] | None
    default_hidden_tool_names: set[str] | None = None
    path_access_mode: str = "normal"
    path_dangerous_roots: list[str] | None = None
    owner_scope_root: str = ""
    allowed_tools: list[str] | None = None
    disabled_tools: list[str] | None = None
    write_boundary: dict[str, object] | None = None
    payload_limits: ToolPayloadNormalizeLimits | None = None
    runtime_guard_policy: object | None = None
    runtime_snapshot: ToolRuntimeSnapshot | None = None
    owner_type: str = "main_agent"
    operation_store: object | None = None
    operation_store_required: bool = True
    operation_owner_id: str = ""


@dataclass
class _ToolBlockParseContext:
    calls: list[tuple[int, dict[str, Any]]]
    scan_text: str
    payload_limits: ToolPayloadNormalizeLimits | None


def parse_registry_tool_calls(
    text: str,
    *,
    payload_limits: ToolPayloadNormalizeLimits | None = None,
) -> list[dict[str, Any]]:

    scan_text = mask_protected_control_ranges(text)
    raw_ranges = _top_level_raw_block_ranges(scan_text)
    calls = _top_level_raw_block_calls(scan_text)
    scan_without_raw = _mask_ranges(scan_text, raw_ranges)
    calls.extend(_parse_tool_block_calls(scan_without_raw, payload_limits=payload_limits))
    top_level_text = _mask_ranges(scan_without_raw, _tool_block_ranges(scan_without_raw))
    calls.extend(malformed_tool_marker_calls(top_level_text))
    calls.extend(malformed_file_write_raw_block_calls(top_level_text))
    calls.extend(parse_xmlish_tool_calls(top_level_text))
    calls.sort(key=lambda item: item[0])
    return [payload for _, payload in calls]


def mask_protected_control_ranges(text: str) -> str:
    ranges = _protected_control_ranges(text)
    if not ranges:
        return text
    chars = list(text)
    for start, end in ranges:
        for index in range(start, end):
            chars[index] = " "
    return "".join(chars)


def _protected_control_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for start_marker, end_marker in _PROTECTED_MARKER_PAIRS:
        ranges.extend(_marker_ranges(text, start_marker, end_marker))
    return sorted(ranges)


def _marker_ranges(text: str, start_marker: str, end_marker: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    cursor = 0
    while True:
        start = text.find(start_marker, cursor)
        if start == -1:
            return ranges
        body_start = start + len(start_marker)
        end = text.find(end_marker, body_start)
        if end == -1:
            ranges.append((start, len(text)))
            return ranges
        ranges.append((start, end + len(end_marker)))
        cursor = end + len(end_marker)


def next_tool_block_start(text: str, cursor: int) -> tuple[int, str] | None:
    starts = [
        (pos, marker)
        for marker in _START_MARKERS
        for pos in [_next_protocol_marker_pos(text, marker, cursor)]
        if pos != -1
    ]
    return min(starts, key=lambda item: item[0]) if starts else None


def next_tool_block_end(text: str, start_at: int) -> tuple[int, str] | None:
    ends = [
        (pos, marker)
        for marker in _END_MARKERS
        for pos in [_next_protocol_end_marker_pos(text, marker, start_at)]
        if pos != -1
    ]
    return min(ends, key=lambda item: item[0]) if ends else None


def _next_protocol_marker_pos(text: str, marker: str, cursor: int) -> int:
    while True:
        pos = text.find(marker, cursor)
        if pos == -1:
            return -1
        body_start = pos + len(marker)
        if _marker_starts_protocol_line(text, pos) or _inline_tool_start_marker_valid(text, body_start):
            return pos
        cursor = body_start


def _inline_tool_start_marker_valid(text: str, body_start: int) -> bool:
    # 对称于 _inline_tool_end_marker_valid:开始标记 [TOOL_CALL] 即使不在行首(前面有正文),
    # 只要其后内容像工具调用(strip 后以 { 开头的 JSON)就识别——否则模型把工具调用接在
    # 正文同一行(如 "...印证。[TOOL_CALL]\n{json}\n[/TOOL_CALL]")时整块被静默丢弃,
    # 工具不执行也不报错(minimax-M3 实测暴露:tool_rounds=0、无 error_code、run 空转结束)。
    # 用"以 { 开头"而非"能解析出合法 payload"做判据:这样即使内容坏了(如一个块塞多个
    # JSON、结束标记残缺),也会被识别并走正常块解析报 TOOL_CALL_JSON_INVALID 引导模型修正,
    # 而不是静默丢弃整块。正文偶然提到 [TOOL_CALL] 后面不是 { 不会误触发。
    end = text.find("[/TOOL_CALL]", body_start)
    raw = text[body_start : end if end != -1 else len(text)].strip().strip("`")
    return raw.startswith("{")


def _next_protocol_end_marker_pos(text: str, marker: str, cursor: int) -> int:
    while True:
        pos = text.find(marker, cursor)
        if pos == -1:
            return -1
        if _marker_starts_protocol_line(text, pos) or _inline_tool_end_marker_valid(text, cursor, pos):
            return pos
        cursor = pos + len(marker)


def _inline_tool_end_marker_valid(text: str, body_start: int, marker_pos: int) -> bool:
    raw = text[body_start:marker_pos].strip().strip("`")
    if not raw:
        return False
    payload = parse_tool_block_payload(raw)
    return payload.get("tool") != "__parse_error__"


def _marker_starts_protocol_line(text: str, pos: int) -> bool:
    line_start = text.rfind("\n", 0, pos) + 1
    return not text[line_start:pos].strip()


def _top_level_raw_block_ranges(scan_text: str) -> list[tuple[int, int]]:
    tool_ranges = _tool_block_ranges(scan_text)
    return [
        raw_range
        for raw_range in write_file_raw_block_ranges(scan_text)
        if not _position_in_ranges(raw_range[0], tool_ranges)
    ]


def _top_level_raw_block_calls(scan_text: str) -> list[tuple[int, dict[str, Any]]]:
    tool_ranges = _tool_block_ranges(scan_text)
    return [
        item
        for item in parse_write_file_raw_blocks(scan_text)
        if not _position_in_ranges(item[0], tool_ranges)
    ]


def _parse_tool_block_calls(
    scan_text: str,
    *,
    payload_limits: ToolPayloadNormalizeLimits | None,
) -> list[tuple[int, dict[str, Any]]]:
    calls: list[tuple[int, dict[str, Any]]] = []
    context = _ToolBlockParseContext(calls=calls, scan_text=scan_text, payload_limits=payload_limits)
    cursor = 0
    while True:
        start_info = next_tool_block_start(scan_text, cursor)
        if start_info is None:
            break
        start, marker_start = start_info
        body_start = start + len(marker_start)
        end_info = next_tool_block_end(scan_text, body_start)
        if end_info is None:
            _append_unclosed_tool_block(context, start, body_start)
            break
        cursor = _append_closed_tool_block(context, start, body_start, end_info)
    return calls


def _tool_block_ranges(scan_text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    cursor = 0
    while True:
        start_info = next_tool_block_start(scan_text, cursor)
        if start_info is None:
            return ranges
        start, marker_start = start_info
        body_start = start + len(marker_start)
        end_info = next_tool_block_end(scan_text, body_start)
        if end_info is None:
            ranges.append((start, len(scan_text)))
            return ranges
        end, marker_end = end_info
        cursor = end + len(marker_end)
        ranges.append((start, cursor))


def _mask_ranges(text: str, ranges: list[tuple[int, int]]) -> str:
    if not ranges:
        return text
    chars = list(text)
    for start, end in ranges:
        for index in range(start, end):
            chars[index] = " "
    return "".join(chars)


def _position_in_ranges(pos: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= pos < end for start, end in ranges)


def malformed_tool_marker_calls(text: str) -> list[tuple[int, dict[str, Any]]]:
    calls = [
        (
            pos,
            parse_error_payload(
                "工具调用开始标记格式错误，缺少 ]",
                _malformed_marker_raw(text, pos),
                error_code="TOOL_CALL_MARKER_MALFORMED",
            ),
        )
        for opener in _MALFORMED_OPENERS
        for pos in _malformed_opener_positions(text, opener)
    ]
    return sorted(calls, key=lambda item: item[0])


def _malformed_opener_positions(text: str, opener: str) -> list[int]:
    positions: list[int] = []
    cursor = 0
    while True:
        pos = text.find(opener, cursor)
        if pos == -1:
            return positions
        cursor = pos + len(opener)
        if _is_malformed_protocol_opener(text, pos, opener):
            positions.append(pos)


def _is_malformed_protocol_opener(text: str, pos: int, opener: str) -> bool:
    return (
        not _is_valid_opener(text, pos)
        and _looks_like_line_start_marker(text, pos, opener)
    )


def _is_valid_opener(text: str, pos: int) -> bool:
    return any(text.startswith(opener, pos) for opener in _VALID_OPENERS)


def _looks_like_line_start_marker(text: str, pos: int, opener: str) -> bool:
    line_start = text.rfind("\n", 0, pos) + 1
    if text[line_start:pos].strip():
        return False
    next_char = text[pos + len(opener) : pos + len(opener) + 1]
    return not next_char or next_char.isspace() or next_char in {"{", ":"}


def _malformed_marker_raw(text: str, pos: int) -> str:
    end_info = next_tool_block_end(text, pos)
    if end_info is None:
        return text[pos:].strip()
    end, marker = end_info
    return text[pos : end + len(marker)].strip()


def _append_unclosed_tool_block(
    context: _ToolBlockParseContext,
    start: int,
    body_start: int,
) -> None:
    raw = context.scan_text[body_start:].strip().strip("`")
    payload = parse_tool_block_payload(raw, limits=context.payload_limits)
    error_payload = _unclosed_tool_call_parse_error(raw, context.payload_limits)
    context.calls.append((
        start,
        error_payload
        if payload.get("tool") == "__parse_error__"
        else payload,
    ))


def _append_closed_tool_block(
    context: _ToolBlockParseContext,
    start: int,
    body_start: int,
    end_info: tuple[int, str],
) -> int:
    end, marker_end = end_info
    raw = context.scan_text[body_start:end].strip().strip("`")
    payload = parse_tool_block_payload(raw, limits=context.payload_limits)
    nested_start_info = next_tool_block_start(context.scan_text, body_start)
    if payload.get("tool") == "__parse_error__" and nested_start_info and nested_start_info[0] < end:
        nested_start = nested_start_info[0]
        malformed_raw = context.scan_text[body_start:nested_start].strip().strip("`")
        context.calls.append((
            start,
            _unclosed_tool_call_parse_error(malformed_raw, context.payload_limits),
        ))
        return nested_start
    context.calls.append((start, payload))
    return end + len(marker_end)


def parse_registry_tool_call_envelopes(
    text: str,
    *,
    scope: RunScope | None = None,
    source: str = "text_protocol",
) -> list[ToolCallEnvelope]:
    return payloads_to_tool_envelopes(
        parse_registry_tool_calls(text),
        scope=scope,
        source=source,
    )


# LLM: 工具调用先按 owner/allowlist 鉴权，再核对同一 run 快照，最后才进入参数、副作用和实现执行。
# 函数用途: 作为所有文本/native 工具调用的唯一解析、授权、可用性和运行门入口。
def execute_registry_call(call: ExecuteRegistryCallParams) -> ToolExecutionResult:
    envelope = tool_call_envelope_from_execution_payload(call.payload)
    if isinstance(envelope, ToolExecutionResult):
        return envelope
    normalized_payload = _normalized_payload_or_error(call, envelope)
    if isinstance(normalized_payload, ToolExecutionResult):
        return normalized_payload
    wrapped_error = _same_name_wrapper_error(normalized_payload, envelope)
    if wrapped_error:
        return wrapped_error
    try:
        tool_name = normalize_tool_name(normalized_payload.get("tool"), limits=call.payload_limits)
    except ValueError as exc:
        # tool 名缺失/类型错/过长/含控制字符(native 下空 name 也会到这) → 调用 payload 结构错，
        # 给精确码而非无码兜底成 UNKNOWN_ERROR(否则模型被告知"放弃"而非"重构一个完整调用")。
        return attach_result_envelope(
            ToolExecutionResult("unknown", False, str(exc), error_code="TOOL_CALL_PAYLOAD_INVALID"), envelope
        )
    auth_error = _registry_auth_error(tool_name, call)
    if auth_error:
        code = _registry_auth_error_code(tool_name, call)
        output = auth_error if not code else f"{code}: {auth_error}"
        return attach_result_envelope(ToolExecutionResult(tool_name, False, output, error_code=code), envelope)
    snapshot_error = _runtime_snapshot_unavailable(tool_name, call, envelope)
    if snapshot_error is not None:
        return snapshot_error
    normalized_input = _normalized_tool_input_or_error(
        normalized_payload,
        call.tools.get(tool_name),
        envelope,
        call,
    )
    if isinstance(normalized_input, ToolExecutionResult):
        return normalized_input
    normalized_payload = normalized_input.payload
    gate_decision = tool_call_gate_decision(
        normalized_payload,
        call,
        envelope,
    )
    if not gate_decision.allowed:
        result = runtime_gate_block_result(normalized_payload, gate_decision, envelope)
        attach_input_coercions(result, normalized_input)
        attach_input_sources(result, normalized_input)
        return result
    result = _execute_allowed_registry_call(
        call,
        envelope,
        tool_name,
        normalized_payload,
        gate_decision,
    )
    attach_runtime_gate(result, gate_decision)
    attach_input_coercions(result, normalized_input)
    attach_input_sources(result, normalized_input)
    return result


def _execute_allowed_registry_call(
    call: ExecuteRegistryCallParams,
    envelope: ToolCallEnvelope | None,
    tool_name: str,
    payload: dict[str, Any],
    gate_decision: GateDecision,
) -> ToolExecutionResult:
    tool = call.tools[tool_name]
    spec = tool.spec
    spec_effect = str(spec.effect or "").strip().lower()
    effective_effect = str(
        gate_decision.evidence.get("tool_execution_action") or ""
    ).strip().lower()

    def invoke() -> ToolExecutionResult:
        return _invoke_registry_with_envelope(
            call,
            envelope,
            tool_name,
            payload,
        )

    if not (
        {spec_effect, effective_effect}
        & {"mutating", "dangerous"}
    ):
        return invoke()
    if str(spec.idempotency_scope or "") not in {"operation", "business"}:
        return attach_result_envelope(
            ToolExecutionResult(
                tool_name,
                False,
                "side-effect tool is missing a valid idempotency_scope",
                error_code="TOOL_MANIFEST_IDEMPOTENCY_POLICY_MISSING",
            ),
            envelope,
        )
    scope = envelope.scope if envelope is not None else RunScope()
    boundary = call.write_boundary if isinstance(call.write_boundary, dict) else {}
    operation_id = str(
        (envelope.operation_id if envelope is not None else "")
        or gate_decision.evidence.get("operation_id")
        or ""
    )
    run_id = str(
        scope.run_id
        or scope.request_id
        or boundary.get("run_id")
        or "unscoped"
    )
    task_id = str(
        scope.task_id
        or boundary.get("task_id")
        or run_id
    )
    result = execute_tool_operation(
        ToolOperationExecutionRequest(
            store=call.operation_store,
            store_required=call.operation_store_required,
            owner_id=str(
                call.operation_owner_id
                or scope.owner_id
                or "local/main"
            ),
            run_id=run_id,
            task_id=task_id,
            operation_id=operation_id,
            tool_name=tool_name,
            args_hash=str(gate_decision.evidence.get("args_hash") or ""),
            idempotency_key=str(
                (envelope.idempotency_key if envelope is not None else "")
                or gate_decision.evidence.get("idempotency_key")
                or ""
            ),
            idempotency_scope=str(spec.idempotency_scope or ""),
            idempotency_namespace=tool_name,
            timeout_seconds=int(spec.timeout_seconds or 0),
            invoke=invoke,
        )
    )
    return attach_result_envelope(result, envelope)


def runtime_gate_block_result(
    payload: dict[str, Any],
    decision: GateDecision,
    envelope: ToolCallEnvelope | None,
) -> ToolExecutionResult:
    result = ToolExecutionResult(
        str(decision.evidence.get("tool_name") or payload.get("tool") or "unknown"),
        False,
        _gate_output(decision),
        error_code=decision.finding_codes[0] if decision.finding_codes else "RUNTIME_GATE_DENIED",
    )
    result = attach_result_envelope(result, envelope)
    attach_runtime_gate(result, decision)
    return result


def attach_runtime_gate(result: ToolExecutionResult, decision: GateDecision) -> None:
    envelope = dict(result.result_envelope or {})
    envelope["runtime_gate"] = decision.to_dict()
    result.result_envelope = envelope


# LLM: 类型纠正审计只写路径和前后类型，绝不能保存参数原值或凭据。
# 函数用途: 把本次 Schema 引导的安全类型转换附加到统一工具结果 envelope。
def attach_input_coercions(
    result: ToolExecutionResult,
    normalized: NormalizedToolPayload,
) -> None:
    if not normalized.coercions:
        return
    envelope = dict(result.result_envelope or {})
    envelope["input_coercions"] = [item.to_dict() for item in normalized.coercions]
    result.result_envelope = envelope


# LLM: 参数来源账目只携带路径和引用，必须与类型纠正一样在统一工具结果 envelope 上附加。
# 函数用途: 保存模型输入、安全默认值和可信上下文补参的逐字段来源，供审计与恢复读取。
def attach_input_sources(
    result: ToolExecutionResult,
    normalized: NormalizedToolPayload,
) -> None:
    if not normalized.input_sources:
        return
    envelope = dict(result.result_envelope or {})
    envelope["input_sources"] = [
        item.to_dict() for item in normalized.input_sources
    ]
    result.result_envelope = envelope


def _gate_output(decision: GateDecision) -> str:
    codes = ",".join(decision.finding_codes) or "RUNTIME_GATE_DENIED"
    hint = " 路径超出允许的工作区范围，请使用工作区内或已授权 root 下的路径。" if _has_path_finding(decision) else ""
    parameter_hint = _parameter_gate_hint(decision)
    model_message = decision.model_message
    if model_message:
        return (
            f"runtime gate denied: gate={decision.gate}; status={decision.status}; "
            f"findings={codes}; {model_message}{parameter_hint}{hint}"
        )
    return (
        f"runtime gate denied: gate={decision.gate}; status={decision.status}; "
        f"findings={codes}; 工具未授权或未通过运行时门{parameter_hint}{hint}"
    )


# LLM: 参数修复提示只能投影 Schema 路径/关键字/期望结构，不能拼入用户输入原值。
# 函数用途: 给模型补充可直接修正的字段位置，避免只收到笼统错误码。
def _parameter_gate_hint(decision: GateDecision) -> str:
    details: list[dict[str, Any]] = []
    for finding in decision.findings:
        issues = finding.evidence.get("issues")
        if isinstance(issues, list):
            details.extend(dict(item) for item in issues if isinstance(item, dict))
    if not details:
        return ""
    return f"; parameter_issues={json.dumps(details, ensure_ascii=False, separators=(',', ':'))}"


def _has_path_finding(decision: GateDecision) -> bool:
    return any(code.startswith("PATH_") for code in decision.finding_codes)


def _prepare_tool_payload(
    payload: object,
    *,
    limits: ToolPayloadNormalizeLimits | None,
) -> dict[str, Any] | ToolExecutionResult:
    if isinstance(payload, ToolCallEnvelope):
        payload = payload_from_tool_call_envelope(payload)
    normalized_payload, payload_error = normalize_tool_payload(payload, limits=limits)
    if payload_error:
        # payload 不是合法 JSON 对象/字段名超限/含控制字符等结构错 → 给精确码而非无码兜底成
        # UNKNOWN_ERROR(否则模型被告知"放弃"而非"按 schema 重构一个完整调用")。
        return ToolExecutionResult("unknown", False, payload_error, error_code="TOOL_CALL_PAYLOAD_INVALID")
    assert normalized_payload is not None
    return normalized_payload


def _normalized_payload_or_error(
    call: ExecuteRegistryCallParams,
    envelope: ToolCallEnvelope | None,
) -> dict[str, Any] | ToolExecutionResult:
    prepared = _prepare_tool_payload(envelope or call.payload, limits=call.payload_limits)
    if isinstance(prepared, ToolExecutionResult):
        return attach_result_envelope(prepared, envelope)
    if prepared.get("tool") == "__parse_error__":
        return attach_result_envelope(
            ToolExecutionResult(
                "__parse_error__",
                False,
                parse_error_message(prepared),
                error_code=str(prepared.get("error_code") or "TOOL_CALL_UNCLOSED"),
            ),
            envelope,
        )
    return prepared


def parse_error_message(payload: dict[str, Any]) -> str:
    error = str(payload.get("error") or "工具调用解析失败")
    error_code = str(payload.get("error_code") or "").strip()
    hint = f"{error}。{_PARSE_RETRY_HINT}"
    if error_code in {"TOOL_CALL_UNCLOSED", "TOOL_INLINE_CONTENT_STREAM_ABORTED"}:
        hint = f"{hint}{_TRUNCATED_PAYLOAD_HINT}"
    raw = str(payload.get("raw") or "")
    is_write_payload = '"write_file"' in raw
    has_write_recovery = isinstance(payload.get("write_recovery"), dict)
    if error_code in {"TOOL_CALL_UNCLOSED", "TOOL_INLINE_CONTENT_STREAM_ABORTED"} and is_write_payload and '"content"' in raw or has_write_recovery:
        hint = f"{hint}{_TRUNCATED_WRITE_HINT}"
    return hint


def _unclosed_tool_call_parse_error(
    raw: str,
    limits: ToolPayloadNormalizeLimits | None,
) -> dict[str, Any]:
    payload = parse_error_payload(
        "工具调用缺少结束标记 [/TOOL_CALL]",
        raw,
        limits=limits,
        error_code="TOOL_CALL_UNCLOSED",
    )
    payload.update(_write_file_unclosed_recovery_fields(raw))
    return payload


def _write_file_unclosed_recovery_fields(raw: str) -> dict[str, object]:
    if _WRITE_FILE_TOOL_RE.search(raw) is None or _WRITE_FILE_CONTENT_RE.search(raw) is None:
        return {}
    path = _write_file_path(raw)
    recovery: dict[str, object] = {
        "strategy": "restart_same_file_with_append_chunks",
        "max_chunk_chars": RECOVERY_WRITE_CHUNK_CHARS,
        "first_tool_call": {
            "tool": "write_file",
            "path": path,
            "mode": "overwrite",
            "content": f"<first chunk <= {RECOVERY_WRITE_CHUNK_CHARS} chars>",
        },
        "next_tool_call": {
            "tool": "write_file",
            "path": path,
            "mode": "append",
            "content": f"<next chunk <= {RECOVERY_WRITE_CHUNK_CHARS} chars>",
        },
    }
    if path:
        recovery["path"] = path
    return {
        "source_tool": "write_file",
        "path": path,
        "content_field_present": True,
        "previous_write_committed": False,
        "write_recovery": recovery,
    }


def _write_file_path(raw: str) -> str:
    match = _WRITE_FILE_PATH_RE.search(raw)
    if match is None:
        return ""
    try:
        return str(json.loads(f'"{match.group("path")}"'))
    except json.JSONDecodeError:
        return match.group("path")


def _same_name_wrapper_error(
    payload: dict[str, Any],
    envelope: ToolCallEnvelope | None,
) -> ToolExecutionResult | None:
    tool_text = str(payload.get("tool") or "").strip()
    if not tool_text:
        return None
    if not isinstance(payload.get(tool_text), dict):
        return None
    body = {
        "ok": False,
        "error": "tool parameters must be top-level current fields; same-name wrapper objects are not accepted.",
        "invalid_field": tool_text,
        "how_to_fix": f"Move fields from {tool_text} to the top level next to tool.",
    }
    return attach_result_envelope(
        ToolExecutionResult(
            tool_text,
            False,
            json.dumps(body, ensure_ascii=False, indent=2),
            error_code="TOOL_INVALID_ARGUMENTS",
        ),
        envelope,
    )


# LLM: Schema 编译或归一化异常必须在任何工具门和实现之前 fail closed，不能回退原始参数继续执行。
# 函数用途: 使用当前工具的唯一输入结构做安全类型纠正，并把结构配置错误转成明确失败。
def _normalized_tool_input_or_error(
    payload: dict[str, Any],
    tool: BaseTool | None,
    envelope: ToolCallEnvelope | None,
    call: ExecuteRegistryCallParams,
) -> NormalizedToolPayload | ToolExecutionResult:
    if tool is None:
        return NormalizedToolPayload(dict(payload))
    spec = getattr(tool, "spec", None)
    if spec is None:
        return NormalizedToolPayload(dict(payload))
    try:
        return normalize_tool_payload_for_spec(
            payload,
            spec,
            completion_context=_tool_input_completion_context(call, envelope),
        )
    except (TypeError, ValueError) as exc:
        tool_name = str(payload.get("tool") or getattr(spec, "name", "") or "unknown")
        return attach_result_envelope(
            ToolExecutionResult(
                tool_name,
                False,
                f"tool input schema unavailable: {exc}",
                error_code="TOOL_UNAVAILABLE",
            ),
            envelope,
        )


# LLM: 可信补参上下文由 Registry 从 typed scope 和硬边界构造，模型 payload 不能参与其根对象。
# 函数用途: 为统一参数补全提供当前调用身份、任务写边界和规范 workspace 根。
def _tool_input_completion_context(
    call: ExecuteRegistryCallParams,
    envelope: ToolCallEnvelope | None,
) -> ToolInputCompletionContext:
    return ToolInputCompletionContext(
        call_id=envelope.call_id if envelope is not None else "",
        call_source=envelope.source if envelope is not None else "",
        trusted_context={
            "run_scope": envelope.scope.to_dict() if envelope is not None else {},
            "write_boundary": (
                dict(call.write_boundary)
                if isinstance(call.write_boundary, dict)
                else {}
            ),
            "registry": {
                "workspace_root": str(call.workspace_root.resolve(strict=False)),
            },
        },
    )


def _invoke_registry_with_envelope(
    call: ExecuteRegistryCallParams,
    envelope: ToolCallEnvelope | None,
    tool_name: str,
    payload: dict[str, Any],
) -> ToolExecutionResult:
    payload = _with_execution_scope(payload, envelope)
    request = RegistryToolInvokeRequest(
        tool_name=tool_name,
        payload=payload,
        tools=call.tools,
        workspace_root=call.workspace_root,
        workspace_roots=call.workspace_roots,
        allowed_tools=call.allowed_tools,
        write_boundary=call.write_boundary,
        path_access_mode=call.path_access_mode,
        path_dangerous_roots=call.path_dangerous_roots,
        owner_scope_root=call.owner_scope_root,
        runtime_snapshot=call.runtime_snapshot,
        owner_type=call.owner_type,
    )
    return attach_result_envelope(
        resilient_tool_invoke(
            ResilientToolInvokeRequest(
                invoke=lambda: invoke_registry_tool(request),
                spec=call.tools[tool_name].spec,
                workspace_root=call.workspace_root,
                write_boundary=call.write_boundary,
            )
        ),
        envelope,
    )


def _with_execution_scope(payload: dict[str, Any], envelope: ToolCallEnvelope | None) -> dict[str, Any]:
    if envelope is None or not envelope.scope.run_id:
        return payload
    return {
        **payload,
        "__run_scope": envelope.scope.to_dict(),
        "__tool_call_id": envelope.call_id,
    }


def _registry_auth_error(tool_name: str, call: ExecuteRegistryCallParams) -> str:
    return registry_auth_error(
        tool_name,
        _registry_auth_context(call),
    )


def _registry_auth_error_code(tool_name: str, call: ExecuteRegistryCallParams) -> str:
    return registry_auth_error_code(tool_name, _registry_auth_context(call))


# LLM: 快照检查只发生在硬授权通过后；未进入本轮快照的工具也不能因动态注册而在中途扩张权限面。
# 函数用途: 拒绝本轮快照外或未就绪的工具，并保留创建快照时的稳定机器原因。
def _runtime_snapshot_unavailable(
    tool_name: str,
    call: ExecuteRegistryCallParams,
    envelope: ToolCallEnvelope | None,
) -> ToolExecutionResult | None:
    snapshot = call.runtime_snapshot
    if snapshot is None or tool_name in snapshot.available_tool_names:
        return None
    if tool_name not in call.tools:
        # 保留“从未注册”和“本轮快照外”两种稳定错误语义；未知名称由统一 invoke 入口
        # 返回 TOOL_NOT_REGISTERED，动态注册到进程但不在旧快照中的实现才在这里阻断。
        return None
    unavailable = {
        name: (error_code, reason)
        for name, error_code, reason in snapshot.unavailable_tools
    }
    error_code, reason = unavailable.get(
        tool_name,
        (
            "TOOL_UNAVAILABLE",
            "工具不在当前 run 开始时固定的运行快照中",
        ),
    )
    payload = json.dumps(
        {
            "error": "tool_unavailable",
            "tool": tool_name,
            "reason": reason or "当前运行环境未就绪",
        },
        ensure_ascii=False,
    )
    return attach_result_envelope(
        ToolExecutionResult(
            tool_name,
            False,
            payload,
            error_code=error_code or "TOOL_UNAVAILABLE",
        ),
        envelope,
    )


# LLM: 鉴权上下文只保留真正参与权限判断的字段；readiness 和 capabilities 不得伪装成授权条件。
# 函数用途: 从调用参数构造 owner 策略、默认隐藏工具和显式 allowlist 的唯一鉴权输入。
def _registry_auth_context(call: ExecuteRegistryCallParams) -> ToolAuthContext:
    return ToolAuthContext(
        allowed=allowed_tool_set(call.allowed_tools),
        disabled=allowed_tool_set(call.disabled_tools) or set(),
        default_hidden=allowed_tool_set(call.default_hidden_tool_names) or set(),
    )
