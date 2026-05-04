from __future__ import annotations

"""LLM: result processing helpers for runner output, evidence, and capability requests.

给人看的解释：
这些函数各自处理 runner 结果的一部分——结构化输出里的证据、能力请求、工具使用记录等。
主 mixin 只需要调用这些函数，避免 record_runner_result 方法过长。
"""

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .models import (
    CapabilityRequest,
    SubAgentParsedOutput,
    SubAgentRunnerResult,
    SubAgentTask,
    VerificationEvidence,
)
from .parsing import (
    _dict_list,
    _normalize_runner_items,
    _split_allowed_items,
    _string_dict,
    _string_list,
)
from .policies import (
    _runner_next_action,
    _status_from_structured_output,
    _verification_from_runner_status,
)
from .reports import AcceptanceReviewFinding
from .runner_rendering import _render_runner_item_line
from .utils import _merge_list, _new_id


@dataclass(frozen=True)
class OutputPayloadContext:
    """Bundle of all _build_output_payload parameters into a single object."""
    task: SubAgentTask
    dry_run: bool
    ok: bool
    message: str
    backend: str
    tool_rounds: int
    parsed: SubAgentParsedOutput
    actual_tools: list[str] | None
    structured_evidence_count: int
    structured_request_count: int
    created_request_ids: list[str]
    ignored_tools: list[str]
    ignored_skills: list[str]
    artifacts: list[dict[str, Any]]
    tests: list[dict[str, Any]]
    patches: list[dict[str, Any]]
    lessons: list[str]
    blockers: list[str]
    next_actions: list[str]
    structured_repair_attempted: bool
    structured_repair_ok: bool
    structured_repair_error: str
    now: float


@dataclass(frozen=True)
class RunnerResultContext:
    """Bundle of all _build_runner_result parameters into a single object."""
    task: SubAgentTask
    dry_run: bool
    ok: bool
    message: str
    backend: str
    tool_rounds: int
    prompt: str
    response: str
    parsed: SubAgentParsedOutput
    structured_repair_attempted: bool
    structured_repair_ok: bool
    structured_repair_error: str
    structured_evidence_count: int
    structured_request_count: int
    artifact_count: int
    test_count: int
    patch_count: int
    lesson_count: int
    now: float


def _merge_actual_tools(task, actual_tools, used_tools, allowed_tools, parsed_used_tools, now):
    """Merge actual tools into task.used_tools and add evidence for missing ones.

    Args:
        task: the task object (modified in place)
        actual_tools: the actual executed tools list (may be empty)
        used_tools: tools from structured output that were in allowed set
        allowed_tools: set of allowed tool names
        parsed_used_tools: ALL tools from structured output (before filtering)
        now: timestamp
    """
    actual_allowed_tools = [item for item in actual_tools if item in allowed_tools]
    task.used_tools = _merge_list(task.used_tools, actual_allowed_tools)
    # When actual_tools is empty, fall back to treating ALL structured output tools as ignored
    # since there's no execution proof to validate any of them.
    if not actual_tools:
        ignored_from_structured = list(parsed_used_tools)
        return ignored_from_structured
    ignored_tools = [item for item in task.used_tools if item not in actual_allowed_tools]
    for tool_name in actual_allowed_tools:
        if not any(
            item.ok
            and (
                item.kind == tool_name
                or item.command == tool_name
                or item.command.startswith(f"{tool_name} ")
            )
            for item in task.evidence
        ):
            task.evidence.append(
                VerificationEvidence(
                    kind=tool_name,
                    summary=f"系统记录 runner 实际执行过 {tool_name}。",
                    command=tool_name,
                    ok=True,
                    created_at=now,
                )
            )
    return ignored_tools


def _create_evidence_from_parsed(parsed, now):
    """Create VerificationEvidence items from parsed structured output."""
    count = 0
    for item in parsed.evidence:
        summary = str(item.get("summary", "")).strip()
        if not summary:
            continue
        count += 1
    return count


def _create_capability_requests_from_parsed(task, parsed, now):
    """Create CapabilityRequest items from parsed structured output."""
    count = 0
    created_ids = []
    for item in parsed.capability_requests:
        problem = str(item.get("problem", "")).strip()
        needed = str(item.get("needed_capability", "")).strip()
        if not problem or not needed:
            continue
        request = CapabilityRequest(
            id=_new_id("capreq"),
            from_run_id=task.id,
            problem=problem,
            needed_capability=needed,
            expected_output=str(item.get("expected_output", "") or ""),
            tried=_string_list(item.get("tried", [])),
            evidence=_string_list(item.get("evidence", [])),
            constraints=_string_dict(item.get("constraints", {})),
            created_at=now,
        )
        task.capability_requests.append(request)
        created_ids.append(request.id)
        count += 1
    return count, created_ids


