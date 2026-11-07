"""W2 的 owner 策略存储；CRUD 复用 V13 事务，工具热路径只取有界只读快照。"""
from __future__ import annotations

import importlib
import json
import logging
import os
import re
import threading
import uuid
from collections import OrderedDict
from pathlib import Path, PurePosixPath, PureWindowsPath

from ..common.cache_freshness import cache_entry_trustworthy, cache_stat_signature
from ..conversation.business_access import BusinessError
from ..plugin_host.errors import HostAPIError
from ..plugin_host.schemas import validate_object
from .v13_store import V13Store, _page_rows, v13_store_for

_collections = {
    'intercept.rules': ('policy_command_rules', 'PolicyRule', 'rule_id', 'rule'),
    'intercept.tool_config': ('policy_tool_config', 'ToolFilterConfig', 'tool', 'config'),
    'intercept.judge': ('policy_judge_config', 'JudgeConfig', '', 'config'),
    'scope_policy.rules': ('scope_rules', 'ScopeRule', 'rule_id', 'rule'),
    'scope_policy.task_rules': ('scope_task_rules', 'ScopeRule', 'rule_id', 'rule'),
    'scope_policy.task_scope': ('scope_task_scopes', 'TaskScope', 'task_id', 'scope'),
    'scope_policy.task_constraints': ('scope_task_constraints', 'TaskConstraint', 'constraint_id', 'constraint'),
}
_snapshot_cache = OrderedDict()
_warning_generations = OrderedDict()
_cache_lock = threading.RLock()
_logger = logging.getLogger(__name__)


def _foundation():
    # 注册器反向导入本模块，只有实际调用时才能取得完整的 V13 基础接口。
    from ..gateway_parts import business_api_v13
    return business_api_v13


def _limit(name):
    # W0 未合入时保留契约默认；正式限制模块一旦存在就必须读取它。
    defaults = {'POLICY_RULES_MAX_PER_OWNER': 500, 'SCOPE_RULES_MAX_PER_OWNER': 500,
                'SCOPE_TASK_RULES_MAX_PER_TASK': 100}
    try:
        limits = importlib.import_module(__package__ + '.gap158_limits')
    except ModuleNotFoundError as error:
        if error.name != __package__ + '.gap158_limits':
            raise
        return defaults[name]
    value = getattr(limits, name, defaults[name])
    if type(value) is not int or value < 1:
        raise HostAPIError('HOST_NOT_READY', 503)
    return value


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _shape(name):
    from ..gateway_parts.business_api_v13_gap158_w2 import V13_DEFS
    return V13_DEFS[name]


def _public(row, shape):
    result = {key: _copy(value) for key, value in row.items() if key in _shape(shape)['properties']}
    return validate_object(result, _shape(shape))


def _not_found():
    raise HostAPIError('RESOURCE_NOT_FOUND', 404)


def _belongs(row, owner_id, task_id=None):
    return (isinstance(row, dict) and row.get('owner_id') == owner_id
            and (task_id is None or row.get('task_id') == task_id))


def _required(call, row, task_id=None):
    if not _belongs(row, call.owner_id, task_id):
        _not_found()
    _foundation().v13_require_access(call, row)
    return row


def _owner_path(root, value, workspace_root=None):
    """入库固定真实目标；相对路径使用宿主工作区，绝不把 owner 根当 cwd。"""
    try:
        if not isinstance(value, str) or '\x00' in value or not value:
            raise ValueError('path')
        base = Path(root).resolve()
        candidate = Path(value)
        if PureWindowsPath(value).drive and not candidate.is_absolute():
            raise ValueError('foreign absolute path')
        workspace = Path(workspace_root).resolve() if workspace_root is not None else base
        target = (candidate if candidate.is_absolute() else workspace / candidate).resolve()
        if not candidate.is_absolute() and not target.is_relative_to(workspace):
            raise ValueError('outside workspace')
        if not target.is_relative_to(base):
            raise ValueError('outside')
        return str(target)
    except (OSError, ValueError, RuntimeError) as error:
        raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403) from error


