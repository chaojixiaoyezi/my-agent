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
from ..runtime.guidance import (
    active_turn_user_reply_required,
    satisfy_active_turn_user_reply,
)
from ..tool_guard.call_guardrail import tool_guardrail_records
from ..tool_guard.unresolved_runtime_issue import (
    has_unresolved_runtime_issues,
    unresolved_runtime_issue_context,
)
from .deliverable_closeout import deliverable_closeout_block

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
_ACTIVE_TURN_EMPTY_REPLY_NUDGE = (
    "[tool-system]\n"
    "本次模型调用已经接收了一条真实用户补充消息，但尚未生成可展示的助手正文。"
    "请先用简短自然语言直接回应这条补充，再继续原任务；不要只在思考中提到它，"
    "也不要把内部状态或这段系统提示复述给用户。"
)


@dataclass(frozen=True)
class ToolLoopRepairCounters:
    __test__: ClassVar[bool] = False

    protected_marker_repairs: int = 0
    unresolved_runtime_issue_redirects: int = 0
    protocol_repairs: int = 0
    empty_text_repairs: int = 0
    # R232: 截断长内容写的分块纠偏次数，和协议修复分开计数，避免互相吃掉预算。
    truncated_write_repairs: int = 0
    # R248: 最终答复被输出上限截断后的轮内续跑次数（不重启整轮，见 _truncated_final_resume）。
    truncated_output_repairs: int = 0
    # 门槛5 续跑: 本轮模型调用被供应商超时打断且重试救回后，模型零工具调用只回一句承诺
    # 时的轮内续跑次数（至多 1 次，与截断续跑分开计数、互不吃预算）。
    provider_timeout_resume_repairs: int = 0


# LLM: 截断续跑与"分块写纠偏"是两件事：前者针对最终答复被输出上限截断，后者针对工具参数被截断。
#   分开计数，避免互相吃掉预算。
# 函数用途: 增加一次截断续跑计数，同时保留其他修复计数。
def _inc_truncated_output(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        protected_marker_repairs=counters.protected_marker_repairs,
        unresolved_runtime_issue_redirects=counters.unresolved_runtime_issue_redirects,
        protocol_repairs=counters.protocol_repairs,
        empty_text_repairs=counters.empty_text_repairs,
        truncated_write_repairs=counters.truncated_write_repairs,
        truncated_output_repairs=counters.truncated_output_repairs + 1,
        provider_timeout_resume_repairs=counters.provider_timeout_resume_repairs,
    )


# LLM: 超时续跑同样是有界的一次性修复，计数与截断续跑、协议修复、空正文 nudge 全部独立。
# 函数用途: 增加一次"供应商超时重试后模型只承诺不动作"的轮内续跑计数。
def _inc_provider_timeout_resume(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        protected_marker_repairs=counters.protected_marker_repairs,
        unresolved_runtime_issue_redirects=counters.unresolved_runtime_issue_redirects,
        protocol_repairs=counters.protocol_repairs,
        empty_text_repairs=counters.empty_text_repairs,
        truncated_write_repairs=counters.truncated_write_repairs,
        truncated_output_repairs=counters.truncated_output_repairs,
        provider_timeout_resume_repairs=counters.provider_timeout_resume_repairs + 1,
    )


# LLM: 输出上限截断后的续跑指令照抄成熟 harness 的做法（终端交互 query.ts 的
#   "Resume directly — no apology, no recap … break remaining work into smaller pieces"）：
#   明确禁止道歉/复述，要求接着断点写，并把剩余工作拆小；不引入任何"关键词判合格"的机器验收。
# 常量用途: 截断续跑回灌给模型的宿主指令。
_TRUNCATED_OUTPUT_RESUME_LIMIT = 2
_TRUNCATED_OUTPUT_RESUME = (
    "[output-limit-resume]\n"
    "上一条回复被供应商输出上限截断了，不能当作完成。请直接从被截断处接着写："
    "不要道歉、不要复述已经写过的内容、不要总结。"
    "把剩余工作拆成更小的块，每块做完立刻落盘或执行，再继续下一块；"
    "需要调用工具就直接调用。本条指令不指定任何具体工具，也不假定上一轮调用了哪个工具。"
)

