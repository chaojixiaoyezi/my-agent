"""Owner 私有告警话题锚点；未知发送结果不得触发第二个根消息。

v2 权威是单事件记录。新 Owner 的第一条保留旧路径，兼容原件检查；其余记录按
哈希前缀分区。ready 只固定布局，不保存发送状态。历史全部耐久导入后才发布 ready；
正常操作只取同事件的线程/进程锁，不读取或重写全量历史。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import (
    _portable_directory,
    _supports_dir_fd,
    ensure_private_dir,
    open_directory_beneath,
    read_bytes_beneath,
    write_text_atomic_beneath,
)

MAX_RECORD_BYTES = 16777216  # 保留原生产读取上限；写入也检查 UTF-8 字节上限。
_ROOT_PARTS = ('data', 'alert_topic_anchors.json')
_STORE_PARTS = ('data', 'alert_topic_anchors')
_READY_PARTS = (*_STORE_PARTS, 'ready.json')
_SCHEMA = 'alert_topic_anchors.v2'
_READY_SCHEMA = 'alert_topic_anchors.ready.v2'


# LLM: 锚点身份由显式来源、Owner、目标和事件决定。
# 函数用途: 计算稳定告警键。
def make_alert_key(source, owner_id, target_chat_id, event_id):
    values = (source, owner_id, target_chat_id, event_id)
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError('alert identity must be explicit')
    return hashlib.sha256('|'.join(values).encode('utf-8')).hexdigest()


# LLM: Keys are exact SHA-256 identities, never relative paths.
# 函数用途: 校验用于点记录和锁的事件键。
def _validate_key(alert_key):
    if not isinstance(alert_key, str) or not re.fullmatch('[0-9a-f]{64}', alert_key):
        raise ValueError('invalid alert key')


# LLM: Fixed directories use existing private no-follow primitives on both supported paths.
# 函数用途: 创建一条受管私有目录链。
def _directory(root, parts):
    if _supports_dir_fd():
        os.close(open_directory_beneath(root, parts, create=True))
    else:
        _portable_directory(root, parts, create=True)


# LLM: Layout preparation does not initialize/reset any old anchor authority.
# 函数用途: 准备 Owner 私有存储根，不读取全量历史。
def _storage_root(owner_home):
    root = Path(owner_home)
    ensure_private_dir(root)
    _directory(root, _STORE_PARTS)
    return root


# LLM: Hash partitioning bounds directory fan-out; the complete key remains in the leaf.
# 函数用途: 定位唯一事件记录与其同键锁。
def _record_parts(alert_key):
    _validate_key(alert_key)
    digest = hashlib.sha256(alert_key.encode('ascii')).hexdigest()
    return (*_STORE_PARTS, digest[:2], alert_key + '.json')


# LLM: Non-finite JSON must not become successfully written but unreadable canonical state.
# 函数用途: 拒绝非标准 JSON 数值。
def _reject_constant(value):
    raise ValueError('invalid alert JSON constant: ' + value)


# LLM: Check the exact production reader limit before replacement, not after durable mutation.
# 函数用途: 编码一份有界且可重读的原子记录。
def _encode(data, *, limit=MAX_RECORD_BYTES):
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n'
    if len(text.encode('utf-8')) > limit:
        raise ValueError('alert record exceeds read limit')
    return text


# LLM: This immutable layout record is not alert state or dispatch permission.
# 函数用途: 有界读取已完成的布局切换标记。
def _read_ready(root):
    raw = read_bytes_beneath(root, _READY_PARTS, max_bytes=4096)
    if raw is None:
        return None
    data = json.loads(raw, parse_constant=_reject_constant)
    if not isinstance(data, dict) or data.get('schema_version') != _READY_SCHEMA:
        raise ValueError('invalid alert layout marker')
    primary = data.get('primary_key')
    if primary != '':
        _validate_key(primary)
    if not isinstance(data.get('migration_id'), str) or not re.fullmatch('[0-9a-f]{32}', data['migration_id']):
        raise ValueError('invalid alert layout identity')
    return data


# LLM: Admission precedes event locking; ready callers never take an owner-wide lock.
# 函数用途: 准备同事件锁；迁移未完成时不给调用方返回空旧记录。
def _prepare(owner_home, alert_key):
    _validate_key(alert_key)
    root = _storage_root(owner_home)
    if _read_ready(root) is None:
        from .alert_topic_anchor_migration import MigrationPending, migrate_legacy

        if not migrate_legacy(root, max_records=128, primary_key=alert_key)['complete']:
            raise MigrationPending('alert anchor migration is incomplete; retry bounded migration')
    parts = _record_parts(alert_key)
    _directory(root, parts[:-1])
    return root, root.joinpath(*parts)


# LLM: The old path holds at most one primary record, or an empty migrated-format fence.
# 函数用途: 安全读取旧路径单记录槽；缺失或旧格式不能重置成空。
def _read(root):
    raw = read_bytes_beneath(root, _ROOT_PARTS, max_bytes=MAX_RECORD_BYTES)
    if raw is None:
        raise ValueError('missing alert primary record')
    data = json.loads(raw, parse_constant=_reject_constant)
    if not isinstance(data, dict) or data.get('schema_version') != _SCHEMA:
        raise ValueError('invalid alert anchor record')
    return data


# LLM: Same-event (or incomplete-migration) lock owns writes; correlation shares the replace.
# 函数用途: 原子保存一条有界记录，不再累积整个 Owner 历史。
def _write(root, data):
    key = data['alert_key']
    layout = {'primary_key': data['primary_key'], 'migration_id': data['migration_id']}
    _validate_record(data, key, layout, staging=True)
    parts = _ROOT_PARTS if not key or key == data['primary_key'] else _record_parts(key)
    write_text_atomic_beneath(root, parts, _encode(data))


# LLM: Preserve extra legacy metadata, but require complete state and provider identifiers.
# 函数用途: 校验锚点必要事实，不把坏记录解释成缺失。
def _validate_row(row):
    if (not isinstance(row, dict) or row.get('state') not in {'pending', 'sent', 'unknown'}
            or not isinstance(row.get('root_message_id'), str) or not isinstance(row.get('thread_id'), str)
            or 'updated_at' not in row or row['state'] == 'sent' and not row['root_message_id']):
        raise ValueError('invalid alert anchor entry')
    _encode(row)


# LLM: Exact key, layout generation, state and original dispatch are one atomic authority.
# 函数用途: 校验单记录封装，迁移暂存允许暂时只有派发键。
def _validate_record(data, key, layout, *, staging=False):
    if key:
        _validate_key(key)
    if (not isinstance(data, dict) or data.get('schema_version') != _SCHEMA
            or data.get('alert_key') != key or data.get('primary_key') != layout['primary_key']
            or data.get('migration_id') != layout['migration_id']):
        raise ValueError('invalid alert record identity')
    rows, dispatches = data.get('anchors'), data.get('root_delivery_keys')
    if (not isinstance(rows, dict) or not isinstance(dispatches, dict)
            or set(rows) - {key} or set(dispatches) - {key}
            or any(not isinstance(value, str) for value in dispatches.values())):
        raise ValueError('invalid alert point record')
    if key in rows:
        _validate_row(rows[key])
    elif not staging:
        raise ValueError('missing alert anchor entry')


# LLM: An empty primary slot is only initial committed layout, not empty legacy recovery.
# 函数用途: 按准确键读取一条记录，并核对布局绑定。
def _read_key(root, key, layout, *, staging=False):
    primary = key == layout['primary_key']
    if primary:
        data = _read(root)
    else:
        raw = read_bytes_beneath(root, _record_parts(key), max_bytes=MAX_RECORD_BYTES)
        if raw is None:
            return None
        data = json.loads(raw, parse_constant=_reject_constant)
    _validate_record(data, key, layout, staging=staging or primary and not data.get('anchors'))
    return data


# LLM: Public callers receive the unchanged row shape, not layout or dispatch metadata.
# 函数用途: 构造包含原始派发关联的单记录封装。
def _record(key, layout, row=None, dispatch=''):
    return {'schema_version': _SCHEMA, 'alert_key': key, 'primary_key': layout['primary_key'],
        'migration_id': layout['migration_id'], 'anchors': {key: dict(row)} if row is not None else {},
        'root_delivery_keys': {key: dispatch} if dispatch else {}}


# LLM: Ready cannot disappear into an absent-anchor result after event admission.
# 函数用途: 在事件锁内重新核对固定布局并读取当前事件。
def _current(root, alert_key):
    layout = _read_ready(root)
    if layout is None:
        raise ValueError('missing alert layout marker')
    data = _read_key(root, alert_key, layout)
    return layout, data or _record(alert_key, layout)


# LLM: 同事件判断、pending 与原始派发键同一锁/原子记录；布尔只给本次发送，不是持久重放许可。
# 函数用途: 预留锚点并区分唯一首发者与已有预留。
def _reserve_for_delivery(owner_home, alert_key, *, now, dispatch_key=''):
    root, lock = _prepare(owner_home, alert_key)
    with locked_json_path(lock):
        layout, data = _current(root, alert_key)
        if alert_key in data['anchors']:
            row = data['anchors'][alert_key]
            return dict(row), False, data.get('root_delivery_keys', {}).get(alert_key, '')
        row = {'root_message_id': '', 'thread_id': '', 'state': 'pending', 'updated_at': now}
        if not isinstance(dispatch_key, str):
            raise ValueError('invalid alert dispatch key')
        _write(root, _record(alert_key, layout, row, dispatch_key))
        return dict(row), True, dispatch_key


# LLM: 公共预留接口幂等，不把重放解释成新发送许可。
# 函数用途: 为告警预留根锚点。
def reserve(owner_home, alert_key, *, now) -> dict:
    row, _, _ = _reserve_for_delivery(owner_home, alert_key, now=now)
    return row


# LLM: 提供方确认只更新同事件原子记录；原始派发键保留，不接受逻辑回执替代根 ID。
# 函数用途: 标记已确认的根与线程。
def mark_sent(owner_home, alert_key, *, root_message_id, thread_id, now) -> dict:
    if not isinstance(root_message_id, str) or not root_message_id or not isinstance(thread_id, str):
        raise ValueError('confirmed provider root is required')
    root, lock = _prepare(owner_home, alert_key)
    with locked_json_path(lock):
        _, data = _current(root, alert_key)
        previous = data['anchors'].get(alert_key, {})
        if previous.get('root_message_id') and previous['root_message_id'] != root_message_id:
            raise ValueError('alert root cannot change after confirmation')
        if previous.get('state') == 'sent' and (not thread_id or previous.get('thread_id')):
            return dict(previous)
        row = {'root_message_id': root_message_id, 'thread_id': thread_id, 'state': 'sent', 'updated_at': now}
        data['anchors'][alert_key] = row
        _write(root, data)
        return dict(row)


# LLM: 同事件锁内未知不清除已确认根或原始派发键，不重新授予发送许可。
# 函数用途: 保留未知发送状态。
def mark_unknown(owner_home, alert_key, *, now) -> dict:
    root, lock = _prepare(owner_home, alert_key)
    with locked_json_path(lock):
        _, data = _current(root, alert_key)
        previous = data['anchors'].get(alert_key, {})
        if previous.get('state') == 'sent':
            return dict(previous)
        row = {'root_message_id': previous.get('root_message_id', ''),
            'thread_id': previous.get('thread_id', ''), 'state': 'unknown', 'updated_at': now}
        data['anchors'][alert_key] = row
        _write(root, data)
        return dict(row)
