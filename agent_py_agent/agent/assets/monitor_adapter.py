"""监控绑定及只读投影：依赖、版本和最近运行均来自结构化事实。"""
# LLM: 监控仅读Scheduler事实及显式资产ID，保存独立绑定，不执行引用动作或读取凭据；公开值统一过AssetDescriptor。
# 模块用途: 提供监控标准Key及引用拓扑；绑定写入共用资产根恢复锁和CAS版本，仍存data/monitors/<id>.json。
from __future__ import annotations

import json
import os
import sqlite3
import unicodedata
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import (
    list_names_beneath,
    open_readonly_file_beneath,
    read_bytes_beneath,
    write_text_atomic_beneath,
)
from . import _verify_directory_beneath, asset_root_lock, next_asset_revision, safe_asset_id
from .asset_metadata import normalize_trigger_matchers
from .descriptor import AssetDescriptor
from .revision_history import get_version, latest_version


# LLM: 根由调用方授权，不resolve链接；有界nofollow读，缺失与损坏严格区分，不创建目录。
# 函数用途: 读取本Owner一份JSON对象，供绑定和Scheduler视图使用。
def _read_object(home, parts):
    raw = read_bytes_beneath(home, parts, max_bytes=4 * 1024 * 1024)
    if raw is None:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('资产元数据必须是JSON对象')
    return value


# LLM: 组合准备不持资产根锁；独立协调锁串行绑定写者，原根锁→单项锁只负责CAS与提交，避免递归flock。
# 函数用途: 固定发布版本、更新隐藏组合并原子保存监控绑定，不修改Scheduler原任务。
def save_monitor_binding(owner_home, monitor_id, *, name='', summary='', schedule_spec=None, **bindings):
    home, identifier = Path(owner_home).absolute(), safe_asset_id(monitor_id)
    allowed = {'workflow_ids', 'script_ids', 'skill_ids', 'access_targets', 'data_sources', 'credential_refs', 'expected_revision',
               'alias', 'trigger_matchers', 'pinned_version', 'upgrade_policy', 'appended_steps', 'execution_target'}
    if bindings.keys() - allowed:
        raise TypeError('监控绑定包含未知字段')
    refs = {kind: list(dict.fromkeys(bindings.get(key, ()))) for kind, key in
            (('workflows', 'workflow_ids'), ('scripts', 'script_ids'), ('skills', 'skill_ids'))}
    payload = {'name': name, 'summary': summary, 'schedule_spec': schedule_spec or {}, 'refs': refs,
               'metadata': {key: bindings[key] for key in ('alias', 'trigger_matchers') if key in bindings},
               **{key: bindings.get(key, ()) for key in ('access_targets', 'data_sources', 'credential_refs')}}
    parts = ('data', 'monitors', identifier + '.json')
    home.mkdir(parents=True, exist_ok=True)
    _verify_directory_beneath(home, ('data', 'monitors'), create=True)
    with locked_json_path(home / 'data' / 'monitors' / '.bindings'):
        previous = _read_object(home, parts)
        revision = next_asset_revision(previous, bindings.get('expected_revision'))
        if previous:
            payload = {**previous, **{key: value for key, value in payload.items()
                if key not in ('metadata', 'refs')}, 'metadata': {**previous.get('metadata', {}), **payload['metadata']},
                'refs': {kind: refs[kind] if key in bindings else previous.get('refs', {}).get(kind, [])
                         for kind, key in (('workflows', 'workflow_ids'), ('scripts', 'script_ids'), ('skills', 'skill_ids'))}}
        payload['metadata'] = _binding_pins(home, identifier, payload, bindings)
        payload['revision'] = revision
        descriptor = _monitor_dict(home, identifier, payload, {})
        stored = {**descriptor, 'refs': descriptor['metadata']['asset_refs']}
        with asset_root_lock(home):
            with locked_json_path(home / 'data' / 'monitors' / (identifier + '.json')):
                next_asset_revision(_read_object(home, parts), revision - 1)
                write_text_atomic_beneath(home, parts, json.dumps(stored, ensure_ascii=False, allow_nan=False) + '\n')
    return descriptor


