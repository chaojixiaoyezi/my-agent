# LLM: NO业务经原权限/车道与历史；仅workflows/scripts的结构化断点可升级原修复轮，typed认证通知边界不变。
# 模块用途: 执行监控工具并保存JSON对象结果，让断点触发依赖修复或按非空run提示词进入原模型续跑。
from __future__ import annotations

import json
import time


# LLM: 原租约/权限不变；断点先入账，run.prompt非空才借原notify修复；否则仅完成监控观察供依赖消费，不改变工具失败事实。
# 函数用途: 运行NO工具并保存对象结果，结构化断点按任务冻结参数升级，普通失败保持零模型与原收口。
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
    try:
        parsed = json.loads(output)
    except (ValueError, TypeError):
        parsed = None
    parsed = parsed if isinstance(parsed, dict) else None
    needs_repair = (action['name'] in ('workflows', 'scripts') and isinstance(parsed, dict)
        and (parsed.get('needs_repair') is True or parsed.get('status') == 'paused_for_resume'))
    confirmed = ok or needs_repair
    if confirmed:
        service.repository.record_rule_result(claim.run_id, claim.claim_id,
            parsed if isinstance(parsed, dict) else {'call_id': execution.call.call_id, 'ok': True})
    if needs_repair and notify is not None and str(claim.run.get('prompt') or '').strip():
        return notify(kwargs)
    now = time.time()
    service.conversation_store.tasks.update_status({'task_id': claim.run_id, 'status': 'completed' if confirmed else 'abandoned',
        'expected_status': 'active', 'now': now})
    commit = _commit_tool_result(agent, service, claim, kwargs, output) if ok else None
    if commit is not None and commit.outbox_frozen:
        service.park_waiting(claim,response=output,delivery_status=commit.delivery_status,delivery_reason='tool_rule_result',now=now)
        return
    service.finish(claim, status='done' if confirmed else 'failed', response=output,
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
