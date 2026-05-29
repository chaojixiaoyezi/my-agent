# LLM: Compact resume payload helpers assemble human handoff and machine continue packets.
# 模块用途: 把 resume 派生输出集中打包，避免 compact_resume.py 同时负责读取、校验、展示和继续包。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .compact_continue_packet import (
    CompactContinuePacketRequest,
    build_compact_continue_packet,
)
from .compact_resume_completion import (
    CompactCompletionPromptRequest,
    build_compact_completion_prompt,
)
from .compact_resume_handoff import (
    CompactResumeHandoffRequest,
    build_compact_resume_handoff,
    render_compact_resume_context_block,
)


# LLM: CompactResumePayloadPartsRequest carries already-read resume facts into the rendering layer.
# 类用途: 汇总 compact resume 的已验证事实，payload helper 不再读取文件或解析 owner。
@dataclass(frozen=True)
class CompactResumePayloadPartsRequest:
    metadata: dict[str, Any]
    artifacts: dict[str, Any]
    consistency: dict[str, Any]
    action_guard: dict[str, Any]
    recommended: list[str]
    next_actions: list[str]
    subagent_refs: dict[str, Any]
    fail_safe_checkpoints: list[dict[str, Any]]
    main_context_bundle: dict[str, Any]
    compaction_state: dict[str, Any] = field(default_factory=dict)
    handoff_summary: str = ""


# LLM: CompactResumePayloadParts is the stable bridge back to compact_resume schema assembly.
# 类用途: 保存 handoff、completion prompt、context block 和 continue packet，供最终 payload 直接引用。
@dataclass(frozen=True)
class CompactResumePayloadParts:
    handoff: dict[str, Any]
    completion_prompt: dict[str, Any]
    context_block: str
    continue_packet: dict[str, Any]


# LLM: build_compact_resume_payload_parts is pure assembly over already-loaded compact artifacts.
# 函数用途: 生成手动恢复交接包、可复制上下文块和机器继续包；不读写任何文件。
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


# LLM: _continue_packet freezes resume state for manual, semi-auto, and future auto callers.
# 函数用途: 生成继续工作包；只打包已读取的 compact resume 结果，不再读写任何文件。
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
