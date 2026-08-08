from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from typing import ClassVar

_LOGGER = logging.getLogger(__name__)

from ...backends import ModelResponse
from ...backends.tool_protocol_adapter import (
    ProviderToolCallRequest,
    ProviderToolCallResult,
    ToolProtocolViolation,
    canonical_tool_calls_from_response,
)
from ...tooling.content_recovery_mode import (
    LongContentRecoveryRequest,
    long_content_recovery_context,
)
from ...tooling.runtime_contracts import ToolCall, ToolChoice
from .._runtime_params import ToolLoopExecuteParams
from ..tool_guard.call_guardrail import tool_guardrail_records
from ..tool_guard.unresolved_runtime_issue import (
    has_unresolved_runtime_issues,
    unresolved_runtime_issue_context,
)

_PROTECTED_TOOL_MARKERS = (
    "[tool-record",
    "[tool-output-record",
    "[/tool-call]",
)

# 长期助手 式空响应 nudge 的有界次数:执行过工具后模型空正文无工具调用时,
# 塞一条提示要求继续;超限后诚实失败(USER_REPLY_UNAVAILABLE),不让坏输出无限烧 token。
_MAX_EMPTY_TEXT_REPAIRS = 2
_EMPTY_TEXT_NUDGE = (
    "[tool-system]\n"
    "上一轮你执行了工具调用，但回复正文为空。请基于上方真实工具结果继续推进："
    "任务未完成就调用下一步工具，任务已完成才给最终回答。不要重复读取同一批材料，"
    "也不要只复述计划。"
)


@dataclass(frozen=True)
class ToolLoopRepairCounters:
    __test__: ClassVar[bool] = False

    protected_marker_repairs: int = 0
    unresolved_runtime_issue_redirects: int = 0
    protocol_repairs: int = 0
    empty_text_repairs: int = 0


# LLM: repair counters 只记录真实协议修复次数，不再承载任何工作风格或检查点提醒状态。
# 函数用途: 增加一次内部工具标记修复计数，同时保留其他错误修复计数。
def _inc_protected_marker(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        protected_marker_repairs=counters.protected_marker_repairs + 1,
        unresolved_runtime_issue_redirects=counters.unresolved_runtime_issue_redirects,
        protocol_repairs=counters.protocol_repairs,
        empty_text_repairs=counters.empty_text_repairs,
    )


# LLM: unresolved issue 只允许一次结构化重定向，计数必须与 protected marker 相互独立。
# 函数用途: 增加一次未解决运行错误的修复计数。
def _inc_unresolved_runtime_issue(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        protected_marker_repairs=counters.protected_marker_repairs,
        unresolved_runtime_issue_redirects=counters.unresolved_runtime_issue_redirects + 1,
        protocol_repairs=counters.protocol_repairs,
        empty_text_repairs=counters.empty_text_repairs,
    )


def _inc_protocol(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        protected_marker_repairs=counters.protected_marker_repairs,
        unresolved_runtime_issue_redirects=counters.unresolved_runtime_issue_redirects,
        protocol_repairs=counters.protocol_repairs + 1,
        empty_text_repairs=counters.empty_text_repairs,
    )


# LLM: 空正文 nudge 有界(默认 2 次):工具执行后模型必须产出终态正文或继续调工具,
# 静默空收口只会让用户收不到回复。计数与协议修复相互独立。
# 函数用途: 增加一次"执行过工具但空正文"的修复计数。
def _inc_empty_text(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        protected_marker_repairs=counters.protected_marker_repairs,
        unresolved_runtime_issue_redirects=counters.unresolved_runtime_issue_redirects,
        protocol_repairs=counters.protocol_repairs,
        empty_text_repairs=counters.empty_text_repairs + 1,
    )


@dataclass(frozen=True)
class ToolLoopResponseDecisionRequest:
    __test__: ClassVar[bool] = False

    agent: object
    params: ToolLoopExecuteParams
    response: object
    counters: ToolLoopRepairCounters
    turn_id: str = ""


@dataclass(frozen=True)
class ToolLoopResponseDecision:
    __test__: ClassVar[bool] = False

    action: str
    response: object
    calls: list[ToolCall]
    counters: ToolLoopRepairCounters


@dataclass(frozen=True)
class UnresolvedRuntimeIssueDecision:
    __test__: ClassVar[bool] = False

    action: str
    response: object
    calls: list[ToolCall]
    counters: ToolLoopRepairCounters


@dataclass(frozen=True)
class UnresolvedRuntimeIssueDecisionRequest:
    __test__: ClassVar[bool] = False

    agent: object
    params: object
    response: object
    counters: ToolLoopRepairCounters


