from __future__ import annotations

"""LLM: filesystem task workspace skeletons for runtime memory.

Human version:
This module creates the task-level memory workspace described by the runtime
memory design without moving existing subagent work-order files. Existing
subagent runs keep their legacy directories; the task workspace records an
adapter pointer so later phases can add richer agent-run workspaces safely.
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# LLM: task workspace owns the run adapter path, but run files live in a focused helper.
from .agent_run_workspace import (
    AgentRunWorkspacePaths,
    agent_run_workspace_paths,
    ensure_agent_run_workspace,
)
from .artifact_registry import ArtifactManifestResult, sync_artifact_manifests
from .compact_chain import (
    CompactChainResult,
    default_compact_chain_result,
    sync_agent_run_compact_chain,
)
from .daily_ledger import (
    DailyLedgerAppendResult,
    DailyLedgerWorkspaceRefs,
    append_subagent_task_event,
)
from .memory_gate import MemoryGateResult, memory_gate_paths, sync_agent_run_memory_gate
from .shared_workspace import SharedWorkspaceResult, shared_workspace_paths, sync_shared_workspace
from .task_workspace_rendering import (
    write_summary,
    write_task_yaml_if_missing,
)


@dataclass(frozen=True)
class TaskWorkspacePaths:
    """Concrete paths for one task workspace plus one legacy run adapter."""

    root: Path
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


@dataclass(frozen=True)
class _TaskWorkspaceRuntimeRefs:
    artifact_manifest: ArtifactManifestResult | None = None
    compact_chain: CompactChainResult | None = None
    memory_gate: MemoryGateResult | None = None
    daily_ledger: DailyLedgerAppendResult | None = None


@dataclass(frozen=True)
class _RuntimeSyncInputs:
    workspace: str | Path
    task: Any
    paths: TaskWorkspacePaths
    now: float


@dataclass(frozen=True)
class _TaskWorkspacePathInputs:
    workspace: str | Path
    task_id: str
    run_id: str


def task_workspace_path(workspace: str | Path, task_id: str) -> Path:
    """Return the task workspace path under a subagent manager workspace."""

    return Path(workspace) / "tasks" / _safe_segment(task_id)


def ensure_subagent_task_workspace(workspace: str | Path, task: Any) -> TaskWorkspacePaths:
    """Create/update the Phase 0 task workspace for a persisted subagent task."""

    task_id = str(getattr(task, "root_id", "") or getattr(task, "id", "task"))
    run_id = str(getattr(task, "id", "") or task_id)
    path_inputs = _TaskWorkspacePathInputs(workspace, task_id, run_id)
    now = float(getattr(task, "updated_at", 0.0) or time.time())
    paths = _paths_for(path_inputs)
    _ensure_directories(paths)
    write_task_yaml_if_missing(paths.task_yaml, task_id, task, now)
    previous_state = _read_json_object(paths.state_json)
    _write_json(paths.state_json, _state_payload(task_id, run_id, task, now))
    write_summary(paths.current_summary, task_id, run_id, task)
    # LLM: shared workspace is task-local collaboration state, not main long-term memory.
    shared = sync_shared_workspace(paths.root, task, now=now)
    ensure_agent_run_workspace(paths.agent_adapter_dir, task, task_id=task_id, now=now)
    runtime_refs = _sync_runtime_refs(_RuntimeSyncInputs(workspace, task, paths, now))
    _append_timeline(paths.timeline_jsonl, _timeline_event(task, now, previous_state))
    return _paths_for(path_inputs, runtime_refs=runtime_refs, shared=shared)


def _sync_runtime_refs(inputs: _RuntimeSyncInputs) -> _TaskWorkspaceRuntimeRefs:
    # LLM: artifact manifests are written before the daily event so ledger refs are resolvable.
    artifact_manifest = sync_artifact_manifests(
        inputs.task,
        task_workspace_root=inputs.paths.root,
        agent_run_workspace_root=inputs.paths.agent_adapter_dir,
        now=inputs.now,
    )
    compact_chain = sync_agent_run_compact_chain(
        inputs.task,
        agent_run_workspace_root=inputs.paths.agent_adapter_dir,
        artifact_manifest_jsonl=artifact_manifest.agent_manifest_jsonl,
        now=inputs.now,
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


def _append_daily_ledger(
    inputs: _RuntimeSyncInputs,
    artifact_manifest: ArtifactManifestResult,
    compact_chain: CompactChainResult,
    memory_gate: MemoryGateResult,
) -> DailyLedgerAppendResult:
    # LLM: daily ledger records compact refs only; task/run files keep the detailed facts.
    return append_subagent_task_event(
        inputs.workspace,
        inputs.task,
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
    )


def _paths_for(
    path_inputs: _TaskWorkspacePathInputs,
    runtime_refs: _TaskWorkspaceRuntimeRefs | None = None,
    shared: SharedWorkspaceResult | None = None,
) -> TaskWorkspacePaths:
    runtime_refs = runtime_refs or _TaskWorkspaceRuntimeRefs()
    root = task_workspace_path(path_inputs.workspace, path_inputs.task_id)
    shared_dir = root / "shared"
    shared = shared or shared_workspace_paths(root)
    artifacts_dir = root / "artifacts"
    agents_dir = root / "agents"
    agent_adapter_dir = agents_dir / _safe_segment(path_inputs.run_id)
    return TaskWorkspacePaths(
        root=root,
        task_yaml=root / "task.yaml",
        state_json=root / "state.json",
        timeline_jsonl=root / "timeline.jsonl",
        current_summary=root / "summaries" / "current_summary.md",
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
        daily_ledger=runtime_refs.daily_ledger or _default_daily_ledger(path_inputs.workspace),
        legacy_run_ref_json=agent_adapter_dir / "legacy_run_ref.json",
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


def _state_payload(task_id: str, run_id: str, task: Any, now: float) -> dict[str, object]:
    return {
        "version": 1,
        "task_id": task_id,
        "primary_run_id": run_id,
        "status": str(getattr(task, "status", "")),
        "verification_status": str(getattr(task, "verification_status", "")),
        "progress": float(getattr(task, "progress", 0.0) or 0.0),
        "current_step": str(getattr(task, "current_step", "")),
        "latest_summary": str(getattr(task, "latest_summary", "")),
        "blockers": list(getattr(task, "blockers", []) or []),
        "artifact_refs": list(getattr(task, "artifact_refs", []) or []),
        "evidence_refs": list(getattr(task, "evidence_refs", []) or []),
        "child_run_ids": list(getattr(task, "child_ids", []) or []),
        "updated_at": now,
        "legacy": {
            "task_dir": str(getattr(task, "task_dir", "")),
            "task_json": str(Path(str(getattr(task, "task_dir", ""))) / "task.json")
            if getattr(task, "task_dir", "")
            else "",
        },
    }


def _timeline_event(task: Any, now: float, previous_state: dict[str, object]) -> dict[str, object]:
    task_id = str(getattr(task, "root_id", "") or getattr(task, "id", "task"))
    run_id = str(getattr(task, "id", "") or task_id)
    return {
        "ts": now,
        "event": "task_workspace_synced",
        "task_id": task_id,
        "run_id": run_id,
        "status": str(getattr(task, "status", "")),
        "previous_status": str(previous_state.get("status") or ""),
        "summary": str(getattr(task, "latest_summary", "")),
        "refs": {
            "legacy_task_dir": str(getattr(task, "task_dir", "")),
        },
    }


def _read_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _append_timeline(path: Path, payload: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _touch_jsonl(path: Path) -> None:
    if not path.exists():
        path.write_text("", encoding="utf-8")


def _write_if_missing(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def _safe_segment(value: str) -> str:
    cleaned = str(value or "task").replace("/", "_").replace("\\", "_").strip()
    return cleaned or "task"


__all__ = ["TaskWorkspacePaths", "ensure_subagent_task_workspace", "task_workspace_path"]
