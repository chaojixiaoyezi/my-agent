# LLM: Runner selection helpers keep scoped dispatch id validation outside batch execution.
# 模块用途: 处理 runner 候选范围、显式 run_id 预检和错 id 诊断，避免执行器批处理逻辑变大。

from __future__ import annotations

from ..subagents.services.dispatch_params import DispatchRecordParams
from .dispatch_params import DispatchContext


# LLM: scoped_runner_tasks keeps nested dispatch focused and can honor explicit child order.
# 函数用途: 根据 include/parent/root/exclude 过滤 runner 候选；runner 可精确指定本轮要跑的 direct child ids。
def scoped_runner_tasks(tasks: list, ctx: DispatchContext) -> list:
    included = [str(item) for item in (ctx.include_run_ids or []) if str(item).strip()]
    include_order = {run_id: index for index, run_id in enumerate(included)}
    scoped = scope_visible_runner_tasks(tasks, ctx)
    if include_order:
        scoped = [task for task in scoped if task.id in include_order]
        scoped.sort(key=lambda task: include_order.get(task.id, len(include_order)))
    return scoped


# LLM: scope_visible_runner_tasks applies parent/root/exclude scope without narrowing explicit include ids.
# 函数用途: 计算当前 dispatch 能看到的 runner 范围；用于候选过滤和错误提示里的“可用 direct child ids”。
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


# LLM: invalid_include_run_ids_record stops a runner parent from silently pursuing hallucinated child ids.
# 函数用途: 当 include_run_ids 不存在或不在当前 parent/root 范围内时返回阻断记录，并提示可用 direct child ids。
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
    return agent.subagents.make_dispatch_record(
        params=DispatchRecordParams(
            step="runner_selection",
            action="invalid_run_ids",
            dry_run=not ctx.apply,
            applied=False,
            ok=False,
            message=invalid_include_run_ids_message(invalid, visible_ids, suggestions),
            evidence_paths=[str(task.task_dir) for task in visible_tasks[:20] if task.task_dir],
        ),
    )


# LLM: requested_include_ids normalizes model-provided run refs while preserving order for diagnostics.
# 函数用途: 提取 include_run_ids 中的非空字符串，去重但保留首次出现顺序，方便返回给父节点重试。
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


# LLM: run_id_suffix_suggestions offers exact child-id repair hints without auto-correcting state.
# 函数用途: 对“前缀时间戳抄错但短后缀相同”的 id 给出可能纠正项；只提示，不替模型执行。
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


# LLM: _run_id_suffix keeps correction matching conservative and format-agnostic.
# 函数用途: 获取 run_id 最后一段短后缀；格式不匹配时返回空字符串，避免误判。
def _run_id_suffix(run_id: str) -> str:
    parts = str(run_id or "").split("-")
    return parts[-1] if len(parts) >= 3 else ""


# LLM: invalid_include_run_ids_message keeps the recovery instruction compact and copy-safe for LLM parents.
# 函数用途: 生成错 run_id 阻断消息，包含 invalid_run_ids、valid_scope_run_ids 和可选 possible_corrections。
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
