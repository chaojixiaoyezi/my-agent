"""模型调用诊断投影：沿 owner 原 runtime.db 写入，不保存提示词或生成正文。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from itertools import combinations, islice
from pathlib import Path
from types import SimpleNamespace

from ..gateway_parts.business_api_v13 import v13_require_access
from ..plugin_host.errors import HostAPIError
from ..runtime_context import current_task_attributes
from ..runtime_db.schema import runtime_db_path
from ..settings.parameter_changes import mask_value

try:
    from . import gap158_limits as _limits
except ImportError:
    # W0 未合入时仍严格使用接口契约默认值，不增加第二份配置来源。
    _limits = SimpleNamespace(
        LLM_RECORDS_METADATA_MAX_BYTES=4096,
        LLM_RECORDS_AGGREGATE_DEFAULT_WINDOW_SECONDS=604800,
        LLM_RECORDS_AGGREGATE_MAX_WINDOW_SECONDS=2678400,
        LLM_RECORDS_AGGREGATE_MAX_MODELS=200,
        LLM_RECORDS_PRUNE_BATCH_ROWS=500,
    )

_cursor_key = os.urandom(32)
_table_ddl = """CREATE TABLE IF NOT EXISTS llm_call_records (
record_id TEXT PRIMARY KEY, call_id TEXT NOT NULL UNIQUE, owner_id TEXT NOT NULL,
request_id TEXT NOT NULL DEFAULT '', run_id TEXT NOT NULL DEFAULT '',
task_id TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT '', thread_id TEXT NOT NULL DEFAULT '',
model TEXT NOT NULL DEFAULT '', backend TEXT NOT NULL DEFAULT '', profile_id TEXT NOT NULL DEFAULT '',
purpose TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
cached_tokens INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0,
error_code TEXT NOT NULL DEFAULT '', metadata_json TEXT NOT NULL DEFAULT '{}',
started_at REAL NOT NULL, finished_at REAL, created_at REAL NOT NULL)"""
_indexes = (
    'CREATE INDEX IF NOT EXISTS idx_llm_records_owner_created ON llm_call_records(owner_id, created_at DESC, record_id DESC)',
    'CREATE INDEX IF NOT EXISTS idx_llm_records_owner_task ON llm_call_records(owner_id, task_id, created_at DESC)',
    'CREATE INDEX IF NOT EXISTS idx_llm_records_owner_model ON llm_call_records(owner_id, model, created_at DESC)',
    # 相同时间戳的稳定分页需要最后一个排序键，不能依赖 rowid 的隐式顺序。
    'CREATE INDEX IF NOT EXISTS idx_llm_records_task_keyset ON llm_call_records(owner_id, task_id, created_at DESC, record_id DESC)',
    'CREATE INDEX IF NOT EXISTS idx_llm_records_model_keyset ON llm_call_records(owner_id, model, created_at DESC, record_id DESC)',
)
# 所有可组合的等值筛选都需要紧接排序键；只加单列索引仍会在稀疏组合上扫完整个 owner。
_filter_fields = ('task_id', 'role', 'model', 'status')
_indexes += tuple(
    'CREATE INDEX IF NOT EXISTS idx_llm_records_filter_' + '_'.join(fields) +
    ' ON llm_call_records(owner_id, ' + ', '.join(fields) + ', created_at DESC, record_id DESC)'
    for width in range(1, len(_filter_fields) + 1)
    for fields in combinations(_filter_fields, width)
    if fields not in {('task_id',), ('model',)}
)


@contextmanager
def _database(owner_home):
    """只补本扩展表，不实例化重型 Agent 或另建业务库。"""
    path = runtime_db_path(Path(owner_home))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(_table_ddl)
        for statement in _indexes:
            conn.execute(statement)
        with conn:
            yield conn
    finally:
        conn.close()


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True, allow_nan=False)


def _metadata(raw):
    """先移除正文再复用参数台账脱敏口；只接纳有界 JSON，不序列化任意对象。"""
    budget = [200]

    def clean(value, depth=0):
        budget[0] -= 1
        if budget[0] < 0 or depth > 12:
            return None
        if isinstance(value, dict):
            return {str(k)[:128]: clean(v, depth + 1) for k, v in islice(value.items(), 100)
                    if str(k).lower() not in {
                        'prompt', 'prompts', 'output', 'input', 'messages', 'response', 'content', 'text',
                        'system_prompt', 'user_prompt', 'completion', 'completion_text', 'response_text',
                    }}
        if isinstance(value, (list, tuple)):
            return [clean(v, depth + 1) for v in islice(value, 100)]
        if isinstance(value, str):
            return value[:_limits.LLM_RECORDS_METADATA_MAX_BYTES]
        if value is None or type(value) in (bool, int):
            return value
        if isinstance(value, float) and math.isfinite(value):
            return value
        return None

    value = clean(raw if isinstance(raw, dict) else {})
    # mask_value 是 parameter_changes 实际使用的唯一脱敏 helper；逐项保留非秘密结构。
    # 同形 helper 与它同源，避免把 dict 先变成文本后丢失嵌套凭据语义。
    from ..settings.user_config_capability import masked_structure

    value = masked_structure('metadata', value)
    encoded = _json(value)
    while len(encoded.encode('utf-8')) > _limits.LLM_RECORDS_METADATA_MAX_BYTES and value:
        value.pop(next(reversed(value)))
        encoded = _json(value)
    return encoded


def _status(record):
    if record.status in {'finished', 'completed'}:
        return 'completed'
    if record.status == 'cancelled' or record.error_type in {
        'CancelledError', 'CanceledError', 'ToolCancelled', 'GeneratorExit', 'KeyboardInterrupt',
    }:
        return 'cancelled'
    return 'failed'


def make_record_sink(agent):
    """闭包创建无 I/O；core 在绑定 home_paths 之前安装，调用时才解析真实 owner。"""
    clock_offset = time.time() - time.monotonic()

    def record_sink(record):
        from ..settings.model_scope import deployment_model_config

        config = deployment_model_config(agent)
        if not bool(getattr(config, 'llm_call_records_enabled', False)):
            return
        paths = agent.home_paths
        owner_id = str(paths.owner_id)
        attrs = current_task_attributes(agent) or {}
        metadata = record.metadata
        terminal_at = next((v for v in (record.finished_at, record.failure_at, record.timeout_at)
                            if v is not None), record.last_activity_at)
        payload = {
            'record_id': 'llm-' + hashlib.sha256(record.call_id.encode()).hexdigest(),
            'call_id': record.call_id, 'owner_id': owner_id,
            'request_id': str(record.request_id)[:128], 'run_id': str(record.run_id)[:128],
            'task_id': str(attrs.get('task_id') or metadata.get('task_id') or '')[:128],
            'role': str(attrs.get('role') or metadata.get('role') or '')[:64],
            'thread_id': str(attrs.get('thread_id') or metadata.get('thread_id') or '')[:128],
            'model': str(record.model)[:128], 'backend': str(record.backend)[:64],
            'profile_id': str(metadata.get('profile_id') or '')[:128],
            'purpose': str(metadata.get('purpose') or '')[:64], 'status': _status(record),
            'input_tokens': max(0, record.accounted_input_tokens if record.status == 'finished' else record.input_tokens),
            'output_tokens': max(0, record.output_tokens), 'cached_tokens': max(0, record.cached_input_tokens),
            'latency_ms': max(0, round((record.total_latency_seconds or 0) * 1000)),
            'error_code': mask_value('error_code', record.error_code)[:64],
            'metadata_json': _metadata(metadata),
            'started_at': max(0, clock_offset + record.started_at),
            'finished_at': max(0, clock_offset + terminal_at), 'created_at': time.time(),
        }
        with _database(paths.owner_home_dir) as conn:
            columns = ','.join(payload)
            conn.execute(f'INSERT OR IGNORE INTO llm_call_records ({columns}) VALUES '
                         f'({",".join("?" for _ in payload)})', tuple(payload.values()))
            _prune(conn, owner_id, max(0, int(getattr(config, 'llm_call_records_max_rows', 50000))))

    return record_sink


def _prune(conn, owner_id, max_rows):
    """先按索引定位保留边界，每笔 DELETE 严格封顶，不读全表回写。"""
    if max_rows == 0:
        return
    boundary = conn.execute(
        'SELECT created_at, record_id FROM llm_call_records WHERE owner_id=? '
        'ORDER BY created_at DESC, record_id DESC LIMIT 1 OFFSET ?', (owner_id, max_rows),
    ).fetchone()
    if boundary is None:
        return
    while True:
        deleted = conn.execute(
            'DELETE FROM llm_call_records WHERE owner_id=? AND record_id IN ('
            'SELECT record_id FROM llm_call_records WHERE owner_id=? AND (created_at, record_id)<=(?,?) '
            'ORDER BY created_at, record_id LIMIT ?)',
            (owner_id, owner_id, boundary['created_at'], boundary['record_id'], _limits.LLM_RECORDS_PRUNE_BATCH_ROWS),
        ).rowcount
        if deleted < _limits.LLM_RECORDS_PRUNE_BATCH_ROWS:
            return


def _dto(row):
    value = dict(row)
    value.pop('owner_id')
    value.pop('created_at')
    value['metadata'] = json.loads(value.pop('metadata_json'))
    if value['finished_at'] is None:
        value.pop('finished_at')
    return value


def _cursor(binding, position=None, token=None):
    """签名只绑定 owner、过滤与 keyset，不是授权；重启或换筛选明确过期。"""
    if token:
        try:
            if not isinstance(token, str) or len(token) > 2048:
                raise ValueError('cursor')
            body, signature = json.loads(base64.b64decode(token, altchars=b'-_', validate=True))
            expected = hmac.digest(_cursor_key, _json(body).encode(), 'sha256').hex()
            if (not hmac.compare_digest(signature, expected) or body['binding'] != binding
                    or len(body['position']) != 2 or not isinstance(body['position'][0], (int, float))
                    or not math.isfinite(body['position'][0]) or not isinstance(body['position'][1], str)):
                raise ValueError('cursor')
            return body['position']
        except (ValueError, TypeError, KeyError, UnicodeError) as error:
            raise HostAPIError('CURSOR_EXPIRED') from error
    body = {'binding': binding, 'position': position}
    signature = hmac.digest(_cursor_key, _json(body).encode(), 'sha256').hex()
    return base64.urlsafe_b64encode(_json([body, signature]).encode()).decode()


def _filters(call):
    clauses, args = ['owner_id=?'], [call.owner_id]
    for key in ('task_id', 'role', 'model', 'status'):
        if key in call.params:
            clauses.append(key + '=?')
            args.append(call.params[key])
    for key, operator in (('since', '>='), ('until', '<=')):
        if key in call.params:
            clauses.append('created_at' + operator + '?')
            args.append(call.params[key])
    return ' AND '.join(clauses), args


def _list(conn, call):
    limit = call.params.get('limit', 50)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise HostAPIError('INVALID_SCHEMA')
    where, args = _filters(call)
    binding = hashlib.sha256(_json([call.owner_id, str(Path(call.owner_home).resolve()), call.action,
                                   {k: v for k, v in call.params.items() if k not in {'cursor', 'limit'}}]).encode()).hexdigest()
    if call.params.get('cursor'):
        position = _cursor(binding, token=call.params['cursor'])
        where += ' AND (created_at, record_id)<(?,?)'
        args.extend(position)
    rows = conn.execute('SELECT * FROM llm_call_records WHERE ' + where +
                        ' ORDER BY created_at DESC, record_id DESC LIMIT ?', (*args, limit + 1)).fetchall()
    page = rows[:limit]
    for row in page:
        v13_require_access(call, dict(row))
    more = len(rows) > limit
    token = _cursor(binding, [page[-1]['created_at'], page[-1]['record_id']]) if more else ''
    result = {'items': [_dto(row) for row in page], 'next_cursor': token,
              'coverage': 'partial' if more else 'complete'}
    if call.action == 'llm_records.by_task':
        totals = conn.execute(
            'SELECT COUNT(*) calls, COALESCE(SUM(status="failed"),0) failed, '
            'COALESCE(SUM(input_tokens),0) input_tokens, COALESCE(SUM(output_tokens),0) output_tokens, '
            'COALESCE(SUM(cached_tokens),0) cached_tokens FROM llm_call_records WHERE owner_id=? AND task_id=?',
            (call.owner_id, call.params['task_id']),
        ).fetchone()
        result.update(task_id=call.params['task_id'], totals=dict(totals))
    else:
        result['enabled'] = True
    return result


def _by_model(conn, call):
    until = float(call.params.get('until', call.clock()))
    since = float(call.params.get('since', max(0, until - _limits.LLM_RECORDS_AGGREGATE_DEFAULT_WINDOW_SECONDS)))
    if not (math.isfinite(since) and math.isfinite(until) and 0 <= since <= until):
        raise HostAPIError('INVALID_SCHEMA')
    bounded = max(since, until - _limits.LLM_RECORDS_AGGREGATE_MAX_WINDOW_SECONDS)
    rows = conn.execute(
        'SELECT model, COUNT(*) calls, SUM(status="failed") failed, SUM(input_tokens) input_tokens, '
        'SUM(output_tokens) output_tokens, CAST(AVG(latency_ms) AS INTEGER) avg_latency_ms '
        'FROM llm_call_records WHERE owner_id=? AND created_at>=? AND created_at<=? AND model<>"" '
        'GROUP BY model ORDER BY model LIMIT ?',
        (call.owner_id, bounded, until, _limits.LLM_RECORDS_AGGREGATE_MAX_MODELS + 1),
    ).fetchall()
    return {'window': {'since': bounded, 'until': until},
            'items': [dict(row) for row in rows[:_limits.LLM_RECORDS_AGGREGATE_MAX_MODELS]],
            'coverage': 'partial' if bounded != since or len(rows) > _limits.LLM_RECORDS_AGGREGATE_MAX_MODELS else 'complete'}


def execute(call):
    """供 V13 分片调用；身份与权限始终来自已签发 call，不消费 params 中的 owner。"""
    v13_require_access(call)
    if call.action == 'llm_records.list' and not bool(
            getattr(call.context.host.config, 'llm_call_records_enabled', False)):
        return {'enabled': False, 'items': [], 'next_cursor': '', 'coverage': 'complete'}
    with _database(call.owner_home) as conn:
        if call.action in {'llm_records.list', 'llm_records.by_task'}:
            return _list(conn, call)
        if call.action == 'llm_records.by_model':
            return _by_model(conn, call)
        if call.action not in {'llm_records.get', 'llm_records.delete'}:
            raise HostAPIError('INVALID_SCHEMA')
        record_id = call.params['record_id']
        row = conn.execute('SELECT * FROM llm_call_records WHERE owner_id=? AND record_id=?',
                           (call.owner_id, record_id)).fetchone()
        if row is not None:
            v13_require_access(call, dict(row))
        if call.action == 'llm_records.get':
            if row is None:
                raise HostAPIError('RESOURCE_NOT_FOUND', 404)
            return {'record': _dto(row)}
        v13_require_access(call)
        deleted = conn.execute('DELETE FROM llm_call_records WHERE owner_id=? AND record_id=?',
                               (call.owner_id, record_id)).rowcount > 0
        return {'deleted': deleted, 'record_id': record_id}
