
from __future__ import annotations

"""minimal agent-run workspaces inside a runtime-memory task workspace.

Human version:
Each subagent run now gets a small filesystem workspace under
`tasks/<task_id>/work/agents/<run_id>/`. The legacy work-order directory remains the
write-compatible source for existing code; this workspace is the new recovery
and takeover surface that later phases can grow independently.
"""

from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from ..common.json_io import append_jsonl_records, write_json_object, write_jsonl_records


@dataclass(frozen=True)
class AgentRunWorkspacePaths:
    """Concrete files for one task-local agent run workspace."""

    root: Path
    agent_yaml: Path
    state_json: Path
    task_md: Path
    timeline_jsonl: Path
    checkpoint_json: Path
    summary_md: Path
    final_report_md: Path
    findings_jsonl: Path
    inbox_dir: Path
    outbox_dir: Path
    artifacts_dir: Path
    compactions_dir: Path
    compaction_ledger_jsonl: Path
    latest_compaction_summary_md: Path
    latest_compaction_metadata_json: Path
    legacy_run_ref_json: Path


@dataclass(frozen=True)
class EnsureAgentRunWorkspaceRequest:
    """Bundle inputs for syncing one agent-run workspace."""

    root: Path
    task: Any
    task_id: str
    now: float


def ensure_agent_run_workspace(
    request: EnsureAgentRunWorkspaceRequest | Path | None = None,
    task: Any | None = None,
    *,
    root: Path | None = None,
    task_id: str | None = None,
    now: float | None = None,
) -> AgentRunWorkspacePaths:
    """Create/update the Phase 1 agent-run workspace skeleton for a subagent task."""

    inputs = _coerce_ensure_request(request, task, root=root, task_id=task_id, now=now)
    paths = agent_run_workspace_paths(inputs.root)
    _ensure_directories(paths)
    _write_agent_yaml_if_missing(paths.agent_yaml, inputs.task, inputs.task_id, inputs.now)
    write_json_object(paths.state_json, _state_payload(inputs.task, inputs.task_id, inputs.now), sort_keys=False)
    _write_markdown(paths.task_md, _task_markdown(inputs.task, inputs.task_id))
    write_json_object(paths.checkpoint_json, _checkpoint_payload(inputs.task, inputs.task_id, inputs.now), sort_keys=False)
    _write_markdown(paths.summary_md, _summary_markdown(inputs.task, inputs.task_id))
    _write_final_report(paths.final_report_md, inputs.task, inputs.task_id)
    _write_findings(paths.findings_jsonl, inputs.task)
    write_json_object(
        paths.legacy_run_ref_json,
        _legacy_run_ref_payload(inputs.task, inputs.task_id, inputs.now),
        sort_keys=False,
    )
    _append_timeline(paths.timeline_jsonl, _timeline_event(inputs.task, inputs.task_id, inputs.now))
    return paths


def _coerce_ensure_request(
    request: EnsureAgentRunWorkspaceRequest | Path | None,
    task: Any | None,
    *,
    root: Path | None,
    task_id: str | None,
    now: float | None,
) -> EnsureAgentRunWorkspaceRequest:
    if isinstance(request, EnsureAgentRunWorkspaceRequest):
        return request
    resolved_root = root if root is not None else request
    if resolved_root is None or task is None or task_id is None or now is None:
        raise TypeError("ensure_agent_run_workspace requires root, task, task_id, and now")
    return EnsureAgentRunWorkspaceRequest(
        root=Path(resolved_root),
        task=task,
        task_id=task_id,
        now=now,
    )


