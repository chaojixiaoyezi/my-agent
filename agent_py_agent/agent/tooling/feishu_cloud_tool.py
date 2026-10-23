# LLM: 云入口属于原 ToolRegistry；准确 owner 许可或实时本人/群证明、网络上界和原操作账共同裁决，不暴露应用秘密或任意 HTTP。
# 模块用途: 让主子代理使用本人或群已共享的飞书资源，宿主定位 wiki、核验权限并返回明确错误。
from __future__ import annotations

import json
from urllib.parse import quote, urlencode

from ..adapter.feishu_cloud import FeishuCloudError, request_cloud_api
from ..capability.runtime_config_reload import (
    capability_config_for_agent,
    deployment_capability_config,
)
from ..user_space.feishu_cloud_authorization import authorize_cloud_resource, resolve_cloud_resource
from ..user_space.feishu_sheet_authorization import authorize_sheet_write
from ..user_space.feishu_sheet_verification import verify_sheet_write
from ..user_space.owner_policy import resolve_effective_owner_policy
from ..user_space.resource_grants import (
    normalized_resource_grant,
    read_resource_grants,
    resource_operation_allowed,
)
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

_TYPES = {'sheet_meta': 'sheet', 'sheet_read': 'sheet', 'sheet_append': 'sheet', 'sheet_update': 'sheet',
          'document_read': 'docx', 'document_append': 'docx', 'wiki_resolve': 'wiki'}
_READ_ACTIONS = {'sheet_meta', 'sheet_read', 'document_read', 'wiki_resolve'}


# LLM: schema 与 API 计划共用准确动作；数据只作为供应商载荷，不能成为 owner、凭据、URL 或授权配置。
# 函数用途: 构造模型可见的准确资源接口，不开放配置或请求头参数。
def _model_spec() -> ToolModelSpec:
    return ToolModelSpec(name='feishu_cloud', description=(
        '使用宿主凭据读写本人或当前群已共享的飞书云资源，或管理员准确授权的资源。'
        'sheet_meta 查询电子表格的所有工作表标签页（返回 sheet_id、title、行列数，未知 sheet_id 时先调此动作，勿盲猜 Sheet1）；'
        'sheet_read 读范围，sheet_append 追加行，sheet_update 更新范围（多行数据请合并为一次二维 values 批量写入，勿逐行调工具）；'
        'document_read 分页读文档块，document_append 写入文档块；wiki_resolve 返回对象 resource_id/resource_type。'
        '分页结果 next_cursor 对应下一次 page_token。'
        '用 resource_token 传准确编号；知识库链接的节点编号可设 resource_type=wiki，由宿主定位并核验实际对象权限，无需重复人工授权。'
        '当前群必须是文档协作者，群成员个人权限不等于群共享。凭据由宿主托管，勿索取 app_secret。'
        '表格更新会核验与正规 A1 范围相交的保护；追加的目的行由飞书决定，存在保护时须本人拥有所有保护范围编辑权。'
        '群不能借个人保护范围编辑权；未知保护信息会拒绝代写。'
        '表格写入回执区分已接受与读回 verified/mismatch/unverifiable/disabled；未核验不代表未写入，不能直接重复追加。'),
        input_schema={'type': 'object', 'properties': {
            'action': {'type': 'string', 'enum': list(_TYPES)}, 'resource_token': {'type': 'string'},
            'identity': {'type': 'string', 'enum': ['bot', 'user'], 'description': '默认bot使用应用身份；user须当前私人用户已通过feishu_auth本人授权。群资源使用bot。'},
            'resource_type': {'type': 'string', 'description': '可选：wiki 表示传入知识库节点编号；否则与动作对应的 sheet/docx 一致。'},
            'range': {'type': 'string', 'description': 'sheet_read/sheet_append/sheet_update 必填，格式为 sheet_id!A1:C10（未知 sheet_id 时先调 sheet_meta）。'},
            'values': {'type': 'array', 'items': {'type': 'array', 'items': {}}, 'description': '追加或更新的二维单元格数据（支持多行一次性批量提交）。'},
            'block_id': {'type': 'string', 'description': '文档块父编号，缺省为文档根。'},
            'children': {'type': 'array', 'items': {'type': 'object'}, 'description': 'document_append 必填，飞书文档块对象。'},
            'page_token': {'type': 'string', 'description': 'document_read 分页游标。'},
        }, 'required': ['action', 'resource_token'], 'additionalProperties': False},
        hints=ToolModelHints(category='network', keywords=('飞书', '云文档', '表格', 'wiki'),
            default_deferred=True, deferred_summary='使用宿主凭据读写本人或当前群已共享的飞书表格和云文档'))