# LLM: 供应商超时续跑与输出截断续跑是两件事：前者针对「本轮模型调用被供应商超时打断、
#   门槛5 重试救回来了，但模型只回一句承诺就停」；后者针对「最终答复被输出上限截断」。
#   各自独立计数，任何一方的预算都不能吃掉另一方；门槛5 的重试资格一行都不放宽——
#   这里只处理「重试已经结束之后」，而且只重发模型调用 + 一条宿主指令，绝不重放工具。
# 常量用途: 超时续跑回灌给模型的宿主指令（至多一次）。
_PROVIDER_TIMEOUT_RESUME_LIMIT = 1
_PROVIDER_TIMEOUT_RESUME = (
    "[provider-timeout-resume]\n"
    "上一条回复之前，本轮模型调用已经被供应商超时打断过一次并重试过。"
    "如果本轮工作还没做完，请直接从断点继续把剩余工作做完："
    "不要道歉、不要复述已经做过的步骤、不要承诺「我重新来」。"
    "需要调用工具就直接调用。本条指令不指定任何具体工具，"
    "也不假定上一轮调用了哪个工具；宿主没有重放任何已执行的工具。"
)

# LLM: repair counters 只记录真实协议修复次数，不再承载任何工作风格或检查点提醒状态。
# 函数用途: 增加一次内部工具标记修复计数，同时保留其他错误修复计数。
def _inc_protected_marker(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        protected_marker_repairs=counters.protected_marker_repairs + 1,
        unresolved_runtime_issue_redirects=counters.unresolved_runtime_issue_redirects,
        protocol_repairs=counters.protocol_repairs,
        empty_text_repairs=counters.empty_text_repairs,
        truncated_write_repairs=counters.truncated_write_repairs,
        truncated_output_repairs=counters.truncated_output_repairs,
        provider_timeout_resume_repairs=counters.provider_timeout_resume_repairs,
    )


# LLM: unresolved issue 只允许一次结构化重定向，计数必须与 protected marker 相互独立。
# 函数用途: 增加一次未解决运行错误的修复计数。
def _inc_unresolved_runtime_issue(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        protected_marker_repairs=counters.protected_marker_repairs,
        unresolved_runtime_issue_redirects=counters.unresolved_runtime_issue_redirects + 1,
        protocol_repairs=counters.protocol_repairs,
        empty_text_repairs=counters.empty_text_repairs,
        truncated_write_repairs=counters.truncated_write_repairs,
        truncated_output_repairs=counters.truncated_output_repairs,
        provider_timeout_resume_repairs=counters.provider_timeout_resume_repairs,
    )


def _inc_protocol(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        protected_marker_repairs=counters.protected_marker_repairs,
        unresolved_runtime_issue_redirects=counters.unresolved_runtime_issue_redirects,
        protocol_repairs=counters.protocol_repairs + 1,
        empty_text_repairs=counters.empty_text_repairs,
        truncated_write_repairs=counters.truncated_write_repairs,
        truncated_output_repairs=counters.truncated_output_repairs,
        provider_timeout_resume_repairs=counters.provider_timeout_resume_repairs,
    )


