# LLM: Owner/workspace只由宿主授予；凭据双参数经Store隔离，保存/探测/迁移仍走审批，公开结果不含秘密。
# 模块用途: 提供数据源结构化工具；参数schema共用纯构造器，结果保留专属安全输出投影。
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..assets.datasource_store import DatasourceStore
from .input_schema import validate_tool_input
from .models import BaseTool, ToolHandlerOutcome
from .scripts_tool import _policy
from .tool_spec import _spec


# LLM: 结果已由DatasourceStore安全投影，不套会误删has_secret的资产描述符codec；失败不回显输入或驱动异常正文。
# 函数用途: 构造结构化数据源工具回执。
def _result(data, ok=True, code='', *, effect=''):
    return ToolHandlerOutcome('datasources', ok, json.dumps(data, ensure_ascii=False, allow_nan=False),
                              error_code=code, effect_outcome=effect)


# LLM: 所有权限绑定到同一可信宿主；共用纯schema不改变安全ID、连接规格或专属结果投影。
# 类用途: 向Agent暴露数据源管理操作。
class DatasourcesTool(BaseTool):
    name = 'datasources'
    model_spec = _spec('datasources', '管理数据源代号、隔离凭据、SQLite schema与迁移；输出不包含密码，外库导出不代表远端已导入。',
        ['list', 'get', 'save', 'test', 'inspect_schema', 'migrate'], {
            'datasource_id': {'type': 'string'}, 'workspace_root': {'type': 'string'},
            'spec_payload': {'type': 'object'}, 'secret_payload': {'type': 'object'},
            'credential_values': {'type': 'object'},
            'scope': {'type': 'string', 'enum': ['owner', 'workspace']},
            'expected_revision': {'type': 'integer', 'minimum': 0},
            'target_spec': {'type': 'object'},
            'mode': {'type': 'string', 'enum': ['prepare_and_validate', 'switch']},
            'auto_switch': {'type': 'boolean'}})
    runtime_policy = _policy('datasources', ('list', 'get', 'inspect_schema'), ('save', 'test', 'migrate'))

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
            return _result({'error': '参数不符合数据源Schema'}, False, 'TOOL_INVALID_ARGUMENTS', effect='not_started')
        action = params['action']
        required = [] if action == 'list' else ['spec_payload'] if action == 'save' else ['datasource_id']
        if action == 'migrate':
            required.append('target_spec')
        if any(key not in params for key in required):
            return _result({'error': '缺少动作必填参数', 'required': required}, False, 'TOOL_INVALID_ARGUMENTS', effect='not_started')
        effective_root = self._effective_workspace_root()
        if 'workspace_root' in params and (effective_root is None or Path(params['workspace_root']).absolute() != effective_root):
            return _result({'error': '工作区未获宿主授权'}, False, 'DATASOURCE_WORKSPACE_NOT_AUTHORIZED', effect='not_started')
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
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
            effect = 'unknown' if params['action'] in ('save', 'migrate') else 'failed'
            return _result({'error': '数据源操作失败', 'exception_type': type(error).__name__}, False,
                           'DATASOURCE_OPERATION_FAILED', effect=effect)

    # LLM: workspace仅宿主授权；save兼容两个凭据参数并交Store合并/冲突检查，读取只返回安全投影。
    # 函数用途: 将动作及凭据别名映射到同Owner领域接口。
    def _dispatch(self, params):
        store, action = self.store, params['action']
        options = {'workspace_root': self._effective_workspace_root()}
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
