
from __future__ import annotations

"""filesystem task workspace skeletons for runtime memory.

Human version:
This module creates the task-level memory workspace described by the runtime
memory design. Subagent runs live under the current task workspace at
`work/agents/<run_id>/`.
"""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...common.json_io import locked_json_path, write_json_file_atomic_unlocked
from ...common.path_segments import safe_path_segment
from ..agent_run_workspace import (
    AgentRunWorkspacePaths,
    EnsureAgentRunWorkspaceRequest,
    agent_run_workspace_paths,
    ensure_agent_run_workspace,
)
from ..artifact.registry import (
    ArtifactManifestResult,
    SyncArtifactManifestsRequest,
    sync_artifact_manifests,
)
from ..daily_ledger import (
    AppendSubagentTaskEventRequest,
    DailyLedgerAppendResult,
    DailyLedgerWorkspaceRefs,
    append_subagent_task_event,
)
from ..shared_workspace import (
    SharedWorkspaceResult,
    SyncSharedWorkspaceRequest,
    shared_workspace_paths,
    sync_shared_workspace,
)
from .payloads import (
    append_timeline,
    read_json_object,
    timeline_event,
)
from .rendering import (
    WriteTaskYamlRequest,
    write_parent_summary_placeholder,
    write_summary,
    write_task_yaml,
)
from .state_merge import TaskStateMergeRequest, next_task_state


@dataclass(frozen=True)
class TaskWorkspacePaths:
    """Concrete paths for one task workspace plus one agent run workspace."""

    root: Path
    work_dir: Path
    task_yaml: Path
    state_json: Path
    timeline_jsonl: Path
    current_summary: Path
    shared_dir: Path
    shared_blackboard: Path
    shared_messages: Path
    shared_findings: Path
    shared_evidence_packets_dir: Path
    shared_evidence_index_jsonl: Path
    shared: SharedWorkspaceResult
    artifacts_dir: Path
    agents_dir: Path
    agent_adapter_dir: Path
    agent_run: AgentRunWorkspacePaths
    artifact_manifest: ArtifactManifestResult
    daily_ledger: DailyLedgerAppendResult


@dataclass(frozen=True)
class _TaskWorkspaceRuntimeRefs:
    artifact_manifest: ArtifactManifestResult | None = None
    daily_ledger: DailyLedgerAppendResult | None = None


@dataclass(frozen=True)
class _RuntimeSyncInputs:
    workspace: str | Path
    task: Any
    paths: TaskWorkspacePaths
    now: float


@dataclass(frozen=True)
class _TaskWorkspacePathInputs:
    root: str | Path
    task_id: str
    run_id: str


@dataclass(frozen=True)
class _TaskWorkspaceIdentityRequest:
    paths: TaskWorkspacePaths
    task_id: str
    run_id: str
    task: Any
    now: float


@dataclass(frozen=True)
class EnsureSubagentTaskWorkspaceRequest:
    """Bundle inputs for syncing a task workspace and its runtime refs."""

    workspace: str | Path
    task: Any


def task_workspace_path(workspace: str | Path, task_id: str) -> Path:
    """Return the task workspace path under a subagent manager workspace."""

    return Path(workspace) / "tasks" / safe_path_segment(task_id, default="task", replacement="_")


def resolve_task_workspace_root(workspace: str | Path, task: Any, task_id: str) -> Path:
    # attributes 里的 task_root 是自报路径（audit 体系有意把任务工作区放到
    # manager workspace 外），只在与框架已写字段 task_workspace_dir 同域、或
    # 位于 workspace 之内时才信任——单独篡改逃逸到任意目录（如 /etc）会被
    # 忽略回退框架计算。完整伪造（同时改所有字段）超出文件级防线，归 R1
    # WorkspaceBinding。
    task_root = _task_root_from_attrs(getattr(task, "attributes", {}) or {})
    if task_root and _task_root_trusted(task_root, task, workspace):
        return Path(task_root)
    existing = str(getattr(task, "task_workspace_dir", "") or "").strip()
    if existing and Path(existing).name == _task_segment(task_id):
        return Path(existing)
    return Path(workspace) / "tasks" / _task_segment(task_id)


def _task_root_trusted(task_root: str, task: Any, workspace: str | Path) -> bool:
    try:
        root = Path(task_root).expanduser()
    except TypeError:
        return False
    if not root.is_absolute() or ".." in root.parts or len(root.parts) <= 1:
        return False
    existing = str(getattr(task, "task_workspace_dir", "") or "").strip()
    if not existing:
        # 首次保存 task_workspace_dir 尚未写回：只要求路径形态安全。此时
        # attributes 是框架自己（source_worker 等）刚写入的。
        return True
    try:
        existing_root = Path(existing).expanduser()
    except TypeError:
        return False
    if not existing_root.is_absolute() or ".." in existing_root.parts:
        return False
    if _same_or_under(root, existing_root) or _same_or_under(existing_root, root):
        return True
    # existing 是框架默认物化点（workspace/tasks/<segment>，create_run 预填、
    # 尚未按 attributes 声明物化）→ task_root 仍是权威声明；existing 是别处
    # 物化而 task_root 不同域 → 不一致（单独篡改 attributes 逃逸）→ 拒绝。
    segment = _task_segment(str(getattr(task, "root_id", "") or getattr(task, "id", "") or ""))
    default_root = Path(workspace).expanduser() / "tasks" / segment
    return _same_or_under(existing_root, default_root) and _same_or_under(default_root, existing_root)