# LLM: 截断分块纠偏与协议修复分开计数：两者预算不能互相吞掉，也不能共用一个上限解释。
# 函数用途: 增加一次"截断长内容写"的分块纠偏计数。
def _inc_truncated_write(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        protected_marker_repairs=counters.protected_marker_repairs,
        unresolved_runtime_issue_redirects=counters.unresolved_runtime_issue_redirects,
        protocol_repairs=counters.protocol_repairs,
        empty_text_repairs=counters.empty_text_repairs,
        truncated_write_repairs=counters.truncated_write_repairs + 1,
        truncated_output_repairs=counters.truncated_output_repairs,
        provider_timeout_resume_repairs=counters.provider_timeout_resume_repairs,
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
        truncated_write_repairs=counters.truncated_write_repairs,
        truncated_output_repairs=counters.truncated_output_repairs,
        provider_timeout_resume_repairs=counters.provider_timeout_resume_repairs,
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
    # R232: 未完成响应仍然整轮零执行，但被丢弃的工具名是供应商事实，先给有界恢复一次机会。
    # 没有它，截断的 write_file 只会退化成通用格式纠偏，现场问题回到"反复重生成又截断"的死循环。
    truncated_write = _truncated_tool_recovery_decision(
        request, _truncated_tool_names_from_response(request.response)
    )
    if truncated_write is not None:
        return truncated_write
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
        + "\n上一轮没有形成可执行工具调用（工具协议格式无效；文本 [TOOL_CALL] 块已随"
        "EXEC-31b 移除，只接受结构化工具调用 tool_use）。请改用结构化工具调用"
        "（tool_use）重新发起，使用 Tool Catalog 里该工具自己的参数名，参数直接平铺；"
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
    # (unfinished, CLI resume_loop 下一轮重发); 其他 violation 保持 blocked
    # fail-closed。格式类 = TOOL_CALL_UNCLOSED + 旧 text 快照的
    # PROTOCOL_VIOLATION(EXEC-31b 起 text 快照只能来自旧数据/配置错误,
    # 纯协议错误); native 流边界守卫(tool_stream/boundary.py)产出的
    # 未闭合块 violations 带 source_protocol=native, 不在降级集合内。
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
                    "任何正文或伪工具块。请改用结构化工具调用(tool_use)重新发起请求。"
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


# LLM: 协议失败审计必须同时携带不可变工具快照身份与有界工具名集合；只做附加落账，任何异常都不得反噬主链。
# 函数用途: 保存模型原始违规、修复次数和本轮真实工具表，便于区分模型越界与底座能力装配错误。
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
    - 解析阶段(text/native adapter)、模型、协议、repair 计数、是否将 break；
    - 本轮冻结工具快照的 hash、allowed/available 名称，区分模型越界与能力装配漂移。
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
        tool_runtime_snapshot = getattr(params, "tool_runtime_snapshot", None)
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
                "tool_runtime_snapshot_hash": str(
                    getattr(tool_runtime_snapshot, "snapshot_hash", "") or ""
                ),
                "allowed_tools": sorted(
                    str(item)
                    for item in tuple(
                        getattr(tool_runtime_snapshot, "allowed_tools", ()) or ()
                    )
                    if str(item).strip()
                ),
                "available_tools": sorted(
                    str(item)
                    for item in tuple(
                        getattr(tool_runtime_snapshot, "available_tool_names", ()) or ()
                    )
                    if str(item).strip()
                ),
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
# 长内容工具：被截断后需要"分块写"这类有界恢复，而不是通用格式纠偏。
_TRUNCATED_LONG_CONTENT_TOOLS = frozenset({"write_file", "apply_patch"})


# LLM: 未完成响应整轮零执行后，恢复入口只能来自供应商结构化事实 response.truncated_tool_names；
# 命中长内容工具时先给一次有界分块纠偏，超过上限直接硬出口，绝不据此生成可执行调用。
# 函数用途: 把截断的工具名接回长内容恢复，避免截断 write 退化成死循环或无声失败。
def _truncated_tool_recovery_decision(
    request: ToolLoopResponseDecisionRequest,
    names: list[str],
) -> ToolLoopResponseDecision | None:
    if not _native_tool_use_active(request.params):
        return None
    if not bool(getattr(request.response, "truncated", False)):
        return None
    if not any(name in _TRUNCATED_LONG_CONTENT_TOOLS for name in names):
        return None
    return _native_truncated_write_decision(request, _truncated_probe_calls(request, names))


def _truncated_tool_names_from_response(response: object) -> list[str]:
    raw = getattr(response, "truncated_tool_names", None) or ()
    if not isinstance(raw, (list, tuple)):
        return []
    names: list[str] = []
    for item in raw:
        name = str(item or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _truncated_probe_calls(
    request: ToolLoopResponseDecisionRequest,
    names: list[str],
) -> list[ToolCall]:
    """只为给恢复上下文提供真实工具名，不是可执行调用：不进入执行列表，参数只带截断标记。

    身份字段沿用本轮 run/turn/attempt，便于审计把恢复指令追到具体尝试；
    参数门、授权与执行路径都不会看到这些对象（本轮整轮零执行）。
    """
    params = request.params
    snapshot = getattr(params, "tool_runtime_snapshot", None)
    run_id = str(getattr(params, "run_id", "") or "") or "truncated-probe"
    turn_id = str(getattr(request, "turn_id", "") or "") or f"{run_id}:truncated-probe"
    attempt_id = str(getattr(params, "attempt_id", "") or getattr(params, "request_id", "") or "") or run_id
    probes: list[ToolCall] = []
    for index, name in enumerate(names, start=1):
        if name not in _TRUNCATED_LONG_CONTENT_TOOLS:
            continue
        # schema_hash 必须取本轮运行快照的真实值：它只是身份字段，不代表该调用被授权执行。
        runtime = snapshot.runtime(name) if snapshot is not None and hasattr(snapshot, "runtime") else None
        schema_hash = str(getattr(getattr(runtime, "model_spec", None), "schema_hash", "") or "")
        if not schema_hash.startswith("sha256:"):
            continue
        probes.append(
            ToolCall(
                call_id=f"truncated-probe-{index}",
                tool_name=name,
                arguments={"truncated": True},
                source_protocol="native",
                schema_hash=schema_hash,
                run_id=run_id,
                turn_id=turn_id,
                attempt_id=attempt_id,
            )
        )
    return probes


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
    # R232: 整轮零执行后不再产生 write_file 失败记录，上限必须按本轮已发生的分块纠偏次数计，
    # 否则该出口永不可达，截断写会退回"反复重生成又截断"的死循环。
    attempts = int(request.counters.truncated_write_repairs) + 1
    if attempts >= _NATIVE_TRUNCATED_WRITE_LOOP_LIMIT:
        return ToolLoopResponseDecision(
            "break",
            _native_truncated_write_loop_break_response(request.response.backend, attempts),
            [],
            request.counters,
        )
    request.params.tool_context.append(_native_truncated_write_recovery_context(calls))
    return ToolLoopResponseDecision("continue", None, [], _inc_truncated_write(request.counters))


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
    unresolved_issue_decision = unresolved_runtime_issue_no_tool_call_decision(
        _unresolved_runtime_issue_request(request)
    )
    if unresolved_issue_decision is not None:
        return _unresolved_runtime_issue_decision(unresolved_issue_decision)
    if not request.has_protected_marker:
        response_text = str(getattr(request.response, "text", "") or "").strip()
        # 门槛5 续跑(真机: 超时后模型只回「我重新来」)：本轮结构化地发生过被重试救回的
        # 供应商超时，而模型这一轮零工具调用、只给了正文 —— 只承诺不动作。旧行为直接
        # break 把承诺当收口，用户看到一句承诺 + 空提示符。这里至多续跑一次。
        timeout_resume = _provider_timeout_resume_decision(request, response_text)
        if timeout_resume is not None:
            return timeout_resume
        reply_required = active_turn_user_reply_required(request.params)
        if reply_required and response_text:
            satisfy_active_turn_user_reply(request.params)
        elif (
            reply_required
            and request.counters.empty_text_repairs < _MAX_EMPTY_TEXT_REPAIRS
        ):
            # This branch is controlled by exact consumed guidance ids. Thinking
            # is intentionally insufficient: users need one ordinary assistant
            # segment before a waiting parent yields again.
            request.params.tool_context.append(_ACTIVE_TURN_EMPTY_REPLY_NUDGE)
            return ToolLoopResponseDecision(
                "continue", None, [], _inc_empty_text(request.counters)
            )
        # 长期助手 式空响应 nudge(参考 长期助手 conversation_loop 6511-6547:
        # "You just executed tool calls but returned an empty response... continue")。
        # 执行过工具后模型空正文无工具调用 = 静默收口:直接 break 会让用户收不到
        # 任何回复(USER_REPLY_UNAVAILABLE)。有界重试(默认 2 次)后仍空 → 诚实失败。
        if (
            not response_text
            and list(getattr(request.params, "executed_tools", None) or [])
            and request.counters.empty_text_repairs < _MAX_EMPTY_TEXT_REPAIRS
        ):
            request.params.tool_context.append(_EMPTY_TEXT_NUDGE)
            return ToolLoopResponseDecision(
                "continue", None, [], _inc_empty_text(request.counters)
            )
        final_response = request.response
        # EXEC-38(owner 拍板 2026-08-16): 程序验证收窄为只做"未知副作用"
        # ——产物/交付目录验证删除(EXEC-06b/34/37 撤销)。理由: 任务千奇百怪,
        # 很多任务没有落盘交付概念; 对照 会话运行时/轻量运行时 均无产出门, 模型自决
        # 何时交付(EXEC-31 edit 回显 + prompt 禁重读已在行为层引导);
        # 防假完成交由"如实报告"合同与 UNKNOWN 安全闸。
        # EXEC-39(2026-09-11): 在子代理 run 里恢复它的**严格子集**——只校验宿主自己声明过的
        # 交付清单(required_file_refs/output_refs…)，主代理用户会话永不生效，且有界放行。
        # 真机证据: 子代理声明了要写 core_*.go，收工时一个都不存在却报完成。
        deliverable_block = deliverable_closeout_block(request.params)
        if deliverable_block:
            request.params.tool_context.append(deliverable_block)
            return ToolLoopResponseDecision("continue", None, [], request.counters)
        if bool(getattr(final_response, "truncated", False)):
            # EXEC-05 长任务真机: 最终答复被供应商输出上限截断(max_tokens/length)不能当作完成交付。
            # R248(2026-09-11 真机): 原来只标记 unfinished 就收口，恢复靠**管理器重启整个 runner 轮次**，
            #   于是同一策略原地打转——两个子代理各被截断 3/4 次、重启 4/5 个 attempt、耗掉 35 分钟才
            #   碰巧跑完。这里改成轮内续跑：先把断点续写指令回灌，同一个 run 再走一轮；超限才按
            #   unfinished 收口，让上游按既有恢复路径处理。
            if request.counters.truncated_output_repairs < _TRUNCATED_OUTPUT_RESUME_LIMIT:
                request.params.tool_context.append(_TRUNCATED_OUTPUT_RESUME)
                return ToolLoopResponseDecision(
                    "continue", None, [], _inc_truncated_output(request.counters)
                )
            final_response = replace(
                final_response,
                runtime_status="unfinished",
                runtime_reason="MODEL_RESPONSE_TRUNCATED",
                runtime_source="tool_loop",
            )
        return ToolLoopResponseDecision("break", final_response, [], request.counters)
    if request.counters.protected_marker_repairs < 1:
        return ToolLoopResponseDecision(
            "continue", None, [], _inc_protected_marker(request.counters)
        )
    final = protected_tool_marker_block_response(request.response.backend)
    return ToolLoopResponseDecision("break", final, [], request.counters)


# LLM: 触发完全结构化：只认「产出这条响应的那次物理模型调用所属的 model turn 序号被
#   生成层登记为『本轮发生过被重试救回的供应商超时』」（live_archive_state 上的结构化
#   字段，与账本同源），绝不读模型正文——把承诺换成任意其它文本，行为完全一致。
#   门槛5 的重试资格判定一行未动：本函数只处理「重试已经结束之后」；能走到这里说明
#   重试那一枪已经打出去了、供应商也真的答了（否则这里拿到的是异常而不是响应）。
#   有界: 每次裁决最多一次，计数与截断续跑互不吃预算；只追加一条宿主指令，不重放工具。
# 函数用途: 本轮「可恢复供应商失败 + 模型零工具调用只承诺」时给出至多一次轮内续跑。
def _provider_timeout_resume_decision(
    request: _NoToolCallsRequest,
    response_text: str,
) -> ToolLoopResponseDecision | None:
    if not response_text:
        # 空正文属于既有 empty-text nudge 的预算，两条路径不互相记账。
        return None
    if request.counters.provider_timeout_resume_repairs >= _PROVIDER_TIMEOUT_RESUME_LIMIT:
        # 续跑预算用尽：按原语义收口（承诺当普通 plain final 返回），不进入无限续跑。
        return None
    from ..tool_model_generation import provider_timeout_resume_eligible

    if not provider_timeout_resume_eligible(request.params):
        return None
    request.params.tool_context.append(_PROVIDER_TIMEOUT_RESUME)
    return ToolLoopResponseDecision(
        "continue", None, [], _inc_provider_timeout_resume(request.counters)
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
