# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""Compatibility facade for runner result processing helpers.

Structured output and output-payload builders live in focused modules.
"""

import json
from dataclasses import asdict
from pathlib import Path

from .models import SubAgentParsedOutput, SubAgentRunnerResult, SubAgentTask
from .parsing import _dict_list
from .policies import _status_from_structured_output, _verification_from_runner_status
from .reports import AcceptanceReviewFinding
from .result_contexts import OutputPayloadContext, RunnerResultContext
from .result_debrief import _append_runner_debrief_content
from .result_payloads import _build_output_payload
from .result_structured import (
    _create_capability_requests_from_parsed,
    _create_evidence_from_parsed,
    _merge_actual_tools,
    _merge_task_tools,
    _normalize_parsed_fields,
    _process_evidence_items,
    _process_structured_output,
    _split_tools_and_skills,
)


# LLM: _build_runner_result 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 构建执行器结果所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
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


# LLM: _write_runner_result_files 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 写入执行器结果文件的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
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

    if prompt:
        Path(task.runner_prompt_file).write_text(prompt, encoding="utf-8")
    if response:
        Path(task.runner_response_file).write_text(response, encoding="utf-8")
    Path(task.output_json).write_text(
        json.dumps(output_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    Path(task.runner_result_json).write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


__all__ = [
    "AcceptanceReviewFinding",
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
    "_status_from_structured_output",
    "_verification_from_runner_status",
    "_write_runner_result_files",
]
