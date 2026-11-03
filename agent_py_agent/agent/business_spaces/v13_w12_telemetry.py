"""运营遥测只读聚合，保留原生账本来源、覆盖边界与未知值。

调度器的普通构造器会建目录，普通快照可能恢复资产事务；本模块仅给原生
history/runtime_snapshot/queued_runs 绑定 no-follow 字节快照，不调用恢复写口。
原生成功终态为 done（计划中的 ok），没有持久失败量的阶段返回 failed=null。
"""
from __future__ import annotations

import json
import math
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

from ..assets.workspace_assets import _BATCH_FILE, _read_lock
from ..common.json_io import jsonl_lines
from ..common.nofollow_fs import read_bytes_beneath, regular_file_exists_beneath
from ..conversation.business_access import BusinessError
from ..gateway_parts.remote_push_service import read_push_json, remote_push_root
from ..plugin_host.errors import HostAPIError
from ..scheduler.alert_delivery import delivery_event_key
from ..scheduler.repository import SchedulerRepository
from .v13_store import v13_store_for

if TYPE_CHECKING:
    from ..gateway_parts.business_api_v13 import V13Call


class _SourceError(ValueError):
    """仅携带来源机器码，不公开异常正文或私有路径。"""

    # LLM: 不可读来源不能被解释成零条成功。
    # 函数用途: 保存结构化来源错误。
    def __init__(self, code):
        self.error_code = code
        super().__init__(code)


class _ReadOnlyScheduler(SchedulerRepository):
    """只读适配原生解析器及三个公开读口，不调用有写副作用的构造器。"""

    # LLM: 只持单次字节快照，不建目录、索引、准入或恢复写者。
    # 函数用途: 绑定原生历史路径、真实 owner 和本次快照。
    def __init__(self, call, raw):
        paths = call.context.host.home_paths
        self.root = Path(call.owner_home) / 'data' / 'scheduler'
        self.store_path, self.history_path = self.root / 'store.json', self.root / 'history.jsonl'
        self.owner = {'provider': paths.owner_provider, 'kind': paths.owner_kind, 'id': call.owner_id}
        self._snapshot = self._load_store_from_bytes_unlocked(raw)

    # LLM: 原生 runtime_snapshot 和 queued_runs 消费同一代已验证事实。
    # 函数用途: 返回不共享可变引用的只读快照。
    def _store_snapshot(self, *, readonly=False):
        return deepcopy(self._snapshot)

    # LLM: 历史也经 no-follow 字节读口，仍复用原生 owner/schema 解析、去重排序及二百条上限。
    # 函数用途: 阻止普通 Path 历史读取越过 owner 边界，不建立缓存或恢复事务。
    def history(self, *, job_id='', limit=50):
        raw = read_bytes_beneath(self.root.parent.parent, ('data', 'scheduler', 'history.jsonl'))
        errors, rows = [], {}
        for line in jsonl_lines(raw.decode('utf-8') if raw is not None else ''):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                errors.append('SCHEDULER_HISTORY_JSON_INVALID')
                continue
            run, error = self._parse_run(record, terminal=True)
            if error:
                errors.append(error)
            elif run is not None and (not job_id or run['job_id'] == job_id):
                rows[str(run['run_id'])] = run
        selected = sorted(rows.values(), key=lambda row: (
            float(row.get('ended_at') or row.get('created_at') or 0.0), str(row['run_id'])), reverse=True)
        return deepcopy(selected[:max(1, min(int(limit or 50), 200))]), sorted(set(errors))


# LLM: 只消费异常结构化码；实时权限失败不能被降成来源缺失。
# 函数用途: 提取可公开错误码并保护授权边界。
def _error_code(error, default):
    code = getattr(error, 'error_code', None) or getattr(error, 'code', None)
    if code in {'CAPABILITY_DENIED', 'ACTOR_REQUIRED', 'BUSINESS_ACCESS_DENIED',
                'RESOURCE_NOT_FOUND', 'SPACE_SCOPE_MISMATCH'}:
        raise error
    return code if isinstance(code, str) and 0 < len(code) <= 128 else default


