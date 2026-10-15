# LLM: 必需通知只从执行过的typed工具回执和原宿主账投影；正文、模型dict及权限错误本身都不能授予外发义务。
# 模块用途: 以安全小字段保存需要用户处理的认证拒绝，供原后台表达、来源门和冻结重投共用。
from __future__ import annotations

import re
from types import SimpleNamespace

SCHEMA = 'required-user-action.v1'
KIND = 'credential_authentication_rejected'


# LLM: 纯形状校验不等于授权；实际新发送仍须原credential freshness与owner/claim/来源门。
# 函数用途: 拒绝坏代次和伪造路径，只保留首次原认证拒绝所需的宿主标识。
def normalize_required_action(value):
    if (not isinstance(value, dict) or value.get('schema') != SCHEMA or value.get('kind') != KIND
            or not isinstance(value.get('credential_ref'), str)
            or not re.fullmatch(r'cred_[0-9a-f]{32}', value['credential_ref'])
            or type(value.get('generation')) is not int or value['generation'] < 1):
        return None
    return {key: value[key] for key in ('schema', 'kind', 'credential_ref', 'generation')}


# LLM: typed执行与handler身份由原ToolExecutor提供；不消费输出正文或模型构造对象，迟到旧代false不产生义务。
# 函数用途: 从实际web_fetch首次明确认证拒绝中提取有界通知事实，秘密和HTTP正文均不复制。
def required_action_from_result(result):
    from ..tooling.runtime_contracts import ToolResult

    if (not isinstance(result, ToolResult) or result.tool_name != 'web_fetch'
            or result.handler_executed is not True or result.ok):
        return None
    details = result.metadata.get('handler_details')
    auth = details.get('credential_authentication') if isinstance(details, dict) else None
    if (not isinstance(auth, dict) or auth.get('status') != 'rejected'
            or type(auth.get('http_status')) is not int or auth['http_status'] != 401
            or auth.get('record_updated') is not True or auth.get('user_action_required') is not True):
        return None
    return normalize_required_action({'schema': SCHEMA, 'kind': KIND,
        'credential_ref': auth.get('credential_ref'), 'generation': auth.get('generation')})


# LLM: 原archive白名单只由typed工具结果写入required_user_action；本函数只读取同片canonical结果，不接模型元数据。
# 函数用途: 从宿主生成的工具归档取通知义务，并按凭据代次去重。
def archived_required_actions(records):
    found = {}
    for row in records or ():
        if not isinstance(row, dict) or row.get('tool') != 'web_fetch' or row.get('ok') is not False:
            continue
        envelope = row.get('tool_result_envelope')
        value = normalize_required_action(envelope.get('required_user_action')) if isinstance(envelope, dict) else None
        if value is not None:
            found[(value['credential_ref'], value['generation'])] = value
    return list(found.values())


# LLM: 模型只负责表达，不取得凭据ref、代次、秘密或写账权限；用户决定后续，不自动发卡/停用/重试。
# 函数用途: 给原零工具表达入口提供可公开的失败与处理选择事实。
def required_action_public_facts():
    return {'authentication_rejected': True, 'http_status': 401, 'user_decision_required': True,
        'available_user_choices': ['refill_through_original_secure_input', 'disable_use'],
        'automatic_refill_started': False, 'automatic_disable_applied': False,
        'automatic_authentication_retry': False, 'reply_is_interim': False,
        'affected_action': 'credential_authentication', 'scheduled_monitor_disabled': False,
        'only_affected_authentication_cannot_continue': True}


# LLM: 开关来自原能力配置；关闭时保持正常后台静默与零新增模型/外发成本。
# 函数用途: 读取这项额外通知能力，不以模型参数替代用户配置。
def notice_enabled(agent):
    from ..capability.runtime_config_reload import capability_config_for_agent

    return getattr(capability_config_for_agent(agent), 'background_user_action_notice_enabled', False) is True


# LLM: 只从本owner原pending wake取受保护宿主字段；请求只提供定位，不把缓存metadata或模型attrs当权威。
# 函数用途: 读取同一任务/线程尚未交付的认证处理义务。
def required_notice(agent, request):
    raw = getattr(request, 'wake_signal', None)
    wake = raw if isinstance(raw, dict) else {}
    if not wake.get('wake_signal_id'):
        return None
    signal = agent.conversation_store.wakes.pending_one(str(wake.get('wake_signal_id') or ''))
    if signal is None or signal.thread_id != request.thread_id or signal.root_task_id != request.task_id:
        return None
    value = signal.metadata.get('required_user_action')
    if (not isinstance(value, dict) or value.get('schema') != 'wake-required-user-action.v1'
            or value.get('thread_id') != request.thread_id or value.get('task_id') != request.task_id):
        return None
    actions = [action for item in value.get('actions', ()) if (action := normalize_required_action(item)) is not None]
    return {**value, 'actions': actions} if actions else None