# LLM: 参数校验无网络；准确编号阻止路径替换，条件必填阻止默认误写，API 长度限制只守客观载荷上界。
# 函数用途: 在审批与操作 claim 前检查本次云调用的形状。
def _validate_params(params: dict) -> None:
    action = params.get('action')
    if params.get('identity', 'bot') not in {'bot', 'user'}:
        raise ValueError('identity 无效。')
    if action not in _TYPES:
        raise ValueError('action 无效。')
    resource_type = params.get('resource_type', _TYPES[action])
    if resource_type not in {_TYPES[action], 'wiki'} or (action == 'wiki_resolve' and resource_type != 'wiki'):
        raise ValueError('resource_type 与动作不一致。')
    normalized_resource_grant({'provider': 'feishu', 'resource_type': resource_type,
        'resource_token': params.get('resource_token'), 'operations': [action]})
    if action in {'sheet_read', 'sheet_append', 'sheet_update'} and (
            not isinstance(params.get('range'), str) or not params['range'].strip() or len(params['range']) > 512):
        raise ValueError('表格读写动作需要准确 range（若未知 sheet_id 请先调用 action="sheet_meta"）。')
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
    if action == 'sheet_meta':
        return 'GET', '/sheets/v2/spreadsheets/' + token + '/metainfo', None
    if action.startswith('sheet_'):
        path = '/sheets/v2/spreadsheets/' + token + '/values'
        if action == 'sheet_read':
            return 'GET', path + '/' + quote(params['range'], safe=''), None
        body = {'valueRange': {'range': params['range'], 'values': params['values']}}
        return ('POST', path + '_append', body) if action == 'sheet_append' else ('PUT', path, body)
    parent = quote(params.get('block_id') or params['resource_token'], safe='')
    path = f'/docx/v1/documents/{token}/blocks/{parent}/children'
    if action == 'document_append':
        return 'POST', path, {'children': params['children'], 'index': -1}
    return 'GET', path + '?' + urlencode({'page_size': 50, 'page_token': params.get('page_token', '')}), None


