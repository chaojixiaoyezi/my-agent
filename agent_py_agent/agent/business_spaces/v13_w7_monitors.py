"""监控规则、原生调度镜像及基于 W8 事实的检测指标。

规则和回执归 V13Store，执行归 SchedulerRepository。create_rule 的 origin
及 update_rule 的 exclusions 关键字供同片生命周期复用，不扩大公开 JSON schema。
原生镜像只消费已耐久提交的规则意图。待同步元数据与规则同存于原 Store，
同步与检查点之间中断沿原生 source_request_id 重试，不以旧回执覆盖当前规则。
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..assets.datasource_store import DatasourceStore
from ..assets.script_store import ScriptStore
from ..assets.script_store_support import delivery_scheduler
from ..assets.skill_adapter import get_owner_skill_descriptor
from ..common.directory_lock import locked_private_directory
from ..plugin_host.errors import HostAPIError
from ..plugin_host.schemas import canonical_json, thaw, validate_object
from ..scheduler.repository import (
    SchedulerConflictError,
    SchedulerJobCreateRequest,
    SchedulerNotFoundError,
)
from ..scheduler.schedule import ScheduleValidationError, validate_schedule


# LLM: 延迟导入避免 W0 静态注册器载入分片时形成循环。
# 函数用途: 为实时调用取得同 owner 的事务存储。
def _store(call):
    from .v13_store import v13_store_for
    return v13_store_for(call)


# LLM: 输入 owner_id 从不参与授权；每次复核实时 context。
# 函数用途: 核验调用或已读记录的归属。
def _access(call, row=None):
    from ..gateway_parts.business_api_v13 import v13_require_access
    return v13_require_access(call, row)


# LLM: 空间必须来自原冻结服务；缺省只选择本 owner 唯一的活动空间。
# 函数用途: 复用 W7 有界空间解析，不创建空间或凭名称猜默认值。
def _space(call, space_id=None):
    from .v13_w7_simulation_sessions import resolve_space
    return resolve_space(call, space_id)['space_id']


# LLM: 同片调用仍使用 HTTP 的闭合公开参数 schema。
# 函数用途: 校验参数而不要求调用对象的 action 改名。
def _params(action, params):
    from ..gateway_parts.business_api_v13 import v13_validate_params
    return thaw(v13_validate_params(action, params))


# LLM: 摘要只依据结构化值，包含记录 CAS 和内部选项。
# 函数用途: 为回执与原生关联生成稳定指纹。
def _digest(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


# LLM: 时间由注入时钟或准确事实提供，不使用展示文案。
# 函数用途: 把 epoch 秒转为 UTC 字符串。
def _iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


# LLM: nullable 时间不变成零点；坏格式或无时区保留未知。
# 函数用途: 解析事实 date-time 为 epoch 或 None。
def _epoch(value):
    if type(value) is not str:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


# LLM: 私有 owner 字段不能进入闭合 MonitorRule DTO。
# 函数用途: 投影规则的完整公开字段。
def _view(row):
    result = {key: deepcopy(value) for key, value in row.items() if key != 'owner_id' and not key.startswith('_')}
    if row.get('_scheduler_pending', {}).get('deleting') or row.get('schedule') is None:
        result['scheduler_job_id'] = None
    return result


# LLM: 记录 CAS 必须在持锁最新记录上比较，与集合 CAS 分离。
# 函数用途: 取得、授权规则并可选检查记录版本。
def _rule(call, records, rule_id, *, cas=False):
    row = records.get(rule_id)
    if row is None or row.get('_deleted'):
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    _access(call, row)
    if cas and (type(call.expected_revision) is not int or call.expected_revision != row['revision']):
        raise HostAPIError('REVISION_CONFLICT', 409)
    return row


# LLM: kind 决定相关字段，原生验证器负责时间与 cron 语义。
# 函数用途: 校验调度并返回无 anchor 的公开形状。
def _schedule(value):
    if value is None:
        return None
    try:
        kind = value['kind']
        if (kind == 'every' and 'expression' in value) or (kind == 'cron' and 'every_seconds' in value):
            raise ScheduleValidationError('计划字段与类型不一致')
        ZoneInfo(value.get('timezone', 'UTC'))
        native = dict(value)
        if kind == 'every':
            native['anchor_at'] = 0
        checked = validate_schedule(native)
        return {key: item for key, item in checked.items() if key != 'anchor_at'}
    except (ScheduleValidationError, ZoneInfoNotFoundError, KeyError, TypeError, ValueError) as error:
        raise HostAPIError('INVALID_SCHEMA') from error


# LLM: 引用检查只读，不测试连接、读取秘密或执行脚本。
# 函数用途: 核验本 owner 的脚本、Skill、数据源和 MITRE 引用。
def _references(call, row):
    try:
        if row.get('script_id') is not None and ScriptStore(call.owner_home).get_script(row['script_id']) is None:
            raise FileNotFoundError('script')
        for skill_id in row.get('skill_ids', []):
            get_owner_skill_descriptor(call.owner_home, skill_id)
        for datasource_id in row.get('datasource_ids', []):
            DatasourceStore(call.owner_home).get_datasource_spec(datasource_id)
    except (FileNotFoundError, KeyError) as error:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404) from error
    except ValueError as error:
        raise HostAPIError('INVALID_SCHEMA') from error
    if any(re.fullmatch(r'T[0-9]{4}(\.[0-9]{3})?', item) is None for item in row.get('mitre_techniques', [])):
        raise HostAPIError('INVALID_SCHEMA')


# LLM: 原冻结后台 agent 是唯一执行身份；与 W0 使用相同空间身份键。
# 函数用途: 在规则事务外取得或创建助手，避免重入集合目录锁。
def _assistant(call, space_id):
    from ..gateway_parts.business_api_v13 import v13_call_frozen
    store = _store(call)
    existing = store.get('assistant_agents', space_id)
    if existing is not None:
        _access(call, existing)
        return existing['agent_id']

    # LLM: 锁内重读防并发重复创建，原后台端口也持稳定幂等键。
    # 函数用途: 保存唯一助手的真实 agent_id。
    def mutate(records):
        row = records.get(space_id)
        if row is not None:
            _access(call, row)
            return row
        created = v13_call_frozen(call, 'background_agents.create',
            {'space_id': space_id, 'name': '空间助手', 'purpose': 'v13结构化异步工作', 'after_task': 'reuse'},
            idempotency_key='v13-assistant-' + _digest([call.owner_id, space_id])[:48])
        at = _iso(call.clock())
        row = {'space_id': space_id, 'owner_id': call.owner_id, 'agent_id': created['agent']['agent_id'],
               'purpose': 'v13', 'revision': 0, 'created_at': at, 'updated_at': at}
        records[space_id] = row
        return row

    return store.transaction('assistant_agents', mutate,
        idempotency_key='assistant-' + _digest(space_id)[:48], params_digest=_digest(space_id))['agent_id']


# LLM: prompt 包含完整结构化引用和排除项，但不承担机器路由与授权。
# 函数用途: 渲染原生调度或单次工作的检测定义。
def _instruction(row):
    fields = ('rule_id', 'space_id', 'name', 'severity', 'query', 'script_id',
              'skill_ids', 'datasource_ids', 'exclusions')
    return '按以下结构化规则使用固定脚本、已授权 Skill 与数据源执行检测；遵守排除项，不扩大权限。\n' + canonical_json(
        {key: row[key] for key in fields if key in row})


# LLM: 不静默截断查询或丢 Skill，原生限制在副作用前显式拒绝。
# 函数用途: 检查计划是否能无损表达该规则。
def _mirror_limits(row):
    if row.get('schedule') is not None and (len(row.get('skill_ids', [])) > 32 or len(_instruction(row)) > 32000):
        raise HostAPIError('INVALID_SCHEMA')


# LLM: job_id 只能关联本规则，存储污染不能变成控制其它计划的权限。
# 函数用途: 读取并核验调度镜像规范来源。
def _job(repo, row):
    if not row.get('scheduler_job_id'):
        return None
    try:
        job = repo.get_job(row['scheduler_job_id'], include_deleted=True)
    except SchedulerNotFoundError:
        return None
    if job['name'] != 'monitor:' + row['rule_id'] or not job.get('source_request_id', '').startswith(
            'v13-monitor-' + row['rule_id'] + '-'):
        raise HostAPIError('STATE_CONFLICT', 409)
    return job


# LLM: 创建沿原生幂等键；更新只写差异，中断重试不重复创建或漂移 anchor。
# 函数用途: 将规则收敛为唯一 active/paused 计划，或移除原计划。
def _mirror(call, row, agent_id=None, *, deleting=False):
    if not row.get('scheduler_job_id') and (deleting or row['schedule'] is None or not row['enabled']):
        return None
    _access(call, row)
    repo = delivery_scheduler(call.owner_home, call.owner_id)
    changed = False
    try:
        job = _job(repo, row)
        if deleting or row['schedule'] is None:
            if job is not None and job['status'] != 'deleted':
                repo.delete_job(job['job_id'], expected_version=job['version'], now=call.clock())
            return None
        if job is not None and job['status'] == 'deleted':
            job = None
        if job is None and not row['enabled']:
            return None
        native = dict(row['schedule'])
        if native['kind'] == 'every':
            native['anchor_at'] = _epoch(row['created_at'])
        desired = {'name': 'monitor:' + row['rule_id'], 'prompt': _instruction(row),
                   'schedule': validate_schedule(native), 'skill_refs': row['skill_ids']}
        if job is None:
            if not agent_id:
                raise HostAPIError('HOST_NOT_READY', 503)
            job, _replayed = repo.create_job(SchedulerJobCreateRequest(
                name=desired['name'], prompt=desired['prompt'], schedule=desired['schedule'],
                thread_id=agent_id, skill_refs=row['skill_ids'],
                source_request_id='v13-monitor-' + row['rule_id'] + '-' + str(row['revision']), now=call.clock()))
            changed = True
        patch = {key: value for key, value in desired.items() if job.get(key) != value}
        if patch:
            job = repo.update_job(job['job_id'], patch=patch, expected_version=job['version'], now=call.clock())
            changed = True
        if row['enabled'] and job['status'] != 'active':
            job = repo.resume_job(job['job_id'], expected_version=job['version'], now=call.clock())
            changed = True
        elif not row['enabled'] and job['status'] != 'paused':
            job = repo.pause_job(job['job_id'], expected_version=job['version'], now=call.clock())
            changed = True
        return job['job_id']
    except ScheduleValidationError as error:
        raise HostAPIError('INVALID_SCHEMA') from error
    except SchedulerConflictError as error:
        if changed:
            raise RuntimeError('原生调度已变更，当前提交结果未知；请同键重试') from error
        raise HostAPIError('REVISION_CONFLICT', 409) from error


# LLM: mutate 内只登记意图；原生任务身份尚未创建时不虚构 job_id。
# 函数用途: 将当前规则的同步要求纳入同笔权威提交。
def _stage_mirror(row, agent_id=None, *, deleting=False):
    if row.get('scheduler_job_id') or row['enabled'] and row['schedule'] is not None:
        row['_scheduler_pending'] = {'agent_id': agent_id, 'deleting': deleting}


# LLM: 持有镜像串行锁时只读取当前 Store；绝不从回执里的旧规则驱动原生写入。
# 函数用途: 同步已提交目标规则，并经原 Store 保存镜像身份或清除删除墓碑。
def _sync_rules(call, record_ids):
    store, changes, snapshots = _store(call), {}, {}
    for rule_id in dict.fromkeys(record_ids):
        row = store.get('monitor_rules', rule_id)
        if row is None or '_scheduler_pending' not in row:
            continue
        _access(call, row)
        pending = row['_scheduler_pending']
        try:
            job_id = _mirror(call, row, pending['agent_id'], deleting=pending['deleting'])
        except HostAPIError as error:
            raise RuntimeError('规则已提交，原生镜像同步未完成；请同键重试') from error
        snapshots[rule_id] = row
        if row.get('_deleted'):
            changes[rule_id] = None
        else:
            changed = {**row, 'scheduler_job_id': job_id}
            del changed['_scheduler_pending']
            changes[rule_id] = changed
    if not changes:
        return

    # LLM: 检查点不改业务 revision；锁外的非本模块写者也不能被旧快照覆盖。
    # 函数用途: 核验同步目标仍是同一版本后保存纯元数据结果。
    def checkpoint(records):
        for rule_id, row in snapshots.items():
            if records.get(rule_id) != row:
                raise RuntimeError('镜像目标已变化；请重试当前权威版本')
        for rule_id, row in changes.items():
            if row is None:
                del records[rule_id]
            else:
                records[rule_id] = row
        return {}

    store.action += ':scheduler-checkpoint'
    token = _digest([snapshots, changes])
    store.transaction('monitor_rules', checkpoint,
        idempotency_key='mirror-' + token, params_digest=token)


# LLM: 原始回执在创建任务前已提交，返回时只补充该回执版本的真实原生身份。
# 函数用途: 投影首次创建镜像的 job_id，旧键回放只读原生墓碑而不重新调度。
def _mirror_result(call, result):
    result = deepcopy(result)
    row = result.get('rule')
    if (type(row) is dict and row.get('enabled') and row.get('schedule') is not None
            and row.get('scheduler_job_id') is None):
        repo = delivery_scheduler(call.owner_home, call.owner_id)
        source = 'v13-monitor-' + row['rule_id'] + '-' + str(row['revision'])
        jobs, errors = repo.list_jobs(include_deleted=True)
        matches = [job for job in jobs if job.get('source_request_id') == source
                   and job['name'] == 'monitor:' + row['rule_id']]
        if errors or len(matches) != 1:
            raise RuntimeError('规则镜像身份尚未确认；请同键重试')
        row['scheduler_job_id'] = matches[0]['job_id']
    return result


# LLM: 回执包含记录 CAS；绝不把记录版本当集合 expected_revision。
# 函数用途: 串行业务提交与提交后原生镜像同步，恢复仅消费当前权威意图。
def _write(call, collection, mutate, extra=None, *, record_ids=(), commit=None):
    if collection != 'monitor_rules':
        return _commit(call, collection, mutate, extra, record_ids=record_ids, commit=commit)
    _access(call)
    with locked_private_directory(call.owner_home, relative_parts=('data', 'business_v13'),
                                  lock_name='.monitor-mirror.lock'):
        # 新版本提交前先完成此前已提交的意图，防止丢失尚未落盘的原生身份。
        _sync_rules(call, record_ids)
        result = _commit(call, collection, mutate, extra, record_ids=record_ids, commit=commit)
        _sync_rules(call, record_ids)
        return _mirror_result(call, result)


# LLM: 组合 commit 仍仅组合纯记录 mutate；生命周期的多集合提交点保持唯一。
# 函数用途: 复用原单集合或调用者提供的多集合 Store 事务。
def _commit(call, collection, mutate, extra=None, *, record_ids=(), commit=None):
    if commit is not None:
        if not callable(commit):
            raise HostAPIError('INVALID_SCHEMA')
        _access(call)
        return commit(mutate, extra)
    store, reader = _store(call), _store(call)
    original_authorize = store.authorize

    # LLM: 回执在 mutate 前返回，记录授权必须在每次事务准入与锁内末核重做。
    # 函数用途: 复核最新现存目标记录；删除后的缺记录不阻断原成功回放。
    def authorize():
        if original_authorize() is False:
            raise HostAPIError('CAPABILITY_DENIED', 403)
        for record_id in dict.fromkeys(record_ids):
            current = reader.get(collection, record_id)
            if current is not None:
                _access(call, current)
        return True

    store.authorize = authorize
    return store.transaction(collection, mutate, idempotency_key=call.idempotency_key,
        params_digest=_digest([call.params, call.expected_revision, extra]))


# LLM: 内部 origin/commit 只由受信同片调用提供；commit 组合原生 mutate，不嵌套 Store 锁。
# 函数用途: 耐久创建规则后同步并返回真实原生调度镜像身份。
def create_rule(call, *, origin='manual', commit=None):
    params = _params('monitors.rules.create', call.params)
    if origin not in {'manual', 'proposal', 'community', 'sample'}:
        raise HostAPIError('INVALID_SCHEMA')
    sid = _space(call, params.get('space_id'))
    rule_id = 'rule-' + _digest([call.owner_id, call.subject_id, call.action, call.idempotency_key])[:48]
    at = _iso(call.clock())
    row = {'rule_id': rule_id, 'space_id': sid, 'owner_id': call.owner_id, 'name': params['name'],
           'description': params.get('description', ''), 'enabled': params.get('enabled', False),
           'severity': params['severity'], 'query': params['query'], 'schedule': _schedule(params.get('schedule')),
           'script_id': params.get('script_id'), 'skill_ids': params.get('skill_ids', []),
           'datasource_ids': params.get('datasource_ids', []), 'mitre_techniques': params.get('mitre_techniques', []),
           'tags': params.get('tags', []), 'exclusions': [], 'origin': origin,
           'scheduler_job_id': None, 'last_run': None, 'revision': 1, 'created_at': at, 'updated_at': at}
    _mirror_limits(row)
    existing = _store(call).get('monitor_rules', rule_id)
    agent_id = None
    if existing is None:
        _references(call, row)
        if row['enabled'] and row['schedule'] is not None:
            agent_id = _assistant(call, sid)

    # LLM: 回执重放先于回调，冲突不会新建计划或重复提交记录。
    # 函数用途: 持锁创建规则和镜像意图，不产生原生调度副作用。
    def mutate(records):
        if rule_id in records:
            raise HostAPIError('STATE_CONFLICT', 409)
        _access(call, row)
        _references(call, row)
        _stage_mirror(row, agent_id)
        records[rule_id] = row
        return {'rule': _view(row)}

    return _write(call, 'monitor_rules', mutate, origin, record_ids=(rule_id,), commit=commit)


# LLM: 内部 exclusions/commit 供生命周期原子追加排除项，公开 JSON 不获得这些字段。
# 函数用途: 持锁记录 CAS 更新规则并同步启停、计划和提示词。
def update_rule(call, *, exclusions=None, commit=None):
    from ..gateway_parts.business_api_v13 import v13_schema
    params = _params('monitors.rules.update', call.params)
    if exclusions is not None:
        shape = {'type': 'object', 'properties': {'items': {'type': 'array', 'maxItems': 200,
                 'items': {'$ref': '#/$defs/V13RuleExclusion'}}}, 'required': ['items'], 'additionalProperties': False}
        exclusions = thaw(validate_object({'items': exclusions}, v13_schema(shape)))['items']
    old = _store(call).get('monitor_rules', params['rule_id'])
    agent_id = None
    if old is not None:
        _access(call, old)
        proposed = {**old, **params}
        if proposed['enabled'] and proposed['schedule'] is not None and old['revision'] == call.expected_revision:
            _references(call, proposed)
            _mirror_limits(proposed)
            agent_id = _assistant(call, _space(call, old['space_id']))

    # LLM: 不用锁外快照整条覆盖；以最新记录合并明确 patch 后再写。
    # 函数用途: 原子更新规则及镜像意图，记录缺失时仍允许 Store 优先回放回执。
    def mutate(records):
        latest = _rule(call, records, params['rule_id'], cas=True)
        _space(call, latest['space_id'])
        changed = {**latest, **params, 'revision': latest['revision'] + 1, 'updated_at': _iso(call.clock())}
        changed['schedule'] = _schedule(changed['schedule'])
        if exclusions is not None:
            changed['exclusions'] = exclusions
        _references(call, changed)
        _mirror_limits(changed)
        _stage_mirror(changed, agent_id)
        records[params['rule_id']] = changed
        return {'rule': _view(changed)}

    return _write(call, 'monitor_rules', mutate, exclusions, record_ids=(params['rule_id'],), commit=commit)


# LLM: 原生删除保留 tombstone 以支持崩溃后重试；公开记录删除和回执同笔提交。
# 函数用途: 以记录 CAS 删除规则及其原生计划。
def delete_rule(call):
    params = _params('monitors.rules.delete', call.params)

    # LLM: 缺记录检查在回调内，删除后的同键重放仍返回原成功结果。
    # 函数用途: 持锁核 CAS 并提交删除意图，原生计划稍后从耐久墓碑清理。
    def mutate(records):
        row = _rule(call, records, params['rule_id'], cas=True)
        _space(call, row['space_id'])
        if row.get('scheduler_job_id'):
            row = {**row, '_deleted': True, 'revision': row['revision'] + 1}
            _stage_mirror(row, deleting=True)
            records[params['rule_id']] = row
        else:
            del records[params['rule_id']]
        return {'rule_id': params['rule_id'], 'deleted': True}

    return _write(call, 'monitor_rules', mutate, record_ids=(params['rule_id'],))


# LLM: 整批只写一笔集合事务；同一 ID 重复出现不重复递增版本或操作原生计划。
# 函数用途: 批量启停规则，缺失/其它空间规则返回逐条 RESOURCE_NOT_FOUND。
def bulk_toggle(call):
    params = _params('monitors.rules.bulk_toggle', call.params)
    sid = _space(call, params['space_id']) if 'space_id' in params else None
    agents = {}
    if params['enabled']:
        for row in _store(call).list('monitor_rules'):
            if (row.get('rule_id') in params['rule_ids'] and row.get('owner_id') == call.owner_id
                    and (sid is None or row.get('space_id') == sid) and row.get('schedule') is not None):
                _access(call, row)
                _references(call, row)
                _mirror_limits(row)
                agents[row['space_id']] = _assistant(call, _space(call, row['space_id']))

    # LLM: 全部规则与镜像意图同笔提交，原生副作用只能在提交后发生。
    # 函数用途: 一笔提交批量规则与逐条结果。
    def mutate(records):
        outcomes, changes = {}, {}
        for rule_id in dict.fromkeys(params['rule_ids']):
            row = records.get(rule_id)
            if row is None or row.get('owner_id') != call.owner_id or (sid is not None and row['space_id'] != sid):
                outcomes[rule_id] = {'rule_id': rule_id, 'enabled': None, 'revision': None,
                                     'error_code': 'RESOURCE_NOT_FOUND'}
                continue
            _access(call, row)
            _space(call, row['space_id'])
            updated = {**row}
            if row['enabled'] != params['enabled']:
                updated.update(enabled=params['enabled'], revision=row['revision'] + 1, updated_at=_iso(call.clock()))
                _references(call, updated)
                _mirror_limits(updated)
            changes[rule_id] = updated
        for rule_id, row in changes.items():
            _stage_mirror(row, agents.get(row['space_id']))
            records[rule_id] = row
            outcomes[rule_id] = {'rule_id': rule_id, 'enabled': row['enabled'],
                                 'revision': row['revision'], 'error_code': None}
        return {'results': [outcomes[rule_id] for rule_id in params['rule_ids']]}

    return _write(call, 'monitor_rules', mutate, record_ids=params['rule_ids'])


# LLM: 只返回当前 owner 且当前授权的记录，不从空结果推断其它 owner 无数据。
# 函数用途: 读取并按可选空间筛选规则。
def _rules(call):
    _access(call)
    sid = _space(call, call.params['space_id']) if 'space_id' in call.params else None
    rows = []
    for row in _store(call).list('monitor_rules'):
        if row.get('_deleted') or row.get('owner_id') != call.owner_id or (sid is not None and row.get('space_id') != sid):
            continue
        _access(call, row)
        rows.append(row)
    return rows


# LLM: 复用 W0 的 HMAC 分页；绑定排序、筛选、身份与源修订而非只截列表。
# 函数用途: 为派生视图生成不可跨范围复用的分页结果。
def _page(call, rows, binding):
    from .v13_store import _page_rows
    filters = {key: value for key, value in call.params.items() if key not in {'cursor', 'limit'}}
    return _page_rows(rows, [str(call.owner_home), call.owner_id, call.subject_id, call.action, filters, binding, rows],
        cursor=call.params.get('cursor'), limit=call.params.get('limit', 50))


# LLM: 标量精确过滤与数组成员过滤分开，所有筛选参与游标绑定。
# 函数用途: 列出规则，支持原 issue 示例、标签及 MITRE 等筛选。
def list_rules(call):
    rows = _rules(call)
    for key in ('enabled', 'severity', 'origin'):
        if key in call.params:
            rows = [row for row in rows if row[key] == call.params[key]]
    for key, field in (('tag', 'tags'), ('mitre_technique', 'mitre_techniques')):
        if key in call.params:
            rows = [row for row in rows if call.params[key] in row[field]]
    return _page(call, [_view(row) for row in rows], _store(call).revision('monitor_rules'))


# LLM: 不存在和其它 owner 均为 404，不以空对象成功掩盖缺失。
# 函数用途: 返回一条完整规则。
def get_rule(call):
    row = _rule(call, {call.params['rule_id']: _store(call).get('monitor_rules', call.params['rule_id'])},
                call.params['rule_id'])
    _space(call, row['space_id'])
    return {'rule': _view(row)}


# LLM: schema 的 Timestamp 可空，但执行窗口必须两端有真实时间且严格递增。
# 函数用途: 校验单次检测或回测的时间窗口。
def _window(value):
    since, until = _epoch(value.get('since')), _epoch(value.get('until'))
    if since is None or until is None or since >= until:
        raise HostAPIError('INVALID_SCHEMA')
    return value


# LLM: 单次测试不写规则；仅经 W0 原生异步工作路径提交模型任务。
# 函数用途: 对规则 ID 或草稿恰选其一执行单次试运行。
def test_rule(call):
    from ..gateway_parts.business_api_v13 import v13_start_model_job
    params = _params('monitors.rules.test', call.params)
    if ('rule_id' in params) == ('rule' in params):
        raise HostAPIError('INVALID_SCHEMA')
    if 'rule_id' in params:
        row = _store(call).get('monitor_rules', params['rule_id'])
        row = _rule(call, {params['rule_id']: row}, params['rule_id'])
        sid = _space(call, row['space_id'])
        if 'space_id' in params and params['space_id'] != sid:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    else:
        sid = _space(call, params.get('space_id'))
        row = {**params['rule'], 'space_id': sid, 'exclusions': []}
    _references(call, row)
    window = _window(params['window']) if 'window' in params else None
    instruction = (_instruction(row) + '\n单次试运行，不修改规则。窗口=' + canonical_json(window)
        + '\n只输出 JSON: {"matched":整数或null,"sample_matches":对象数组,"errors":字符串数组,'
          '"coverage":"complete/partial/unknown"}。没有来源证据的数字必须为 null。')
    return v13_start_model_job(call, 'monitor_rule_test', instruction, space_id=sid)


# LLM: 历史回测使用准确窗口，结果由异步作业 schema 校验，不捏造同步完成。
# 函数用途: 对已存在规则提交历史窗口回测。
def backtest_rule(call):
    from ..gateway_parts.business_api_v13 import v13_start_model_job
    params = _params('monitors.rules.backtest', call.params)
    window = _window(params['window'])
    row = _rule(call, {params['rule_id']: _store(call).get('monitor_rules', params['rule_id'])}, params['rule_id'])
    sid = _space(call, row['space_id'])
    _references(call, row)
    instruction = (_instruction(row) + '\n历史回测，不修改规则。窗口=' + canonical_json(window)
        + '\n只输出 JSON: {"window":上述窗口,"evaluated_events":整数或null,"matches":整数或null,'
          '"estimated_alerts_per_day":数字或null,"sample_matches":对象数组,"coverage":"complete/partial/unknown"}。'
          '没有来源证据的数字必须为 null。')
    return v13_start_model_job(call, 'monitor_rule_backtest', instruction, space_id=sid)


# LLM: 技术集合只来自实际规则；未声明安装目录时不捏造 MITRE 总数或战术映射。
# 函数用途: 统计每个技术对应规则和启用数，未知分母保持 null。
def coverage(call):
    grouped = {}
    for row in _rules(call):
        for technique_id in set(row['mitre_techniques']):
            grouped.setdefault(technique_id, []).append(row)
    techniques = []
    for technique_id, rules in sorted(grouped.items()):
        techniques.append({'technique_id': technique_id, 'tactic': None,
            'rule_ids': sorted(row['rule_id'] for row in rules)[:100],
            'enabled_rule_count': sum(row['enabled'] is True for row in rules)})
    covered = sum(item['enabled_rule_count'] > 0 for item in techniques)
    return {'framework': 'mitre', 'techniques': techniques[:1000], 'covered_technique_count': covered,
            'total_known': None, 'coverage_ratio': None, 'coverage': 'partial'}


# LLM: 聚合前剔除缺字段、重复身份和明显无效的源记录，不静默累加脏数据。
# 函数用途: 读取本 owner 的 W8/基线事实，返回脏数据标记并可选保留剔除项供归属缺口。
def _source_rows(call, collection, id_field, *, space_id=None, rejected=None):
    required = {
        'soc_alerts': ('title', 'severity', 'status', 'rule_id', 'created_at', 'assignee',
                       'acknowledged_at', 'resolved_at'),
        'soc_feedback': ('alert_id', 'rule_id', 'override_verdict', 'created_at'),
        'soc_cases': ('severity', 'status', 'created_at', 'closed_at'),
        'detection_baselines': ('metric', 'value', 'rule_id', 'window', 'created_at'),
        'monitor_sla_policies': ('policies', 'revision'),
    }[collection]
    selected, dirty = [], False

    # LLM: 剔除事实仍需携带原归属，不能让空有效集合冒充完整零样本。
    # 函数用途: 标记源数据缺口并按需记录被剔除的事实。
    def reject(row):
        nonlocal dirty
        dirty = True
        if rejected is not None:
            rejected.append(row)

    for row in _store(call).list(collection):
        if row.get('owner_id') != call.owner_id:
            continue
        if space_id is not None and row.get('space_id') != space_id:
            continue
        if (type(row.get(id_field)) is not str or not row[id_field]
                or type(row.get('space_id')) is not str or not row['space_id']
                or any(key not in row for key in required)):
            reject(row)
            continue
        if collection in {'soc_alerts', 'soc_cases'}:
            states = ({'new', 'acknowledged', 'in_progress', 'resolved', 'closed'} if collection == 'soc_alerts'
                      else {'open', 'investigating', 'contained', 'resolved', 'closed'})
            if (type(row['severity']) is not str or type(row['status']) is not str
                    or row['severity'] not in {'info', 'low', 'medium', 'high', 'critical'} or row['status'] not in states):
                reject(row)
                continue
        if ('rule_id' in required and row['rule_id'] is not None and type(row['rule_id']) is not str
                or collection == 'soc_alerts' and (type(row['title']) is not str
                    or row['assignee'] is not None and type(row['assignee']) is not str)
                or collection == 'soc_feedback' and (type(row['alert_id']) is not str
                    or type(row['override_verdict']) is not str)):
            reject(row)
            continue
        if collection == 'detection_baselines':
            if (type(row['value']) not in {int, float} or not math.isfinite(row['value']) or row['value'] < 0
                    or type(row['metric']) is not str or type(row['window']) is not dict
                    or _epoch(row['window'].get('since')) is None or _epoch(row['window'].get('until')) is None
                    or _epoch(row['window']['since']) >= _epoch(row['window']['until'])):
                reject(row)
                continue
        _access(call, row)
        selected.append(row)
    counts = Counter(row[id_field] for row in selected)
    for row in selected:
        if counts[row[id_field]] > 1:
            reject(row)
    return [row for row in selected if counts[row[id_field]] == 1], dirty


# LLM: 最终反馈按 alert 去重，旧改判不会继续作为当前分母；只用 override_verdict。
# 函数用途: 将真实反馈关联到同规则、同空间的实际告警。
def _latest_feedback(alerts, feedback):
    by_alert = {row['alert_id']: row for row in alerts}
    latest, dirty = {}, False
    for row in feedback:
        alert = by_alert.get(row['alert_id'])
        at = _epoch(row['created_at'])
        if alert is None or at is None or row['space_id'] != alert['space_id'] or row['rule_id'] != alert['rule_id']:
            dirty = True
            continue
        key = (at, row['override_id'])
        if row['alert_id'] not in latest or key > latest[row['alert_id']][0]:
            latest[row['alert_id']] = (key, row)
    return [item[1] for item in latest.values()], dirty


# LLM: 没有真/误报证据的 confidence 为 null，不使用模型分数或虚构样本。
# 函数用途: 计算各规则的告警数和最终反馈可信比例。
def confidence(call):
    rules = _rules(call)
    if 'rule_id' in call.params:
        rules = [row for row in rules if row['rule_id'] == call.params['rule_id']]
        if not rules:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    alerts, bad_alerts = _source_rows(call, 'soc_alerts', 'alert_id')
    feedback, bad_feedback = _source_rows(call, 'soc_feedback', 'override_id')
    feedback, bad_join = _latest_feedback(alerts, feedback)
    result = []
    for rule in rules:
        selected = [row for row in alerts if row['rule_id'] == rule['rule_id'] and row['space_id'] == rule['space_id']]
        verdicts = [row['override_verdict'] for row in feedback
                    if row['rule_id'] == rule['rule_id'] and row['space_id'] == rule['space_id']]
        tp, fp = verdicts.count('true_positive'), verdicts.count('false_positive')
        result.append({'rule_id': rule['rule_id'], 'alerts_total': len(selected), 'true_positive': tp,
                       'false_positive': fp, 'confidence': tp / (tp + fp) if tp + fp else None})
    has_evidence = [row['confidence'] is not None for row in result]
    state = ('unknown' if result and not any(has_evidence) else 'partial'
             if bad_alerts or bad_feedback or bad_join or not all(has_evidence) or len(result) > 1000 else 'complete')
    return {'rules': result[:1000], 'coverage': state}


# LLM: 基线仅接受该规则的非负有限 rate；全空间基线不能冒充每条规则基线。
# 函数用途: 选择最新有效的每规则 alerts_per_day 基线。
def _baseline_rate(rows, rule, now):
    matches = []
    for row in rows:
        value, at = row['value'], _epoch(row['created_at'])
        if (row['space_id'] == rule['space_id'] and row['rule_id'] == rule['rule_id']
                and row['metric'] == 'alerts_per_day' and type(value) in {int, float}
                and math.isfinite(value) and value >= 0 and at is not None and at <= now
                and _epoch(row['window']['until']) <= now):
            matches.append((at, row['baseline_id'], value))
    return max(matches)[2] if matches else None


# LLM: 明确无规则的 null 不污染规则；归属字段缺失或无效时只在已知空间/规则范围内保守判未知。
# 函数用途: 判断被剔除的告警是否可能属于当前规则。
def _has_source_gap(rejected, rule):
    for row in rejected:
        sid, rid = row.get('space_id'), row.get('rule_id')
        if type(sid) is str and sid and sid != rule['space_id']:
            continue
        if 'rule_id' in row and (rid is None or type(rid) is str) and rid != rule['rule_id']:
            continue
        return True
    return False


# LLM: 等长前窗必须有实际观察；缺基线/零分母不制造比例，未来告警不进入当前率。
# 函数用途: 从真实告警窗口与历史基线计算检测漂移。
def drift(call):
    rules, now = _rules(call), call.clock()
    days = call.params.get('window_days', 7)
    duration = days * 86400
    rejected = []
    alerts, dirty = _source_rows(call, 'soc_alerts', 'alert_id', rejected=rejected)
    baselines, bad_baselines = _source_rows(call, 'detection_baselines', 'baseline_id')
    result = []
    for rule in rules:
        selected = [row for row in alerts if row['rule_id'] == rule['rule_id'] and row['space_id'] == rule['space_id']]
        times = [_epoch(row['created_at']) for row in selected]
        incomplete = any(at is None for at in times) or _has_source_gap(rejected, rule)
        dirty = dirty or incomplete or any(at is not None and at > now for at in times)
        current = None if incomplete else sum(now - duration <= at <= now for at in times) / days
        previous = sum(at is not None and now - duration * 2 <= at < now - duration for at in times)
        baseline = _baseline_rate(baselines, rule, now)
        if baseline is None and previous and not incomplete:
            baseline = previous / days
        ratio = current / baseline if current is not None and baseline is not None and baseline > 0 else None
        result.append({'rule_id': rule['rule_id'], 'baseline_rate': baseline, 'current_rate': current,
            'drift_ratio': ratio, 'status': 'unknown' if ratio is None else
            'drifting' if abs(ratio - 1) >= 0.5 else 'stable'})
    unknown = any(row['status'] == 'unknown' for row in result)
    state = ('unknown' if result and all(row['status'] == 'unknown' for row in result) else
             'partial' if dirty or bad_baselines or unknown or len(result) > 1000 else 'complete')
    return {'rules': result[:1000], 'coverage': state}


# LLM: SLA 目标完全由真实策略决定；同严重度重复会导致歧义，必须拒绝。
# 函数用途: 校验策略数组并复制其闭合投影。
def _policies(values):
    from ..gateway_parts.business_api_v13 import v13_schema
    shape = {'type': 'object', 'properties': {'items': {'type': 'array', 'maxItems': 5,
             'items': {'$ref': '#/$defs/V13SlaPolicy'}}}, 'required': ['items'], 'additionalProperties': False}
    rows = thaw(validate_object({'items': values}, v13_schema(shape)))['items']
    if len({row['severity'] for row in rows}) != len(rows):
        raise HostAPIError('INVALID_SCHEMA')
    return rows


# LLM: 首次写入记录 CAS 为零；集合中其它空间的提交不影响本空间记录 CAS。
# 函数用途: 配置空间真实 SLA 策略并保存耐久回执。
def set_sla(call):
    params = _params('monitors.sla.set', call.params)
    sid, values = _space(call, params.get('space_id')), _policies(params['policies'])

    # LLM: 回执检查先于回调；CAS 和合并都基于锁内最新记录。
    # 函数用途: 一笔提交一个空间的 SLA 策略。
    def mutate(records):
        previous = records.get(sid)
        if previous is not None:
            _access(call, previous)
        revision = previous['revision'] if previous else 0
        if type(call.expected_revision) is not int or call.expected_revision != revision:
            raise HostAPIError('REVISION_CONFLICT', 409)
        at = _iso(call.clock())
        records[sid] = {'space_id': sid, 'owner_id': call.owner_id, 'policies': values,
            'revision': revision + 1, 'created_at': previous['created_at'] if previous else at, 'updated_at': at}
        return {'policies': values, 'revision': revision + 1}

    return _write(call, 'monitor_sla_policies', mutate, record_ids=(sid,))


# LLM: 无策略或未配置阶段目标时保持未知，不为页面填默认 SLA。
# 函数用途: 读取并验证空间 SLA 策略。
def _policy_row(call, sid):
    row = _store(call).get('monitor_sla_policies', sid)
    if row is None:
        return []
    _access(call, row)
    return _policies(row['policies'])


# LLM: 完成时间、待处理与缺失时间分别处理，不能把终态缺时间伪装成未处理。
# 函数用途: 计算一个阶段的实际截止时间和是否违约，返回坏数据标记。
def _deadline(row, minutes, stage, now, *, case=False):
    if minutes is None:
        return None, False, False
    created = _epoch(row['created_at'])
    if created is None or created > now:
        return None, False, True
    due = created + minutes * 60
    end_field = 'closed_at' if case else 'acknowledged_at' if stage == 'ack' else 'resolved_at'
    end = _epoch(row.get(end_field))
    terminal = row['status'] in {'resolved', 'closed'} or (stage == 'ack' and row['status'] != 'new')
    if row.get(end_field) is not None and end is None:
        return due, False, True
    if end is None and terminal:
        return due, False, True
    if end is not None and (end < created or end > now):
        return due, False, True
    return due, (now if end is None else end) > due, False


# LLM: breaches 只含真实违约；不存在策略返回明确 unknown，不虚构目标。
# 函数用途: 根据 W8 告警/事件时间和空间策略计算 ack/resolve 违约。
def sla(call):
    sid, now = _space(call, call.params.get('space_id')), call.clock()
    policies = [row for row in _policy_row(call, sid)
                if 'severity' not in call.params or row['severity'] == call.params['severity']]
    if not policies:
        return {'policies': [], 'breaches': [], 'coverage': 'unknown'}
    by_severity = {row['severity']: row for row in policies}
    alerts, dirty = _source_rows(call, 'soc_alerts', 'alert_id', space_id=sid)
    cases, bad_cases = _source_rows(call, 'soc_cases', 'case_id', space_id=sid)
    result = []
    for is_case, rows in ((False, alerts), (True, cases)):
        for row in rows:
            if 'severity' in call.params and row['severity'] != call.params['severity']:
                continue
            policy = by_severity.get(row['severity'])
            if policy is None:
                dirty = True
                continue
            for stage in (('resolve',) if is_case else ('ack', 'resolve')):
                due, breached, bad = _deadline(row, policy[stage + '_minutes'], stage, now, case=is_case)
                dirty = dirty or bad
                if breached:
                    result.append({'alert_id': None if is_case else row['alert_id'],
                        'case_id': row['case_id'] if is_case else None, 'severity': row['severity'],
                        'due_at': _iso(due), 'breached': True, 'stage': stage})
    result.sort(key=lambda row: (row['due_at'], row['alert_id'] or row['case_id'], row['stage']))
    return {'policies': policies, 'breaches': result[:500],
            'coverage': 'partial' if dirty or bad_cases or len(result) > 500 else 'complete'}


# LLM: 告警源没有 sla_due_at 字段，必须由真实 created_at、阶段与策略推导。
# 函数用途: 返回 open 告警，按实际 SLA 截止时间与创建时间排序后安全分页。
def queue(call):
    sid = _space(call, call.params['space_id']) if 'space_id' in call.params else None
    alerts, dirty = _source_rows(call, 'soc_alerts', 'alert_id', space_id=sid)
    policies, items, known = {}, [], True
    for row in alerts:
        if row['status'] not in {'new', 'acknowledged', 'in_progress'}:
            continue
        if any(row[key] != call.params[key] for key in ('status', 'severity') if key in call.params):
            continue
        if row['space_id'] not in policies:
            policies[row['space_id']] = {item['severity']: item for item in _policy_row(call, row['space_id'])}
        policy = policies[row['space_id']].get(row['severity'])
        stage = 'ack' if row['status'] == 'new' and row['acknowledged_at'] is None else 'resolve'
        due = None
        if policy is not None:
            due, _breached, bad = _deadline(row, policy[stage + '_minutes'], stage, call.clock())
            dirty = dirty or bad
        known = known and due is not None
        items.append({key: row[key] for key in ('alert_id', 'title', 'severity', 'status', 'created_at',
                      'assignee', 'rule_id')} | {'sla_due_at': _iso(due) if due is not None else None})
    items.sort(key=lambda row: (_epoch(row['sla_due_at']) if row['sla_due_at'] is not None else float('inf'),
                               _epoch(row['created_at']) if _epoch(row['created_at']) is not None else float('inf'),
                               row['alert_id']))
    store = _store(call)
    page = _page(call, items, [store.revision('soc_alerts'), store.revision('monitor_sla_policies')])
    if dirty or not known:
        page['coverage'] = 'partial' if items else 'unknown'
    return page
