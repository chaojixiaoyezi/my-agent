"""空间/团队/流程的薄命令消费端：宿主认证 HTTP 是唯一业务执行入口。"""
from __future__ import annotations

import json
import copy
from dataclasses import dataclass, field
from functools import lru_cache, partial
from uuid import uuid4

from agent_py_agent.agent.plugin_host.business_action_specs import (
    business_action_spec,
    business_schema_bundle,
    standalone_schema,
)
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.schemas import validate_object

# 冻结回执中 HTTP 200 只对应 completed、202 只对应 accepted；服务端回执没有 ok 字段。
_CONFIRMED_STATES = {200: 'completed', 202: 'accepted'}
# V02-02：冻结动作目录没有切换会话当前空间的写入动作。会话执行用的空间在派发任务时由
# 业务上下文（space_id）冻结，`/space use` 只能只读展示，不得把 spaces.get 当成切换成功。
_SWITCH_UNSUPPORTED_CODE = 'SPACES_SWITCH_UNSUPPORTED'
_SWITCH_UNSUPPORTED_WARNING = ('未切换会话当前空间：宿主没有 spaces.use 类写入动作；'
                               '下一次任务的归属空间仍由派发时的 space_id 决定。')


def _action_spec(action: str):
    """冻结目录优先；未收录的原生扩展动作沿同一 v13 注册表解析，不建第二客户端目录。"""
    try:
        return business_action_spec(action)
    except HostAPIError:
        from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_action_spec

        return v13_action_spec(action)


@lru_cache(maxsize=1)
def _response_envelope_schema() -> dict:
    return standalone_schema(business_schema_bundle()['response_envelope_schema'])


@lru_cache(maxsize=1)
def _permissive_envelope_schema() -> dict:
    """v13扩展动作的封套仍按原结构校验，data单独交给该动作自身结果schema。"""
    schema = copy.deepcopy(_response_envelope_schema())
    schema['properties']['data'] = {}
    return schema


def _envelope_schema(action) -> dict:
    """冻结动作保留完整data联合；未收录的扩展动作只放宽封套里的data，不放宽动作结果。"""
    if isinstance(action, str):
        try:
            business_action_spec(action)
        except HostAPIError:
            return _permissive_envelope_schema()
    return _response_envelope_schema()


def _unconfirmed(request: dict, status: int, code: str, body: object) -> dict:
    """无法确认的结果只投影为未知；保留原准备请求身份，供核对或原样重送同一对象。"""
    result = {'ok': False, 'http_status': status, 'request_id': request['request_id'],
              'idempotency_key': request.get('idempotency_key'), 'operation_id': None, 'state': 'unknown',
              'data': {}, 'error': {'code': code}, 'allowed_actions': [], 'warnings': [], 'effect_outcome': 'unknown'}
    if isinstance(body, dict) and body:
        result['body'] = body
    return result


def _contradicts(request: dict, status: int, envelope: dict) -> bool:
    """HTTP 状态与回执矛盾，或成功 DTO 不符本请求动作时，回执不可采信。"""
    if envelope['error'] is not None:
        return status < 400 or envelope['state'] != 'failed'
    if status >= 400:
        return True
    if envelope['state'] not in ('completed', 'accepted', 'failed'):
        return False  # pending/blocked/unknown 是未完成回执，原样保留但不算成功。
    if _CONFIRMED_STATES.get(status) != envelope['state']:
        return True
    try:
        _action_spec(request['action']).validate_result(envelope['data'], accepted=status == 202)
    except HostAPIError:
        return True
    return False


def _receipt(request: dict, status: int, body: object) -> dict:
    """消费冻结回执封套；本地 ok 只表示得到 200/completed 或 202/accepted 且 error=null。"""
    try:
        envelope = validate_object(body, _envelope_schema(request.get('action')))
    except HostAPIError:
        envelope = None
    if envelope is None or _contradicts(request, status, envelope):
        # 既不能升级为成功，也不能断言未执行。
        return _unconfirmed(request, status, 'BUSINESS_RESPONSE_INVALID', body)
    # 回执字段（含权威 request_id/operation_id/effect_outcome/warnings）原样保留，只追加本地投影字段。
    ok = envelope['error'] is None and envelope['state'] in _CONFIRMED_STATES.values()
    return {**envelope, 'ok': ok, 'http_status': status, 'idempotency_key': request.get('idempotency_key')}