# LLM: 工具实例固定 owner，子代理继承原工具与网络上界；CLI/HTTP后端均在同一资源权限门之后。
# 类用途: 核验准确资源许可或实时飞书权限，再通过已配置宿主后端执行云操作。
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

    # LLM: 云能力沿owner配置，CLI边界只读共享部署源；坏配置失败关闭，资源授权仍逐调用校验。
    # 函数用途: 关闭功能时隐藏执行能力，不因管理员模式绕过。
    def availability(self):
        config = capability_config_for_agent(self.agent)
        if config is None or not config.feishu_cloud_tools_enabled:
            return ToolAvailability.unavailable('飞书云资源工具未启用。')
        try:
            deployment = deployment_capability_config(self.agent.home_paths)
        except (OSError, ValueError, TypeError):
            return ToolAvailability.unavailable('飞书后端部署配置无法验证。')
        if deployment.feishu_cli_backend_enabled:
            from ..adapter.feishu_cli import cli_binary
            try:
                cli_binary()
            except FileNotFoundError:
                return ToolAvailability.unavailable('随包飞书 CLI 后端未安装，请更新完整安装包。')
        return ToolAvailability.ready()

    # LLM: 与直接 handler 调用共用纯参数校验；不能在审批前请求云数据。
    # 函数用途: 让缺 range/values/children 的调用先报参数错误。
    def validate_invocation(self, params, context):
        try:
            _validate_params(params)
        except (ValueError, TypeError):
            return ToolHandlerOutcome('feishu_cloud', False, '云资源参数不完整或无效；请核对编号、range 和对应数据。', error_code='TOOL_INVALID_ARGUMENTS')
        return None

    # LLM: 原协调器持有UNKNOWN核对claim；本入口只读原目标，不写表、不重做分析或换operation编号。
    # 函数用途: 让准确更新在目标状态可证明时沿原操作结算，追加位置未知时继续安全受阻。
    def reconcile_operation(self, params, context):
        from .feishu_cloud_recovery import reconcile_cloud_operation
        return reconcile_cloud_operation(self, params, context)

    # LLM: 先核查网络和原策略，再现查准确资源/表格保护权限；写后同身份读回独立报告核验状态，读失败不撤销已接受写入或重放。
    # 函数用途: 让用户按飞书已有分享权限使用云资源，核验保护范围后代写，返回本次许可来源及可读错误。
    def execute(self, params):
        try:
            _validate_params(params)
            if not self.availability().available:
                return _failure('TOOL_UNAVAILABLE', '飞书云资源工具未启用。')
            if params.get('__network_access_mode') == 'disabled' or not resolve_effective_owner_policy(self.agent.home_paths).network_enabled:
                return _failure('NETWORK_DISABLED', '当前用户或父代理未开放工具网络权限。')
            deployment = deployment_capability_config(self.agent.home_paths)
            user_token = ''
            cloud_request = request_cloud_api
            if deployment.feishu_cli_backend_enabled:
                from ..adapter.feishu_cli import request_cli_cloud_api
                cloud_request = request_cli_cloud_api
            if params.get('identity') == 'user':
                if not deployment.feishu_cli_backend_enabled:
                    return _failure('FEISHU_USER_AUTH_REQUIRED', '本人身份需要已配置的受控CLI后端。')
                from functools import partial

                from ..user_space.feishu_oauth import user_access_token
                try:
                    user_token = user_access_token(self.agent)
                    cloud_request = partial(cloud_request, user_token=user_token)
                except (OSError, ValueError, TypeError):
                    return _failure('FEISHU_USER_AUTH_REQUIRED', '本人飞书授权不可用；请在私人助手会话通过feishu_auth授权。')
            read_resource_grants(self.agent.home_paths)
            config = capability_config_for_agent(self.agent)
            collaborator_enabled = bool(getattr(config, 'feishu_cloud_collaborator_authorization_enabled', False))
            resource_type = params.get('resource_type', _TYPES[params['action']])
            if resource_type == 'wiki' and not collaborator_enabled and not resource_operation_allowed(
                    self.agent.home_paths, 'feishu', 'wiki', params['resource_token'], 'wiki_resolve'):
                return _failure('CLOUD_RESOURCE_NOT_AUTHORIZED', '自助资源权限未启用，知识库定位需要准确的管理员许可。')
            adapter = self.agent.channel_registry.adapter_for('feishu')
            if adapter is None:
                return _failure('FEISHU_CREDENTIAL_UNAVAILABLE', '宿主未配置飞书应用凭据，请管理员配置；无需向助手发送 app_secret。')
            if user_token:
                from ..adapter.feishu_cli import UserCloudIdentity
                from ..conversation.tool_approval_scope import tool_approval_scope
                from ..user_space.feishu_oauth import oauth_actor
                current = getattr(self.agent, '_current_run_params', None)
                scope = tool_approval_scope(self.agent, str(current.run_id or current.task_id)) if current is not None else None
                actor = oauth_actor(self.agent, scope)
                adapter = UserCloudIdentity(adapter.app_id, adapter.app_secret, user_token, actor)
            resource = resolve_cloud_resource(adapter, resource_type, params['resource_token'], params['action'])
            if params['action'] != 'wiki_resolve' and resource.resource_type != _TYPES[params['action']]:
                return _failure('CLOUD_RESOURCE_TYPE_MISMATCH', '知识库实际对象与所选操作类型不一致，请选择相应云资源动作。')
            authorization = authorize_cloud_resource(self.agent, adapter, resource, params['action'],
                                                     collaborator_enabled=collaborator_enabled)
            authorize_sheet_write(adapter, resource, params, authorization)
            result = resource.wiki_data if params['action'] == 'wiki_resolve' else cloud_request(
                adapter, *_request_plan({**params, 'resource_token': resource.resource_token}))
            if params['action'] == 'sheet_meta' and isinstance(result, dict) and isinstance(result.get('sheets'), list):
                result = {**result, 'sheets_summary': [
                    {'sheet_id': row.get('sheetId'), 'title': row.get('title', ''), 'index': row.get('index'),
                     'row_count': row.get('rowCount'), 'column_count': row.get('columnCount'), 'hidden': row.get('hidden', False)}
                    for row in result['sheets'] if isinstance(row, dict) and 'blockInfo' not in row and row.get('sheetId')
                ]}
            public_authorization = {key: value for key, value in authorization.items() if key != 'principal_id'}
            payload = {'action': params['action'], 'data': result, 'resource_authorization': public_authorization}
            if params['action'] in {'sheet_append', 'sheet_update'}:
                payload['write_verification'] = verify_sheet_write(adapter, cloud_request,
                    {**params, 'resource_token': resource.resource_token}, result,
                    enabled=bool(getattr(config, 'feishu_cloud_verify_sheet_writes_enabled', True)))
            return ToolHandlerOutcome('feishu_cloud', True, json.dumps(payload, ensure_ascii=False))
        except FeishuCloudError as exc:
            return _failure(exc.code, str(exc), unknown=exc.unknown, facts=exc.public_facts())
        except (ValueError, TypeError, OSError):
            return _failure('CLOUD_RESOURCE_POLICY_INVALID', '云资源参数或用户授权策略不可验证，未继续调用；请管理员核查。')


# LLM: 数值错误事实来自宿主异常，不含正文或秘密；unknown 保留供应商不确定性，不按展示文字重试。
# 函数用途: 返回可读错误与结构化原因，交给原操作账保存真实副作用状态。
def _failure(code: str, message: str, *, unknown: bool = False, facts: dict | None = None):
    details = dict(facts or {})
    output = json.dumps({'error': message, **details}, ensure_ascii=False) if details else message
    return ToolHandlerOutcome('feishu_cloud', False, output, error_code=code,
                              effect_outcome='unknown' if unknown else 'not_started', result_envelope={'cloud_error': details} if details else {})