def _domain(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise HostAPIError('INVALID_SCHEMA')
    leading = '.' if value.startswith('.') else ''
    host = value[len(leading):].rstrip('.')
    try:
        host = host.encode('idna').decode('ascii').lower()
    except UnicodeError as error:
        raise HostAPIError('INVALID_SCHEMA') from error
    if (not host or len(host) > 253 or any(re.fullmatch(
            r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) is None for label in host.split('.'))):
        raise HostAPIError('INVALID_SCHEMA')
    return leading + host


def _normalize_rule(owner_root, row, shape, workspace_root=None):
    row = dict(row)
    if shape == 'PolicyRule':
        if row.get('path_prefix'):
            row['path_prefix'] = _owner_path(owner_root, row['path_prefix'], workspace_root)
    elif shape == 'ScopeRule':
        row['pattern'] = (_owner_path(owner_root, row['pattern'], workspace_root) if row['kind'].startswith('path_')
                          else _domain(row['pattern']))
    _public(row, shape)
    return row


def _task(call):
    """任务存在性只经冻结读口；原始文件、调用参数和展示缓存都不是任务权威。"""
    task_id = call.params['task_id']
    try:
        result = _foundation().v13_call_frozen(call, 'tasks.get', {'task_id': task_id})
    except (HostAPIError, BusinessError) as error:
        if error.code in {'OBSERVATION_NOT_FOUND', 'TASK_NOT_FOUND', 'RESOURCE_NOT_FOUND', 'BUSINESS_ACCESS_DENIED'}:
            _not_found()
        raise
    row = result.get('task')
    if (not isinstance(row, dict) or row.get('task_id') != task_id
            or row.get('owner_id', call.owner_id) != call.owner_id):
        _not_found()
    return row


def _workspace_root(call, task):
    candidate = (task or {}).get('workspace_root') or getattr(call.context.host, 'workspace_root', None)
    if not isinstance(candidate, (str, Path)) or not Path(candidate).is_absolute():
        raise HostAPIError('HOST_NOT_READY', 503)
    return Path(candidate).resolve()


def _workspace_paths(root, paths):
    result = []
    for value in paths:
        try:
            if (not isinstance(value, str) or not value or '\x00' in value or '\\' in value
                    or value.startswith('-') or PurePosixPath(value).is_absolute()
                    or PureWindowsPath(value).drive or '..' in value.split('/')):
                raise ValueError('path')
            base = Path(root).resolve()
            target = (base / value).resolve()
            if not target.is_relative_to(base):
                raise ValueError('outside')
            canonical = target.relative_to(base).as_posix()
            if canonical not in result:
                result.append(canonical)
        except (OSError, ValueError, RuntimeError) as error:
            raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403) from error
    return result


def _default_config(group, params):
    if group == 'intercept.tool_config':
        return {'tool': params['tool'], 'default_decision': 'inherit', 'judge_enabled': False}
    return {'enabled': False, 'max_calls_per_hour': 0, 'timeout_seconds': 30}


def _profile_exists(call, profile_id):
    # 只读取真实模型目录，不创建模型、不使用 profile 字符串当作已配置事实。
    if not profile_id:
        return
    params, seen = {'limit': 100}, set()
    while True:
        page = _foundation().v13_call_frozen(call, 'model_profiles.list', params)
        if any(row.get('profile_id') == profile_id for row in page['items']):
            return
        cursor = page.get('next_cursor')
        if not cursor:
            _not_found()
        if cursor in seen or len(seen) >= 100:
            raise HostAPIError('HOST_NOT_READY', 503)
        seen.add(cursor)
        params = {'limit': 100, 'cursor': cursor}


def _capacity(records, collection, task_id=None):
    name = 'POLICY_RULES_MAX_PER_OWNER' if collection.startswith('policy_') else 'SCOPE_RULES_MAX_PER_OWNER'
    maximum = _limit(name)
    if len(records) >= maximum:
        raise BusinessError('LIMIT_EXCEEDED', details={'limit': maximum})
    if task_id is not None:
        maximum = _limit('SCOPE_TASK_RULES_MAX_PER_TASK')
        if sum(row.get('task_id') == task_id for row in records.values()) >= maximum:
            raise BusinessError('LIMIT_EXCEEDED', details={'limit': maximum})


