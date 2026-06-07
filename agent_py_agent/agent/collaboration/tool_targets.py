
from __future__ import annotations

from typing import TYPE_CHECKING

from ..agent_core.orchestration.scope_resolution import (
    ScopeResolution,
    identity_scope_resolution,
    scope_resolution_payload,
)
from ..runtime_errors import runtime_error_report
from ..subagents.models import (
    SUBAGENT_DEAD_STATUSES,
    SUBAGENT_HANDLED_TERMINAL_STATUSES,
    task_status_in,
)
from .tool_values import dict_value, string_values

if TYPE_CHECKING:
    from ..core import SimpleAgent

_UNAVAILABLE_TARGET_STATUSES = SUBAGENT_DEAD_STATUSES | SUBAGENT_HANDLED_TERMINAL_STATUSES


def resolved_target_agent_ids(agent: SimpleAgent, params: dict[str, object]) -> list[str]:
    targets, _errors = resolved_target_agent_ids_report(agent, params)
    return targets


def resolved_target_agent_ids_report(agent: SimpleAgent, params: dict[str, object]) -> tuple[list[str], list[dict[str, object]]]:
    raw = string_values(params.get("target_agent_ids"))
    routing = dict_value(params.get("routing_requirements"))
    raw.extend(string_values(routing.get("target_agent_ids")))
    return _resolve_raw_targets_report(agent, raw)


