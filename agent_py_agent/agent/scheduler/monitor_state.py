# LLM: 复合监控仍保存于原 scheduler job/runs；本模块只处理纯状态，车道、配额、领取、恢复和历史由原仓库负责。
# 模块用途: 将同一监控的独立规则展开为原运行快照并合回唯一账本，避免长规则阻塞其它规则。
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from .rules import normalize_rules
from .schedule import ScheduleValidationError

_RUNTIME_FIELDS = frozenset({'next_run_at', 'last_run_at', 'last_run_status', 'last_error_code',
    'run_count', 'consecutive_failures', 'backoff_until', 'precheck_checkpoint', 'result', 'definition_generation'})
_ACTION_FIELDS = frozenset({'prompt', 'schedule', 'execution', 'execution_target', 'precheck', 'policy'})
_DEFINITION_FIELDS = ('schedule', 'prompt', 'use_llm', 'tool', 'execution', 'execution_target',
    'precheck', 'policy', 'delivery', 'enabled', 'depends_on', 'condition', 'skill_refs',
    'misfire_grace_seconds', 'notification_binding')


# LLM: 事件规则唯一权威仍是原 event_source_state.generation；普通旧记录缺字段显式适配为初始代。
# 函数用途: 统一读取 job/rule 定义身份，拒绝损坏计数，不使用 version 或 claim_epoch。
def definition_generation(view):
    if (view.get('schedule') or {}).get('kind') == 'event_source':
        state = (view.get('event_source_state') or {}).get(str(view.get('rule_id') or view.get('id') or '')) or {}
        value = state.get('generation', 1)
    else:
        value = view.get('definition_generation', 1)
    if type(value) is not int or value < 1:
        raise ScheduleValidationError('规则定义代次无效')
    return value


# LLM: 只生成读取适配，不写磁盘；保留原事件 state 的事务内引用，副作用 lease 必须仍写原 store，不能复制脱账。
# 函数用途: 让旧时间/监控记录明确拥有初始定义身份。
def adapt_definition_generations(job):
    result = dict(job)
    if result.get('rules'):
        result['rules'] = [dict(rule) for rule in result['rules']]
    for view in result.get('rules') or [result]:
        if (view.get('schedule') or {}).get('kind') != 'event_source':
            view['definition_generation'] = definition_generation(view)
    return result


# LLM: 原事件同步负责退休/取消语义，本函数仅为未覆盖的定义字段推进同一个事件代次；普通规则各自维护唯一代次。
# 函数用途: 在原仓库写事务内同步新旧定义身份，名称和运行统计不影响定义。
def sync_definition_generations(job, previous=None, *, retire=False):
    from .event_sources import sync_event_sources

    result = sync_event_sources(job, previous, retire=retire)
    old_views = ({r['id']: _view(previous, r) for r in previous['rules']} if previous and previous.get('rules')
                 else {'': previous} if previous else {})
    definitions = result.get('rules') or [result]
    retired = deepcopy((previous or {}).get('definition_generation_tombstones') or {})
    active_keys = {str(rule.get('id') or '') for rule in definitions}
    for key, old_view in old_views.items():
        if key and key not in active_keys and (old_view.get('schedule') or {}).get('kind') != 'event_source':
            retired[key] = definition_generation(old_view) + 1
    for rule in definitions:
        key = str(rule.get('id') or '') if result.get('rules') else ''
        view = _view(result, rule) if result.get('rules') else result
        old = old_views.get(key)
        # 单规则迁入 rule1 时沿用原定义身份，不重置为 1。
        if old is None and key == 'rule1' and previous and not previous.get('rules'):
            old = previous
        changed = bool(old and (any(view.get(k) != old.get(k) for k in _DEFINITION_FIELDS)
                                or result['status'] != previous['status']))
        seed = max(retired.pop(key, 1), int((result.get('event_source_state', {}).get(key) or {}).get('generation', 1)))
        generation = max(seed, definition_generation(old) + int(changed)) if old else seed
        if (view.get('schedule') or {}).get('kind') == 'event_source':
            state = result['event_source_state'][key]
            # 跨类型与删除重建也必须承接旧代下界；最终只写原事件权威计数。
            if state['generation'] < generation:
                state.update(generation=generation, pending=[])
            rule.pop('definition_generation', None)
        else:
            rule['definition_generation'] = generation
    if retired:
        result['definition_generation_tombstones'] = retired
    else:
        result.pop('definition_generation_tombstones', None)
    return result