# LLM: scheduler_job显式传入优先；缺省仅读本Owner canonical store，不猜路径或创建仓库实例。
# 函数用途: 从Scheduler集合取得Job记录，调用方自行处理删除状态。
def _jobs(owner_home, scheduler_jobs=()):
    store = _read_object(owner_home, ('data', 'scheduler', 'store.json')) or {}
    if scheduler_jobs:
        supplied = list(scheduler_jobs)
        jobs = {row['job_id']: row for row in supplied}
        if len(jobs) != len(supplied):
            raise ValueError('重复的Scheduler job_id')
    else:
        jobs = store.get('jobs', {})
    if not isinstance(jobs, dict):
        raise ValueError('Scheduler jobs必须是对象')
    result = dict(jobs)
    rules = _read_object(owner_home, ('data', 'business_v13', 'monitor_rules.json')) or {}
    for row in rules.get('records', {}).values():
        if not isinstance(row, dict) or row.get('_deleted'):
            continue
        identifier = row.get('scheduler_job_id') or row.get('rule_id')
        if not isinstance(identifier, str):
            continue
        existing = result.get(identifier, {})
        result[identifier] = {'job_id': identifier, 'name': row.get('name', identifier),
            'schedule': row.get('schedule') or {}, 'status': 'active' if row.get('enabled') else 'paused',
            **existing, **{kind + '_id': row[kind + '_id'] for kind in ('workflow', 'script') if row.get(kind + '_id')}}
    return list(result.values())


# LLM: 绑定与Job按精确ID合并，重复Job拒绝；deleted覆盖同ID绑定，不复活已删除监控，不吞坏记录。
# 函数用途: 列出本Owner监控标准字典，顺序按ID稳定，无资产时不写盘。
def list_monitor_descriptors(owner_home, scheduler_jobs=()):
    home = Path(owner_home).absolute()
    jobs = {}
    for job in _jobs(home, scheduler_jobs):
        identifier = safe_asset_id(job['job_id'])
        if identifier in jobs:
            raise ValueError('重复的Scheduler job_id')
        jobs[identifier] = job
    try:
        names = list_names_beneath(home, ('data', 'monitors'))
    except FileNotFoundError:
        names = []
    identifiers = set(jobs) | {safe_asset_id(Path(name).stem) for name in names if name.endswith('.json')}
    return [get_monitor_descriptor(home, identifier, jobs.get(identifier)) for identifier in sorted(identifiers)
            if jobs.get(identifier, {}).get('status') != 'deleted']


# LLM: ID始终单段且须与Job及已存绑定匹配；不存在抛FileNotFoundError，损坏/链接错误不伪装成空对象。
# 函数用途: 合并一个监控的真实调度记录与扩展绑定，返回独立安全字典。
def get_monitor_descriptor(owner_home, monitor_id, scheduler_job=None):
    home, identifier = Path(owner_home).absolute(), safe_asset_id(monitor_id)
    binding = _read_object(home, ('data', 'monitors', identifier + '.json'))
    if binding is not None and binding.get('id') != identifier:
        raise ValueError('监控绑定ID不匹配')
    if scheduler_job is None:
        scheduler_job = next((row for row in _jobs(home) if row.get('job_id') == identifier), None)
    if scheduler_job is not None and scheduler_job.get('job_id') != identifier:
        raise ValueError('Scheduler Job与监控ID不匹配')
    if binding is None and scheduler_job is None:
        raise FileNotFoundError('监控不存在')
    job = dict(scheduler_job or {})
    if '_last_run' not in job:
        store = _read_object(home, ('data', 'scheduler', 'store.json')) or {}
        active = _latest_run(store.get('runs'), identifier)
        archived = _archived_run(home, store.get('owner', {}), identifier)
        job['_last_run'] = _latest_run({'active': active, 'archived': archived}, identifier)
    return _monitor_dict(home, identifier, binding or {}, job)


# LLM: 时间只转换结构化epoch，缺失保None；不把当前时间或文案当成最后运行证据。
# 函数用途: 规范Scheduler的时间戳以适配公共RuntimeSummary字符串字段。
def _run_time(value):
    if value is None or value == 0:
        return None
    if type(value) in (int, float):
        return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')
    if not isinstance(value, str):
        raise ValueError('运行时间必须是时间戳或字符串')
    return value


