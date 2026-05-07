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
from .agent_run_workspace import AgentRunWorkspacePaths, ensure_agent_run_workspace
from .artifact_registry import ArtifactManifestResult, sync_artifact_manifests
from .daily_ledger import (
    DailyLedgerAppendResult,
    DailyLedgerWorkspaceRefs,
    append_subagent_task_event,
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
    artifacts_dir: Path
    agents_dir: Path
    agent_adapter_dir: Path
    agent_run: AgentRunWorkspacePaths
    artifact_manifest: ArtifactManifestResult
    daily_ledger: DailyLedgerAppendResult
    legacy_run_ref_json: Path


@dataclass(frozen=True)
class _TaskWorkspaceRuntimeRefs:
    artifact_manifest: ArtifactManifestResult | None = None
    daily_ledger: DailyLedgerAppendResult | None = None


def task_workspace_path(workspace: str | Path, task_id: str) -> Path:
    """Return the task workspace path under a subagent manager workspace."""

    return Path(workspace) / "tasks" / _safe_segment(task_id)


def ensure_subagent_task_workspace(workspace: str | Path, task: Any) -> TaskWorkspacePaths:
    """Create/update the Phase 0 task workspace for a persisted subagent task."""

    task_id = str(getattr(task, "root_id", "") or getattr(task, "id", "task"))
    run_id = str(getattr(task, "id", "") or task_id)
    now = float(getattr(task, "updated_at", 0.0) or time.time())
    paths = _paths_for(workspace, task_id, run_id)
    _ensure_directories(paths)
    _write_task_yaml_if_missing(paths.task_yaml, task_id, task, now)
    previous_state = _read_json_object(paths.state_json)
    _write_json(paths.state_json, _state_payload(task_id, run_id, task, now))
    _write_summary(paths.current_summary, task_id, run_id, task)
    _write_if_missing(paths.shared_blackboard, _blackboard_content(task_id))
    _touch_jsonl(paths.shared_messages)
    _touch_jsonl(paths.shared_findings)
    ensure_agent_run_workspace(paths.agent_adapter_dir, task, task_id=task_id, now=now)
    # LLM: artifact manifests are written before the daily event so ledger refs are resolvable.
    artifact_manifest = sync_artifact_manifests(
        task,
        task_workspace_root=paths.root,
        agent_run_workspace_root=paths.agent_adapter_dir,
        now=now,
    )
    _append_timeline(paths.timeline_jsonl, _timeline_event(task, now, previous_state))
    # LLM: daily ledger records compact refs only; task/run files keep the detailed facts.
    daily_ledger = append_subagent_task_event(
        workspace,
        task,
        workspace_refs=DailyLedgerWorkspaceRefs(
            paths.root,
            paths.agent_adapter_dir,
            artifact_manifest.task_manifest_jsonl,
            artifact_manifest.agent_manifest_jsonl,
        ),
        now=now,
    )
    return _paths_for(
        workspace,
        task_id,
        run_id,
        runtime_refs=_TaskWorkspaceRuntimeRefs(
            artifact_manifest=artifact_manifest,
            daily_ledger=daily_ledger,
        ),
    )


def _paths_for(
    workspace: str | Path,
    task_id: str,
    run_id: str,
    runtime_refs: _TaskWorkspaceRuntimeRefs | None = None,
) -> TaskWorkspacePaths:
    runtime_refs = runtime_refs or _TaskWorkspaceRuntimeRefs()
    root = task_workspace_path(workspace, task_id)
    shared_dir = root / "shared"
    artifacts_dir = root / "artifacts"
    agents_dir = root / "agents"
    agent_adapter_dir = agents_dir / _safe_segment(run_id)
    return TaskWorkspacePaths(
        root=root,
        task_yaml=root / "task.yaml",
        state_json=root / "state.json",
        timeline_jsonl=root / "timeline.jsonl",
        current_summary=root / "summaries" / "current_summary.md",
        shared_dir=shared_dir,
        shared_blackboard=shared_dir / "blackboard.md",
        shared_messages=shared_dir / "messages.jsonl",
        shared_findings=shared_dir / "findings.jsonl",
        artifacts_dir=artifacts_dir,
        agents_dir=agents_dir,
        agent_adapter_dir=agent_adapter_dir,
        agent_run=_agent_run_paths(agent_adapter_dir),
        artifact_manifest=runtime_refs.artifact_manifest
        or _default_artifact_manifest(artifacts_dir, agent_adapter_dir),
        daily_ledger=runtime_refs.daily_ledger or _default_daily_ledger(workspace),
        legacy_run_ref_json=agent_adapter_dir / "legacy_run_ref.json",
    )


def _agent_run_paths(agent_adapter_dir: Path) -> AgentRunWorkspacePaths:
    return AgentRunWorkspacePaths(
        root=agent_adapter_dir,
        agent_yaml=agent_adapter_dir / "agent.yaml",
        state_json=agent_adapter_dir / "state.json",
        task_md=agent_adapter_dir / "task.md",
        timeline_jsonl=agent_adapter_dir / "timeline.jsonl",
        checkpoint_json=agent_adapter_dir / "checkpoint.json",
        summary_md=agent_adapter_dir / "summary.md",
        final_report_md=agent_adapter_dir / "final_report.md",
        findings_jsonl=agent_adapter_dir / "findings.jsonl",
        inbox_dir=agent_adapter_dir / "inbox",
        outbox_dir=agent_adapter_dir / "outbox",
        artifacts_dir=agent_adapter_dir / "artifacts",
        compactions_dir=agent_adapter_dir / "compactions",
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


def _write_task_yaml_if_missing(path: Path, task_id: str, task: Any, now: float) -> None:
    if path.exists():
        return
    goal = str(getattr(task, "goal", ""))
    content = (
        "version: 1\n"
        f'task_id: "{_yaml_quote(task_id)}"\n'
        f'primary_run_id: "{_yaml_quote(str(getattr(task, "id", "")))}"\n'
        f'parent_run_id: "{_yaml_quote(str(getattr(task, "parent_id", "")))}"\n'
        f"depth: {int(getattr(task, 'depth', 0) or 0)}\n"
        f'created_at: {float(getattr(task, "created_at", 0.0) or now)}\n'
        "source: subagent_persistence_adapter\n"
        "objective: |-\n"
        f"{_indent_block(goal or '待填写')}\n"
        "legacy:\n"
        f'  task_dir: "{_yaml_quote(str(getattr(task, "task_dir", "")))}"\n'
    )
    path.write_text(content, encoding="utf-8")


def _write_summary(path: Path, task_id: str, run_id: str, task: Any) -> None:
    latest = str(getattr(task, "latest_summary", "")) or "暂无"
    content = (
        "# Current Summary\n\n"
        f"- task_id: {task_id}\n"
        f"- primary_run_id: {run_id}\n"
        f"- status: {getattr(task, 'status', '')}\n"
        f"- current_step: {getattr(task, 'current_step', '') or getattr(task, 'status', '')}\n\n"
        "## Latest\n\n"
        f"{latest}\n"
    )
    path.write_text(content, encoding="utf-8")


def _blackboard_content(task_id: str) -> str:
    return (
        "# Blackboard\n\n"
        f"- task_id: {task_id}\n"
        "- purpose: shared task-local facts, decisions, blockers, and handoff notes\n\n"
        "## Facts\n\n- 暂无\n\n"
        "## Decisions\n\n- 暂无\n\n"
        "## Blockers\n\n- 暂无\n"
    )


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


def _yaml_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _indent_block(value: str) -> str:
    return "\n".join(f"  {line}" for line in value.splitlines() or [""])


__all__ = ["TaskWorkspacePaths", "ensure_subagent_task_workspace", "task_workspace_path"]
