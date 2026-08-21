
from __future__ import annotations

"""Runner result processing helpers."""

import time
from dataclasses import asdict
from pathlib import Path

from ..common.json_io import write_json_file_atomic, write_text_file_atomic
from .models import SubAgentParsedOutput, SubAgentRunnerResult, SubAgentTask
from .parsing import _dict_list
from .policies import (
    RunnerNextActionParams,
    _runner_next_action,
    _status_from_structured_output,
    _verification_from_runner_status,
)
from .result_contexts import OutputPayloadContext, RunnerResultContext
from .result_structured import (
    _create_capability_requests_from_parsed,
    _create_evidence_from_parsed,
    _merge_actual_tools,
    _merge_task_tools,
    _normalize_parsed_fields,
    _process_structured_output,
    _split_tools_and_skills,
    merge_actual_tools_for_unparsed,
)
from .result_structured_evidence import process_evidence_items as _process_evidence_items
from .runner_rendering import _render_runner_item_line


def _build_structured_output_payload(ctx: OutputPayloadContext) -> dict[str, object]:
    return {
        "found": ctx.parsed.found,
        "ok": ctx.parsed.ok,
        "parse_error": ctx.parsed.parse_error,
        "repair_attempted": ctx.structured_repair_attempted,
        "repair_ok": ctx.structured_repair_ok,
        "repair_error": ctx.structured_repair_error,
        "summary": ctx.parsed.summary,
        "status": ctx.parsed.status,
        "blocked_reason": ctx.parsed.blocked_reason,
        "failure_type": ctx.parsed.failure_type,
        "evidence_count": ctx.structured_evidence_count,
        "capability_request_count": ctx.structured_request_count,
        "capability_request_ids": ctx.created_request_ids,
        "coverage_records": ctx.parsed.coverage_records,
        "artifact_count": len(ctx.artifacts),
        "test_count": len(ctx.tests),
        "patch_count": len(ctx.patches),
        "lesson_count": len(ctx.lessons),
        "actual_tools": ctx.actual_tools or [],
        "ignored_unauthorized_tools": ctx.ignored_tools,
        "ignored_unauthorized_skills": ctx.ignored_skills,
    }


def _build_output_payload(ctx: OutputPayloadContext) -> dict[str, object]:
    task = ctx.task
    return {
        "run_id": task.id,
        "dry_run": ctx.dry_run,
        "ok": ctx.ok,
        "status": task.status,
        "turn_end_reason": ctx.turn_end_reason,
        "message": ctx.message,
        "backend": ctx.backend,
        "tool_rounds": ctx.tool_rounds,
        "runner_attempts": task.runner_attempts,
        "runner_last_error": task.runner_last_error,
        "response": "",
        "structured_output": _build_structured_output_payload(ctx),
        "used_tools": task.used_tools,
        "used_skills": task.used_skills,
        "artifacts": ctx.artifacts,
        "evidence_packets": ctx.evidence_packets,
        "findings": ctx.findings,
        "tests": ctx.tests,
        "patches": ctx.patches,
        "evidence_summaries": [item.summary for item in task.evidence],
        "lessons": ctx.lessons,
        "blockers": ctx.blockers,
        "next_action": _runner_next_action(
            params=RunnerNextActionParams(
                dry_run=ctx.dry_run,
                ok=ctx.ok,
                status=task.status,
                capability_request_count=ctx.structured_request_count,
                next_actions=ctx.next_actions,
            ),
        ),
        "next_actions": ctx.next_actions,
        "created_at": ctx.now,
    }


