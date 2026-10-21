# LLM: 监控仅读Scheduler事实及显式资产ID，保存独立绑定，不执行引用动作或读取凭据；公开值统一过AssetDescriptor。
# 模块用途: 提供监控标准Key，自动合并规则/预检的工作流脚本拓扑边，扩展绑定仍存data/monitors/<id>.json。
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import (
    list_names_beneath,
    open_directory_beneath,
    read_bytes_beneath,
    write_text_atomic_beneath,
)
from . import safe_asset_id
from .descriptor import AssetDescriptor


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


# LLM: 显式参数集合保留任务给定的全部关键词；用bindings聚合以守住六参数门，未知键写前拒绝。
# 函数用途: 原子保存监控绑定；此入口不修改Scheduler，未给出的绑定按空值保存。
def save_monitor_binding(owner_home, monitor_id, *, name='', summary='', schedule_spec=None, **bindings):
    home, identifier = Path(owner_home).absolute(), safe_asset_id(monitor_id)
    allowed = {'workflow_ids', 'script_ids', 'skill_ids', 'access_targets', 'data_sources', 'credential_refs'}
    if bindings.keys() - allowed:
        raise TypeError('监控绑定包含未知字段')
    refs = {kind: list(dict.fromkeys(bindings.get(key, ()))) for kind, key in
            (('workflows', 'workflow_ids'), ('scripts', 'script_ids'), ('skills', 'skill_ids'))}
    payload = {'name': name, 'summary': summary, 'schedule_spec': schedule_spec or {}, 'refs': refs,
               **{key: bindings.get(key, ()) for key in ('access_targets', 'data_sources', 'credential_refs')}}
    descriptor = _monitor_dict(home, identifier, payload, {})
    parts = ('data', 'monitors', identifier + '.json')
    home.mkdir(parents=True, exist_ok=True)
    os.close(open_directory_beneath(home, ('data', 'monitors'), create=True))
    with locked_json_path(home / 'data' / 'monitors' / (identifier + '.json')):
        _read_object(home, parts)
        write_text_atomic_beneath(home, parts, json.dumps(descriptor, ensure_ascii=False, allow_nan=False) + '\n')
    return descriptor


# LLM: scheduler_job显式传入优先；缺省仅读本Owner canonical store，不猜路径或创建仓库实例。
# 函数用途: 从Scheduler集合取得Job记录，调用方自行处理删除状态。
def _jobs(owner_home, scheduler_jobs=()):
    if scheduler_jobs:
        return list(scheduler_jobs)
    store = _read_object(owner_home, ('data', 'scheduler', 'store.json')) or {}
    jobs = store.get('jobs', {})
    if not isinstance(jobs, dict):
        raise ValueError('Scheduler jobs必须是对象')
    return list(jobs.values())


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
    return _monitor_dict(home, identifier, binding or {}, scheduler_job or {})


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
              'enabled': job.get('status') == 'active' if job else source.get('enabled', False)}
    if job.get('rules') is not None:
        result.update(kind='composite', expression='', rules=[{'id': rule['id'], **_schedule(
            {'schedule': rule['schedule'], 'next_run_at': rule.get('next_run_at'),
             'status': 'active' if rule['enabled'] and job.get('status') == 'active' else 'paused'}, {})}
            for rule in job['rules']])
    if type(result['enabled']) is not bool:
        raise ValueError('监控enabled必须是布尔值')
    return result


# LLM: AssetDescriptor仍负责类型/秘密过滤；引用按绑定优先再原规则顺序去重，规则只提供边而不授予执行权。
# 函数用途: 合并监控绑定和Scheduler自动引用，保留调度/运行摘要的唯一权威与展示扩展。
def _monitor_dict(home, identifier, binding, job):
    schedule = _schedule(job, binding)
    refs = dict(binding.get('refs', {}))
    for kind, identifiers in _scheduler_tool_asset_ids(job).items():
        refs[kind] = list(dict.fromkeys([*refs.get(kind, []), *identifiers]))
    refs['skills'] = list(dict.fromkeys([*refs.get('skills', []), *_scheduler_skill_ids(job.get('skill_refs', []))]))
    runtime = {'last_run_at': _run_time(job.get('last_run_at')), 'last_status': job.get('last_run_status', ''),
               'last_duration_ms': job.get('last_duration_ms'), 'run_count': job.get('run_count', 0),
               'consecutive_failures': job.get('consecutive_failures', 0), 'last_error': job.get('last_error') or ''}
    descriptor = AssetDescriptor(id=identifier, kind='monitor', name=binding.get('name') or job.get('name') or identifier,
        summary=binding.get('summary') or job.get('prompt', ''), version=str(job.get('version', binding.get('version', '1.0.0'))),
        owner_id=job.get('owner', {}).get('id', home.name), refs=refs, runtime_summary=runtime,
        metadata={'schedule_spec': schedule, 'scheduler_skill_refs': job.get('skill_refs', [])}, **{key: binding.get(key, ()) for key in
            ('access_targets', 'data_sources', 'credential_refs')}).to_dict()
    return {**descriptor, 'schedule_spec': descriptor['metadata']['schedule_spec']}


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
