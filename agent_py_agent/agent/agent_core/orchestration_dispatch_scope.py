# LLM: Dispatch scope helpers keep nested runner dispatch explicit and testable.
# 模块用途: 集中处理 runner 内 dispatch 的 parent/root/exclude/finalize 默认值，避免主工具入口膨胀。

from __future__ import annotations

import re

from .parameters import _bool_param, _non_negative_int, _string_list
from .runner_context import current_subagent_run_id
from .spawn_role_seed import is_explicit_root_role

_DISPATCH_FINAL_STATUSES = {"DONE", "FAILED", "TIMEOUT", "CHANNEL_ERROR", "TAKEN_OVER"}
_CONCRETE_FILE_TARGET_RE = re.compile(
    r"[\w.-]+\.(?:html|css|js|mjs|cjs|ts|tsx|jsx|py|md|json|yaml|yml|txt|csv|vue|svelte)\b",
    re.IGNORECASE,
)
_NON_WORKER_ROLE_TOKENS = {"acceptor", "bug_finder", "checker", "coordinator", "critic", "qa", "reviewer", "tester"}


# LLM: dispatch_apply_default keeps top-level dispatch safe while runner-context dispatch can actually advance children.
# 函数用途: 顶层工具省略 apply 时继续 dry-run；runner 内部省略 apply 时默认推进当前节点直接孩子。
def dispatch_apply_default(agent, params: dict[str, object]) -> bool:
    if "apply" in params:
        return _bool_param(params.get("apply"), default=False)
    return bool(current_subagent_run_id(agent))


# LLM: dispatch_execute_runners_default matches runner-context apply default without overriding explicit false.
# 函数用途: runner 内部未显式设置 execute_runners 时默认真实执行直接 child；顶层仍保持不执行。
def dispatch_execute_runners_default(agent, params: dict[str, object], *, apply: bool) -> bool:
    if "execute_runners" in params:
        return _bool_param(params.get("execute_runners"), default=False)
    return bool(apply and current_subagent_run_id(agent))


# LLM: dispatch_execute_acceptance_tests_default validates real runner output at every dispatch boundary.
# 函数用途: 真实执行 runner 时默认跑父级验收 tests；显式 false 仍可关闭，dry-run 不执行。
def dispatch_execute_acceptance_tests_default(
    agent,
    params: dict[str, object],
    *,
    apply: bool,
    execute_runners: bool,
) -> bool:
    if "execute_acceptance_tests" in params:
        return _bool_param(params.get("execute_acceptance_tests"), default=False)
    return bool(apply and execute_runners)


# LLM: dispatch_auto_apply_acceptance_followup_default only applies machine-proven acceptance follow-ups.
# 函数用途: 真实 runner 的验收 tests 全通过后默认落状态；失败、dry-run 或显式 false 都不会自动闭环。
def dispatch_auto_apply_acceptance_followup_default(
    agent,
    params: dict[str, object],
    *,
    apply: bool,
    execute_runners: bool,
    execute_acceptance_tests: bool,
) -> bool:
    if "auto_apply_acceptance_followup" in params:
        return _bool_param(params.get("auto_apply_acceptance_followup"), default=False)
    return bool(apply and execute_runners and execute_acceptance_tests)


# LLM: dispatch_max_runners_default prevents runner-context parents from advancing only one child and timing out.
# 函数用途: 顶层默认每轮 1 个 runner；runner 内部默认推进最多 6 个直接 child，显式参数优先。
def dispatch_max_runners_default(agent, params: dict[str, object]) -> int:
    if "max_runners" in params:
        return _non_negative_int(params.get("max_runners"), default=1)
    if current_subagent_run_id(agent):
        return 6
    return 1


# LLM: dispatch_workflow_mode keeps nested runner dispatch from spawning workflow workers accidentally.
# 函数用途: 顶层 dispatch 继续跟随配置；runner 内部未显式指定时默认 off，避免层级测试被自动 workflow 打散。
def dispatch_workflow_mode(agent, params: dict[str, object], parser) -> str:
    if _top_level_root_role_dispatch(agent, params):
        return "off"
    if _top_level_concrete_worker_dispatch(agent, params):
        return "off"
    if "workflow_mode" in params:
        return parser(params.get("workflow_mode"), agent.config.subagent_workflow_mode)
    if current_subagent_run_id(agent):
        return "off"
    return parser(None, agent.config.subagent_workflow_mode)


# LLM: _top_level_root_role_dispatch protects coordinator-owned hierarchy from generic workflow auto-splitting.
# 函数用途: 顶层正在执行 root/coordinator 时关闭 workflow 自动拆分，避免绕过该 coordinator 自己创建孩子。
def _top_level_root_role_dispatch(agent, params: dict[str, object]) -> bool:
    if current_subagent_run_id(agent):
        return False
    if not _bool_param(params.get("apply"), default=False):
        return False
    requested_mode = str(params.get("workflow_mode") or agent.config.subagent_workflow_mode or "").strip().lower()
    if requested_mode not in {"plan", "auto", "manual"}:
        return False
    try:
        runs = agent.subagents.list_runs()
    except Exception:
        return False
    return any(_is_active_root_role_task(task) for task in runs)


