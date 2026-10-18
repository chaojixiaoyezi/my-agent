# LLM: 运输字段仅定位同一原请求/会话claim，不授P2/P3或业务权；v3必须完整往返，字段类型和目标/fence严格一致，不能降级丢弃。
# 模块用途: 校验普通输入中固定的一次运输投影，所有读取者共用同一纯codec。
from __future__ import annotations

import copy

from ..runtime_errors import DataCorruptionError
from .execution_target import GatewayExecutionTarget


# LLM: 只接受规范完整定位字段，不访问文件或推导权限；返回独立副本防调用者在摘要后改嵌套内容。
# 函数用途: 恢复一份完整运输快照，坏结构明确拒绝，禁止将未知当未绑定。
def _validated_foreground_transport(value):
    bad = DataCorruptionError('gateway input transport is invalid')
    if (type(value) is not dict or set(value) != {'schema_version', 'target', 'gateway_claim', 'route'}
            or value.get('schema_version') != 'gateway_input_transport.v1'):
        raise bad
    target = GatewayExecutionTarget.from_dict(value['target'])
    fence, route = value['gateway_claim'], value['route']
    if (target is None or value['target'] != target.to_dict() or target.kind != 'request'
            or type(fence) is not dict or set(fence) != {'request_id', 'execution_attempt_id', 'lease_epoch', 'lease_owner'}
            or any(type(fence.get(key)) is not str or not fence[key] or '/' in fence[key] or '\\' in fence[key]
                   for key in ('request_id', 'execution_attempt_id', 'lease_owner'))
            or type(fence.get('lease_epoch')) is not int or fence['lease_epoch'] < 1
            or target.turn_id != fence['request_id'] or target.execution_attempt_id != fence['execution_attempt_id']
            or type(route) is not dict or not route or type(route.get('c_run_id')) is not str
            or not route['c_run_id'] or not (route.get('worker') == 'C'
                or type(route.get('worker')) is int and route['worker'] >= 0)):
        raise bad
    return copy.deepcopy(value)


# LLM: 新版只消费原BG task/reference双身份；v1仍交原validator，不能通过宽松kind分支改变旧解释。
# 函数用途: 按显式版本恢复运输快照，完整字段类型与原引用逐项相符才接受。
def validated_input_transport(value):
    if type(value) is not dict or value.get('schema_version') != 'gateway_input_transport.v2':
        return _validated_foreground_transport(value)
    bad = DataCorruptionError('gateway background input transport is invalid')
    if set(value) != {'schema_version', 'target', 'gateway_claim', 'route', 'claim_transport_reference'}:
        raise bad
    target = GatewayExecutionTarget.from_dict(value['target'])
    ref, fence, route = value['claim_transport_reference'], value['gateway_claim'], value['route']
    keys = {'schema', 'request_id', 'gateway_execution_attempt_id', 'lease_epoch', 'lease_owner',
            'invocation_run_id', 'core_task_id', 'core_run_id', 'core_agent_run_id', 'core_attempt_id',
            'claim_id', 'thread_id'}
    if (target is None or type(value['target']) is not dict or value['target'] != target.to_dict()
            or target.kind != 'task' or type(ref) is not dict or set(ref) != keys
            or ref.get('schema') != 'gateway_claim_transport_reference.v1'
            or any(type(ref[k]) is not str or not ref[k] for k in keys - {'lease_epoch'})
            or type(ref['lease_epoch']) is not int or ref['lease_epoch'] < 1
            or type(fence) is not dict or set(fence) != {'request_id', 'execution_attempt_id', 'lease_epoch', 'lease_owner'}
            or type(route) is not dict or not route or type(route.get('c_run_id')) is not str or not route['c_run_id']
            or not (route.get('worker') == 'C' or type(route.get('worker')) is int and route['worker'] >= 0)):
        raise bad
    expected_fence = {'request_id': ref['request_id'], 'execution_attempt_id': ref['gateway_execution_attempt_id'],
                      'lease_epoch': ref['lease_epoch'], 'lease_owner': ref['lease_owner']}
    authority = {'schema_version': 'conversation_claim_execution.v1', 'claim_id': ref['claim_id'],
                 'invocation_run_id': ref['invocation_run_id'],
                 **{k: ref['core_' + k] for k in ('task_id', 'run_id', 'agent_run_id', 'attempt_id')}}
    if (any(type(fence[k]) is not type(v) or fence[k] != v for k, v in expected_fence.items())
            or any('/' in ref[k] or '\\' in ref[k] for k in ('request_id', 'gateway_execution_attempt_id', 'lease_owner'))
            or target.turn_id != ref['invocation_run_id'] or target.execution_attempt_id != ref['core_attempt_id']
            or target.task_id != ref['core_task_id'] or target.thread_id != ref['thread_id']
            or target.claim_id != ref['claim_id'] or target.runtime_authority != authority):
        raise bad
    return copy.deepcopy(value)


# LLM: 只定位原Gateway processing/T，不把后台invocation或新input ID当运输请求；不授写权。
# 函数用途: 为固定提交和生命周期检查取得运输请求键。
def transport_gateway_request_id(value):
    return validated_input_transport(value)['gateway_claim']['request_id']


# LLM: by_turn和原guidance始终按业务turn索引，BG为invocation，v1保原request；索引不是授权。
# 函数用途: 取得同一次业务收件和guidance结算键。
def transport_business_turn_id(value):
    return validated_input_transport(value)['target']['turn_id']