def _same_or_under(child: Path, anchor: Path) -> bool:
    try:
        child.resolve(strict=False).relative_to(anchor.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _task_root_from_attrs(attrs: Any) -> str:
    if not isinstance(attrs, dict):
        return ""
    run_workspace = attrs.get("run_workspace")
    if not isinstance(run_workspace, dict):
        return ""
    return str(run_workspace.get("task_root") or "").strip()


def _task_segment(value: str) -> str:
    return safe_path_segment(value, default="task", replacement="_")


def ensure_subagent_task_workspace(
    request: EnsureSubagentTaskWorkspaceRequest | str | Path | None = None,
    task: Any | None = None,
    *,
    workspace: str | Path | None = None,
) -> TaskWorkspacePaths:
    """Create/update the Phase 0 task workspace for a persisted subagent task.

    Child saves may share the parent task root, so they preserve existing parent
    state and only add child refs instead of replacing ``work/state.json``.
    """

    inputs = _coerce_ensure_request(request, task, workspace=workspace)
    raw_task_id = str(getattr(inputs.task, "root_id", "") or getattr(inputs.task, "id", "task"))
    run_id = str(getattr(inputs.task, "id", "") or raw_task_id)
    root = resolve_task_workspace_root(inputs.workspace, inputs.task, raw_task_id)
    task_id = _workspace_task_id(inputs.task, raw_task_id, run_id)
    path_inputs = _TaskWorkspacePathInputs(root, task_id, run_id)
    now = float(getattr(inputs.task, "updated_at", 0.0) or time.time())
    paths = _paths_for(path_inputs)
    _ensure_directories(paths)
    _sync_task_workspace_identity(_TaskWorkspaceIdentityRequest(paths, task_id, run_id, inputs.task, now))
    # Every child projects into the same parent task state.  Atomic replace
    # alone prevents torn JSON but not a lost read-modify-write update: two
    # children can both read nine links and each write its own tenth.  Hold one
    # shared path lock across the read, monotonic merge and atomic replace.
    with locked_json_path(paths.state_json):
        previous_state = read_json_object(paths.state_json)
        write_json_file_atomic_unlocked(
            paths.state_json,
            next_task_state(
                TaskStateMergeRequest(task_id, run_id, inputs.task, now, previous_state)
            ),
            sort_keys=False,
        )
    if run_id == task_id:
        write_summary(paths.current_summary, task_id, run_id, inputs.task)
    elif not paths.current_summary.exists():
        write_parent_summary_placeholder(paths.current_summary, task_id, run_id)
    shared = sync_shared_workspace(
        SyncSharedWorkspaceRequest(task_workspace_root=paths.work_dir, task=inputs.task, now=now)
    )
    ensure_agent_run_workspace(
        EnsureAgentRunWorkspaceRequest(
            root=paths.agent_adapter_dir,
            task=inputs.task,
            task_id=task_id,
            now=now,
        )
    )
    runtime_refs = _sync_runtime_refs(_RuntimeSyncInputs(inputs.workspace, inputs.task, paths, now))
    append_timeline(paths.timeline_jsonl, timeline_event(inputs.task, now, previous_state))
    return _paths_for(path_inputs, runtime_refs=runtime_refs, shared=shared)


def _workspace_task_id(
    task: Any,
    raw_task_id: str,
    run_id: str,
) -> str:
    if run_id != raw_task_id:
        return raw_task_id
    parent_id = str(getattr(task, "parent_id", "") or "").strip()
    if parent_id and parent_id != run_id:
        return parent_id
    return raw_task_id


def _sync_task_workspace_identity(request: _TaskWorkspaceIdentityRequest) -> None:
    paths = request.paths
    task_id = request.task_id
    run_id = request.run_id
    if run_id == task_id:
        write_task_yaml(
            WriteTaskYamlRequest(paths.task_yaml, task_id, request.task, request.now, primary_run_id=run_id)
        )
        return
    if paths.task_yaml.exists():
        return
    write_task_yaml(
        WriteTaskYamlRequest(paths.task_yaml, task_id, request.task, request.now, primary_run_id=task_id, parent_run_id="")
    )


def _coerce_ensure_request(
    request: EnsureSubagentTaskWorkspaceRequest | str | Path | None,
    task: Any | None,
    *,
    workspace: str | Path | None,
) -> EnsureSubagentTaskWorkspaceRequest:
    if isinstance(request, EnsureSubagentTaskWorkspaceRequest):
        return request
    resolved_workspace = workspace if workspace is not None else request
    if resolved_workspace is None or task is None:
        raise TypeError("ensure_subagent_task_workspace requires workspace and task")
    return EnsureSubagentTaskWorkspaceRequest(workspace=resolved_workspace, task=task)


def _sync_runtime_refs(inputs: _RuntimeSyncInputs) -> _TaskWorkspaceRuntimeRefs:
    artifact_manifest = sync_artifact_manifests(
        SyncArtifactManifestsRequest(
            task=inputs.task,
            task_workspace_root=inputs.paths.work_dir,
            agent_run_workspace_root=inputs.paths.agent_adapter_dir,
            now=inputs.now,
        )
    )
    daily_ledger = _append_daily_ledger(inputs, artifact_manifest)
    return _TaskWorkspaceRuntimeRefs(
        artifact_manifest=artifact_manifest,
        daily_ledger=daily_ledger,
    )


def _append_daily_ledger(
    inputs: _RuntimeSyncInputs,
    artifact_manifest: ArtifactManifestResult,
) -> DailyLedgerAppendResult:
    return append_subagent_task_event(
        AppendSubagentTaskEventRequest(
            root=inputs.workspace,
            task=inputs.task,
            workspace_refs=DailyLedgerWorkspaceRefs(
                inputs.paths.root,
                inputs.paths.agent_adapter_dir,
                artifact_manifest.task_manifest_jsonl,
                artifact_manifest.agent_manifest_jsonl,
            ),
            now=inputs.now,
        ),
    )


def _paths_for(
    path_inputs: _TaskWorkspacePathInputs,
    runtime_refs: _TaskWorkspaceRuntimeRefs | None = None,
    shared: SharedWorkspaceResult | None = None,
) -> TaskWorkspacePaths:
    runtime_refs = runtime_refs or _TaskWorkspaceRuntimeRefs()
    root = Path(path_inputs.root)
    work_dir = root / "work"
    shared_dir = work_dir / "shared"
    shared = shared or shared_workspace_paths(work_dir)
    artifacts_dir = work_dir / "artifacts"
    agents_dir = work_dir / "agents"
    agent_adapter_dir = agents_dir / safe_path_segment(path_inputs.run_id, default="task", replacement="_")
    return TaskWorkspacePaths(
        root=root,
        work_dir=work_dir,
        task_yaml=work_dir / "task.yaml",
        state_json=work_dir / "state.json",
        timeline_jsonl=work_dir / "timeline.jsonl",
        current_summary=work_dir / "summaries" / "current_summary.md",
        shared_dir=shared_dir,
        shared_blackboard=shared.blackboard_md,
        shared_messages=shared.messages_jsonl,
        shared_findings=shared.findings_jsonl,
        shared_evidence_packets_dir=shared.evidence_packets_dir,
        shared_evidence_index_jsonl=shared.evidence_index_jsonl,
        shared=shared,
        artifacts_dir=artifacts_dir,
        agents_dir=agents_dir,
        agent_adapter_dir=agent_adapter_dir,
        agent_run=agent_run_workspace_paths(agent_adapter_dir),
        artifact_manifest=runtime_refs.artifact_manifest
        or _default_artifact_manifest(artifacts_dir, agent_adapter_dir),
        daily_ledger=runtime_refs.daily_ledger or _default_daily_ledger(root),
    )


def _default_artifact_manifest(artifacts_dir: Path, agent_adapter_dir: Path) -> ArtifactManifestResult:
    return ArtifactManifestResult(
        task_manifest_jsonl=artifacts_dir / "manifest.jsonl",
        agent_manifest_jsonl=agent_adapter_dir / "artifacts" / "manifest.jsonl",
    )


def _default_daily_ledger(workspace: str | Path) -> DailyLedgerAppendResult:
    return DailyLedgerAppendResult(Path(workspace) / "daily" / "pending" / "events.jsonl", "")


def _ensure_directories(paths: TaskWorkspacePaths) -> None:
    for directory in [
        paths.root,
        paths.root / "output",
        paths.work_dir,
        paths.current_summary.parent,
        paths.shared_dir,
        paths.shared_dir / "evidence_packets",
        paths.shared_dir / "handoffs",
        paths.shared_dir / "locks",
        paths.artifacts_dir,
        paths.artifacts_dir / "tool_outputs",
        paths.artifacts_dir / "log_samples",
        paths.artifacts_dir / "code_snapshots",
        paths.artifacts_dir / "reports",
        paths.agents_dir,
        paths.agent_adapter_dir,
    ]:
        directory.mkdir(parents=True, exist_ok=True)


__all__ = [
    "EnsureSubagentTaskWorkspaceRequest",
    "TaskWorkspacePaths",
    "ensure_subagent_task_workspace",
    "task_workspace_path",
]
