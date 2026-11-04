# LLM: 新完成直接提交摘要事务；旧spool只按固定批次迁移，持久凭据提交后才允许ACK。
# 模块用途: 校验摘要、维护旧格式迁移边界并提供聚合只读投影。
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from functools import lru_cache

from ..assets import MAX_ASSET_BYTES, asset_root_lock_held, safe_asset_id
from ..common.log_redaction import redact_sensitive_value
from ..common.nofollow_fs import (
    read_bytes_beneath,
    unlink_file_beneath,
    write_text_atomic_beneath,
)
from .run_list_index import pending_names
from .run_summary_index import RunSummaryIndex, generation


# LLM: kind只定义受管路径命名空间，ID始终为单段；所有实际IO继续经过nofollow原语。
# 函数用途: 返回单一资产的spool相对目录，不创建或解析外部路径。
def _parts(kind, asset_id, lifecycle=''):
    if kind not in {'script', 'workflow'}:
        raise ValueError('运行摘要kind必须是script或workflow')
    parts = ('.run-summary-spool', kind, safe_asset_id(asset_id))
    return (*parts, safe_asset_id(lifecycle)) if lifecycle else parts


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


# LLM: 旧内部入口仍可发布迁移输入；生产完成入口使用record，不再积累spool。
# 函数用途: 原子发布一条旧格式输入，保留兼容及迁移回归的路径检查。
def append(owner_home, kind, asset_id, record):
    parts = _parts(kind, asset_id)
    collection, filename = ('scripts', 'script.json') if kind == 'script' else ('workflows', 'workflow.json')
    raw = read_bytes_beneath(owner_home, (collection, asset_id, filename), max_bytes=MAX_ASSET_BYTES)
    if raw is not None:
        parts = _parts(kind, asset_id, generation(json.loads(raw).get('metadata', {})))
    value = normalize_record(record)
    write_text_atomic_beneath(owner_home, (*parts, uuid.uuid4().hex + '.json'),
                              json.dumps(value, ensure_ascii=False, allow_nan=False))
    return value


# LLM: 只枚举未ACK的一批叶；排序跨批次由聚合边界处理，目录FD由通用helper关闭。
# 函数用途: 读取固定批次的完整旧记录，后续ACK只触及这个快照。
def _pending(owner_home, parts, *, record_limit_bytes=16384):
    names = pending_names(owner_home, parts, 32)
    records = []
    for name in sorted(names):
        if name.startswith('.') and '.tmp-' in name:
            continue
        if not re.fullmatch(r'[0-9a-f]{32}\.json', name):
            raise ValueError('运行摘要spool叶名称无效')
        try:
            raw = read_bytes_beneath(owner_home, (*parts, name), max_bytes=record_limit_bytes)
        except FileNotFoundError:
            continue  # 另一消费者已完成事务及ACK，唯一运行凭据仍在SQLite中。
        if raw is None:
            continue
        value = json.loads(raw)
        if not isinstance(value, dict) or not value.get('run_id') or not value.get('run_at'):
            raise ValueError('运行摘要spool缺少持久身份或时间')
        records.append((name, normalize_record(value)))
    return tuple(sorted(records, key=lambda item: (item[1]['run_at'], item[1]['run_id'], item[0])))


# LLM: 多个迁移者可以读取同一叶，事务内run_id去重使重复ACK只需容忍准确叶已消失。
# 函数用途: 清理已经提交凭据的旧输入，不吞掉其它IO错误。
def _ack(owner_home, parts, pending):
    for name, _ in pending:
        try:
            unlink_file_beneath(owner_home, (*parts, name))
        except FileNotFoundError:
            pass


# LLM: 每次生产完成同时消费至多一批旧叶；新记录与聚合同事务，提交结果不依赖资产定义写回。
# 函数用途: 不取Owner资产根锁提交最终观察，并有界推进旧spool恢复。
def record(owner_home, kind, asset_id, value, summary, metadata):
    parts = _parts(kind, asset_id, generation(metadata))
    value = normalize_record(value)
    pending = _pending(owner_home, parts)
    result, _ = RunSummaryIndex(owner_home).ingest(kind, asset_id, summary, metadata,
                                                [value], legacy_records=pending)
    _ack(owner_home, parts, pending)
    return result


# LLM: 故障告警按错误类型有限缓存，不带spool正文或认证材料。
# 函数用途: 为只读摘要投影限频保存警告时刻。
@lru_cache(maxsize=128)
def _warning_clock(error_type):
    return [float('-inf')]


# LLM: 公开投影只读取一个聚合，不回放运行行或旧spool；迁移由生产record与显式fold推进。
# 函数用途: 在旧公开读口返回固定大小摘要，损坏时保留定义基准并记录类型日志。
def project(owner_home, kind, asset_id, summary, metadata):
    original = summary
    try:
        _parts(kind, asset_id)
        return RunSummaryIndex(owner_home).project(kind, asset_id, summary, metadata)
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        error_type = type(exc).__name__
        clock, now = _warning_clock(error_type), time.monotonic()
        if now - clock[0] >= 60:
            clock[0] = now
            logging.getLogger(__name__).warning('summary projection failed error_type=%s', error_type,
                                                extra={'error_type': error_type})
        return original


# LLM: SQLite先提交聚合和凭据，旧定义仅保存固定大小快照；定义写回或ACK失败均可重放。
# 函数用途: 在原根锁内迁移一批旧输入并保存摘要快照，不扩充metadata身份历史。
def fold(owner_home, kind, asset_id, summary, metadata, commit):
    parts = _parts(kind, asset_id, generation(metadata))
    if not asset_root_lock_held(owner_home):
        raise RuntimeError('fold必须持有原资产根锁')
    pending = _pending(owner_home, parts)
    before_summary, before_metadata = summary, metadata
    summary, complete = RunSummaryIndex(owner_home).ingest(kind, asset_id, summary, metadata,
                                                         [], legacy_records=pending)
    metadata = dict(metadata)
    if complete:
        metadata.pop('_run_summary_spool', None)
    if summary != before_summary or metadata != before_metadata:
        commit(summary, metadata)
    _ack(owner_home, parts, pending)
    return summary, metadata
