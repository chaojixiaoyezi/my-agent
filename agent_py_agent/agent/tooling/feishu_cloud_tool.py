# LLM: 云文档入口属于原 ToolRegistry；owner 资源名单、网络上界和原操作账共同裁决，模型不能拿到应用秘密或任意 HTTP 通道。
# 模块用途: 为主子代理提供已授权飞书表格读写、文档块读写和 wiki 定位，不复制应用凭据。
from __future__ import annotations

import json
from urllib.parse import quote, urlencode

from ..adapter.feishu_cloud import FeishuCloudError, request_cloud_api
from ..capability.runtime_config_reload import capability_config_for_agent
from ..user_space.owner_policy import resolve_effective_owner_policy
from ..user_space.resource_grants import normalized_resource_grant, resource_operation_allowed
from .models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    TimeoutPolicy,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolInputPolicy,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
    TrustedParameterBinding,
)

_TYPES = {'sheet_read': 'sheet', 'sheet_append': 'sheet', 'sheet_update': 'sheet',
          'document_read': 'docx', 'document_append': 'docx', 'wiki_resolve': 'wiki'}
_READ_ACTIONS = {'sheet_read', 'document_read', 'wiki_resolve'}


# LLM: schema 与 API 计划共用准确动作；数据只作为供应商载荷，不能成为 owner、凭据、URL 或授权配置。
# 函数用途: 构造模型可见的准确资源接口，不开放配置或请求头参数。
def _model_spec() -> ToolModelSpec:
    return ToolModelSpec(name='feishu_cloud', description=(
        '读写管理员给当前用户授权的飞书云资源。sheet_read 读范围，sheet_append 追加行，sheet_update 更新范围；'
        'document_read 分页读文档块，document_append 写入文档块；wiki_resolve 返回对象 resource_id/resource_type。'
        '分页结果 next_cursor 对应下一次 page_token。'
        '用 resource_token 传准确编号；wiki 解析后的 sheet/docx 仍须单独授权。凭据由宿主托管，勿索取 app_secret。'
        '写后用读操作核对；未知写入错误不能直接重试追加。'),
        input_schema={'type': 'object', 'properties': {
            'action': {'type': 'string', 'enum': list(_TYPES)}, 'resource_token': {'type': 'string'},
            'range': {'type': 'string', 'description': '表格动作必填，sheet_id!A1:C10 或追加范围。'},
            'values': {'type': 'array', 'items': {'type': 'array', 'items': {}}, 'description': '追加或更新的二维单元格数据。'},
            'block_id': {'type': 'string', 'description': '文档块父编号，缺省为文档根。'},
            'children': {'type': 'array', 'items': {'type': 'object'}, 'description': 'document_append 必填，飞书文档块对象。'},
            'page_token': {'type': 'string', 'description': 'document_read 分页游标。'},
        }, 'required': ['action', 'resource_token'], 'additionalProperties': False},
        hints=ToolModelHints(category='network', keywords=('飞书', '云文档', '表格', 'wiki'),
            default_deferred=True, deferred_summary='使用宿主凭据读写当前用户已授权的飞书表格和云文档'))


# LLM: 参数校验无网络；准确编号阻止路径替换，条件必填阻止默认误写，API 长度限制只守客观载荷上界。
# 函数用途: 在审批与操作 claim 前检查本次云调用的形状。
def _validate_params(params: dict) -> None:
    action = params.get('action')
    if action not in _TYPES:
        raise ValueError('action 无效。')
    normalized_resource_grant({'provider': 'feishu', 'resource_type': _TYPES[action],
        'resource_token': params.get('resource_token'), 'operations': [action]})
    if action.startswith('sheet_') and (not isinstance(params.get('range'), str) or not params['range'].strip() or len(params['range']) > 512):
        raise ValueError('表格动作需要准确 range。')
    if action in {'sheet_append', 'sheet_update'}:
        rows = params.get('values')
        if not isinstance(rows, list) or not rows or len(rows) > 5000 or any(not isinstance(row, list) or not row or len(row) > 100 for row in rows):
            raise ValueError('values 需为非空二维数组，单次最多 5000 行、100 列。')
    if action == 'document_append' and (not isinstance(params.get('children'), list) or not params['children']
            or len(params['children']) > 50 or any(not isinstance(x, dict) for x in params['children'])):
        raise ValueError('children 需为非空文档块数组，单次最多 50 块。')
    if len(json.dumps(params, ensure_ascii=False, allow_nan=False).encode()) > 2 * 1024 * 1024:
        raise ValueError('本次请求过大，请分批处理。')


