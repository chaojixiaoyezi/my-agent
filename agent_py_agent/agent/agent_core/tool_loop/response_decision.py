from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from typing import ClassVar

_LOGGER = logging.getLogger(__name__)


def _append_workspace_diagnostic(probe: dict[str, object]) -> None:
    """workspace 根缺失诊断 JSONL 落盘（双席 seq2075/2076：日志可能丢，需可追溯）。

    fail-silent：写失败绝不影响收口判定。路径用临时目录（各环境可写）；
    内容为探针结构化快照（不含完整 contract/密钥）。
    """
    try:
        import os
        import tempfile
        import time

        path = os.path.join(
            tempfile.gettempdir(), "my-agent-workspace-diagnostics.jsonl"
        )
        record = dict(probe)
        record["ts"] = int(time.time())
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001 诊断落盘失败绝不影响判定
        pass

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
    "[tool-result;",
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
    turn_id: str = ""


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
    # G5(2026-08-10 用户裁决):adapter 恒不返回 calls+violations 并存——任何
    # 协议错误(未闭合/截断/JSON 损坏/fence/超限)即「不完整响应」,整轮零执行
    # (calls==()),违规反馈走 _protocol_violation_decision;全好块才执行。
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
            turn_id=str(request.turn_id or ""),
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
    # 2026-08-14 双CLI复刻(bs4 run-r2): 协议违规只进内存 trace + warning 日志
    # (CLI 路径 warning 不进输出文件), 磁盘上无结构化记录——审计断链。此处把
    # provider 原始响应头/解析阶段/repair 计数/模型/协议/副作用事实落 runtime_events,
    # 与 append-only 权威事件同库 (A.3/A.8: 每事件追到具体 attempt)。fail-silent,
    # 审计附加保证绝不反噬执行路径。
    _persist_protocol_violation_event(request, payload, model_output)
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
    # 2026-08-15 3×3 真机(cell1/cell2/cell3 多实例同构): deepseek-v4-flash
    # 高频输出未闭合工具块(约每 10-14 轮一次)。纯格式错误——整轮零执行
    # 已保证安全(本轮 execute 权已取消, J.5 不变), 但任务级 blocked
    # failed 会把长任务杀死。仅当 violations 全为格式类时降级为可续跑族
    # (unfinished, CLI resume_loop 下一轮重发完整工具块); 其他 violation
    # 保持 blocked fail-closed。格式类 = TOOL_CALL_UNCLOSED([TOOL_CALL]
    # 未闭合) + PROTOCOL_VIOLATION@text(text parser 的「text tool block
    # is not closed」未闭合块——唯一 error 类型, 纯格式); 其他来源的
    # PROTOCOL_VIOLATION(如 native 流)不在内。
    if violations and all(
        str(v.code or "") in {"TOOL_CALL_UNCLOSED", "PROTOCOL_VIOLATION"}
        and str(getattr(v, "source_protocol", "") or "") in {"text", ""}
        for v in violations
    ):
        return ToolLoopResponseDecision(
            "break",
            ModelResponse(
                text=(
                    "本轮工具协议连续不符合运行约定(未闭合工具块)，系统没有执行"
                    "任何正文或伪工具块。请重新发起请求，输出完整闭合的 [TOOL_CALL] 块。"
                ),
                backend=str(getattr(request.response, "backend", "") or ""),
                runtime_status="unfinished",
                runtime_reason="TOOL_CALL_UNCLOSED",
                runtime_source="tool_protocol_adapter",
            ),
            [],
            request.counters,
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


def _persist_protocol_violation_event(
    request: ToolLoopResponseDecisionRequest,
    violations: list[dict[str, str]],
    model_output: str,
) -> None:
    """协议违规落 runtime_events 权威账本（fail-silent，审计附加保证）。

    2026-08-14 双CLI复刻(bs4 run-r2): 协议违规只进内存 trace + warning 日志
    (CLI 路径 warning 不进输出文件), 磁盘上无结构化记录——provider 原始响应、
    解析阶段、repair 次数、终态全部不可审计。此处落 append-only 事件:
    - provider 原始响应头(前 500 字, 含模型实际输出的工具协议风格, 如 XML
      <tool_calls> vs [TOOL_CALL], 不截断关键证据);
    - 结构化 violations(code/detail/source_protocol/evidence_preview);
    - 解析阶段(text/native adapter)、模型、协议、repair 计数、是否将 break。
    """
    try:
        params = request.params
        # R1 接线模式: agent.subagents.runtime_db 是 owner 权威库
        # (_tool_loop_service.py:652 同款); LOCAL_UNMANAGED 无权威库 → noop
        subagents = getattr(request, "agent", None)
        subagents = getattr(subagents, "subagents", None)
        repo = getattr(subagents, "runtime_db", None)
        if repo is None or not callable(getattr(repo, "append_event", None)):
            return
        run_id = str(getattr(params, "run_id", "") or "")
        attempt_id = str(getattr(params, "attempt_id", "") or "")
        if not run_id or not attempt_id:
            return
        row = repo.agent_run_for_run_id(run_id)
        if row is None:
            return
        max_repairs = max(1, int(getattr(params, "max_protocol_repairs", 2) or 2))
        will_break = request.counters.protocol_repairs >= max_repairs
        protocol = getattr(request, "protocol", None)
        protocol_snapshot = getattr(protocol, "runtime_snapshot", None)
        if protocol_snapshot is None:
            protocol_snapshot = getattr(request, "protocol_snapshot", None)
        source_protocol = str(
            getattr(protocol_snapshot, "source_protocol", "") or ""
        )
        capability = getattr(protocol_snapshot, "capability", None)
        repo.append_event(
            event_type="protocol_violation",
            attempt_id=attempt_id,
            agent_run_id=str(row["agent_run_id"]),
            task_run_id=str(row["task_run_id"] or ""),
            payload={
                "turn_id": str(request.turn_id or ""),
                "violations": violations,
                "model_output_head": model_output[:500],
                "source_protocol": source_protocol,
                "model": str(getattr(capability, "model", "") or "")
                if capability is not None
                else "",
                "provider": str(getattr(capability, "provider", "") or "")
                if capability is not None
                else "",
                "protocol_repairs": request.counters.protocol_repairs,
                "will_break": will_break,
                "stage": "tool_protocol_adapter",
            },
        )
    except Exception:  # noqa: BLE001 审计落账失败绝不反噬执行路径
        pass


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
        # 收口机器兜底 gate(2026-08-16 收口模式对齐 会话运行时): 程序验证只做
        # 未知错误——attempt 内有工具结果未知族(写副作用未知/超时/执行者死)
        # 才机器兜底核实;无未知 → 模型自审收口(收口审计纪律提示词约束)。
        # 兜底未决达 4 次熔断放过(BLOCKED/UNKNOWN_UNRESOLVED 通知用户),
        # 绝不无限重试(2026-08-15 强制验证时代 cell 多次假完成实锤已被
        # 同模型 会话运行时 实证的「模型自审+强 audit 提示词」取代)。
        verified = _delivery_verify_no_tool_call_decision(request)
        if verified is not None:
            return verified
        return ToolLoopResponseDecision("break", request.response, [], request.counters)
    if request.counters.protected_marker_repairs < 1:
        return ToolLoopResponseDecision(
            "continue", None, [], _inc_protected_marker(request.counters)
        )
    final = protected_tool_marker_block_response(request.response.backend)
    return ToolLoopResponseDecision("break", final, [], request.counters)


def _delivery_verify_no_tool_call_decision(
    request: _NoToolCallsRequest,
) -> ToolLoopResponseDecision | None:
    """收口机器验证 gate（2026-08-16 收口模式对齐 会话运行时 + 群复核加固）。

    三支裁决（verify_commands 是部署者显式验收合同，不是自动程序验证）：
    1. 有 verify_commands 合同 → 收口强制执行机器验证（无论有无未知）：
       passed=终态 delivery_verify 盖章；failed=unfinished 修复；验证失败
       计数熔断（同 attempt 第 4 次放过 → BLOCKED 通知，绝不无限重试）。
       合同声明但非法（CONTRACT_INVALID）→ fail-closed，绝不放行。
    2. 无合同 + 无未知 → None（audit-only 直接结束，模型自审收口——收口
       审计纪律提示词约束）。
    3. 无合同 + 有未知（TOOL_OPERATION_OUTCOME_UNKNOWN：写副作用未知/超时/
       执行者死）→ 注入未知核验提示 + deferral 计数熔断（第 4 次放过）。
    群复核 seq2353/2354 纠偏：自动程序验证只做未知（无合同时）；显式验收
    合同保留机器硬门——「只做未知」不适用于部署者明确要求的 verify_commands。
    """
    # 2026-08-15 边界归位: delivery_verify 从 cli 层移到 agent_core.tool_loop
    # (agent_core 禁导 cli, 打包边界检查 RUNTIME_IMPORTS_CLI 实锤)。
    from .delivery_verify import (
        CLOSEOUT_UNRESOLVED_LIMIT,
        UNKNOWN_OUTCOME_ERROR_CODE,
        VERIFY_CONTRACT_INVALID,
        VERIFY_PASSED,
        VERIFY_SKIPPED,
        _contract_hash,
        attempt_unknown_operation_count,
        attempt_unknown_resolution_count,
        build_verification_id,
        delivery_verify_commands,
        delivery_verify_failure_context,
        persist_closeout_deferral_event,
        persist_delivery_verify_event,
        run_delivery_verification,
    )

    contract = getattr(request.params, "delivery_contract", None)
    _commands, verify_state = delivery_verify_commands(contract)

    if verify_state in (VERIFY_PASSED, VERIFY_CONTRACT_INVALID):
        # 支 1：有验收合同（或声明但非法）→ 机器验证裁决，失败进修复链 +
        # 熔断计数（第 4 次放过）。CONTRACT_INVALID 无法跑命令 → 直接 fail-closed。
        return _contract_verify_decision(
            request, contract, verify_state, run_delivery_verification,
        )

    # 支 2/3：无验收合同 → 自动程序验证只做未知错误。
    unknown_count = attempt_unknown_operation_count(request.agent, request.params)
    if unknown_count <= 0:
        # 支 2：audit-only 直接结束（模型自审，收口审计纪律提示词约束）。
        return None

    # 支 3：有未知 → 落 deferral 事件（UNKNOWN 计数，持久化重启不清零，
    # 按 attempt+turn_id 幂等去重——同收口轮重放不放大计数）+
    # 注入核验提示继续；第 4 次 deferral 即熔断放过（本轮落完 count 达上限）。
    persist_closeout_deferral_event(
        request.agent,
        request.params,
        diagnostic=f"unknown_operations={unknown_count}",
        turn_id=str(getattr(request, "turn_id", "") or ""),
    )
    if attempt_unknown_resolution_count(request.agent, request.params) >= CLOSEOUT_UNRESOLVED_LIMIT:
        return _closeout_circuit_breaker_response(request)
    request.params.tool_context.append(
        "[tool-system closeout-unknown]\n"
        f"当前任务存在 {unknown_count} 个工具结果未知状态的操作（错误码 "
        f"{UNKNOWN_OUTCOME_ERROR_CODE}：写副作用未知/超时/执行者死），系统无法"
        "确认其实际结果。收口前请核验实际状态（检查文件/进程/重跑只读命令）；"
        "无法核验时明确报告未知，不得宣告完成。"
    )
    return ToolLoopResponseDecision("continue", None, [], request.counters)


def _contract_verify_decision(
    request: _NoToolCallsRequest,
    contract: object,
    verify_state: str,
    run_delivery_verification: object,
) -> ToolLoopResponseDecision:
    """支 1：显式 verify_commands 合同的收口机器验证裁决。

    - passed → 终态收口 ok（runtime_source=delivery_verify，gate 层一票否决
      续跑）。
    - 确定性失败（known_failure：非零 exit_code）→ unfinished 修复；同 attempt
      第 4 次确定性失败 → verify 修复熔断 DELIVERY_VERIFY_FAILED_EXHAUSTED。
    - 验证未决（timeout/exec error/cwd 越界/workspace 缺失）→ UNKNOWN 计数，
      第 4 次 → UNKNOWN_UNRESOLVED（两套计数独立，seq2356）。
    - CONTRACT_INVALID → fail-closed（双席 seq2004 硬缺口2: 坏合同不得伪装
      成自然收口）。
    """
    from .delivery_verify import (
        CLOSEOUT_UNRESOLVED_LIMIT,
        VERIFY_FAILED,
        VERIFY_PASSED,
        VERIFY_SKIPPED,
        _contract_hash,
        _delivery_verify_unknown_pending,
        attempt_unknown_resolution_count,
        attempt_verify_failure_count,
        build_verification_id,
        delivery_verify_failure_context,
        persist_delivery_verify_event,
    )

    if verify_state == VERIFY_SKIPPED:
        # 无验证命令（理论上支 1 不进来，防御）：audit-only 不干预。
        return None

    # workspace 根缺失 → fail-closed（绝不跳过边界检查执行；历史取证快照
    # 逻辑保留：根缺失时落诊断 JSONL 定位）。
    try:
        from ..run_task_workspace_writer import current_run_task_workspace_root
        from .run_task_workspace_writer_probe import (
            workspace_root_candidates_probe,
        )

        workspace_root = current_run_task_workspace_root(request.agent, request.params)
        if workspace_root is None:
            probe = workspace_root_candidates_probe(request.agent, request.params)
            _LOGGER.warning(
                "delivery-verify workspace root missing: run_id=%s attempt_id=%s probe=%s",
                str(getattr(request.params, "run_id", "") or ""),
                str(getattr(request.params, "attempt_id", "") or ""),
                probe,
            )
            _append_workspace_diagnostic(probe)
    except Exception:  # noqa: BLE001 workspace 拿不到 → 根缺失 fail-closed
        workspace_root = None

    if verify_state == VERIFY_PASSED:
        try:
            state, results = run_delivery_verification(request.params, workspace_root)
        except Exception:  # noqa: BLE001 验证执行异常保守判失败（fail-closed）
            state, results = VERIFY_FAILED, []
    else:
        # CONTRACT_INVALID：声明了但结构非法，无命令可跑 → fail-closed
        state, results = VERIFY_FAILED, []
    contract_hash = _contract_hash(contract)
    verification_id = build_verification_id(request.params, contract)
    persist_delivery_verify_event(
        request.agent,
        request.params,
        state,
        results,
        contract_hash,
        verification_id,
    )
    if state == VERIFY_PASSED:
        # 验收通过 = 任务终态：清空续跑原因，任何账本/轮限原因不得覆盖。
        passed = replace(
            request.response,
            runtime_reason="",
            runtime_source="delivery_verify",
        )
        return ToolLoopResponseDecision("break", passed, [], request.counters)

    # failed → 分类熔断（seq2356 两套独立计数，本轮失败事件已落账）：
    # 验证未决（timeout/exec error/cwd 越界/workspace 缺失）→ UNKNOWN 计数；
    # 确定性失败（非零 exit_code）→ verify 修复计数。达上限即放过，不给
    # 模型再修（否则无限修复链）。
    if _delivery_verify_unknown_pending(state, results):
        if attempt_unknown_resolution_count(request.agent, request.params) >= CLOSEOUT_UNRESOLVED_LIMIT:
            return _closeout_circuit_breaker_response(request)
    else:
        if attempt_verify_failure_count(request.agent, request.params) >= CLOSEOUT_UNRESOLVED_LIMIT:
            return _verify_exhausted_response(request)

    # 未熔断 → unfinished 修复（fail-closed：坏合同/cwd 越界/根缺失不得伪装
    # 成自然收口，双席 seq2004 硬缺口2）。
    failure_text = (
        delivery_verify_failure_context(results)
        if results
        else (
            "[tool-system delivery-verify-failed]\n"
            "delivery contract 声明的 verify_commands 结构非法、cwd 越界或"
            "workspace 根缺失，产物无法机器验证，任务未完成。"
        )
    )
    request.params.tool_context.append(failure_text)
    from dataclasses import replace as _replace

    text = str(getattr(request.response, "text", "") or "")
    return ToolLoopResponseDecision(
        "break",
        _replace(
            request.response,
            text=(
                text + "\n\n[delivery-verify]\n产物未通过机器验证，任务未完成；"
                "请根据上方验证失败输出继续修复，不要宣告完成。"
            ),
            runtime_status="unfinished",
            runtime_reason="DELIVERY_VERIFY_FAILED",
            runtime_source="delivery_verify",
        ),
        [],
        request.counters,
    )


def _closeout_circuit_breaker_response(
    request: _NoToolCallsRequest,
) -> ToolLoopResponseDecision:
    """UNKNOWN 熔断（第 4 次放过）：BLOCKED/UNKNOWN_UNRESOLVED + 通知用户。

    只用于「状态仍无法确定」（验证未决/无合同写副作用未知），与 verify
    确定性失败熔断（_verify_exhausted_response）分开（seq2356）。
    """
    return ToolLoopResponseDecision(
        "break",
        ModelResponse(
            text=(
                "任务存在持续无法核验的工具结果未知状态（写副作用未知/超时/"
                "执行者死）：系统已连续多次机器兜底核实仍无法确定实际状态，"
                "已停止自动重试（第 4 次放过）。请人工核验目标系统实际状态后"
                "决定下一步；当前任务按「未知未解决」收口，不会自动宣告完成。"
            ),
            backend=str(getattr(request.response, "backend", "") or ""),
            runtime_status="blocked",
            runtime_reason="UNKNOWN_UNRESOLVED",
            runtime_source="closeout_unknown_fallback",
        ),
        [],
        request.counters,
    )


def _verify_exhausted_response(
    request: _NoToolCallsRequest,
) -> ToolLoopResponseDecision:
    """verify 修复熔断（第 4 次放过）：验证反复确定性失败 → BLOCKED 通知。

    与 UNKNOWN 熔断分开（seq2356）：明确是「验证反复失败」，reason 区分，
    不进入 UNKNOWN_UNRESOLVED 语义。
    """
    return ToolLoopResponseDecision(
        "break",
        ModelResponse(
            text=(
                "任务产物反复未通过机器验证（连续 4 次确定性失败：编译/测试/"
                "断言不通过），系统已停止自动修复重试（第 4 次放过）。请人工"
                "检查验证失败输出后决定下一步；当前任务不会自动宣告完成。"
            ),
            backend=str(getattr(request.response, "backend", "") or ""),
            runtime_status="blocked",
            runtime_reason="DELIVERY_VERIFY_FAILED_EXHAUSTED",
            runtime_source="closeout_unknown_fallback",
        ),
        [],
        request.counters,
    )


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
    # 本 run 真实成功执行证据(executed_tools 非空) → 义务已有动作证据,
    # 收口不再拿评估拆解粒度卡死已完成任务(G4-001 真机铁证: 评估拆 3 个
    # action、模型 2 次调用合并完成 → 第 3 个 action 无独立调用被误杀)。
    outcome = required_action_no_tool_decision(
        snapshot,
        has_succeeded_evidence=bool(getattr(request.params, "executed_tools", ())),
    )
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
