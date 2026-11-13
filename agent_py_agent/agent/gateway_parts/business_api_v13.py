"""v13 扩展动作的唯一注册器；沿原活跃上下文、冻结领域端口和 owner 存储执行。"""
from __future__ import annotations

import hashlib
import importlib
import json
import time
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

from ..conversation.business_access import BusinessError
from ..plugin_host.business_action_specs import (
    business_action_spec,
    business_action_specs,
    business_defs,
)
from ..plugin_host.contracts import DomainResult
from ..plugin_host.errors import HostAPIError
from ..plugin_host.schemas import thaw, validate_object
from .business_api_v13_contracts import V13Action, V13Call


@dataclass(frozen=True)
class V13ActionSpec:
    definition: V13Action
    requested_name: str

    @property
    # LLM: 别名只改变请求名，授权与handler始终使用规范动作。
    # 函数用途: 返回规范动作名。
    def action(self):
        return self.definition.name

    @property
    # LLM: 不向冻结插件目录追加未授权的v13插件动作。
    # 函数用途: 声明原生扩展的展示名。
    def plugin_action(self):
        return self.action

    @property
    # LLM: 元数据仅供统一dispatcher选择，不签发权限。
    # 函数用途: 返回兼容原BusinessActionSpec的动作属性。
    def metadata(self):
        return {'action': self.action, 'implementer': 'V13-W0', 'effect': self.definition.effect,
            'idempotency_required': self.requires_idempotency,
            'expected_revision_required': self.expected_revision_required}

    @property
    # LLM: 非只读动作总是要求幂等键，声明不能削弱原运输门。
    # 函数用途: 返回幂等键要求。
    def requires_idempotency(self):
        return self.definition.idempotency_required or self.definition.effect != 'read'

    @property
    # LLM: CAS要求来自规范动作定义。
    # 函数用途: 返回修订号要求。
    def expected_revision_required(self):
        return self.definition.expected_revision_required

    @property
    # LLM: 管理员判断另走原生身份，不能冒充人类票据。
    # 函数用途: 保留原Spec的票据属性。
    def human_authorization_required(self):
        return False

    @property
    # LLM: schema按完整本地defs闭包校验，不依赖部署外任务文件。
    # 函数用途: 返回参数schema。
    def params_schema(self):
        return v13_schema(self.definition.params_schema)

    @property
    # LLM: 成功结果必须在发送前验证。
    # 函数用途: 返回终态schema。
    def result_schema(self):
        return v13_schema(self.definition.result_schema)

    @property
    # LLM: 仅显式DomainResult(202)选择受理schema。
    # 函数用途: 返回异步受理schema。
    def accepted_result_schema(self):
        shape = self.definition.accepted_result_schema
        return v13_schema(shape) if shape is not None else None

    @property
    # LLM: 请求信封闭合；身份不能从JSON携带，所有写都要求稳定键。
    # 函数用途: 构造原生JSON请求schema。
    def native_request_schema(self):
        properties = {'action': {'const': self.requested_name},
            'request_id': {'type': 'string', 'minLength': 1, 'maxLength': 128},
            'params': thaw(self.definition.params_schema),
            'idempotency_key': {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{16,128}$'},
            'expected_revision': {'type': 'integer', 'minimum': 0}}
        required = ['action', 'request_id', 'params']
        if self.requires_idempotency:
            required.append('idempotency_key')
        if self.expected_revision_required:
            required.append('expected_revision')
        return v13_schema({'type': 'object', 'properties': properties, 'required': required,
            'additionalProperties': False})

    # LLM: 受理不能冒充已完成；未声明受理shape必须拒绝。
    # 函数用途: 验证领域实际结果。
    def validate_result(self, value, *, accepted=False):
        schema = self.accepted_result_schema if accepted else self.result_schema
        if schema is None:
            raise HostAPIError('INVALID_SCHEMA')
        return validate_object(value, schema)


# LLM: 只接受本地引用；JSON递归值以闭包登记一次，不无限展开。
# 函数用途: 收集schema定义依赖。
def _schema_refs(value):
    if isinstance(value, dict):
        if '$ref' in value:
            ref = value['$ref']
            if not isinstance(ref, str) or not ref.startswith('#/$defs/'):
                raise ValueError('v13 schema必须引用本地定义')
            yield ref.removeprefix('#/$defs/').replace('~1', '/').replace('~0', '~')
        for child in value.values():
            yield from _schema_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _schema_refs(child)


# LLM: 合并冻结defs与分片v13 defs；冲突及未解析引用是部署错误而非静默缺能力。
# 函数用途: 为schema附上传递可达的定义闭包。
def v13_schema(schema):
    result = thaw(schema)
    definitions = {**business_defs(), **V13_DEFS}
    supplied = result.pop('$defs', {})
    for name, shape in supplied.items():
        if name in definitions and definitions[name] != shape:
            raise ValueError('v13定义冲突')
        definitions[name] = shape
    pending, closure = list(_schema_refs(result)), {}
    while pending:
        name = pending.pop()
        if name in closure:
            continue
        if name not in definitions:
            raise ValueError('v13定义缺失: ' + name)
        closure[name] = thaw(definitions[name])
        pending.extend(_schema_refs(closure[name]))
    if closure:
        result['$defs'] = {name: closure[name] for name in sorted(closure)}
    return result


# LLM: 只添加v13新码；其它HTTP语义仍归冻结_error_code。
# 函数用途: 查询扩展错误的状态码。
def v13_error_status(code):
    return {'VERIFICATION_FAILED': 403, 'STATE_CONFLICT': 409, 'QUERY_REJECTED': 400,
        'LIMIT_EXCEEDED': 400, 'RATE_LIMITED': 429}.get(code)


# LLM: 校验仍使用原plugin_host验证器，拒绝额外字段。
# 函数用途: 验证规范v13参数。
def v13_validate_params(action, params):
    return validate_object(params, v13_action_spec(action).params_schema)


# LLM: 校验同一注册器声明的实际阶段。
# 函数用途: 验证v13返回值。
def v13_validate_result(action, result, *, accepted=False):
    return v13_action_spec(action).validate_result(result, accepted=accepted)


# LLM: 必须是原上下文签发对象，复验owner/home及实时授权；facts不能冒领其它owner。
# 函数用途: 在读取和每次提交前核验调用权限。
def v13_require_access(call, record=None):
    from .business_api import _ISSUED, BusinessCallContext
    context = call.context
    if type(context) is not BusinessCallContext or context not in _ISSUED:
        raise HostAPIError('CAPABILITY_DENIED', 403)
    if (call.owner_id != context.owner_id or call.subject_id != context.subject_id
            or Path(call.owner_home).resolve() != Path(context.host.home_paths.owner_home_dir).resolve()
            or call.services.owner_id != call.owner_id):
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    facts = {'owner_id': call.owner_id, **call.params}
    if record is not None:
        if record.get('owner_id', call.owner_id) != call.owner_id:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        facts.update(record)
    context.check(call.action, facts)
    return True


# LLM: 原生HTTP只消费middleware当前裁决；本地仅local main，业务RBAC绝不能扩大管理员。
# 函数用途: 核验宿主管理员而非业务角色。
def v13_require_admin(call):
    v13_require_access(call)
    host = call.context.host
    native = getattr(host, 'business_native_admin', None)
    paths = host.home_paths
    allowed = (native() is True if callable(native) else
        getattr(paths, 'owner_provider', None) == 'local' and getattr(paths, 'owner_kind', None) == 'main')
    if not allowed:
        raise HostAPIError('BUSINESS_ACCESS_DENIED', 403)
    return True


# LLM: 提供者只能由可信宿主绑定，模型JSON与字符串模块名不能新增端口。
# 函数用途: 取得已绑定的原生提供者。
def v13_provider(call, name):
    v13_require_access(call)
    provider = getattr(call.context.host, 'business_v13_providers', {}).get(name)
    if provider is None:
        raise HostAPIError('HOST_NOT_READY', 503)
    return provider


# LLM: frozen调用复用同一个实时context和领域端口，不新造actor或旁路授权。
# 函数用途: 从扩展handler调用冻结业务动作。
def v13_call_frozen(call, action, params, *, idempotency_key=None, expected_revision=None):
    v13_require_access(call)
    spec = business_action_spec(action)
    params = validate_object(params, spec.params_schema)
    call.context.check(action, {'owner_id': call.owner_id, **params})
    if not call.services.ready(action):
        raise HostAPIError('HOST_NOT_READY', 503)
    raw = call.services.invoke(action, params, call.context,
        idempotency_key=call.idempotency_key if idempotency_key is None else idempotency_key,
        expected_revision=expected_revision)
    accepted = isinstance(raw, DomainResult) and raw.http_status == 202
    data = spec.validate_result(thaw(raw.data) if isinstance(raw, DomainResult) else raw, accepted=accepted)
    call.context.check(action, {'owner_id': call.owner_id, **params})
    return DomainResult(202, data) if accepted else data


# LLM: 空间必须来自原W1当前owner的真实记录，不以输入ID或v13缓存授予访问。
# 函数用途: 解析并授权当前空间。
def v13_resolve_space(call, space_id=None):
    selected = space_id or call.params.get('space_id')
    if not selected:
        raise HostAPIError('INVALID_SCHEMA')
    return v13_call_frozen(call, 'spaces.get', {'space_id': selected})['space']


# LLM: 调用对象只从实时边界构造；readiness不获得写入权。
# 函数用途: 固定规范动作、当前身份和一次请求的信封字段。
def _call(services, action, params, context, *, request_id='', idempotency_key='', expected_revision=None):
    return V13Call(v13_action_spec(action).action, params, request_id,
        services.owner_id, getattr(context, 'subject_id', ''), context, services, services.owner_home,
        idempotency_key, expected_revision, getattr(services.host, 'business_v13_clock', time.time))


# LLM: ready回调只检查真实来源，不把结果缓存为授权。
# 函数用途: 查询扩展动作当前是否可执行。
def v13_ready(action, services, context=None):
    if not bool(getattr(services.host.config, 'business_spaces_enabled', False)):
        return False
    definition = v13_action_spec(action).definition
    call = _call(services, action, {}, context)
    try:
        return definition.ready is None or definition.ready(call) is True
    except (OSError, ValueError, TypeError, RuntimeError, HostAPIError, BusinessError):
        return False


# LLM: 每次写前重核原context；返回值在本层和运输层都按当前阶段校验。
# 函数用途: 执行一个已注册扩展动作。
def v13_invoke(action, params, context, services, *, request_id='', idempotency_key='', expected_revision=None):
    spec = v13_action_spec(action)
    params = v13_validate_params(action, params)
    call = _call(services, action, params, context, request_id=request_id,
        idempotency_key=idempotency_key, expected_revision=expected_revision)
    v13_require_access(call)
    if spec.definition.admin:
        v13_require_admin(call)
    if not v13_ready(action, services, context):
        raise HostAPIError('HOST_NOT_READY', 503)
    if spec.requires_idempotency and not idempotency_key:
        raise HostAPIError('INVALID_SCHEMA')
    if spec.expected_revision_required and (type(expected_revision) is not int or expected_revision < 0):
        raise HostAPIError('INVALID_SCHEMA')
    v13_require_access(call)
    result = spec.definition.handler(call)
    accepted = isinstance(result, DomainResult) and result.http_status == 202
    data = v13_validate_result(action, thaw(result.data) if isinstance(result, DomainResult) else result,
        accepted=accepted)
    return DomainResult(202, data) if accepted else data


# LLM: 目录独立于冻结system.capabilities，逐项查询来源与当前授权，不填补未合入分片。
# 函数用途: 返回就绪且有权的扩展动作及显式别名。
def _capabilities_extensions(call):
    supported, aliases = [], []
    for name in _ACTIONS:
        selected = replace(call, action=name, params={})
        try:
            v13_require_access(selected)
            if _ACTIONS[name].admin:
                v13_require_admin(selected)
            if not v13_ready(name, call.services, call.context):
                continue
        except (HostAPIError, BusinessError):
            continue
        supported.append(name)
    for alias, canonical in ALIASES.items():
        if canonical not in supported:
            continue
        try:
            call.context.check(alias, {'owner_id': call.owner_id})
        except (HostAPIError, BusinessError):
            continue
        aliases.append({'alias': alias, 'canonical': canonical})
    return {'schema_version': 'business.v13', 'supported_actions': sorted(supported),
        'aliases': sorted(aliases, key=lambda row: row['alias']), 'transport_modes': ['native_json'],
        'limits': {'page_default': 50, 'page_max': 100}}


# LLM: 仅有原后台写口与任务观察口时可接异步；模型输出不是执行事实。
# 函数用途: 检查通用作业所依赖的两条原生端口。
def _jobs_ready(call):
    return callable(getattr(call.services.background, 'execute', None)) and call.services.ready('tasks.get')


# LLM: 所有引用必须来自本owner真实线程并逐次执行冻结资源授权。
# 函数用途: 校验thread_id所指向的后台身份或v13会话。
def _alias_thread(context, services, thread_id, *, space_id=None, background_only=False):
    from ..business_spaces.v13_store import v13_store_for
    from ..conversation.background_team_lifecycle import background_identity
    store = getattr(context.host, 'conversation_store', None)
    if store is None:
        raise HostAPIError('HOST_NOT_READY', 503)
    thread = store.threads.load(thread_id)
    if thread is None or thread.owner_id != context.owner_id:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    identity = background_identity(thread)
    if identity is None and not background_only:
        call = V13Call('messages.list', {}, '', context.owner_id, context.subject_id,
            context, services, services.owner_home)
        # W11会话ID本身就是线程ID；不按展示title或metadata猜会话类型。
        conversations = v13_store_for(call).list('conversations')
        if not any(row.get('conversation_id') == thread_id and row.get('owner_id') == context.owner_id
                for row in conversations):
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    elif identity is None:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    if space_id is not None and (identity is None or identity.space_id != space_id):
        raise HostAPIError('SPACE_SCOPE_MISMATCH', 404)
    context.check('messages.list', {'owner_id': context.owner_id, 'thread_id': thread_id,
        'agent_id': thread_id, 'space_id': identity.space_id if identity else space_id})
    return thread


# LLM: 别名在冻结闭合params校验之前显式改写；不让新字段渗入W2或冻结schema。
# 函数用途: 规范三个thread_id参数别名，返回任务直派标记。
def v13_frozen_alias(request, context, services):
    action = request['action']
    params = request.get('params')
    if action not in {'messages.list', 'events.list', 'tasks.submit'} or type(params) is not dict or 'thread_id' not in params:
        return request, False
    schema = business_action_spec(action).native_request_schema
    shape = schema['properties']['params']
    shape['properties']['thread_id'] = {'type': 'string', 'minLength': 1, 'maxLength': 128, 'x-max-bytes': 128}
    shape['required'] = [key for key in shape.get('required', []) if key not in {'target', 'controller_agent_id'}]
    request = validate_object(request, schema)
    params = dict(request['params'])
    thread_id = params.pop('thread_id')
    if action == 'messages.list':
        if 'target' in params:
            raise HostAPIError('INVALID_SCHEMA')
        _alias_thread(context, services, thread_id)
        params['target'] = {'kind': 'background_main', 'id': thread_id}
    elif action == 'events.list':
        selected = dict(params.get('filter', {}))
        if selected.get('agent_id', thread_id) != thread_id:
            raise HostAPIError('INVALID_SCHEMA')
        selected['agent_id'] = thread_id
        params['filter'] = selected
    else:
        if 'controller_agent_id' in params or 'asset_refs' in params:
            raise HostAPIError('INVALID_SCHEMA')
        _alias_thread(context, services, thread_id, space_id=params.get('space_id'), background_only=True)
        params['agent_id'] = thread_id
    return {**request, 'params': params}, action == 'tasks.submit'


# LLM: 冻结submit的直派别名沿同一W2 port及幂等键，原动作授权与accepted shape保持。
# 函数用途: 映射thread_id直派并返回原submit受理DTO。
def v13_submit_alias(request, context, services):
    params = validate_object(request['params'], business_action_spec('tasks.assign').params_schema)
    context.check('tasks.submit', {'owner_id': context.owner_id, **params})
    if not services.ready('tasks.assign'):
        raise HostAPIError('HOST_NOT_READY', 503)
    result = services.invoke('tasks.assign', params, context,
        idempotency_key=request['idempotency_key'], expected_revision=request.get('expected_revision'))
    data = _assignment_receipt(result)
    return DomainResult(202, {'task_id': data['task_id'], 'operation_id': data['operation_id'],
        'request_id': request['request_id'], 'state': 'accepted'})


# LLM: 固定W2 assign的耐久回执可适配202；不依据任意JSON state猜执行状态，不改冻结外部回包。
# 函数用途: 兼容原任务接单返回值，操作引用直接沿用真实assignment_ref。
def _assignment_receipt(result):
    spec = business_action_spec('tasks.assign')
    if isinstance(result, DomainResult):
        if result.http_status != 202:
            raise HostAPIError('HOST_NOT_READY', 503)
        return spec.validate_result(thaw(result.data), accepted=True)
    data = spec.validate_result(result)
    if data['receipt_state'] != 'accepted':
        raise HostAPIError('HOST_NOT_READY', 503)
    return spec.validate_result({'assignment_ref': data['assignment_ref'], 'task_id': data['task_id'],
        'operation_id': data['assignment_ref']}, accepted=True)


V13_SLICE_MODULES = (
    'agent_py_agent.agent.gateway_parts.business_api_v13_workflow_health',
    'agent_py_agent.agent.gateway_parts.business_api_v13_w7',
    'agent_py_agent.agent.gateway_parts.business_api_v13_w8',
    'agent_py_agent.agent.gateway_parts.business_api_v13_w9',
    'agent_py_agent.agent.gateway_parts.business_api_v13_w10',
    'agent_py_agent.agent.gateway_parts.business_api_v13_w11',
    'agent_py_agent.agent.gateway_parts.business_api_v13_w12',
    'agent_py_agent.agent.gateway_parts.business_api_v13_sessions',
    'agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w1',
    'agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w2',
    'agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w3',
    'agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w4',
    'agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w5',
    'agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w6',
    'agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w7',
    'agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w8',
    'agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w9',
)


# LLM: 分片只有显式四张表可耦合；缺整个分片可暂缺，分片内部依赖损坏必须显式报错。
# 函数用途: 合并静态分片并拒绝动作、别名和契约冲突。
def _load_registry(module_names=None):
    actions = {item.name: item for item in V13_ACTIONS}
    aliases, definitions, kinds = {}, dict(_OWN_DEFS), {}
    collections = {'assistant_agents': 'owner', 'async_jobs': 'owner', 'audit_log': 'owner', 'admin_ops': 'main'}
    for name in V13_SLICE_MODULES if module_names is None else module_names:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as error:
            if error.name != name:
                raise
            continue
        for action in module.V13_ACTIONS:
            if action.name in actions or action.name in aliases:
                raise ValueError('v13动作重名: ' + action.name)
            actions[action.name] = action
        for table, incoming in ((definitions, getattr(module, 'V13_DEFS', {})),
                (kinds, getattr(module, 'V13_JOB_KINDS', {})),
                (collections, getattr(module, 'V13_COLLECTIONS', {}))):
            for key, value in incoming.items():
                if key in table and table[key] != value:
                    raise ValueError('v13契约冲突: ' + key)
                table[key] = value
    for action in actions.values():
        if action.name in business_action_specs():
            raise ValueError('v13不能覆盖冻结动作')
        for alias in action.aliases:
            if alias in aliases or alias in actions or alias in business_action_specs():
                raise ValueError('v13别名重名: ' + alias)
            aliases[alias] = action.name
    if any(value not in {'main', 'owner'} for value in collections.values()):
        raise ValueError('v13 collection scope无效')
    return actions, aliases, definitions, kinds, collections


# LLM: 输出只读映射，不能由一次JSON请求动态注册handler。
# 函数用途: 返回规范动作和别名的兼容Spec表。
def v13_action_specs():
    return MappingProxyType({name: V13ActionSpec(_ACTIONS[ALIASES.get(name, name)], name)
        for name in (*_ACTIONS, *ALIASES)})


# LLM: 未注册名称与冻结动作遵守同一个UNSUPPORTED_ACTION码。
# 函数用途: 解析一个扩展动作或显式别名。
def v13_action_spec(action):
    canonical = ALIASES.get(action, action)
    if canonical not in _ACTIONS:
        raise HostAPIError('UNSUPPORTED_ACTION')
    return V13ActionSpec(_ACTIONS[canonical], action)


# LLM: 指纹只基于严格JSON结构，不使用自然语言状态识别。
# 函数用途: 为内部幂等派发生成稳定摘要。
def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


# LLM: 时钟由当前调用注入，不读取用户展示时间作为执行事实。
# 函数用途: 生成统一UTC时间戳。
def _timestamp(call):
    return datetime.fromtimestamp(call.clock(), timezone.utc).isoformat()


# LLM: Store锁内只创建一次空间助手；原后台创建仍沿冻结W2与稳定键。
# 函数用途: 获取空间唯一的v13后台助手。
def _assistant_agent(call, space_id):
    from ..business_spaces.v13_store import v13_store_for
    store = v13_store_for(call)

    # LLM: 原Store串行化同空间创建，重放不会重复创建线程。
    # 函数用途: 在助手集合事务中绑定原后台agent。
    def mutate(records):
        row = records.get(space_id)
        if row is not None:
            v13_require_access(call, row)
            return row
        created = v13_call_frozen(call, 'background_agents.create',
            {'space_id': space_id, 'name': '空间助手', 'purpose': 'v13结构化异步工作', 'after_task': 'reuse'},
            idempotency_key='v13-assistant-' + _digest([call.owner_id, space_id])[:48])
        at = _timestamp(call)
        row = {'space_id': space_id, 'owner_id': call.owner_id, 'agent_id': created['agent']['agent_id'],
            'purpose': 'v13', 'revision': 0, 'created_at': at, 'updated_at': at}
        records[space_id] = row
        return row

    return store.transaction('assistant_agents', mutate,
        idempotency_key='assistant-' + _digest(space_id)[:48], params_digest=_digest(space_id))['agent_id']


# LLM: 作业只关联原SessionTask；派发前耐久固定首次提示、事实和目标，预留不冒充任务受理。
# 函数用途: 先保存作业上下文，再关联原派发回执；中断重试继续首次快照。
def v13_start_model_job(call, kind, instruction, *, space_id=None, agent_id=None, context_facts=None):
    from ..business_spaces.v13_store import v13_store_for
    if kind not in JOB_KINDS or type(instruction) is not str or not instruction:
        raise HostAPIError('HOST_NOT_READY', 503)
    v13_require_access(call)
    space = v13_resolve_space(call, space_id)
    store = v13_store_for(call)
    fingerprint = _digest([call.action, call.params, kind, space['space_id'], agent_id])
    job_id = 'job-' + _digest([call.owner_id, call.subject_id, call.action, call.idempotency_key])[:48]
    previous = store.get('async_jobs', job_id)
    if previous is not None:
        v13_require_access(call, previous)
        if previous.get('intent_digest') != fingerprint:
            raise HostAPIError('IDEMPOTENCY_CONFLICT', 409)
        if previous['task_id'] is not None and previous['operation_id'] is not None:
            return DomainResult(202, _job_accepted(previous))
    else:
        facts = validate_object({} if context_facts is None else context_facts,
            v13_schema({'$ref': '#/$defs/JsonObject'}))
        _digest(facts)
        facts = deepcopy(facts)
        if agent_id is None:
            agent_id = _assistant_agent(call, space['space_id'])
        else:
            _alias_thread(call.context, call.services, agent_id, space_id=space['space_id'], background_only=True)
            v13_call_frozen(call, 'background_agents.get', {'agent_id': agent_id})

        # LLM: 此事务无任务派发副作用；并发先提交者决定首次上下文，回执只保存作业身份。
        # 函数用途: 在原W2派发之前耐久预留可恢复的首次快照。
        def prepare(records):
            row = records.get(job_id)
            if row is not None:
                v13_require_access(call, row)
                if row.get('intent_digest') != fingerprint:
                    raise HostAPIError('IDEMPOTENCY_CONFLICT', 409)
            else:
                at = _timestamp(call)
                records[job_id] = {'job_id': job_id, 'owner_id': call.owner_id, 'kind': kind, 'action': call.action,
                    'subject_id': call.subject_id, 'space_id': space['space_id'], 'agent_id': agent_id,
                    'intent_digest': fingerprint, 'context_facts': facts, 'instruction': instruction,
                    'task_id': None, 'operation_id': None, 'state': 'unknown',
                    'result': None, 'error': None, 'revision': 0, 'created_at': at, 'updated_at': at}
            return {'job_id': job_id}

        store.transaction('async_jobs', prepare, idempotency_key='prepare-' + job_id, params_digest=fingerprint)

    # LLM: W2持有真实派发幂等账；提交前中断后仍使用已落盘提示和同一原生键，不凭预留声称accepted。
    # 函数用途: 从首次快照派发并提交准确的原生任务和操作引用。
    def dispatch(records):
        row = records[job_id]
        v13_require_access(call, row)
        if row['intent_digest'] != fingerprint:
            raise HostAPIError('IDEMPOTENCY_CONFLICT', 409)
        if row['task_id'] is None or row['operation_id'] is None:
            accepted = v13_call_frozen(call, 'tasks.assign', {'agent_id': row['agent_id'],
                'space_id': row['space_id'], 'instruction': row['instruction']}, idempotency_key='v13-' + job_id)
            data = _assignment_receipt(accepted)
            row.update(task_id=data['task_id'], operation_id=data['operation_id'], state='accepted',
                revision=row['revision'] + 1, updated_at=_timestamp(call))
        return _job_accepted(row)

    result = store.transaction('async_jobs', dispatch, idempotency_key=call.idempotency_key, params_digest=fingerprint)
    return DomainResult(202, result)


# LLM: 重放受理引用不会把终态改回accepted，也不要求分片访问W0的写口。
# 函数用途: 从已保存的作业提取同一份受理回执。
def _job_accepted(row):
    return {'state': 'accepted', 'operation_id': row['operation_id'], 'job_id': row['job_id'],
        'task_id': row['task_id']}


# LLM: 终态先提交再调用分片回写；回调按至少一次语义自行幂等，失败保留终态以供原入口重试。
# 函数用途: 在首次终态和终态重读时触发已注册回调，传副本防止篡改W0响应或记录。
def _job_on_complete(call, row):
    if row['state'] not in {'completed', 'failed'}:
        return
    callback = JOB_KINDS.get(row['kind'], {}).get('on_complete')
    if callback is None:
        return
    if not callable(callback):
        raise HostAPIError('HOST_NOT_READY', 503)
    v13_require_access(call, row)
    callback(call, deepcopy(row))
    v13_require_access(call, row)


# LLM: 只读取匹配准确task的canonical assistant正文；summary或其它任务的末条不能冒充输出。
# 函数用途: 从冻结消息读口读取作业最终模型输出。
def _job_text(call, row):
    params = {'target': {'kind': 'background_main', 'id': row['agent_id']}, 'limit': 100}
    for page_index in range(10):
        page = v13_call_frozen(call, 'messages.list', params)
        messages = [message for message in page['items']
            if message['task_id'] == row['task_id'] and message['role'] == 'assistant']
        if messages:
            return messages[-1]['text']
        if page['next_cursor'] is None:
            break
        params['cursor'] = page['next_cursor']
    store = getattr(getattr(call.services, 'host', None), 'conversation_store', None)
    task = store.session_tasks.load(row['task_id']) if store is not None else None
    meta = getattr(task, 'metadata', None) if task is not None else None
    if isinstance(meta, dict):
        trusted = meta.get('trusted_task_completion')
        candidate = meta.get('task_learning_candidate')
        material = candidate.get('request_material') if isinstance(candidate, dict) else None
        if (isinstance(trusted, dict) and trusted.get('task_id') == row['task_id']
                and trusted.get('owner_id') == row['owner_id']
                and trusted.get('thread_id') == row['agent_id']
                and isinstance(material, dict) and material.get('task_id') == row['task_id']
                and isinstance(material.get('final_response'), str) and material['final_response']):
            return material['final_response']
    raise HostAPIError('HOST_NOT_READY', 503)


# LLM: 状态只来自规范任务字段；终态结果经schema与只读业务约束，回写失败不得改成模型失败。
# 函数用途: 同步并校验模型作业，在耐久终态后执行可重试的分片回调。
def _async_jobs_get(call):
    from ..business_spaces.v13_store import v13_store_for
    store = v13_store_for(call)
    row = store.get('async_jobs', call.params['job_id'])
    if row is None:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    v13_require_access(call, row)
    coverage = 'unknown' if row['task_id'] is None else 'complete'
    if row['task_id'] is not None and row['state'] not in {'completed', 'failed'}:
        task = v13_call_frozen(call, 'tasks.get', {'task_id': row['task_id']})['task']
        state = {'queued': 'accepted', 'pending': 'accepted', 'accepted': 'accepted', 'running': 'running',
            'done': 'completed', 'completed': 'completed', 'failed': 'failed', 'cancelled': 'failed'}.get(task['status'], 'unknown')
        result, error = None, None
        if state == 'completed':
            kind = JOB_KINDS.get(row['kind'])
            if kind is None:
                raise HostAPIError('HOST_NOT_READY', 503)
            text = _job_text(call, row)
            validator = kind.get('validate_result')
            if validator is not None:
                v13_require_access(call, row)
                if not callable(validator):
                    raise HostAPIError('HOST_NOT_READY', 503)
            try:
                result = validate_object(kind['finalize'](text), v13_schema(kind['result_schema']))
                if validator is not None and validator(call, deepcopy(row), deepcopy(result)) is not True:
                    raise ValueError('作业结果未通过登记的业务约束')
            except (HostAPIError, ValueError, TypeError, KeyError):
                state, result = 'failed', None
                error = {'code': 'JOB_RESULT_INVALID', 'message': '模型输出不符合已登记的结果结构'}
        elif state == 'failed':
            error = {'code': 'JOB_TASK_FAILED', 'message': '规范后台任务未成功完成'}
        if state == 'unknown':
            coverage = 'unknown'
        if state != row['state'] or result != row['result'] or error != row['error']:
            observed = row

            # LLM: 锁内基于最新行更新，晚到读取不能把终态倒退为running。
            # 函数用途: 单调提交作业观察状态和结果。
            def mutate(records):
                latest = records.get(observed['job_id'])
                if latest is None:
                    raise HostAPIError('RESOURCE_NOT_FOUND', 404)
                v13_require_access(call, latest)
                if latest['state'] not in {'completed', 'failed'} and latest['revision'] == observed['revision']:
                    latest.update(state=state, result=result, error=error,
                        revision=latest['revision'] + 1, updated_at=_timestamp(call))
                return latest

            row = store.transaction('async_jobs', mutate,
                idempotency_key='observe-' + _digest([row['job_id'], row['revision'], state, result, error])[:48],
                params_digest=_digest([row['job_id'], row['revision'], state, result, error]))
    _job_on_complete(call, row)
    return {key: row[key] for key in ('job_id', 'kind', 'state', 'task_id', 'result', 'error',
        'created_at', 'updated_at')} | {'coverage': coverage}


_OWN_DEFS = {'V13AliasRef': {'type': 'object',
                 'properties': {'alias': {'type': 'string', 'minLength': 1, 'maxLength': 128},
                                'canonical': {'type': 'string', 'minLength': 1, 'maxLength': 128}},
                 'required': ['alias', 'canonical'],
                 'additionalProperties': False},
 'V13JobError': {'type': 'object',
                 'properties': {'code': {'type': 'string', 'minLength': 1, 'maxLength': 128},
                                'message': {'type': 'string', 'minLength': 0, 'maxLength': 512}},
                 'required': ['code', 'message'],
                 'additionalProperties': False}}
V13_DEFS = dict(_OWN_DEFS)

V13_ACTIONS = (
    V13Action('system.capabilities.extensions', 'read',
        {'additionalProperties': False, 'properties': {}, 'required': [], 'type': 'object'},
        {'additionalProperties': False,
 'properties': {'aliases': {'items': {'$ref': '#/$defs/V13AliasRef'}, 'maxItems': 500, 'type': 'array'},
                'limits': {'additionalProperties': False,
                           'properties': {'page_default': {'minimum': 0, 'type': 'integer'},
                                          'page_max': {'minimum': 0, 'type': 'integer'}},
                           'required': ['page_default', 'page_max'],
                           'type': 'object'},
                'schema_version': {'maxLength': 128, 'minLength': 1, 'type': 'string'},
                'supported_actions': {'items': {'maxLength': 128, 'minLength': 1, 'type': 'string'},
                                      'maxItems': 1000,
                                      'type': 'array'},
                'transport_modes': {'items': {'enum': ['native_json'], 'type': 'string'},
                                    'maxItems': 1,
                                    'type': 'array'}},
 'required': ['schema_version', 'supported_actions', 'aliases', 'transport_modes', 'limits'],
 'type': 'object'},
        _capabilities_extensions),
    V13Action('async_jobs.get', 'read',
        {'additionalProperties': False,
 'properties': {'job_id': {'$ref': '#/$defs/Id'}},
 'required': ['job_id'],
 'type': 'object'},
        {'additionalProperties': False,
 'properties': {'coverage': {'$ref': '#/$defs/Coverage'},
                'created_at': {'$ref': '#/$defs/Timestamp'},
                'error': {'anyOf': [{'$ref': '#/$defs/V13JobError'}, {'type': 'null'}]},
                'job_id': {'$ref': '#/$defs/Id'},
                'kind': {'maxLength': 64, 'minLength': 1, 'type': 'string'},
                'result': {'anyOf': [{'$ref': '#/$defs/JsonObject'}, {'type': 'null'}]},
                'state': {'enum': ['accepted', 'running', 'completed', 'failed', 'unknown'],
                          'type': 'string'},
                'task_id': {'$ref': '#/$defs/NullableId'},
                'updated_at': {'$ref': '#/$defs/Timestamp'}},
 'required': ['job_id', 'kind', 'state', 'task_id', 'result', 'error', 'created_at', 'updated_at',
              'coverage'],
 'type': 'object'},
        _async_jobs_get, ready=_jobs_ready),
)

_ACTIONS, ALIASES, V13_DEFS, JOB_KINDS, COLLECTIONS = _load_registry()


# LLM: 仅已登记v13会话可复用canonical消息；未登记普通线程不能借background_main读取。
# 函数用途: 为v13会话适配冻结messages.list结果，普通后台身份仍由原观察服务读取。
def v13_conversation_messages(params, context, services):
    from ..business_spaces.v13_store import v13_store_for
    from ..common.log_redaction import redact_sensitive_value
    from ..conversation.background_team_lifecycle import background_identity
    target = params['target']
    if target['kind'] != 'background_main':
        return None
    store = getattr(context.host, 'conversation_store', None)
    if store is None:
        return None
    thread = store.threads.load(target['id'])
    if thread is None or background_identity(thread) is not None:
        return None
    call = V13Call('messages.list', params, '', context.owner_id, context.subject_id,
        context, services, services.owner_home)
    conversation = v13_store_for(call).get('conversations', thread.thread_id)
    if conversation is None:
        return None
    _alias_thread(context, services, thread.thread_id)
    v13_require_access(call, conversation)
    projection = services.observation
    actor = services.observation_actor(context)
    projection.authorize_call('messages.list', params, actor=actor)
    scope = projection.scope_digest('messages.list', params, actor)
    position = projection.cursor_position(params.get('cursor'), scope)
    path = store.storage.message_path(thread.thread_id)
    info = path.stat() if path.exists() else None
    generation = _digest([info.st_dev, info.st_ino]) if info is not None else 'unavailable'
    before, offset, bound_limit = None, 0, params.get('limit', 50)
    if position is not None:
        if position.get('generation') != generation:
            raise BusinessError('CURSOR_EXPIRED')
        before, offset, bound_limit = position.get('before'), position.get('offset'), position.get('bound_limit')
        if (type(before) is not int or before < 0 or type(offset) is not int or offset < 0
                or type(bound_limit) is not int or not 1 <= bound_limit <= 100):
            raise BusinessError('CURSOR_EXPIRED')
    page = store.messages.history_page_report(thread.thread_id, before=before, limit=bound_limit)
    if page.errors:
        raise BusinessError('CURSOR_EXPIRED' if position else 'HOST_NOT_READY')
    if offset > len(page.rows):
        raise BusinessError('CURSOR_EXPIRED')
    selected, items = page.rows[offset:offset + params.get('limit', 50)], []
    for message in selected:
        v13_require_access(call, conversation)
        if message.role not in {'user', 'assistant', 'tool', 'host'}:
            continue
        if len(message.content) > 262144:
            raise BusinessError('LIMIT_EXCEEDED')
        items.append({'message_id': message.message_id, 'thread_id': thread.thread_id,
            'task_id': message.metadata.get('task_id'), 'space_id': conversation.get('space_id'),
            'role': message.role, 'text': redact_sensitive_value(message.content),
            'occurred_at': datetime.fromtimestamp(message.created_at, timezone.utc).isoformat() if message.created_at else None,
            'source': {'kind': 'canonical_message', 'source_ref': message.message_id, 'revision': None}, 'artifact_refs': []})
    offset += len(selected)
    next_position = ({'before': page.after, 'offset': offset, 'bound_limit': bound_limit} if offset < len(page.rows)
        else {'before': page.before, 'offset': 0, 'bound_limit': bound_limit} if page.before else None)
    if next_position is not None:
        next_position['generation'] = generation
    coverage = 'partial' if next_position else 'complete'
    return {'items': items, 'next_cursor': projection.cursor(scope, next_position) if next_position else None,
        'coverage': coverage, 'history_boundary': {'first_message_id': items[0]['message_id'] if items else None,
            'last_message_id': items[-1]['message_id'] if items else None, 'coverage': coverage,
            'reason_code': 'CANONICAL_PAGE'}}