# LLM: 只有当前原runner可登记；先用原凭据scope/代次读门复核，再写同一wake，不从任意dict恢复执行权。
# 函数用途: 在任务终结之前保留首次认证拒绝的通知义务，避免正常结果静默吞掉需要用户处理的事项。
def freeze_notice(agent, request, actions):
    from ..user_space.task_credentials import credential_rejection_current

    if not notice_enabled(agent):
        return False
    current = [item for item in actions if credential_rejection_current(agent, item['credential_ref'], item['generation'])]
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    return bool(agent.conversation_store.wakes.freeze_required_user_action(str(wake.get('wake_signal_id') or ''),
        thread_id=request.thread_id, task_id=request.task_id, actions=current)) if current else False


# LLM: 此处仍在原agent.run/current_prompt_scope内；只延后同片已核义务的完成，不复活终态，普通前台/无通知行为不变。
# 函数用途: 在core正常关TaskLink前冻结原wake通知，让发送前仍能核验active任务与原owner/actor。
def defer_required_action_completion(agent, context):
    if context.source != 'background_main_agent':
        return False
    actions = archived_required_actions(context.archive_tool_calls)
    if not actions or not notice_enabled(agent):
        return False
    attrs = context.task_attributes if isinstance(context.task_attributes, dict) else {}
    wake_id = attrs.get('background_wake_signal_id')
    if not actions or not wake_id:
        return False
    request = SimpleNamespace(thread_id=str(attrs.get('conversation_thread_id') or ''),
        task_id=context.task_id, wake_signal={'wake_signal_id': wake_id})
    return freeze_notice(agent, request, actions)


# LLM: 每次新发送/未发送重投仍须当前原claim；RunParams只由原TaskLink/HostActor装配，不能传模型scope或恢复已结束attempt。
# 函数用途: 在原后台领取上下文复核凭据版本，晚到新代、撤销、跨owner与取消都不能新发通知。
def notice_is_current(agent, request):
    from ..agent_core.runtime_mixin import current_prompt_scope
    from ..user_space.task_credentials import credential_rejection_current
    from .runtime import _run_params

    if not notice_enabled(agent):
        return False
    notice = required_notice(agent, request)
    if notice is None:
        return False
    run = agent.scheduler_repository.get_active_run(request.task_id)
    if run is not None:
        job = agent.scheduler_repository.get_job(run['job_id'], include_deleted=True)
        if job['status'] != 'active':
            return False
    thread = agent.conversation_store.threads.require(request.thread_id)
    from ..scheduler.execution import resolve_delivery, validate_delivery_origin
    from .channels import DeliveryContext
    origin, channel, target = resolve_delivery(agent, thread, request, request.route_channel, request.route_target)
    if not validate_delivery_origin(agent, request, DeliveryContext(channel=channel, target=target, thread_id=origin.thread_id, task_id=request.task_id)):
        return False
    params = _run_params(request.thread_id, request, agent, thread=thread)
    with current_prompt_scope(agent, '', params):
        return any(credential_rejection_current(agent, item['credential_ref'], item['generation']) for item in notice['actions'])


# LLM: 原宿主工具已退出执行scope但后台claim仍在；重新绑定的参数只由原TaskLink/HostActor生成，不从工具参数猜权限。
# 函数用途: 给precheck/NO首次失败冻结通知义务，返回false时保持原失败收口。
def freeze_tool_notice(agent, request, result):
    action = required_action_from_result(result)
    return freeze_request_notice(agent, request, [action]) if action is not None else False


# LLM: actions仅来自原typed结果或已落账precheck观察；外部scope不接收，读取失败不伪造身份。
# 函数用途: 给宿主工作失败复用原后台参数复核，再冻结同一wake的通知事实。
def freeze_request_notice(agent, request, actions):
    from ..agent_core.runtime_mixin import current_prompt_scope
    from .runtime import _run_params

    thread = agent.conversation_store.threads.require(request.thread_id)
    params = _run_params(request.thread_id, request, agent, thread=thread)
    with current_prompt_scope(agent, '', params):
        return freeze_notice(agent, request, actions)


