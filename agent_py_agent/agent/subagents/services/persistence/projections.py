"""Derived projection writes for subagent persistence.

Canonical subagent state is saved before this module runs. Everything here is a
rebuildable projection for humans, owner indexes, or control-plane lookup, so a
projection error is collected into ``projection_warnings.json`` instead of
blocking the canonical save.
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ....common.json_io import (
    locked_json_path,
    write_private_json_file_atomic,
    write_private_text_file_atomic,
    write_private_text_file_atomic_unlocked,
)
from ....common.nofollow_fs import open_readonly_file_beneath
from ....contracts.protocol_status import TOOL_STATUS_FAILED
from ....local_storage.records import (
    DERIVED_RECORD_TTL_SECONDS,
    _derived_metadata_scopes,
    derived_terminal_expired,
)
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

# 子代理只读投影账本最多保留的行数，不替代原执行账本。
MAX_PROJECTION_LEDGER_LINE_COUNT = 2000


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


# LLM: canonical保存早于本入口；过期终态不再回填全文，轻量注册和非索引人类投影仍可重建，异常保原warnings。
# 函数用途: 同步可重建投影，但不让30天外派生全文重新长回数据库。
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
        lambda: write_private_text_file_atomic(projection_dir / "thought.md", render_thought_markdown(task)),
    )
    run_step("owner_agent_projection", lambda: _write_owner_agent_projection(manager, task, owner_projection))
    run_step("owner_runtime_indexes", lambda: register_owner_runtime_indexes(manager, task))
    if not _task_projection_expired(task):
        run_step("manager_index", lambda: manager.indexing.index_task(task))
    else:
        records.append(ProjectionRecord("manager_index", "skipped", time.time()))
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
    write_private_json_file_atomic(Path(task.status_report_json), asdict(task.latest_status_report))


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


# LLM: task/thread仍为权威，过期终态只保留轻量task_registry，不刷新全文或控制面派生快照时间。
# 函数用途: 在30天准入内刷新子代理SQLite投影，始终保留任务注册。
def _sync_local_store_projection(manager: Any, task: SubAgentTask) -> None:
    if not manager.local_store:
        return
    if not _task_projection_expired(task):
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


# LLM: 原task顶层与attributes沿LocalStore同一有界scope检查；明示delivery/consume/callback欠账也不能跳过。
# 函数用途: 判断全文是否过期，保留嵌套通知/收口事实，不将待处理任务误当历史归档。
def _task_projection_expired(task, *, now: float | None = None):
    names = ("status", "ended_at", "updated_at", "completed_at", "created_at", "runtime_closeout_pending",
             "completion_notice_pending", "notification_pending", "wake_pending", "pending_closeout", "closeout_pending",
             "notify_pending", "completion_delivery_pending", "pending_completion_notification", "closeout_status",
             "notification_status", "completion_notification_status", "completion_delivery_status", "completion_notified",
             "parent_notified", "delivery_pending", "consume_pending", "callback_pending", "attributes", "metadata",
             "conversation_runtime", "closeout", "notification", "wake", "scope")
    # 缺失字段不补None；真实声明的None通知状态仍按共享规则保守保留。
    missing = object()
    payload = {name: value for name in names if (value := getattr(task, name, missing)) is not missing}
    if any(scope.get(key) for scope in _derived_metadata_scopes(payload)
           for key in ("delivery_pending", "consume_pending", "callback_pending")):
        return False
    current = time.time() if now is None else float(now)
    return derived_terminal_expired(payload, current - DERIVED_RECORD_TTL_SECONDS)


def _write_owner_agent_projection(manager: Any, task: SubAgentTask, projection: dict[str, Any]) -> None:
    owner_home = str(getattr(manager, "owner_home_dir", "") or "").strip()
    if not owner_home:
        return
    root = Path(owner_home) / "agents" / task.id
    root.mkdir(parents=True, exist_ok=True)
    write_private_json_file_atomic(root / "state.json", projection)
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
    write_private_json_file_atomic(root / "refs.json", refs)


# LLM: A descendant owns only its run/agent projections. A standalone manager
# root (id == root_id) may seed its own task row, but child goals/statuses must
# never race to overwrite the shared parent task's title or lifecycle.
# 函数用途: 登记子代理运行和代理引用；只有独立根运行才登记自己的任务行。
def register_owner_runtime_indexes(manager: Any, task: SubAgentTask) -> None:
    home_paths = getattr(manager, "home_paths", None)
    if home_paths is None:
        return
    owner_id = str(getattr(home_paths, "owner_id", "") or task.owner or getattr(manager, "owner_id", "") or "")
    task_id = str(task.root_id or task.id)
    task_path = str(getattr(task, "task_workspace_dir", "") or "").strip()
    run_path = str(getattr(task, "agent_run_workspace_dir", "") or task.task_dir or "").strip()
    try:
        if task.id == task_id and task_path:
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


# LLM: 双层同路径锁覆盖读取/合并/原子替换，deque有界；nofollow读取与0600私有替换保留原权限边界。
# 函数用途: 滚动保留投影台账最后2000行，超大旧文件也不全量载入内存。
def _append_jsonl_lines(path: Path, lines: list[str]) -> None:
    try:
        with locked_json_path(path):
            retained = _projection_ledger_tail(path)
            retained.extend(f"{line}\n" for line in lines)
            write_private_text_file_atomic_unlocked(path, "".join(retained))
    except OSError:
        return


# LLM: 只保留尾部行，缺文件为空；拒绝链接，不让旧坏UTF8阻断可重建诊断投影。
# 函数用途: 流式读取有界投影台账尾部。
def _projection_ledger_tail(path):
    try:
        descriptor = open_readonly_file_beneath(path.parent, (path.name,))
    except FileNotFoundError:
        return deque(maxlen=MAX_PROJECTION_LEDGER_LINE_COUNT)
    with os.fdopen(descriptor, "r", encoding="utf-8", errors="replace") as handle:
        return deque(handle, maxlen=MAX_PROJECTION_LEDGER_LINE_COUNT)


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
        write_private_json_file_atomic(
            path,
            {
                "schema_version": "subagent_projection_warnings.v1",
                "warnings": warnings,
                "updated_at": time.time(),
            },
        )
    except OSError:
        return
