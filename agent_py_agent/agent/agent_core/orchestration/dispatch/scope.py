
from __future__ import annotations

from ....common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from ....subagents.models import TaskStatus, task_has_status
from ...parameters import _bool_param, _non_negative_int
from ...runner.context import current_subagent_run_id
from ..run_scope import remembered_orchestration_run_ids


def dispatch_include_run_ids_param(params: dict[str, object], *, agent: object | None = None) -> list[str]:
    ids = _explicit_run_ids_from_params(params)
    for run_id in _direct_children_run_ids(params, agent):
        _append_unique_id(ids, run_id)
    return ids


def _explicit_run_ids_from_params(params: dict[str, object]) -> list[str]:
    ids: list[str] = []
    for run_id in _run_id_list_value(params.get("run_ids")):
        _append_unique_id(ids, run_id)
    return ids


def _run_id_list_value(value: object) -> list[str]:
    if isinstance(value, (bool, int, float)):
        return []
    return string_list(value, TOOL_TEXT_LIST_OPTIONS)


def _direct_children_run_ids(params: dict[str, object], agent: object | None) -> list[str]:
    if not _bool_param(params.get("direct_children"), default=False):
        return []
    if agent is None or current_subagent_run_id(agent):
        return []
    return sorted(remembered_orchestration_run_ids(agent))


def _append_unique_id(ids: list[str], run_id: str) -> None:
    if run_id and run_id not in ids:
        ids.append(run_id)


def dispatch_apply_default(agent, params: dict[str, object], *, dry_run: bool | None = None) -> bool:
    if dry_run is not None and "dry_run" in params:
        return not dry_run
    if dispatch_include_run_ids_param(params, agent=agent):
        return True
    return bool(current_subagent_run_id(agent))


def dispatch_start_runners_default(agent, params: dict[str, object], *, mutate_state: bool) -> bool:
    if mutate_state and dispatch_include_run_ids_param(params, agent=agent):
        return True
    return bool(mutate_state and current_subagent_run_id(agent))


def dispatch_max_runners_default(agent, params: dict[str, object], *, start_runners: bool | None = None) -> int:
    if "max_runners" in params:
        return _non_negative_int(params.get("max_runners"), default=1)
    explicit_run_ids = dispatch_include_run_ids_param(params, agent=agent)
    if start_runners and explicit_run_ids:
        return len(explicit_run_ids)
    if current_subagent_run_id(agent):
        return 6
    return 1


def dispatch_parent_run_id(agent, params: dict[str, object]) -> str:
    current = current_subagent_run_id(agent)
    if current:
        return current
    explicit = str(params.get("parent_run_id") or "").strip()
    if explicit:
        return explicit
    return ""

def dispatch_exclude_run_ids(agent, params: dict[str, object]) -> list[str]:
    excluded = string_list(params.get("exclude_run_ids"), TOOL_TEXT_LIST_OPTIONS)
    current = current_subagent_run_id(agent)
    if current and current not in excluded:
        excluded.append(current)
    for ancestor_id in _active_ancestor_run_ids(agent, current):
        if ancestor_id not in excluded:
            excluded.append(ancestor_id)
    return excluded


def _active_ancestor_run_ids(agent, current_run_id: str) -> list[str]:
    run_id = str(current_run_id or "").strip()
    if not run_id:
        return []
    ancestors: list[str] = []
    seen: set[str] = {run_id}
    parent_id = _parent_id_for_run(agent, run_id)
    while parent_id and parent_id not in seen:
        seen.add(parent_id)
        try:
            parent = agent.subagents.load(parent_id)
        except Exception:
            ancestors.append(parent_id)
            break
        if not _is_active_ancestor(parent):
            break
        ancestors.append(parent_id)
        parent_id = _safe_run_id(getattr(parent, "parent_id", ""))
    return ancestors


def _parent_id_for_run(agent, run_id: str) -> str:
    try:
        task = agent.subagents.load(run_id)
    except Exception:
        return ""
    return _safe_run_id(getattr(task, "parent_id", ""))


def _is_active_ancestor(task) -> bool:
    active_attempt = _safe_run_id(getattr(task, "runner_active_attempt_id", ""))
    return task_has_status(task, TaskStatus.RUNNING) or bool(active_attempt)


def _safe_run_id(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def dispatch_take_over_by_default(agent, params: dict[str, object]) -> str:
    explicit = str(params.get("take_over_by") or "").strip()
    if explicit:
        return explicit
    return current_subagent_run_id(agent)
