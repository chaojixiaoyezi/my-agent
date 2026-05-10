# LLM: Dispatch scope helpers keep nested runner dispatch explicit and testable.
# 模块用途: 集中处理 runner 内 dispatch 的 parent/root/exclude/finalize 默认值，避免主工具入口膨胀。

from __future__ import annotations

from .parameters import _bool_param, _non_negative_int, _string_list
from .runner_context import current_subagent_run_id


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


# LLM: dispatch_execute_acceptance_tests_default makes runner parents validate direct children after execution.
# 函数用途: runner 内部调度默认跑父级验收 tests；顶层和显式 false 仍保持原来的非自动执行边界。
def dispatch_execute_acceptance_tests_default(agent, params: dict[str, object], *, apply: bool) -> bool:
    if "execute_acceptance_tests" in params:
        return _bool_param(params.get("execute_acceptance_tests"), default=False)
    return bool(apply and current_subagent_run_id(agent))


# LLM: dispatch_auto_apply_acceptance_followup_default closes runner-context children after passed tests.
# 函数用途: 顶层仍保留人工 follow-up；runner 内 apply+tests 通过后默认落子节点验收状态。
def dispatch_auto_apply_acceptance_followup_default(
    agent,
    params: dict[str, object],
    *,
    apply: bool,
    execute_acceptance_tests: bool,
) -> bool:
    if "auto_apply_acceptance_followup" in params:
        return _bool_param(params.get("auto_apply_acceptance_followup"), default=False)
    return bool(apply and execute_acceptance_tests and current_subagent_run_id(agent))


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


# LLM: dispatch_finalize_acceptance lets runner parents write acceptance/test refs for direct children.
# 函数用途: runner 内部 dispatch 默认进入验收阶段以捕获 child 测试结果；显式 false 仍可只推进执行。
def dispatch_finalize_acceptance(agent, params: dict[str, object]) -> bool:
    if "finalize_acceptance" in params:
        return _bool_param(params.get("finalize_acceptance"), default=True)
    return True
