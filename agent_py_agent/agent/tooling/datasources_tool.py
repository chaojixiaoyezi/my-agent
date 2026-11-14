"""同Owner的数据源管理及显式确认的清单变更；不自动登记或移除来源。"""
# LLM: Owner/workspace只由宿主授予；凭据双参数经Store隔离，保存/探测/迁移仍走审批，公开结果不含秘密。
# 模块用途: 提供数据源结构化工具；参数schema共用纯构造器，结果保留专属安全输出投影。
from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from ..assets.datasource_store import (
    DatasourceSpecValidationError,
    DatasourceStore,
    prepare_datasource_spec,
)
from ..assets.source_manifest import (
    ManifestRevisionConflict,
    load_manifest,
    prune_sources,
    save_manifest,
)
from .input_schema import validate_tool_input
from .models import BaseTool, ToolHandlerOutcome
from .scripts_tool import _policy
from .tool_spec import _spec


# LLM: 结果已由DatasourceStore安全投影，不套会误删has_secret的资产描述符codec；失败不回显输入或驱动异常正文。
# 函数用途: 构造结构化数据源工具回执。
def _result(data, ok=True, code='', *, effect='', next_action=''):
    return ToolHandlerOutcome('datasources', ok, json.dumps(data, ensure_ascii=False, allow_nan=False),
                              error_code=code, effect_outcome=effect, next_action=next_action)


def _datasource_model_spec():
    spec = _spec('datasources', '管理数据源代号、隔离凭据、SQLite schema与迁移；输出不包含密码，外库导出不代表远端已导入。',
        ['list', 'get', 'save', 'test', 'inspect_schema', 'migrate',
         'manifest_get', 'manifest_register', 'manifest_prune'], {
            'datasource_id': {
                'type': 'string',
                'description': '数据源稳定代号（get/test/inspect_schema/migrate 必填，仅含字母、数字、下划线、点或短横线）。',
            },
            'workspace_root': {
                'type': 'string',
                'description': '可选目标工作区根目录路径；传入时必须与宿主当前授权的工作区完全一致。',
            },
            'spec_payload': {
                'type': 'object',
                'description': 'save 必填 datasource_id、kind、title，均为非空字符串；扩展连接参数放 options 对象。例如 {"datasource_id":"orders","kind":"sqlite","title":"订单"}',
            },
            'secret_payload': {
                'type': 'object',
                'description': 'save 可选隔离凭据秘密对象（如密码、token）；写入加密凭据库，公开摘要不回显明文。',
            },
            'credential_values': {
                'type': 'object',
                'description': 'save 可选命名凭据键值对象（与 secret_payload 一并经凭据库隔离保存并做冲突核验）。',
            },
            'scope': {
                'type': 'string',
                'enum': ['owner', 'workspace'],
                'description': '数据源保存作用域：owner（当前用户全局，默认）或 workspace（当前授权工作区）。',
            },
            'expected_revision': {
                'type': ['integer', 'null'],
                'minimum': 0,
                'description': '乐观并发控制期望整数 revision；更新已有数据源或 manifest_prune 时传当前精确整数版本。',
            },
            'source_system': {
                'type': 'string',
                'description': '来源清单所属外部系统名称（manifest_get/manifest_register/manifest_prune 必填）。',
            },
            'sources': {
                'type': 'array',
                'maxItems': 256,
                'description': 'manifest_register 登记的来源声明列表（最多 256 条）。',
                'items': {
                    'type': 'object',
                    'required': ['source_id'],
                    'additionalProperties': False,
                    'properties': {
                        'source_id': {'type': 'string', 'description': '来源条目唯一稳定代号。'},
                        'core': {'type': 'boolean', 'description': '是否为核心保留来源。'},
                        'credential_ref': {'type': 'string', 'description': '绑定的隔离凭据引用标识（不含明文密钥）。'},
                        'endpoint_ref': {'type': 'string', 'description': '绑定的服务端点引用标识。'},
                    },
                },
            },
            'source_ids': {
                'type': 'array',
                'maxItems': 256,
                'description': 'manifest_prune 显式裁剪移除的 source_id 列表。',
                'items': {'type': 'string'},
            },
            'confirmed': {
                'type': 'boolean',
                'description': '清单写操作（manifest_register/manifest_prune）显式确认标志，必须为 true 才允许提交。',
            },
            'target_spec': {
                'type': 'object',
                'description': 'migrate 目标数据源连接规格对象（结构同 spec_payload）。',
            },
            'mode': {
                'type': 'string',
                'enum': ['prepare_and_validate', 'switch'],
                'description': 'migrate 模式：prepare_and_validate 仅导出预检不切换（默认），switch 在验证通过后切换规格。',
            },
            'auto_switch': {
                'type': 'boolean',
                'description': 'migrate 在预检验证通过后是否自动切换激活目标规格（默认 false）。',
            }})
    schema = deepcopy(spec.input_schema)
    schema['properties']['action']['description'] = (
        '数据源动作：list/get/inspect_schema/manifest_get 只读查询；'
        'save 保存规格与隔离凭据，test 探测连接，migrate 预检或切换迁移，manifest_register/manifest_prune 维护来源清单。'
    )
    hints = replace(
        spec.hints,
        use_cases=(
            '登记、查询、连通性探测当前 owner 或工作区的数据源规格与隔离凭据（list/get/save/test）',
            '只读检查 SQLite 数据源表结构或预检数据源迁移（inspect_schema/migrate）',
            '在用户显式确认下读取、登记或按 expected_revision 裁剪外部系统来源清单（manifest_*）',
        ),
        avoid_when=(
            '执行固化数据提取脚本或做脚本候选源评估时用 scripts（本工具只管数据源连接规格与来源清单）',
            '向用户私密收集外部 API Key/Token 凭据引用时用 request_credentials',
        ),
        examples=(
            '{"action":"list"}',
            '{"action":"save","spec_payload":{"datasource_id":"orders","kind":"sqlite","title":"订单库"}}',
            '{"action":"inspect_schema","datasource_id":"orders"}',
        ),
    )
    return replace(spec, input_schema=schema, hints=hints, schema_hash='')


