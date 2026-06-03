
from __future__ import annotations

"""SQLite codec helpers for agent runtime control-plane projections.

这个文件不承载业务判断，只负责把数据类安全地写入/读出 SQLite。
把 SQL 放在这里可以让 `control_plane.py` 继续保持像 API 层，而不是变成 SQL 长文件。
"""

import json
import sqlite3
from typing import Any

from .control_plane_models import AgentEventRecord, AgentRunRecord, TaskRollupRecord

AGENT_RUN_UPSERT_SQL = """
INSERT INTO agent_runs (
    run_id, root_task_id, parent_run_id, depth, role, agent_name,
    status, progress, current_step, latest_summary, workspace_path,
    checkpoint_ref, latest_compact_ref, compact_count, heartbeat_at,
    created_at, updated_at, metadata_json, reserved_json
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(run_id) DO UPDATE SET
    root_task_id=excluded.root_task_id,
    parent_run_id=excluded.parent_run_id,
    depth=excluded.depth,
    role=excluded.role,
    agent_name=excluded.agent_name,
    status=excluded.status,
    progress=excluded.progress,
    current_step=excluded.current_step,
    latest_summary=excluded.latest_summary,
    workspace_path=excluded.workspace_path,
    checkpoint_ref=excluded.checkpoint_ref,
    latest_compact_ref=excluded.latest_compact_ref,
    compact_count=excluded.compact_count,
    heartbeat_at=excluded.heartbeat_at,
    updated_at=excluded.updated_at,
    metadata_json=excluded.metadata_json,
    reserved_json=excluded.reserved_json
"""

AGENT_EVENT_INSERT_SQL = """
INSERT INTO agent_events (
    event_id, root_task_id, run_id, parent_run_id,
    event_type, payload_json, created_at, reserved_json
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

TASK_ROLLUP_UPSERT_SQL = """
INSERT INTO task_rollups (
    task_id, status, progress, running_agents, blocked_agents,
    completed_agents, failed_agents, latest_summary, updated_at,
    metadata_json, reserved_json
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(task_id) DO UPDATE SET
    status=excluded.status,
    progress=excluded.progress,
    running_agents=excluded.running_agents,
    blocked_agents=excluded.blocked_agents,
    completed_agents=excluded.completed_agents,
    failed_agents=excluded.failed_agents,
    latest_summary=excluded.latest_summary,
    updated_at=excluded.updated_at,
    metadata_json=excluded.metadata_json,
    reserved_json=excluded.reserved_json
"""


def agent_run_values(record: AgentRunRecord, created_at: float, updated_at: float) -> tuple[object, ...]:
    return (
        record.run_id,
        record.root_task_id,
        record.parent_run_id,
        int(record.depth),
        record.role,
        record.agent_name,
        record.status,
        float(record.progress),
        record.current_step,
        record.latest_summary,
        record.workspace_path,
        record.checkpoint_ref,
        record.latest_compact_ref,
        int(record.compact_count),
        float(record.heartbeat_at),
        float(created_at),
        float(updated_at),
        json_dumps(record.metadata),
        json_dumps(record.reserved),
    )


def task_rollup_values(rollup: TaskRollupRecord, updated_at: float) -> tuple[object, ...]:
    return (
        rollup.task_id,
        rollup.status,
        float(rollup.progress),
        int(rollup.running_agents),
        int(rollup.blocked_agents),
        int(rollup.completed_agents),
        int(rollup.failed_agents),
        rollup.latest_summary,
        float(updated_at),
        json_dumps(rollup.metadata),
        json_dumps(rollup.reserved),
    )


def agent_run_from_row(row: sqlite3.Row) -> AgentRunRecord:
    return AgentRunRecord(
        run_id=row["run_id"],
        root_task_id=row["root_task_id"],
        parent_run_id=row["parent_run_id"],
        depth=int(row["depth"]),
        role=row["role"],
        agent_name=row["agent_name"],
        status=row["status"],
        progress=float(row["progress"]),
        current_step=row["current_step"],
        latest_summary=row["latest_summary"],
        workspace_path=row["workspace_path"],
        checkpoint_ref=row["checkpoint_ref"],
        latest_compact_ref=row["latest_compact_ref"],
        compact_count=int(row["compact_count"]),
        heartbeat_at=float(row["heartbeat_at"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        metadata=json_loads(row["metadata_json"]),
        reserved=json_loads(row["reserved_json"]),
    )


def agent_event_from_row(row: sqlite3.Row) -> AgentEventRecord:
    return AgentEventRecord(
        event_id=row["event_id"],
        root_task_id=row["root_task_id"],
        run_id=row["run_id"],
        parent_run_id=row["parent_run_id"],
        event_type=row["event_type"],
        payload=json_loads(row["payload_json"]),
        created_at=float(row["created_at"]),
        reserved=json_loads(row["reserved_json"]),
    )


def task_rollup_from_row(row: sqlite3.Row) -> TaskRollupRecord:
    return TaskRollupRecord(
        task_id=row["task_id"],
        status=row["status"],
        progress=float(row["progress"]),
        running_agents=int(row["running_agents"]),
        blocked_agents=int(row["blocked_agents"]),
        completed_agents=int(row["completed_agents"]),
        failed_agents=int(row["failed_agents"]),
        latest_summary=row["latest_summary"],
        updated_at=float(row["updated_at"]),
        metadata=json_loads(row["metadata_json"]),
        reserved=json_loads(row["reserved_json"]),
    )


def json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)


def json_loads(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}
