from __future__ import annotations

"""Structured runner-output processing for subagent results."""

from dataclasses import dataclass

from .models import (
    CapabilityRequest,
    EvidencePacket,
    Finding,
    SubAgentParsedOutput,
    SubAgentTask,
    VerificationEvidence,
)
from .parsing import _normalize_runner_items, _split_allowed_items, _string_dict, _string_list
from .utils import _merge_list, _new_id


@dataclass(frozen=True)
class MergeActualToolsParams:
    """LLM: bundle actual tool merge inputs from runner telemetry."""

    task: SubAgentTask
    actual_tools: list[str]
    used_tools: list[str]
    allowed_tools: set[str]
    parsed_used_tools: list[str]
    now: float


@dataclass(frozen=True)
class MergeTaskToolsParams:
    """LLM: bundle parsed and actual tool state for task mutation."""

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


def _string_refs(value: object) -> list[str]:
    return _string_list(value)


def _float_confidence(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _process_evidence_packets(parsed, task, now):
    """LLM: Turn runner evidence packets into task-tree evidence facts."""
    packets: list[dict[str, object]] = []
    for item in parsed.evidence_packets:
        claim = str(item.get("claim", "") or "").strip()
        evidence_refs = _string_refs(item.get("evidence_refs", []))
        artifact_refs = _string_refs(item.get("artifact_refs", []))
        if not claim or not (evidence_refs or artifact_refs):
            continue
        packet = EvidencePacket(
            id=str(item.get("id", "") or _new_id("evpkt")),
            claim=claim,
            checked_scope=str(item.get("checked_scope", "") or ""),
            evidence_refs=evidence_refs,
            artifact_refs=artifact_refs,
            counter_evidence_refs=_string_refs(item.get("counter_evidence_refs", [])),
            confidence=_float_confidence(item.get("confidence", 0.0)),
            unresolved_risks=_string_refs(item.get("unresolved_risks", [])),
            created_at=now,
        )
        task.evidence_packets.append(packet)
        task.evidence_refs = _merge_list(task.evidence_refs, packet.evidence_refs)
        task.artifact_refs = _merge_list(task.artifact_refs, packet.artifact_refs)
        packets.append({
            "id": packet.id,
            "claim": packet.claim,
            "checked_scope": packet.checked_scope,
            "evidence_refs": packet.evidence_refs,
            "artifact_refs": packet.artifact_refs,
            "counter_evidence_refs": packet.counter_evidence_refs,
            "confidence": packet.confidence,
            "unresolved_risks": packet.unresolved_risks,
            "created_at": packet.created_at,
        })
    return packets


def _process_findings(parsed, task, now):
    """LLM: Store parent-readable findings that cite evidence packets."""
    findings: list[dict[str, object]] = []
    for item in parsed.findings:
        claim = str(item.get("claim", "") or "").strip()
        evidence_packet_ids = _string_refs(item.get("evidence_packet_ids", []))
        evidence_refs = _string_refs(item.get("evidence_refs", []))
        if not claim or not (evidence_packet_ids or evidence_refs):
            continue
        finding = Finding(
            id=str(item.get("id", "") or _new_id("finding")),
            claim=claim,
            status=str(item.get("status", "OPEN") or "OPEN"),
            severity=str(item.get("severity", "") or ""),
            confidence=_float_confidence(item.get("confidence", 0.0)),
            evidence_packet_ids=evidence_packet_ids,
            evidence_refs=evidence_refs,
            counter_evidence_refs=_string_refs(item.get("counter_evidence_refs", [])),
            created_at=now,
        )
        task.findings.append(finding)
        task.evidence_refs = _merge_list(task.evidence_refs, finding.evidence_refs)
        findings.append({
            "id": finding.id,
            "claim": finding.claim,
            "status": finding.status,
            "severity": finding.severity,
            "confidence": finding.confidence,
            "evidence_packet_ids": finding.evidence_packet_ids,
            "evidence_refs": finding.evidence_refs,
            "counter_evidence_refs": finding.counter_evidence_refs,
            "created_at": finding.created_at,
        })
    return findings


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

    structured_evidence_count = _process_evidence_items(parsed, task, now)
    evidence_packets = _process_evidence_packets(parsed, task, now)
    findings = _process_findings(parsed, task, now)
    structured_request_count, created_request_ids = _create_capability_requests_from_parsed(task, parsed, now)
    normalized = _normalize_parsed_fields(parsed)
    task.artifact_refs = _merge_list(
        task.artifact_refs,
        [
            ref
            for ref in (str(item.get("path") or item.get("uri") or item.get("artifact_id") or "") for item in normalized["artifacts"])
            if ref
        ],
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
