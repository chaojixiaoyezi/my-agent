
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...subagents.role_templates import role_template_snapshot_for_task

if TYPE_CHECKING:
    from ...subagents import SubAgentTask


def runner_timeout_disabled(config: Any) -> bool:
    raw_value = getattr(config, "runner_timeout_seconds", "off")
    if isinstance(raw_value, str):
        return raw_value.strip().lower() in {"off", "none", "disabled", "false", "no", "0"}
    try:
        return float(raw_value) == 0.0
    except (TypeError, ValueError):
        return False


def get_task_timeout(task: SubAgentTask, runner_timeout_seconds: float, config: Any) -> float:
    from ..dynamic_timeout import calculate_dynamic_timeout, estimate_task_tokens
    from .dispatch import _resolve_runner_timeout_seconds

    role_override = _runner_timeout_for_task_role(task, config)
    if role_override is not None:
        if _runner_timeout_value_disabled(role_override):
            return 0.0
        resolved_override = _resolve_runner_timeout_seconds(role_override)
        if resolved_override > 0:
            return resolved_override
        if not _runner_timeout_value_auto(role_override):
            role_override = None

    if role_override is None and runner_timeout_seconds > 0:
        return runner_timeout_seconds
    if role_override is None and runner_timeout_disabled(config):
        return 0.0

    if task.attributes and "dynamic_timeout_seconds" in task.attributes:
        timeout = float(task.attributes["dynamic_timeout_seconds"])
        if timeout > 0:
            return timeout

    estimated_input_tokens, estimated_output_tokens = estimate_task_tokens(task.goal, task.plan)
    return calculate_dynamic_timeout(config, estimated_input_tokens, estimated_output_tokens)


def resolve_runner_config(config: Any, job_count: int) -> tuple[float, int, int]:
    from .dispatch import (
        _resolve_runner_concurrency,
        _resolve_runner_start_rate,
        _resolve_runner_timeout_seconds,
    )

    runner_start_rate = _resolve_runner_start_rate(config.runner_start_rate, job_count)
    if runner_start_rate and runner_start_rate < job_count:
        job_count = runner_start_rate
    runner_concurrency = _resolve_runner_concurrency(
        config.runner_concurrency,
        job_count,
        auto_limit=getattr(config, "runner_auto_concurrency", job_count),
    )
    runner_timeout_seconds = _resolve_runner_timeout_seconds(config.runner_timeout_seconds)
    return runner_timeout_seconds, runner_concurrency, runner_start_rate


def _runner_timeout_for_task_role(task: SubAgentTask, config: Any) -> object | None:
    mapping = getattr(config, "runner_timeout_by_role", {}) or {}
    if not isinstance(mapping, dict):
        return None
    keys = _runner_timeout_role_keys(task)
    normalized = {str(key).strip().lower(): value for key, value in mapping.items()}
    for key in keys:
        if key in normalized:
            return normalized[key]
    return normalized.get("default", normalized.get("*"))


def _runner_timeout_role_keys(task: SubAgentTask) -> list[str]:
    keys: list[str] = []
    run_id = str(getattr(task, "id", "") or "")
    parent_id = str(getattr(task, "parent_id", "") or "")
    root_id = str(getattr(task, "root_id", "") or "")
    role = str(getattr(task, "role", "") or "").strip().lower()
    if (not parent_id or (run_id and root_id and run_id == root_id)) and _runner_timeout_root_like_role(role, task):
        keys.append("root")
    if str((getattr(task, "attributes", {}) or {}).get("takeover_source_run_id") or "").strip():
        keys.append("takeover")
    if role:
        keys.append(role)
    if role in {"child_worker", "leaf_worker"}:
        keys.append("worker")
    return keys


def _runner_timeout_root_like_role(role: str, task: SubAgentTask | None = None) -> bool:
    if not str(role or "").strip():
        return True
    return bool(role_template_snapshot_for_task(task).get("can_spawn_children")) if task is not None else False


def _runner_timeout_value_disabled(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"off", "none", "disabled", "false", "no", "0"}
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def _runner_timeout_value_auto(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() == "auto"
