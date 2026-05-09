# LLM: Dispatch scope helpers keep nested runner dispatch explicit and testable.
# 模块用途: 集中处理 runner 内 dispatch 的 parent/root/exclude/finalize 默认值，避免主工具入口膨胀。

from __future__ import annotations

from .parameters import _bool_param, _string_list
from .runner_context import current_subagent_run_id


# LLM: dispatch_workflow_mode keeps nested runner dispatch from spawning workflow workers accidentally.
# 函数用途: 顶层 dispatch 继续跟随配置；runner 内部未显式指定时默认 off，避免层级测试被自动 workflow 打散。
def dispatch_workflow_mode(agent, params: dict[str, object], parser) -> str:
    if "workflow_mode" in params:
        return parser(params.get("workflow_mode"), agent.config.subagent_workflow_mode)
    if current_subagent_run_id(agent):
        return "off"
    return parser(None, agent.config.subagent_workflow_mode)


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
    return excluded


# LLM: dispatch_finalize_acceptance defers acceptance when a child runner is launched from its parent runner.
# 函数用途: runner 内部 dispatch 默认只推进子节点执行，不立刻替父级验收；显式参数仍可覆盖。
def dispatch_finalize_acceptance(agent, params: dict[str, object]) -> bool:
    if "finalize_acceptance" in params:
        return _bool_param(params.get("finalize_acceptance"), default=True)
    if current_subagent_run_id(agent):
        return False
    return True