# LLM: 所有权限绑定到同一可信宿主；共用纯schema不改变安全ID、连接规格或专属结果投影。
# 类用途: 向Agent暴露数据源管理操作。
class DatasourcesTool(BaseTool):
    name = 'datasources'
    model_spec = _datasource_model_spec()
    runtime_policy = _policy('datasources', ('list', 'get', 'inspect_schema', 'manifest_get'),
                            ('save', 'test', 'migrate', 'manifest_register', 'manifest_prune'))

    # LLM: 构造不读写数据源，workspace由宿主静态或动态提供；不同Owner Store不接受注入。
    # 函数用途: 绑定当前Owner和可选当前工作区提供者。
    def __init__(self, owner_home, *, workspace_root=None, workspace_root_provider=None, store=None):
        self.owner_home = Path(owner_home).absolute()
        self.workspace_root = Path(workspace_root).absolute() if workspace_root is not None else None
        self.workspace_root_provider = workspace_root_provider
        if store is not None and Path(store.owner_home).absolute() != self.owner_home:
            raise ValueError('DATASOURCE_OWNER_MISMATCH')
        self.store = store or DatasourceStore(self.owner_home)

    # LLM: 优先读取可信宿主的实时工作区提供者，未配置时回退构造期静态目录，不接受模型覆写。
    # 函数用途: 取得当前受宿主授权的有效工作区根目录。
    def _effective_workspace_root(self):
        root = self.workspace_root_provider() if callable(self.workspace_root_provider) else self.workspace_root
        return Path(root).absolute() if root is not None else None

    # LLM: Schema和条件必填在claim前校验，不创建文件或解析认证。
    # 函数用途: 返回参数预检错误，合法调用返回None。
    def validate_invocation(self, params, context):
        if not validate_tool_input(params, self.model_spec.input_schema).ok:
            return _result(
                {'error': '参数不符合数据源Schema'},
                False,
                'TOOL_INVALID_ARGUMENTS',
                effect='not_started',
                next_action='按数据源 Schema 修正参数类型并删除未声明字段后再试，不要把对象或数组传成字符串。',
            )
        action = params['action']
        required = [] if action == 'list' else ['spec_payload'] if action == 'save' else ['datasource_id']
        if action in ('manifest_get', 'manifest_register', 'manifest_prune'):
            required = ['source_system']
            if action == 'manifest_register':
                required.append('sources')
            if action == 'manifest_prune':
                required += ['source_ids', 'expected_revision']
            if (action != 'manifest_get' and params.get('confirmed') is not True
                    or action == 'manifest_prune' and type(params.get('expected_revision')) is not int):
                return _result(
                    {'error': '清单变更需要明确确认，移除还需要精确revision'},
                    False,
                    'TOOL_INVALID_ARGUMENTS',
                    effect='not_started',
                    next_action='先用 manifest_get 读取当前 revision，经用户确认后显式传 confirmed=true（manifest_prune 同时传整数 expected_revision）。',
                )
        if action == 'migrate':
            required.append('target_spec')
        if any(key not in params for key in required):
            return _result(
                {'error': '缺少动作必填参数', 'required': required},
                False,
                'TOOL_INVALID_ARGUMENTS',
                effect='not_started',
                next_action=f'补齐动作 {action} 必填字段 {required} 后再调用。',
            )
        if action == 'save':
            try:
                prepare_datasource_spec(params['spec_payload'], params.get('scope', 'owner'))
            except DatasourceSpecValidationError as error:
                advice = '修正指出的字段后重新调用；连接扩展字段放入 options，凭据使用 secret_payload'
                return _result({'error': '连接规格校验失败，尚未开始操作', 'error_code': error.error_code, **error.details,
                    'next_action': advice,
                    'example': {'action': 'save', 'spec_payload': {'datasource_id': 'orders', 'kind': 'sqlite', 'title': '订单'}}},
                    False, 'TOOL_INVALID_ARGUMENTS', effect='not_started', next_action=advice)
        effective_root = self._effective_workspace_root()
        if 'workspace_root' in params and (effective_root is None or Path(params['workspace_root']).absolute() != effective_root):
            return _result(
                {'error': '工作区未获宿主授权'},
                False,
                'DATASOURCE_WORKSPACE_NOT_AUTHORIZED',
                effect='not_started',
                next_action='省略 workspace_root 以使用宿主当前绑定目录，或仅传入宿主已授权的准确工作区根路径。',
            )
        return None

    # LLM: 只调固定领域接口；真实失败返回ok=False，异常仅带类型，变更异常保守unknown而非假称未提交。
    # 函数用途: 执行一个已校验的数据源动作并返回安全回执。
    def execute(self, params):
        invalid = self.validate_invocation(params, None)
        if invalid is not None:
            return invalid
        try:
            data = self._dispatch(params)
            ok = data.get('ok', True) is not False and data.get('state') != 'FAILED' and data.get('effect_outcome') != 'unknown'
            return _result(data, ok, data.get('error_code', '') if not ok else '', effect=data.get('effect_outcome', ''))
        except ManifestRevisionConflict:
            return _result(
                {'error_code': 'ASSET_REVISION_CONFLICT', 'error': '清单版本已变化，请先重新读取'},
                False,
                'TOOL_INVALID_ARGUMENTS',
                effect='not_started',
                next_action='先调用 datasources(action="manifest_get") 获取最新 revision 后再决定是否重新提交。',
            )
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
            effect = 'unknown' if params['action'] in ('save', 'migrate', 'manifest_register', 'manifest_prune') else 'failed'
            advice = '先用 list/get 核对现有规格与 revision；禁止原样盲重试写操作' if effect == 'unknown' else '核对数据源代号、配置和访问权限后重试'
            return _result({'error': '数据源操作异常，无法确认写入结果' if effect == 'unknown' else '数据源读取或探测异常',
                'exception_type': type(error).__name__, 'effect_outcome': effect,
                'next_action': advice}, False,
                           'DATASOURCE_OPERATION_FAILED', effect=effect, next_action=advice)

    # LLM: workspace仅宿主授权；save兼容两个凭据参数并交Store合并/冲突检查，读取只返回安全投影。
    # 函数用途: 将动作及凭据别名映射到同Owner领域接口。
    def _dispatch(self, params):
        store, action = self.store, params['action']
        options = {'workspace_root': self._effective_workspace_root()}
        if action == 'manifest_get':
            return {'manifest': load_manifest(self.owner_home, params['source_system'])}
        if action == 'manifest_register':
            return {'manifest': save_manifest(self.owner_home, params['source_system'], params['sources'],
                expected_revision=params.get('expected_revision'))}
        if action == 'manifest_prune':
            return {'manifest': prune_sources(self.owner_home, params['source_system'], params['source_ids'],
                expected_revision=params['expected_revision'])}
        if action == 'list':
            return {'datasources': store.list_datasources(**options)}
        if action == 'save':
            secret = params.get('secret_payload') or params.get('credential_values')
            return store.save_datasource(params['spec_payload'], secret_payload=secret,
                credential_values=params.get('credential_values'),
                scope=params.get('scope', 'owner'), expected_revision=params.get('expected_revision'), **options)
        source_id = params['datasource_id']
        if action == 'get':
            spec = store.get_datasource_spec(source_id, **options)
            return store.public_summary(spec)
        if action == 'test':
            return store.test_datasource(source_id, **options)
        if action == 'inspect_schema':
            return store.inspect_schema(source_id, **options)
        return store.migrate_datasource(source_id, params['target_spec'], mode=params.get('mode', 'prepare_and_validate'),
            auto_switch=params.get('auto_switch', False), **options)