# LLM: _top_level_concrete_worker_dispatch prevents simple one-file workers from growing generic workflow tails.
# 函数用途: 顶层推进明确文件交付 worker 时，即使模型误传 workflow_mode=auto，也关闭 producer/critic/repair 自动扩展。
def _top_level_concrete_worker_dispatch(agent, params: dict[str, object]) -> bool:
    if current_subagent_run_id(agent):
        return False
    if not _bool_param(params.get("apply"), default=False):
        return False
    requested_mode = str(params.get("workflow_mode") or agent.config.subagent_workflow_mode or "").strip().lower()
    if requested_mode not in {"plan", "auto", "manual"}:
        return False
    try:
        runs = agent.subagents.list_runs()
    except Exception:
        return False
    return any(_is_active_concrete_worker_task(task) for task in runs)


# LLM: _is_active_root_role_task identifies explicit root/coordinator runs that should own child creation.
# 函数用途: 判断任务是否是未结束的顶层协调节点，用于阻止 dispatch workflow 污染层级。
def _is_active_root_role_task(task) -> bool:
    return (
        not str(getattr(task, "parent_id", "") or "").strip()
        and is_explicit_root_role(str(getattr(task, "role", "") or ""))
        and str(getattr(task, "status", "") or "").upper() not in _DISPATCH_FINAL_STATUSES
    )


# LLM: _is_active_concrete_worker_task mirrors create-time workflow disabling at dispatch time.
# 函数用途: 判断顶层普通 worker 是否已经有明确文件交付目标；这类任务先让 worker 自己完成，质量波次后置。
def _is_active_concrete_worker_task(task) -> bool:
    if str(getattr(task, "parent_id", "") or "").strip():
        return False
    if str(getattr(task, "status", "") or "").upper() in _DISPATCH_FINAL_STATUSES:
        return False
    role = str(getattr(task, "role", "") or "worker").strip().lower().replace("-", "_")
    if role and (role in _NON_WORKER_ROLE_TOKENS or any(token in role for token in _NON_WORKER_ROLE_TOKENS)):
        return False
    goal = str(getattr(task, "goal", "") or "")
    return bool(_CONCRETE_FILE_TARGET_RE.search(goal))


# LLM: dispatch_parent_run_id scopes runner-context dispatch to the current node's direct children.
# 函数用途: runner 内部未显式传 parent_run_id 时默认使用当前 run，避免调度器重跑自己。
def dispatch_parent_run_id(agent, params: dict[str, object]) -> str:
    explicit = str(params.get("parent_run_id") or "").strip()
    if explicit:
        return explicit
    return current_subagent_run_id(agent)


# LLM: dispatch_exclude_run_ids ensures nested dispatch never selects the active runner itself.
# 函数用途: 合并显式排除列表和当前 runner id，传给 runner 候选过滤。
def dispatch_exclude_run_ids(agent, params: dict[str, object]) -> list[str]:
    excluded = _string_list(params.get("exclude_run_ids"))
    current = current_subagent_run_id(agent)
    if current and current not in excluded:
        excluded.append(current)
    for ancestor_id in _active_ancestor_run_ids(agent, current):
        if ancestor_id not in excluded:
            excluded.append(ancestor_id)
    return excluded


# LLM: _active_ancestor_run_ids protects live parent runners during nested dispatch.
# 函数用途: 子/孙 runner 调度自己的孩子时，把仍在执行的祖先排除在 due-check/action-apply 外，避免误接管活父级。
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
            break
        if not _is_active_ancestor(parent):
            break
        ancestors.append(parent_id)
        parent_id = _safe_run_id(getattr(parent, "parent_id", ""))
    return ancestors


# LLM: _parent_id_for_run keeps MagicMock/default values from becoming fake run ids.
# 函数用途: 安全读取当前 run 的 parent_id；测试桩或缺失字段直接返回空字符串。
def _parent_id_for_run(agent, run_id: str) -> str:
    try:
        task = agent.subagents.load(run_id)
    except Exception:
        return ""
    return _safe_run_id(getattr(task, "parent_id", ""))


# LLM: _is_active_ancestor mirrors heartbeat propagation without importing due-check internals.
# 函数用途: 判断祖先是否还在真实执行中；只有活祖先才从当前 nested dispatch 的恢复扫描中排除。
def _is_active_ancestor(task) -> bool:
    status = str(getattr(task, "status", "") or "").upper()
    active_attempt = _safe_run_id(getattr(task, "runner_active_attempt_id", ""))
    return status == "RUNNING" or bool(active_attempt)


# LLM: _safe_run_id accepts only concrete string ids from task records.
# 函数用途: 避免 MagicMock、None 或非字符串对象被 str() 后误当成真实 run_id。
def _safe_run_id(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


# LLM: dispatch_take_over_by_default keeps recovery apply usable inside parent runners.
# 函数用途: runner 内部未显式传接管者时默认使用当前父级 run id，避免模型因漏传底层参数而无法接管失联孩子。
def dispatch_take_over_by_default(agent, params: dict[str, object]) -> str:
    explicit = str(params.get("take_over_by") or "").strip()
    if explicit:
        return explicit
    return current_subagent_run_id(agent)


# LLM: dispatch_finalize_acceptance lets runner parents write acceptance/test refs for direct children.
# 函数用途: runner 内部 dispatch 默认进入验收阶段以捕获 child 测试结果；显式 false 仍可只推进执行。
def dispatch_finalize_acceptance(agent, params: dict[str, object]) -> bool:
    if "finalize_acceptance" in params:
        return _bool_param(params.get("finalize_acceptance"), default=True)
    return True
