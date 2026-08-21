
from __future__ import annotations

from .models import SubAgentRunnerResult


def render_runner_result_markdown(result: SubAgentRunnerResult) -> str:
    status = "OK" if result.ok else "FAIL"
    mode = "dry-run" if result.dry_run else "execute"
    lines = _runner_result_header_lines(result, mode, status)
    lines.extend(_runner_structured_output_lines(result))
    lines.extend(_runner_result_file_lines(result))
    return "\n".join(lines) + "\n"


def _runner_result_header_lines(
    result: SubAgentRunnerResult,
    mode: str,
    status: str,
) -> list[str]:
    return [
        "# SUBAGENT RUNNER RESULT",
        "",
        f"- run_id: {result.run_id}",
        f"- created_at: {result.created_at}",
        f"- mode: {mode}",
        f"- status: {result.status}",
        f"- turn_end_reason: {result.turn_end_reason or 'none'}",
        f"- ok: {status}",
        f"- backend: {result.backend or 'none'}",
        f"- tool_rounds: {result.tool_rounds}",
        f"- runner_attempts: {result.runner_attempts}",
        f"- runner_last_error: {result.runner_last_error or 'none'}",
        f"- evidence_count: {result.evidence_count}",
        f"- capability_request_count: {result.capability_request_count}",
        f"- artifact_count: {result.artifact_count}",
        f"- test_count: {result.test_count}",
        f"- patch_count: {result.patch_count}",
        f"- lesson_count: {result.lesson_count}",
        "",
        "## Message",
        "",
        result.message or "none",
    ]


def _runner_structured_output_lines(result: SubAgentRunnerResult) -> list[str]:
    if result.structured_summary or result.blocked_reason or result.structured_parse_error:
        lines = ["", "## Structured Output", ""]
        if result.structured_summary:
            lines.append(f"- summary: {result.structured_summary}")
        if result.blocked_reason:
            lines.append(f"- blocked_reason: {result.blocked_reason}")
        if result.structured_parse_error:
            lines.append(f"- parse_error: {result.structured_parse_error}")
        if result.structured_repair_error:
            lines.append(f"- repair_error: {result.structured_repair_error}")
        return lines
    return []


def _runner_result_file_lines(result: SubAgentRunnerResult) -> list[str]:
    return [
        "",
        "## Files",
        "",
        f"- execution_context_json: {result.execution_context_json}",
        f"- execution_context_file: {result.execution_context_file}",
        f"- prompt_file: {result.prompt_file or 'none'}",
        f"- response_file: {result.response_file or 'none'}",
        f"- result_json: {result.result_json}",
        f"- output_json: {result.output_json}",
    ]


__all__ = ["render_runner_result_markdown"]
