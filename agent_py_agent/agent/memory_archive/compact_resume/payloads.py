
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..compact_continue_packet import (
    CompactContinuePacketRequest,
    build_compact_continue_packet,
)
from .completion import (
    CompactCompletionPromptRequest,
    build_compact_completion_prompt,
)
from .handoff import (
    CompactResumeHandoffRequest,
    build_compact_resume_handoff,
    render_compact_resume_context_block,
)


@dataclass(frozen=True)
class CompactResumePayloadPartsRequest:
    metadata: dict[str, Any]
    artifacts: dict[str, Any]
    artifact_load_errors: list[dict[str, object]]
    consistency: dict[str, Any]
    action_guard: dict[str, Any]
    recommended: list[str]
    next_actions: list[str]
    subagent_refs: dict[str, Any]
    fail_safe_checkpoints: list[dict[str, Any]]
    fail_safe_checkpoint_load_errors: list[dict[str, Any]]
    main_context_bundle: dict[str, Any]
    compaction_state: dict[str, Any] = field(default_factory=dict)
    handoff_summary: str = ""


@dataclass(frozen=True)
class CompactResumePayloadParts:
    handoff: dict[str, Any]
    completion_prompt: dict[str, Any]
    context_block: str
    continue_packet: dict[str, Any]


def build_compact_resume_payload_parts(request: CompactResumePayloadPartsRequest) -> CompactResumePayloadParts:
    completion_prompt = build_compact_completion_prompt(
        CompactCompletionPromptRequest(
            apply_id=str(request.metadata.get("apply_id", "")),
            plan_id=str(request.metadata.get("plan_id", "")),
            work_state=request.artifacts["work_state"],
        )
    )
    handoff = build_compact_resume_handoff(
        CompactResumeHandoffRequest(
            metadata=request.metadata,
            work_state=request.artifacts["work_state"],
            consistency=request.consistency,
            action_guard=request.action_guard,
            recommended_read_paths=request.recommended,
            next_actions=request.next_actions,
            fail_safe_checkpoints=request.fail_safe_checkpoints,
            fail_safe_checkpoint_load_errors=request.fail_safe_checkpoint_load_errors,
            artifact_load_errors=request.artifact_load_errors,
            completion_prompt=completion_prompt,
            main_context_bundle=request.main_context_bundle,
            compaction_state=request.compaction_state,
            handoff_summary=request.handoff_summary,
        )
    )
    return CompactResumePayloadParts(
        handoff=handoff,
        completion_prompt=completion_prompt,
        context_block=render_compact_resume_context_block(handoff),
        continue_packet=_continue_packet(request, handoff),
    )


def _continue_packet(request: CompactResumePayloadPartsRequest, handoff: dict[str, Any]) -> dict[str, Any]:
    return build_compact_continue_packet(
        CompactContinuePacketRequest(
            metadata=request.metadata,
            work_state=request.artifacts["work_state"],
            consistency=request.consistency,
            action_guard=request.action_guard,
            handoff=handoff,
            recommended_read_paths=request.recommended,
            next_actions=request.next_actions,
            subagent_owner_refs=request.subagent_refs,
            main_context_bundle=request.main_context_bundle,
            compaction_state=request.compaction_state,
            handoff_summary=request.handoff_summary,
        )
    )


__all__ = [
    "CompactResumePayloadPartsRequest",
    "build_compact_resume_payload_parts",
]