# LLM: 拒绝布尔、NaN、无时区日期及数量级异常的时间。
# 函数用途: 将原生 epoch 或 V13 时间解析成有限秒数。
def _epoch(value):
    if type(value) in (int, float):
        if math.isfinite(value) and 0 <= value <= 253402300799:
            return float(value)
    elif isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if stamp.tzinfo is not None:
                return _epoch(stamp.timestamp())
        except (ValueError, OverflowError):
            pass
    raise _SourceError('TELEMETRY_TIMESTAMP_INVALID')


# LLM: 缺失活动时间不能造为当前时间。
# 函数用途: 输出标准 UTC 日期。
def _at(value):
    return None if value is None else datetime.fromtimestamp(value, timezone.utc).isoformat()


# LLM: 身份和可选空间沿现有实时授权读口，不从统计记录扩权。
# 函数用途: 核验调用并冻结单次时窗。
def _window(call):
    from ..gateway_parts.business_api_v13 import v13_require_access, v13_resolve_space

    v13_require_access(call)
    if call.action == 'telemetry.funnel.get' and call.params.get('space_id'):
        try:
            v13_resolve_space(call, call.params['space_id'])
        except (BusinessError, HostAPIError) as error:
            if getattr(error, 'code', None) in {'SPACE_NOT_FOUND', 'SPACE_SCOPE_MISMATCH'}:
                raise HostAPIError('RESOURCE_NOT_FOUND', 404) from None
            raise
    hours = call.params.get('window_hours', 24)
    now = _epoch(call.clock())
    return hours, now - hours * 3600, now


# LLM: 缺账本与空账本不同，先核身份、时间和重复，再统计。
# 函数用途: 读取本 owner 的时窗回执及全部待接纳项的排队时间。
def _push_rows(call, since, until):
    root = remote_push_root(SimpleNamespace(agent=call.context.host))
    entries = read_push_json(root, 'receipts.json').get('entries')
    if not isinstance(entries, dict):
        raise _SourceError('REMOTE_PUSH_RECEIPTS_UNAVAILABLE')
    rows = {}
    for entry in entries.values():
        if not isinstance(entry, dict) or not isinstance(entry.get('receipt'), dict):
            raise _SourceError('REMOTE_PUSH_RECEIPT_INVALID')
        receipt = entry['receipt']
        if not isinstance(receipt.get('target_owner'), str):
            raise _SourceError('REMOTE_PUSH_RECEIPT_INVALID')
        if receipt['target_owner'] != call.owner_id:
            continue
        stamp, identifier = _epoch(receipt.get('queued_at')), receipt.get('push_id')
        if not isinstance(identifier, str) or not identifier or type(entry.get('accepted')) is not bool:
            raise _SourceError('REMOTE_PUSH_RECEIPT_INVALID')
        row = {'id': identifier, 'at': stamp, 'accepted': entry['accepted']}
        if identifier in rows and rows[identifier] != row:
            raise _SourceError('REMOTE_PUSH_RECEIPT_CONFLICT')
        rows[identifier] = row
    return ([row for row in rows.values() if since <= row['at'] <= until],
            [row['at'] for row in rows.values() if not row['accepted'] and row['at'] <= until])


# LLM: 仅打开已有根读锁；pending 事务由原写者处理，查询绝不恢复。
# 函数用途: 经原生读口取得有覆盖边界的历史、健康及队列。
def _scheduler_rows(call, since, until):
    with _read_lock(call.owner_home, ('.asset-batch.lock',)):
        if regular_file_exists_beneath(call.owner_home, (_BATCH_FILE,)):
            raise _SourceError('SCHEDULER_ASSET_RECOVERY_REQUIRED')
        raw = read_bytes_beneath(call.owner_home, ('data', 'scheduler', 'store.json'))
        if raw is None:
            raise _SourceError('SCHEDULER_SOURCE_MISSING')
        repository = _ReadOnlyScheduler(call, raw)
        snapshot = repository.runtime_snapshot()
        errors = snapshot.get('load_error_codes', [])
        if snapshot.get('state') != 'available':
            raise _SourceError(errors[0] if errors else 'SCHEDULER_UNAVAILABLE')
        history, history_errors = repository.history(limit=200)
        if history_errors:
            raise _SourceError(history_errors[0])
        # 原生接口最多返回 200 条，边界仍在时窗内就不宣称全量。
        if len(history) >= 200 and _epoch(history[-1].get('ended_at')) >= since:
            raise _SourceError('SCHEDULER_HISTORY_TRUNCATED')
        queue = repository.queued_runs(now=until, limit=max(1, snapshot['active_runs']))
        selected = [row for row in history if since <= _epoch(row.get('ended_at')) <= until]
        return repository, selected, queue, snapshot


