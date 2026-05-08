# LLM: Compact resume blocked payloads keep missing-apply errors schema-compatible and read-only.
# 模块用途: 生成 compact apply 缺失时的阻断恢复结果，保持和正常 resume 输出同一 JSON 形状。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compact_action_guard import (
    CompactActionGuardOptions,
    CompactActionGuardRequest,
    build_compact_action_guard,
)
from .compact_subagent_owner import (
    CompactSubagentOwnerRequest,
    resolve_compact_subagent_owner,
)
from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_reserved_fields,
    runtime_memory_schema_payload,
)

COMPACT_RESUME_SCHEMA = RuntimeMemorySchemaOptions("compact_resume")
COMPACT_RESUME_CONSISTENCY_SCHEMA = RuntimeMemorySchemaOptions("compact_resume_consistency_report")


# LLM: BlockedCompactResumeRequest carries the unresolved apply ref and owner identity for a safe error payload.
# 类用途: 描述缺失 compact metadata 时需要回传的 workspace、apply 引用、owner 和阻断状态。
@dataclass(frozen=True)
class BlockedCompactResumeRequest:
    workspace: Path
    apply_ref: str
    owner_type: str
    owner_id: str
    resume_mode: str
    metadata_path: Path
    status: str


# LLM: build_blocked_compact_resume returns a valid v2 payload without reading or writing task files.
# 函数用途: 生成缺失 metadata 时的阻断结果，CLI 可据此返回非零退出码。
def build_blocked_compact_resume(request: BlockedCompactResumeRequest) -> dict[str, Any]:
    consistency = _blocked_consistency(request)
    action_guard = _blocked_action_guard(request, consistency)
    return {
        "version": COMPACT_RESUME_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_RESUME_SCHEMA),
        "ok": False,
        "mode": "resume_from_compact",
        "workspace_root": str(request.workspace),
        "apply_id": request.apply_ref,
        "plan_id": "",
        "owner": _owner_payload(request),
        "refs": {"metadata": str(request.metadata_path)},
        "work_state": {},
        "consistency_report": consistency,
        "action_guard": action_guard,
        "handoff": {},
        "continue_packet": {},
        "fail_safe_checkpoints": [],
        "completion_prompt": {},
        "recommended_read_paths": [str(request.metadata_path)],
        "next_actions": ["Find a valid compact apply id or rerun memory-compact --apply."],
        "context_block": "",
        "subagent_session_compact": _subagent_extension(request),
        "reserved": runtime_memory_reserved_fields(COMPACT_RESUME_SCHEMA),
    }


# LLM: _blocked_consistency mirrors the normal consistency report shape for missing metadata.
# 函数用途: 构造 ok=false 的一致性报告，只登记 metadata_loaded hard 失败。
def _blocked_consistency(request: BlockedCompactResumeRequest) -> dict[str, Any]:
    return {
        "version": COMPACT_RESUME_CONSISTENCY_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_RESUME_CONSISTENCY_SCHEMA),
        "ok": False,
        "status": request.status,
        "owner": _owner_payload(request),
        "apply_id": request.apply_ref,
        "plan_id": "",
        "checks": [{"name": "metadata_loaded", "ok": False, "severity": "hard"}],
        "missing_fields": [],
        "reserved": runtime_memory_reserved_fields(COMPACT_RESUME_CONSISTENCY_SCHEMA),
    }


# LLM: _blocked_action_guard keeps missing metadata from accidentally becoming a resumable state.
# 函数用途: 为阻断 payload 生成 action guard，保持 auto/manual 都不能继续。
def _blocked_action_guard(request: BlockedCompactResumeRequest, consistency: dict[str, Any]) -> dict[str, Any]:
    return build_compact_action_guard(
        CompactActionGuardRequest(
            consistency_report=consistency,
            work_state={},
            refs={"metadata": str(request.metadata_path)},
            options=CompactActionGuardOptions(
                mode=request.resume_mode,
                owner_type=request.owner_type,
                owner_id=request.owner_id,
            ),
        )
    )


# LLM: _subagent_extension exposes task-local refs even for blocked subagent resume requests.
# 函数用途: 在缺 metadata 时仍返回 owner 解析边界，便于调用方知道不会写主 memory。
def _subagent_extension(request: BlockedCompactResumeRequest) -> dict[str, Any]:
    return resolve_compact_subagent_owner(
        CompactSubagentOwnerRequest(
            workspace=request.workspace,
            owner_type=request.owner_type,
            owner_id=request.owner_id,
            resume_mode=request.resume_mode,
        )
    )


# LLM: _owner_payload records who requested this compact resume without changing behavior.
# 函数用途: 生成 owner 字段，兼容主代理和未来子代理 session compact。
def _owner_payload(request: BlockedCompactResumeRequest) -> dict[str, str]:
    return {"owner_type": request.owner_type, "owner_id": request.owner_id}


__all__ = ["BlockedCompactResumeRequest", "build_blocked_compact_resume"]
