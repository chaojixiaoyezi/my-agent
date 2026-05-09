# LLM: LocalStore control-plane APIs provide fast runtime projections without replacing file facts.
# 模块用途: 写入和查询 agent run、agent event、task rollup 等控制面投影。

from __future__ import annotations

"""LocalStore control-plane projection helpers for agent runtime status.

给人看的解释：
这个文件把 task/run workspace 里已经存在的状态同步到 SQLite。
上级代理或接管代理可以快速查询 agent tree，但真正恢复时仍要回到 task/run 文件事实源。
"""

import time
import uuid
from dataclasses import replace
from typing import Any

from .control_plane_codec import (
    AGENT_EVENT_INSERT_SQL,
    AGENT_RUN_UPSERT_SQL,
    TASK_ROLLUP_UPSERT_SQL,
    agent_event_from_row,
    agent_run_from_row,
    agent_run_values,
    json_dumps,
    task_rollup_from_row,
    task_rollup_values,
)
from .control_plane_models import (
    AgentEventInput,
    AgentEventRecord,
    AgentRunRecord,
    AgentRuntimeQueryContext,
    AgentRuntimeQueryResult,
    AgentTreeReport,
    TaskRollupRecord,
)

_RUNNING_STATUSES = {"RUNNING"}
_BLOCKED_STATUSES = {"BLOCKED"}
_COMPLETED_STATUSES = {"DONE", "COMPLETED", "ACCEPTED", "AWAITING_ACCEPTANCE"}
_FAILED_STATUSES = {"FAILED", "ERROR", "TIMEOUT"}
_TAKEOVER_CANDIDATE_STATUSES = _BLOCKED_STATUSES | _FAILED_STATUSES


