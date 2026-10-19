# LLM: 派工沿原ToolExecutor/G1及父工具上界；原图片显式传media_refs并核开关，OPEN仍交原父裁决，不能自动批准。
# 模块用途: 为IM启动准确工具范围并获授权原图片的孩子，恢复引用，再按真实结果或申请处理。
from __future__ import annotations

import json
from dataclasses import replace

from ..runtime_context import current_subagent_run_id
from ..runtime_errors import DataCorruptionError
from ..tooling.runtime_contracts import ToolCall
from .execution_threads import (
    IM_TASK_EXECUTION_KEY,
    delegated_session_task,
    execution_origin_thread,
)


# LLM: 接单事实来自宿主耐久SessionTask；无工具只是本片表达角色，不证明孩子缺能力或执行失败，正文不是完成依据。
# 函数用途: 为已排队任务登记模型短回执及尚未观察结果的事实，避免主聊天重复执行或误报能力失败。
def prepare_dispatch_ack(params):
    from ..agent_core.tool_loop.natural_user_reply import (
        pending_natural_user_reply,
        queue_natural_user_reply,
    )

    ack = (params.task_attributes or {}).get("host_im_task_ack")
    if ack and not pending_natural_user_reply(params):
        queue_natural_user_reply(params, kind="background_dispatch", facts={
            **ack, "current_user_request": params.user_prompt, "reply_is_interim": True,
            "task_continues_without_more_user_input": ack["status"] not in {"done", "failed", "cancelled"},
            "current_turn_role": "dispatch_acknowledgement",
            "worker_execution_outcome": "not_observed_in_this_turn",
            "coordinator_role": "本片仅确认宿主派工，不评估孩子的业务能力；无工具表达轮不能证明孩子无法执行。",
        })


# LLM: 只读原线程策略和原派活记录；不接受模型 attributes 自报委派，不把子代理线程或一般后台当成委派任务。
# 函数用途: 找到本片应执行的委派任务与不可变策略，坏归属明确报错。
def _execution_policy(agent, params):
    if current_subagent_run_id(agent):
        return None
    attrs = params.task_attributes or {}
    thread_id = str(attrs.get("conversation_thread_id") or "")
    if not thread_id or not getattr(agent, "conversation_store", None):
        return None
    thread = agent.conversation_store.threads.require(thread_id)
    policy = thread.metadata.get(IM_TASK_EXECUTION_KEY)
    if policy is None:
        return None
    source = execution_origin_thread(agent.conversation_store.threads.require, thread)
    task = delegated_session_task(agent.conversation_store, thread_id)
    if (not isinstance(policy, dict) or policy.get("mode") != "delegated" or task is None
            or task.target_thread_id != thread_id or task.sender_thread_id != source.thread_id
            or thread.owner_id != str(agent.home_paths.owner_id)):
        raise DataCorruptionError("自动委派执行身份不一致")
    return thread, policy, task


# LLM: 委派主代理沿原晋升绑定耐久根；先验证所选包的完整引用再写原 task pins，不从更新后的目录替换旧版本。
# 函数用途: 建立真实父任务并固定主子所用资源版本，停用或换代交给原不可用回执处理。
def _prepare_root(agent, params, policy):
    from ..capability.task_references import normalize_skill_reference
    from .task_promotion import promote_conversation_task_for_run

    root = promote_conversation_task_for_run(agent, params)
    if root is None:
        raise DataCorruptionError("委派根任务无法绑定")
    # LLM: 只保存对原 TaskStore 根的引用，不能换根；取消与生命周期沿此精确原身份。
    # 函数用途: 固定后台会话对应的持久父任务供精确停止使用。
    def bind_root(current):
        fixed = dict(current.metadata[IM_TASK_EXECUTION_KEY])
        if fixed.get("root_task_id") not in {None, root.task_id}:
            raise DataCorruptionError("委派根任务冲突")
        return replace(current, metadata={**current.metadata, IM_TASK_EXECUTION_KEY: {**fixed, "root_task_id": root.task_id}})

    agent.conversation_store.threads.update_atomic(root.thread_id, bind_root)
    snapshot = agent.current_skill_snapshot()
    allowed = []
    for value in policy["package_refs"]:
        ref = normalize_skill_reference(value)
        package = snapshot.resolve_package(ref["package_id"])
        if package is None or package.to_ref() != ref:
            raise DataCorruptionError("委派所选能力版本已不可用")
        agent.conversation_store.tasks.pin_skill_reference(
            task_id=root.task_id, thread_id=root.thread_id, reference=ref,
            execution_authority_check=params.cancellation_token.raise_if_cancelled,
        )
        allowed.append(ref["stable_id"])
    return allowed


