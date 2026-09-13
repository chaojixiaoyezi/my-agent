
from __future__ import annotations

"""minimal agent-run workspaces inside a runtime-memory task workspace.

Human version:
Each subagent run now gets a small filesystem workspace under
`tasks/<task_id>/work/agents/<run_id>/`. This workspace is the recovery and
takeover surface for the run.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..common.json_io import (
    append_jsonl_records,
    jsonl_lines,
    write_json_object,
    write_jsonl_records,
)


@dataclass(frozen=True)
class AgentRunWorkspacePaths:
    """Concrete files for one task-local agent run workspace."""

    root: Path
    agent_yaml: Path
    state_json: Path
    task_md: Path
    timeline_jsonl: Path
    events_jsonl: Path
    checkpoint_json: Path
    summary_md: Path
    final_report_md: Path
    findings_jsonl: Path
    artifacts_jsonl: Path
    inbox_dir: Path
    outbox_dir: Path
    artifacts_dir: Path


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
    _write_findings(paths.findings_jsonl, inputs.task, inputs.now)
    event = _timeline_event(inputs.task, inputs.task_id, inputs.now)
    _append_timeline(paths.timeline_jsonl, event)
    _append_timeline(paths.events_jsonl, event)
    write_jsonl_records(paths.artifacts_jsonl, agent_run_artifact_records(paths, inputs.task, inputs.task_id, inputs.now))
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
        events_jsonl=root / "events.jsonl",
        checkpoint_json=root / "checkpoint.json",
        summary_md=root / "summary.md",
        final_report_md=root / "final_report.md",
        findings_jsonl=root / "findings.jsonl",
        artifacts_jsonl=root / "artifacts.jsonl",
        inbox_dir=root / "inbox",
        outbox_dir=root / "outbox",
        artifacts_dir=root / "artifacts",
    )


def _ensure_directories(paths: AgentRunWorkspacePaths) -> None:
    for directory in [
        paths.root,
        paths.inbox_dir,
        paths.outbox_dir,
        paths.artifacts_dir,
        paths.artifacts_dir / "tool_outputs",
        paths.artifacts_dir / "reports",
    ]:
        directory.mkdir(parents=True, exist_ok=True)


def agent_run_artifact_records(
    paths: AgentRunWorkspacePaths,
    task: Any,
    task_id: str,
    now: float,
) -> list[dict[str, object]]:
    run_id = str(getattr(task, "id", ""))
    records: list[dict[str, object]] = [
        {
            "version": 1,
            "kind": "final_report",
            "task_id": task_id,
            "run_id": run_id,
            "path": str(paths.final_report_md),
            "exists": paths.final_report_md.exists(),
            "scope": "task_local_subagent",
            "updated_at": now,
        }
    ]
    for kind, refs in (
        ("artifact_ref", list(getattr(task, "artifact_refs", []) or [])),
        ("evidence_ref", list(getattr(task, "evidence_refs", []) or [])),
    ):
        records.extend(_agent_run_ref_records(kind, refs, {"task_id": task_id, "run_id": run_id, "updated_at": now}))
    return records


def _agent_run_ref_records(kind: str, refs: list[object], context: dict[str, object]) -> list[dict[str, object]]:
    return [
        {
            "version": 1,
            "kind": kind,
            "task_id": context["task_id"],
            "run_id": context["run_id"],
            "ref": str(ref),
            "scope": "task_local_subagent",
            "updated_at": context["updated_at"],
        }
        for ref in refs
    ]


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
        "task_checkpoint_ref": str(getattr(task, "checkpoint_json", "")),
        "task_status_report_ref": str(getattr(task, "status_report_json", "")),
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
        f"- agent_run_workspace: {getattr(task, 'agent_run_workspace_dir', '')}\n\n"
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


def _write_findings(path: Path, task: Any, now: float) -> None:
    # 增量结论账(收尾一公里):findings.jsonl 里可能有宿主审计链边干边写的行,
    # task.findings 只有最终结果块解析出的行。曾整文件覆盖写——收尾一崩/最终块缺失,
    # 工具行全灭。改按 id 幂等合并(与 shared 侧同一把尺),两边都留。
    from .shared_workspace import _finding_records, _merge_jsonl_by_id

    _merge_jsonl_by_id(path, _finding_records(task, now))


def _write_markdown(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _append_timeline(path: Path, payload: dict[str, object]) -> None:
    if _last_event_signature(path) == _event_signature(payload):
        return
    append_jsonl_records(path, [payload])


def _last_event_signature(path: Path) -> tuple[object, ...] | None:
    if not path.exists():
        return None
    try:
        # JSONL 记录边界只能是物理 LF：splitlines() 会在 U+0085/U+2028/U+2029 等合法正文字符处切开记录。
        lines = jsonl_lines(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        return _event_signature(payload) if isinstance(payload, dict) else None
    return None


def _event_signature(payload: dict[str, object]) -> tuple[object, ...]:
    return (
        payload.get("event"),
        payload.get("task_id"),
        payload.get("run_id"),
        payload.get("status"),
        payload.get("summary"),
    )


def _yaml_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


__all__ = [
    "AgentRunWorkspacePaths",
    "EnsureAgentRunWorkspaceRequest",
    "agent_run_workspace_paths",
    "ensure_agent_run_workspace",
]
