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
from ....contracts.protocol_status import TOOL_STATUS_FAILED
from ....user_space.home_indexes import (
    AgentIndexRef,
    RunIndexRef,
    TaskIndexRef,
    register_agent_ref,
    register_run_ref,
    register_task_ref,
)
from ...models import SubAgentTask
from ..agent_run_state import build_agent_run_state, build_owner_agent_projection
from ..control_plane_projection import sync_subagent_control_plane_projection


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
    projection_dir = _projection_dir(task, task_dir)

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
        lambda: (projection_dir / "thought.md").write_text(render_thought_markdown(task), encoding="utf-8"),
    )
    run_step("owner_agent_projection", lambda: _write_owner_agent_projection(manager, task, owner_projection))
    run_step("owner_runtime_indexes", lambda: register_owner_runtime_indexes(manager, task))
    run_step("manager_index", lambda: manager.indexing.index_task(task))
    run_step("local_store_projection", lambda: _sync_local_store_projection(manager, task))
    _append_projection_ledger(projection_dir, records)
    _write_projection_warnings(projection_dir, records)
    return tuple(records)


def _projection_dir(task: SubAgentTask, task_dir: Path) -> Path:
    raw = str(getattr(task, "agent_run_workspace_dir", "") or "").strip()
    if raw:
        path = Path(raw)
        path.mkdir(parents=True, exist_ok=True)
        return path
    return task_dir


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


# LLM: Projection sync joins the manager's owner ConversationStore only for read-model fields;
# task and thread files remain the two canonical authorities for their separate domains.
# 函数用途: 保存子代理时刷新 SQLite 查询投影，并带上该代理正式 Compact 次数。
def _sync_local_store_projection(manager: Any, task: SubAgentTask) -> None:
    if not manager.local_store:
        return
    sync_subagent_control_plane_projection(
        manager.local_store,
        task,
        getattr(manager, "conversation_store", None),
    )
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
        "final_report": task.agent_run_final_report_md,
        "output_json": task.output_json,
        "runner_result": task.runner_result_json,
        "result_file": task.runner_result_file,
        "artifact_refs": list(getattr(task, "artifact_refs", []) or []),
        "updated_at": task.updated_at,
    }
    write_json_file_atomic(root / "refs.json", refs)


def register_owner_runtime_indexes(manager: Any, task: SubAgentTask) -> None:
    home_paths = getattr(manager, "home_paths", None)
    if home_paths is None:
        return
    owner_id = str(getattr(home_paths, "owner_id", "") or task.owner or getattr(manager, "owner_id", "") or "")
    task_id = str(task.root_id or task.id)
    task_path = str(getattr(task, "task_workspace_dir", "") or "").strip()
    run_path = str(getattr(task, "agent_run_workspace_dir", "") or task.task_dir or "").strip()
    try:
        if task_path:
            register_task_ref(
                home_paths,
                TaskIndexRef(
                    owner_id=owner_id,
                    task_id=task_id,
                    task_path=task_path,
                    status=task.status,
                    title=task.goal,
                ),
            )
        if run_path:
            register_run_ref(
                home_paths,
                RunIndexRef(
                    owner_id=owner_id,
                    run_id=task.id,
                    task_id=task_id,
                    run_path=run_path,
                    status=task.status,
                ),
            )
        register_agent_ref(
            home_paths,
            AgentIndexRef(
                owner_id=owner_id,
                agent_id=task.id,
                task_id=task_id,
                run_path=run_path,
                status=task.status,
            ),
        )
    except OSError:
        return


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
        if record.status == TOOL_STATUS_FAILED
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
