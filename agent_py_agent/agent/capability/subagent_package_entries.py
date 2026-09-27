# LLM: 子入口只消费显式授权与同代 pin，沿原首请求资格和 RuntimeFacts；不选包、不调用模型、不创建任务或改包引用。
# 模块用途: 在原创建时准备宿主资格，真实子请求 build/capture 前一次读取有界入口，具体方法仍按需读取。
from __future__ import annotations

from concurrent.futures import CancelledError
from dataclasses import replace

from ..common.cancellation import ToolCancelled
from .package_selection_context import prepare_package_entry_context
from .package_selection_scope import PackageSelectionScope
from .runtime_config_reload import capability_config_for_agent
from .subagent_entry_authority import SubagentEntryAuthority, SubagentEntryInitialization
from .task_references import normalize_skill_reference, task_skill_references


# LLM: 只读配置；关闭不读取 Skill 快照/包正文，也不初始化原首请求标记。
# 函数用途: 根和递归创建共用同一个包入口开关，默认路径保持原样。
def subagent_entries_enabled(agent: object) -> bool:
    config = capability_config_for_agent(agent)
    return bool(config is not None and config.enable_capability_package_selection)


# LLM: 当前 scoped 快照只作为权限上界；引用来自孩子 canonical 规格，不从描述、父正文或当前安装猜版本。
# 函数用途: 返回明确授权、身份完整且与当前允许快照同代的包引用，去掉重复引用。
def _authorized_references(task: object, snapshot: object) -> list[dict[str, str]]:
    allowed = set(task.allowed_skills or ())
    references = []
    for value in task_skill_references(task):
        ref = normalize_skill_reference(value)
        if ref.get("kind") != "capability_package" or ref["stable_id"] not in allowed:
            continue
        package = snapshot.resolve_package(ref["package_id"])
        if package is not None and package.to_ref() == ref and ref not in references:
            references.append(ref)
    return references


# LLM: 原宿主在最终创建事务内传递 typed 初始化材料；不修改原 task/advice，不向模型暴露新增参数。
# 函数用途: 仅为有同代包授权的新孩子准备首请求资格；无权限或坏引用保持无资格，不读取正文。
def prepare_subagent_entry_initialization(agent: object, prepared: object):
    initialization = None
    task = prepared.task
    if (subagent_entries_enabled(agent) and getattr(agent.config, "enable_tools", False)
            and getattr(agent.config, "enable_plugins", False) and "skill_search" in task.allowed_tools):
        try:
            if _authorized_references(task, agent.current_skill_snapshot()):
                initialization = SubagentEntryInitialization(task.id, task.agent_thread_id)
        except (InterruptedError, ToolCancelled, CancelledError):
            raise
        except (ValueError, RuntimeError):
            pass
    return replace(prepared, package_entry_initialization=initialization)


# LLM: 原 scope 领取资格后，本地 prepared 标记只防同次 build 重入，不构成持久资格；恢复绝不从空历史推断首次。
# 函数用途: 在完整提示冻结前准备孩子有权读取的入口，成功内容和失败诊断沿原 IR，不增加模型调用。
def prepare_subagent_package_entries(agent: object, params: object) -> None:
    from ..agent_core.subagent.model_selection import first_request_preparation
    from ..agent_core.tool_ir_history import record_runtime_facts_turn_ir
    from ..agent_core.tool_loop.recovery import runtime_run_scope

    if not subagent_entries_enabled(agent):
        return
    preparation = first_request_preparation(agent, params)
    if preparation is None or preparation.package_entries_prepared:
        return
    preparation.package_entries_prepared = True
    with agent.subagents.creation_guard():
        task = agent.subagents.load(params.run_id)
        authority = SubagentEntryAuthority(agent, params, preparation, task.agent_thread_id, runtime_run_scope(agent, params))
        authority.check()
        try:
            context = _prepare_entries(agent, params, task, authority)
        except (InterruptedError, ToolCancelled, CancelledError):
            raise
        except Exception:
            authority.check()
            record_runtime_facts_turn_ir(params, "能力包入口准备提示：CAPABILITY_ENTRY_PREPARATION_UNAVAILABLE",
                                         source="capability_package_entry_warning")
            return
        authority.check()
        if context is None:
            return
        if context.text:
            record_runtime_facts_turn_ir(params, context.text, source="capability_package_entries")
        if context.warning_codes:
            record_runtime_facts_turn_ir(params, "能力包入口准备提示：" + ", ".join(context.warning_codes),
                                         source="capability_package_entry_warning")


# LLM: 只读入口复用原 ActionPolicy、共享 reader 和总预算；child 的 pin 路径只验证既有引用，不写 ThreadTaskLink。
# 函数用途: 在冻结工具与包快照交集内加载可呈现的页，预算不足保留续读回执而不执行包脚本。
def _prepare_entries(agent: object, params: object, task: object, authority: SubagentEntryAuthority):
    from ..agent_core.model.call_runtime import max_output_tokens
    from ..agent_core.model.context_window import resolve_model_context_window_tokens

    if not (getattr(agent.config, "enable_tools", False) and getattr(agent.config, "enable_plugins", False)):
        return None
    tools = params.tool_runtime_snapshot
    runtime = tools.runtime("skill_search") if tools is not None else None
    if ("skill_search" not in task.allowed_tools or runtime is None
            or not runtime.availability.available or not runtime.exposure.model_visible):
        return None
    snapshot = agent.current_skill_snapshot()
    references = _authorized_references(task, snapshot)
    config = capability_config_for_agent(agent)
    budget = config.capability_bundle_max_tokens
    if type(budget) is not int or budget < 0:
        raise ValueError("CAPABILITY_ENTRY_BUDGET_INVALID")
    window = max(1, resolve_model_context_window_tokens(agent) - max_output_tokens(agent))
    return prepare_package_entry_context(
        agent, params, PackageSelectionScope(config, snapshot, tools), references,
        authority=authority, claim_id=params.attempt_id, max_tokens=min(budget, window) if budget else window,
    )