def _list(call, store, collection, shape, task_id):
    rows, revision = store._record_view(collection, None, all_rows=True)
    params = call.params
    filters = {key: params[key] for key in ('enabled', 'tool', 'kind') if key in params}
    visible = []
    for row in rows:
        if not _belongs(row, call.owner_id, task_id):
            continue
        _foundation().v13_require_access(call, row)
        if all(row.get(key) == value for key, value in filters.items()):
            visible.append(_public(row, shape))
    page = _page_rows(visible, [str(call.owner_home), call.owner_id, call.subject_id,
        call.action, revision, task_id, filters, visible],
        cursor=params.get('cursor') or None, limit=params.get('limit', 50))
    # W2 使用可省略的非 null 字符串；不把 V13Store 的 null 终游标塞入闭合 schema。
    return {'revision': revision, **page, 'next_cursor': page['next_cursor'] or ''}


def _get(call, store, group, collection, shape, identity):
    row, revision = store._record_view(collection, identity)
    task_id = call.params.get('task_id')
    if row is not None and _belongs(row, call.owner_id, task_id):
        _foundation().v13_require_access(call, row)
        return {_collections[group][3]: _public(row, shape), 'revision': revision}
    if group == 'scope_policy.task_scope':
        return {'revision': revision}
    return {'config': _default_config(group, call.params), 'revision': revision}


def _crud(call):
    group, verb = call.action.rsplit('.', 1)
    collection, shape, id_field, result_field = _collections[group]
    params = call.params
    task_id = params.get('task_id')
    task = _task(call) if task_id is not None else None
    store = v13_store_for(call, 'owner')
    identity = params.get(id_field, 'default') if id_field else 'default'
    if verb == 'list':
        return _list(call, store, collection, shape, task_id)
    if verb == 'get':
        return _get(call, store, group, collection, shape, identity)

    def normalize(row):
        try:
            root = _workspace_root(call, task) if (shape == 'PolicyRule' and row.get('path_prefix')
                or shape == 'ScopeRule' and row.get('kind', '').startswith('path_')) else None
            return _normalize_rule(call.owner_home, row, shape, root)
        except HostAPIError as error:
            if error.code == 'PATH_OUTSIDE_WORKSPACE':
                raise HostAPIError('INVALID_SCHEMA') from error
            raise

    # 先核目标归属，再进事务；删除重放仍先交原回执，不能被已删除目标挡掉。
    before, _revision = store._record_view(collection, identity)
    absent = not _belongs(before, call.owner_id, task_id)
    if not absent:
        _foundation().v13_require_access(call, before)

    def mutate(records):
        # 此读发生于 V13Store 原集合锁内，与本笔旧状态是同一逻辑快照。
        revision = store._read(collection)['revision'] + 1
        current = records.get(identity)
        if verb == 'create':
            _capacity(records, collection, task_id)
            new_id = ('constraint-' if id_field == 'constraint_id' else 'rule-') + uuid.uuid4().hex
            row = {'owner_id': call.owner_id, **params, id_field: new_id}
            if shape == 'PolicyRule':
                row = {'priority': 0, 'enabled': True, **row,
                       'created_at': call.clock(), 'updated_at': call.clock()}
            elif shape == 'ScopeRule':
                row = {'priority': 0, 'enabled': True, **row}
            row = normalize(row)
            records[new_id] = row
            return {result_field: _public(row, shape), 'revision': revision}

        if verb == 'set':
            if current is not None:
                _required(call, current, task_id)
            else:
                _capacity(records, collection)
            if group == 'intercept.judge':
                # 原回执先于依赖复验；已提交请求的重放不能被目录的后续删除改写。
                _profile_exists(call, params.get('model_profile_id', ''))
            if shape == 'TaskScope':
                root = _workspace_root(call, task)
                row = {**params, 'workspace_subpaths': _workspace_paths(root, params['workspace_subpaths'])}
            else:
                row = {**_default_config(group, params), **params}
            row = {'owner_id': call.owner_id, **row}
            _public(row, shape)
            records[identity] = row
            return {result_field: _public(row, shape), 'revision': revision}

        if current is None and group == 'scope_policy.task_scope' and verb == 'delete':
            return {'deleted': False, 'revision': revision}
        current = _required(call, current, task_id)
        if verb == 'delete':
            del records[identity]
            return {'deleted': True, 'revision': revision}
        if verb not in {'update', 'toggle'}:
            raise HostAPIError('UNSUPPORTED_ACTION')
        row = {**current, **{key: value for key, value in params.items() if key not in {id_field, 'task_id'}}}
        if shape == 'PolicyRule':
            row['updated_at'] = call.clock()
        row = normalize(row)
        records[identity] = row
        return {result_field: _public(row, shape), 'revision': revision}

    try:
        return store.transaction(collection, mutate, idempotency_key=call.idempotency_key,
            params_digest=_foundation()._digest(params), expected_revision=call.expected_revision)
    except BusinessError as error:
        # 原事务先查回执再 CAS。缺失/外 owner/错 task 不通过不同 CAS 错误泄漏存在性。
        if (error.code == 'REVISION_CONFLICT' and absent
                and (verb in {'update', 'delete', 'toggle'} or before is not None)):
            _not_found()
        raise


