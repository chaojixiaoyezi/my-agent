from __future__ import annotations

"""cancel_subagents control tool with TaskStatus-backed status filters."""

import json
import os
import signal
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ....runtime_errors import runtime_error_report
from ....subagents.models import FailureType, normalize_task_status, task_status_in
from ....tooling.models import BaseTool, ToolExecutionResult
from ...agent_tree.status import agent_tree_status_payload
from ..tool_specs import build_cancel_subagents_spec

if TYPE_CHECKING:
    from ....core import SimpleAgent
    from ....subagents.models import SubAgentTask


@dataclass(frozen=True)
class _CancelPayloadRequest:
    ok: bool
    params: dict[str, object]
    cancelled: list[dict[str, object]]
    failed: list[dict[str, object]]
    skipped: list[dict[str, object]]
    dry_run: bool


@dataclass(frozen=True)
class _CancelOneRequest:
    task: SubAgentTask
    params: dict[str, object]


@dataclass(frozen=True)
class _ResolveRunIdsResult:
    ok: bool
    run_ids: list[str]
    error_payload: dict[str, object]


@dataclass(frozen=True)
class _ListRunsForCancelResult:
    ok: bool
    tasks: list[SubAgentTask]
    error_payload: dict[str, object]


@dataclass(frozen=True)
class _StatusFilterResult:
    ok: bool
    statuses: set[str]
    error_payload: dict[str, object]


class CancelSubagentsTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_cancel_subagents_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        run_ids_result = _resolve_run_ids(self.agent, params)
        if not run_ids_result.ok:
            return ToolExecutionResult(
                "cancel_subagents",
                False,
                json.dumps(run_ids_result.error_payload, ensure_ascii=False, indent=2),
            )
        run_ids = run_ids_result.run_ids
        if not run_ids:
            return ToolExecutionResult("cancel_subagents", False, "缺少 run_id/run_ids/root_id/status，未取消任何子代理。")
        dry_run = bool(params.get("dry_run"))
        status_filter_result = _status_filter(params.get("status"))
        if not status_filter_result.ok:
            return ToolExecutionResult(
                "cancel_subagents",
                False,
                json.dumps(status_filter_result.error_payload, ensure_ascii=False, indent=2),
            )
        status_filter = status_filter_result.statuses
        targets = _filter_existing_targets(self.agent, run_ids, status_filter)
        if dry_run:
            return self._payload_result(_CancelPayloadRequest(True, params, _dry_run_targets(targets), [], [], True))
        cancelled: list[dict[str, object]] = []
        failed: list[dict[str, object]] = []
        skipped: list[dict[str, object]] = []
        for item in targets:
            task = item.get("task")
            if task is None:
                failed.append({"run_id": item.get("run_id", ""), "error": item.get("error", "load_failed")})
                continue
            try:
                cancelled.append(
                    _cancel_one(self.agent, _CancelOneRequest(task=task, params=params))
                )
            except Exception as exc:  # pragma: no cover - defensive persistence/process edge cases.
                failed.append({"run_id": getattr(task, "id", ""), **runtime_error_report(exc, context="cancel_subagents.cancel_one")})
        target_ids = {str(item.get("run_id", "")) for item in targets}
        for run_id in run_ids:
            if run_id not in target_ids:
                skipped.append({"run_id": run_id, "reason": "status_filter_or_missing"})
        return self._payload_result(_CancelPayloadRequest(not failed, params, cancelled, failed, skipped, False))

    def _payload_result(self, request: _CancelPayloadRequest) -> ToolExecutionResult:
        payload = {
            "ok": request.ok,
            "dry_run": request.dry_run,
            "cancelled": request.cancelled,
            "failed": request.failed,
            "skipped": request.skipped,
            "agent_tree": _compact_agent_tree(agent_tree_status_payload(self.agent, {"root_id": request.params.get("root_id", "")})),
        }
        return ToolExecutionResult("cancel_subagents", request.ok, json.dumps(payload, ensure_ascii=False, indent=2))


