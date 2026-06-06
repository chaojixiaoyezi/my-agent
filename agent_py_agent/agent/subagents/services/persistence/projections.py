"""Derived projection writes for subagent persistence.

Canonical subagent state is saved before this module runs. Everything here is a
rebuildable projection for humans, owner indexes, or control-plane lookup, so a
projection error is collected into ``projection_warnings.json`` instead of
blocking the canonical save.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ....common.json_io import write_json_file_atomic
from ...models import SubAgentTask
from ..agent_run_state import build_agent_run_state, build_owner_agent_projection
from ..control_plane_projection import sync_subagent_control_plane_projection
from ..owner_indexes import register_owner_runtime_indexes


@dataclass(frozen=True)
class ProjectionRecord:
    step: str
    status: str
    updated_at: float
    error_type: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": "subagent_projection_record.v1",
            "step": self.step,
            "status": self.status,
            "updated_at": self.updated_at,
        }
        if self.error_type:
            payload["error_type"] = self.error_type
        if self.message:
            payload["message"] = self.message
        return payload


def sync_derived_projections(
    manager: Any,
    task: SubAgentTask,
    task_dir: Path,
    owner_projection: dict[str, Any],
) -> tuple[ProjectionRecord, ...]:
    records: list[ProjectionRecord] = []

    def run_step(name: str, action: Callable[[], None]) -> None:
        try:
            action()
            records.append(ProjectionRecord(name, "ok", time.time()))
        except Exception as exc:  # noqa: BLE001 - projections must not break canonical state.
            records.append(
                ProjectionRecord(
                    name,
                    "failed",
                    time.time(),
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
            )

    run_step("status_report", lambda: _write_status_report(task))
    run_step(
        "thought_markdown",
        lambda: (task_dir / "thought.md").write_text(render_thought_markdown(task), encoding="utf-8"),
    )
    run_step("owner_agent_projection", lambda: _write_owner_agent_projection(manager, task, owner_projection))
    run_step("owner_runtime_indexes", lambda: register_owner_runtime_indexes(manager, task))
    run_step("manager_index", lambda: manager.indexing.index_task(task))
    run_step("local_store_projection", lambda: _sync_local_store_projection(manager, task))
    _append_projection_ledger(task_dir, records)
    _write_projection_warnings(task_dir, records)
    return tuple(records)


def rebuild_derived_projections(manager: Any, run_id: str) -> tuple[ProjectionRecord, ...]:
    """Rebuild derived projections from canonical subagent state."""
    task = manager.load(run_id)
    task_dir = Path(task.task_dir)
    state = build_agent_run_state(task)
    owner_projection = build_owner_agent_projection(task, state)
    return sync_derived_projections(manager, task, task_dir, owner_projection)


def _write_status_report(task: SubAgentTask) -> None:
    if not task.status_report_json:
        return
    write_json_file_atomic(Path(task.status_report_json), asdict(task.latest_status_report))


def render_thought_markdown(task: SubAgentTask) -> str:
    return (
        "# Thought\n\n"
        f"{task.thought}\n\n"
        "## Plan\n"
        + "\n".join(f"- {item}" for item in task.plan)
        + "\n\n"
        "## Capability Boundary\n"
        f"- Agent: {task.agent_name}\n"
        f"- Role: {task.role}\n"
        f"- Owner: {task.owner or 'none'}\n"
        f"- Supervisor: {task.supervisor or 'none'}\n"
        f"- Final owner: {task.final_owner or 'none'}\n"
        f"- Parent: {task.parent_id or 'none'}\n"
        f"- Depth: {task.depth}\n"
        f"- Allowed skills: {', '.join(task.allowed_skills) or 'none'}\n"
        f"- Allowed tools: {', '.join(task.allowed_tools) or 'none'}\n\n"
        "## Write Boundary\n"
        f"- Task dir: {task.task_dir}\n"
        f"- Allowed write roots: {', '.join(task.allowed_write_roots) or 'none'}\n"
        f"- Forbidden write roots: {', '.join(task.forbidden_write_roots) or 'none'}\n\n"
        "## Acceptance Checks\n"
        + "\n".join(f"- {item}" for item in task.acceptance_checks or ["未设置"])
        + "\n\n"
        "## Evidence\n"
        + "\n".join(f"- [{item.kind}] {item.summary}" for item in task.evidence or [])
        + ("\n" if task.evidence else "- 暂无\n")
    )


def _sync_local_store_projection(manager: Any, task: SubAgentTask) -> None:
    if not manager.local_store:
        return
    sync_subagent_control_plane_projection(manager.local_store, task)
    manager.local_store.task_registry.register_task(
        task_id=task.id,
        session_id=task.root_id,
        user_id=task.owner or "",
        status=task.status,
        goal=task.goal,
    )


def _write_owner_agent_projection(manager: Any, task: SubAgentTask, projection: dict[str, Any]) -> None:
    owner_home = str(getattr(manager, "owner_home_dir", "") or "").strip()
    if not owner_home:
        return
    root = Path(owner_home) / "agents" / task.id
    root.mkdir(parents=True, exist_ok=True)
    write_json_file_atomic(root / "state.json", projection)
    refs = {
        "schema_version": "owner-agent-projection.v1",
        "run_id": task.id,
        "owner_id": task.owner,
        "task_workspace_dir": task.task_workspace_dir,
        "agent_run_workspace_dir": task.agent_run_workspace_dir,
        "compact_dir": task.agent_run_compactions_dir,
        "final_report": task.agent_run_final_report_md,
        "updated_at": task.updated_at,
    }
    write_json_file_atomic(root / "refs.json", refs)


def _append_projection_ledger(task_dir: Path, records: list[ProjectionRecord]) -> None:
    lines = _projection_ledger_lines(records)
    if not lines:
        return
    _append_jsonl_lines(task_dir / "projection_ledger.jsonl", lines)


def _projection_ledger_lines(records: list[ProjectionRecord]) -> list[str]:
    return [json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) for record in records]


def _append_jsonl_lines(path: Path, lines: list[str]) -> None:
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.writelines(f"{line}\n" for line in lines)
    except OSError:
        return


def _write_projection_warnings(task_dir: Path, records: list[ProjectionRecord]) -> None:
    path = task_dir / "projection_warnings.json"
    warnings = [
        {
            "step": record.step,
            "error_type": record.error_type,
            "message": record.message,
        }
        for record in records
        if record.status == "failed"
    ]
    if not warnings:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return
    try:
        write_json_file_atomic(
            path,
            {
                "schema_version": "subagent_projection_warnings.v1",
                "warnings": warnings,
                "updated_at": time.time(),
            },
        )
    except OSError:
        return
