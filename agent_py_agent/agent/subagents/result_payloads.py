from __future__ import annotations

"""Payload assembly for subagent runner result files."""

from .policies import RunnerNextActionParams, _runner_next_action
from .result_contexts import OutputPayloadContext


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
        "evidence_count": ctx.structured_evidence_count,
        "capability_request_count": ctx.structured_request_count,
        "capability_request_ids": ctx.created_request_ids,
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
        "verification_status": task.verification_status,
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
        # LLM: output.json keeps traceable claim data beside legacy artifacts/tests.
        "evidence_packets": ctx.evidence_packets,
        "findings": ctx.findings,
        "tests": ctx.tests,
        "patches": ctx.patches,
        "acceptance": [item.summary for item in task.evidence],
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
