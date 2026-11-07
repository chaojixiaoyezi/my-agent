"""有界政策历史：只追加 JSONL，游标固定分段 inode 与读取上界，不改写旧历史。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path

from ..common.directory_lock import locked_private_directory
from ..common.nofollow_fs import (
    append_text_beneath,
    open_directory_beneath,
    open_readonly_file_beneath,
    read_bytes_beneath,
    write_bytes_atomic_beneath,
)
from ..conversation.business_access import BusinessError
from ..user_space.owner_quota import OwnerQuotaChange, owner_quota_enforcer_from_policy

_CURSOR_KEY = os.urandom(32)
_PARTS = ('data', 'business_v13', 'policy_history')


def limit_value(name):
    """W0 尚未合入时沿派工冻结的同值默认；整合后只认公共限制模块。"""
    from importlib import import_module
    defaults = {'POLICY_HISTORY_SEGMENT_BYTES': 4194304, 'POLICY_HISTORY_MAX_SEGMENTS': 8,
                'POLICY_HISTORY_SCAN_MAX_RECORDS': 5000, 'POLICY_TASK_SUMMARY_SCAN_RECORDS': 2000,
                'POLICY_PENDING_MAX_PER_OWNER': 200, 'POLICY_JUDGE_WORKERS': 2}
    try:
        module = import_module('.gap158_limits', __package__)
    except ModuleNotFoundError as error:
        if error.name != __package__ + '.gap158_limits':
            raise
        return defaults[name]
    return getattr(module, name, defaults[name])


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _index(home):
    raw = read_bytes_beneath(home, (*_PARTS, 'index.json'), max_bytes=65536)
    if raw is None:
        return []
    try:
        value = json.loads(raw)
        if (not isinstance(value, list) or len(value) > limit_value('POLICY_HISTORY_MAX_SEGMENTS')
                or any(type(name) is not str or len(name) != 46 or not name.startswith('segment-')
                       or not name.endswith('.jsonl') or any(c not in '0123456789abcdef' for c in name[8:-6])
                       for name in value) or len(value) != len(set(value))):
            raise ValueError('history index')
        return value
    except (ValueError, TypeError) as error:
        raise BusinessError('HOST_NOT_READY') from error


def append_record(owner_home, record):
    """同一 owner 配额→目录锁内追加；仅换段时更新小索引并淘汰最老段。"""
    home = Path(owner_home)
    row = {'record_id': 'policy-' + uuid.uuid4().hex, 'at': time.time(), **record}
    text = _encode(row) + '\n'
    payload_size = len(text.encode('utf-8'))
    segment_bytes = limit_value('POLICY_HISTORY_SEGMENT_BYTES')
    if payload_size > segment_bytes:
        raise BusinessError('LIMIT_EXCEEDED')
    with owner_quota_enforcer_from_policy(home).admission() as admission:
        with locked_private_directory(home, relative_parts=_PARTS, lock_name='.history.lock'):
            names = _index(home)
            size = 0
            if names:
                fd = open_readonly_file_beneath(home, (*_PARTS, names[-1]))
                try:
                    size = os.fstat(fd).st_size
                finally:
                    os.close(fd)
            rotate = not names or size + payload_size > segment_bytes
            dropped = []
            if rotate:
                names.append('segment-' + uuid.uuid4().hex + '.jsonl')
                cap = limit_value('POLICY_HISTORY_MAX_SEGMENTS')
                dropped, names = names[:-cap], names[-cap:]
                size = 0
            target = home.joinpath(*_PARTS, names[-1])
            changes = [OwnerQuotaChange(target, final_size=size + payload_size)]
            if rotate:
                index_raw = (_encode(names) + '\n').encode()
                changes.append(OwnerQuotaChange(home.joinpath(*_PARTS, 'index.json'), final_size=len(index_raw)))
            admission.check(changes)
            append_text_beneath(home, (*_PARTS, names[-1]), text)
            if rotate:
                write_bytes_atomic_beneath(home, (*_PARTS, 'index.json'), index_raw)
                parent = open_directory_beneath(home, _PARTS)
                try:
                    for name in dropped:
                        os.unlink(name, dir_fd=parent)
                finally:
                    os.close(parent)
    return row


def _token(home, kind, filters, state):
    body = _encode({'owner': str(Path(home).absolute()), 'kind': kind, 'filters': filters, 'state': state})
    signature = hmac.digest(_CURSOR_KEY, body.encode(), 'sha256').hex()
    return base64.urlsafe_b64encode(_encode([body, signature]).encode()).decode()


def _unpack(home, kind, filters, token):
    try:
        if type(token) is not str or len(token) > 2048:
            raise ValueError('cursor')
        body, signature = json.loads(base64.b64decode(token, altchars=b'-_', validate=True))
        if not hmac.compare_digest(signature, hmac.digest(_CURSOR_KEY, body.encode(), 'sha256').hex()):
            raise ValueError('cursor')
        value = json.loads(body)
        if (value['owner'] != str(Path(home).absolute()) or value['kind'] != kind
                or value['filters'] != filters):
            raise ValueError('cursor binding')
        return value['state']
    except (ValueError, TypeError, KeyError, UnicodeError) as error:
        raise BusinessError('CURSOR_EXPIRED') from error


def _snapshot(home):
    result = []
    for name in _index(home):
        try:
            fd = open_readonly_file_beneath(home, (*_PARTS, name))
        except FileNotFoundError as error:
            raise BusinessError('CURSOR_EXPIRED') from error
        try:
            info = os.fstat(fd)
            result.append([name, info.st_ino, info.st_size])
        finally:
            os.close(fd)
    return {'segments': result, 'index': 0, 'offset': 0}


def _scan(home, params, *, kind='history', budget=None, result_limit=None):
    """每页按物理行计费；不匹配的行也消耗预算，过滤不能引起无界扫描。"""
    page_limit = params.get('limit', 50)
    if type(page_limit) is not int or not 1 <= page_limit <= 100:
        raise BusinessError('INVALID_SCHEMA')
    filters = {key: value for key, value in params.items() if key not in {'cursor', 'limit'}}
    cursor = params.get('cursor')
    state = _unpack(home, kind, filters, cursor) if cursor else _snapshot(home)
    budget = limit_value('POLICY_HISTORY_SCAN_MAX_RECORDS') if budget is None else budget
    maximum = page_limit if result_limit is None else result_limit
    rows, scanned = [], 0
    try:
        segments, index, offset = state['segments'], state['index'], state['offset']
        if type(index) is not int or type(offset) is not int or not 0 <= index <= len(segments) or offset < 0:
            raise ValueError('position')
        while index < len(segments) and scanned < budget and len(rows) < maximum:
            name, inode, end = segments[index]
            if name not in _index(home):
                raise BusinessError('CURSOR_EXPIRED')
            fd = open_readonly_file_beneath(home, (*_PARTS, name))
            with os.fdopen(fd, 'rb') as source:
                info = os.fstat(source.fileno())
                if info.st_ino != inode or info.st_size < end or offset > end:
                    raise BusinessError('CURSOR_EXPIRED')
                source.seek(offset)
                while source.tell() < end and scanned < budget and len(rows) < maximum:
                    line = source.readline(min(end - source.tell(), limit_value('POLICY_HISTORY_SEGMENT_BYTES')))
                    scanned += 1
                    offset = source.tell()
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError('history row')
                    except (ValueError, UnicodeError):
                        continue
                    if all((row.get(key, 0) >= value if key == 'since' else row.get(key) == value)
                           for key, value in filters.items() if key != 'since') and (
                               'since' not in filters or row.get('at', 0) >= filters['since']):
                        rows.append(row)
                if offset == end:
                    index, offset = index + 1, 0
        state.update(index=index, offset=offset)
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise BusinessError('CURSOR_EXPIRED' if cursor else 'HOST_NOT_READY') from error
    more = index < len(segments)
    return rows, _token(home, kind, filters, state) if more else None, scanned


def history_page(owner_home, params):
    rows, cursor, _ = _scan(owner_home, params)
    return {'items': rows, 'next_cursor': cursor or '', 'coverage': 'partial' if cursor else 'complete'}


def execution(owner_home, execution_id):
    rows, cursor, _ = _scan(owner_home, {'execution_id': execution_id, 'limit': 1})
    if not rows:
        # 有界预算内未命中不能冒充全库不存在；依赖未就绪是该动作已有错误码。
        raise BusinessError('HOST_NOT_READY' if cursor else 'RESOURCE_NOT_FOUND')
    return {'decision': rows[0]}


def task_page(owner_home, params):
    """摘要只统计本次有界历史窗；partial 不冒称全任务累计，分组页不会丢后半组。"""
    return _task_groups(owner_home, params)


def _recent_window(home, snapshot, budget):
    """倒序读固定字节上界内的最新物理行；坏行同样计费，不全读每个分段。"""
    rows, scanned = [], 0
    segments = snapshot['segments']
    names = _index(home)
    for number in range(len(segments) - 1, -1, -1):
        name, inode, end = segments[number]
        if name not in names:
            raise BusinessError('CURSOR_EXPIRED')
        fd = open_readonly_file_beneath(home, (*_PARTS, name))
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if info.st_ino != inode or info.st_size < end:
                raise BusinessError('CURSOR_EXPIRED')
            position, buffer, first = end, b'', True
            while position or buffer:
                while b'\n' not in buffer and position:
                    begin = max(0, position - 8192)
                    source.seek(begin)
                    buffer = source.read(position - begin) + buffer
                    position = begin
                if b'\n' in buffer:
                    buffer, _, line = buffer.rpartition(b'\n')
                else:
                    line, buffer = buffer, b''
                if first and not line:
                    first = False
                    continue
                first = False
                scanned += 1
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        rows.append(row)
                except (ValueError, UnicodeError):
                    pass
                if scanned >= budget:
                    return rows, bool(buffer or position or number)
    return rows, False


def _task_groups(owner_home, params):
    # 固定最新记录窗后仅分页组，不继续前移历史窗口而重复拆分同一任务。
    filters = {'limit': params.get('limit', 50)}
    if type(filters['limit']) is not int or not 1 <= filters['limit'] <= 100:
        raise BusinessError('INVALID_SCHEMA')
    cursor = params.get('cursor')
    if cursor:
        state = _unpack(owner_home, 'task_groups', filters, cursor)
        offset, start = state['group_offset'], state['snapshot']
    else:
        start, offset = _snapshot(owner_home), 0
    budget = limit_value('POLICY_TASK_SUMMARY_SCAN_RECORDS')
    try:
        rows, earlier = _recent_window(owner_home, start, budget)
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise BusinessError('CURSOR_EXPIRED' if cursor else 'HOST_NOT_READY') from error
    groups = {}
    for row in rows:
        if not row.get('task_id'):
            continue
        item = groups.setdefault(row['task_id'], {'task_id': row['task_id'], 'allow': 0, 'ask': 0, 'deny': 0, 'last_decision_at': 0})
        decision = row.get('decision')
        if decision not in {'allow', 'ask', 'deny', 'judge_pending'}:
            continue
        item['ask' if decision == 'judge_pending' else decision] += 1
        item['last_decision_at'] = max(item['last_decision_at'], row['at'])
    items = sorted(groups.values(), key=lambda row: row['task_id'])
    if type(offset) is not int or not 0 <= offset <= len(items):
        raise BusinessError('CURSOR_EXPIRED')
    end = min(offset + filters['limit'], len(items))
    following = None
    if end < len(items):
        following = _token(owner_home, 'task_groups', filters, {'snapshot': start, 'group_offset': end})
    return {'items': items[offset:end], 'next_cursor': following or '',
            'coverage': 'partial' if earlier or following else 'complete'}
