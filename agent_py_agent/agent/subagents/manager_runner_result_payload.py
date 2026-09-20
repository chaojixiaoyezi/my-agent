# LLM: runner 状态写回与结果载荷共用宿主结束协议；显式 reason 须传至失败分类，联测自然让出、异常和恢复。
# 模块用途: 组装子代理本轮状态和持久结果，不从模型正文推断成功、失败或继续。
from __future__ import annotations

"""Helpers for applying runner status and assembling runner result payloads."""

from dataclasses import dataclass, field

from ..turn_end import infer_turn_end_reason
from .models import (
    SUBAGENT_FAILURE_STATUSES,
    SubAgentParsedOutput,
    SubAgentTask,
    TaskStatus,
    task_status_in,
)
from .result_contexts import OutputPayloadContext
from .result_processors import _build_output_payload
from .runner_result_state import RunnerResultFieldParams, apply_runner_result_fields


@dataclass
class RecordRunnerResultParams:
    """Parameters for record_runner_result (18 fields replacing 18 positional params)."""

    run_id: str
    dry_run: bool
    ok: bool
    message: str
    attempt_id: str = ""
    prompt: str = ""
    response: str = ""
    backend: str = ""
    tool_rounds: int = 0
    status: str = ""
    turn_end_reason: str = ""
    verification_status: str = ""
    failure_type: str = ""
    structured_output: SubAgentParsedOutput | None = None
    actual_tools: list[str] | None = None
    # 系统级工具失败账本(A1):来自 archive_tool_calls 的 ok=False 摘要。
    # None=本轮拿不到系统数据(超时/worker 异常),[]=系统确认零工具失败。
    tool_failures: list[dict[str, str]] | None = None
    structured_repair_attempted: bool = False
    structured_repair_ok: bool = False
    structured_repair_error: str = ""


@dataclass
class BuildAndPersistContext:
    """Bundle for _build_and_persist_result to reduce parameter count."""

    task: SubAgentTask
    params: RecordRunnerResultParams
    final_ok: bool
    final_message: str
    parsed: SubAgentParsedOutput
    output_payload: dict
    structured_evidence_count: int
    structured_request_count: int
    artifacts: list
    evidence_packets: list
    findings: list
    tests: list
    patches: list
    lessons: list
    blockers: list
    next_actions: list
    now: float


@dataclass
class _ApplyStatusParams:
    """Bundle for _apply_status_and_build_payload to reduce parameter count."""

    task: SubAgentTask
    parsed: SubAgentParsedOutput
    structured_evidence_count: int
    structured_request_count: int
    created_request_ids: list[str]
    structured_repair_attempted: bool
    structured_repair_ok: bool
    structured_repair_error: str
    actual_tools: list[str] | None
    ignored_tools: list[str]
    ignored_skills: list[str]
    artifacts: list
    evidence_packets: list
    findings: list
    tests: list
    patches: list
    lessons: list
    next_actions: list
    turn_end_reason: str


@dataclass
class _BuildContextParams:
    """Bundle for _make_build_context to reduce parameter count."""

    task: SubAgentTask
    dry_run: bool
    final_ok: bool
    final_message: str
    parsed: SubAgentParsedOutput
    output_payload: dict
    structured_evidence_count: int
    structured_request_count: int
    artifacts: list
    evidence_packets: list
    findings: list
    tests: list
    patches: list
    lessons: list
    blockers: list
    next_actions: list
    now: float


@dataclass
class _ExtractedOutput:
    """Bundle for _extract_parsed_output result tuple."""

    parsed: SubAgentParsedOutput
    ignored_tools: list[str] = field(default_factory=list)
    ignored_skills: list[str] = field(default_factory=list)
    structured_evidence_count: int = 0
    structured_request_count: int = 0
    created_request_ids: list[str] = field(default_factory=list)
    artifacts: list = field(default_factory=list)
    evidence_packets: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    tests: list = field(default_factory=list)
    patches: list = field(default_factory=list)
    lessons: list = field(default_factory=list)
    next_actions: list = field(default_factory=list)


@dataclass
class _CapDataParams:
    """Bundle for _runner_make_cap_data to reduce parameter count."""

    parsed: SubAgentParsedOutput
    structured_evidence_count: int
    structured_request_count: int
    created_request_ids: list[str]
    structured_repair_attempted: bool
    structured_repair_ok: bool
    structured_repair_error: str


