# LLM: Owner根只由宿主构造器注入；复用资产Store与受审批的ScriptRunner，不从模型参数取得执行权限。
# 模块用途: 暴露固化脚本管理、源评估与单次运行的结构化工具，支持领域接口注入。
from __future__ import annotations

import importlib
import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import open_directory_beneath
from .input_schema import validate_tool_input
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

REQUIRED_KEYS = {'name', 'access_targets', 'data_sources', 'credential_refs', 'io_contract'}


# LLM: 独立动作锁包住工具层读改写；领域Store仍持原集合锁，直接Store写者不受本锁保护。
# 函数用途: 串行化同Owner同类资产的管理动作，防止增量更新或Skill绑定丢失。
def _dispatch(tool, params):
    if params['action'] in ('list', 'get', 'run', 'resume', 'inspect_run'):
        return tool._dispatch(params)
    tool.owner_home.mkdir(parents=True, exist_ok=True)
    collection = tool.model_spec.name
    os.close(open_directory_beneath(tool.owner_home, (collection,), create=True))
    with locked_json_path(tool.owner_home / collection / '.tool-actions'):
        return tool._dispatch(params)


# LLM: 注入对象来自宿主，显式不同Owner仍拒绝，不能借依赖注入跨Owner执行。
# 函数用途: 校验Store或Engine的公开Owner绑定，不读取资产。
def _owner(home, backend):
    if backend is not None and Path(getattr(backend, 'owner_home', home)).absolute() != home:
        raise ValueError('注入后端Owner与工具Owner不一致')


# LLM: 本函数只校验JSON形状，不接触业务存储，可在operation claim前调用。
# 函数用途: 共用Schema及按动作必填项，防止错误参数先创建Owner或留下未知操作。
def _validate(tool, params):
    name = tool.model_spec.name
    valid = validate_tool_input(params, tool.model_spec.input_schema).ok
    if not valid:
        return _outcome(name, {'error': '参数不符合工具Schema'}, False, 'TOOL_INVALID_ARGUMENTS')
    action = params['action']
    key = 'script_id' if name == 'scripts' else 'workflow_id'
    required = [] if action == 'list' else [key]
    if action in ('resume', 'inspect_run'):
        required = ['workflow_run_id']
    if action == 'register':
        required += ['descriptor', 'code']
    if action == 'save':
        required += ['definition']
    if action in ('update_sources', 'record_candidate'):
        required += ['source_id'] + (['candidate'] if action == 'record_candidate' else [])
    if action in ('bind_skill', 'unbind_skill'):
        required += ['step_id', 'skill_id']
    if any(key not in params or params[key] == '' for key in required):
        return _outcome(name, {'error': '缺少条件必填参数', 'required': required}, False, 'TOOL_INVALID_ARGUMENTS')
    return None


# LLM: 只容忍可选目标模块本身缺失；依赖损坏不降级成另一条隐式写链。
# 函数用途: 延迟读取并行开发的领域模块，允许宿主注入同协议对象。
def _optional(name):
    try:
        return importlib.import_module('agent_py_agent.agent.' + name)
    except ModuleNotFoundError as exc:
        if exc.name not in {'agent_py_agent.agent.' + name, 'agent_py_agent.agent.' + name.split('.')[0]}:
            raise
        return None


# LLM: 转换始终复制JSON，保领域to_dict的版本字段；不序列化可执行对象。
# 函数用途: 把领域结果、dataclass或轻量协议结果变成普通JSON值。
def _plain(value):
    value = value.to_dict() if hasattr(value, 'to_dict') else asdict(value) if is_dataclass(value) else value
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


# LLM: 两工具共用错误出口，业务结果保留；未知执行异常不谎称没有副作用。
# 函数用途: 将动作结果转换为宿主结果，运行失败也附完整业务回执。
def _outcome(name, data, ok=True, code=''):
    return ToolHandlerOutcome(name, ok, json.dumps(_plain(data), ensure_ascii=False), error_code=code)


# LLM: 危险动作必须走原审批，轻量协议或领域模块不授予额外执行权；资源按当前Owner工具实例串行。
# 函数用途: 给两工具统一声明只读、写入与危险动作的真实效果。
def _policy(name, reads, dangerous):
    return ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy('mutating', by_parameter=(('action', tuple(
            [(action, 'read_only') for action in reads] + [(action, 'dangerous') for action in dangerous])),)),
        approval_policy=ApprovalPolicy('always', always_effects=('dangerous',)),
        idempotency_policy=IdempotencyPolicy('operation'),
        resource_scopes=ResourceScopePolicy(mode='declared', static_scopes=(name + ':current_owner',)))