def agent_run_workspace_paths(root: Path) -> AgentRunWorkspacePaths:
    """Return all Phase 1 files for an agent-run workspace root."""

    return AgentRunWorkspacePaths(
        root=root,
        agent_yaml=root / "agent.yaml",
        state_json=root / "state.json",
        task_md=root / "task.md",
        timeline_jsonl=root / "timeline.jsonl",
        checkpoint_json=root / "checkpoint.json",
        summary_md=root / "summary.md",
        final_report_md=root / "final_report.md",
        findings_jsonl=root / "findings.jsonl",
        inbox_dir=root / "inbox",
        outbox_dir=root / "outbox",
        artifacts_dir=root / "artifacts",
        compactions_dir=root / "compactions",
        compaction_ledger_jsonl=root / "compactions" / "compaction_ledger.jsonl",
        latest_compaction_summary_md=root / "compactions" / "latest_summary.md",
        latest_compaction_metadata_json=root / "compactions" / "latest_metadata.json",
        legacy_run_ref_json=root / "legacy_run_ref.json",
    )


def _ensure_directories(paths: AgentRunWorkspacePaths) -> None:
    for directory in [
        paths.root,
        paths.inbox_dir,
        paths.outbox_dir,
        paths.artifacts_dir,
        paths.artifacts_dir / "tool_outputs",
        paths.artifacts_dir / "reports",
        paths.compactions_dir,
    ]:
        directory.mkdir(parents=True, exist_ok=True)


def _state_payload(task: Any, task_id: str, now: float) -> dict[str, object]:
    return {
        "version": 1,
        "task_id": task_id,
        "run_id": str(getattr(task, "id", "")),
        "parent_run_id": str(getattr(task, "parent_id", "")),
        "root_task_id": task_id,
        "depth": int(getattr(task, "depth", 0) or 0),
        "status": str(getattr(task, "status", "")),
        "verification_status": str(getattr(task, "verification_status", "")),
        "progress": float(getattr(task, "progress", 0.0) or 0.0),
        "current_step": str(getattr(task, "current_step", "")),
        "latest_summary": str(getattr(task, "latest_summary", "")),
        "blockers": list(getattr(task, "blockers", []) or []),
        "artifact_refs": list(getattr(task, "artifact_refs", []) or []),
        "evidence_refs": list(getattr(task, "evidence_refs", []) or []),
        "updated_at": now,
    }


def _checkpoint_payload(task: Any, task_id: str, now: float) -> dict[str, object]:
    return {
        "version": 1,
        "task_id": task_id,
        "run_id": str(getattr(task, "id", "")),
        "status": str(getattr(task, "status", "")),
        "progress": float(getattr(task, "progress", 0.0) or 0.0),
        "current_step": str(getattr(task, "current_step", "")),
        "latest_summary": str(getattr(task, "latest_summary", "")),
        "blockers": list(getattr(task, "blockers", []) or []),
        "artifact_refs": list(getattr(task, "artifact_refs", []) or []),
        "evidence_refs": list(getattr(task, "evidence_refs", []) or []),
        "legacy_checkpoint_ref": str(getattr(task, "checkpoint_json", "")),
        "legacy_status_report_ref": str(getattr(task, "status_report_json", "")),
        "updated_at": now,
    }


def _legacy_run_ref_payload(task: Any, task_id: str, now: float) -> dict[str, object]:
    task_dir = str(getattr(task, "task_dir", ""))
    return {
        "version": 2,
        "mode": "legacy_subagent_work_order_adapter",
        "task_id": task_id,
        "run_id": str(getattr(task, "id", "")),
        "parent_run_id": str(getattr(task, "parent_id", "")),
        "depth": int(getattr(task, "depth", 0) or 0),
        "status": str(getattr(task, "status", "")),
        "legacy_task_dir": task_dir,
        "legacy_task_json": str(Path(task_dir) / "task.json") if task_dir else "",
        "legacy_run_json": str(Path(task_dir) / "run.json") if task_dir else "",
        "agent_run_workspace_status": "phase_1_skeleton",
        "updated_at": now,
    }