@dataclass(frozen=True)
class _NoToolCallsRequest:
    agent: object
    params: ToolLoopExecuteParams
    response: object
    counters: ToolLoopRepairCounters
    has_protected_marker: bool


def tool_loop_response_decision(
    request: ToolLoopResponseDecisionRequest,
) -> ToolLoopResponseDecision:
    has_protected_marker = contains_protected_tool_marker(request.response.text)
    if has_protected_marker:
        request.params.tool_context.append(
            protected_tool_marker_repair_context(native=_native_tool_use_active(request.params))
        )

    if not request.agent.config.enable_tools:
        final = _disabled_tools_response(request.response, has_protected_marker)
        return ToolLoopResponseDecision("break", final, [], request.counters)

    adapted = _tool_calls_from_response(request)
    if adapted.violations:
        return _protocol_violation_decision(request, adapted.violations)
    if adapted.calls:
        return _tool_calls_decision(request, list(adapted.calls))

    return _no_tool_calls_decision(
        _NoToolCallsRequest(
            request.agent,
            request.params,
            request.response,
            request.counters,
            has_protected_marker,
        )
    )


def _tool_calls_from_response(
    request: ToolLoopResponseDecisionRequest,
) -> ProviderToolCallResult:
    """Adapt only the protocol fixed before this run; prose never gains authority."""

    params = request.params
    run_id = str(params.run_id or "").strip()
    turn_id = str(request.turn_id or "").strip() or f"{run_id}:model-turn"
    return canonical_tool_calls_from_response(
        ProviderToolCallRequest(
            response=request.response,
            protocol=params.tool_protocol_snapshot,
            runtime_snapshot=params.tool_runtime_snapshot,
            turn_id=turn_id,
            attempt_id=str(params.attempt_id or params.request_id or run_id or "attempt"),
            required_actions=tuple(
                getattr(
                    getattr(params, "effective_contract_snapshot", None), "required_actions", ()
                )
                or ()
            ),
            tool_choice=_current_model_turn_tool_choice(params),
        )
    )


def _current_model_turn_tool_choice(params: object) -> ToolChoice | None:
    state = getattr(params, "live_archive_state", None)
    candidate = state.get("_current_model_turn_tool_choice") if isinstance(state, dict) else None
    return candidate if isinstance(candidate, ToolChoice) else None


def _native_tool_use_active(params: object) -> bool:
    from ..native_tool_protocol import native_tool_use_active

    return native_tool_use_active(params)


def _protocol_violation_decision(
    request: ToolLoopResponseDecisionRequest,
    violations: tuple[ToolProtocolViolation, ...],
) -> ToolLoopResponseDecision:
    model_output = str(getattr(request.response, "text", "") or "")
    _LOGGER.warning(
        "protocol violation: turn=%s violations=%s model_output_head=%r",
        str(request.turn_id or ""),
        [item.to_dict() for item in violations],
        model_output[:400],
    )
    payload = [item.to_dict() for item in violations]
    state = getattr(request.params, "live_archive_state", None)
    if isinstance(state, dict):
        trace = state.setdefault("protocol_violation_trace", [])
        if isinstance(trace, list):
            trace.append(
                {
                    "turn_id": str(request.turn_id or ""),
                    "violations": payload,
                }
            )
    request.params.tool_context.append(
        "[tool-protocol-violation]\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        + "\n上一轮没有形成可执行工具调用（工具协议格式无效）。若仍需操作，请按以下唯一格式"
        "重新发起——[TOOL_CALL] 包裹的单个 JSON 对象，参数直接平铺：\n"
        '[TOOL_CALL]\n{"tool": "read_file", "path": "README.md"}\n[/TOOL_CALL]\n'
        "写文件类似：{\"tool\": \"write_file\", \"path\": \"output/main.go\", "
        "\"content\": \"package main...\"}。必须使用 Tool Catalog 里该工具自己的参数名；"
        "不要用 args/arguments/param_name 包裹参数，不要写 => 或 <invoke> 风格，"
        "不要把工具调用写进正文、代码块或解释文字。"
    )
    max_repairs = max(1, int(getattr(request.params, "max_protocol_repairs", 2) or 2))
    if request.counters.protocol_repairs < max_repairs:
        return ToolLoopResponseDecision(
            "continue",
            None,
            [],
            _inc_protocol(request.counters),
        )
    return ToolLoopResponseDecision(
        "break",
        ModelResponse(
            text=(
                "本轮工具协议连续不符合运行约定，系统没有执行任何正文或伪工具块。"
                "请重新发起请求，或改用当前模型真实支持的工具协议。"
            ),
            backend=str(getattr(request.response, "backend", "") or ""),
            runtime_status="blocked",
            runtime_reason="PROTOCOL_VIOLATION",
            runtime_source="tool_protocol_adapter",
        ),
        [],
        request.counters,
    )


