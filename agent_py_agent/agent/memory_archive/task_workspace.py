# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

from __future__ import annotations

"""filesystem task workspace skeletons for runtime memory.

Human version:
This module creates the task-level memory workspace described by the runtime
memory design without moving existing subagent work-order files. Existing
subagent runs keep their legacy directories; the task workspace records an
adapter pointer so later phases can add richer agent-run workspaces safely.
"""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# LLM: task workspace owns the run adapter path, but run files live in a focused helper.
from .agent_run_workspace import (
    AgentRunWorkspacePaths,
    EnsureAgentRunWorkspaceRequest,
    agent_run_workspace_paths,
    ensure_agent_run_workspace,
)
from .artifact_registry import (
    ArtifactManifestResult,
    SyncArtifactManifestsRequest,
    sync_artifact_manifests,
)
from .compact_chain import (
    CompactChainResult,
    SyncAgentRunCompactChainRequest,
    default_compact_chain_result,
    sync_agent_run_compact_chain,
)
from .daily_ledger import (
    AppendSubagentTaskEventRequest,
    DailyLedgerAppendResult,
    DailyLedgerWorkspaceRefs,
    append_subagent_task_event,
)
from .memory_gate import MemoryGateResult, memory_gate_paths, sync_agent_run_memory_gate
from .shared_workspace import (
    SharedWorkspaceResult,
    SyncSharedWorkspaceRequest,
    shared_workspace_paths,
    sync_shared_workspace,
)