def dispatch(call):
    """所有静态 V13Action 只绑定这个入口；其它子系统在请求期才按固定接口加载。"""
    _foundation().v13_require_access(call)
    action = call.action
    group, _verb = action.rsplit('.', 1)
    if group in _collections and action != 'intercept.judge.usage':
        return _crud(call)
    if action in {'intercept.pending.list', 'intercept.pending.decide', 'intercept.judge.usage'}:
        judge = importlib.import_module(__package__ + '.gap158_w2_judge')
        if action == 'intercept.pending.list':
            return judge.pending_list(call)
        if action == 'intercept.pending.decide':
            return judge.decide_pending(call, decided_by='owner')
        return judge.usage(call.owner_home, call.params, now=call.clock())
    history = importlib.import_module(__package__ + '.gap158_w2_policy_history')
    if action == 'intercept.history.list':
        return history.history_page(call.owner_home, call.params)
    if action == 'intercept.history.execution':
        return history.execution(call.owner_home, call.params['execution_id'])
    if action == 'intercept.task.list':
        return history.task_page(call.owner_home, call.params)
    raise HostAPIError('UNSUPPORTED_ACTION')


def _generation(owner_root, collection):
    root = Path(owner_root)
    directory = root / 'data' / 'business_v13'
    paths = (root, root / 'data', directory, directory / (collection + '.json'),
             directory / '.transaction.json')
    signatures, trusted = [], True
    for path in paths:
        try:
            info = os.stat(path, follow_symlinks=False)
            signatures.append(cache_stat_signature(info))
            trusted = trusted and cache_entry_trustworthy(info.st_mtime_ns)
        except FileNotFoundError:
            signatures.append(None)
    return tuple(signatures), trusted


def _warn_once(owner_root, collection, generation):
    # 父目录和 journal 的无关写入只能使缓存失效，不能重复报警同一文件代次。
    file_generation = generation[3] if generation is not None else None
    key = (str(owner_root), collection, file_generation)
    with _cache_lock:
        if key in _warning_generations:
            _warning_generations.move_to_end(key)
            return
        _warning_generations[key] = None
        while len(_warning_generations) > 256:
            _warning_generations.popitem(last=False)
    # 不记录原路径或文件正文，避免配置中的秘密进入工具运行日志。
    _logger.warning('W2 策略集合 %s 缺失、损坏或含不安全记录，本代使用空集合/忽略非法路径', collection)