# LLM: 宿主创建仍走原冻结授权及operation账；只有活跃或BLOCKED孩子的OPEN申请可交直属父裁决，GAP/其它终态不复活。
# 函数用途: 原主sender先表达精确结果回报；独立委派线程创建/恢复孩子并沿原能力等待和终态汇总。
def prepare_delegated_execution(agent, params):
    if _prepare_sender_result_reply(agent, params):
        return
    resolved = _execution_policy(agent, params)
    if resolved is None:
        return
    thread, policy, task = resolved
    from ..agent_core.runtime.guidance import inject_pending_guidance
    from ..agent_core.tool_ir_history import record_runtime_facts_turn_ir

    inject_pending_guidance(agent, params)

    child_ids = policy.get("child_run_ids")
    if child_ids is None:
        allowed = _prepare_root(agent, params, policy)
        child_ids = list(agent.subagent_run_ids_for_request(task.task_id))
        if not child_ids:
            child_ids = _start_child(agent, params, task, allowed)
        _bind_children(agent, thread.thread_id, child_ids)
    children = [agent.subagents.load(run_id) for run_id in child_ids]
    if not children or any(child is None for child in children):
        raise DataCorruptionError("委派孩子记录不可读")
    for child in children:
        attrs = child.attributes or {}
        if attrs.get("conversation_request_id") != task.task_id:
            raise DataCorruptionError("委派孩子不属于当前任务")
    agent.conversation_store.session_tasks.bind_turn(task.task_id, turn_id=task.task_id)
    if "create_subagents" not in params.executed_tools:
        params.executed_tools.append("create_subagents")
    record_runtime_facts_turn_ir(params, json.dumps({
        "execution_mode": "delegated", "session_task_id": task.task_id,
        "children": [{"run_id": child.id, "status": child.status} for child in children],
        "coordinator_role": "依据孩子的真实结果汇总；工作正文由孩子执行。活跃孩子未结束时阶段回复并等生命周期事件，不轮询。",
    }, ensure_ascii=False), source="im_task_delegation")
    from ..agent_core.orchestration.child_result_index import child_result_index
    from ..agent_core.tool_loop.natural_user_reply import queue_natural_user_reply
    from ..subagents.models import SUBAGENT_ENDED_STATUSES, TaskStatus

    active = any(child.status not in SUBAGENT_ENDED_STATUSES for child in children)
    failed = not active and any(child.status != TaskStatus.DONE.value for child in children)
    resolution_children = [child for child in children
        if child.status not in SUBAGENT_ENDED_STATUSES or child.status == TaskStatus.BLOCKED.value]
    resolution = _prepare_capability_resolution(params, resolution_children)
    if resolution is not None:
        return resolution
    queue_natural_user_reply(params, kind="subagents_active" if active else "delegated_failure" if failed else "delegated_result", facts={
        "current_user_request": task.goal, "session_task_id": task.task_id,
        "reply_is_interim": active, "task_continues_without_more_user_input": active,
        "children": [{"run_id": child.id, "status": child.status,
                      **({"result": child.result, "failure_type": child.failure_type} if not active else {})} for child in children],
        "result_refs": child_result_index(agent, children) if not active else [],
    })


