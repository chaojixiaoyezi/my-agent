
from __future__ import annotations

"""Structured runner-output processing for subagent results."""

from dataclasses import dataclass

from ..common.value_parsing import text_or_sequence_strings
from .capability_request_identity import find_equivalent_capability_request
from .coverage_records import merge_task_coverage_records
from .models import (
    CapabilityRequest,
    SubAgentParsedOutput,
    SubAgentTask,
    VerificationEvidence,
)
from .parsing import _normalize_runner_items, _split_allowed_items, _string_dict
from .result_artifact_evidence import merge_artifact_evidence, normalize_artifact_items
from .result_structured_evidence import (
    process_evidence_items,
    process_evidence_packets,
    process_findings,
)
from .utils import _merge_list, _new_id


@dataclass(frozen=True)
class MergeActualToolsParams:

    task: SubAgentTask
    actual_tools: list[str]
    used_tools: list[str]
    allowed_tools: set[str]
    parsed_used_tools: list[str]
    now: float


@dataclass(frozen=True)
class MergeTaskToolsParams:

    task: SubAgentTask
    used_tools: list[str]
    used_skills: list[str]
    actual_tools: list[str] | None
    parsed_used_tools: list[str]
    now: float


def _merge_actual_tools(params: MergeActualToolsParams):
    task = params.task
    actual_allowed_tools = [item for item in params.actual_tools if item in params.allowed_tools]
    task.used_tools = _merge_list(task.used_tools, actual_allowed_tools)
    if not params.actual_tools:
        return list(params.parsed_used_tools)
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
                    created_at=params.now,
                )
            )
    return ignored_tools


def _create_evidence_from_parsed(parsed, now):
    count = 0
    for item in parsed.evidence:
        summary = str(item.get("summary", "")).strip()
        if summary:
            count += 1
    return count


def _create_capability_requests_from_parsed(task, parsed, now):
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
            capability_type=str(item.get("capability_type", "generic") or "generic"),
            tried=text_or_sequence_strings(item.get("tried", [])),
            evidence=text_or_sequence_strings(item.get("evidence", [])),
            constraints=_string_dict(item.get("constraints", {})),
            requested_tools=text_or_sequence_strings(item.get("requested_tools", [])),
            requested_skills=text_or_sequence_strings(item.get("requested_skills", [])),
            requested_mcp_tools=text_or_sequence_strings(item.get("requested_mcp_tools", [])),
            requested_commands=text_or_sequence_strings(item.get("requested_commands", [])),
            cwd_scope=text_or_sequence_strings(item.get("cwd_scope", [])),
            path_scope=text_or_sequence_strings(item.get("path_scope", [])),
            network_scope=text_or_sequence_strings(item.get("network_scope", [])),
            output_budget=_object_dict(item.get("output_budget", {})),
            risk_level=str(item.get("risk_level", "") or ""),
            alternatives_attempted=text_or_sequence_strings(item.get("alternatives_attempted", [])),
            escalation_target=str(item.get("escalation_target", "") or ""),
            created_at=now,
        )
        if existing := find_equivalent_capability_request(task.capability_requests, request):
            created_ids.append(existing.id)
            count += 1
            continue
        task.capability_requests.append(request)
        created_ids.append(request.id)
        count += 1
    return count, created_ids


def _object_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): val for key, val in value.items()}


def _split_tools_and_skills(parsed, allowed_tools, allowed_skills):
    used_tools, ignored_tools = _split_allowed_items(parsed.used_tools, allowed_tools)
    used_skills, ignored_skills = _split_allowed_items(parsed.used_skills, allowed_skills)
    return used_tools, ignored_tools, used_skills, ignored_skills


def _merge_task_tools(params: MergeTaskToolsParams):
    if params.actual_tools is not None:
        return _merge_actual_tools(
            MergeActualToolsParams(
                params.task,
                params.actual_tools,
                params.used_tools,
                set(params.task.allowed_tools),
                params.parsed_used_tools,
                params.now,
            )
        )
    params.task.used_tools = _merge_list(params.task.used_tools, params.used_tools)
    return []


def merge_actual_tools_for_unparsed(task: SubAgentTask, actual_tools: list[str] | None, now: float) -> list[str]:
    if actual_tools is None:
        return []
    return _merge_task_tools(MergeTaskToolsParams(task, [], [], actual_tools, [], now))


def _normalize_parsed_fields(parsed):
    return {
        "artifacts": _normalize_runner_items(parsed.artifacts),
        "tests": _normalize_runner_items(parsed.tests),
        "patches": _normalize_runner_items(parsed.patches),
        "lessons": parsed.lessons,
        "next_actions": parsed.next_actions,
    }


def _process_structured_output(
    task: SubAgentTask,
    parsed: SubAgentParsedOutput,
    now: float,
    actual_tools: list[str] | None,
) -> dict[str, object]:
    allowed_tools = set(task.allowed_tools)
    allowed_skills = set(task.allowed_skills)
    used_tools, ignored_tools, used_skills, ignored_skills = _split_tools_and_skills(
        parsed, allowed_tools, allowed_skills
    )
    if actual_tools is not None:
        ignored_tools = _merge_task_tools(
            MergeTaskToolsParams(task, used_tools, used_skills, actual_tools, parsed.used_tools, now)
        )
    else:
        task.used_tools = _merge_list(task.used_tools, used_tools)
    task.used_skills = _merge_list(task.used_skills, used_skills)

    structured_evidence_count = process_evidence_items(parsed, task, now)
    evidence_packets = process_evidence_packets(parsed, task, now)
    findings = _process_findings_and_coverage(parsed, task, now)
    structured_request_count, created_request_ids = _create_capability_requests_from_parsed(task, parsed, now)
    normalized = _normalize_parsed_fields(parsed)
    normalized["artifacts"] = normalize_artifact_items(task, normalized["artifacts"])
    evidence_packets = merge_artifact_evidence(
        task,
        normalized["artifacts"],
        evidence_packets,
        now,
    )
    task.blockers = _merge_list(task.blockers, [parsed.blocked_reason] if parsed.blocked_reason else [])
    return {
        "ignored_tools": ignored_tools,
        "ignored_skills": ignored_skills,
        "structured_evidence_count": structured_evidence_count,
        "structured_request_count": structured_request_count,
        "evidence_packets": evidence_packets,
        "findings": findings,
        "created_request_ids": created_request_ids,
        "artifacts": normalized["artifacts"],
        "tests": normalized["tests"],
        "patches": normalized["patches"],
        "lessons": normalized["lessons"],
        "next_actions": normalized["next_actions"],
    }


def _process_findings_and_coverage(parsed, task: SubAgentTask, now: float) -> list[dict[str, object]]:
    merge_task_coverage_records(task, parsed.coverage_records)
    return process_findings(parsed, task, now)