def _runner_compute_blockers(ok, status, parsed, message):
    """Compute blockers list based on task state."""
    del ok
    if task_status_in(status, SUBAGENT_FAILURE_STATUSES):
        return [parsed.blocked_reason or message]
    return []


def _runner_make_result_meta(ok, message, response, dry_run):
    """Build result metadata dict."""
    return {"ok": ok, "message": message, "response": response, "dry_run": dry_run}


def _runner_make_cap_data(params: _CapDataParams):
    """Build capability data dict."""
    return {
        "parsed": params.parsed,
        "structured_evidence_count": params.structured_evidence_count,
        "structured_request_count": params.structured_request_count,
        "created_request_ids": params.created_request_ids,
        "structured_repair_attempted": params.structured_repair_attempted,
        "structured_repair_ok": params.structured_repair_ok,
        "structured_repair_error": params.structured_repair_error,
    }


@dataclass
class _RunnerMetaParams:

    dry_run: bool
    ok: bool
    message: str
    backend: str
    tool_rounds: int
    now: float


@dataclass
class _ContextBuildRequest:

    params: RecordRunnerResultParams
    extracted: _ApplyStatusParams
    output_payload: dict
    state: dict


def _runner_make_runner_meta(params: _RunnerMetaParams):
    """Build runner metadata dict."""
    return {
        "dry_run": params.dry_run,
        "ok": params.ok,
        "message": params.message,
        "backend": params.backend,
        "tool_rounds": params.tool_rounds,
        "now": params.now,
    }


def _make_build_context(p: _BuildContextParams) -> BuildAndPersistContext:
    """Build BuildAndPersistContext from computed values."""
    return BuildAndPersistContext(
        task=p.task,
        params=RecordRunnerResultParams(run_id=p.task.id, dry_run=p.dry_run, ok=p.final_ok, message=p.final_message),
        final_ok=p.final_ok,
        final_message=p.final_message,
        parsed=p.parsed,
        output_payload=p.output_payload,
        structured_evidence_count=p.structured_evidence_count,
        structured_request_count=p.structured_request_count,
        artifacts=p.artifacts,
        evidence_packets=p.evidence_packets,
        findings=p.findings,
        tests=p.tests,
        patches=p.patches,
        lessons=p.lessons,
        blockers=p.blockers,
        next_actions=p.next_actions,
        now=p.now,
    )


def _output_payload_context(extracted: _ApplyStatusParams, runner_meta: dict, cap_data: dict, blockers: list) -> OutputPayloadContext:
    """Build the payload context after task status has been applied."""
    return OutputPayloadContext(
        task=extracted.task,
        dry_run=runner_meta["dry_run"],
        ok=runner_meta["ok"],
        message=runner_meta["message"],
        backend=runner_meta["backend"],
        tool_rounds=runner_meta["tool_rounds"],
        parsed=extracted.parsed,
        actual_tools=extracted.actual_tools,
        structured_evidence_count=extracted.structured_evidence_count,
        structured_request_count=extracted.structured_request_count,
        created_request_ids=cap_data["created_request_ids"],
        ignored_tools=extracted.ignored_tools,
        ignored_skills=extracted.ignored_skills,
        artifacts=extracted.artifacts,
        evidence_packets=extracted.evidence_packets,
        findings=extracted.findings,
        tests=extracted.tests,
        patches=extracted.patches,
        lessons=extracted.lessons,
        blockers=blockers,
        next_actions=extracted.next_actions,
        turn_end_reason=extracted.turn_end_reason,
        structured_repair_attempted=cap_data.get("structured_repair_attempted", False),
        structured_repair_ok=cap_data.get("structured_repair_ok", False),
        structured_repair_error=cap_data.get("structured_repair_error", ""),
        now=runner_meta["now"],
    )


def _build_context_params(request: _ContextBuildRequest) -> _BuildContextParams:
    """Bundle fields for final BuildAndPersistContext construction."""
    params = request.params
    extracted = request.extracted
    state = request.state
    return _BuildContextParams(
        task=extracted.task,
        dry_run=params.dry_run,
        final_ok=state["final_ok"],
        final_message=state["final_message"],
        parsed=extracted.parsed,
        output_payload=request.output_payload,
        structured_evidence_count=extracted.structured_evidence_count,
        structured_request_count=extracted.structured_request_count,
        artifacts=extracted.artifacts,
        evidence_packets=extracted.evidence_packets,
        findings=extracted.findings,
        tests=extracted.tests,
        patches=extracted.patches,
        lessons=extracted.lessons,
        blockers=state["blockers"],
        next_actions=extracted.next_actions,
        now=state["now"],
    )