# LLM: Job有schedule时完全覆盖绑定计划，不能让旧enabled复活已暂停Job；复合规则显式展示而不猜单规则。
# 函数用途: 将cron/every/at与多规则计划转为统一调度摘要。
def _schedule(job, binding):
    source = job.get('schedule', binding.get('schedule_spec', {}))
    if not isinstance(source, dict):
        raise ValueError('监控schedule_spec必须是对象')
    expression = source.get('expression', source.get('every_seconds', source.get('at', '')))
    result = {'kind': source.get('kind', ''), 'expression': str(expression), 'timezone': source.get('timezone', 'UTC'),
              'next_run_at': job.get('next_run_at', source.get('next_run_at')),
              'enabled': job.get('status') == 'active' if 'status' in job or 'job_id' in job else source.get('enabled', False)}
    if job.get('rules') is not None:
        result.update(kind='composite', expression='', rules=[{'id': rule['id'], **_schedule(
            {'schedule': rule['schedule'], 'next_run_at': rule.get('next_run_at'),
             'status': 'active' if rule['enabled'] and job.get('status') == 'active' else 'paused'}, {})}
            for rule in job['rules']])
    if type(result['enabled']) is not bool:
        raise ValueError('监控enabled必须是布尔值')
    return result


# LLM: AssetDescriptor负责类型及脱敏，revision来自绑定而非Scheduler；引用按绑定优先去重，仅提供拓扑不授执行权。
# 函数用途: 合并监控调度和引用摘要，保留已持久化的资产版本。
def _monitor_dict(home, identifier, binding, job):
    schedule = _schedule(job, binding)
    refs = dict(binding.get('refs', {}))
    for kind, identifiers in _scheduler_tool_asset_ids(job).items():
        refs[kind] = list(dict.fromkeys([*refs.get(kind, []), *identifiers]))
    refs['skills'] = list(dict.fromkeys([*refs.get('skills', []), *_scheduler_skill_ids(job.get('skill_refs', []))]))
    for kind, asset_id in _target_refs(job):
        key = kind + 's'
        refs[key] = list(dict.fromkeys([*refs.get(key, []), asset_id]))
    metadata = binding.get('metadata')
    metadata = metadata if isinstance(metadata, dict) else {}
    metadata = {**metadata, 'alias': _alias(metadata.get('alias') or job.get('name') or identifier),
                'upgrade_policy': metadata.get('upgrade_policy') if metadata.get('upgrade_policy') in ('pinned', 'follow_compatible') else 'pinned',
                'trigger_matchers': normalize_trigger_matchers(metadata.get('trigger_matchers')),
                'last_run': _run_projection(job.get('_last_run')), 'asset_refs': refs}
    target = metadata.get('base_target')
    pin = metadata.get('pinned_version')
    if isinstance(target, dict) and type(pin) is int and pin > 0:
        latest = latest_version(home, target['kind'], target['asset_id'])
        metadata['version_projection'] = (f'当前 v{pin} · 最新 v{latest["version"]}'
                                          if latest and latest['version'] > pin else f'当前 v{pin}')
        if not _current_asset_exists(home, target):
            metadata['version_projection'] += ' · deleted'
    runtime = {'last_run_at': _run_time(job.get('last_run_at')), 'last_status': job.get('last_run_status', ''),
               'last_duration_ms': job.get('last_duration_ms'), 'run_count': job.get('run_count', 0),
               'consecutive_failures': job.get('consecutive_failures', 0), 'last_error': job.get('last_error') or ''}
    descriptor = AssetDescriptor(id=identifier, kind='monitor', name=binding.get('name') or job.get('name') or identifier,
        summary=binding.get('summary') or job.get('prompt', ''), version=str(job.get('version', binding.get('version', '1.0.0'))),
        owner_id=job.get('owner', {}).get('id', home.name), refs=refs, runtime_summary=runtime, revision=binding.get('revision', 0),
        metadata={**metadata, 'schedule_spec': schedule, 'scheduler_skill_refs': job.get('skill_refs', [])}, **{key: binding.get(key, ()) for key in
            ('access_targets', 'data_sources', 'credential_refs')}).to_dict()
    descriptor['metadata']['asset_refs'] = descriptor['refs']
    structured = [{'kind': kind, 'id': asset_id} for kind in ('workflow', 'script') for asset_id in refs.get(kind + 's', [])]
    return {**descriptor, 'refs': structured, 'schedule_spec': descriptor['metadata']['schedule_spec']}