def _process_structured_output(
    task: SubAgentTask,
    parsed: SubAgentParsedOutput,
    now: float,
    actual_tools: list[str] | None,
) -> dict[str, object]:
    """LLM: extract evidence, capability requests, tool usage from structured output.

    新手说明:
    当 runner 的模型回复能被解析成结构化输出时，这个函数负责：
    - 拆分授权/未授权的工具和 skill
    - 把结构化证据追加到 task.evidence
    - 把能力请求创建并追加到 task.capability_requests
    - 返回一个字典包含所有中间结果，供 record_runner_result 使用。
    """

    allowed_tools = set(task.allowed_tools)
    allowed_skills = set(task.allowed_skills)
    used_tools, ignored_tools = _split_allowed_items(parsed.used_tools, allowed_tools)
    used_skills, ignored_skills = _split_allowed_items(parsed.used_skills, allowed_skills)

    structured_evidence_count = 0
    structured_request_count = 0
    created_request_ids: list[str] = []
    artifacts = _normalize_runner_items(parsed.artifacts)
    tests = _normalize_runner_items(parsed.tests)
    patches = _normalize_runner_items(parsed.patches)
    lessons = parsed.lessons
    next_actions = parsed.next_actions

    if actual_tools is not None:
        ignored_tools = _merge_actual_tools(task, actual_tools, used_tools, allowed_tools, parsed.used_tools, now)
    else:
        task.used_tools = _merge_list(task.used_tools, used_tools)
    task.used_skills = _merge_list(task.used_skills, used_skills)

    for item in parsed.evidence:
        summary = str(item.get("summary", "")).strip()
        if not summary:
            continue
        task.evidence.append(
            VerificationEvidence(
                kind=str(item.get("kind", "note") or "note"),
                summary=summary,
                command=str(item.get("command", "") or ""),
                path=str(item.get("path", "") or ""),
                url=str(item.get("url", "") or ""),
                ok=bool(item.get("ok", True)),
                created_at=now,
            )
        )
        structured_evidence_count += 1

    structured_request_count, created_request_ids = _create_capability_requests_from_parsed(task, parsed, now)

    return {
        "ignored_tools": ignored_tools,
        "ignored_skills": ignored_skills,
        "structured_evidence_count": structured_evidence_count,
        "structured_request_count": structured_request_count,
        "created_request_ids": created_request_ids,
        "artifacts": artifacts,
        "tests": tests,
        "patches": patches,
        "lessons": lessons,
        "next_actions": next_actions,
    }


def _build_output_payload(ctx: OutputPayloadContext) -> dict[str, object]:
    """LLM: assemble the output.json payload for a runner result.

    新手说明:
    把 runner 结果的所有字段组装成一个字典，写入 task 的 output.json。
    这个函数只管拼数据，不管写文件。
    """
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
        "structured_output": {
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
        },
        "used_tools": task.used_tools,
        "used_skills": task.used_skills,
        "artifacts": ctx.artifacts,
        "tests": ctx.tests,
        "patches": ctx.patches,
        "acceptance": [item.summary for item in task.evidence],
        "lessons": ctx.lessons,
        "blockers": ctx.blockers,
        "next_action": _runner_next_action(
            dry_run=ctx.dry_run,
            ok=ctx.ok,
            status=task.status,
            capability_request_count=ctx.structured_request_count,
            next_actions=ctx.next_actions,
        ),
        "next_actions": ctx.next_actions,
        "created_at": ctx.now,
    }


def _build_runner_result(ctx: RunnerResultContext) -> SubAgentRunnerResult:
    """LLM: build the SubAgentRunnerResult dataclass from processed fields.

    新手说明:
    用处理后的字段组装 SubAgentRunnerResult 数据对象，后续写入 runner_result_json。
    """
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


def _write_runner_result_files(
    task: SubAgentTask,
    result: SubAgentRunnerResult,
    output_payload: dict[str, object],
    *,
    prompt: str,
    response: str,
) -> None:
    """LLM: write runner result JSON, markdown, and prompt/response files.

    新手说明:
    把 runner 结果写入三个文件：output.json（完整结果）、runner_result.json（数据类序列化）、
    runner_result.md（人类可读的 markdown）。
    """

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


def _append_runner_debrief_content(
    task: SubAgentTask,
    parsed: SubAgentParsedOutput,
) -> None:
    """LLM: append structured runner output sections to the DEBRIEF file.

    新手说明:
    把结构化 runner 产出追加到 DEBRIEF，方便人接管。包括 artifacts、tests、patches、
    lessons 和 next_actions 五个段落。
    """

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
