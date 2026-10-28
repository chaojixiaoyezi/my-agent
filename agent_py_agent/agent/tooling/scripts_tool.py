# LLM: Owner根只由宿主构造器注入；复用资产Store与受审批的ScriptRunner，不从模型参数取得执行权限。
# 模块用途: 暴露固化脚本管理、源评估与单次运行；schema与完整描述符构造复用所属纯模块。
from __future__ import annotations

import json
import os
from contextlib import nullcontext
from dataclasses import asdict, fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..assets import AssetPostCommitReadbackError, valid_skill_reference
from ..assets.descriptor import AssetDescriptor, asset_descriptor_from_definition, clean_json
from ..assets.script_store import ScriptStore
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
    ToolRuntimePolicy,
)
from .tool_spec import _spec


# LLM: 独立动作锁仅包管理动作；源检查与其它只读入口不建资产动作目录，绑定另归Store集合锁。
# 函数用途: 串行化同Owner同类资产的管理动作，防止增量更新或Skill绑定丢失。
def _dispatch(tool, params):
    from ..workflows.datasource_runtime import _ASSET_IM_NOTIFIER, bind_asset_im_notifier
    notifier = getattr(tool, 'im_notifier', None)
    ctx = (bind_asset_im_notifier(tool.owner_home, notifier)
           if callable(notifier) and _ASSET_IM_NOTIFIER.get() is None else nullcontext())
    with ctx:
        if params['action'] in ('list', 'get', 'preview', 'run', 'resume', 'inspect_run', 'check_sources'):
            return tool._dispatch(params)
        tool.owner_home.mkdir(parents=True, exist_ok=True)
        collection = tool.model_spec.name
        from ..assets import _verify_directory_beneath
        _verify_directory_beneath(tool.owner_home, (collection,), create=True)
        with locked_json_path(tool.owner_home / collection / '.tool-actions'):
            return tool._dispatch(params)


# LLM: 注入对象来自宿主，显式不同Owner仍拒绝，不能借依赖注入跨Owner执行。
# 函数用途: 校验Store或Engine的公开Owner绑定，不读取资产。
def _owner(home, backend):
    if backend is not None and Path(getattr(backend, 'owner_home', home)).absolute() != home:
        raise ValueError('注入后端Owner与工具Owner不一致')


# LLM: JSON及显式Skill引用在claim前纯校验，不接触存储；目录ID与Skill来源引用采用各自合同。
# 函数用途: 共用Schema及按动作必填项，防止错误参数先创建Owner或留下未知操作。
def _validate(tool, params):
    name = tool.model_spec.name
    valid = validate_tool_input(params, tool.model_spec.input_schema).ok
    if not valid:
        return _outcome(name, {'error': '参数不符合工具Schema'}, False, 'TOOL_INVALID_ARGUMENTS')
    if 'skill_id' in params:
        try:
            valid_skill_reference(params['skill_id'])
        except (TypeError, ValueError) as exc:
            return _outcome(name, {'error': str(exc)}, False, 'TOOL_INVALID_ARGUMENTS')
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


# LLM: 仅供Store内部继承字段，不脱敏或序列化；展示必须另经_plain，避免把脱敏代码写回磁盘。
# 函数用途: 无损取得领域对象、dataclass或原字典的数据。
def _raw(value):
    return value.to_dict() if hasattr(value, 'to_dict') else asdict(value) if is_dataclass(value) else value


# LLM: 只用于模型可见投影，保留领域版本字段并共用有限JSON安全codec，不能作为更新的旧值。
# 函数用途: 把领域结果转换为脱敏的普通JSON值。
def _plain(value):
    return clean_json(_raw(value))


# LLM: 两工具共用错误出口；明确提交后读回事实优先于异常类型，进程清理未确认仍保留unknown。
# 函数用途: 将动作结果转换为宿主结果，运行失败也附完整业务回执与精确副作用状态。
def _outcome(name, data, ok=True, code='', *, effect_outcome='', failure_stage=''):
    if not ok and not effect_outcome:
        cleanup_unverified = isinstance(data, dict) and (
            bool((data.get('escalation_context') or {}).get('cleanup_error'))
            or any(bool((s.get('escalation_result') or {}).get('cleanup_error')) for s in data.get('steps') or () if isinstance(s, dict))
        )
        effect_outcome = 'unknown' if cleanup_unverified else ('not_started' if code == 'TOOL_INVALID_ARGUMENTS' else 'failed')
    return ToolHandlerOutcome(name, ok, json.dumps(_plain(data), ensure_ascii=False), error_code=code,
                              effect_outcome='' if ok else effect_outcome, failure_stage=failure_stage)