def _build_runner_result(ctx: RunnerResultContext) -> SubAgentRunnerResult:
    """Build the SubAgentRunnerResult dataclass from processed fields."""

    task = ctx.task
    return SubAgentRunnerResult(
        run_id=task.id,
        dry_run=ctx.dry_run,
        ok=ctx.ok,
        status=task.status,
        verification_status=task.verification_status,
        message=ctx.message,
        turn_end_reason=ctx.turn_end_reason,
        backend=ctx.backend,
        tool_rounds=ctx.tool_rounds,
        runner_attempts=task.runner_attempts,
        runner_last_error=task.runner_last_error,
        execution_context_json=task.execution_context_json,
        execution_context_file=task.execution_context_file,
        prompt_file=task.runner_prompt_file if ctx.prompt else "",
        response_file=task.runner_response_file if ctx.response else "",
        result_file=task.runner_result_file,
        result_json=task.runner_result_json,
        output_json=task.output_json,
        structured_output_found=ctx.parsed.found,
        structured_output_ok=ctx.parsed.ok,
        structured_parse_error=ctx.parsed.parse_error,
        structured_repair_attempted=ctx.structured_repair_attempted,
        structured_repair_ok=ctx.structured_repair_ok,
        structured_repair_error=ctx.structured_repair_error,
        structured_summary=ctx.parsed.summary,
        evidence_count=ctx.structured_evidence_count,
        capability_request_count=ctx.structured_request_count,
        artifact_count=ctx.artifact_count,
        test_count=ctx.test_count,
        patch_count=ctx.patch_count,
        lesson_count=ctx.lesson_count,
        blocked_reason=ctx.parsed.blocked_reason,
        created_at=ctx.now,
    )


def _write_runner_result_files(
    task: SubAgentTask,
    result: SubAgentRunnerResult,
    output_payload: dict[str, object],
    *,
    params: object | None = None,
    prompt: str,
    response: str,
) -> None:
    """Write runner result JSON, markdown, and prompt/response files."""

    # 原子写(temp+replace,同短板4):runner 结果"半写即损坏",崩溃中断不能留半截
    # JSON/文本文件,否则下一轮聚合读到坏文件。sort_keys=False 保持既有字段顺序。
    if prompt:
        write_text_file_atomic(Path(task.runner_prompt_file), prompt)
    if response:
        write_text_file_atomic(Path(task.runner_response_file), response)
    write_json_file_atomic(Path(task.output_json), output_payload, sort_keys=False)
    write_json_file_atomic(Path(task.runner_result_json), asdict(result), sort_keys=False)


def _append_runner_debrief_content(
    task: SubAgentTask,
    parsed: SubAgentParsedOutput,
) -> None:
    """Append structured runner output sections to the task DEBRIEF file."""

    sections: list[str] = []
    if parsed.artifacts:
        sections.append("## Runner Artifacts")
        sections.extend(_render_runner_item_line(item) for item in parsed.artifacts)
    if parsed.tests:
        sections.append("## Runner Tests")
        sections.extend(_render_runner_item_line(item) for item in parsed.tests)
    if parsed.patches:
        sections.append("## Runner Patches")
        sections.extend(_render_runner_item_line(item) for item in parsed.patches)
    if parsed.lessons:
        sections.append("## Runner Lessons")
        sections.extend(f"- {item}" for item in parsed.lessons)
    if parsed.next_actions:
        sections.append("## Runner Next Actions")
        sections.extend(f"- {item}" for item in parsed.next_actions)
    if not sections:
        return

    path = Path(task.debrief_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("# DEBRIEF\n\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n## Runner Structured Output\n\n")
        handle.write(f"- created_at: {time.time()}\n")
        handle.write(f"- run_id: {task.id}\n\n")
        handle.write("\n\n".join(sections))
        handle.write("\n")


__all__ = [
    "OutputPayloadContext",
    "RunnerResultContext",
    "SubAgentParsedOutput",
    "_append_runner_debrief_content",
    "_build_output_payload",
    "_build_runner_result",
    "_create_capability_requests_from_parsed",
    "_create_evidence_from_parsed",
    "_dict_list",
    "_merge_actual_tools",
    "_merge_task_tools",
    "_normalize_parsed_fields",
    "_process_evidence_items",
    "_process_structured_output",
    "_split_tools_and_skills",
    "merge_actual_tools_for_unparsed",
    "_status_from_structured_output",
    "_verification_from_runner_status",
    "_write_runner_result_files",
]