# LLM: Store 处理已提交日志覆盖，revision=0 表示没有来源，不能假报零。
# 函数用途: 读取 owner 集合并在聚合前消除重复及坏行。
def _collection_rows(call, collection, id_field):
    from ..gateway_parts.business_api_v13 import v13_require_access

    store = v13_store_for(call)
    revision = store.revision(collection)
    rows = store.list(collection)
    if revision != store.revision(collection):
        raise _SourceError('TELEMETRY_SOURCE_CHANGED')
    if revision == 0:
        raise _SourceError('TELEMETRY_COLLECTION_MISSING')
    selected = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('owner_id'), str):
            raise _SourceError('TELEMETRY_RECORD_INVALID')
        if row['owner_id'] != call.owner_id:
            continue
        if call.params.get('space_id') and row.get('space_id') != call.params['space_id']:
            continue
        v13_require_access(call, row)
        identifier = row.get(id_field)
        if not isinstance(identifier, str) or not identifier:
            raise _SourceError('TELEMETRY_RECORD_INVALID')
        if identifier in selected and selected[identifier] != row:
            raise _SourceError('TELEMETRY_RECORD_CONFLICT')
        if collection == 'soc_alerts' and ('case_id' not in row or
                row['case_id'] is not None and (not isinstance(row['case_id'], str) or not row['case_id'])):
            raise _SourceError('TELEMETRY_RECORD_INVALID')
        if collection in ('script_runs', 'async_jobs') and row.get('state') not in {
                'accepted', 'awaiting_approval', 'running', 'completed', 'failed', 'unknown'}:
            raise _SourceError('TELEMETRY_RECORD_INVALID')
        selected[identifier] = row
    return list(selected.values())


# LLM: 无事件身份时不以空正文或随机 run id 造融合键。
# 函数用途: 用原生 delivery_event_key 及持久 outbox 事件身份去重。
def _event_keys(repository, row):
    job = repository._snapshot['jobs'].get(row['job_id'], {})
    if not isinstance(job, dict):
        raise _SourceError('SCHEDULER_JOB_INVALID')
    outbox = job.get('delivery_outbox', {})
    if not isinstance(outbox, dict):
        raise _SourceError('SCHEDULER_EVENT_KEY_INVALID')
    prefix = row['run_id'] + ':'
    keys = {key[len(prefix):] for key in outbox if key.startswith(prefix)}
    if any(not key or len(key) > 128 for key in keys):
        raise _SourceError('SCHEDULER_EVENT_KEY_INVALID')
    if keys:
        return {delivery_event_key(SimpleNamespace(wake_signal={}),
                    SimpleNamespace(idempotency_key='scheduler-event:' + key)) for key in keys}
    wake_id = row.get('wake_signal_id')
    if not isinstance(wake_id, str) or not wake_id:
        raise _SourceError('SCHEDULER_EVENT_KEY_MISSING')
    return {delivery_event_key(SimpleNamespace(wake_signal={'wake_signal_id': wake_id}),
                              SimpleNamespace(idempotency_key=''))}