# LLM: 只接Store提交后typed异常；业务stage精确保留，宿主阶段沿既有execution枚举，不越界扩通用模型。
# 函数用途: 给两个资产工具统一返回已提交但读回失败的unknown回执。
def _post_commit_failure(name, error):
    return _outcome(name, {'error': str(error), 'committed': True,
                          'failure_stage': 'post_commit_readback', 'effect_outcome': 'unknown'},
                    False, 'TOOL_POST_COMMIT_READBACK_FAILED', effect_outcome='unknown',
                    failure_stage='execution')


# LLM: 危险动作在ask模式走原审批、在auto/full-access模式沿自主决策放行；资源按当前Owner工具实例串行。
# 函数用途: 给两工具统一声明只读、写入与危险动作的真实效果。
def _policy(name, reads, dangerous):
    return ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy('mutating', by_parameter=(('action', tuple(
            [(action, 'read_only') for action in reads] + [(action, 'dangerous') for action in dangerous])),)),
        approval_policy=ApprovalPolicy('dangerous'),
        idempotency_policy=IdempotencyPolicy('operation'),
        resource_scopes=ResourceScopePolicy(mode='declared', static_scopes=(name + ':current_owner',)))


# LLM: Store与runner均绑定同一个可信Owner；纯schema/descriptor沿共享构造器，execute不接收执行身份或任意命令。
# 类用途: 为Agent提供脚本目录、管理、源评估和受控运行工具。
class ScriptsTool(BaseTool):
    model_spec = _spec('scripts', '管理Owner固化脚本、标准提取Key与源评估；脚本应输出单行JSON对象到stdout；run仅单次覆盖数据源。',
        ['list', 'get', 'register', 'update', 'update_sources', 'record_candidate', 'run', 'delete', 'check_sources'], {
            'expected_revision': {'type': ['integer', 'null'], 'minimum': 0},
            **{key: {'type': 'string'} for key in ('script_id', 'code', 'entrypoint', 'interpreter', 'source_id', 'scope_key')},
            'descriptor': {'type': 'object', 'description': (
                '必填标准提取Key对象：{name: 非空字符串, access_targets: 列表(如[]), '
                'data_sources: 列表(如[{"source_id":"s1","system":"web","query_scope":{}}]), '
                'credential_refs: 列表(如[]), io_contract: {"inputs":[字段名],"outputs":[必选输出字段名]}，可选id/kind/summary/tags}')},
            'inputs': {'type': 'object', 'description': '传给脚本stdin及MY_AGENT_SCRIPT_INPUT_JSON的JSON对象'},
            'query_scope': {'type': 'object', 'description': 'update_sources时合并到指定source_id的查询范围字典'},
            'candidate': {'type': 'object', 'description': '候选源评估：{candidate_id, decision(active|ignored|pending), reason, sample_count}'},
            **{key: {'type': 'array', 'items': {'type': 'string'}} for key in ('add', 'remove')},
            'override_data_sources': {'type': ['object', 'array'], 'description': '仅本次传给runner，绝不写回资产'},
            'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 3600}})
    runtime_policy = _policy('scripts', ('list', 'get', 'check_sources'), ('run', 'delete'))

    # LLM: Store/runner和项目范围只由宿主注入；动态provider每次读取，模型参数不授予路径权限。
    # 函数用途: 固定Owner并保存显式领域依赖。
    def __init__(self, owner_home: Path, *, store=None, runner=None, workspace_root=None,
                 workspace_root_provider=None, im_notifier=None):
        self.owner_home = Path(owner_home).absolute()
        _owner(self.owner_home, store)
        _owner(self.owner_home, runner)
        self.store, self.runner, self.im_notifier = store, runner, im_notifier
        self.workspace_root, self.workspace_root_provider = workspace_root, workspace_root_provider

    # LLM: 只使用宿主提供的项目；无显式项目时兼容已绑定runner范围，不从脚本元数据恢复权限。
    # 函数用途: 获取当前操作使用的项目根。
    def _workspace_root(self):
        return (self.workspace_root_provider() if callable(self.workspace_root_provider)
                else self.workspace_root or getattr(self.runner, 'workspace_root', None))

    # LLM: 纯参数预检供原执行器claim前使用，不创建Owner或取得领域Store。
    # 函数用途: 返回条件必填及Schema错误，合法调用返回None。
    def validate_invocation(self, params, context):
        return _validate(self, params)

    # LLM: 显式Store优先；默认Store每次绑定当前workspace，避免缓存旧项目目录。
    # 函数用途: 按需取得当前Owner唯一脚本存储。
    def _store(self):
        return self.store if self.store is not None else ScriptStore(self.owner_home, workspace_root=self._workspace_root())

    # LLM: 提交后错误优先；CAS冲突单独结构化，运行保留领域源错误码，不从普通文本推导执行权。
    # 函数用途: 路由结构化脚本动作并返回JSON或原失败事实。
    def execute(self, params):
        invalid = self.validate_invocation(params, None)
        if invalid is not None:
            return invalid
        try:
            data = _dispatch(self, params)
            ok = params['action'] != 'run' or data.get('ok') is True
            return _outcome('scripts', data, ok, 'TOOL_EXECUTION_FAILED' if not ok else '')
        except AssetPostCommitReadbackError as exc:
            return _post_commit_failure('scripts', exc)
        except (ValueError, TypeError, KeyError) as exc:
            if str(exc).startswith('ASSET_REVISION_CONFLICT:'):
                return _outcome('scripts', {'error_code': 'ASSET_REVISION_CONFLICT', 'error': str(exc)},
                                False, 'TOOL_INVALID_ARGUMENTS', effect_outcome='not_started')
            return _outcome('scripts', {'error': str(exc)}, False, 'TOOL_INVALID_ARGUMENTS')
        except (OSError, RuntimeError, ImportError) as exc:
            return _outcome('scripts', {'error': str(exc)}, False, 'TOOL_EXECUTION_FAILED')

    # LLM: action精确枚举；check_sources刷新而get稳定，运行仍经源硬门；workspace/runner仅取原宿主绑定。
    # 函数用途: 将公开动作交给原领域存储、实时检查或执行器。
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
        if action == 'check_sources':
            from ..workflows.datasource_runtime import project_script_asset_fields
            return project_script_asset_fields(self.owner_home, old, workspace_root=self._workspace_root(), refresh_timestamp=True)
        if action == 'delete':
            return {'deleted': store.delete_script(script_id)}
        if action in ('update_sources', 'record_candidate'):
            return self._sources(params, old)
        if action == 'run':
            from ..workflows.datasource_runtime import run_script_with_datasources
            return _plain(run_script_with_datasources(self.owner_home, script_id, params.get('inputs', {}),
                runner=self.runner, workspace_root=self._workspace_root(),
                timeout_seconds=params.get('timeout_seconds'), override_data_sources=params.get('override_data_sources')))
        raise ValueError('未知脚本action')

    # LLM: update只继承原正式字段和代码；完整定义沿资产领域校验，不写回实时投影，CAS仍由Store原锁检查。
    # 函数用途: 创建或更新脚本，保留其代码与执行参数。
    def _save(self, params, old):
        prior = _raw(old) if old else {}
        names = {item.name for item in fields(AssetDescriptor)}
        defaults = {key: value for key, value in prior.items() if key in names}
        descriptor = asset_descriptor_from_definition(params.get('descriptor', defaults), params['script_id'], 'script')
        options = {key: params.get(key, prior.get(key, value)) for key, value in
                   (('entrypoint', 'main.py'), ('interpreter', 'python3'), ('timeout_seconds', 120))}
        if options['interpreter'] not in ('python3', 'bash', 'sh'):
            raise ValueError('只支持python3/bash/sh解释器')
        code = params.get('code', prior.get('code'))
        if not isinstance(code, str):
            raise ValueError('代码必须为文本')
        if 'expected_revision' in params:
            options['expected_revision'] = params['expected_revision']
        return _plain(self._store().save_script(descriptor, code, **options))

    # LLM: 读改写沿Store原值，不把展示脱敏字段回写；active候选仅加入显式scope_key，评估时间由宿主生成。
    # 函数用途: 增删源范围列表并登记候选评估，不启动探测或执行代码。
    def _sources(self, params, old):
        sources = _raw(old)['data_sources']
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