def contains_protected_tool_marker(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _PROTECTED_TOOL_MARKERS)


def sanitize_protected_tool_marker_response(
    response: ModelResponse, *, native: bool = False
) -> ModelResponse:
    if not contains_protected_tool_marker(response.text):
        return response
    # native 下系统执行的是结构化 tool_use，不是文本 [TOOL_CALL] 块；说成「只执行真实
    # [TOOL_CALL] 块」会把 native 模型往回引到已废弃的文本协议（弱模型有训练惯性）。
    execution_note = (
        "系统只会执行结构化工具调用（tool_use）。"
        if native
        else "系统只会执行真实 [TOOL_CALL] 块。"
    )
    return ModelResponse(
        text=(
            "[assistant-response-omitted]\n"
            "模型回复包含系统内部的 tool-record/tool-output-record 标记，"
            f"该回复正文不进入后续 live prompt；{execution_note}"
        ),
        backend=response.backend,
    )


def protected_tool_marker_repair_context(*, native: bool = False) -> str:
    # native 下纠偏措辞要指向结构化工具调用，不能教模型再写文本 [TOOL_CALL]（治根护栏：
    # 原生协议禁止退回文本协议，纠错提示更不能反向把它带回去）。
    reissue = (
        "请改用结构化工具调用（tool_use）请求工具，或只基于已经存在的真实工具回执总结。"
        if native
        else "请改用真实 `[TOOL_CALL]...[/TOOL_CALL]` 请求工具，或只基于已经存在的真实工具回执总结。"
    )
    return (
        "[tool-system]\n"
        "上一轮模型回复包含系统内部的 `[tool-record]` / `[tool-output-record]` 标记。"
        "这些标记只能由工具循环在真实工具执行后写入，模型不能自行书写、复制或假装工具成功。"
        f"{reissue}"
    )


def protected_tool_marker_block_response(backend: str) -> ModelResponse:
    return ModelResponse(
        text=(
            "系统已阻止本轮结果：模型输出了系统内部的工具记录标记，"
            "但没有提供可执行的真实工具调用或可信的真实工具回执。"
            "当前不能把这次回复视为完成；请重新发起真实工具调用，"
            "或读取现有 task/subagent 状态后再汇报。"
        ),
        backend=backend,
        runtime_status="blocked",
        runtime_reason="PROTECTED_TOOL_MARKER",
    )


def _disabled_tools_response(response, has_protected_marker: bool):
    if not has_protected_marker:
        return response
    return protected_tool_marker_block_response(response.backend)


# LLM: 有工具调用时只处理协议截断、长内容恢复和内部标记清理；不得按读取轮数注入额外工作。
# 函数用途: 决定本轮真实工具调用是执行、纠偏后重试，还是因客观错误停止。
def _tool_calls_decision(
    request: ToolLoopResponseDecisionRequest,
    calls: list[ToolCall],
) -> ToolLoopResponseDecision:
    native_truncated_write = _native_truncated_write_decision(request, calls)
    if native_truncated_write is not None:
        return native_truncated_write
    clean_response = sanitize_protected_tool_marker_response(
        request.response, native=_native_tool_use_active(request.params)
    )
    return ToolLoopResponseDecision("run_tools", clean_response, calls, request.counters)


# native 长 content 写被 max_tokens/SSE 截断 → 参数清空 → 同一截断空参 write_file 连续失败
# 这么多次即认定死循环（反复重生成又截断），打硬出口而非无限重试。建议 3：给模型 1~2 次
# 分块纠偏机会后仍截断就停，并把真实失败原因交给最终回复。
_NATIVE_TRUNCATED_WRITE_LOOP_LIMIT = 3
_TRUNCATED_WRITE_FAILURE_CLASS = "code:TOOL_PARAMETER_REQUIRED"