# LLM: API 目标由动作和准确对象编号生成，quote 会编码范围和块编号；不从模型 URL 拼宿主凭据请求。
# 函数用途: 将合法调用转换成固定官方 API 路径与 JSON 载荷。
def _request_plan(params: dict) -> tuple[str, str, dict | None]:
    action, token = params['action'], quote(params['resource_token'], safe='')
    if action.startswith('sheet_'):
        path = '/sheets/v2/spreadsheets/' + token + '/values'
        if action == 'sheet_read':
            return 'GET', path + '/' + quote(params['range'], safe=''), None
        body = {'valueRange': {'range': params['range'], 'values': params['values']}}
        return ('POST', path + '_append', body) if action == 'sheet_append' else ('PUT', path, body)
    if action == 'wiki_resolve':
        return 'GET', '/wiki/v2/spaces/get_node?' + urlencode({'token': params['resource_token']}), None
    parent = quote(params.get('block_id') or params['resource_token'], safe='')
    path = f'/docx/v1/documents/{token}/blocks/{parent}/children'
    if action == 'document_append':
        return 'POST', path, {'children': params['children'], 'index': -1}
    return 'GET', path + '?' + urlencode({'page_size': 50, 'page_token': params.get('page_token', '')}), None


# LLM: 同一工具供所有 owner 注册；宿主实例固定 owner，子代理工具快照仍由原派工裁剪。Full Access 不豁免资源名单。
# 类用途: 在用户资源授权通过后复用飞书适配器进行一次云操作。
class FeishuCloudTool(BaseTool):
    model_spec = _model_spec()
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy('mutating', by_parameter=(('action', tuple((x, 'read_only') for x in sorted(_READ_ACTIONS))),)),
        idempotency_policy=IdempotencyPolicy('operation'), timeout_policy=TimeoutPolicy(seconds=90),
        input_policy=ToolInputPolicy(internal_parameters=('__network_access_mode',), trusted_parameter_bindings=(
            ('__network_access_mode', TrustedParameterBinding(source_refs=('write_boundary.network_access_mode',), authority='host_authoritative')),)),
    )

    # LLM: 注册不解析密钥、不发 HTTP、不创建用户资源授权。
    # 函数用途: 保存当前真实用户的宿主 Agent。
    def __init__(self, agent):
        self.agent = agent

    # LLM: 开关读原 capability 配置，未知配置失败关闭；资源授权仍逐调用校验。
    # 函数用途: 关闭功能时隐藏执行能力，不因管理员模式绕过。
    def availability(self):
        config = capability_config_for_agent(self.agent)
        if config is None or not config.feishu_cloud_tools_enabled:
            return ToolAvailability.unavailable('飞书云资源工具未启用。')
        return ToolAvailability.ready()

    # LLM: 与直接 handler 调用共用纯参数校验；不能在审批前请求云数据。
    # 函数用途: 让缺 range/values/children 的调用先报参数错误。
    def validate_invocation(self, params, context):
        try:
            _validate_params(params)
        except (ValueError, TypeError):
            return ToolHandlerOutcome('feishu_cloud', False, '云资源参数不完整或无效；请核对编号、range 和对应数据。', error_code='TOOL_INVALID_ARGUMENTS')
        return None

    # LLM: 请求前重读当前授权及网络上界；先拒绝再解析凭据。原执行器管理确认、并发、操作 claim 和未知写入，不能新造重试账。
    # 函数用途: 调用已授权云接口，成功返回数据，拒绝给出明确中文原因。
    def execute(self, params):
        try:
            _validate_params(params)
            if not self.availability().available:
                return _failure('TOOL_UNAVAILABLE', '飞书云资源工具未启用。')
            if params.get('__network_access_mode') == 'disabled' or not resolve_effective_owner_policy(self.agent.home_paths).network_enabled:
                return _failure('NETWORK_DISABLED', '当前用户或父代理未开放工具网络权限。')
            if not resource_operation_allowed(self.agent.home_paths, 'feishu', _TYPES[params['action']], params['resource_token'], params['action']):
                return _failure('CLOUD_RESOURCE_NOT_AUTHORIZED', '当前用户没有这份资源的此项操作授权，请管理员登记准确资源编号和操作；未发送云请求。')
            adapter = self.agent.channel_registry.adapter_for('feishu')
            if adapter is None:
                return _failure('FEISHU_CREDENTIAL_UNAVAILABLE', '宿主未配置飞书应用凭据，请管理员配置；无需向助手发送 app_secret。')
            result = request_cloud_api(adapter, *_request_plan(params))
            return ToolHandlerOutcome('feishu_cloud', True, json.dumps({'action': params['action'], 'data': result}, ensure_ascii=False))
        except FeishuCloudError as exc:
            return _failure(exc.code, str(exc), unknown=exc.unknown)
        except (ValueError, TypeError, OSError):
            return _failure('CLOUD_RESOURCE_POLICY_INVALID', '云资源参数或用户授权策略不可验证，未继续调用；请管理员核查。')


# LLM: unknown 保留真实供应商不确定性，not_started 只用于未调用或明确拒绝；不按文字猜副作用。
# 函数用途: 生成可读错误并交给原操作账处理。
def _failure(code: str, message: str, *, unknown: bool = False):
    return ToolHandlerOutcome('feishu_cloud', False, message, error_code=code, effect_outcome='unknown' if unknown else 'not_started')