# LLM: 故障仅影响依赖该来源的阶段，未持久的失败量保留 null。
# 函数用途: 构造漏斗与健康共用的五阶段事实。
def _stages(call, since, until):
    stages = [dict(id=name, source=source, processed=None, failed=None, lag_ms=None,
                   last_activity_at=None, reason_code=None, degraded=False) for name, source in (
        ('ingest', 'remote_push/receipts.json:target_owner,queued_at'),
        ('normalize', 'scheduler.history:status=done;runtime_snapshot;queued_runs'),
        ('fuse', 'scheduler.history:distinct delivery_event_key'),
        ('correlate', 'soc_alerts:created_at,case_id'),
        ('alert', 'soc_alerts:created_at'))]
    try:
        pushes, pending = _push_rows(call, since, until)
        stages[0].update(processed=len(pushes), lag_ms=int((until - min(pending)) * 1000) if pending else 0,
                         last_activity_at=_at(max((row['at'] for row in pushes), default=None)))
    except (OSError, ValueError, TypeError, RuntimeError, HostAPIError, BusinessError) as error:
        stages[0]['reason_code'] = _error_code(error, 'REMOTE_PUSH_UNAVAILABLE')
    try:
        repository, runs, queue, snapshot = _scheduler_rows(call, since, until)
        done = [row for row in runs if row['status'] == 'done']
        failed = [row for row in runs if row['status'] == 'failed']
        pending = [_epoch(row.get('created_at')) for row in queue]
        stages[1].update(processed=len(done), failed=len(failed),
            lag_ms=int(max(0, until - min(pending)) * 1000) if pending else 0,
            last_activity_at=_at(max((_epoch(row['ended_at']) for row in runs), default=None)),
            degraded=snapshot['health'] == 'degraded')
        if snapshot['health'] == 'degraded':
            stages[1]['reason_code'] = next(iter(snapshot.get('load_error_codes', [])), 'SCHEDULER_DEGRADED')
        try:
            keys, failed_keys = set(), set()
            for row in done:
                keys.update(_event_keys(repository, row))
            for row in runs:
                if row.get('delivery_status') == 'failed':
                    failed_keys.update(_event_keys(repository, row))
            stages[2].update(processed=len(keys), failed=len(failed_keys),
                last_activity_at=stages[1]['last_activity_at'], degraded=snapshot['health'] == 'degraded',
                reason_code=stages[1]['reason_code'])
        except (ValueError, TypeError, KeyError) as error:
            stages[2]['reason_code'] = _error_code(error, 'SCHEDULER_EVENT_KEY_INVALID')
    except (OSError, ValueError, TypeError, RuntimeError, HostAPIError, BusinessError) as error:
        code = _error_code(error, 'SCHEDULER_UNAVAILABLE')
        stages[1]['reason_code'] = stages[2]['reason_code'] = code
    try:
        alerts = [row for row in _collection_rows(call, 'soc_alerts', 'alert_id')
                  if since <= _epoch(row.get('created_at')) <= until]
        linked = [row for row in alerts if row.get('case_id')]
        for stage, rows in ((stages[3], linked), (stages[4], alerts)):
            stage.update(processed=len(rows),
                last_activity_at=_at(max((_epoch(row['created_at']) for row in rows), default=None)))
    except (OSError, ValueError, TypeError, RuntimeError, HostAPIError, BusinessError) as error:
        code = _error_code(error, 'TELEMETRY_COLLECTION_UNAVAILABLE')
        stages[3]['reason_code'] = stages[4]['reason_code'] = code
    return stages


# LLM: 独立来源不是严格子集，负缩减率不能钳成虚构的零。
# 函数用途: 返回真实数量、来源及未知范围。
def funnel(call: V13Call) -> dict:
    hours, since, until = _window(call)
    stages = _stages(call, since, until)
    counts = [stage['processed'] for stage in stages]
    unknown = [stage['id'] for stage in stages if stage['processed'] is None]
    ratio = None
    if counts[0] and counts[4] is not None:
        if counts[4] <= counts[0]:
            ratio = 1 - counts[4] / counts[0]
        else:
            unknown.append('reduction_ratio')
    return dict(window_hours=hours, ingested_count=counts[0], normalized_count=counts[1],
        fused_count=counts[2], correlated_count=counts[3], alert_count=counts[4], reduction_ratio=ratio,
        stages=[{'stage': stage['id'], 'count': stage['processed'], 'source': stage['source']} for stage in stages],
        unknown=unknown, coverage='partial' if unknown else 'complete')


# LLM: 严格使用大于阈值，整体状态取最差阶段。
# 函数用途: 计算吞吐、排队延迟与五阶段健康状态。
def pipeline_health(call: V13Call) -> dict:
    hours, since, until = _window(call)
    result = []
    for stage in _stages(call, since, until):
        processed, failed = stage['processed'], stage['failed']
        degraded = stage.pop('degraded')
        stage['eps'] = processed / (hours * 3600) if processed is not None else None
        stage['status'] = 'healthy'
        if processed is None:
            stage['status'] = 'unavailable'
        elif (degraded or stage['lag_ms'] is not None and stage['lag_ms'] > 300000
                or failed is not None and processed + failed > 0 and failed / (processed + failed) > 0.05):
            stage['status'] = 'degraded'
        result.append(stage)
    overall = next((status for status in ('unavailable', 'degraded')
                    if any(stage['status'] == status for stage in result)), 'healthy')
    return {'window_hours': hours, 'overall': overall, 'stages': result, 'generated_at': _at(until)}


