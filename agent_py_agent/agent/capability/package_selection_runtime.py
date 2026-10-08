# LLM: 仅真实主业务请求在 build/capture 前调用一次准备；原 TaskLink CAS 拥有幂等性，模型 I/O 不持锁，最终正文沿原 IR 进入预算和历史。
# 模块用途: 接起新主任务的选包、入口读取与上下文装配；失败只告警，旧任务、孩子、已领取或已结束标记不重放。
from __future__ import annotations

import logging
from concurrent.futures import CancelledError

from ..agent_core.model.call_runtime import max_output_tokens
from ..agent_core.model.context_window import resolve_model_context_window_tokens
from ..agent_core.tool_ir_history import record_runtime_facts_turn_ir
from ..common.cancellation import ToolCancelled, bind_cancellation_token
from ..conversation.authority import CONVERSATION_WORKSPACE_TASK_ID_ATTR
from ..conversation.task_promotion import promote_conversation_task_for_run
from .package_selection import build_package_selection_material, select_capability_packages
from .package_selection_authority import PackageSelectionAuthority, package_selection_model_digest
from .package_selection_context import prepare_package_entry_context
from .package_selection_scope import package_selection_scope


# LLM: 关闭路径不碰任务或历史；已存在但无标记的 TaskLink 永不补选，损坏标记保留证据并警告。取消不能被可选失败吞掉。
# 函数用途: 在原主模型请求准备前做一次受控选包，后续请求仍走原生成、选模与工具循环。
def prepare_capability_package_selection(agent: object, params: object) -> None:
    try:
        scope = package_selection_scope(agent, params)
        if scope is None:
            return
        link = _selection_task(agent, params)
        if link is None:
            return
        warning_codes = link.capability_selection_warning_codes
        if warning_codes:
            _selection_warning(params, warning_codes)
            return
        marker = link.capability_selection
        if marker is None or marker.status != "pending" or link.status != "active":
            return
        _prepare_pending_selection(agent, params, scope, link)
    except (InterruptedError, ToolCancelled, CancelledError):
        raise
    except Exception as exc:
        logging.getLogger(__name__).warning("CAPABILITY_SELECTION_PREPARATION_UNAVAILABLE: %s", type(exc).__name__)
        _selection_warning(params, ("CAPABILITY_SELECTION_PREPARATION_UNAVAILABLE",))


# LLM: 只加载本轮精确 task；无链接或明确选中终态cwd时委托原晋升裁决，新successor可初始化而旧同task缺键仍跳过。
# 函数用途: 获取当前任务，按原结构化续作边界绑定新执行代，不搜索thread最新任务或自行复活旧Goal。
def _selection_task(agent: object, params: object):
    attrs = getattr(params, "task_attributes", None)
    if not isinstance(attrs, dict) or not attrs.get("conversation_thread_id"):
        return None
    task_id = str(attrs.get("conversation_task_id") or params.task_id or "")
    store = getattr(agent, "conversation_store", None)
    if store is None or not task_id:
        return None
    link = store.tasks.load(task_id)
    if link is not None:
        if link.thread_id != attrs["conversation_thread_id"]:
            return None
        exact = link.task_id in (attrs.get("conversation_task_id"), attrs.get(CONVERSATION_WORKSPACE_TASK_ID_ATTR))
        if not exact or link.status not in {"completed", "interrupted"}:
            return link
    params.cancellation_token.raise_if_cancelled()
    return promote_conversation_task_for_run(agent, params)


# LLM: 选择只传独立输入预算与实际窗口，不传推荐上限；claim 先于辅助 I/O，同一 claim 收口，崩溃不重试，入口与子代理合同不变。
# 函数用途: 准备有界候选、领取一次选择，调用当前主后端后在原事务内提交入口和回执。
def _prepare_pending_selection(agent, params, scope, link) -> None:
    window = max(1, resolve_model_context_window_tokens(agent) - max_output_tokens(agent))
    material = build_package_selection_material(
        params.root_user_prompt or params.user_prompt, scope.skills.packages,
        max_input_tokens=scope.config.capability_package_selection_max_input_tokens,
        context_window_tokens=window,
    )
    authority = PackageSelectionAuthority(agent, params, link.task_id, link.thread_id,
                                          params.request_id, params.run_id, params.attempt_id)
    tasks = agent.conversation_store.tasks
    claim = authority.atomic(lambda: tasks.claim_capability_selection(
        task_id=link.task_id, thread_id=link.thread_id, request_id=params.request_id, run_id=params.run_id,
        attempt_id=params.attempt_id, candidate_digest=material.candidate_digest,
        model_binding_digest=package_selection_model_digest(agent), execution_is_current=authority.is_current,
    ))
    if claim is None:
        return
    with bind_cancellation_token(params.cancellation_token):
        authority.check()
        result = select_capability_packages(agent, material, request_id=params.request_id, run_id=params.run_id,
                                            task_id=link.task_id, thread_id=link.thread_id)
        authority.atomic(lambda: _commit_selection(agent, params, scope, authority, claim, result, window))


# LLM: 入口通过原 policy 和 shared reader；finished 是选择回执而不是采用/质量保证。CAS 成功后才写 RuntimeFacts，后续正常 capture 含同一正文。
# 函数用途: 在同一活动回合内完成入口读取与一次性收口，把必要告警和已读资料交给主模型；失败原因只写回执，不给模型。
def _commit_selection(agent, params, scope, authority, claim, result, window: int) -> None:
    warnings = list(result.warning_codes)
    context = None
    if result.error_code:
        warnings.append(result.error_code)
    if result.outcome == "selected":
        budget = scope.config.capability_bundle_max_tokens
        if type(budget) is not int or budget < 0:
            warnings.append("CAPABILITY_SELECTION_ENTRY_BUDGET_INVALID")
        else:
            context = prepare_package_entry_context(
                agent, params, scope, result.selected_refs, authority=authority, claim_id=claim.claim_id,
                max_tokens=min(budget, window) if budget else window,
            )
            warnings.extend(context.warning_codes)
    authority.check()
    finished = agent.conversation_store.tasks.finish_capability_selection(
        task_id=authority.task_id, thread_id=authority.thread_id, expected_claim=claim,
        outcome=result.outcome, selected_refs=result.selected_refs, warning_codes=tuple(dict.fromkeys(warnings)),
        failure=result.failure_facts, execution_is_current=authority.is_current,
    )
    if finished is None:
        return
    authority.check()
    if context is not None and context.text:
        record_runtime_facts_turn_ir(params, context.text, source="capability_package_entries")
    if warnings:
        _selection_warning(params, tuple(dict.fromkeys(warnings)))


# LLM: 只把有限机器码作为上下文事实，不能解释为任务失败、权限或自动重试指令；原 IR 同来源去重避免反复堆积。
# 函数用途: 提醒主模型选包准备的客观限制，普通任务继续按原权限处理。
def _selection_warning(params: object, codes: tuple[str, ...]) -> None:
    record_runtime_facts_turn_ir(params, "能力包准备提示（不代表任务失败）：" + ", ".join(codes),
                                 source="capability_package_selection_warning")