# LLM: Schema和目录声明同源；没有owner_home参数，调用者不能改Owner根或注入后端。
# 函数用途: 构造默认收起的工具描述与封闭动作集合。
def _spec(name, description, actions, properties):
    properties = {key: {**value, **({'pattern': r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'}
                  if key.endswith('_id') or key in ('scope_key', 'entrypoint') else {})} for key, value in properties.items()}
    return ToolModelSpec(name, description, {'type': 'object', 'properties': {
        'action': {'type': 'string', 'enum': actions}, **properties}, 'required': ['action'], 'additionalProperties': False},
        hints=ToolModelHints(category='capability', default_deferred=True, deferred_summary=description))


# LLM: 领域值对象优先；同协议注入时传独立字典，仍强制标准Key与目录ID一致。
# 函数用途: 校验脚本描述符并恢复AssetDescriptor，缺依赖时保留同形JSON。
def _descriptor(data, asset_id, kind):
    if not isinstance(data, dict) or REQUIRED_KEYS - data.keys() or data.get('id') != asset_id or data.get('kind') != kind:
        raise ValueError('必须提供标准提取Key且id/kind与目标一致')
    if not isinstance(data['name'], str) or not data['name'].strip() or not isinstance(data['io_contract'], dict):
        raise ValueError('name或io_contract类型无效')
    if any(not isinstance(data[key], list) for key in ('access_targets', 'data_sources', 'credential_refs')):
        raise ValueError('目标、数据源和凭据引用必须为列表')
    module = _optional('assets.descriptor')
    return module.AssetDescriptor.from_dict(data) if module else _plain(data)


# LLM: Store与runner均绑定同一个可信Owner；execute入口不接收身份、路径或任意命令。
# 类用途: 为Agent提供脚本目录、管理、源评估和受控运行工具。
class ScriptsTool(BaseTool):
    model_spec = _spec('scripts', '管理Owner固化脚本、标准提取Key与源评估；run仅单次覆盖数据源，执行和删除需原审批。',
        ['list', 'get', 'register', 'update', 'update_sources', 'record_candidate', 'run', 'delete'], {
            **{key: {'type': 'string'} for key in ('script_id', 'code', 'entrypoint', 'interpreter', 'source_id', 'scope_key')},
            **{key: {'type': 'object'} for key in ('descriptor', 'inputs', 'query_scope', 'candidate')},
            **{key: {'type': 'array', 'items': {'type': 'string'}} for key in ('add', 'remove')},
            'override_data_sources': {'type': ['object', 'array'], 'description': '仅本次传给runner，绝不写回资产'},
            'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 3600}})
    runtime_policy = _policy('scripts', ('list', 'get'), ('run', 'delete'))

    # LLM: 可注入具有get/list/save/delete/update_data_sources方法的Store和run_script协议，不在构造时启动进程。
    # 函数用途: 固定Owner，并保留尚未就绪领域模块的轻量接口接缝。
    def __init__(self, owner_home: Path, *, store=None, runner=None):
        self.owner_home = Path(owner_home).absolute()
        _owner(self.owner_home, store)
        _owner(self.owner_home, runner)
        self.store, self.runner = store, runner

    # LLM: 纯参数预检供原执行器claim前使用，不创建Owner或取得领域Store。
    # 函数用途: 返回条件必填及Schema错误，合法调用返回None。
    def validate_invocation(self, params, context):
        return _validate(self, params)

    # LLM: 优先正式资产Store；模块缺失且未注入时显式不可用，不另建平行账本。
    # 函数用途: 按需取得当前Owner唯一脚本存储。
    def _store(self):
        if self.store is None:
            module = _optional('assets.script_store')
            if module is None:
                raise RuntimeError('资产模块未就绪，请宿主注入ScriptStore协议')
            self.store = module.ScriptStore(self.owner_home)
        return self.store

    # LLM: 输入错误和真实运行失败分开；不吞中断，不以业务JSON文字改写成功状态。
    # 函数用途: 路由结构化脚本动作并返回JSON或原失败事实。
    def execute(self, params):
        invalid = self.validate_invocation(params, None)
        if invalid is not None:
            return invalid
        try:
            data = _dispatch(self, params)
            ok = params['action'] != 'run' or data.get('ok') is True
            return _outcome('scripts', data, ok, 'TOOL_EXECUTION_FAILED' if not ok else '')
        except (ValueError, TypeError, KeyError) as exc:
            return _outcome('scripts', {'error': str(exc)}, False, 'TOOL_INVALID_ARGUMENTS')
        except (OSError, RuntimeError, ImportError) as exc:
            return _outcome('scripts', {'error': str(exc)}, False, 'TOOL_EXECUTION_FAILED')

    # LLM: action只做精确枚举；所有ID由领域Store校验，run不接受路径替代ID。
    # 函数用途: 将公开动作交给领域存储或执行器。
    def _dispatch(self, params):
        action, store = params['action'], self._store()
        if action == 'list':
            return {'scripts': _plain(store.list_scripts())}
        script_id = params['script_id']
        old = store.get_script(script_id)
        if action == 'register' and old is not None:
            raise ValueError('脚本已存在，请使用update')
        if action != 'register' and old is None:
            raise ValueError('脚本不存在')
        if action in ('register', 'update'):
            return self._save(params, old)
        if action == 'get':
            return _plain(old)
        if action == 'delete':
            return {'deleted': store.delete_script(script_id)}
        if action in ('update_sources', 'record_candidate'):
            return self._sources(params, old)
        if action == 'run':
            runner = self.runner
            if runner is None:
                module = _optional('workflows')
                runner = module.ScriptRunner(self.owner_home) if module else None
            if runner is None:
                raise RuntimeError('请宿主注入run_script协议')
            return _plain(runner.run_script(script_id, params.get('inputs', {}),
                timeout_seconds=params.get('timeout_seconds'), override_data_sources=params.get('override_data_sources')))
        raise ValueError('未知脚本action')

    # LLM: update继承未改描述字段与代码，register必须完整；先校验全部声明再委托原子Store。
    # 函数用途: 创建或更新脚本，保留其代码与执行参数。
    def _save(self, params, old):
        prior = _plain(old) if old else {}
        defaults = {key: value for key, value in prior.items()
                    if key not in {'entrypoint', 'interpreter', 'timeout_seconds', 'code', 'entrypoint_path'}}
        descriptor = _descriptor(params.get('descriptor', defaults), params['script_id'], 'script')
        options = {key: params.get(key, prior.get(key, value)) for key, value in
                   (('entrypoint', 'main.py'), ('interpreter', 'python3'), ('timeout_seconds', 120))}
        if options['interpreter'] not in ('python3', 'bash', 'sh'):
            raise ValueError('只支持python3/bash/sh解释器')
        code = params.get('code', prior.get('code'))
        if not isinstance(code, str):
            raise ValueError('代码必须为文本')
        return _plain(self._store().save_script(descriptor, code, **options))

    # LLM: active候选仅加入显式scope_key，ignored/pending不激活；评估时间由宿主生成。
    # 函数用途: 增删源范围列表并登记候选评估，不启动探测或执行代码。
    def _sources(self, params, old):
        sources = _plain(old)['data_sources']
        source = next((row for row in sources if row['source_id'] == params['source_id']), None)
        if source is None:
            raise ValueError('数据源不存在')
        scope, patch = source['query_scope'].copy(), params.get('query_scope', {}).copy()
        scope.update(patch)
        candidate = params.get('candidate')
        if candidate:
            candidate = {**candidate, 'evaluated_at': candidate.get('evaluated_at', datetime.now(timezone.utc).isoformat())}
            if candidate.get('decision') == 'active' and candidate['candidate_id'] in params.get('remove', []):
                raise ValueError('不能同时激活和移除同一候选')
        key = params.get('scope_key')
        if params.get('add') or params.get('remove') or candidate and candidate.get('decision') == 'active':
            if not key or not isinstance(scope.get(key, []), list):
                raise ValueError('增删或激活必须指定列表scope_key')
            added = params.get('add', []) + ([candidate['candidate_id']] if candidate and candidate.get('decision') == 'active' else [])
            patch[key] = list(dict.fromkeys(value for value in scope.get(key, []) + added if value not in params.get('remove', [])))
        return _plain(self._store().update_data_sources(params['script_id'], params['source_id'], query_scope=patch, candidate=candidate))
