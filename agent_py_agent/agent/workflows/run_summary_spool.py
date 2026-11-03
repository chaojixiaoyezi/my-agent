# LLM: 追加不取资产根锁；fold必须由调用者持原根锁，摘要与幂等凭据必须同一事务提交后才能ACK。
# 模块用途: 按资产安全暂存运行摘要，允许并发追加、锁内合并和崩溃重放。
from __future__ import annotations

import json
import logging
import math
import re
import time
import uuid
from datetime import datetime, timezone
from functools import lru_cache

from ..assets import asset_root_lock_held, next_run_summary, safe_asset_id
from ..assets.descriptor import RuntimeSummary
from ..common.log_redaction import redact_sensitive_value
from ..common.nofollow_fs import (
    list_names_beneath,
    read_bytes_beneath,
    unlink_file_beneath,
    write_text_atomic_beneath,
)


# LLM: kind只定义受管路径命名空间，ID始终为单段；所有实际IO继续经过nofollow原语。
# 函数用途: 返回单一资产的spool相对目录，不创建或解析外部路径。
def _parts(kind, asset_id):
    if kind not in {'script', 'workflow'}:
        raise ValueError('运行摘要kind必须是script或workflow')
    return ('.run-summary-spool', kind, safe_asset_id(asset_id))


# LLM: 固定JSON字段不授路径或执行权；错误先脱敏再截断，非法数值在落盘前拒绝。
# 函数用途: 规范一次最终运行观察，缺省run_id为独立调用生成唯一身份。
def normalize_record(record, *, error_limit=512):
    if not isinstance(record, dict) or set(record) - {'run_id', 'status', 'duration_ms', 'run_at', 'last_error'}:
        raise ValueError('运行摘要记录字段无效')
    run_id = record.get('run_id', '')
    if run_id == '':
        run_id = uuid.uuid4().hex
    safe_asset_id(run_id)
    status, duration = record.get('status'), record.get('duration_ms')
    if not isinstance(status, str) or not status.strip() or len(status) > 128:
        raise ValueError('运行摘要状态无效')
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
        raise ValueError('运行耗时必须为非负有限数')
    run_at = record.get('run_at')
    if run_at is None:
        run_at = datetime.now(timezone.utc).isoformat()
    if not isinstance(run_at, str) or not run_at.strip():
        raise ValueError('运行时间必须为非空时间字符串')
    return {'run_id': run_id, 'status': status, 'duration_ms': duration,
            'run_at': run_at,
            'last_error': str(redact_sensitive_value(str(record.get('last_error') or '')))[:error_limit]}


# LLM: 每次追加独占UUID叶且原子发布，fold不截断共享文件，两个追加者不会互相覆盖或与ACK竞争同名叶。
# 函数用途: 不取得Owner资产锁地持久化一条完整运行摘要并返回其规范记录。
def append(owner_home, kind, asset_id, record):
    parts = _parts(kind, asset_id)
    value = normalize_record(record)
    write_text_atomic_beneath(owner_home, (*parts, uuid.uuid4().hex + '.json'),
                              json.dumps(value, ensure_ascii=False, allow_nan=False))
    return value


# LLM: 只枚举当前资产且不改游标；临时原子写叶尚未发布，损坏、链接或陌生正式叶必须失败关闭。
# 函数用途: 读取一个完整spool快照，后续ACK只能删除这个快照的叶。
def _pending(owner_home, parts, *, record_limit_bytes=16384):
    try:
        names = list_names_beneath(owner_home, parts)
    except FileNotFoundError:
        return ()
    records = []
    for name in sorted(names):
        if name.startswith('.') and '.tmp-' in name:
            continue
        if not re.fullmatch(r'[0-9a-f]{32}\.json', name):
            raise ValueError('运行摘要spool叶名称无效')
        raw = read_bytes_beneath(owner_home, (*parts, name), max_bytes=record_limit_bytes)
        if raw is None:
            raise FileNotFoundError('运行摘要快照叶消失')
        value = json.loads(raw)
        if not isinstance(value, dict) or not value.get('run_id') or not value.get('run_at'):
            raise ValueError('运行摘要spool缺少持久身份或时间')
        records.append((name, normalize_record(value)))
    return tuple(sorted(records, key=lambda item: (item[1]['run_at'], item[1]['run_id'], item[0])))


# LLM: 故障告警按错误类型有限缓存，不带spool正文或认证材料。
# 函数用途: 为只读摘要投影限频保存警告时刻。
@lru_cache(maxsize=128)
def _warning_clock(error_type):
    return [float('-inf')]


# LLM: 只读投影不授执行权、不写盘、不ACK；损坏spool有类型日志但不能破坏原资产只读入口。
# 函数用途: 在旧公开读口展示尚未fold的最新摘要，canonical提交仍只由fold负责。
def project(owner_home, kind, asset_id, summary, metadata):
    original = summary
    try:
        seen = set(metadata.get('_run_summary_spool', {}).get('applied_run_ids', ()))
        for _, record in _pending(owner_home, _parts(kind, asset_id)):
            if record['run_id'] not in seen:
                summary = next_run_summary(summary, record['status'], record['duration_ms'], record['run_at'],
                                           last_error=record['last_error'])
                seen.add(record['run_id'])
        return summary
    except (OSError, ValueError, TypeError) as exc:
        error_type = type(exc).__name__
        clock, now = _warning_clock(error_type), time.monotonic()
        if now - clock[0] >= 60:
            clock[0] = now
            logging.getLogger(__name__).warning('summary projection failed error_type=%s', error_type,
                                                extra={'error_type': error_type})
        return original


# LLM: 幂等凭据与摘要由commit同事务持久化；不能独立写ACK账本或淘汰旧run_id，否则崩溃重放会重复计数。
# 函数用途: 在调用者原资产根锁内合并快照，提交成功后清理已处理叶，返回合并结果。
def fold(owner_home, kind, asset_id, summary, metadata, commit):
    parts = _parts(kind, asset_id)
    if not asset_root_lock_held(owner_home):
        raise RuntimeError('fold必须持有原资产根锁')
    pending = _pending(owner_home, parts)
    state = metadata.get('_run_summary_spool', {'schema_version': 'run_summary_spool.v1', 'applied_run_ids': []})
    if (not isinstance(state, dict) or set(state) != {'schema_version', 'applied_run_ids'}
            or state['schema_version'] != 'run_summary_spool.v1'
            or not isinstance(state['applied_run_ids'], list)):
        raise ValueError('运行摘要幂等凭据损坏')
    seen = {safe_asset_id(value) for value in state['applied_run_ids']}
    changed = False
    for _, record in pending:
        if record['run_id'] in seen:
            continue
        updated = next_run_summary(summary, record['status'], record['duration_ms'], record['run_at'],
                                   last_error=record['last_error'])
        summary = RuntimeSummary.from_dict(updated.to_dict())
        seen.add(record['run_id'])
        changed = True
    metadata = dict(metadata)
    if changed:
        metadata['_run_summary_spool'] = {'schema_version': 'run_summary_spool.v1', 'applied_run_ids': sorted(seen)}
        commit(summary, metadata)
    for name, _ in pending:
        unlink_file_beneath(owner_home, (*parts, name))
    return summary, metadata