def _space_use_result(detail: dict) -> dict:
    """V02-02：把 `/space use` 的只读详情投影成「明确未切换」，绝不返回切换成功。

    宿主只有按任务冻结的业务上下文，没有可以复用的会话当前空间写入动作；因此不新建客户端
    本地选择（那会与真实派发不一致），而是保留详情、把 ok 置 False 并给出稳定错误码。
    """
    if not isinstance(detail, dict) or detail.get('ok') is not True:
        return detail  # 详情本身就没读成功：原样上抛，不覆盖真实错误。
    data = detail.get('data') or {}
    space = data.get('space') or {}
    return {**detail, 'ok': False, 'state': 'blocked', 'operation_id': None, 'effect_outcome': 'none',
            'data': {**data, 'space_id': space.get('space_id', ''), 'switch': 'not_performed'},
            'warnings': [*(detail.get('warnings') or []), _SWITCH_UNSUPPORTED_WARNING],
            'error': {'code': _SWITCH_UNSUPPORTED_CODE, 'message': _SWITCH_UNSUPPORTED_WARNING,
                      'retryable': False, 'details': []}}


@dataclass(frozen=True)
class PreparedBusinessCall:
    """投递前冻结原参数与幂等键；未知回执只允许核对或重送同一对象。"""
    payload_json: str

    def payload(self) -> dict:
        return json.loads(self.payload_json)


class BusinessClient:
    def __init__(self, transport):
        self.transport = transport

    def prepare(self, action: str, params: dict, *, idempotency_key: str | None = None,
                expected_revision: int | None = None, authorization_ticket: str | None = None) -> PreparedBusinessCall:
        spec = _action_spec(action)
        request = {'action': action, 'params': params, 'request_id': uuid4().hex}
        if spec.requires_idempotency:
            request['idempotency_key'] = idempotency_key if idempotency_key is not None else uuid4().hex
        elif idempotency_key is not None:
            request['idempotency_key'] = idempotency_key
        if expected_revision is not None:
            request['expected_revision'] = expected_revision
        if authorization_ticket is not None:
            request['authorization_ticket'] = authorization_ticket
        # 同一冻结目录拒绝 owner/path 和缺失 CAS；不是客户端自行授予权限。
        validate_object(request, spec.native_request_schema)
        return PreparedBusinessCall(json.dumps(request, ensure_ascii=False))

    def send(self, prepared: PreparedBusinessCall) -> dict:
        request = prepared.payload()
        try:
            status, body = self.transport('/client/business', request, timeout=10.0)
        except (OSError, TimeoutError):
            status, body = 0, {}
        if not status:
            return _unconfirmed(request, 0, 'BUSINESS_TRANSPORT_UNKNOWN', body)
        return _receipt(request, status, body)

    def call(self, action: str, params: dict) -> dict:
        return self.send(self.prepare(action, params))


def business_client_for_agent(agent: object, *, conversation_id: str = "") -> BusinessClient:
    from .chat_client_context import post_gateway_json

    # 会话作用域只选同一用户/通道下的规范线程（与 /ask 同一 conversation_id），不携带授权。
    return BusinessClient(partial(post_gateway_json, agent, conversation_id=str(conversation_id or "")))


@dataclass
class BusinessCommandSession:
    client: BusinessClient
    workflow_table: object = field(default=None)


