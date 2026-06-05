
from __future__ import annotations

from ....common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from ...parameters import _bool_param, _non_negative_int
from ...runner.context import current_subagent_run_id
from ...spawn_role_seed import is_explicit_root_role
from .run_ids import dispatch_include_run_ids_param

_DISPATCH_FINAL_STATUSES = {"DONE", "FAILED", "TIMEOUT", "CHANNEL_ERROR", "TAKEN_OVER"}


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


def dispatch_workflow_mode(agent, params: dict[str, object], parser) -> str:
    if _top_level_root_role_dispatch(agent, params):
        return "off"
    if "workflow_mode" in params:
        return parser(params.get("workflow_mode"), agent.config.subagent_workflow_mode)
    if _explicit_workflow_off_target_dispatch(agent, params):
        return "off"
    if current_subagent_run_id(agent):
        return "off"
    return parser(None, agent.config.subagent_workflow_mode)


def _explicit_workflow_off_target_dispatch(agent, params: dict[str, object]) -> bool:
    if current_subagent_run_id(agent):
        return False
    if "dry_run" in params and _bool_param(params.get("dry_run"), default=True):
        return False
    run_ids = dispatch_include_run_ids_param(params, agent=agent)
    if not run_ids:
        return False
    return all(_target_workflow_mode(agent, run_id) == "off" for run_id in run_ids)


def _target_workflow_mode(agent, run_id: str) -> str:
    try:
        task = agent.subagents.load(run_id)
    except Exception:
        return ""
    return str(getattr(task, "workflow_mode", "") or "").strip().lower()


def _top_level_root_role_dispatch(agent, params: dict[str, object]) -> bool:
    if current_subagent_run_id(agent):
        return False
    if "dry_run" in params and _bool_param(params.get("dry_run"), default=True):
        return False
    requested_mode = str(params.get("workflow_mode") or agent.config.subagent_workflow_mode or "").strip().lower()
    if requested_mode not in {"plan", "auto", "manual"}:
        return False
    try:
        runs = agent.subagents.list_runs()
    except Exception:
        return False
    return any(_is_active_root_role_task(task, agent) for task in runs)


def _is_active_root_role_task(task, agent) -> bool:
    return (
        not str(getattr(task, "parent_id", "") or "").strip()
        and is_explicit_root_role(str(getattr(task, "role", "") or ""), getattr(agent.subagents, "role_template_dirs", None))
        and str(getattr(task, "status", "") or "").upper() not in _DISPATCH_FINAL_STATUSES
    )


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
    status = str(getattr(task, "status", "") or "").upper()
    active_attempt = _safe_run_id(getattr(task, "runner_active_attempt_id", ""))
    return status == "RUNNING" or bool(active_attempt)


def _safe_run_id(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def dispatch_take_over_by_default(agent, params: dict[str, object]) -> str:
    explicit = str(params.get("take_over_by") or "").strip()
    if explicit:
        return explicit
    return current_subagent_run_id(agent)