def _snapshot_rows(store, collection, shape):
    """缓存只保存数据副本，不保存授权；journal 与父目录代次同时参与失效。"""
    cache_key = (str(store.owner_home), store.owner_id, collection)
    generation = None
    for _attempt in range(3):
        try:
            generation, trustworthy = _generation(store.owner_home, collection)
            with _cache_lock:
                cached = _snapshot_cache.get(cache_key)
                if trustworthy and cached is not None and cached[0] == generation:
                    _snapshot_cache.move_to_end(cache_key)
                    return _copy(cached[1])
            rows, revision = store._record_view(collection, None, all_rows=True)
            maximum = _limit('POLICY_RULES_MAX_PER_OWNER' if collection.startswith('policy_')
                             else 'SCOPE_RULES_MAX_PER_OWNER')
            if len(rows) > maximum or collection == 'policy_judge_config' and len(rows) > 1:
                raise ValueError('bound')
            result = [_public(row, shape) for row in rows if _belongs(row, store.owner_id)]
            # 缺文件但 journal 中存在已提交版本时，以原 V13 的逻辑快照为准。
            missing = generation[3] is None and revision == 0
            after, trust_after = _generation(store.owner_home, collection)
            if after != generation:
                continue
            if missing:
                _warn_once(store.owner_home, collection, generation)
            if trustworthy and trust_after:
                with _cache_lock:
                    _snapshot_cache[cache_key] = (generation, _copy(result))
                    _snapshot_cache.move_to_end(cache_key)
                    while len(_snapshot_cache) > 128:
                        _snapshot_cache.popitem(last=False)
            return result
        except (OSError, ValueError, RuntimeError, BusinessError):
            # HostAPIError 是 ValueError；失败不恢复、不创建 owner 目录或写配置。
            _warn_once(store.owner_home, collection, generation)
            return []
    _warn_once(store.owner_home, collection, generation)
    return []


def _snapshot_rules(store, collection, shape):
    # 规则入库已固化 canonical 目标。资源侧每次 resolve；不得因路径变化删掉 allow
    # 后变成“无限制”，也不对 500 条规则重复访问文件系统。
    return _snapshot_rows(store, collection, shape)


def get_snapshot(owner_root, kind, task_id='', workspace_root=None):
    """给已开启的策略钩子取快照；调用方应先判开关，关闭时不得调用本函数。

    此处不执行任务查找、模型请求或恢复写盘。TaskScope 的相对路径在提供真实
    workspace_root 时重核；未提供根时保留相对约束，由评估器按真实事实收窄。
    """
    store = V13Store.read_only(owner_root)
    if kind == 'command':
        configs = _snapshot_rows(store, 'policy_judge_config', 'JudgeConfig')
        return {'rules': _snapshot_rules(store, 'policy_command_rules', 'PolicyRule'),
                'tool_configs': _snapshot_rows(store, 'policy_tool_config', 'ToolFilterConfig'),
                'judge_config': configs[0] if configs else {}}
    if kind != 'scope':
        raise ValueError('unsupported policy kind')
    task_rules = _snapshot_rules(store, 'scope_task_rules', 'ScopeRule') if task_id else []
    constraints = _snapshot_rows(store, 'scope_task_constraints', 'TaskConstraint') if task_id else []
    scopes = _snapshot_rows(store, 'scope_task_scopes', 'TaskScope') if task_id else []
    scope = next((row for row in scopes if row['task_id'] == task_id), None)
    if scope is not None and workspace_root is not None:
        try:
            scope = {**scope, 'workspace_subpaths': _workspace_paths(workspace_root, scope['workspace_subpaths'])}
        except (OSError, ValueError, RuntimeError):
            generation, _trusted = _generation(store.owner_home, 'scope_task_scopes')
            _warn_once(store.owner_home, 'scope_task_scopes', generation)
            # 保留限制而非失效后放行：原相对根外目标在纯评估器中明确拒绝。
            scope = {**scope, 'workspace_subpaths': ['../invalid-scope']}
    return {'rules': _snapshot_rules(store, 'scope_rules', 'ScopeRule'),
            'task_rules': [row for row in task_rules if row.get('task_id') == task_id],
            'constraints': [row for row in constraints if row['task_id'] == task_id],
            'task_scope': scope}