def _timeline_event(task: Any, task_id: str, now: float) -> dict[str, object]:
    return {
        "ts": now,
        "event": "agent_run_workspace_synced",
        "task_id": task_id,
        "run_id": str(getattr(task, "id", "")),
        "status": str(getattr(task, "status", "")),
        "summary": str(getattr(task, "latest_summary", "")),
    }


def _write_agent_yaml_if_missing(path: Path, task: Any, task_id: str, now: float) -> None:
    if path.exists():
        return
    content = (
        "version: 1\n"
        f'run_id: "{_yaml_quote(str(getattr(task, "id", "")))}"\n'
        f'root_task_id: "{_yaml_quote(task_id)}"\n'
        f'parent_run_id: "{_yaml_quote(str(getattr(task, "parent_id", "")))}"\n'
        f'agent_name: "{_yaml_quote(str(getattr(task, "agent_name", "")))}"\n'
        f'role: "{_yaml_quote(str(getattr(task, "role", "")))}"\n'
        f'owner: "{_yaml_quote(str(getattr(task, "owner", "")))}"\n'
        f"depth: {int(getattr(task, 'depth', 0) or 0)}\n"
        f"created_at: {float(getattr(task, 'created_at', 0.0) or now)}\n"
        "visibility: task-local\n"
        "memory_scope: run_workspace\n"
    )
    path.write_text(content, encoding="utf-8")


def _task_markdown(task: Any, task_id: str) -> str:
    plan_lines = "\n".join(f"- {item}" for item in list(getattr(task, "plan", []) or [])) or "- 暂无"
    acceptance_checks = list(getattr(task, "acceptance_checks", []) or [])
    acceptance_lines = "\n".join(f"- [ ] {item}" for item in acceptance_checks) or "- [ ] 未设置"
    return (
        "# Task\n\n"
        f"- task_id: {task_id}\n"
        f"- run_id: {getattr(task, 'id', '')}\n"
        f"- legacy_task_dir: {getattr(task, 'task_dir', '')}\n\n"
        "## Goal\n\n"
        f"{getattr(task, 'goal', '') or '待填写'}\n\n"
        "## Plan\n\n"
        f"{plan_lines}\n\n"
        "## Acceptance\n\n"
        f"{acceptance_lines}\n"
    )


def _summary_markdown(task: Any, task_id: str) -> str:
    latest = str(getattr(task, "latest_summary", "")) or "暂无"
    return (
        "# Summary\n\n"
        f"- task_id: {task_id}\n"
        f"- run_id: {getattr(task, 'id', '')}\n"
        f"- status: {getattr(task, 'status', '')}\n"
        f"- current_step: {getattr(task, 'current_step', '') or getattr(task, 'status', '')}\n\n"
        "## Latest\n\n"
        f"{latest}\n"
    )


def _write_final_report(path: Path, task: Any, task_id: str) -> None:
    result = str(getattr(task, "result", ""))
    status = str(getattr(task, "status", ""))
    if not result and path.exists():
        return
    body = result or "待完成后填写"
    _write_markdown(
        path,
        "# Final Report\n\n"
        f"- task_id: {task_id}\n"
        f"- run_id: {getattr(task, 'id', '')}\n"
        f"- status: {status}\n\n"
        "## Result\n\n"
        f"{body}\n",
    )


def _write_findings(path: Path, task: Any) -> None:
    payloads = [_finding_payload(item) for item in list(getattr(task, "findings", []) or [])]
    write_jsonl_records(path, [payload for payload in payloads if payload is not None])


def _finding_payload(item: object) -> dict[str, object] | None:
    if is_dataclass(item):
        return asdict(item)
    return item if isinstance(item, dict) else None


def _write_markdown(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _append_timeline(path: Path, payload: dict[str, object]) -> None:
    append_jsonl_records(path, [payload])


def _yaml_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


__all__ = [
    "AgentRunWorkspacePaths",
    "EnsureAgentRunWorkspaceRequest",
    "agent_run_workspace_paths",
    "ensure_agent_run_workspace",
]
