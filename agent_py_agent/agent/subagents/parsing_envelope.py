# LLM: Typed envelope bridge for legacy subagent result blocks.
# 模块用途: 把旧 [SUBAGENT_RESULT] 文本转换为 SubagentResultEnvelope，避免 parser 主文件继续变厚。

from __future__ import annotations

"""Compatibility bridge from legacy subagent result JSON to typed envelopes."""

from dataclasses import dataclass, field

from ..action_protocol import (
    ArtifactRef,
    EvidenceRef,
    RunScope,
    SubagentResultEnvelope,
    path_refs_from_subagent_refs,
)
from .parsing import parse_subagent_runner_output
from .parsing_values import _string_dict, _string_list


# LLM: SubagentResultEnvelopeParseRequest keeps result conversion bundle-shaped.
# 类用途: 集中保存旧结果文本、运行范围和真实工具列表，防止转换函数继续增加散参数。
@dataclass(frozen=True)
class SubagentResultEnvelopeParseRequest:
    text: str
    result_id: str = ""
    run_id: str = ""
    scope: RunScope | None = None
    actual_tools: list[str] = field(default_factory=list)


# LLM: parse_subagent_result_envelope is the typed compatibility bridge for legacy child reports.
# 函数用途: 把旧 [SUBAGENT_RESULT] 文本转换成 SubagentResultEnvelope；summary 只展示，真实工具由执行层传入。
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
        actual_tools=_string_list(request.actual_tools),
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


# LLM: _artifact_ref_from_payload turns model artifact arrays into refs without reading files.
# 函数用途: 从旧 artifacts 条目生成 ArtifactRef，后续由 resolver 按 ref/path 读取真实产物。
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


# LLM: _evidence_ref_from_payload preserves verification refs while keeping prose non-authoritative.
# 函数用途: 从旧 evidence_packets 条目生成 EvidenceRef；验收只看 ref 和 claim，不从 summary 猜事实。
def _evidence_ref_from_payload(item: dict[str, object]) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=str(item.get("evidence_id") or item.get("id") or ""),
        claim=str(item.get("claim") or ""),
        checked_scope=str(item.get("checked_scope") or ""),
        evidence_refs=_string_list(item.get("evidence_refs", [])),
        artifact_refs=_string_list(item.get("artifact_refs", [])),
        confidence=_float_or_zero(item.get("confidence")),
        reserved=_string_dict(item.get("reserved", {})),
    )


# LLM: _float_or_zero keeps evidence confidence parsing local to legacy subagent payloads.
# 函数用途: 把模型给出的 confidence 转为数字；无法解析时安全降级为 0.0。
def _float_or_zero(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
