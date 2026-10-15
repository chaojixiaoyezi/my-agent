# LLM: 事件退代仍使用原 run 取消字段与 pending_cancellation_runs，不另建停止队列；校验拒绝损坏来源状态。
# 模块用途: 在原 scheduler 锁内登记旧代运行取消，并校验事件 job 的 owner、创建者和持久状态。
from __future__ import annotations

import math

from .event_sources import event_rule_views, event_run_current
from .schedule import ScheduleValidationError


# LLM: 已在原 mutation_scope 内，取消意图同 job 退代原子落盘；原控制服务负责实际终止后代与工具。
# 函数用途: 为仍在执行或等待的旧代事件 run 记录原取消意图，不影响时间规则。
def cancel_retired_event_runs(store, job, *, now):
    pending = set(job.get('cancel_pending_run_ids') or [])
    for run_id, run in list(store['runs'].items()):
        if (run.get('job_id') == job['job_id'] and run.get('event_source') is not None
                and run.get('status') in {'running', 'waiting'} and not event_run_current(job, run)):
            store['runs'][run_id] = {**run, 'cancel_requested_at': now, 'cancel_reason': 'event_source_retired'}
            pending.add(run_id)
    return {**job, 'cancel_pending_run_ids': sorted(pending)} if pending else job


# LLM: source 字符串本身不是授权；本层只保证 owner/creator 形状与原状态一致，真实群 capability 仍由工具/宿主门复核。
# 函数用途: 在创建及读取时拒绝跨群、匿名来源、NO 动作或损坏事件状态。
def validate_event_job(job):
    views = event_rule_views(job)
    if not views:
        return
    owner, creator = job.get('owner') or {}, job.get('creator') or {}
    for view in views:
        source = view['schedule']['source']
        if (owner.get('kind') != 'group' or owner.get('provider') != source['channel']
                or str(owner.get('id') or '').split('/')[-1] != source['chat_id']
                or creator.get('channel') != source['channel'] or not creator.get('actor_id')
                or view.get('use_llm', True) is not True):
            raise ScheduleValidationError('事件订阅必须由准确当前群人类创建者登记并交 LLM 研判')
        state = (job.get('event_source_state') or {}).get(str(view.get('rule_id') or ''))
        if (not isinstance(state, dict) or type(state.get('generation')) is not int or state['generation'] < 1
                or type(state.get('active')) is not bool or not isinstance(state.get('pending'), list)
                or len(state['pending']) > view['schedule']['max_batch_events'] or not isinstance(state.get('receipts'), dict)):
            raise ScheduleValidationError('事件订阅状态损坏')
        _validate_event_state(state)


# LLM: 近期receipt只按真实provider时间退休；损坏水位、非有限时间或不完整摘要必须进入原坏账诊断，不能稳定ACK。
# 函数用途: 校验原事件状态的有限数值和近期去重形状，不修复或写入文件。
def _validate_event_state(state):
    from .event_effects import MAX_ACTIVE_EVENT_EFFECTS
    from .event_source_repository import MAX_EVENT_RECEIPTS

    for key in ('received_watermark', 'retired_through', 'last_dispatch_at'):
        value = state.get(key)
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            raise ScheduleValidationError('事件订阅水位损坏')
    if len(state['receipts']) > MAX_EVENT_RECEIPTS:
        raise ScheduleValidationError('事件订阅近期去重超过容量')
    for key, receipt in state['receipts'].items():
        if (not isinstance(key, str) or len(key) != 64 or not isinstance(receipt, dict)
                or set(receipt) != {'digest', 'created_at'} or not isinstance(receipt['digest'], str)
                or len(receipt['digest']) != 64 or type(receipt['created_at']) not in {int, float}
                or not math.isfinite(receipt['created_at']) or receipt['created_at'] < 0):
            raise ScheduleValidationError('事件订阅近期去重损坏')
    effects = state.get('active_effects', {})
    if not isinstance(effects, dict) or len(effects) > MAX_ACTIVE_EVENT_EFFECTS:
        raise ScheduleValidationError('事件订阅在途操作超过容量或损坏')
    for key, row in effects.items():
        if (not isinstance(key, str) or not key.startswith(('model:', 'delivery:')) or len(key) > 1033
                or not isinstance(row, dict) or not isinstance(row.get('lease_id'), str) or not row['lease_id']
                or not isinstance(row.get('run_id'), str) or not row['run_id'].startswith('srun_')
                or row.get('kind') not in {'model', 'delivery'} or not key.startswith(row['kind'] + ':')
                or type(row.get('generation')) is not int or row['generation'] < 1
                or type(row.get('claim_epoch')) is not int or row['claim_epoch'] < 0
                or type(row.get('runner_pid')) is not int or row['runner_pid'] < 1
                or type(row.get('started_at')) not in {int, float}
                or not math.isfinite(row['started_at']) or row['started_at'] < 0):
            raise ScheduleValidationError('事件订阅在途操作身份损坏')


# LLM: event_source 是宿主冻结身份，损坏不得退化为普通任务；旧无事件字段的定时run保持原协议。
# 函数用途: 校验原run中的来源代次与canonical结构，不写账或重新授权。
def validate_event_run(run):
    frozen = run.get('event_source')
    if frozen is None:
        return
    if (not isinstance(frozen, dict) or set(frozen) != {'generation', 'source'}
            or type(frozen.get('generation')) is not int or frozen['generation'] < 1):
        raise ScheduleValidationError('事件运行来源损坏')
    from .event_sources import normalize_event_schedule
    normalize_event_schedule({'kind': 'event_source', 'source': frozen['source']})
