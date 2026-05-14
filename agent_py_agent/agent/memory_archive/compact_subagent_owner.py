# LLM: Compact subagent owner refs are read-only breadcrumbs for future subagent session compaction.
# 模块用途: 根据 owner_type/owner_id 查找子代理 task-local run workspace 引用，不写主 memory 或 runner 文件。

from __future__ import annotations

"""read-only subagent owner reference resolver for compact resume."""

from collections.abc import Iterable
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
    subagent_workspace: Path | None = None


# LLM: resolve_compact_subagent_owner returns stable refs only; it never creates or edits run files.
# 函数用途: 为 subagent_run/subagent_session compact resume 找到 task-local run workspace 和 legacy 引用。
def resolve_compact_subagent_owner(request: CompactSubagentOwnerRequest) -> dict[str, Any]:
    base = _base_payload(request)
    if request.owner_type not in SUPPORTED_SUBAGENT_OWNER_TYPES:
        return base | {"status": "not_subagent_owner", "refs": {}, "workspace_refs": [], "legacy_run_ref": {}}
    if not request.owner_id:
        return base | {"status": "missing_owner_id", "refs": {}, "workspace_refs": [], "legacy_run_ref": {}}
    refs = _owner_refs(request)
    status = _owner_status(refs)
    return base | {
        "status": status,
        "refs": refs,
        "workspace_refs": refs.get("agent_run_workspaces", []),
        "legacy_run_ref": _read_legacy_run_ref(refs.get("legacy_run_ref", "")),
        "recommended_read_paths": _recommended_subagent_read_paths(refs),
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


# LLM: _owner_refs searches bounded root/configured subagent workspace shapes for owner-local refs.
# 函数用途: 从 workspace 和配置 subagent_workspace 收集 run workspace 与 legacy work-order 恢复引用。
def _owner_refs(request: CompactSubagentOwnerRequest) -> dict[str, Any]:
    owner_id = _owner_path_segment(request.owner_id)
    if not owner_id:
        return {}
    run_workspaces = _run_workspaces_from_search_roots(request, owner_id)
    legacy_dirs = _legacy_task_dirs(request, owner_id)
    run_workspaces = _unique_existing_dirs(
        [
            *run_workspaces,
            *[
                str(path)
                for path in (
                    _run_workspace_from_legacy_task(legacy_dir, request) for legacy_dir in legacy_dirs
                )
                if path
            ],
        ]
    )
    primary = Path(run_workspaces[0]) if run_workspaces else None
    legacy_dir = legacy_dirs[0] if legacy_dirs else None
    refs: dict[str, Any] = {
        "agent_run_workspace": str(primary) if primary else "",
        "agent_run_workspaces": run_workspaces,
        "legacy_task_dir": str(legacy_dir) if legacy_dir else "",
    }
    if primary:
        refs.update(_primary_run_refs(primary))
    legacy_ref = refs.get("legacy_run_ref", "")
    if not legacy_ref and primary:
        candidate = primary / "legacy_run_ref.json"
        if candidate.exists():
            refs["legacy_run_ref"] = str(candidate)
    return {key: value for key, value in refs.items() if value}


# LLM: _run_workspaces_from_search_roots keeps configured runtime subagent dirs first-class resume sources.
# 函数用途: 在主 workspace 与配置 subagent_workspace 的 tasks/*/agents/<run_id> 下查找候选 run workspace。
def _run_workspaces_from_search_roots(request: CompactSubagentOwnerRequest, owner_id: str) -> list[str]:
    return _unique_existing_dirs(
        path
        for root in _run_workspace_search_roots(request)
        for path in _agent_run_workspaces(root, owner_id)
    )


# LLM: _run_workspace_search_roots bounds owner lookup to known local runtime roots.
# 函数用途: 生成 run workspace 搜索根，避免为了找子代理恢复引用而扫描整个用户工作目录。
def _run_workspace_search_roots(request: CompactSubagentOwnerRequest) -> list[Path]:
    return _unique_paths([request.workspace, *_configured_subagent_workspace_roots(request)])


# LLM: _configured_subagent_workspace_roots normalizes the explicit subagent workspace passed by config/CLI.
# 函数用途: 把配置中的 subagent_workspace 解析为绝对路径，并去掉空值和重复项。
def _configured_subagent_workspace_roots(request: CompactSubagentOwnerRequest) -> list[Path]:
    return _unique_paths([request.subagent_workspace] if request.subagent_workspace else [])


# LLM: _agent_run_workspaces finds direct child run dirs without globbing owner-controlled text.
# 函数用途: 查找 tasks/<task>/agents/<run_id> 候选路径，把 owner_id 当普通目录名而不是 glob 表达式。
def _agent_run_workspaces(workspace: Path, owner_id: str) -> list[str]:
    tasks_root = workspace / "tasks"
    if not tasks_root.exists():
        return []
    matches: list[str] = []
    for task_dir in sorted(path for path in tasks_root.iterdir() if path.is_dir()):
        candidate = task_dir / "agents" / owner_id
        if candidate.is_dir():
            matches.append(str(candidate))
    return matches


# LLM: _legacy_task_dirs supports both default root/subagents and configured subagent_workspace/<run_id>.
# 函数用途: 查找旧 work-order 目录，真实 E2E 会把它放进配置指定的 runtime subagents 根目录。
def _legacy_task_dirs(request: CompactSubagentOwnerRequest, owner_id: str) -> list[Path]:
    candidates = [
        *(root / owner_id for root in _configured_subagent_workspace_roots(request)),
        request.workspace / "subagents" / owner_id,
    ]
    return [path for path in _unique_paths(candidates) if path.is_dir()]


# LLM: _run_workspace_from_legacy_task upgrades legacy work-order metadata into current run workspace refs.
# 函数用途: 从旧 task.json 的 agent_run_workspace_dir 找到新 run workspace，支撑断点接管和 compact resume。
def _run_workspace_from_legacy_task(
    legacy_dir: Path, request: CompactSubagentOwnerRequest
) -> Path | None:
    payload = read_json_object(legacy_dir / "task.json")
    raw = str(payload.get("agent_run_workspace_dir") or "")
    if not raw:
        return None
    path = Path(raw).expanduser()
    path = path if path.is_absolute() else legacy_dir / path
    if not path.is_dir():
        return None
    allowed_roots = [request.workspace, *_configured_subagent_workspace_roots(request)]
    return path if _path_is_under_any(path, allowed_roots) else None


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
        "latest_continue_packet": run_workspace / "compactions" / "latest_continue_packet.json",
        "session_compact_ledger": run_workspace / "compactions" / "session_compact_ledger.jsonl",
        "agent_artifacts": run_workspace / "artifacts",
        "legacy_run_ref": run_workspace / "legacy_run_ref.json",
    }
    return {key: str(path) for key, path in candidates.items() if path.exists()}