# LLM: payload/JSON helpers stay focused so workspace orchestration does not keep growing.
from .task_workspace_payloads import (
    append_timeline,
    read_json_object,
    state_payload,
    timeline_event,
    write_json,
)
from .task_workspace_rendering import (
    write_summary,
    write_task_yaml_if_missing,
)
from .task_workspace_roots import resolve_task_workspace_root


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 TaskWorkspacePaths 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 TaskWorkspacePaths 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class TaskWorkspacePaths:
    """Concrete paths for one task workspace plus one legacy run adapter."""

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
    # LLM: compact chain is synced after checkpoint/artifacts so refs are resolvable.
    compact_chain: CompactChainResult
    # LLM: memory gate queues candidates only; promotion remains an explicit later step.
    memory_gate: MemoryGateResult
    daily_ledger: DailyLedgerAppendResult
    legacy_run_ref_json: Path


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _TaskWorkspaceRuntimeRefs 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _TaskWorkspaceRuntimeRefs 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class _TaskWorkspaceRuntimeRefs:
    artifact_manifest: ArtifactManifestResult | None = None
    compact_chain: CompactChainResult | None = None
    memory_gate: MemoryGateResult | None = None
    daily_ledger: DailyLedgerAppendResult | None = None


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _RuntimeSyncInputs 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _RuntimeSyncInputs 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class _RuntimeSyncInputs:
    workspace: str | Path
    task: Any
    paths: TaskWorkspacePaths
    now: float


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _TaskWorkspacePathInputs 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _TaskWorkspacePathInputs 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class _TaskWorkspacePathInputs:
    root: str | Path
    task_id: str
    run_id: str


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 EnsureSubagentTaskWorkspaceRequest 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 EnsureSubagentTaskWorkspaceRequest 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class EnsureSubagentTaskWorkspaceRequest:
    """Bundle inputs for syncing a task workspace and its runtime refs."""

    # LLM: task workspace remains the outer adapter; downstream runtime syncs receive bundles.
    workspace: str | Path
    task: Any


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 task_workspace_path 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 task workspace path 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def task_workspace_path(workspace: str | Path, task_id: str) -> Path:
    """Return the task workspace path under a subagent manager workspace."""

    return Path(workspace) / "tasks" / _safe_segment(task_id)


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 ensure_subagent_task_workspace 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 校验 ensure subagent task workspace 的输入、状态或路径，提前暴露无效数据和越界条件。
def ensure_subagent_task_workspace(
    request: EnsureSubagentTaskWorkspaceRequest | str | Path | None = None,
    task: Any | None = None,
    *,
    workspace: str | Path | None = None,
) -> TaskWorkspacePaths:
    """Create/update the Phase 0 task workspace for a persisted subagent task."""

    inputs = _coerce_ensure_request(request, task, workspace=workspace)
    task_id = str(getattr(inputs.task, "root_id", "") or getattr(inputs.task, "id", "task"))
    run_id = str(getattr(inputs.task, "id", "") or task_id)
    root = resolve_task_workspace_root(inputs.workspace, inputs.task, task_id)
    path_inputs = _TaskWorkspacePathInputs(root, task_id, run_id)
    now = float(getattr(inputs.task, "updated_at", 0.0) or time.time())
    paths = _paths_for(path_inputs)
    _ensure_directories(paths)
    write_task_yaml_if_missing(paths.task_yaml, task_id, inputs.task, now)
    previous_state = read_json_object(paths.state_json)
    write_json(paths.state_json, state_payload(task_id, run_id, inputs.task, now))
    write_summary(paths.current_summary, task_id, run_id, inputs.task)
    # LLM: shared workspace is task-local collaboration state, not main long-term memory.
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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _coerce_ensure_request 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 coerce ensure request 涉及的字段，让后续匹配和存储使用同一形态。
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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _sync_runtime_refs 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 sync runtime refs 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _sync_runtime_refs(inputs: _RuntimeSyncInputs) -> _TaskWorkspaceRuntimeRefs:
    # LLM: artifact manifests are written before the daily event so ledger refs are resolvable.
    artifact_manifest = sync_artifact_manifests(
        SyncArtifactManifestsRequest(
            task=inputs.task,
            task_workspace_root=inputs.paths.work_dir,
            agent_run_workspace_root=inputs.paths.agent_adapter_dir,
            now=inputs.now,
        )
    )
    compact_chain = sync_agent_run_compact_chain(
        SyncAgentRunCompactChainRequest(
            task=inputs.task,
            agent_run_workspace_root=inputs.paths.agent_adapter_dir,
            artifact_manifest_jsonl=artifact_manifest.agent_manifest_jsonl,
            now=inputs.now,
        )
    )
    memory_gate = sync_agent_run_memory_gate(
        inputs.task,
        agent_run_workspace_root=inputs.paths.agent_adapter_dir,
        now=inputs.now,
    )
    daily_ledger = _append_daily_ledger(inputs, artifact_manifest, compact_chain, memory_gate)
    return _TaskWorkspaceRuntimeRefs(
        artifact_manifest=artifact_manifest,
        compact_chain=compact_chain,
        memory_gate=memory_gate,
        daily_ledger=daily_ledger,
    )


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _append_daily_ledger 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append daily ledger 相关记录，集中处理目标路径、格式化和状态更新。
def _append_daily_ledger(
    inputs: _RuntimeSyncInputs,
    artifact_manifest: ArtifactManifestResult,
    compact_chain: CompactChainResult,
    memory_gate: MemoryGateResult,
) -> DailyLedgerAppendResult:
    # LLM: daily ledger records compact refs only; task/run files keep the detailed facts.
    return append_subagent_task_event(
        AppendSubagentTaskEventRequest(
            root=inputs.workspace,
            task=inputs.task,
            workspace_refs=DailyLedgerWorkspaceRefs(
                inputs.paths.root,
                inputs.paths.agent_adapter_dir,
                artifact_manifest.task_manifest_jsonl,
                artifact_manifest.agent_manifest_jsonl,
                compact_chain.ledger_jsonl,
                memory_gate.candidates_jsonl,
                memory_gate.skill_spark_gate_json,
            ),
            now=inputs.now,
        ),
    )


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _paths_for 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 paths for 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
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
    agent_adapter_dir = agents_dir / _safe_segment(path_inputs.run_id)
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
        compact_chain=runtime_refs.compact_chain or default_compact_chain_result(agent_adapter_dir),
        memory_gate=runtime_refs.memory_gate or memory_gate_paths(agent_adapter_dir),
        daily_ledger=runtime_refs.daily_ledger or _default_daily_ledger(root),
        legacy_run_ref_json=agent_adapter_dir / "legacy_run_ref.json",
    )


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _default_artifact_manifest 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 default artifact manifest 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _default_artifact_manifest(artifacts_dir: Path, agent_adapter_dir: Path) -> ArtifactManifestResult:
    return ArtifactManifestResult(
        task_manifest_jsonl=artifacts_dir / "manifest.jsonl",
        agent_manifest_jsonl=agent_adapter_dir / "artifacts" / "manifest.jsonl",
    )


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _default_daily_ledger 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 default daily ledger 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _default_daily_ledger(workspace: str | Path) -> DailyLedgerAppendResult:
    return DailyLedgerAppendResult(Path(workspace) / "daily" / "pending" / "events.jsonl", "")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _ensure_directories 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 校验 ensure directories 的输入、状态或路径，提前暴露无效数据和越界条件。
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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _safe_segment 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 safe segment 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _safe_segment(value: str) -> str:
    cleaned = str(value or "task").replace("/", "_").replace("\\", "_").strip()
    return cleaned or "task"


__all__ = [
    "EnsureSubagentTaskWorkspaceRequest",
    "TaskWorkspacePaths",
    "ensure_subagent_task_workspace",
    "task_workspace_path",
]
