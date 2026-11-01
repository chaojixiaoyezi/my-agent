"""真实native业务入口；W13只需注入原Owner服务/actor，不从参数恢复认证或工作根。

模型schema与插件目录来自同一W0表。只注册W1的29个动作，不能把102个目录项伪装为handler。
validate_invocation纯校验；authorize_invocation由原ActionPolicy在effect/claim之前调用。
"""
from __future__ import annotations

import json
from functools import lru_cache

from ..conversation.business_access import BusinessError, require_actor
from ..plugin_host.business_action_specs import (
    business_action_spec,
    business_action_specs,
    standalone_schema,
)
from ..plugin_host.errors import HostAPIError
from ..plugin_host.schemas import validate_object
from .models import (
    ApprovalPolicy,
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)


@lru_cache(maxsize=1)
def _specs():
    return tuple(s for s in business_action_specs().values() if s.metadata['implementer'] == 'W1')


def _schema():
    branches = []
    for spec in _specs():
        branch = spec.native_request_schema
        branch.pop('$defs', None)
        branches.append(branch)
    # 根properties只声明共同字段，完整条件必填、closed params和字节门都由分支负责。
    common = branches[0]['properties']
    result = {'type': 'object', 'properties': {**common,
        'action': {'type': 'string', 'enum': [s.action for s in _specs()]},
        'params': {'type': 'object'},
        'expected_revision': {'type': 'integer', 'minimum': 0, 'maximum': 9223372036854775807}},
        'required': ['action', 'params', 'request_id'], 'additionalProperties': False, 'oneOf': branches}
    return _model_projection(standalone_schema(result))


def _model_projection(value):
    # 原ToolModelSpec只接受有限子集。仅展示层将uniqueItems保存在x扩展；完整原schema
    # 在validate_request及ActionPolicy业务门原样执行，因此重复值仍在effect/claim前拒绝。
    if isinstance(value, dict):
        return {('x-business-unique-items' if key == 'uniqueItems' else key): _model_projection(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_model_projection(item) for item in value]
    return value


def _effect(spec):
    if spec.host_mode == 'read':
        return 'read_only'
    return 'dangerous' if spec.action in ('approvals.decide', 'policies.apply', 'assets.versions.accept') else 'mutating'


class BusinessSpacesTool(BaseTool):
    model_spec = ToolModelSpec('business_spaces', '管理业务空间、挂载、界面绑定、资产引用和原Workflow治理；不创建执行任务或授予权限。',
        _schema(), hints=ToolModelHints(category='business', keywords=('空间', '挂载', '业务', '资产', '治理')))
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy('mutating', by_parameter=(('action', tuple((s.action, _effect(s)) for s in _specs())),)),
        approval_policy=ApprovalPolicy('dangerous'), idempotency_policy=IdempotencyPolicy('operation'),
        resource_scopes=ResourceScopePolicy(mode='declared', static_scopes=('business:current_owner',)))

    def __init__(self, service, *, actor):
        self.service, self.actor = service, actor

    @staticmethod
    def validate_request(params):
        if type(params) is not dict:
            raise BusinessError('INVALID_SCHEMA')
        try:
            spec = business_action_spec(params.get('action'))
            if spec not in _specs():
                raise BusinessError('UNSUPPORTED_ACTION')
            return validate_object(params, spec.native_request_schema)
        except HostAPIError as error:
            raise BusinessError('INVALID_SCHEMA') from error

    def validate_invocation(self, params, context):
        try:
            self.validate_request(params)
        except BusinessError as error:
            return self._failure(error.code, effect='not_started', stage='validation')
        return None

    def authorize_invocation(self, params):
        request = self.validate_request(params)
        require_actor(self.actor, self.service)
        self.service.authorize_call(request['action'], request['params'], actor=self.actor)
        return request

    @staticmethod
    def _failure(code, *, effect='unknown', stage='execution'):
        control = 'TOOL_INVALID_ARGUMENTS' if code == 'INVALID_SCHEMA' else (
            'HOST_NOT_READY' if code == 'HOST_NOT_READY' else
            'TOOL_EXECUTION_DENIED' if effect == 'not_started' else 'TOOL_EXECUTION_ERROR')
        return ToolHandlerOutcome('business_spaces', False, json.dumps({'error_code': code}),
            error_code=control, reported_error_code=code, result_envelope={'error_code': code},
            effect_outcome=effect, failure_stage=stage)

    def execute(self, params):
        try:
            request = self.authorize_invocation(params)
        except BusinessError as error:
            return self._failure(error.code, effect='not_started',
                stage='validation' if error.code == 'INVALID_SCHEMA' else 'authorization')
        except (OSError, ValueError, TypeError):
            return self._failure('STORE_UNAVAILABLE', effect='not_started', stage='authorization')
        try:
            result = self.service.call(request['action'], request['params'], actor=self.actor,
                expected_revision=request.get('expected_revision'), idempotency_key=request.get('idempotency_key'))
            return ToolHandlerOutcome('business_spaces', True, json.dumps(result, ensure_ascii=False), result_envelope=result)
        except BusinessError as error:
            # 已进入领域/原后端后，不能仅凭错误码推断未提交；包括提交后的CAS类错误。
            return self._failure(error.code)
        except (OSError, ValueError, TypeError):
            # 原子rename后fsync/结算也可能报错；保留unknown，不自动重放或泄露原异常。
            return self._failure('STORE_UNAVAILABLE')
