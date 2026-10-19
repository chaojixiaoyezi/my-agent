# LLM: 委派投递只读原线程/SessionTask/当前runner与attempt，定位不是授权；不新建账或从正文判断发话角色。
# 模块用途: 共用委派原来源核验，让后台与孩子只向canonical主会话交结果。
from __future__ import annotations

from ..runtime_context import current_subagent_attempt_id, current_subagent_run_id
from ..runtime_errors import DataCorruptionError
from .execution_threads import (
    EXECUTION_ORIGIN_KEY,
    IM_TASK_EXECUTION_KEY,
    delegated_session_task,
    execution_origin_thread,
)


# LLM: 精确原ThreadStore中的委派策略、SessionTask与来源必须同owner；返回值只收紧交付，不签发执行权限。
# 函数用途: 识别原宿主委派工作；给定task只按原canonical scheduler run排除正常调度，坏链不退普通。
def delegated_delivery_task(agent, thread_id, *, task_id=""):
    if not thread_id:
        return None
    if task_id:
        from types import SimpleNamespace

        from ..scheduler.execution import execution_run

        if execution_run(agent, SimpleNamespace(task_id=task_id, thread_id=thread_id)) is not None:
            return None
    store = agent.conversation_store
    thread = store.threads.require(thread_id)
    if IM_TASK_EXECUTION_KEY not in thread.metadata:
        return None
    policy = thread.metadata[IM_TASK_EXECUTION_KEY]
    if (
        not isinstance(policy, dict)
        or policy.get("mode") != "delegated"
        or thread.metadata.get(EXECUTION_ORIGIN_KEY, {}).get("kind") != "delegated"
    ):
        raise DataCorruptionError("委派交付策略不可用")
    task = delegated_session_task(store, thread_id)
    if task is None:
        raise DataCorruptionError("委派交付任务不可用")
    if thread.owner_id != str(agent.home_paths.owner_id) or thread.owner_home != str(
        agent.home_paths.owner_home_dir
    ):
        raise DataCorruptionError("委派交付owner不一致")
    return task


# LLM: 当前runner必须对应原current attempt与精确root TaskLink；先核原claim/BG是否存在，缺params不能将原后台降普通，不接受模型attrs自报策略。
# 函数用途: 先核孩子/原claim/BG运输，再区分typed普通本地域；会话域缺attrs仍查canonical，禁止后台替主会话发话。
def current_delegated_delivery_task(agent):
    child_id = current_subagent_run_id(agent)
    if child_id:
        child = agent.subagents.load(child_id)
        if child is None:
            raise DataCorruptionError("当前孩子不可读")
        root = agent.conversation_store.tasks.load(child.root_id)
        if root is None:
            raise DataCorruptionError("当前孩子根任务不可用")
        task = delegated_delivery_task(agent, root.thread_id, task_id=child.root_id)
        if task is not None and (
            not current_subagent_attempt_id(agent)
            or child.runner_active_attempt_id != current_subagent_attempt_id(agent)
            or (child.attributes or {}).get("conversation_request_id") != task.task_id
        ):
            raise DataCorruptionError("当前委派孩子执行轮不一致")
        return task
    params = getattr(agent, "_current_run_params", None)
    from ..gateway_parts.multiprocess_background_execution import current_background_request
    from .claim_execution_binding import current_claim_execution
    from .tool_approval_scope import tool_approval_scope, tool_approval_scope_is_current

    claim = current_claim_execution()
    background = current_background_request()
    if params is None:
        if claim is not None or background is not None:
            raise DataCorruptionError("当前后台执行缺少原运行参数")
        return None
    if claim is not None:
        if claim.claims is not agent.conversation_store.claims:
            raise DataCorruptionError("当前委派claim对象不一致")
        row = claim.claims.load(claim.thread_id, claim_scope_id=claim.claim_scope_id)
        task = delegated_delivery_task(
            agent,
            claim.thread_id,
            task_id=row.get("task_id") or (row.get("runtime_authority") or {}).get("task_id", ""),
        )
        if task is not None:
            import time

            authority = row.get("runtime_authority") or {}
            policy = agent.conversation_store.threads.require(claim.thread_id).metadata[
                IM_TASK_EXECUTION_KEY
            ]
            if (
                row.get("claim_id") != claim.claim_id
                or row.get("status") != "running"
                or row.get("thread_id") != claim.thread_id
                or float(row.get("expires_at") or 0) <= time.time()
                or any(
                    authority.get(key) != getattr(params, key, "")
                    for key in ("task_id", "run_id", "attempt_id")
                )
                or policy.get("root_task_id") != authority.get("task_id")
                or (params.task_attributes or {}).get("conversation_thread_id") != claim.thread_id
            ):
                raise DataCorruptionError("当前委派后台执行来源不可用")
    else:
        from ..agent_core.runtime.loop_models import RunParams

        if background is not None:
            raise DataCorruptionError("当前后台运输缺少原claim")
        # 仅原typed本地非会话域可以没有Conversation TaskLink；这不是读取失败的fallback。
        if (isinstance(params, RunParams) and params.context_scope in {"default", "isolated"}
                and isinstance(params.task_attributes or {}, dict)
                and not (params.task_attributes or {}).get("conversation_thread_id")
                and params.host_channel_actor is None):
            return None
        scope = tool_approval_scope(agent, params.run_id)
        task = delegated_delivery_task(agent, scope.thread_id, task_id=scope.root_task_id)
        if task is not None:
            attrs = params.task_attributes or {}
            policy = agent.conversation_store.threads.require(scope.thread_id).metadata[
                IM_TASK_EXECUTION_KEY
            ]
            if (
                not scope.active
                or not tool_approval_scope_is_current(agent, scope)
                or attrs.get("conversation_thread_id") != scope.thread_id
                or policy.get("root_task_id") != scope.root_task_id
            ):
                raise DataCorruptionError("当前委派主执行来源不可用")

    return task


# LLM: 目标仅取原execution origin冻结binding并重核原source；不允许模型指定接收人，不读取凭据。
# 函数用途: 给末发送门比较原委派任务的IM通知地址，正常其它业务目标仍守原领域权限。
def delegated_origin_target(agent, task):
    from .channels import binding_delivery_target
    from .execution_threads import EXECUTION_ORIGIN_KEY

    thread = agent.conversation_store.threads.require(task.target_thread_id)
    execution_origin_thread(agent.conversation_store.threads.require, thread)
    binding = thread.metadata[EXECUTION_ORIGIN_KEY]["binding"]
    return binding["channel"], binding_delivery_target(
        binding["channel"],
        binding["actor_id"],
        binding["conversation_id"],
        group=agent.home_paths.owner_kind == "group",
    )
