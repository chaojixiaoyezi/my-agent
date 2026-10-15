# LLM: 事件批次、去重及代次只存在原 scheduler store；原 mutation_scope 保持 quota→owner 锁，执行仍由原 run/Wake 接手。
# 模块用途: 为原监控添加事件登记与有界批次预留，积压满时向原 Ingress 施加可重试背压，不丢弃已接受事件。
from __future__ import annotations

import math
import time
from copy import deepcopy

from .event_sources import (
    event_identity,
    event_matches,
    event_model_prompt,
    event_rule_views,
    event_run_current,
)

# 单条事件规则最多保留 4096 条近期精确 receipt：窗口内满时背压，整时间桶退休后明确拒绝旧事件。
MAX_EVENT_RECEIPTS = 4096
# 晚到窗口固定一小时；允许供应商时间最多领先宿主五分钟，未来异常不得抬高退休水位。
EVENT_LATENESS_SECONDS = 3600
EVENT_FUTURE_TOLERANCE_SECONDS = 300

# LLM: 本 mixin 不创建线程、队列或文件；每次变更经 SchedulerRepository 原写入口和配额合同。
# 类用途: 将准确外部观察登记到现有 job 并预留原 srun。
class SchedulerEventSourceOperations:
    # LLM: 先复核所有命中规则容量再同事务写入；重复重投不占容量，冲突及背压不得返回 accepted。
    # 函数用途: 持久登记同群已核验事件，保存原 Ingress refs；无订阅时只返回 ignored。
    def accept_source_event(self, event, *, now=None):
        current = time.time() if now is None else float(now)
        stamp = event.get('created_at')
        if type(stamp) not in {int, float} or not math.isfinite(stamp) or stamp < 0 or stamp > current + EVENT_FUTURE_TOLERANCE_SECONDS:
            return {'status': 'retryable', 'reason_code': 'EVENT_SOURCE_TIMESTAMP_INVALID'}
        identity, digest = event_identity(event)
        matches, duplicates, stale, updates = [], 0, 0, {}
        with self._mutation_scope() as (store, admission):
            jobs, errors = self._valid_jobs(store)
            if errors:
                return {'status': 'retryable', 'reason_code': 'EVENT_SOURCE_STATE_INVALID'}
            for job in jobs:
                if job['status'] != 'active':
                    continue
                for view in event_rule_views(job):
                    outcome, key, state = _event_candidate(job, view, event, identity, digest, current)
                    if state is not None:
                        updates[(job['job_id'], key)] = state
                    if outcome == 'duplicate':
                        duplicates += 1
                    if outcome == 'stale':
                        stale += 1
                    if outcome == 'backpressure':
                        return {'status': 'retryable', 'reason_code': 'EVENT_SOURCE_BACKPRESSURE'}
                    if outcome == 'register':
                        matches.append((job['job_id'], key))
            for (job_id, key), state in updates.items():
                job = deepcopy(store['jobs'][job_id])
                if (job_id, key) in matches:
                    state['pending'] = [*state['pending'], deepcopy(event)]
                    state['receipts'][identity] = {'digest': digest, 'created_at': stamp}
                    state['received_watermark'] = max(float(state.get('received_watermark') or 0), stamp)
                job['event_source_state'][key] = state
                job.update(version=int(job['version']) + 1, updated_at=current)
                store['jobs'][job_id] = job
            if updates:
                self._write_store_unlocked(store, admission)
        if not matches:
            return {'status': 'accepted' if duplicates else 'ignored', 'reason_code': 'EVENT_SOURCE_DUPLICATE' if duplicates
                else 'EVENT_SOURCE_STALE' if stale else 'EVENT_SOURCE_NOT_SUBSCRIBED'}
        return {'status': 'accepted', 'reason_code': 'EVENT_SOURCE_REGISTERED', 'subscriptions': len(matches)}

    # LLM: idle 只读快照，存在可运行 pending 才进入原写事务；时间仅控制批次间隔，绝不轮询平台历史。
    # 函数用途: 将事件待办一次预留成原 srun，忙碌车道保留待办，后续由原后台 tick 派发。
    def reserve_event_runs(self, *, now=None, limit=32):
        from .monitor_state import condition_matches
        from .repository import _reserved_run

        current = time.time() if now is None else float(now)
        if not any(state.get('pending') for job in self._valid_jobs(self._store_snapshot())[0]
                if job['status'] == 'active' for state in (job.get('event_source_state') or {}).values()):
            return []
        reserved = []
        with self._mutation_scope() as (store, admission):
            for original in self._valid_jobs(store)[0]:
                if original['status'] != 'active':
                    continue
                for view in event_rule_views(original):
                    key = str(view.get('rule_id') or '')
                    job = store['jobs'][original['job_id']]
                    state = (job.get('event_source_state') or {}).get(key)
                    if not state or not state.get('active') or not state.get('pending'):
                        continue
                    last = float(state.get('last_dispatch_at') or 0)
                    if ((last and current < last + view['schedule']['min_interval_seconds'])
                            or float(view.get('backoff_until') or 0) > current
                            or self._job_has_active_run(store, job['job_id'], key)
                            or not condition_matches(job, view, current)):
                        continue
                    run = _reserved_run(view, trigger='event', scheduled_for=current, dispatch_after=current, now=current)
                    run.update(event_source={'generation': state['generation'], 'source': deepcopy(view['schedule']['source'])},
                        event_batch=deepcopy(state['pending']), prompt=event_model_prompt(run['prompt'], state['pending']))
                    updated = deepcopy(job)
                    updated['event_source_state'][key].update(pending=[], last_dispatch_at=current)
                    updated.update(version=int(job['version']) + 1, updated_at=current)
                    store['jobs'][job['job_id']], store['runs'][run['run_id']] = updated, run
                    reserved.append(run)
                    if len(reserved) >= max(1, limit):
                        break
                if len(reserved) >= max(1, limit):
                    break
            if reserved:
                self._write_store_unlocked(store, admission)
        return deepcopy(reserved)

    # LLM: 当前原 store 是唯一退代权威；不根据 job.version、模型正文或内存缓存判断取消。
    # 函数用途: 给原执行和冻结重投查询事件 run 是否仍可采样及外发。
    def event_run_is_current(self, run):
        if run.get('event_source') is None:
            return True
        store = self._store_snapshot()
        job, error = self._parse_job(store['jobs'].get(run['job_id']))
        return error == '' and job is not None and event_run_current(job, run)