def _resolve_run_ids(agent: SimpleAgent, params: dict[str, object]) -> _ResolveRunIdsResult:
    explicit = _explicit_run_ids(params)
    root_id = str(params.get("root_id") or "").strip()
    status_filter_result = _status_filter(params.get("status"))
    if not status_filter_result.ok:
        return _ResolveRunIdsResult(False, [], status_filter_result.error_payload)
    status_filter = status_filter_result.statuses
    ids = list(explicit)
    if root_id:
        tasks_result = _list_runs_for_cancel(agent)
        if not tasks_result.ok:
            return _ResolveRunIdsResult(False, [], tasks_result.error_payload)
        tasks = tasks_result.tasks
        ids.extend(_subtree_ids(tasks, root_id))
    if status_filter and not explicit and not root_id:
        tasks_result = _list_runs_for_cancel(agent)
        if not tasks_result.ok:
            return _ResolveRunIdsResult(False, [], tasks_result.error_payload)
        tasks = tasks_result.tasks
        ids.extend(str(task.id) for task in tasks if task_status_in(task.status, status_filter))
    return _ResolveRunIdsResult(True, _dedupe(ids), {})


def _list_runs_for_cancel(agent: SimpleAgent) -> _ListRunsForCancelResult:
    try:
        tasks = agent.subagents.list_runs()
    except Exception as exc:
        return _ListRunsForCancelResult(
            False,
            [],
            {"ok": False, "error": runtime_error_report(exc, context="cancel_subagents.list_runs")},
        )
    return _ListRunsForCancelResult(True, tasks, {})


def _explicit_run_ids(params: dict[str, object]) -> list[str]:
    ids: list[str] = []
    single = str(params.get("run_id") or "").strip()
    if single:
        ids.append(single)
    raw_many = params.get("run_ids")
    if isinstance(raw_many, str):
        ids.extend(part.strip() for part in raw_many.split(","))
    elif isinstance(raw_many, list):
        ids.extend(str(part).strip() for part in raw_many)
    return [item for item in ids if item]


def _status_filter(value: object) -> _StatusFilterResult:
    if not value:
        return _StatusFilterResult(True, set(), {})
    statuses: set[str] = set()
    invalid: list[str] = []
    for item in _status_filter_values(value):
        try:
            statuses.add(normalize_task_status(item))
        except ValueError:
            invalid.append(str(item))
    if invalid:
        return _StatusFilterResult(
            False,
            set(),
            {
                "ok": False,
                "error": "invalid_status_filter",
                "invalid_statuses": invalid,
                "message": "status 只接受当前 TaskStatus 协议值。",
            },
        )
    return _StatusFilterResult(True, statuses, {})


def _status_filter_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _subtree_ids(tasks: list[SubAgentTask], root_id: str) -> list[str]:
    by_id = {str(task.id): task for task in tasks}
    children: dict[str, list[str]] = {}
    for task in tasks:
        parent_id = str(getattr(task, "parent_id", "") or "")
        if parent_id:
            children.setdefault(parent_id, []).append(str(task.id))
    found: list[str] = []
    queue = [root_id]
    while queue:
        current = queue.pop(0)
        if current in found:
            continue
        if current in by_id:
            found.append(current)
        queue.extend(children.get(current, []))
    return found


def _filter_existing_targets(agent: SimpleAgent, run_ids: list[str], status_filter: set[str]) -> list[dict[str, object]]:
    targets: list[dict[str, object]] = []
    for run_id in run_ids:
        item = _load_cancel_target(agent, run_id)
        task = item.get("task")
        if task is None:
            targets.append({"run_id": run_id, "error": item.get("error")})
            continue
        if status_filter and not task_status_in(getattr(task, "status", ""), status_filter):
            continue
        targets.append(item)
    return targets


def _load_cancel_target(agent: SimpleAgent, run_id: str) -> dict[str, object]:
    try:
        return {"run_id": run_id, "task": agent.subagents.load(run_id)}
    except Exception as exc:
        return {"run_id": run_id, "task": None, "error": runtime_error_report(exc, context="cancel_subagents.load")}


