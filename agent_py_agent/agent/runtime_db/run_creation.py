# LLM: 普通代理与显式宿主命令共用同连接创建逻辑；事务由调用方持有，不能在这里提交或懒建执行权。
# 模块用途: 在原 RuntimeDB 事务内创建任务运行树、首次尝试和创建事件。

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field

from ..common.id_generator import new_id


# LLM: 仅携带一次创建需要的事实，attempt 状态和 runner 元数据由原仓储入口验证与提供。
# 类用途: 给同一连接上的根运行或子运行创建提供明确参数。
@dataclass(frozen=True)
class RunCreation:
    owner_id: str
    run_id: str
    role: str
    attempt_status: str
    attempt_metadata: dict
    goal: str = ""
    conversation_task_id: str = ""
    thread_id: str = ""
    parent_run_id: str = ""
    task_run_metadata: dict = field(default_factory=dict)


# LLM: 保留父树复用、首次 generation、委托及事件顺序；调用方须在同一事务提交全部返回身份。
# 函数用途: 创建完整运行链；任何写入失败均交由外层事务整体回滚。
def create_run_chain(conn: sqlite3.Connection, request: RunCreation, *, now: float) -> dict[str, str]:
    task_id, task_run_id, parent_id = _task_tree(conn, request, now)
    agent_id, attempt_id = _first_attempt(conn, request, task_run_id, parent_id, now)
    delegation_id = _delegate(conn, parent_id, agent_id, attempt_id, now) if parent_id else ""
    for event_type in ("task.created", "agent_run.created"):
        conn.execute(
            "INSERT INTO runtime_events(event_id, event_type, attempt_id, agent_run_id, "
            "task_run_id, payload_json, created_at) VALUES(?, ?, ?, ?, ?, '{}', ?)",
            (uuid.uuid4().hex, event_type, attempt_id, agent_id, task_run_id, now),
        )
    return {
        "task_id": task_id,
        "task_run_id": task_run_id,
        "agent_run_id": agent_id,
        "attempt_id": attempt_id,
        "delegation_id": delegation_id,
    }


# LLM: child 共享父 TaskRun；旧入口父记录不存在时仍按原协议自成根，不能在搬迁中改变既有恢复语义。
# 函数用途: 找到父树，或为独立执行登记任务身份和新的 TaskRun。
def _task_tree(conn: sqlite3.Connection, request: RunCreation, now: float) -> tuple[str, str, str]:
    if str(request.parent_run_id or "").strip():
        parent = conn.execute(
            "SELECT ar.agent_run_id, ar.task_run_id, tr.task_id FROM agent_runs ar "
            "JOIN task_runs tr ON tr.task_run_id = ar.task_run_id WHERE ar.run_id = ?",
            (request.parent_run_id,),
        ).fetchone()
        if parent is not None:
            return str(parent["task_id"] or ""), str(parent["task_run_id"]), str(parent["agent_run_id"])
    task_id = str(request.conversation_task_id or "").strip() or new_id("task_id")
    existing = conn.execute("SELECT task_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if existing is None:
        try:
            conn.execute(
                "INSERT INTO tasks(task_id, owner_id, thread_id, conversation_task_id, "
                "title, goal, created_at, updated_at) VALUES(?, ?, ?, ?, '', ?, ?, ?)",
                (task_id, request.owner_id, request.thread_id, request.conversation_task_id,
                 request.goal, now, now),
            )
        except sqlite3.IntegrityError:
            # 原普通入口允许另一事务先登记同一 conversation task；宿主命令总是新任务。
            pass
    task_run_id = new_id("task_run_id")
    conn.execute(
        "INSERT INTO task_runs(task_run_id, task_id, status, created_at, updated_at, metadata_json) "
        "VALUES(?, ?, 'created', ?, ?, ?)",
        (task_run_id, task_id, now, now, json.dumps(request.task_run_metadata, ensure_ascii=False)),
    )
    return task_id, task_run_id, ""


# LLM: 首次 attempt 与 current 指针同次写入；pending 不伪造 runner 身份，执行权仍由 create_attempt 取得。
# 函数用途: 登记代理执行和第一代尝试，并绑定原 current 指针。
def _first_attempt(
    conn: sqlite3.Connection, request: RunCreation, task_run_id: str, parent_id: str, now: float,
) -> tuple[str, str]:
    agent_id, attempt_id = new_id("agent_run_id"), new_id("attempt_id")
    conn.execute(
        "INSERT INTO agent_runs(agent_run_id, task_run_id, parent_agent_run_id, delegation_id, "
        "run_id, role, status, current_attempt_id, current_attempt_generation, workspace_epoch, "
        "created_at, updated_at) VALUES(?, ?, ?, '', ?, ?, 'created', '', 0, 1, ?, ?)",
        (agent_id, task_run_id, parent_id, request.run_id, request.role, now, now),
    )
    conn.execute(
        "INSERT INTO agent_attempts(attempt_id, agent_run_id, attempt_generation, status, "
        "started_at, metadata_json) VALUES(?, ?, 1, ?, ?, ?)",
        (attempt_id, agent_id, request.attempt_status, now,
         json.dumps(request.attempt_metadata, ensure_ascii=False)),
    )
    conn.execute(
        "UPDATE agent_runs SET current_attempt_id = ?, current_attempt_generation = 1, updated_at = ? "
        "WHERE agent_run_id = ? AND current_attempt_generation = 0",
        (attempt_id, now, agent_id),
    )
    return agent_id, attempt_id


# LLM: 委托沿同一父子树且冻结孩子首次 attempt；不建立第二份子代理身份或调度记录。
# 函数用途: 将已登记的孩子连接到父代理，并回写唯一委托 ID。
def _delegate(
    conn: sqlite3.Connection, parent_id: str, agent_id: str, attempt_id: str, now: float,
) -> str:
    delegation_id = new_id("delegation_id")
    conn.execute(
        "INSERT INTO delegations(delegation_id, parent_agent_run_id, child_agent_run_id, "
        "child_attempt_id, granted_scope_json, created_at) VALUES(?, ?, ?, ?, '{}', ?)",
        (delegation_id, parent_id, agent_id, attempt_id, now),
    )
    conn.execute("UPDATE agent_runs SET delegation_id = ? WHERE agent_run_id = ?",
                 (delegation_id, agent_id))
    return delegation_id
