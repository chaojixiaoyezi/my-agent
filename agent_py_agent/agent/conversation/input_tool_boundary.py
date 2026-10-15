# LLM: 消费真实宿主工具信封，不从文字或工具名猜等待；归档/native pair 在调用前完成，恢复沿原 input dependency/Wake。
# 模块用途: 在完整工具记录边界登记等待，携带原归档的安全执行事实生成零工具回执，再让出执行等原输入事件。
from __future__ import annotations

import json
from dataclasses import replace

from ..common.json_io import read_json_object_report
from .input_dependencies import (
    arm_input_dependency,
    input_dependency_current,
    input_dependency_path,
)


# LLM: 只消费executor真实input_dependency；取消可能早于工具归档，带resolution_delivery=retire的卡取消不得重新armed/发Wake或进入等待，泛用取消不变。
# 函数用途: 将已归档工具的结构化输入依赖置为可恢复；完整本轮其它工具仍按原配对记录。
def record_input_wait(agent, params, result):
    envelope = (getattr(result, 'metadata', {}) or {}).get('handler_details', {})
    dependency = envelope.get('input_dependency') if isinstance(envelope, dict) else None
    if result.ok and isinstance(dependency, dict) and dependency.get('status') == 'waiting_input':
        previous = read_json_object_report(input_dependency_path(agent, dependency['input_id']), context='task_input.replay').payload
        if (previous.get('status')=='cancelled' and previous.get('resolution_delivery')=='retire') or (previous.get('armed') and previous.get('status') != 'pending'):
            params.tool_context.append('[task-input-resolution]\n' + json.dumps({'input_id': previous['input_id'], 'status': previous['status'], 'result_refs': previous.get('result_refs', [])}))
            return
        row = arm_input_dependency(agent, dependency['input_id'])
        if row.get('status')=='cancelled' and row.get('resolution_delivery')=='retire':
            params.tool_context.append('[task-input-resolution]\n' + json.dumps({'input_id':row['input_id'],'status':row['status'],'result_refs':row.get('result_refs',[])}))
            return
        state = params.live_archive_state
        state['waiting_input_id'] = row['input_id']


# LLM: 输入行仅取白名单，主轮执行仅复用公开归档核验；pending不证明投递，不能带秘密、整包历史、重跑申请或合成回执。
# 函数用途: 为已armed的准确输入准备一次自然等待回执；已结算输入不重新等待，不创建第二个输入或调度器。
def prepare_input_wait_reply(agent, params):
    state = getattr(params, 'live_archive_state', None) or {}
    input_id = state.get('waiting_input_id')
    if not input_id:
        return False
    report = read_json_object_report(input_dependency_path(agent, input_id), context='task_input.reply')
    row = report.payload
    if report.load_error or not row or row.get('input_id') != input_id:
        raise ValueError('原输入等待记录不可读')
    state.pop('waiting_input_id', None)
    if row.get('status') != 'pending':
        params.tool_context.append('[task-input-resolution]\n' + json.dumps({
            'input_id': row['input_id'], 'status': row['status'], 'result_refs': row.get('result_refs', []),
        }))
        return False
    if not row.get('armed') or not input_dependency_current(agent, row):
        raise ValueError('原输入等待范围不可用')
    from ..agent_core.tool_loop.completion import turn_execution_reply_facts
    from ..agent_core.tool_loop.natural_user_reply import queue_natural_user_reply

    queue_natural_user_reply(params, kind='input_wait', facts={
        **turn_execution_reply_facts(agent, params),
        'input_dependency': {key: row[key] for key in ('input_id', 'provider', 'status', 'expires_at')},
        'reply_is_interim': True, 'user_input_required': True,
        'task_continues_without_more_user_input': False, 'input_request_registered': True,
        'current_user_request': str(params.user_prompt or '')[:4000],
    })
    return True


# LLM: 仅沿当前表达冻结的输入和原Task refs复读，不轮询、不读取秘密、不补造Wake；旧草稿不能覆盖已发生的回调。
# 函数用途: 在表达收尾前核对必要输入，区分仍需填写、原Wake待消费、单卡退休和有证据的后继接管。
def reconcile_input_wait_reply(agent, params, response):
    from ..agent_core.tool_loop.natural_user_reply import (
        discard_pending_natural_user_reply,
        pending_natural_user_reply,
    )

    phase = pending_natural_user_reply(params)
    if not phase or phase.get('kind') not in {'input_wait', 'input_resume_pending'}:
        return 'normal', response
    input_id = phase['facts']['input_dependency']['input_id']
    row = _input_reply_row(agent, input_id)
    if row['status'] == 'pending':
        if not row.get('armed') or not input_dependency_current(agent, row):
            raise ValueError('原输入等待范围不可用')
        return 'normal', response
    if not input_dependency_current(agent, row):
        raise ValueError('原输入结算范围不可用')
    pending, thread_id = _remaining_input_reply_row(agent, row)
    if pending is not None:
        return _refresh_input_reply_phase(phase, pending, 'input_wait'), response
    if row.get('resolution_delivery') == 'retire':
        status, reason = 'cancelled', 'TASK_INPUT_CANCELLED'
    else:
        receipt = agent.conversation_store.wakes.delivery_receipt(thread_id, 'task-input:' + input_id)
        if receipt == 'pending':
            return _refresh_input_reply_phase(phase, row, 'input_resume_pending'), response
        status, reason = _consumed_input_reply_status(agent, params, row, receipt)
    discard_pending_natural_user_reply(params)
    return 'finish', replace(response, text='', tool_use_blocks=[], runtime_status=status,
        runtime_reason=reason, runtime_source='input_dependency',
        turn_end_reason='aborted' if status == 'cancelled' else 'error')