def _dry_run_targets(targets: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for item in targets:
        task = item.get("task")
        if task is None:
            result.append({"run_id": item.get("run_id", ""), "error": item.get("error", "load_failed")})
            continue
        result.append({"run_id": getattr(task, "id", ""), "status": getattr(task, "status", "")})
    return result


def _cancel_one(agent: SimpleAgent, request: _CancelOneRequest) -> dict[str, object]:
    task = request.task
    params = request.params
    reason = str(params.get("reason") or "cancel_subagents").strip()
    attempt_id = str(getattr(task, "runner_active_attempt_id", "") or "").strip()
    if attempt_id:
        task = agent.subagents.lifecycle.abandon_runner_attempt(task.id, attempt_id, reason=reason)
    now = time.time()
    attrs = dict(getattr(task, "attributes", {}) or {})
    pid_report = _terminate_task_pid(task, bool(params.get("kill_process", True)))
    attrs["cancel_subagents"] = {
        "cancel_status": "CANCELLED",
        "reason": reason,
        "cancelled_at": now,
        "previous_status": str(getattr(task, "status", "") or ""),
        "previous_failure_type": str(getattr(task, "failure_type", "") or ""),
        "abandoned_attempt_id": attempt_id,
        "pid_report": pid_report,
    }
    task.attributes = attrs
    task.status = "ABANDONED"
    task.failure_type = FailureType.CANCELLED.value
    task.ended_at = now
    task.updated_at = now
    task.runner_active_attempt_id = ""
    agent.subagents.save(task)
    agent.subagents.actions._append_task_work_log(task, f"cancel_subagents: status=CANCELLED/ABANDONED reason={reason}")
    return {
        "run_id": task.id,
        "status": task.status,
        "cancel_status": "CANCELLED",
        "abandoned_attempt_id": attempt_id,
        "pid_report": pid_report,
    }


def _terminate_task_pid(task: SubAgentTask, kill_process: bool) -> dict[str, object]:
    pid = _task_pid(task)
    if not pid:
        return {"status": "no_pid"}
    if not kill_process:
        return {"status": "skipped", "pid": pid}
    if not _is_pid_alive(pid):
        return {"status": "not_alive", "pid": pid}
    _terminate_pid(pid)
    exited = _wait_for_pid_exit(pid, 1.0)
    return {"status": "terminated" if exited else "terminate_sent", "pid": pid, "exited": exited}


def _task_pid(task: SubAgentTask) -> int:
    attrs = dict(getattr(task, "attributes", {}) or {})
    candidates = [
        attrs.get("pid"),
        attrs.get("process_pid"),
        _nested_pid(attrs.get("process")),
        _nested_pid(attrs.get("runner_process")),
        _nested_pid(attrs.get("background_start")),
        _nested_pid(attrs.get("background_dispatch")),
    ]
    for value in candidates:
        try:
            pid = int(value or 0)
        except (TypeError, ValueError):
            continue
        if pid > 0:
            return pid
    return 0


def _nested_pid(value: object) -> object:
    if isinstance(value, dict):
        return value.get("pid")
    return None


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _terminate_pid(pid: int) -> None:
    if pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return


def _wait_for_pid_exit(pid: int, timeout: float) -> bool:
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if not _is_pid_alive(pid):
            return True
        time.sleep(0.2)
    return not _is_pid_alive(pid)


def _compact_agent_tree(payload: dict[str, object]) -> dict[str, object]:
    nodes = payload.get("nodes", [])
    if isinstance(nodes, list):
        return {
            "nodes": [_compact_tree_node(node) for node in nodes if isinstance(node, dict)],
            "counts": payload.get("counts", {}),
        }
    root = payload.get("tree")
    if isinstance(root, dict):
        return {"tree": _compact_tree_node(root), "counts": payload.get("counts", {})}
    return {"counts": payload.get("counts", {})}


def _compact_tree_node(node: dict[str, object]) -> dict[str, object]:
    compact = {
        "run_id": node.get("run_id") or node.get("id", ""),
        "status": node.get("status", ""),
        "channel_status": node.get("channel_status", ""),
        "failure_type": node.get("failure_type", ""),
    }
    children = node.get("children", [])
    if isinstance(children, list) and children:
        compact["children"] = [_compact_tree_node(child) for child in children if isinstance(child, dict)]
    return compact
