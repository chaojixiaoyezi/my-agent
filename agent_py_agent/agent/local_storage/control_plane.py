
from __future__ import annotations

"""LocalStore control-plane projection helpers for agent runtime status.

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
_DONE_STATUSES = {"DONE"}
_FAILED_STATUSES = {"FAILED", "TIMEOUT", "CHANNEL_ERROR"}
_TAKEOVER_CANDIDATE_STATUSES = _BLOCKED_STATUSES | _FAILED_STATUSES


class LocalStoreControlPlaneMixin:

    def upsert_agent_run(self, record: AgentRunRecord) -> AgentRunRecord:
        now = time.time()
        created_at = record.created_at or now
        updated_at = record.updated_at or now
        with self._connection() as conn:
            conn.execute(AGENT_RUN_UPSERT_SQL, agent_run_values(record, created_at, updated_at))
            conn.commit()
        return self.get_agent_run(record.run_id) or record

    def get_agent_run(self, run_id: str) -> AgentRunRecord | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        return agent_run_from_row(row) if row else None

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
        )

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

    def rebuild_task_rollup(self, task_id: str) -> TaskRollupRecord:
        runs = self._agent_runs_for_task(task_id)
        rollup = _build_task_rollup(task_id, runs)
        self.upsert_task_rollup(rollup)
        return rollup

    def upsert_task_rollup(self, rollup: TaskRollupRecord) -> TaskRollupRecord:
        updated_at = rollup.updated_at or time.time()
        with self._connection() as conn:
            conn.execute(TASK_ROLLUP_UPSERT_SQL, task_rollup_values(rollup, updated_at))
            conn.commit()
        return self.get_task_rollup(rollup.task_id) or rollup

    def get_task_rollup(self, task_id: str) -> TaskRollupRecord | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM task_rollups WHERE task_id = ?", (task_id,)).fetchone()
        return task_rollup_from_row(row) if row else None

    def list_agent_tree(self, task_id: str) -> AgentTreeReport:
        runs = self._agent_runs_for_task(task_id)
        rollup = self.get_task_rollup(task_id)
        return AgentTreeReport(task_id=task_id, runs=runs, rollup=rollup)

    def list_subtree(self, run_id: str) -> AgentTreeReport:
        root = self.get_agent_run(run_id)
        if root is None:
            return AgentTreeReport(task_id="", runs=[], rollup=None)
        all_runs = self._agent_runs_for_task(root.root_task_id)
        descendants = _select_subtree(all_runs, run_id)
        rollup = self.get_task_rollup(root.root_task_id)
        return AgentTreeReport(task_id=root.root_task_id, runs=descendants, rollup=rollup)

    def list_blocked_runs(self, task_id: str) -> list[AgentRunRecord]:
        return [item for item in self._agent_runs_for_task(task_id) if item.status in _BLOCKED_STATUSES]

    def query_agent_runtime(self, context: AgentRuntimeQueryContext) -> AgentRuntimeQueryResult:
        normalized, warnings = _normalize_runtime_query_context(self, context)
        report = _runtime_query_report(self, normalized)
        return AgentRuntimeQueryResult(
            context=normalized,
            report=report,
            warnings=warnings,
            takeover_hint=_runtime_query_takeover_hint(normalized),
        )

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
            completed_agents=sum(1 for item in runs if item.status in _DONE_STATUSES),
        failed_agents=sum(1 for item in runs if item.status in _FAILED_STATUSES),
        latest_summary=latest_with_summary.latest_summary,
        updated_at=max(item.updated_at for item in runs),
    )


def _average_progress(runs: list[AgentRunRecord]) -> float:
    if not runs:
        return 0.0
    return sum(max(0.0, min(1.0, item.progress)) for item in runs) / len(runs)


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


def _takeover_candidate_runs(runs: list[AgentRunRecord]) -> list[AgentRunRecord]:
    return [item for item in runs if item.status in _TAKEOVER_CANDIDATE_STATUSES]


def _runs_by_parent(runs: list[AgentRunRecord]) -> dict[str, list[AgentRunRecord]]:
    by_parent: dict[str, list[AgentRunRecord]] = {}
    for run in runs:
        by_parent.setdefault(run.parent_run_id, []).append(run)
    return by_parent


def _default_query_target(context: AgentRuntimeQueryContext) -> str:
    if context.scope in {"own_subtree", "subtree"}:
        return context.requester_run_id
    return ""


def _default_query_visibility(scope: str) -> str:
    if scope in {"own_subtree", "subtree"}:
        return "requester_subtree"
    if scope == "takeover_candidates":
        return "takeover_candidates"
    if scope == "blocked_runs":
        return "blocked_runs"
    return "root_task"


def _runtime_query_takeover_hint(context: AgentRuntimeQueryContext) -> str:
    if context.scope == "takeover_candidates":
        return "blocked_failed_timeout_runs"
    return ""