# LLM: 通知只结束本次受影响工作片，不停用/删除计划或凭据；只从active变abandoned，不复活任何终态。
# 函数用途: 在已送达、失效或表达明确失败后收回defer的TaskLink，保持真实失败状态。
def settle_notice_task(agent, request):
    store = agent.conversation_store
    task = store.tasks.load(request.task_id)
    if task is not None and task.thread_id == request.thread_id and task.status == 'active':
        store.tasks.update_status({'task_id': request.task_id, 'status': 'abandoned', 'expected_status': 'active'})


# LLM: 空表达/旧代/取消不制造sent；失败有明确终态而不永久占住active，原wake确认仍由调用方处理。
# 函数用途: 返回无外发的通知终结报告，仅保留结构原因码。
def _undelivered_report(agent, request, reason):
    from .models import BackgroundMainAgentReport

    settle_notice_task(agent, request)
    return BackgroundMainAgentReport(thread_id=request.thread_id, task_id=request.task_id,
        reason='required_user_action', response='', route_channel=request.route_channel, route_target=request.route_target, created_at=request.now, delivery_status='suppressed',
        delivery_reason=reason, wake_handled=True, task_status=agent.conversation_store.tasks.load(request.task_id).status,
        runtime_status='failed', runtime_reason='PERMISSION_BLOCKED')


# LLM: 正文只沿原模型零工具表达或本片已有公开回复；准确来源/凭据代次先核，冻结重投仍原owner_delivery，不重跑业务。
# 函数用途: 对首次拒绝向原用户投一次必要提醒，失败冻结，已过期或表达失败沿原终态收口。
def deliver_required_notice(runtime, request, execution=None):
    from dataclasses import replace

    from ..agent_core.tool_only_execution import express_required_user_action
    from ..scheduler.execution import resolve_delivery
    from .background_delivery import (
        BackgroundDeliveryDependencies,
        _background_delivery_route,
        background_delivery_idempotency_key,
        background_owner_delivery_committed,
        record_background_response,
    )
    from .channels import DeliveryContext
    from .models import BackgroundMainAgentReport
    from .runtime import _run_params

    agent, store = runtime.agent, runtime.store
    if not notice_is_current(agent, request):
        return _undelivered_report(agent, request, 'required_action_superseded')
    thread = store.threads.require(request.thread_id)
    origin, channel, target = resolve_delivery(agent, thread, request, request.route_channel, request.route_target)
    text = str(getattr(execution, 'response', '') or '')
    receipt = getattr(execution, 'public_receipt', None)
    if not text.strip() or (isinstance(receipt, dict) and receipt.get('notification_intent') == 'internal_only'):
        params = _run_params(request.thread_id, request, agent, thread=thread)
        try:
            response = express_required_user_action(agent, params, required_action_public_facts())
        except InterruptedError:
            raise
        except Exception:  # noqa: BLE001 - 表达失败不重跑业务、不伪造文字/送达，原失败可由原状态查询
            return _undelivered_report(agent, request, 'required_action_expression_failed')
        if response.runtime_status in {'cancelled', 'aborted'}:
            return _undelivered_report(agent, request, 'required_action_cancelled')
        text = response.text
    context = DeliveryContext(channel=channel, target=target, thread_id=origin.thread_id,
        task_id=request.task_id, mode='proactive', idempotency_key=background_delivery_idempotency_key(request))
    commit = record_background_response(BackgroundDeliveryDependencies(agent, store, runtime.channels,
        lambda _request: store.tasks.load(request.task_id).status), request, context, text,
        public_receipt={'schema': 'background-result.v1', 'notification_intent': 'publish'},
        deliver=True, delivery_reason='required_user_action')
    route = _background_delivery_route(runtime.channels, context)
    handled = background_owner_delivery_committed(request, target=target,
        ownership=route.ownership, canonical_record=route.canonical_record, commit=commit)
    if handled:
        settle_notice_task(agent, request)
    result = BackgroundMainAgentReport(thread_id=request.thread_id, task_id=request.task_id,
        reason='required_user_action', response=commit.content, route_channel=channel, route_target=target,
        created_at=request.now, delivery_status=commit.delivery_status, delivery_reason='required_user_action',
        wake_handled=handled, task_status=store.tasks.load(request.task_id).status,
        runtime_status='failed', runtime_reason='PERMISSION_BLOCKED')
    return replace(result, tool_call_count=getattr(execution, 'tool_call_count', 0))
