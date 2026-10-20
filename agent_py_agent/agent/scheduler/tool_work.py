# LLM: NO业务经原后台车道、工具与历史；只有typed首次认证拒绝可转原零工具通知，不以输出文字猜成功或告警。
# 模块用途: 执行监控的明确工具动作并记录结构化 JSON 结果，供同监控规则条件使用。
from __future__ import annotations

import json
import time


# LLM: 原租约与工具不变；只有typed认证义务才进入通知热轮，普通NO失败零新增core/model，actual未结不伪结。
# 函数用途: 运行NO工具并保存结果；准确认证拒绝交原通知表达，普通业务保持零模型与原TaskLink收口。
def execute_scheduled_tool(agent, service, claim, signal, kwargs, *, notify=None):
    from ..agent_core.runtime.loop_models import RunParams
    from ..agent_core.tool_only_execution import execute_tool_work
    from ..conversation.background_execution import _BackgroundRunControl
    from ..conversation.runtime import _background_task_attributes, _run_request

    thread = agent.conversation_store.threads.require(claim.run['thread_id'])
    params = RunParams(request_id=claim.run_id, run_id=claim.run_id, task_id=claim.run_id,
        source='scheduler_tool', root_user_prompt=claim.run['name'],
        conversation_task_binding_callback=_BackgroundRunControl(claim.run_id, None),
        task_attributes=_background_task_attributes(thread.thread_id, _run_request(kwargs), agent, thread=thread),
        write_boundary={'task_id': claim.run_id})
    action = claim.run['tool']
    execution, output = execute_tool_work(agent, params, tool_name=action['name'], arguments=action['arguments'])
    ok = execution.result.ok
    if not ok and notify is not None:
        from ..conversation.required_user_action import (
            freeze_tool_notice,
            required_action_from_result,
        )
        if required_action_from_result(execution.result) is not None:
            from ..conversation.host_notification_scope import notification_scope
            request = _run_request(kwargs)
            with notification_scope(agent, request):
                if freeze_tool_notice(agent, request, execution.result):
                    return notify(kwargs)
    if ok:
        try:
            value = json.loads(output)
        except (ValueError, TypeError):
            value = {'call_id': execution.call.call_id, 'ok': True}
        service.repository.record_rule_result(claim.run_id, claim.claim_id, value)
    now = time.time()
    service.conversation_store.tasks.update_status({'task_id': claim.run_id, 'status': 'completed' if ok else 'abandoned',
        'expected_status': 'active', 'now': now})
    commit = _commit_tool_result(agent, service, claim, kwargs, output) if ok else None
    if commit is not None and commit.outbox_frozen:
        service.park_waiting(claim,response=output,delivery_status=commit.delivery_status,delivery_reason='tool_rule_result',now=now)
        return
    service.finish(claim, status='done' if ok else 'failed', response=output,
        delivery_status=commit.delivery_status if commit is not None else 'suppressed',
        delivery_reason='tool_rule_result' if commit is not None else 'tool_only_internal_result',
        error_code=execution.result.error_code or '', now=now)
    service.conversation_store.wakes.mark_handled(signal.wake_signal_id, now=now)
    from ..conversation.task_run_closeout import settle_terminal_task_run
    settle_terminal_task_run(getattr(getattr(agent, 'subagents', None), 'runtime_db', None), service.conversation_store, task_id=claim.run_id)


# LLM: 工具结果与 LLM 结果共用原投递、来源校验、canonical 记录和冻结重投；通知失败不重跑业务工具。
# 函数用途: 保存 NO 规则结果，显式 origin 才向原布置会话外发，其它模式只记内部历史。
def _commit_tool_result(agent,service,claim,kwargs,output):
    from ..conversation.background_delivery import (
        BackgroundDeliveryDependencies,
        record_background_response,
    )
    from ..conversation.runtime import _run_request
    from ..delivery import DeliveryContext
    from .execution import resolve_delivery, scheduled_local_delivery
    request=_run_request(kwargs)
    thread=service.conversation_store.threads.require(claim.run['thread_id'])
    target_thread,channel,target=resolve_delivery(agent,thread,request,'','')
    context=DeliveryContext(channel=channel,target=target,thread_id=target_thread.thread_id,task_id=claim.run_id,
        mode='proactive',idempotency_key='scheduler-run:'+claim.run_id)
    dependencies=BackgroundDeliveryDependencies(agent,service.conversation_store,agent.delivery_service,
        lambda _request: service.conversation_store.tasks.load(claim.run_id).status)
    return record_background_response(dependencies,request,context,output,deliver=True,
        delivery_reason='tool_rule_result',local_transcript_only=scheduled_local_delivery(agent,request))
