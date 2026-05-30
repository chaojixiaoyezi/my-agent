# LLM: Target helpers map model-facing names to durable subagent run IDs when possible.
# 模块用途: 解析协作目标、运行时可用性和当前子代理身份。

from __future__ import annotations

from typing import TYPE_CHECKING

from ..agent_core.orchestration_scope_resolution import (
    ScopeResolution,
    identity_scope_resolution,
    scope_resolution_payload,
)
from .tool_values import dict_value, string_values

if TYPE_CHECKING:
    from ..core import SimpleAgent


def resolved_target_agent_ids(agent: SimpleAgent, params: dict[str, object]) -> list[str]:
    raw = string_values(params.get("target_agent_ids"))
    routing = dict_value(params.get("routing_requirements"))
    for key in ("target_agent_ids", "agent_ids", "run_ids", "responder_agent_ids"):
        raw.extend(string_values(routing.get(key)))
    return _resolve_raw_targets(agent, raw)


def target_runtime_summary(agent: SimpleAgent, target_agent_ids: list[str]) -> dict[str, object]:
    tasks = {str(getattr(task, "id", "") or ""): task for task in subagent_tasks(agent)}
    rows = [_runtime_row(target, tasks.get(str(target or "").strip())) for target in target_agent_ids]
    return {
        "available_target_run_ids": [row["agent_id"] for row in rows if not row["unavailable_reason"]],
        "unavailable_targets": [{"agent_id": row["agent_id"], "reason": row["unavailable_reason"]} for row in rows if row["unavailable_reason"]],
        "target_statuses": [_status_payload(row) for row in rows],
    }


def target_runtime_metadata(summary: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    if summary.get("unavailable_targets"):
        result["unavailable_targets"] = summary["unavailable_targets"]
    if summary.get("target_statuses"):
        result["target_statuses"] = summary["target_statuses"]
    return result


def target_response_payload(summary: dict[str, object]) -> dict[str, object]:
    available = [str(item) for item in summary.get("available_target_run_ids", []) or [] if str(item or "").strip()]
    payload = {"available_target_run_ids": available, "unavailable_targets": list(summary.get("unavailable_targets", []) or []), "target_statuses": list(summary.get("target_statuses", []) or [])}
    if available:
        payload["suggested_dispatch_tool_call"] = _dispatch_suggestion(available)
    return payload


def request_identity(agent: SimpleAgent, params: dict[str, object]) -> dict[str, str]:
    resolution = collaboration_identity_resolution(
        agent,
        params,
        explicit_keys=("agent_id", "run_id"),
    )
    agent_id = str(resolution.effective.get("agent_id") or "").strip()
    agent_name = str(params.get("agent_name") or "").strip()
    agent_role = str(params.get("agent_role") or "").strip()
    if agent_id and not (agent_name and agent_role):
        agent_name, agent_role = _loaded_task_identity(agent, agent_id, agent_name, agent_role)
    current = getattr(agent, "_current_run_params", None)
    agent_id = agent_id or str(getattr(current, "task_id", "") or "").strip()
    return {"agent_id": agent_id, "agent_name": agent_name, "agent_role": agent_role}


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
    manager = getattr(agent, "subagents", None)
    if manager is None or not hasattr(manager, "list_runs"):
        return []
    try:
        return list(manager.list_runs())
    except Exception:
        return []


def _resolve_raw_targets(agent: SimpleAgent, raw: list[str]) -> list[str]:
    resolved: list[str] = []
    for target in raw:
        _append_resolved_targets(resolved, _matching_subagent_run_ids(agent, target) or [target])
    return resolved


def _append_resolved_targets(resolved: list[str], items: list[str]) -> None:
    for item in items:
        if item not in resolved:
            resolved.append(item)


def _matching_subagent_run_ids(agent: SimpleAgent, target: str) -> list[str]:
    text = str(target or "").strip()
    if not text:
        return []
    aliases = _target_aliases(agent, text)
    return [run_id for task in subagent_tasks(agent) if (run_id := _matching_task_run_id(task, aliases))]


def _target_aliases(agent: SimpleAgent, text: str) -> set[str]:
    try:
        return agent.collaboration_store.agent_identity_aliases(text)
    except Exception:
        return {text, text.lower()}


def _matching_task_run_id(task: object, aliases: set[str]) -> str:
    run_id = str(getattr(task, "id", "") or "").strip()
    values = {run_id, run_id.lower(), str(getattr(task, "agent_name", "") or "").strip(), str(getattr(task, "role", "") or "").strip()}
    return run_id if run_id and aliases.intersection(item.lower() if item else item for item in values if item) else ""


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
    return status.lower() if status in {"ABANDONED", "CHANNEL_ERROR", "TIMEOUT", "TAKEN_OVER"} else ""


def _status_payload(row: dict[str, str]) -> dict[str, str]:
    return {key: row[key] for key in ("agent_id", "agent_name", "status", "channel_status")}


def _dispatch_suggestion(available: list[str]) -> dict[str, object]:
    return {"tool": "dispatch_subagents", "dry_run": False, "run_ids": available, "max_runners": len(available), "runner_instruction": "处理点名给自己的 collaboration request；查完后用 submit_collaboration_result 提交命中、未命中、证据引用和限制，然后返回自己的原任务。"}


def _loaded_task_identity(agent: SimpleAgent, agent_id: str, agent_name: str, agent_role: str) -> tuple[str, str]:
    try:
        task = agent.subagents.load(agent_id)
    except Exception:
        return agent_name, agent_role
    return agent_name or str(getattr(task, "agent_name", "") or ""), agent_role or str(getattr(task, "role", "") or "")