# LLM: 错误正文不是机器码，不把脚本 stderr 解析成分组标签。
# 函数用途: 校验结构化错误码的语法和尺寸。
def _machine_code(value):
    return value if isinstance(value, str) and 0 < len(value) <= 128 else None


# LLM: 缺错误码不造兜底分组，仅已知失败参与总数并明确列出缺口。
# 函数用途: 向分组累积一条有结构化原因的失败记录。
def _add_failure(groups, unknown, source, code, stamp, reference):
    code = _machine_code(code)
    if code is None:
        unknown.add(source + '_error_code')
        return
    if not isinstance(reference, str) or not 0 < len(reference) <= 128:
        unknown.add(source + '_sample_ref')
        return
    group = groups.setdefault((source, code), {'source': source, 'error_code': code, 'count': 0,
        'first_at': stamp, 'last_at': stamp, 'sample_refs': []})
    group['count'] += 1
    group['first_at'], group['last_at'] = min(group['first_at'], stamp), max(group['last_at'], stamp)
    if reference not in group['sample_refs'] and len(group['sample_refs']) < 10:
        group['sample_refs'].append(reference)


# LLM: 四种来源独立读取，HTTP 边缘拒收未持久化，因此始终未知。
# 函数用途: 按来源及错误码聚合时窗内失败记录。
def dead_letters(call: V13Call) -> dict:
    hours, since, until = _window(call)
    selected = call.params.get('source')
    groups, unknown = {}, {'remote_push_rejections'}
    if selected in (None, 'scheduler_run', 'alert_delivery'):
        try:
            _repository, runs, _queue, _snapshot = _scheduler_rows(call, since, until)
            for row in runs:
                stamp = _epoch(row['ended_at'])
                if selected in (None, 'scheduler_run') and row['status'] == 'failed':
                    _add_failure(groups, unknown, 'scheduler_run', row.get('error_code'), stamp, row['run_id'])
                if selected in (None, 'alert_delivery') and row.get('delivery_status') == 'failed':
                    _add_failure(groups, unknown, 'alert_delivery', row.get('delivery_reason'), stamp, row['run_id'])
        except (OSError, ValueError, TypeError, RuntimeError, HostAPIError, BusinessError) as error:
            _error_code(error, 'SCHEDULER_UNAVAILABLE')
            unknown.update([selected] if selected else ['scheduler_run', 'alert_delivery'])
    for source, collection, field in (('script_run', 'script_runs', 'execution_id'),
                                       ('async_job', 'async_jobs', 'job_id')):
        if selected is not None and selected != source:
            continue
        try:
            rows = _collection_rows(call, collection, field)
            failures = []
            for row in rows:
                if row.get('state') != 'failed':
                    continue
                stamp = _epoch(row.get('finished_at') if source == 'script_run' else row.get('updated_at'))
                if since <= stamp <= until:
                    if source == 'script_run':
                        # 冻结 script_runs.error 是文本；只读明确的结构化原因。
                        output = row.get('output') or {}
                        if not isinstance(output, dict):
                            raise _SourceError('TELEMETRY_RECORD_INVALID')
                        code = row.get('error_code') or output.get('error_code')
                    else:
                        error = row.get('error') or {}
                        if not isinstance(error, dict):
                            raise _SourceError('TELEMETRY_RECORD_INVALID')
                        code = error.get('code')
                    failures.append((code, stamp, row[field]))
            for code, stamp, reference in failures:
                _add_failure(groups, unknown, source, code, stamp, reference)
        except (OSError, ValueError, TypeError, RuntimeError, HostAPIError, BusinessError) as error:
            _error_code(error, 'TELEMETRY_COLLECTION_UNAVAILABLE')
            unknown.add(source)
    ordered = sorted(groups.values(), key=lambda row: (-row['last_at'], row['source'], row['error_code']))
    total = sum(group['count'] for group in ordered)
    if len(ordered) > 200:
        unknown.add('groups_truncated')
    return {'window_hours': hours, 'groups': [{**row, 'first_at': _at(row['first_at']),
            'last_at': _at(row['last_at'])} for row in ordered[:200]], 'total': total,
            'unknown': sorted(unknown), 'coverage': 'partial' if unknown else 'complete'}