# LLM: 取消、旧代及暂停前预留只能收历史；暂停后显式手动试跑仍可结算同代策略，但不能恢复计划。
# 函数用途: 比较冻结 run 的定义来源与当前 job/rule。
def run_matches_definition(job, run):
    manual_paused = (job.get('status') == 'paused' and run.get('trigger') == 'manual'
                     and run.get('definition_status') == 'paused')
    if (job.get('status') != 'active' and not manual_paused) or not job.get('enabled', True) or run.get('cancel_requested_at'):
        return False
    frozen = run.get('definition_generation', (run.get('event_source') or {}).get('generation', 1))
    return type(frozen) is int and frozen == definition_generation(job)


# LLM: 创建仍由原 owner 锁分配 job/alias；旧单规则字段只用于创建基座，复合对象删去重复权威字段。
# 函数用途: 校验多规则定义，生成原 job 的规则数组及最早到期投影，不执行任务。
def new_monitor(owner, request, now):
    from .repository import _new_job_payload

    definitions = normalize_rules(request.rules)
    first = definitions[0]
    base = _new_job_payload(owner=owner, request=replace(request, rules=None,
        prompt=first['prompt'] or request.name, schedule=first['schedule']), now=now)
    for key in _ACTION_FIELDS:
        base.pop(key, None)
    base.pop('definition_generation', None)
    base['rules'] = [_initialize_rule(rule, now) for rule in definitions]
    base['monitor_schema'] = 'scheduler_monitor.v1'
    return _project(base)


# LLM: 查询校验定义和运行数值，不用展示别名或模型文字推导真实状态；未知字段不能偷偷变成权限。
# 函数用途: 检查账本中的复合监控，防止损坏规则进入执行队列。
def validate_monitor(job):
    if job.get('monitor_schema') != 'scheduler_monitor.v1' or not str(job.get('name') or '').strip():
        raise ScheduleValidationError('监控定义无效')
    normalize_rules([{key: value for key, value in rule.items() if key not in _RUNTIME_FIELDS} for rule in job['rules']])
    for rule in job['rules']:
        if float(rule.get('next_run_at') or 0) < 0 or int(rule.get('run_count') or 0) < 0:
            raise ScheduleValidationError('监控运行字段无效')


# LLM: 对规则的修改仅影响未来预留；旧单任务显式迁移为 rule1 并保留时间、统计和 checkpoint，来源不可改绑。
# 函数用途: 更新完整规则定义或监控公共字段，保持未改规则的周期及历史身份。
def update_monitor(job, patch, now):
    from .repository import _updated_job

    allowed = {'rules', 'name', 'delivery', 'skill_refs', 'misfire_grace_seconds'}
    if set(patch) - allowed:
        raise ScheduleValidationError('复合监控请在 rules 内修改规则计划、动作或策略')
    if job.get('rules') is None:
        migrated = dict(job)
        migrated['rules'] = [dict(id='rule1', name=str(job['name']), enabled=True, use_llm=True,
            tool=None, depends_on=[], condition=None, delivery=None,
            **{key: deepcopy(job[key]) for key in _ACTION_FIELDS if key in job},
            **{key: deepcopy(job[key]) for key in _RUNTIME_FIELDS if key in job})]
        for key in _ACTION_FIELDS:
            migrated.pop(key, None)
        migrated.update(monitor_schema='scheduler_monitor.v1', migration={'from': 'scheduler_job.v1', 'at': now})
        job = migrated
    old = {rule['id']: rule for rule in job['rules']}
    definitions = normalize_rules(patch.get('rules', [{k: v for k, v in r.items() if k not in _RUNTIME_FIELDS} for r in job['rules']]))
    rules = []
    for rule in definitions:
        previous = old.get(rule['id'])
        state = {k: deepcopy(v) for k, v in (previous or {}).items() if k in _RUNTIME_FIELDS}
        if previous is None or previous['schedule'] != rule['schedule'] or previous['enabled'] != rule['enabled']:
            state['next_run_at'] = _initialize_rule(rule, now)['next_run_at']
        rules.append({**rule, **state})
    # 公共字段复用原校验入口，以一条规则的临时视图校验；这个视图不落盘。
    view = _view(job, definitions[0])
    view.pop('rules', None)
    view['prompt'] = view['prompt'] or view['name']
    validated = _updated_job(view, {k: v for k, v in patch.items() if k != 'rules'}, now=now)
    result = {**job, **{k: validated[k] for k in ('name', 'delivery', 'skill_refs', 'misfire_grace_seconds')},
              'rules': rules, 'version': int(job['version']) + 1, 'updated_at': now}
    return _project(sync_definition_generations(result, job))