# LLM: 显式宿主 reason 参与失败分类，推断值仅沿原载荷协议；先落状态再组装，授权阻塞仍优先，联测等待和错误写回。
# 函数用途: 把本轮结束事实交给任务状态写回，再组装持久结果；正常让出不能被未完成布尔误当失败。
def apply_status_and_build_payload(
    params: RecordRunnerResultParams,
    extracted: _ApplyStatusParams,
    now: float,
) -> tuple[dict, BuildAndPersistContext]:
    """Apply host-owned turn outcome to task state and build its payload."""
    result_meta = _runner_make_result_meta(params.ok, params.message, params.response, params.dry_run)
    cap_data = _runner_make_cap_data(
        _CapDataParams(
            parsed=extracted.parsed,
            structured_evidence_count=extracted.structured_evidence_count,
            structured_request_count=extracted.structured_request_count,
            created_request_ids=extracted.created_request_ids,
            structured_repair_attempted=extracted.structured_repair_attempted,
            structured_repair_ok=extracted.structured_repair_ok,
            structured_repair_error=extracted.structured_repair_error,
        )
    )
    status_context = {
        "status": params.status,
        "verification_status": params.verification_status,
        "failure_type": params.failure_type,
        "turn_end_reason": params.turn_end_reason,
        "actual_tools": list(params.actual_tools or []),
    }
    turn_end_reason = infer_turn_end_reason(
        explicit=params.turn_end_reason,
        runtime_status=_runtime_status_from_runner_result(params),
    )
    apply_runner_result_fields(
        RunnerResultFieldParams(extracted.task, result_meta, status_context, extracted.parsed, now)
    )
    turn_end_reason = _effective_turn_end_reason(extracted.task, turn_end_reason)
    extracted.task.turn_end_reason = turn_end_reason
    extracted.turn_end_reason = turn_end_reason
    params.turn_end_reason = turn_end_reason
    final_ok = result_meta["ok"]
    final_message = result_meta["message"]
    blockers = _runner_compute_blockers(final_ok, extracted.task.status, extracted.parsed, final_message)
    # 仅传递已有运行阻塞（权限、通道、工具错误）；不在这里生成质量验收 blocker。
    for blocker in getattr(extracted.task, "blockers", []) or []:
        if blocker not in blockers:
            blockers.append(blocker)
    runner_meta = _runner_make_runner_meta(
        _RunnerMetaParams(params.dry_run, final_ok, final_message, params.backend, params.tool_rounds, now)
    )
    output_payload = _build_output_payload(_output_payload_context(extracted, runner_meta, cap_data, blockers))
    build_state = {"final_ok": final_ok, "final_message": final_message, "blockers": blockers, "now": now}
    return output_payload, _make_build_context(
        _build_context_params(_ContextBuildRequest(params, extracted, output_payload, build_state))
    )


def _runtime_status_from_runner_result(params: RecordRunnerResultParams) -> str:
    """Map the existing runner status to turn-end runtime facts without reading prose."""

    status = str(params.status or "").strip().upper()
    if status == TaskStatus.BLOCKED.value:
        return "blocked"
    if status in {TaskStatus.CANCELLED.value, TaskStatus.ABANDONED.value}:
        return "cancelled"
    if status in {
        TaskStatus.FAILED.value,
        TaskStatus.TIMEOUT.value,
        TaskStatus.CHANNEL_ERROR.value,
    }:
        return "error"
    return "ok" if params.ok else "interrupted"


# LLM: An OPEN capability request is host-owned lifecycle state. Projecting it
# as blocked corrects only the turn reason; it does not inspect or grade output.
# 函数用途: 防止通用 provider completed 覆盖已落盘的待授权阻塞。
def _effective_turn_end_reason(task: object, inferred: str) -> str:
    if (
        str(getattr(task, "status", "") or "").strip() == TaskStatus.BLOCKED.value
        and str(getattr(task, "failure_type", "") or "").strip() == "capability_request"
    ):
        return "blocked"
    return inferred
