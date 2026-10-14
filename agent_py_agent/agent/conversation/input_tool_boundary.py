# LLM: 消费真实宿主工具信封，不从文字或工具名猜等待；归档/native pair 在调用前完成，恢复沿原 input dependency/Wake。
# 模块用途: 在完整工具记录边界登记等待，并返回不轮询模型的技术结束事实。
from __future__ import annotations

import json

from ..backends import ModelResponse
from ..common.json_io import read_json_object_report
from .input_dependencies import arm_input_dependency, input_dependency_path


# LLM: 只消费 executor 的 handler_details.input_dependency；不得接受普通模型 JSON 或把未知输入 ID 当成有效依赖。
# 函数用途: 将已归档工具的结构化输入依赖置为可恢复；完整本轮其它工具仍按原配对记录。
def record_input_wait(agent, params, result):
    envelope = (getattr(result, 'metadata', {}) or {}).get('handler_details', {})
    dependency = envelope.get('input_dependency') if isinstance(envelope, dict) else None
    if result.ok and isinstance(dependency, dict) and dependency.get('status') == 'waiting_input':
        previous = read_json_object_report(input_dependency_path(agent, dependency['input_id']), context='task_input.replay').payload
        if previous.get('armed') and previous.get('status') != 'pending':
            params.tool_context.append('[task-input-resolution]\n' + json.dumps({'input_id': previous['input_id'], 'status': previous['status'], 'result_refs': previous.get('result_refs', [])}))
            return
        row = arm_input_dependency(agent, dependency['input_id'])
        state = params.live_archive_state
        state['waiting_input_id'] = row['input_id']


# LLM: 模型原响应未提供 final；宿主只返回空正文 typed 状态，不能合成用户报告或再请求模型轮询。
# 函数用途: 必要输入等待结束当前运行，让原 finalization/claim/worker 正常释放资源。
def input_wait_response(agent, params):
    state = getattr(params, 'live_archive_state', None) or {}
    if not state.get('waiting_input_id'):
        return None
    return ModelResponse(text='', backend=str(getattr(params, 'backend', '') or ''),
                         runtime_status='unfinished', runtime_reason='TASK_INPUT_WAIT',
                         runtime_source='input_dependency', turn_end_reason='blocked')


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