# LLM: 到期只取本监控启用且已到时的规则；每条规则仍走原 claim/失败退避，状态视图不会保存为第二个 job。
# 函数用途: 为原仓库生成各条规则的独立候选，单规则记录保持原行为。
def due_rule_views(job, now):
    if job.get('rules') is None:
        return [job]
    return [_view(job, rule) for rule in job['rules'] if rule['enabled']
            and 0 < float(rule.get('next_run_at') or 0) <= now and float(rule.get('backoff_until') or 0) <= now]


# LLM: 手动运行必须明确规则；不把多条规则静默缩成第一条，不绕过本规则依赖条件。
# 函数用途: 选择要试跑的准确规则，多个候选时要求调用方说明。
def manual_rule(job, identifier, now):
    candidates = [rule for rule in job['rules'] if rule['id'] == identifier or not identifier]
    if len(candidates) != 1:
        raise ScheduleValidationError('多规则监控 run_now 需要准确 rule_id')
    view=_view(job,candidates[0])
    if not condition_matches(job,view,now):
        raise ScheduleValidationError('规则依赖结果尚未满足')
    return view


# LLM: 规则停用只取消原 queued/claimed 预留，已经 running/waiting 的动作和通知保持冻结版本。
# 函数用途: 找出被停用或移除的准确规则编号，交给原仓库取消入口。
def disabled_rule_ids(previous,current):
    enabled={rule['id'] for rule in current.get('rules') or [] if rule['enabled']}
    return frozenset(rule['id'] for rule in previous.get('rules') or [] if rule['enabled'] and rule['id'] not in enabled)


# LLM: 规则定义与运行字段分开，启停不把 runtime 投影传回定义校验；旧单规则沿原显式迁移。
# 函数用途: 为准确一条规则生成新 enabled 定义，保留其他规则和全部运行统计。
def rule_enable_patch(job,identifier,enabled):
    if not job.get('rules'):
        if identifier!='rule1':
            raise ScheduleValidationError('规则编号不存在')
        job=update_monitor(job,{},0)
    if not any(rule['id']==identifier for rule in job['rules']):
        raise ScheduleValidationError('规则编号不存在')
    return {'rules':[{**{key:value for key,value in rule.items() if key not in _RUNTIME_FIELDS},
        'enabled':enabled if rule['id']==identifier else rule['enabled']} for rule in job['rules']]}


# LLM: 运行快照冻结动作与通知，线程必须隔离以免不同周期共享车道；来源与 owner 不变。
# 函数用途: 把规则转换为原 scheduler run 接受的临时形态。
def _view(job, rule):
    return {**job, **rule, 'name': job['name'], 'rule_id': rule['id'],
        'delivery': job['delivery'] if job['delivery']['mode']=='none' else (rule.get('delivery') or job['delivery']),
        'execution': {**rule['execution'], 'mode': 'isolated'}}


# LLM: 推进周期只合回准确规则运行字段，不覆盖同时被更新的其它规则或监控定义。
# 函数用途: 在原锁事务中更新某条规则的下一次时间与状态投影。
def merge_rule_view(job, view):
    if job.get('rules') is None:
        return view
    result = deepcopy(job)
    for rule in result['rules']:
        if rule['id'] == view['rule_id']:
            rule.update({k: deepcopy(v) for k, v in view.items() if k in _RUNTIME_FIELDS})
            if view['status'] == 'paused' and job['status'] == 'active':
                rule['enabled'] = False
    result.update(version=int(job['version']) + 1, updated_at=view['updated_at'])
    return _project(result)


# LLM: 次数只在原 mark_run_running 第一次真实开工增加，预留/错过/条件跳过不算执行。
# 函数用途: 保存监控和规则实际开工计数及最后开工时间。
def record_start(job, run, now):
    result = deepcopy(job)
    result.update(run_count=int(job.get('run_count') or 0) + 1, last_run_at=now)
    for rule in result.get('rules', []):
        if rule['id'] == run.get('rule_id'):
            rule.update(run_count=int(rule.get('run_count') or 0) + 1, last_run_at=now)
    return result


