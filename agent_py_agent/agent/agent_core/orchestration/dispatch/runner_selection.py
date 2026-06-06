
from __future__ import annotations

from ....subagents.services.dispatch.params import DispatchRecordParams
from .params import DispatchContext


def scoped_runner_tasks(tasks: list, ctx: DispatchContext) -> list:
    included = [str(item) for item in (ctx.include_run_ids or []) if str(item).strip()]
    include_order = {run_id: index for index, run_id in enumerate(included)}
    scoped = scope_visible_runner_tasks(tasks, ctx)
    if include_order:
        scoped = [task for task in scoped if task.id in include_order]
        scoped.sort(key=lambda task: include_order.get(task.id, len(include_order)))
    return scoped


def scoped_current_turn_runner_tasks(
    tasks: list,
    ctx: DispatchContext,
    *,
    active_run_ids: set[str],
) -> list:
    if ctx.planner:
        return list(tasks)
    if requested_include_ids(ctx):
        return list(tasks)
    if str(getattr(ctx, "parent_run_id", "") or "").strip():
        return list(tasks)
    active_ids = {str(item) for item in active_run_ids if str(item or "").strip()}
    if not active_ids:
        return list(tasks)
    root_ids = _active_scope_root_ids(tasks, active_ids)
    return [
        task for task in tasks
        if _task_in_active_scope(task, active_ids, root_ids)
    ]


def scope_visible_runner_tasks(tasks: list, ctx: DispatchContext) -> list:
    excluded = {str(item) for item in (ctx.exclude_run_ids or []) if str(item).strip()}
    scoped = []
    for task in tasks:
        if task.id in excluded:
            continue
        if ctx.parent_run_id and task.parent_id != ctx.parent_run_id:
            continue
        if ctx.root_id and task.root_id != ctx.root_id:
            continue
        scoped.append(task)
    return scoped


def _active_scope_root_ids(tasks: list, active_ids: set[str]) -> set[str]:
    root_ids: set[str] = set()
    for task in tasks:
        task_id = str(getattr(task, "id", "") or "")
        if task_id not in active_ids:
            continue
        root_id = str(getattr(task, "root_id", "") or task_id).strip()
        if root_id:
            root_ids.add(root_id)
    return root_ids


def _task_in_active_scope(task: object, active_ids: set[str], root_ids: set[str]) -> bool:
    task_id = str(getattr(task, "id", "") or "")
    if task_id in active_ids:
        return True
    root_id = str(getattr(task, "root_id", "") or "")
    return bool(root_id and root_id in root_ids)


def invalid_include_run_ids_record(agent, ctx: DispatchContext, all_tasks: list, scoped_tasks: list):
    requested = requested_include_ids(ctx)
    if not requested:
        return None
    scoped_ids = {task.id for task in scoped_tasks}
    invalid = [run_id for run_id in requested if run_id not in scoped_ids]
    if not invalid:
        return None
    visible_tasks = scope_visible_runner_tasks(all_tasks, ctx)
    visible_ids = [task.id for task in visible_tasks]
    suggestions = run_id_suffix_suggestions(invalid, visible_ids)
    return agent.subagents.dispatch.make_dispatch_record(
        params=DispatchRecordParams(
            step="runner_selection",
            action="invalid_run_ids",
            dry_run=ctx.preview_only,
            applied=False,
            ok=False,
            message=invalid_include_run_ids_message(invalid, visible_ids, suggestions),
            evidence_paths=[str(task.task_dir) for task in visible_tasks[:20] if task.task_dir],
        ),
    )


def requested_include_ids(ctx: DispatchContext) -> list[str]:
    requested: list[str] = []
    seen: set[str] = set()
    for item in ctx.include_run_ids or []:
        run_id = str(item).strip()
        if not run_id or run_id in seen:
            continue
        requested.append(run_id)
        seen.add(run_id)
    return requested


def run_id_suffix_suggestions(invalid_ids: list[str], visible_ids: list[str]) -> dict[str, str]:
    by_suffix: dict[str, list[str]] = {}
    for run_id in visible_ids:
        suffix = _run_id_suffix(run_id)
        if suffix:
            by_suffix.setdefault(suffix, []).append(run_id)
    suggestions: dict[str, str] = {}
    for run_id in invalid_ids:
        matches = by_suffix.get(_run_id_suffix(run_id), [])
        if len(matches) == 1:
            suggestions[run_id] = matches[0]
    return suggestions


def _run_id_suffix(run_id: str) -> str:
    parts = str(run_id or "").split("-")
    return parts[-1] if len(parts) >= 3 else ""


def invalid_include_run_ids_message(
    invalid_ids: list[str],
    visible_ids: list[str],
    suggestions: dict[str, str],
) -> str:
    parts = [
        "blocked: include_run_ids 包含不存在或不属于当前 parent/root scope 的 run_id。",
        f"invalid_run_ids={invalid_ids}",
        f"valid_scope_run_ids={visible_ids}",
    ]
    if suggestions:
        parts.append(f"possible_corrections={suggestions}")
    parts.append("请使用 schedule_child_subagents 返回的 created_run_ids 原样重试。")
    return " ".join(parts)
