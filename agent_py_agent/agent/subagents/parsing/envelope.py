
from __future__ import annotations

"""Compatibility bridge from legacy subagent result JSON to typed envelopes."""

from dataclasses import dataclass, field

from ...action_protocol import (
    ArtifactRef,
    EvidenceRef,
    RunScope,
    SubagentResultEnvelope,
    path_refs_from_subagent_refs,
)
from ...common.value_parsing import text_or_sequence_strings
from . import parse_subagent_runner_output
from .values import _string_dict


@dataclass(frozen=True)
class SubagentResultEnvelopeParseRequest:
    text: str
    result_id: str = ""
    run_id: str = ""
    scope: RunScope | None = None
    actual_tools: list[str] = field(default_factory=list)


def parse_subagent_result_envelope(
    request: SubagentResultEnvelopeParseRequest,
) -> SubagentResultEnvelope | None:
    """把旧子代理结果块转为新的 typed envelope，保持旧 parser 行为不变。"""

    parsed = parse_subagent_runner_output(request.text)
    if not parsed.found or not parsed.ok:
        return None
    payload = parsed.raw_json if isinstance(parsed.raw_json, dict) else {}
    resolved_run_id = request.run_id or str(payload.get("run_id") or "")
    artifact_refs = [
        _artifact_ref_from_payload(item, owner_run_id=resolved_run_id)
        for item in parsed.artifacts
    ]
    evidence_refs = [_evidence_ref_from_payload(item) for item in parsed.evidence_packets]
    return SubagentResultEnvelope(
        result_id=request.result_id or str(payload.get("result_id") or payload.get("id") or ""),
        run_id=resolved_run_id,
        status=parsed.status,
        summary=parsed.summary,
        actual_tools=text_or_sequence_strings(request.actual_tools),
        artifact_refs=artifact_refs,
        evidence_refs=evidence_refs,
        path_refs=path_refs_from_subagent_refs(
            artifact_refs=artifact_refs,
            evidence_refs=evidence_refs,
            owner_run_id=resolved_run_id,
        ),
        tests=parsed.tests,
        next_actions=parsed.next_actions,
        blocked_reason=parsed.blocked_reason,
        failure_type=parsed.failure_type,
        scope=request.scope or RunScope(run_id=resolved_run_id),
        reserved={"source": "legacy_subagent_result"},
    )


def _artifact_ref_from_payload(item: dict[str, object], *, owner_run_id: str = "") -> ArtifactRef:
    artifact_id = str(item.get("artifact_id") or item.get("id") or item.get("path") or "")
    return ArtifactRef(
        artifact_id=artifact_id,
        path=str(item.get("path") or ""),
        kind=str(item.get("kind") or "file"),
        owner_run_id=str(item.get("owner_run_id") or owner_run_id),
        hash=str(item.get("hash") or ""),
        summary=str(item.get("summary") or ""),
        reserved=_string_dict(item.get("reserved", {})),
    )


def _evidence_ref_from_payload(item: dict[str, object]) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=str(item.get("evidence_id") or item.get("id") or ""),
        claim=str(item.get("claim") or ""),
        checked_scope=str(item.get("checked_scope") or ""),
        evidence_refs=text_or_sequence_strings(item.get("evidence_refs", [])),
        artifact_refs=text_or_sequence_strings(item.get("artifact_refs", [])),
        confidence=_float_or_zero(item.get("confidence")),
        reserved=_string_dict(item.get("reserved", {})),
    )


def _float_or_zero(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