def target_runtime_summary(
    agent: SimpleAgent,
    target_agent_ids: list[str],
    *,
    resolution_errors: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    task_rows, load_error = subagent_tasks_report(agent)
    tasks = {str(getattr(task, "id", "") or ""): task for task in task_rows}
    rows = [_runtime_row(target, tasks.get(str(target or "").strip())) for target in target_agent_ids]
    payload: dict[str, object] = {
        "available_target_run_ids": [row["agent_id"] for row in rows if not row["unavailable_reason"]],
        "unavailable_targets": [{"agent_id": row["agent_id"], "reason": row["unavailable_reason"]} for row in rows if row["unavailable_reason"]],
        "target_statuses": [_status_payload(row) for row in rows],
    }
    if load_error:
        payload["target_runtime_load_error"] = load_error
    if resolution_errors:
        payload["target_resolution_errors"] = list(resolution_errors)
    return payload


def target_runtime_metadata(summary: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    if summary.get("unavailable_targets"):
        result["unavailable_targets"] = summary["unavailable_targets"]
    if summary.get("target_statuses"):
        result["target_statuses"] = summary["target_statuses"]
    if summary.get("target_runtime_load_error"):
        result["target_runtime_load_error"] = summary["target_runtime_load_error"]
    if summary.get("target_resolution_errors"):
        result["target_resolution_errors"] = summary["target_resolution_errors"]
    return result


def target_response_payload(summary: dict[str, object]) -> dict[str, object]:
    available = [str(item) for item in summary.get("available_target_run_ids", []) or [] if str(item or "").strip()]
    payload = {
        "available_target_run_ids": available,
        "unavailable_targets": list(summary.get("unavailable_targets", []) or []),
        "target_statuses": list(summary.get("target_statuses", []) or []),
    }
    if summary.get("target_runtime_load_error"):
        payload["target_runtime_load_error"] = summary["target_runtime_load_error"]
    if summary.get("target_resolution_errors"):
        payload["target_resolution_errors"] = summary["target_resolution_errors"]
    if summary.get("capability_roster_load_error"):
        payload["capability_roster_load_error"] = summary["capability_roster_load_error"]
    if available:
        payload["suggested_dispatch_tool_call"] = _dispatch_suggestion(available)
    return payload


def request_identity(agent: SimpleAgent, params: dict[str, object]) -> dict[str, str]:
    identity, _error = request_identity_report(agent, params)
    return identity


def request_identity_report(agent: SimpleAgent, params: dict[str, object]) -> tuple[dict[str, str], dict[str, object] | None]:
    resolution = collaboration_identity_resolution(
        agent,
        params,
        explicit_keys=("agent_id", "run_id"),
    )
    agent_id = str(resolution.effective.get("agent_id") or "").strip()
    agent_name = str(params.get("agent_name") or "").strip()
    agent_role = str(params.get("agent_role") or "").strip()
    load_error = None
    if agent_id and not (agent_name and agent_role) and _is_known_subagent_run(agent, agent_id):
        agent_name, agent_role, load_error = _loaded_task_identity_report(
            agent,
            agent_id,
            agent_name,
            agent_role,
        )
    current = getattr(agent, "_current_run_params", None)
    agent_id = agent_id or str(getattr(current, "task_id", "") or "").strip()
    return {"agent_id": agent_id, "agent_name": agent_name, "agent_role": agent_role}, load_error


def actor_agent_id(agent: SimpleAgent, params: dict[str, object]) -> str:
    resolution = collaboration_identity_resolution(
        agent,
        params,
        explicit_keys=("created_by", "actor_agent_id", "requester_agent_id", "source_agent_id", "agent_id", "run_id"),
    )
    return str(resolution.effective.get("agent_id") or request_identity(agent, params)["agent_id"]).strip()


def collaboration_identity_resolution(
    agent: SimpleAgent,
    params: dict[str, object],
    *,
    explicit_keys: tuple[str, ...],
) -> ScopeResolution:
    return identity_scope_resolution(agent, params, explicit_keys=explicit_keys)


def collaboration_scope_payload(
    agent: SimpleAgent,
    params: dict[str, object],
    *,
    explicit_keys: tuple[str, ...],
) -> dict[str, object]:
    return scope_resolution_payload(
        collaboration_identity_resolution(agent, params, explicit_keys=explicit_keys)
    )


def subagent_tasks(agent: SimpleAgent) -> list[object]:
    rows, _load_error = subagent_tasks_report(agent)
    return rows


def subagent_tasks_report(agent: SimpleAgent) -> tuple[list[object], dict[str, object] | None]:
    manager = getattr(agent, "subagents", None)
    if manager is None or not hasattr(manager, "list_runs"):
        return [], None
    try:
        return list(manager.list_runs()), None
    except Exception as exc:
        return [], runtime_error_report(exc, context="raise_collaboration.target_runtime")


def _is_known_subagent_run(agent: SimpleAgent, run_id: str) -> bool:
    rows, _load_error = subagent_tasks_report(agent)
    return any(str(getattr(task, "id", "") or "") == run_id for task in rows)


def _resolve_raw_targets_report(agent: SimpleAgent, raw: list[str]) -> tuple[list[str], list[dict[str, object]]]:
    resolved: list[str] = []
    errors: list[dict[str, object]] = []
    for target in raw:
        matches, match_errors = _matching_subagent_run_ids_report(agent, target)
        errors.extend(match_errors)
        _append_resolved_targets(resolved, matches or [target])
    return resolved, errors


def _append_resolved_targets(resolved: list[str], items: list[str]) -> None:
    for item in items:
        if item not in resolved:
            resolved.append(item)


def _matching_subagent_run_ids(agent: SimpleAgent, target: str) -> list[str]:
    matches, _errors = _matching_subagent_run_ids_report(agent, target)
    return matches


def _matching_subagent_run_ids_report(agent: SimpleAgent, target: str) -> tuple[list[str], list[dict[str, object]]]:
    text = str(target or "").strip()
    if not text:
        return [], []
    identity_keys, identity_error = _target_identity_keys_report(agent, text)
    tasks, list_error = subagent_tasks_report(agent)
    errors = [item for item in (identity_error, list_error) if item]
    return [run_id for task in tasks if (run_id := _matching_task_run_id(task, identity_keys))], errors


def _target_identity_keys(agent: SimpleAgent, text: str) -> set[str]:
    identity_keys, _error = _target_identity_keys_report(agent, text)
    return identity_keys


def _target_identity_keys_report(agent: SimpleAgent, text: str) -> tuple[set[str], dict[str, object] | None]:
    try:
        return agent.collaboration_store.agent_identity_keys(text), None
    except Exception as exc:
        return {text}, runtime_error_report(
            exc,
            context="raise_collaboration.agent_identity_keys",
        )


def _matching_task_run_id(task: object, identity_keys: set[str]) -> str:
    run_id = str(getattr(task, "id", "") or "").strip()
    return run_id if run_id and run_id in identity_keys else ""


def _runtime_row(target: str, task: object | None) -> dict[str, str]:
    run_id = str(target or "").strip()
    status = str(getattr(task, "status", "") or "").upper() if task is not None else "UNKNOWN"
    channel = str(getattr(task, "channel_status", "") or "").upper() if task is not None else ""
    return {"agent_id": run_id, "agent_name": str(getattr(task, "agent_name", "") or ""), "status": status, "channel_status": channel, "unavailable_reason": _unavailable_reason(status, channel)}


def _unavailable_reason(status: str, channel_status: str) -> str:
    if status == "UNKNOWN":
        return "unknown_target"
    if channel_status == "BROKEN":
        return "channel_broken"
    return status.lower() if task_status_in(status, _UNAVAILABLE_TARGET_STATUSES) else ""


def _status_payload(row: dict[str, str]) -> dict[str, str]:
    return {key: row[key] for key in ("agent_id", "agent_name", "status", "channel_status")}


def _dispatch_suggestion(available: list[str]) -> dict[str, object]:
    return {"tool": "dispatch_subagents", "dry_run": False, "run_ids": available, "max_runners": len(available), "runner_instruction": "处理点名给自己的 collaboration request；查完后用 submit_collaboration_result 提交命中、未命中、证据引用和限制，然后返回自己的原任务。"}


def _loaded_task_identity(agent: SimpleAgent, agent_id: str, agent_name: str, agent_role: str) -> tuple[str, str]:
    loaded_name, loaded_role, _error = _loaded_task_identity_report(
        agent,
        agent_id,
        agent_name,
        agent_role,
    )
    return loaded_name, loaded_role


def _loaded_task_identity_report(
    agent: SimpleAgent,
    agent_id: str,
    agent_name: str,
    agent_role: str,
) -> tuple[str, str, dict[str, object] | None]:
    try:
        task = agent.subagents.load(agent_id)
    except Exception as exc:
        return agent_name, agent_role, runtime_error_report(
            exc,
            context="collaboration.identity.subagents.load",
        )
    return (
        agent_name or str(getattr(task, "agent_name", "") or ""),
        agent_role or str(getattr(task, "role", "") or ""),
        None,
    )