# LLM: 绑定更新在监控锁内只读发布历史；默认固定当前编号，未显式升级不改旧pin。
# 函数用途: 冻结基础目标、升级策略及可选隐藏组合。
def _binding_pins(home, identifier, payload, bindings):
    metadata = dict(payload['metadata'])
    target = bindings.get('execution_target', metadata.get('base_target'))
    if 'execution_target' not in bindings:
        for kind, key in (('workflow', 'workflow_ids'), ('script', 'script_ids')):
            ids = payload.get('refs', {}).get(kind + 's', [])
            if key in bindings or target is None:
                if len(ids) == 1:
                    target = {'kind': kind, 'asset_id': safe_asset_id(ids[0])}
    if target is not None:
        if not isinstance(target, dict) or target.get('kind') not in ('workflow', 'script'):
            raise ValueError('monitor binding requires a structured asset target')
        target = {'kind': target['kind'], 'asset_id': safe_asset_id(target['asset_id'])}
    policy = bindings.get('upgrade_policy', metadata.get('upgrade_policy', 'pinned'))
    if policy not in ('pinned', 'follow_compatible'):
        raise ValueError('invalid monitor upgrade_policy')
    if metadata.get('base_target') != target:
        metadata.pop('pinned_version', None)
    metadata.update(base_target=target, upgrade_policy=policy)
    pin = bindings.get('pinned_version', metadata.get('pinned_version'))
    if target:
        latest = latest_version(home, target['kind'], target['asset_id'])
        pin = latest['version'] if pin is None and latest else pin
        if pin is not None and (type(pin) is not int or pin < 1 or get_version(home, target['kind'], target['asset_id'], pin) is None):
            raise ValueError('monitor pin must refer to a published version')
    elif pin is not None:
        raise ValueError('monitor pin requires an asset target')
    metadata['pinned_version'] = pin
    appended = bindings.get('appended_steps', metadata.get('appended_steps', []))
    if not isinstance(appended, list):
        raise ValueError('monitor appended_steps must be a list')
    metadata['appended_steps'] = appended
    if appended:
        if target is None or target['kind'] != 'workflow' or pin is None:
            raise ValueError('monitor composition requires a published workflow')
        from .monitor_composition import save_monitor_composition
        composition = save_monitor_composition(home, identifier, target['asset_id'], pin, appended)
        metadata.update(composition=composition['composition'], composition_version=composition['version'],
                        execution_target={'kind': 'workflow', 'asset_id': composition['workflow_id']})
    else:
        metadata.pop('composition', None)
        metadata.pop('composition_version', None)
        metadata['execution_target'] = target
    return metadata


# LLM: 只查询同owner当前定义，缺失只影响展示，不撤销仍有效的发布历史。
# 函数用途: 为固定版本监控显示原资产已删除的事实。
def _current_asset_exists(home, target):
    from .script_store import ScriptStore
    from .workflow_store import WorkflowStore
    store = WorkflowStore(home) if target['kind'] == 'workflow' else ScriptStore(home)
    return (store.get_workflow(target['asset_id']) if target['kind'] == 'workflow'
            else store.get_script(target['asset_id'])) is not None


# LLM: 别名是有界展示token，控制字符和空白不进入目录前缀，也不提供执行权限。
# 函数用途: 生成大小写无关、单token的监控别名。
def _alias(value):
    if not isinstance(value, str):
        return ''
    return ''.join(char for char in value.casefold() if not char.isspace() and not unicodedata.category(char).startswith('C'))[:32]


# LLM: 目标和v13规则引用只读准确kind/id，未知值作为缺失，不解释prompt。
# 函数用途: 从结构化调度及规则字段提取可展示的依赖。
def _target_refs(job):
    target = job.get('execution_target')
    candidates = [(target.get('kind'), target.get('asset_id'))] if isinstance(target, dict) else []
    candidates.extend((kind, job.get(kind + '_id')) for kind in ('script', 'workflow'))
    result = []
    for kind, identifier in candidates:
        if kind not in ('script', 'workflow') or not isinstance(identifier, str):
            continue
        try:
            result.append((kind, safe_asset_id(identifier)))
        except ValueError:
            continue
    return list(dict.fromkeys(result))


# LLM: 只从原scheduler集合按准确job_id挑选最新记录，坏类型忽略，无磁盘副作用。
# 函数用途: 选取一个监控的最新可观察运行。
def _latest_run(runs, identifier):
    if not isinstance(runs, dict):
        return None
    candidates = [row for row in runs.values() if isinstance(row, dict) and row.get('job_id') == identifier]
    return max(candidates, key=lambda row: float(row.get('ended_at') or row.get('finished_at') or row.get('started_at') or row.get('created_at') or 0), default=None)