# LLM: 本函数只读取原锁内的 job/view；重复冲突必须明确失败，不能因容量满或损坏而稳定忽略。
# 函数用途: 计算单条规则对一次事件的登记资格，不写文件或解析正文。
def _event_candidate(job, view, event, identity, digest, now):
    from .repository import SchedulerConflictError

    key = str(view.get('rule_id') or '')
    if not view.get('enabled', True) or not event_matches(view['schedule'], event):
        return 'ignored', key, None
    original = (job.get('event_source_state') or {}).get(key)
    if original is None or not original.get('active'):
        raise SchedulerConflictError('事件订阅状态不可用')
    state = _retired_event_receipts(original, float(event['created_at']), now)
    if event['created_at'] < float(job.get('created_at') or 0) or event['created_at'] <= state['retired_through']:
        state.update(stale_attempts=int(state.get('stale_attempts') or 0) + 1, last_stale_ref=identity)
        return 'stale', key, state
    previous = state['receipts'].get(identity)
    if previous is not None and previous['digest'] != digest:
        raise SchedulerConflictError('同事件身份的正文或来源发生冲突')
    if previous is not None:
        return 'duplicate', key, state
    if len(state['receipts']) >= MAX_EVENT_RECEIPTS:
        return 'backpressure', key, None
    return ('backpressure' if len(state['pending']) >= view['schedule']['max_batch_events'] else 'register'), key, state


# LLM: 只关闭完整 provider 时间桶，<=水位的所有ID以后明确stale；原pending/run批次与Ingress refs完全不动。
# 函数用途: 按一小时迟到窗口退休近期receipt，宿主时钟上界防未来事件迫使清空真实去重事实。
def _retired_event_receipts(original, stamp, now):
    boundary = max(float(original.get('retired_through') or 0), min(now - EVENT_LATENESS_SECONDS,
        max(float(original.get('received_watermark') or 0), stamp)))
    return {**original, 'retired_through': boundary,
        'receipts': {key: value for key, value in original['receipts'].items() if value['created_at'] > boundary}}