# LLM: 只接受原sender本轮session_message键对应的canonical结果；复制后台attrs/actor不签权，当前主session用零工具表达原事实，消费仍沿原guidance ACK。
# 函数用途: 在原主会话模型轮汇总本次实际注入的真实委派结果，不创建汇总会话或转发后台正文。
def _prepare_sender_result_reply(agent, params):
    if current_subagent_run_id(agent):
        return False
    attrs = params.task_attributes or {}
    key = attrs.get("host_session_message_key")
    if not key:
        return False
    store = agent.conversation_store
    receipt = store.guidance.receipt(key)
    if receipt is None:
        return False
    metadata = receipt.entry.metadata
    task = store.session_tasks.load(metadata.get("session_task_id", ""))
    if task is None or task.status not in {"done", "failed"}:
        return False
    from .im_delegation_delivery import delegated_delivery_task
    from .session_task_report import result_report_key

    canonical = delegated_delivery_task(agent, task.target_thread_id)
    if canonical is None:
        return False
    if (canonical != task or task.sender_thread_id != attrs.get("conversation_thread_id")
            or receipt.entry.target_id != task.sender_thread_id
            or key != result_report_key(task)):
        raise DataCorruptionError("委派结果与原主会话不一致")
    from ..agent_core.runtime.guidance import inject_pending_guidance
    from ..agent_core.tool_loop.natural_user_reply import queue_natural_user_reply

    inject_pending_guidance(agent, params)
    results = _injected_sender_results(agent, params)
    queue_natural_user_reply(params, kind="delegated_result", facts={
        "delegated_results": results,
        "reply_is_interim": False, "task_continues_without_more_user_input": False,
    })
    return True


# LLM: 仅读取本轮原reserve实际注入的条目；每个结果重核原SessionTask/key/sender，不把primary代表其它已注入结果。
# 函数用途: 给主会话模型提供本轮全部真实委派结果，原ACK仍逐guidance确认，不新增批次或存储。
def _injected_sender_results(agent, params):
    from .im_delegation_delivery import delegated_delivery_task
    from .session_task_report import result_report_key

    entries = (params.live_archive_state or {}).get("_guidance_ack_entries", {})
    results = []
    for entry in entries.values():
        task_id = entry.metadata.get("session_task_id", "")
        task = agent.conversation_store.session_tasks.load(task_id) if task_id else None
        if task is None or task.status not in {"done", "failed"}:
            continue
        canonical = delegated_delivery_task(agent, task.target_thread_id)
        if canonical is None:
            continue
        receipt = agent.conversation_store.guidance.receipt(result_report_key(task))
        if (canonical != task or receipt is None or receipt.entry != entry
                or entry.target_id != task.sender_thread_id
                or task.sender_thread_id != params.task_attributes.get("conversation_thread_id")):
            raise DataCorruptionError("已注入委派结果与原主会话不一致")
        results.append({"current_user_request": task.goal, "session_task_id": task.task_id,
            "status": task.status, "result": task.summary, "result_refs": list(task.result_refs),
            "failure_code": task.failure_code})
    if not results:
        raise DataCorruptionError("本轮没有实际注入的委派结果")
    return results


# LLM: OPEN 仅从 canonical 直属孩子读取；保持原父级 resolve 工具的授权上界，不自动批准，不暴露业务工具或模型自报申请。
# 函数用途: 把缺能力的孩子交给后台主代理裁决，避免纯汇总视图让真实申请永远无人处理。
def _prepare_capability_resolution(params, children):
    from ..agent_core.tool_ir_history import record_runtime_facts_turn_ir
    from ..subagents.capability_scope import request_scope_snapshot
    from ..subagents.model_capabilities import capability_request_counts_as_open

    requests = [{"run_id": child.id, "request_id": request.id, "problem": request.problem,
                 "request": request_scope_snapshot(request)}
                for child in children for request in child.capability_requests
                if capability_request_counts_as_open(request.status)]
    if not requests:
        return None
    record_runtime_facts_turn_ir(params, json.dumps({
        "coordinator_role": "只裁决直属孩子的结构化能力申请；不要替孩子执行原工作。按自身当前权限 grant/deny，原工具继续检查授权。",
        "capability_requests": requests,
    }, ensure_ascii=False), source="im_task_capability_resolution")
    return replace(params, allowed_tools=["resolve_capability_requests"])


