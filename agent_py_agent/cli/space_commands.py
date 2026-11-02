"""空间/团队/流程的薄命令消费端：宿主认证 HTTP 是唯一业务执行入口。"""
from __future__ import annotations

import json
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


@lru_cache(maxsize=1)
def _response_envelope_schema() -> dict:
    return standalone_schema(business_schema_bundle()['response_envelope_schema'])


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
        business_action_spec(request['action']).validate_result(envelope['data'], accepted=status == 202)
    except HostAPIError:
        return True
    return False


def _receipt(request: dict, status: int, body: object) -> dict:
    """消费冻结回执封套；本地 ok 只表示得到 200/completed 或 202/accepted 且 error=null。"""
    try:
        envelope = validate_object(body, _response_envelope_schema())
    except HostAPIError:
        envelope = None
    if envelope is None or _contradicts(request, status, envelope):
        # 既不能升级为成功，也不能断言未执行。
        return _unconfirmed(request, status, 'BUSINESS_RESPONSE_INVALID', body)
    # 回执字段（含权威 request_id/operation_id/effect_outcome/warnings）原样保留，只追加本地投影字段。
    ok = envelope['error'] is None and envelope['state'] in _CONFIRMED_STATES.values()
    return {**envelope, 'ok': ok, 'http_status': status, 'idempotency_key': request.get('idempotency_key')}


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
        spec = business_action_spec(action)
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


def business_client_for_agent(agent: object) -> BusinessClient:
    from .chat_client_context import post_gateway_json

    return BusinessClient(partial(post_gateway_json, agent))


@dataclass
class BusinessCommandSession:
    client: BusinessClient
    workflow_table: object = field(default=None)


def consume_business_command(raw: str, *, session: BusinessCommandSession) -> dict | None:
    """仅消费显式命令；JSON 传业务参数，/agents 保持原 AGENTS 入口。"""
    head, _, tail = raw.strip().partition(' ')
    prefixes = {'/space': 'spaces', '/team': 'background_agents', '/workflow': 'workflows'}
    if head not in prefixes:
        return None
    verb, _, arguments = tail.strip().partition(' ')
    action = prefixes[head] + '.' + (verb or 'list')
    spec = business_action_spec(action)
    options = {}
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
        keys = spec.params_schema.get('required', [])
        if len(keys) != 1 or not keys[0].endswith(('_id', '_ref')):
            raise ValueError('参数请使用 JSON 对象；修改须携带原版本与审批票据')
        identifier = arguments.strip()
        if head == '/workflow' and identifier.isdecimal():
            if session.workflow_table is None:
                raise ValueError('请先列出流程再使用编号')
            identifier = session.workflow_table.resolve(identifier)
        params = {keys[0]: identifier}
    result = session.client.send(session.client.prepare(action, params, **options))
    if action == 'workflows.list' and result.get('ok') is True:
        from .workflow_commands import WorkflowTable

        session.workflow_table = session.workflow_table or WorkflowTable()
        result = {**result, 'display_columns': ['名称', '关联空间'],
                  'display_rows': session.workflow_table.update(result.get('data', {}).get('items', []))}
    return result


def run_business_cli(args, *, command: str) -> int:
    """W13 parser 注入 business_client；不构造第二个 Agent 或领域仓库。"""
    api = getattr(args, 'business_client', None)
    if api is None:
        print('业务入口未连接；需由命令宿主提供已认证 business_client。')
        return 2
    raw_params = getattr(args, 'params', '{}')
    params_text = raw_params if isinstance(raw_params, str) else json.dumps(raw_params, ensure_ascii=False)
    result = consume_business_command(f'/{command} {getattr(args, "action", "list")} {params_text}',
                                      session=BusinessCommandSession(api))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result and result.get('ok') is True else 1


def cmd_space(args) -> int:
    return run_business_cli(args, command='space')
