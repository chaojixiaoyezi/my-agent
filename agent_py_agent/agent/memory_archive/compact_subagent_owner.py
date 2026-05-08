# LLM: Compact subagent owner refs are read-only breadcrumbs for future subagent session compaction.
# 模块用途: 根据 owner_type/owner_id 查找子代理 task-local run workspace 引用，不写主 memory 或 runner 文件。

from __future__ import annotations

"""read-only subagent owner reference resolver for compact resume."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compact_resume_io import read_json_object
from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_reserved_fields,
    runtime_memory_schema_payload,
)

COMPACT_SUBAGENT_OWNER_SCHEMA = RuntimeMemorySchemaOptions("compact_subagent_owner_refs")
SUPPORTED_SUBAGENT_OWNER_TYPES = ("subagent_run", "subagent_session")


# LLM: CompactSubagentOwnerRequest keeps owner ref resolution as an explicit compact/resume bundle.
# 类用途: 汇总子代理 owner 引用解析所需的 workspace、owner 类型和模式，避免散参数扩张。
@dataclass(frozen=True)
class CompactSubagentOwnerRequest:
    workspace: Path
    owner_type: str
    owner_id: str
    resume_mode: str


# LLM: resolve_compact_subagent_owner returns stable refs only; it never creates or edits run files.
# 函数用途: 为 subagent_run/subagent_session compact resume 找到 task-local run workspace 和 legacy 引用。
def resolve_compact_subagent_owner(request: CompactSubagentOwnerRequest) -> dict[str, Any]:
    base = _base_payload(request)
    if request.owner_type not in SUPPORTED_SUBAGENT_OWNER_TYPES:
        return base | {"status": "not_subagent_owner", "refs": {}, "workspace_refs": [], "legacy_run_ref": {}}
    if not request.owner_id:
        return base | {"status": "missing_owner_id", "refs": {}, "workspace_refs": [], "legacy_run_ref": {}}
    refs = _owner_refs(request.workspace, request.owner_id)
    status = _owner_status(refs)
    return base | {
        "status": status,
        "refs": refs,
        "workspace_refs": refs.get("agent_run_workspaces", []),
        "legacy_run_ref": _read_legacy_run_ref(refs.get("legacy_run_ref", "")),
        "reserved_hooks": _reserved_hooks(request, refs),
    }


# LLM: _base_payload keeps subagent compact boundaries visible to resume and auto-cycle callers.
# 函数用途: 生成每个 owner 解析结果共有的 schema、owner、模式和“不污染主 memory”边界字段。
def _base_payload(request: CompactSubagentOwnerRequest) -> dict[str, Any]:
    return {
        "version": COMPACT_SUBAGENT_OWNER_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_SUBAGENT_OWNER_SCHEMA),
        "supported_owner_types": list(SUPPORTED_SUBAGENT_OWNER_TYPES),
        "requested_owner_type": request.owner_type,
        "requested_owner_id": request.owner_id,
        "requested_resume_mode": request.resume_mode,
        "owner": {"owner_type": request.owner_type, "owner_id": request.owner_id},
        "memory_scope": "task_local",
        "writes_main_memory": False,
        "automatic_tool_execution": "none",
        "reserved_hooks": {},
        "reserved": runtime_memory_reserved_fields(COMPACT_SUBAGENT_OWNER_SCHEMA),
    }


# LLM: _owner_refs searches only known task workspace shapes under the active runtime workspace.
# 函数用途: 从 tasks/<root>/agents/<run_id> 和 legacy subagents/<run_id> 收集存在的恢复引用路径。
def _owner_refs(workspace: Path, owner_id: str) -> dict[str, Any]:
    run_workspaces = _agent_run_workspaces(workspace, owner_id)
    primary = Path(run_workspaces[0]) if run_workspaces else None
    legacy_dir = workspace / "subagents" / owner_id
    refs: dict[str, Any] = {
        "agent_run_workspace": str(primary) if primary else "",
        "agent_run_workspaces": run_workspaces,
        "legacy_task_dir": str(legacy_dir) if legacy_dir.exists() else "",
    }
    if primary:
        refs.update(_primary_run_refs(primary))
    legacy_ref = refs.get("legacy_run_ref", "")
    if not legacy_ref and primary:
        candidate = primary / "legacy_run_ref.json"
        if candidate.exists():
            refs["legacy_run_ref"] = str(candidate)
    return {key: value for key, value in refs.items() if value}


# LLM: _agent_run_workspaces finds task-local run dirs without scanning outside the runtime workspace.
# 函数用途: 查找所有 tasks/*/agents/<run_id> 候选路径，排序后让恢复输出稳定可比对。
def _agent_run_workspaces(workspace: Path, owner_id: str) -> list[str]:
    tasks_root = workspace / "tasks"
    if not tasks_root.exists():
        return []
    return [str(path) for path in sorted(tasks_root.glob(f"*/agents/{owner_id}")) if path.is_dir()]


# LLM: _primary_run_refs maps the current run workspace contract into compact resume references.
# 函数用途: 返回 agent run workspace 内可恢复文件和目录路径，缺失文件不伪造、不报错。
def _primary_run_refs(run_workspace: Path) -> dict[str, str]:
    candidates = {
        "agent_state": run_workspace / "state.json",
        "agent_checkpoint": run_workspace / "checkpoint.json",
        "agent_summary": run_workspace / "summary.md",
        "agent_task": run_workspace / "task.md",
        "agent_timeline": run_workspace / "timeline.jsonl",
        "agent_findings": run_workspace / "findings.jsonl",
        "agent_compactions": run_workspace / "compactions",
        "agent_artifacts": run_workspace / "artifacts",
        "legacy_run_ref": run_workspace / "legacy_run_ref.json",
    }
    return {key: str(path) for key, path in candidates.items() if path.exists()}


# LLM: _read_legacy_run_ref exposes adapter metadata only when the declared JSON ref exists and is valid.
# 函数用途: 读取 legacy_run_ref.json 中的旧工单目录引用，便于新旧子代理 workspace 互相接管。
def _read_legacy_run_ref(value: str) -> dict[str, Any]:
    if not value:
        return {}
    payload = read_json_object(Path(value))
    return payload if payload else {}


# LLM: _reserved_hooks names future subagent session compact files without creating or mutating them.
# 函数用途: 预留子代理自动会话压缩 hook 的路径和边界，当前只返回 refs，不写文件。
def _reserved_hooks(request: CompactSubagentOwnerRequest, refs: dict[str, Any]) -> dict[str, Any]:
    compactions = str(refs.get("agent_compactions", "") or "")
    return {
        "enabled": False,
        "owner_type": request.owner_type,
        "owner_id": request.owner_id,
        "run_compactions_dir": compactions,
        "session_compact_ledger": f"{compactions}/session_compact_ledger.jsonl" if compactions else "",
        "continue_packet_ref": f"{compactions}/latest_continue_packet.json" if compactions else "",
        "writes_main_memory": False,
        "automatic_tool_execution": "none",
        "notes": [
            "reserved for future task-local subagent session compact",
            "must not write main-agent long-term memory",
        ],
    }


# LLM: _owner_status describes whether a subagent owner can resume from a run workspace or only legacy data.
# 函数用途: 根据找到的 refs 给出 linked_run_workspace、legacy_only 或 owner_refs_not_found 状态。
def _owner_status(refs: dict[str, Any]) -> str:
    if refs.get("agent_run_workspace"):
        return "linked_run_workspace"
    if refs.get("legacy_task_dir"):
        return "legacy_only"
    return "owner_refs_not_found"


__all__ = [
    "CompactSubagentOwnerRequest",
    "resolve_compact_subagent_owner",
]