def _native_truncated_write_decision(
    request: ToolLoopResponseDecisionRequest,
    calls: list[ToolCall],
) -> ToolLoopResponseDecision | None:
    """P0-2:native 写长文档被截断成空参时，激活长内容恢复（分块写），连续 N 次即硬 break。

    只在 native 协议 + 本轮响应疑似截断（``response.truncated``）+ 存在缺参的 write_file 调用
    时介入。正常多轮写大文档（未截断、参数完整）一律不进此分支，零误伤；text 协议另有
    write_abort 路径，也不进。
    """
    if not _native_tool_use_active(request.params):
        return None
    if not bool(getattr(request.response, "truncated", False)):
        return None
    if not _has_truncated_empty_write(calls):
        return None
    prior_failures = _consecutive_truncated_write_failures(request.agent)
    if prior_failures + 1 >= _NATIVE_TRUNCATED_WRITE_LOOP_LIMIT:
        return ToolLoopResponseDecision(
            "break",
            _native_truncated_write_loop_break_response(
                request.response.backend, prior_failures + 1
            ),
            [],
            request.counters,
        )
    request.params.tool_context.append(_native_truncated_write_recovery_context(calls))
    return ToolLoopResponseDecision("continue", None, [], request.counters)


def _has_truncated_empty_write(calls: list[ToolCall]) -> bool:
    for call in calls:
        if _call_tool(call) != "write_file":
            continue
        has_path = bool(str(call.arguments.get("path") or "").strip())
        has_content = (
            call.arguments.get("content") is not None
            or call.arguments.get("data_base64") is not None
        )
        if not has_path or not has_content:
            return True
    return False


def _consecutive_truncated_write_failures(agent: object) -> int:
    """从 guardrail records 尾部数连续的 write_file + TOOL_PARAMETER_REQUIRED 失败。

    复用既有 ``_tool_call_guardrail_records``（已按 tool_name/args_hash/failure_class 结构化）；
    一旦尾部出现非该类记录（例如一次成功 write_file）即中断计数 → 正常写入会自然清零，
    不会把历史失败累计到无关任务上。
    """
    count = 0
    for record in reversed(tool_guardrail_records(agent)):
        if str(record.get("tool_name") or "") != "write_file":
            break
        if record.get("failed") is not True:
            break
        if str(record.get("failure_class") or "") != _TRUNCATED_WRITE_FAILURE_CLASS:
            break
        count += 1
    return count


def _native_truncated_write_recovery_context(calls: list[ToolCall]) -> str:
    payload = next(
        (_tool_call_payload(call) for call in calls if _call_tool(call) == "write_file"),
        {"tool": "write_file"},
    )
    base = long_content_recovery_context(
        LongContentRecoveryRequest(
            payload=payload,
            result_tool="write_file",
            result_ok=False,
            output="",
            result_error_code="TOOL_PARAMETER_REQUIRED",
            truncated=True,
        )
    )
    header = (
        "[tool-system]\n"
        "上一轮 write_file 的参数 JSON 在流式生成时被截断（疑似 max_tokens/长度上限），"
        "导致工具收到空参数而无法执行。请把正文拆成更小的块分多次写入，"
        '第一块用 mode="overwrite" 重写目标文件，后续块用 mode="append"。'
    )
    return f"{header}\n{base}" if base else header


def _native_truncated_write_loop_break_response(backend: str, attempts: int) -> ModelResponse:
    return ModelResponse(
        text=(
            "系统已停止本次写入循环：write_file 的参数在流式生成时连续 "
            f"{attempts} 次被截断（max_tokens/长度上限），分块纠偏后仍未成功闭合参数 JSON。\n"
            "请不要再用单次大块 write_file 重试同一目标；改用显著更小的分块写入"
            "（每块正文更短，第一块 overwrite、后续 append），或先 task_progress 记录已写进度再续写。"
        ),
        backend=backend,
        runtime_status="unfinished",
        runtime_reason="NATIVE_TRUNCATED_WRITE_LOOP",
        runtime_source="tool_loop",
    )


def _call_tool(call: ToolCall) -> str:
    return call.tool_name


def _tool_call_payload(call: ToolCall) -> dict[str, object]:
    return {"tool": call.tool_name, "call_id": call.call_id, **call.arguments}


