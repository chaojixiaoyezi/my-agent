from __future__ import annotations

"""Structured runner-output processing for subagent results."""

from .models import CapabilityRequest, SubAgentParsedOutput, SubAgentTask, VerificationEvidence
from .parsing import _normalize_runner_items, _split_allowed_items, _string_dict, _string_list
from .utils import _merge_list, _new_id


def _merge_actual_tools(task, actual_tools, used_tools, allowed_tools, parsed_used_tools, now):
    actual_allowed_tools = [item for item in actual_tools if item in allowed_tools]
    task.used_tools = _merge_list(task.used_tools, actual_allowed_tools)
    if not actual_tools:
        return list(parsed_used_tools)
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
            tried=_string_list(item.get("tried", [])),
            evidence=_string_list(item.get("evidence", [])),
            constraints=_string_dict(item.get("constraints", {})),
            created_at=now,
        )
        task.capability_requests.append(request)
        created_ids.append(request.id)
        count += 1
    return count, created_ids


def _split_tools_and_skills(parsed, allowed_tools, allowed_skills):
    used_tools, ignored_tools = _split_allowed_items(parsed.used_tools, allowed_tools)
    used_skills, ignored_skills = _split_allowed_items(parsed.used_skills, allowed_skills)
    return used_tools, ignored_tools, used_skills, ignored_skills


def _merge_task_tools(task, used_tools, used_skills, actual_tools, parsed_used_tools, now):
    if actual_tools is not None:
        return _merge_actual_tools(task, actual_tools, used_tools, set(task.allowed_tools), parsed_used_tools, now)
    task.used_tools = _merge_list(task.used_tools, used_tools)
    return []


def _process_evidence_items(parsed, task, now):
    count = 0
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
        count += 1
    return count


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
        ignored_tools = _merge_task_tools(task, used_tools, used_skills, actual_tools, parsed.used_tools, now)
    else:
        task.used_tools = _merge_list(task.used_tools, used_tools)
    task.used_skills = _merge_list(task.used_skills, used_skills)

    structured_evidence_count = _process_evidence_items(parsed, task, now)
    structured_request_count, created_request_ids = _create_capability_requests_from_parsed(task, parsed, now)
    normalized = _normalize_parsed_fields(parsed)
    return {
        "ignored_tools": ignored_tools,
        "ignored_skills": ignored_skills,
        "structured_evidence_count": structured_evidence_count,
        "structured_request_count": structured_request_count,
        "created_request_ids": created_request_ids,
        "artifacts": normalized["artifacts"],
        "tests": normalized["tests"],
        "patches": normalized["patches"],
        "lessons": normalized["lessons"],
        "next_actions": normalized["next_actions"],
    }