# LLM: _recommended_subagent_read_paths gives continuation callers task-local refs before main memory refs.
# 函数用途: 为子代理 compact/resume 显式列出应该先读的 run workspace 文件，避免误读主代理长期记忆。
def _recommended_subagent_read_paths(refs: dict[str, Any]) -> list[str]:
    ordered_keys = [
        "latest_continue_packet",
        "agent_checkpoint",
        "agent_summary",
        "agent_task",
        "agent_timeline",
        "agent_findings",
    ]
    return [str(refs[key]) for key in ordered_keys if refs.get(key)]


# LLM: _read_legacy_run_ref exposes adapter metadata only when the declared JSON ref exists and is valid.
# 函数用途: 读取 legacy_run_ref.json 中的旧工单目录引用，便于新旧子代理 workspace 互相接管。
def _read_legacy_run_ref(value: str) -> dict[str, Any]:
    if not value:
        return {}
    payload = read_json_object(Path(value))
    return payload if payload else {}


# LLM: _owner_path_segment makes owner ids literal directory names and rejects traversal-shaped values.
# 函数用途: 防止 request/session/run id 中的 slash 或 dot segments 被当成路径层级参与恢复扫描。
def _owner_path_segment(owner_id: str) -> str:
    if not owner_id or owner_id in {".", ".."}:
        return ""
    path = Path(owner_id)
    return owner_id if path.name == owner_id and len(path.parts) == 1 else ""


# LLM: _unique_paths keeps search order deterministic while avoiding duplicate root scans.
# 函数用途: 规范化并去重路径列表，保留调用方传入的优先级。
def _unique_paths(paths: Iterable[Path | None]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        if raw is None:
            continue
        path = raw.expanduser()
        key = str(path.resolve()) if path.exists() else str(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


# LLM: _unique_existing_dirs normalizes discovered run workspace refs without changing their contents.
# 函数用途: 去重并只保留真实存在的目录，保证 compact resume 输出稳定。
def _unique_existing_dirs(paths: Iterable[str | Path]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        path = Path(raw)
        if not path.is_dir():
            continue
        key = str(path.resolve())
        if key not in seen:
            seen.add(key)
            result.append(str(path))
    return result


# LLM: _path_is_under_any prevents legacy task metadata from pointing compact resume at unrelated trees.
# 函数用途: 判断候选 run workspace 是否仍位于主 workspace 或配置 subagent_workspace 下。
def _path_is_under_any(path: Path, roots: Iterable[Path]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.expanduser().resolve())
        except ValueError:
            continue
        return True
    return False


# LLM: _reserved_hooks names future subagent session compact files without creating or mutating them.
# 函数用途: 预留子代理自动会话压缩 hook 的路径和边界，当前只返回 refs，不写文件。
def _reserved_hooks(request: CompactSubagentOwnerRequest, refs: dict[str, Any]) -> dict[str, Any]:
    compactions = str(refs.get("agent_compactions", "") or "")
    continue_packet = str(refs.get("latest_continue_packet", "") or "")
    return {
        "enabled": bool(continue_packet),
        "owner_type": request.owner_type,
        "owner_id": request.owner_id,
        "run_compactions_dir": compactions,
        "session_compact_ledger": f"{compactions}/session_compact_ledger.jsonl" if compactions else "",
        "continue_packet_ref": continue_packet or (f"{compactions}/latest_continue_packet.json" if compactions else ""),
        "continue_packet_ready": bool(continue_packet),
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