_VERB_MAP = {
    '/space': {
        'list': 'spaces.list',
        'show': 'spaces.get',
        'get': 'spaces.get',
        'use': 'spaces.get',  # 只读详情；consume_business_command 会把它投影成「明确未切换」，不报成功。
        'packages': 'spaces.packages.list',
        'rename': 'spaces.update',
        'archive': 'spaces.archive',
        'restore': 'spaces.restore',
        'create': 'spaces.create',
    },
    '/team': {
        'list': 'background_agents.list',
        'show': 'background_agents.get',
        'get': 'background_agents.get',
        'create': 'background_agents.create',
        'pause': 'background_agents.suspend',
        'suspend': 'background_agents.suspend',
        'resume': 'background_agents.resume',
        'retire': 'background_agents.retire',
        'restore': 'background_agents.restore',
        'model': 'background_agents.model.set',
    },
    '/workflow': {
        'list': 'workflows.list',
        'show': 'workflows.get',
        'get': 'workflows.get',
    },
}


# LLM: 仅消费显式命令；JSON 与自然入参传业务参数，校验失败不降级为普通正文；
# 流程列表与详情附加展示列及多行渲染，不改原 DTO 结构。
# 函数用途: 消费 /space、/team 与 /workflow 斜杠命令并提交统一 BusinessClient。
def consume_business_command(raw: str, *, session: BusinessCommandSession) -> dict | None:
    head, _, tail = raw.strip().partition(' ')
    prefixes = {'/space': 'spaces', '/team': 'background_agents', '/workflow': 'workflows'}
    if head not in prefixes:
        return None
    verb, _, arguments = tail.strip().partition(' ')
    verb_lower = (verb or 'list').lower()
    action = _VERB_MAP.get(head, {}).get(verb_lower)
    if action is None:
        action = prefixes[head] + '.' + verb_lower
    if head == '/space' and verb_lower == 'use' and not arguments.strip():
        # V02-02：use 没有写入端口；缺参数时明确拒绝，不发送必被 schema 拒绝的空请求。
        raise ValueError('用法：/space use <space_id>')
    spec = business_action_spec(action)
    options: dict[str, object] = {}
    if not arguments:
        params = {}
    elif arguments.lstrip().startswith('{'):
        value = json.loads(arguments)
        if 'params' in value:
            options = {key: value[key] for key in value if key != 'params'}
            if set(options) - {'expected_revision', 'idempotency_key', 'authorization_ticket'}:
                raise ValueError('未知命令选项')
            params = value['params']
        else:
            params = value
    else:
        arg_text = arguments.strip()
        if action == 'spaces.create':
            params = {'name': arg_text}
        elif action == 'spaces.update':
            parts = arg_text.split(maxsplit=1)
            params = {'space_id': parts[0], 'name': parts[1] if len(parts) > 1 else parts[0]}
        elif action in ('spaces.get', 'spaces.archive', 'spaces.restore', 'spaces.packages.list'):
            params = {'space_id': arg_text}
        elif action in ('background_agents.get', 'background_agents.suspend', 'background_agents.resume', 'background_agents.restore'):
            params = {'agent_id': arg_text}
        elif action == 'background_agents.create':
            parts = arg_text.split(maxsplit=2)
            params = {'space_id': parts[0], 'name': parts[1]} if len(parts) >= 2 else {'name': parts[0]}
        elif action == 'background_agents.retire':
            parts = arg_text.split()
            disp = parts[1] if len(parts) > 1 else 'drain'
            if disp == 'cancel':
                disp = 'cancel_tasks'
            params = {'agent_id': parts[0], 'disposition': disp}
        elif action == 'background_agents.model.set':
            parts = arg_text.split()
            params = {'agent_id': parts[0], 'model_profile_id': parts[1] if len(parts) > 1 else ''}
        elif action == 'workflows.get':
            identifier = arg_text
            if identifier.isdecimal():
                if session.workflow_table is None:
                    raise ValueError('请先列出流程再使用编号')
                identifier = session.workflow_table.resolve(identifier)
            params = {'workflow_ref': identifier}
        else:
            keys = spec.params_schema.get('required', [])
            if len(keys) != 1 or not keys[0].endswith(('_id', '_ref')):
                raise ValueError('参数请使用 JSON 对象；修改须携带原版本与审批票据')
            identifier = arg_text
            if head == '/workflow' and identifier.isdecimal():
                if session.workflow_table is None:
                    raise ValueError('请先列出流程再使用编号')
                identifier = session.workflow_table.resolve(identifier)
            params = {keys[0]: identifier}

    if not arguments.lstrip().startswith('{') and 'expected_revision' in spec.native_request_schema.get('required', ()) and 'expected_revision' not in options:
        if action.startswith('spaces.'):
            cur = session.client.call('spaces.get', {'space_id': params['space_id']})
            if not cur.get('ok'):
                return cur
            options['expected_revision'] = cur['data']['space']['revision']
        elif action == 'background_agents.model.set':
            cur = session.client.call('background_agents.model.get', {'agent_id': params['agent_id']})
            if not cur.get('ok'):
                return cur
            options['expected_revision'] = cur['data']['selection_revision']
        elif action.startswith('background_agents.'):
            cur = session.client.call('background_agents.get', {'agent_id': params['agent_id']})
            if not cur.get('ok'):
                return cur
            agent_info = cur['data']['agent']
            rev = agent_info.get('identity_revision')
            if rev is None:
                rev = agent_info.get('revision')
            options['expected_revision'] = rev

    result = session.client.send(session.client.prepare(action, params, **options))
    if head == '/space' and verb_lower == 'use':
        # V02-02：use 只拿到详情，没有改变任何会话状态；不得按 200/completed 报告切换成功。
        return _space_use_result(result)
    if action == 'workflows.list' and result.get('ok') is True:
        from .workflow_commands import WorkflowTable

        session.workflow_table = session.workflow_table or WorkflowTable()
        snapshot_id = result.get('data', {}).get('list_snapshot_id')
        items = result.get('data', {}).get('items', [])
        rows = session.workflow_table.update(items, list_snapshot_id=snapshot_id)
        result = {**result, 'display_columns': ['名称', '关联空间'], 'display_rows': rows}
    elif action == 'workflows.get' and result.get('ok') is True:
        from .workflow_commands import format_workflow_detail

        data = result.get('data', {})
        wf = data.get('workflow', {})
        detail = data.get('detail', {})
        run_refs = data.get('run_refs', [])
        draft_status = 'draft' if not wf.get('enabled', True) else wf.get('lifecycle', 'active')
        result = {
            **result,
            'workflow_detail': {
                'purpose': detail.get('summary', '') or wf.get('name', ''),
                'io_contract': detail.get('io_contract'),
                'version': wf.get('definition_revision', 1),
                'steps': detail.get('steps', []),
                'run_refs': run_refs,
                'draft_status': draft_status,
            },
            'rendered': format_workflow_detail(data),
        }
    return result