# LLM: 输入ID只能定位原owner的canonical行；缺失、损坏、错误绑定必须显式失败。
# 函数用途: 为有界表达复核读取一条准确输入记录，不投影任意metadata。
def _input_reply_row(agent, input_id):
    report = read_json_object_report(input_dependency_path(agent, input_id), context='task_input.reply_finish')
    if report.load_error or not report.payload or report.payload.get('input_id') != input_id:
        raise ValueError('原输入等待记录不可读')
    return report.payload


# LLM: 任务refs是有界查找索引，是否pending仍读每条原行；坏refs不能误当全部输入已结束。
# 函数用途: 找到同规范任务仍待填写的输入，单卡取消不得结束其它输入或任务。
def _remaining_input_reply_row(agent, row):
    task_id = row['scope']['root_task_id']
    link, error = agent.conversation_store.tasks.load_report(task_id)
    if error or link is None:
        raise ValueError('原输入任务不可读')
    for input_id in link.input_dependency_refs:
        candidate = _input_reply_row(agent, input_id)
        if candidate['scope']['root_task_id'] != task_id or candidate['owner'] != row['owner']:
            raise ValueError('原输入任务绑定冲突')
        if row['scope']['agent_kind'] == 'subagent' and candidate['scope']['run_id'] != row['scope']['run_id']:
            continue
        if candidate['status'] == 'pending' and candidate.get('armed') and input_dependency_current(agent, candidate):
            return candidate, link.thread_id
    return None, link.thread_id


# LLM: phase只是原表达的易失事实；变更时由原有界重写生成新正文，不执行工具或重置次数。
# 函数用途: 用真实输入状态替换过期草稿的事实，保持待用户与待原事件两种等待互不冒充。
def _refresh_input_reply_phase(phase, row, kind):
    dependency = {key: row[key] for key in ('input_id', 'provider', 'status', 'expires_at')}
    changed = phase['kind'] != kind or phase['facts']['input_dependency'] != dependency
    phase['kind'] = kind
    phase['facts'].update(input_dependency=dependency, user_input_required=kind == 'input_wait',
        task_continues_without_more_user_input=kind == 'input_resume_pending',
        resume_wake_registered=kind == 'input_resume_pending')
    return 'changed' if changed else 'normal'


# LLM: handled仅证明信号已消费，不等于业务成功；必须由同owner RuntimeDB证明后继attempt，旧片沿CAS失效不覆盖新片。
# 函数用途: 已消费且有后继时退休过期表达，缺失/坏回执或无后继证据则明确报原Wake不可用。
def _consumed_input_reply_status(agent, params, row, receipt):
    if receipt == 'handled':
        db = agent.subagents.runtime_db
        scope = row['scope']
        caller = db.get_attempt(params.attempt_id)
        current = db.get_agent_run(caller['agent_run_id']) if caller else None
        frozen = db.get_attempt(scope.get('execution_attempt_id', '')) if scope['agent_kind'] == 'subagent' else None
        run = (db.main_agent_run_for_task(scope['root_task_id']) if scope['agent_kind'] == 'main'
               else db.get_agent_run(frozen['agent_run_id']) if frozen else None)
        attempt = db.current_attempt(run['agent_run_id']) if run else None
        if (run and current and run['agent_run_id'] == current['agent_run_id']
                and current['run_id'] == params.run_id
                and attempt and attempt['attempt_id'] != params.attempt_id):
            return 'cancelled', 'TASK_INPUT_SUPERSEDED'
    return 'failed', 'TASK_INPUT_WAKE_UNAVAILABLE'


# LLM: 只处理原provider typed失败，不能吞预算拒绝、中断或编程异常；没有正文时仍让Gateway明确报回复失败。
# 函数用途: 表达请求失败不销毁已登记输入；复核回调竞争后仅结束本次表达，不重发卡或绕过调用预算。
def input_wait_expression_failure(agent, params, exc):
    from ..agent_core.tool_loop.natural_user_reply import (
        finish_natural_user_reply,
        pending_natural_user_reply,
    )
    from ..backends import ModelResponse
    from ..backends.errors import ProviderConfigurationError, ProviderRecoverableError

    phase = pending_natural_user_reply(params)
    if (not phase or phase.get('kind') not in {'input_wait', 'input_resume_pending'}
            or not isinstance(exc, (ProviderConfigurationError, ProviderRecoverableError))):
        return None
    response = ModelResponse(text='', backend=str(getattr(agent.backend, 'name', '')))
    verdict, response = reconcile_input_wait_reply(agent, params, response)
    if verdict == 'finish':
        return response
    return finish_natural_user_reply(params, response, accepted=False, rejection_reason='provider_expression_failure')


# LLM: 归档仅携带准确引用和类型字段；私人表单、方案、密文、令牌及任意handler扩展不能越过此投影。
# 函数用途: 给原工具归档保留等待和凭据生命周期的最小安全事实。
def input_archive_facts(envelope):
    facts = {}
    for key, fields in (('input_dependency', ('input_id', 'status', 'expires_at')),
                        ('credential', ('credential_ref', 'status', 'origin', 'expires_at'))):
        value = envelope.get(key)
        if not isinstance(value, dict):
            continue
        projected = {field: value[field] for field in fields if field in value and isinstance(value[field], (str, int, float))}
        if projected:
            facts[key] = projected
    return facts