# LLM: LocalStoreControlPlaneMixin owns query projection writes for agent runtime status.
# 类用途: 为 LocalStore 增加 agent tree、事件流和任务 rollup 查询能力。
class LocalStoreControlPlaneMixin:

    # LLM: upsert_agent_run writes the latest query projection for one run.
    # 函数用途: 写入或更新一个 agent run 的控制面状态行。
    def upsert_agent_run(self, record: AgentRunRecord) -> AgentRunRecord:
        now = time.time()
        created_at = record.created_at or now
        updated_at = record.updated_at or now
        with self._connection() as conn:
            conn.execute(AGENT_RUN_UPSERT_SQL, agent_run_values(record, created_at, updated_at))
            conn.commit()
        return self.get_agent_run(record.run_id) or record

    # LLM: get_agent_run hydrates one run projection by id.
    # 函数用途: 按 run_id 读取 agent run 控制面状态。
    def get_agent_run(self, run_id: str) -> AgentRunRecord | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        return agent_run_from_row(row) if row else None

    # LLM: record_agent_event appends one runtime event to the control-plane event stream.
    # 函数用途: 追加 agent 生命周期事件，供任务树汇报和审计读取。
    def record_agent_event(self, event: AgentEventInput) -> AgentEventRecord:
        event_id = event.event_id or str(uuid.uuid4())
        created_at = event.created_at or time.time()
        with self._connection() as conn:
            conn.execute(
                AGENT_EVENT_INSERT_SQL,
                (
                    event_id,
                    event.root_task_id,
                    event.run_id,
                    event.parent_run_id,
                    event.event_type,
                    json_dumps(event.payload),
                    float(created_at),
                    json_dumps(event.reserved),
                ),
            )
            conn.commit()
        return AgentEventRecord(
            event_id=event_id,
            root_task_id=event.root_task_id,
            run_id=event.run_id,
            parent_run_id=event.parent_run_id,
            event_type=event.event_type,
            payload=dict(event.payload),
            created_at=float(created_at),
            reserved=dict(event.reserved),
        )

    # LLM: list_agent_events returns recent events for a task or run.
    # 函数用途: 查询某个 root task 或 run 的 agent 事件流。
    def list_agent_events(
        self,
        *,
        root_task_id: str = "",
        run_id: str = "",
        limit: int = 50,
    ) -> list[AgentEventRecord]:
        if limit <= 0:
            return []
        clauses: list[str] = []
        params: list[Any] = []
        if root_task_id:
            clauses.append("root_task_id = ?")
            params.append(root_task_id)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM agent_events
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        return [agent_event_from_row(row) for row in rows]

    # LLM: rebuild_task_rollup recomputes counts from current agent_runs rows.
    # 函数用途: 从 agent_runs 重新计算 root task 的聚合状态并写入 task_rollups。
    def rebuild_task_rollup(self, task_id: str) -> TaskRollupRecord:
        runs = self._agent_runs_for_task(task_id)
        rollup = _build_task_rollup(task_id, runs)
        self.upsert_task_rollup(rollup)
        return rollup

    # LLM: upsert_task_rollup stores a precomputed task-level projection.
    # 函数用途: 写入或更新一个 root task 的聚合状态。
    def upsert_task_rollup(self, rollup: TaskRollupRecord) -> TaskRollupRecord:
        updated_at = rollup.updated_at or time.time()
        with self._connection() as conn:
            conn.execute(TASK_ROLLUP_UPSERT_SQL, task_rollup_values(rollup, updated_at))
            conn.commit()
        return self.get_task_rollup(rollup.task_id) or rollup

    # LLM: get_task_rollup reads the fast task status projection.
    # 函数用途: 读取 root task 的控制面聚合状态。
    def get_task_rollup(self, task_id: str) -> TaskRollupRecord | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM task_rollups WHERE task_id = ?", (task_id,)).fetchone()
        return task_rollup_from_row(row) if row else None

    # LLM: list_agent_tree returns every run under one root task in stable tree order.
    # 函数用途: 查询 root task 下的 agent tree 投影。
    def list_agent_tree(self, task_id: str) -> AgentTreeReport:
        runs = self._agent_runs_for_task(task_id)
        rollup = self.get_task_rollup(task_id)
        return AgentTreeReport(task_id=task_id, runs=runs, rollup=rollup)

    # LLM: list_subtree returns a run and all descendants according to parent_run_id links.
    # 函数用途: 查询某个 run 的子树投影。
    def list_subtree(self, run_id: str) -> AgentTreeReport:
        root = self.get_agent_run(run_id)
        if root is None:
            return AgentTreeReport(task_id="", runs=[], rollup=None)
        all_runs = self._agent_runs_for_task(root.root_task_id)
        descendants = _select_subtree(all_runs, run_id)
        rollup = self.get_task_rollup(root.root_task_id)
        return AgentTreeReport(task_id=root.root_task_id, runs=descendants, rollup=rollup)

    # LLM: list_blocked_runs is a focused query for rescue/takeover candidates.
    # 函数用途: 查询 root task 下当前阻塞的 agent run。
    def list_blocked_runs(self, task_id: str) -> list[AgentRunRecord]:
        return [item for item in self._agent_runs_for_task(task_id) if item.status in _BLOCKED_STATUSES]

    # LLM: query_agent_runtime normalizes caller intent before selecting a tree, subtree, or takeover view.
    # 函数用途: 根据 requester/scope/purpose 查询 agent runtime 投影，预留接管和授权扩展字段。
    def query_agent_runtime(self, context: AgentRuntimeQueryContext) -> AgentRuntimeQueryResult:
        normalized, warnings = _normalize_runtime_query_context(self, context)
        report = _runtime_query_report(self, normalized)
        reserved = _runtime_query_reserved(normalized)
        return AgentRuntimeQueryResult(context=normalized, report=report, warnings=warnings, reserved=reserved)

    # LLM: _agent_runs_for_task centralizes tree ordering for all control-plane queries.
    # 函数用途: 按稳定顺序读取一个 root task 的所有 agent run。
    def _agent_runs_for_task(self, task_id: str) -> list[AgentRunRecord]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_runs
                WHERE root_task_id = ?
                ORDER BY depth ASC, created_at ASC, run_id ASC
                """,
                (task_id,),
            ).fetchall()
        return [agent_run_from_row(row) for row in rows]


# LLM: _normalize_runtime_query_context resolves root task and visibility without enforcing policy yet.
# 函数用途: 归一化查询上下文，保留未来 requester/scope 授权检查入口。
def _normalize_runtime_query_context(
    store: LocalStoreControlPlaneMixin,
    context: AgentRuntimeQueryContext,
) -> tuple[AgentRuntimeQueryContext, list[str]]:
    warnings: list[str] = []
    requester = store.get_agent_run(context.requester_run_id) if context.requester_run_id else None
    root_task_id = context.root_task_id or (requester.root_task_id if requester else "")
    target_run_id = context.target_run_id or _default_query_target(context)
    visibility = context.visibility or _default_query_visibility(context.scope)
    if not root_task_id and not target_run_id:
        warnings.append("missing_root_task_or_target_run")
    return replace(
        context,
        root_task_id=root_task_id,
        target_run_id=target_run_id,
        visibility=visibility,
    ), warnings


# LLM: _runtime_query_report keeps scope selection explicit and easy to extend.
# 函数用途: 按 scope 返回整树、子树、阻塞列表或接管候选。
def _runtime_query_report(store: LocalStoreControlPlaneMixin, context: AgentRuntimeQueryContext) -> AgentTreeReport:
    if context.scope in {"own_subtree", "subtree"} and context.target_run_id:
        return store.list_subtree(context.target_run_id)
    if context.scope == "blocked_runs":
        runs = store.list_blocked_runs(context.root_task_id)
        rollup = store.get_task_rollup(context.root_task_id)
        return AgentTreeReport(task_id=context.root_task_id, runs=runs, rollup=rollup)
    if context.scope == "takeover_candidates":
        runs = _takeover_candidate_runs(store._agent_runs_for_task(context.root_task_id))
        rollup = store.get_task_rollup(context.root_task_id)
        return AgentTreeReport(task_id=context.root_task_id, runs=runs, rollup=rollup)
    if context.root_task_id:
        return store.list_agent_tree(context.root_task_id)
    return AgentTreeReport(task_id="", runs=[], rollup=None)


# LLM: _build_task_rollup derives a compact task summary from run projections.
# 函数用途: 统计任务树状态数量并选择最近 summary。
def _build_task_rollup(task_id: str, runs: list[AgentRunRecord]) -> TaskRollupRecord:
    if not runs:
        return TaskRollupRecord(task_id=task_id, status="UNKNOWN", updated_at=time.time())
    latest = max(runs, key=lambda item: (item.updated_at, item.created_at))
    latest_with_summary = max(
        [item for item in runs if item.latest_summary] or runs,
        key=lambda item: (item.updated_at, item.created_at),
    )
    return TaskRollupRecord(
        task_id=task_id,
        status=latest.status,
        progress=_average_progress(runs),
        running_agents=sum(1 for item in runs if item.status in _RUNNING_STATUSES),
        blocked_agents=sum(1 for item in runs if item.status in _BLOCKED_STATUSES),
        completed_agents=sum(1 for item in runs if item.status in _COMPLETED_STATUSES),
        failed_agents=sum(1 for item in runs if item.status in _FAILED_STATUSES),
        latest_summary=latest_with_summary.latest_summary,
        updated_at=max(item.updated_at for item in runs),
    )


# LLM: _average_progress keeps rollup progress deterministic and bounded.
# 函数用途: 计算任务树平均进度，空集合返回 0。
def _average_progress(runs: list[AgentRunRecord]) -> float:
    if not runs:
        return 0.0
    return sum(max(0.0, min(1.0, item.progress)) for item in runs) / len(runs)


# LLM: _select_subtree walks parent_run_id links without trusting directory layout.
# 函数用途: 根据控制面 parent 链筛选 run 子树。
def _select_subtree(runs: list[AgentRunRecord], root_run_id: str) -> list[AgentRunRecord]:
    selected: list[AgentRunRecord] = []
    selected_ids: set[str] = set()
    pending = [root_run_id]
    by_id = {run.run_id: run for run in runs}
    by_parent = _runs_by_parent(runs)
    while pending:
        current = pending.pop(0)
        if current in selected_ids:
            continue
        selected_ids.add(current)
        selected.extend([by_id[current]] if current in by_id else [])
        pending.extend(child.run_id for child in by_parent.get(current, []))
    return [run for run in runs if run.run_id in selected_ids]


# LLM: _takeover_candidate_runs includes failed/timeout leaves so recovery views do not miss them.
# 函数用途: 从已排序的 run 投影中筛出可接管状态，包含 BLOCKED、FAILED、ERROR、TIMEOUT。
def _takeover_candidate_runs(runs: list[AgentRunRecord]) -> list[AgentRunRecord]:
    return [item for item in runs if item.status in _TAKEOVER_CANDIDATE_STATUSES]


# LLM: _runs_by_parent makes subtree traversal shallow and deterministic.
# 函数用途: 为每个 parent_run_id 建立直接子 run 索引。
def _runs_by_parent(runs: list[AgentRunRecord]) -> dict[str, list[AgentRunRecord]]:
    by_parent: dict[str, list[AgentRunRecord]] = {}
    for run in runs:
        by_parent.setdefault(run.parent_run_id, []).append(run)
    return by_parent


# LLM: _default_query_target lets middle agents ask for their own subtree without repeating target ids.
# 函数用途: 根据 scope 推导默认 target_run_id。
def _default_query_target(context: AgentRuntimeQueryContext) -> str:
    if context.scope in {"own_subtree", "subtree"}:
        return context.requester_run_id
    return ""


# LLM: _default_query_visibility records the intended query surface for future policy checks.
# 函数用途: 为不同 scope 提供默认可见性标签。
def _default_query_visibility(scope: str) -> str:
    if scope in {"own_subtree", "subtree"}:
        return "requester_subtree"
    if scope == "takeover_candidates":
        return "takeover_candidates"
    if scope == "blocked_runs":
        return "blocked_runs"
    return "root_task"


# LLM: _runtime_query_reserved keeps future takeover fields visible without changing stable columns.
# 函数用途: 给查询结果补充后续接管/授权扩展提示。
def _runtime_query_reserved(context: AgentRuntimeQueryContext) -> dict[str, object]:
    if context.scope == "takeover_candidates":
        return {"takeover_hint": "blocked_failed_timeout_runs"}
    return {}