# LLM: 经由统一 BusinessClient 消费 CLI 命令，支持显式传入的 business_client，
# 或在宿主环境自动装配轻量 Gateway 客户端；支持 business_args 与 action/params 两种调用方式。
# 函数用途: 执行 CLI 业务命令并输出 JSON 回执。
def run_business_cli(args, *, command: str) -> int:
    api = getattr(args, 'business_client', None)
    if api is None:
        try:
            from .chat_client_context import make_gateway_chat_client
            client = make_gateway_chat_client(args)
            api = business_client_for_agent(client)
        except Exception:
            api = None
    if api is None:
        print('业务入口未连接；需由命令宿主提供已认证 business_client。')
        return 2

    session = getattr(args, 'business_session', None) or BusinessCommandSession(api)
    tokens = getattr(args, 'business_args', None)
    if tokens:
        raw_cmd = f'/{command} ' + ' '.join(tokens)
    else:
        raw_params = getattr(args, 'params', '{}')
        params_text = raw_params if isinstance(raw_params, str) else json.dumps(raw_params, ensure_ascii=False)
        raw_cmd = f'/{command} {getattr(args, "action", "list")} {params_text}'

    result = consume_business_command(raw_cmd, session=session)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result and result.get('ok') is True else 1


# LLM: 执行 space 子命令，将 CLI 参数转送给统一业务客户端。
# 函数用途: 处理 my-agent space 命令。
def cmd_space(args) -> int:
    return run_business_cli(args, command='space')
