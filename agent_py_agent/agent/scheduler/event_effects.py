# LLM: 副作用准入只在原job事件状态存有界在途lease，引用既有模型call/投递键；原run/history和原外发回执仍是结果权威。
# 模块用途: 让事件退代与真正开始模型、投递在同一原CAS排序，不持原store锁跨慢I/O或复活归档run。
from __future__ import annotations

import os
import threading
import time
import uuid
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path

from ..common.heartbeat import process_start_time
from ..common.json_io import JsonlObjectsReadReport, read_jsonl_objects_report
from .event_sources import event_run_current

MAX_ACTIVE_EVENT_EFFECTS = 64
_HISTORY_REPORT_CACHE_MAX_PATHS = 8
_HISTORY_REPORT_CACHE_MAX_BYTES = 16 * 1024 * 1024
_HISTORY_REPORT_CACHE: OrderedDict[str, tuple[tuple[int, ...], tuple[dict, ...]]] = OrderedDict()
_HISTORY_REPORT_CACHE_LOCK = threading.Lock()


# LLM: ctime补足原四元指纹的同尺寸恢复mtime改写；stat失败禁止复用，错误仍交原JSONL报告器。
# 函数用途: 取得历史文件的mtime、尺寸、inode、设备和ctime版本。
def _history_file_stamp(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size, info.st_ino, info.st_dev, info.st_ctime_ns)


# LLM: 只缓存无坏行且读前后同版的原始历史投影；调用方不得修改records，公开返回必须复制，owner/schema仍逐次校验。
# 函数用途: 为副作用、历史列表和取消查询复用最多8路径/16MiB原始字节的JSONL解析。
def _cached_history_report(path: Path, *, context: str) -> JsonlObjectsReadReport:
    key, stamp = str(path), _history_file_stamp(path)
    with _HISTORY_REPORT_CACHE_LOCK:
        cached = _HISTORY_REPORT_CACHE.get(key)
        if stamp is not None and cached is not None and cached[0] == stamp:
            _HISTORY_REPORT_CACHE.move_to_end(key)
            return JsonlObjectsReadReport(list(cached[1]), [])
        _HISTORY_REPORT_CACHE.pop(key, None)
    report = read_jsonl_objects_report(path, context=context)
    if (stamp is not None and not report.load_errors and stamp[1] <= _HISTORY_REPORT_CACHE_MAX_BYTES
            and _history_file_stamp(path) == stamp):
        with _HISTORY_REPORT_CACHE_LOCK:
            _HISTORY_REPORT_CACHE[key] = (stamp, tuple(report.records))
            _HISTORY_REPORT_CACHE.move_to_end(key)
            while (len(_HISTORY_REPORT_CACHE) > _HISTORY_REPORT_CACHE_MAX_PATHS
                   or sum(row[0][1] for row in _HISTORY_REPORT_CACHE.values()) > _HISTORY_REPORT_CACHE_MAX_BYTES):
                _HISTORY_REPORT_CACHE.popitem(last=False)
    return report


# LLM: 所有方法都复用原SchedulerRepository锁与quota；在途lease只有准入/释放，不复制模型或投递结果。
# 类用途: 提供事件运行副作用的线性化开始与结束。
class SchedulerEventEffectOperations:
    # LLM: 精确查询原活动run或同版历史投影，不限最近行数；归属/schema逐次校验，嵌套返回与缓存隔离。
    # 函数用途: 为副作用边界读取准确run，历史未变化时省去重复JSONL解析。
    def effect_run(self, run_id):
        run = self.get_active_run(run_id)
        if run is not None:
            return run
        report = _cached_history_report(self.history_path, context='scheduler.effect.history')
        for raw in reversed(report.records):
            if raw.get('run_id') != run_id:
                continue
            terminal, _error = self._parse_run(deepcopy(raw), terminal=True)
            return terminal
        return None

    # LLM: begin成功是副作用已经准入的线性化点；控制先提交则拒绝，begin先提交则原取消只报告在途待清理。
    # 函数用途: 原子登记一个有限在途操作，释放原锁后才调用原模型或运输；归档运行仅允许既有冻结交付重投。
    def begin_event_effect(self, candidate, operation_key, *, kind, allow_terminal=False):
        from .repository import SchedulerConflictError, _claim_matches, _runner_liveness

        if candidate.get('event_source') is None:
            return None
        if kind not in {'model', 'delivery'} or not isinstance(operation_key, str) or not operation_key or len(operation_key) > 1024:
            raise SchedulerConflictError('事件副作用身份无效')
        archived = self.effect_run(candidate['run_id']) if allow_terminal and kind == 'delivery' else None
        with self._mutation_scope() as (store, admission):
            active, _error = self._parse_run(store['runs'].get(candidate['run_id']))
            run = active if active is not None else archived
            if (active is None and (not allow_terminal or kind != 'delivery')
                    or run is None or active is None and run.get('status') not in {'done', 'failed', 'unknown', 'skipped', 'cancelled'}
                    or run.get('owner') != self.owner or run.get('event_source') != candidate.get('event_source')
                    or active is not None and not _claim_matches(run, str(candidate.get('claim_id') or ''))):
                raise SchedulerConflictError('事件副作用运行已过期')
            job = self._require_job(store, run['job_id'])
            if not event_run_current(job, run):
                raise SchedulerConflictError('事件来源已退休')
            key = str(run.get('rule_id') or '')
            state = job['event_source_state'][key]
            effects = {name: row for name, row in (state.get('active_effects') or {}).items()
                if _runner_liveness(row) != 'dead'}
            identity = kind + ':' + operation_key
            if identity in effects or len(effects) >= MAX_ACTIVE_EVENT_EFFECTS:
                raise SchedulerConflictError('事件副作用仍在途或达到容量')
            lease = {'lease_id': uuid.uuid4().hex, 'run_id': run['run_id'], 'kind': kind,
                'generation': run['event_source']['generation'], 'claim_epoch': int(run.get('claim_epoch') or 0),
                'runner_pid': os.getpid(), 'runner_start_time': process_start_time(os.getpid()), 'started_at': time.time()}
            effects[identity] = lease
            state['active_effects'] = effects
            self._write_store_unlocked(store, admission)
        return {'job_id': run['job_id'], 'rule_id': key, 'operation_key': identity, 'lease_id': lease['lease_id']}

    # LLM: 只释放同一nonce，不要求旧generation仍生效，不能让老finally删除替代调用；不改run/history终态。
    # 函数用途: 在原副作用返回或异常后清理其在途lease，供取消待清理事实对账。
    def finish_event_effect(self, lease):
        if lease is None:
            return
        with self._mutation_scope() as (store, admission):
            job = self._require_job(store, lease['job_id'])
            state = (job.get('event_source_state') or {}).get(lease['rule_id'])
            if state is None:
                return
            effects = dict(state.get('active_effects') or {})
            current = effects.get(lease['operation_key'])
            if current is None or current.get('lease_id') != lease['lease_id']:
                return
            effects.pop(lease['operation_key'])
            state['active_effects'] = effects
            self._write_store_unlocked(store, admission)