# LLM: 无工具调用时只处理 typed runtime error 和受保护标记；普通模型回复直接结束本轮。
# 函数用途: 判断没有工具请求的模型回复应返回、修复一次，还是结构化阻断。
def _no_tool_calls_decision(request: _NoToolCallsRequest) -> ToolLoopResponseDecision:
    if _is_runtime_status_response(request.response):
        return ToolLoopResponseDecision("break", request.response, [], request.counters)
    required_decision = _required_action_no_tool_call_decision(request)
    if required_decision is not None:
        return required_decision
    unresolved_issue_decision = unresolved_runtime_issue_no_tool_call_decision(
        _unresolved_runtime_issue_request(request)
    )
    if unresolved_issue_decision is not None:
        return _unresolved_runtime_issue_decision(unresolved_issue_decision)
    if not request.has_protected_marker:
        # 长期助手 式空响应 nudge(参考 长期助手 conversation_loop 6511-6547:
        # "You just executed tool calls but returned an empty response... continue")。
        # 执行过工具后模型空正文无工具调用 = 静默收口:直接 break 会让用户收不到
        # 任何回复(USER_REPLY_UNAVAILABLE)。有界重试(默认 2 次)后仍空 → 诚实失败。
        if (
            not str(getattr(request.response, "text", "") or "").strip()
            and list(getattr(request.params, "executed_tools", None) or [])
            and request.counters.empty_text_repairs < _MAX_EMPTY_TEXT_REPAIRS
        ):
            request.params.tool_context.append(_EMPTY_TEXT_NUDGE)
            return ToolLoopResponseDecision(
                "continue", None, [], _inc_empty_text(request.counters)
            )
        return ToolLoopResponseDecision("break", request.response, [], request.counters)
    if request.counters.protected_marker_repairs < 1:
        return ToolLoopResponseDecision(
            "continue", None, [], _inc_protected_marker(request.counters)
        )
    final = protected_tool_marker_block_response(request.response.backend)
    return ToolLoopResponseDecision("break", final, [], request.counters)


def _required_action_no_tool_call_decision(
    request: _NoToolCallsRequest,
) -> ToolLoopResponseDecision | None:
    from ...contracts.required_actions import (
        render_required_action_guidance,
        required_action_assessment_failed,
        required_action_no_tool_decision,
    )

    snapshot = getattr(request.params, "effective_contract_snapshot", None)
    if not tuple(
        getattr(snapshot, "required_actions", ()) or ()
    ) and not required_action_assessment_failed(snapshot):
        return None
    outcome = required_action_no_tool_decision(snapshot)
    if outcome == "complete":
        return None
    if outcome == "repair":
        guidance = render_required_action_guidance(snapshot)
        request.params.tool_context.append(
            guidance + "\n上一轮没有产生任何 canonical ToolCall，因此没有动作证据。"
            "请发起所需的真实工具调用；若权限、审批、能力或用户输入不足，"
            "请保持结构化阻塞，不要用自然语言宣称完成。"
        )
        return ToolLoopResponseDecision("continue", None, [], request.counters)
    actions = tuple(getattr(snapshot, "required_actions", ()) or ())
    if not actions and required_action_assessment_failed(snapshot):
        reason = "REQUIRED_ACTION_ASSESSMENT_FAILED"
    else:
        reason = next(
            (
                str(item.blocked_reason or "").strip()
                for item in actions
                if item.status == outcome and str(item.blocked_reason or "").strip()
            ),
            f"REQUIRED_ACTION_{outcome.upper()}",
        )
    return ToolLoopResponseDecision(
        "break",
        replace(
            request.response,
            runtime_status=outcome,
            runtime_reason=reason,
            runtime_source="required_action_completion_gate",
        ),
        [],
        request.counters,
    )


def _is_runtime_status_response(response: object) -> bool:
    status = str(getattr(response, "runtime_status", "") or "").strip()
    if status and status != "ok":
        return True
    for field in ("runtime_reason", "runtime_source"):
        if str(getattr(response, field, "") or "").strip():
            return True
    return False


def unresolved_runtime_issue_no_tool_call_decision(
    request: UnresolvedRuntimeIssueDecisionRequest,
) -> UnresolvedRuntimeIssueDecision | None:
    if not has_unresolved_runtime_issues(request.params):
        return None
    if request.counters.unresolved_runtime_issue_redirects >= 1:
        return UnresolvedRuntimeIssueDecision("break", request.response, [], request.counters)
    repair_context = unresolved_runtime_issue_context(
        request.params,
        request.counters.unresolved_runtime_issue_redirects,
    )
    if repair_context:
        request.params.tool_context.append(repair_context)
        return UnresolvedRuntimeIssueDecision(
            "continue",
            None,
            [],
            _inc_unresolved_runtime_issue(request.counters),
        )
    return UnresolvedRuntimeIssueDecision("break", request.response, [], request.counters)


def _unresolved_runtime_issue_decision(
    decision: UnresolvedRuntimeIssueDecision,
) -> ToolLoopResponseDecision:
    return ToolLoopResponseDecision(
        decision.action, decision.response, decision.calls, decision.counters
    )


def _unresolved_runtime_issue_request(
    request: _NoToolCallsRequest,
) -> UnresolvedRuntimeIssueDecisionRequest:
    return UnresolvedRuntimeIssueDecisionRequest(
        request.agent, request.params, request.response, request.counters
    )