# LLM: 已有SQLite投影只读打开并核原owner/源快照；不创建库、不回填、不取得会写锁文件的history读锁。
# 函数用途: 按job索引点查最近终态；旧未建索引账本只读有界尾部，不全扫。
def _archived_run(home, owner, identifier, *, tail_bytes=256 * 1024):
    from types import SimpleNamespace

    from ..scheduler.history_index import SchedulerHistoryIndex, _snapshot
    source = home / 'data' / 'scheduler' / 'history.jsonl'
    view = SimpleNamespace(path=source.with_suffix('.index.sqlite3'), owner=owner)
    try:
        with closing(SchedulerHistoryIndex._connect(view, read_only=True)) as connection:
            connection.execute('BEGIN')
            state = SchedulerHistoryIndex._state(view, connection)
            snapshot = _snapshot(source)
            if state.get('owner') == owner and all(state.get(key) == snapshot[key] for key in ('stamp', 'head', 'tail')):
                raw = connection.execute('SELECT payload FROM history_rows WHERE generation=? AND job_id=? '
                    'ORDER BY ended_at DESC,run_id DESC LIMIT 1', (state['generation'], identifier)).fetchone()
                if raw is not None and snapshot == _snapshot(source):
                    return json.loads(raw[0])
    except (FileNotFoundError, sqlite3.Error):
        pass
    try:
        descriptor = open_readonly_file_beneath(home, ('data', 'scheduler', 'history.jsonl'))
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, 'rb') as handle:
        start = max(0, os.fstat(handle.fileno()).st_size - tail_bytes)
        handle.seek(start)
        content = handle.read(tail_bytes)
    rows = {}
    for line in content.splitlines()[1 if start else 0:]:
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(row, dict) and row.get('owner') == owner and row.get('job_id') == identifier:
            rows[str(row.get('run_id'))] = row
    return _latest_run(rows, identifier)


# LLM: 运行结果只投影结构化状态；文字只做脱敏后的有界展示，不决定成功或失败。
# 函数用途: 生成最近运行的阶段、耗时和单行摘要。
def _run_projection(run):
    if not isinstance(run, dict):
        return None
    summary = run.get('result_summary', run.get('response', ''))
    summary = ' '.join(summary.split())[:120] if isinstance(summary, str) else ''
    finished = run.get('ended_at', run.get('finished_at'))
    duration = run.get('duration_ms')
    if duration is None and type(finished) in (int, float) and type(run.get('started_at')) in (int, float):
        duration = max(0, round((finished - run['started_at']) * 1000))
    return {'stage': run.get('stage') or ('finished' if finished else run.get('status', '')),
            'status': run.get('status', ''), 'duration_ms': duration, 'result_summary': summary,
            'finished_at': _run_time(finished)}


# LLM: Scheduler保存准确stable_id/hash对象，不能把dict当字符串ID；仅普通Skill转拓扑ID，包身份完整留在metadata而非伪造Skill边。
# 函数用途: 将已归一的任务Skill引用映射到技能资产，保留显式字符串绑定的调用兼容。
def _scheduler_skill_ids(references):
    identifiers = []
    for reference in references:
        if isinstance(reference, str):
            identifiers.append(safe_asset_id(reference))
            continue
        stable_id = reference['stable_id']
        if reference.get('kind') == 'capability_package' or stable_id.startswith('capability:'):
            continue
        identifiers.append(safe_asset_id(stable_id.partition(':')[2] if ':' in stable_id else stable_id))
    return identifiers


# LLM: 只读rules.tool/rules.precheck及顶层precheck的显式参数；不解释prompt，不执行动作，不安全ID沿原校验拒绝。
# 函数用途: 为定时工作流或脚本自动提取拓扑引用，缺失、空值或非字符串不构成资产边。
def _scheduler_tool_asset_ids(job):
    refs = {'workflows': [], 'scripts': []}
    actions = []
    for rule in job.get('rules') or ():
        actions.extend((rule.get('tool'), rule.get('precheck')))
    actions.append(job.get('precheck'))
    for action in actions:
        if not isinstance(action, dict):
            continue
        name = action.get('name', action.get('tool_name'))
        arguments = action.get('arguments')
        if name not in ('workflows', 'scripts') or not isinstance(arguments, dict):
            continue
        identifier = arguments.get('workflow_id' if name == 'workflows' else 'script_id')
        if isinstance(identifier, str) and identifier:
            refs[name].append(safe_asset_id(identifier))
    return refs