# LLM: 旧版本运行不能改新规则定义；结果只来自已成功的工具 JSON，不从 LLM 文案猜条件。
# 函数用途: 将真实终态和确认结果合回规则，原 history 仍保存每次完整运行。
def finish_rule(job, run, now):
    from .repository import _job_after_run

    candidates = [rule for rule in job['rules'] if rule['id'] == run.get('rule_id')]
    if not candidates or not run_matches_definition(_view(job, candidates[0]), run):
        return {**job, 'last_run_status': run['status'], 'last_error_code': run.get('error_code', '')}
    view = _view(job, candidates[0])
    view.pop('rules', None)
    updated = _job_after_run(view, run, now=now)
    updated['last_run_at'] = candidates[0].get('last_run_at', 0)
    if run.get('structured_result') is not None and run['status'] == 'done':
        updated['result'] = {'value': deepcopy(run['structured_result']), 'confirmed_at': now,
            'run_id': run['run_id'], 'definition_generation': run.get('definition_generation', 1)}
    result = merge_rule_view(job, updated)
    result.update(last_run_status=run['status'], last_error_code=run.get('error_code', ''))
    return result


# LLM: 恢复只推进原监控未来周期，不自动补所有漏轮，不重新启用被用户关闭的规则。
# 函数用途: 为暂停后恢复的监控重新计算各启用规则的下次运行时间。
def resume_monitor(job, now):
    from .repository import _resume_next_run

    result = deepcopy(job)
    for rule in result['rules']:
        if rule['enabled']:
            rule['next_run_at'] = _resume_next_run(_view(job, rule), now)
    return _project(result)


# LLM: 条件只读本监控确认结果；缺失、过期、不成功均不触发，依赖不借用其它 owner 或自然语言。
# 函数用途: 对结构化依赖和 JSON 指针作确定性判断。
def condition_matches(job, view, now):
    if job.get('rules') is None:
        return True
    rules = {rule['id']: rule for rule in job['rules']}
    for key in view['depends_on']:
        upstream = rules.get(key)
        result = upstream.get('result') if upstream else None
        if not result or result.get('definition_generation') != definition_generation(_view(job, upstream)):
            return False
    condition = view.get('condition')
    if condition is None:
        return True
    result = rules[condition['rule_id']].get('result')
    age = condition['max_age_seconds']
    if result is None or now < result['confirmed_at'] or (age and now - result['confirmed_at'] > age):
        return False
    from .precheck import _json_pointer
    try:
        value = _json_pointer(result['value'], condition['pointer'])
    except (KeyError, IndexError, TypeError, ValueError):
        return False
    operation = condition['op']
    if operation == 'exists':
        return True
    if operation == 'truthy':
        return bool(value)
    return (value == condition['value']) if operation == 'eq' else (value != condition['value'])


# LLM: 新规则时间使用原 schedule 解析；未启用没有到期时间，统计从实际开工开始。
# 函数用途: 初始化规则运行字段，不产生执行或通知。
def _initialize_rule(rule, now):
    from .repository import _initial_next_run
    return {**rule, 'next_run_at': _initial_next_run(rule['schedule'], now) if rule['enabled'] else 0,
            'run_count': 0, 'last_run_at': 0, 'last_run_status': '', 'consecutive_failures': 0, 'backoff_until': 0}


# LLM: 顶层 next_run_at 只投影启用且仍有周期的规则，必须合并各自退避；唯一规则状态仍在 rules。
# 函数用途: 刷新监控真正最早可运行时间，避免退避期间按旧周期重复唤醒。
def _project(job):
    enabled = [r for r in job['rules'] if r['enabled']]
    due = [max(float(r.get('next_run_at') or 0), float(r.get('backoff_until') or 0))
           for r in enabled if float(r.get('next_run_at') or 0) > 0]
    return {**job, 'next_run_at': min(due, default=0),
            'status': 'paused' if job['status'] == 'active' and not enabled else job['status']}


# LLM: 在原 owner 仓库锁内分配展示别名，已删除的监控也占编号；别名不是授权或跨 owner 身份。
# 函数用途: 为新监控分配稳定递增编号，避免用户指令误操作重用编号。
def next_alias(store):
    numbers = [int(alias[3:]) for row in store['jobs'].values()
               if (alias := str(row.get('alias') or '')).startswith('job') and alias[3:].isdigit()]
    return f"job{max(numbers, default=0) + 1:02d}"


# LLM: 由原 claim 事务调用；仅第一次实际开工写统计，重入 mark_running 不重复增加。
# 函数用途: 在已有仓库锁内把实际开工投回原 job。
def record_run_start(repository,store,run,now):
    if not run.get('started_at'):
        identifier=str(run['job_id'])
        store['jobs'][identifier]=record_start(repository._require_job(store,identifier),run,now)
