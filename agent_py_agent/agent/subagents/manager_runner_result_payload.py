from __future__ import annotations

"""Helpers for applying runner status and assembling runner result payloads."""

from dataclasses import dataclass, field

from .models import SubAgentParsedOutput, SubAgentTask
from .result_contexts import OutputPayloadContext
from .result_processors import _build_output_payload
from .runner_result_state import apply_runner_result_fields


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
    verification_status: str = ""
    failure_type: str = ""
    structured_output: SubAgentParsedOutput | None = None
    actual_tools: list[str] | None = None
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
    tests: list
    patches: list
    lessons: list
    next_actions: list


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
    if not ok or status in {"BLOCKED", "FAILED", "CHANNEL_ERROR", "TIMEOUT"}:
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


def _runner_make_runner_meta(dry_run, ok, message, backend, tool_rounds, now):
    """Build runner metadata dict."""
    return {
        "dry_run": dry_run,
        "ok": ok,
        "message": message,
        "backend": backend,
        "tool_rounds": tool_rounds,
        "now": now,
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
        tests=extracted.tests,
        patches=extracted.patches,
        lessons=extracted.lessons,
        blockers=blockers,
        next_actions=extracted.next_actions,
        structured_repair_attempted=cap_data.get("structured_repair_attempted", False),
        structured_repair_ok=cap_data.get("structured_repair_ok", False),
        structured_repair_error=cap_data.get("structured_repair_error", ""),
        now=runner_meta["now"],
    )


def _build_context_params(
    params: RecordRunnerResultParams,
    extracted: _ApplyStatusParams,
    output_payload: dict,
    state: dict,
) -> _BuildContextParams:
    """Bundle fields for final BuildAndPersistContext construction."""
    return _BuildContextParams(
        task=extracted.task,
        dry_run=params.dry_run,
        final_ok=state["final_ok"],
        final_message=state["final_message"],
        parsed=extracted.parsed,
        output_payload=output_payload,
        structured_evidence_count=extracted.structured_evidence_count,
        structured_request_count=extracted.structured_request_count,
        artifacts=extracted.artifacts,
        tests=extracted.tests,
        patches=extracted.patches,
        lessons=extracted.lessons,
        blockers=state["blockers"],
        next_actions=extracted.next_actions,
        now=state["now"],
    )


def apply_status_and_build_payload(
    params: RecordRunnerResultParams,
    extracted: _ApplyStatusParams,
    now: float,
) -> tuple[dict, BuildAndPersistContext]:
    """Apply status to task and build output payload."""
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
    }
    apply_runner_result_fields(extracted.task, result_meta, status_context, extracted.parsed, now)
    final_ok = result_meta["ok"]
    final_message = result_meta["message"]
    blockers = _runner_compute_blockers(final_ok, extracted.task.status, extracted.parsed, final_message)
    runner_meta = _runner_make_runner_meta(params.dry_run, final_ok, final_message, params.backend, params.tool_rounds, now)
    output_payload = _build_output_payload(_output_payload_context(extracted, runner_meta, cap_data, blockers))
    build_state = {"final_ok": final_ok, "final_message": final_message, "blockers": blockers, "now": now}
    return output_payload, _make_build_context(
        _build_context_params(params, extracted, output_payload, build_state)
    )