# LLM: 自动派工显式传父工具上界与原输入媒体sha；原本人send_message留主sender通知，其它获准业务工具/图片沿原G1，不借attrs扩大权限。
# 函数用途: 沿原执行账创建同owner业务孩子并转交原附件，不改手工默认或自动批准。
def _start_child(agent, params, task, allowed):
    from ..agent_core.orchestration.create_policy import leaf_allowed_tools
    from ..agent_core.tool_loop.recovery import runtime_run_scope
    from ..agent_core.tool_runtime_ledger import write_boundary_with_runtime_ledger
    from ..subagents.role_templates import active_model_subagent_tools
    from .background_execution import BackgroundCapacityDeferred

    runtime = params.tool_runtime_snapshot.runtime("create_subagents")
    if runtime is None:
        raise DataCorruptionError("当前用户没有子代理创建权限")
    from .host_message_context import media_for_run, require_subagent_media

    current = agent._current_run_params
    media = media_for_run(agent, current.host_channel_actor) if current.host_channel_actor is not None else ()
    require_subagent_media(agent, media)
    arguments = {"goal": task.goal, "allowed_skills": allowed,
        "allowed_tools": leaf_allowed_tools(active_model_subagent_tools(sorted(params.tool_runtime_snapshot.available_tool_names)))}
    if media:
        arguments['input_media_refs'] = [ref['sha256'] for ref in media]
        params = replace(params, task_attributes={**(params.task_attributes or {}), 'input_media': list(media)})
    call = ToolCall(
        call_id=f"host-delegate:{task.task_id}", tool_name="create_subagents",
        arguments=arguments,
        source_protocol=params.tool_protocol_snapshot.source_protocol, schema_hash=runtime.model_spec.schema_hash,
        run_id=params.run_id, turn_id=f"{params.run_id}:host-delegate", attempt_id=params.attempt_id,
    )
    execution = agent.tools.execute_tool(
        call, write_boundary=write_boundary_with_runtime_ledger(agent, params),
        runtime_snapshot=params.tool_runtime_snapshot, cancellation_token=params.cancellation_token,
        trusted_run_context={"interactive": False, "task_attributes": dict(params.task_attributes or {}),
                             "run_scope": runtime_run_scope(agent, params).to_dict(), "call_origin": "host_command"},
    )
    result = execution.result
    if result.error_code == "SUBAGENT_CAPACITY_EXCEEDED":
        raise BackgroundCapacityDeferred("im_subagent_capacity")
    if not result.ok:
        raise DataCorruptionError(f"委派未确认启动：{result.error_code}")
    payload = json.loads(result.output)
    ids = payload.get("created_run_ids", []) + payload.get("reused_run_ids", [])
    if not ids or any(not isinstance(value, str) or not value for value in ids):
        raise DataCorruptionError("委派创建回执缺少孩子引用")
    return list(dict.fromkeys(ids))


# LLM: 仅保存对原 SubagentStore 的准确引用；不复制状态或产物，恢复已有引用不能替换孩子。
# 函数用途: 原线程事务内一次绑定已创建的孩子，后续片不再次派工。
def _bind_children(agent, thread_id, child_ids):
    # LLM: 原策略和版本必须保持；副作用仅更新孩子 refs，冲突表明恢复身份坏了。
    # 函数用途: 以原线程锁固定此次派工结果。
    def bind(current):
        policy = dict(current.metadata[IM_TASK_EXECUTION_KEY])
        if policy.get("child_run_ids") not in (None, child_ids):
            raise DataCorruptionError("委派孩子绑定冲突")
        policy["child_run_ids"] = child_ids
        return replace(current, metadata={**current.metadata, IM_TASK_EXECUTION_KEY: policy})

    agent.conversation_store.threads.update_atomic(thread_id, bind)
