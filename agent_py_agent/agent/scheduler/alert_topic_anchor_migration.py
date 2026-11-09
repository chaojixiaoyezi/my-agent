"""Bounded v1 -> point-record migration; incomplete import is never absence.

One slice owns the old ledger lock, reads a bounded byte window and imports at
most max_records map entries. Only a complete, source-bound JSON parse can fence
the old writer with v2 and publish ready. Durable points precede the cursor;
replaying a cut slice is idempotent. No whole-owner JSON load or directory scan.
"""
from __future__ import annotations

import codecs
import json
import os
import stat
import uuid

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import (
    NoFollowPathError,
    _portable_path,
    _portable_snapshot_parents,
    file_descriptor_change_ns,
    file_identity_snapshot,
    open_readonly_file_beneath,
    read_bytes_beneath,
    write_text_atomic_beneath,
)
from . import alert_topic_anchors as anchors

_PROGRESS_PARTS = (*anchors._STORE_PARTS, 'migration.json')
_PROGRESS_SCHEMA = 'alert_topic_anchors.migration.v2'
_PHASES = {'initializing', 'root', 'top_start', 'top_item', 'top_separator',
    'bucket_start', 'bucket_item', 'bucket_separator', 'tail', 'publish', 'complete'}


# LLM: Class-owned limits preserve all budgets without unregistered module numeric declarations.
# 类用途: 保存分片记录数、读取字节和小块读取的固定预算。
class _MigrationLimits:
    MAX_MIGRATION_BYTES = anchors._AnchorLimits.MAX_RECORD_BYTES + 65536
    MAX_MIGRATION_RECORDS = 512
    _CHUNK_BYTES = 65536


class MigrationPending(RuntimeError):
    """No send permission can be inferred until the bounded import completes."""


class _SliceEnd(Exception):
    """The caller must checkpoint only its last complete grammar boundary."""


# LLM: State contains only a cursor, source binding and finite parser state, never event history.
# 函数用途: 有界读取并严格校验迁移检查点。
def _load_progress(root):
    raw = read_bytes_beneath(root, _PROGRESS_PARTS, max_bytes=4096)
    if raw is None:
        return None
    state = json.loads(raw, parse_constant=anchors._reject_constant)
    if (not isinstance(state, dict) or state.get('schema_version') != _PROGRESS_SCHEMA
            or state.get('phase') not in _PHASES or state.get('kind') not in {'new', 'legacy'}
            or type(state.get('cursor')) is not int or state['cursor'] < 0
            or not isinstance(state.get('seen'), list) or len(state['seen']) > 3
            or set(state['seen']) - {'schema_version', 'anchors', 'root_delivery_keys'}):
        raise ValueError('invalid alert migration checkpoint')
    layout = {'schema_version': anchors._READY_SCHEMA,
        'primary_key': state.get('primary_key'), 'migration_id': state.get('migration_id')}
    if layout['primary_key'] != '':
        anchors._validate_key(layout['primary_key'])
    if not isinstance(layout['migration_id'], str) or len(layout['migration_id']) != 32:
        raise ValueError('invalid alert migration identity')
    if state['kind'] == 'legacy' and (not isinstance(state.get('source'), list)
            or len(state['source']) != 5 or any(type(item) is not int for item in state['source'])):
        raise ValueError('invalid legacy source binding')
    if 'source_change_ns' in state and type(state['source_change_ns']) is not int:
        raise ValueError('invalid legacy native source binding')
    return state


# LLM: Atomic checkpoint replacement follows all point writes; failure permits safe replay only.
# 函数用途: 耐久保存固定大小的迁移进度。
def _save_progress(root, state):
    write_text_atomic_beneath(root, _PROGRESS_PARTS, anchors._encode(state, limit=4096))


# LLM: No content-wide hashing/rescan is needed; changing inode, size or either time fails closed.
# 函数用途: 固定旧账本的文件身份与内容版本。
def _seal(descriptor):
    info = os.fstat(descriptor)
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


# LLM: Strict platforms retain the exact dir_fd primitive; portable follows the existing lexical
# parent policy plus before/FD/current single-link identity checks, never treating OSError as absence.
# 函数用途: 安全打开可流式读取的旧文件，兼容既有 portable 分支且关闭失败时的 FD。
def _open_source(root):
    if anchors._supports_dir_fd():
        return open_readonly_file_beneath(root, anchors._ROOT_PARTS)
    target = _portable_path(root, anchors._ROOT_PARTS, create=False)
    parents = _portable_snapshot_parents(root, anchors._ROOT_PARTS)
    before, before_change = file_identity_snapshot(target)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise NoFollowPathError('legacy alert source is not regular single-link')
    descriptor = os.open(target, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
    try:
        opened = os.fstat(descriptor)
        current, current_change = file_identity_snapshot(_portable_path(root, anchors._ROOT_PARTS, create=False))
        opened_change = file_descriptor_change_ns(descriptor, opened)
        fields = ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                or before_change is None or before_change != opened_change or current_change != opened_change
                or parents != _portable_snapshot_parents(root, anchors._ROOT_PARTS)
                or any(getattr(before, field) != getattr(opened, field)
                    or getattr(opened, field) != getattr(current, field) for field in fields)):
            raise NoFollowPathError('legacy alert source changed during portable open')
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


# LLM: Check both the opened descriptor and the current no-follow name, not a detached old fd.
# 函数用途: 确认本片仍读取原版旧文件，且关闭二次检查使用的 FD。
def _verify_source(root, descriptor, state):
    if _seal(descriptor) != state['source'] or not _native_source_matches(descriptor, state):
        raise ValueError('legacy alert source changed during migration')
    current = _open_source(root)
    try:
        if _seal(current) != state['source'] or not _native_source_matches(current, state):
            raise ValueError('legacy alert source changed during migration')
    finally:
        os.close(current)


def _native_source_matches(descriptor, state):
    # 原五字段绝不换含义；旧Windows断点只有第五项本已绑定同一原生ChangeTime才可安全续办。
    expected = state.get('source_change_ns')
    if expected is None and os.name != 'nt':
        return True
    if expected is None:
        expected = state['source'][4]
    return file_descriptor_change_ns(descriptor, os.fstat(descriptor)) == expected


class _Reader:
    """Small look-ahead buffer with byte-exact UTF-8 cursor and a hard read budget."""

    # LLM: Own no descriptor; caller closes it on every success/failure path.
    # 函数用途: 从检查点开始有界读取，游标始终按原始字节计。
    def __init__(self, descriptor, cursor, budget):
        self.fd, self.offset, self.budget = descriptor, cursor, budget
        self.buffer, self.bytes_read, self.eof = '', 0, False
        self.decoder = codecs.getincrementaldecoder('utf-8')()
        self.json = json.JSONDecoder(parse_constant=anchors._reject_constant)
        os.lseek(descriptor, cursor, os.SEEK_SET)

    # LLM: Every os.read is charged; an incomplete token is retried from a committed boundary.
    # 函数用途: 加载下一小块，不超过本片字节预算。
    def _fill(self):
        remaining = self.budget - self.bytes_read
        if remaining <= 0:
            raise _SliceEnd
        raw = os.read(self.fd, min(_MigrationLimits._CHUNK_BYTES, remaining))
        self.bytes_read += len(raw)
        self.eof = not raw
        self.buffer += self.decoder.decode(raw, final=self.eof)
        if len(self.buffer.encode('utf-8')) > _MigrationLimits.MAX_MIGRATION_BYTES:
            raise ValueError('legacy alert value exceeds migration bound')

    # LLM: Encoding consumed valid text yields the original byte position, including CRLF/Unicode.
    # 函数用途: 推进准确字节游标并释放已消费的缓冲。
    def _consume(self, count):
        self.offset += len(self.buffer[:count].encode('utf-8'))
        self.buffer = self.buffer[count:]

    # LLM: Only JSON's four whitespace characters are accepted, not Python splitlines semantics.
    # 函数用途: 跳过 JSON 空白；尾部仍要求真正 EOF。
    def whitespace(self):
        while True:
            count = len(self.buffer) - len(self.buffer.lstrip(' \t\r\n'))
            self._consume(count)
            if self.buffer or self.eof:
                return
            self._fill()

    # LLM: EOF is distinct from absent legacy data and never grants new send authority.
    # 函数用途: 读取下一个语法字符但不消费。
    def peek(self):
        self.whitespace()
        return self.buffer[:1]

    # LLM: Grammar delimiters are checked explicitly, including rejection of trailing commas.
    # 函数用途: 消费一个准确的 JSON 分隔符。
    def expect(self, expected):
        if self.peek() != expected:
            raise ValueError('invalid legacy alert JSON delimiter')
        self._consume(1)

    # LLM: Decode one bounded value/key; colon is a valid key boundary, subsequent grammar checks it.
    # 函数用途: 读取单个键或值，记录与派发映射逐项处理。
    def value(self):
        self.whitespace()
        while True:
            try:
                value, end = self.json.raw_decode(self.buffer)
            except json.JSONDecodeError:
                if self.eof:
                    raise ValueError('invalid or truncated legacy alert JSON') from None
                self._fill()
                continue
            if end == len(self.buffer) and not self.eof:
                self._fill()
                continue
            if end < len(self.buffer) and self.buffer[end] not in ' \t\r\n,:}]':
                if self.eof:
                    raise ValueError('invalid legacy alert JSON value')
                self._fill()
                continue
            self._consume(end)
            return value


# LLM: Staged points are not externally readable until ready; replay preserves merged correlation.
# 函数用途: 将一个旧映射项合入同键原子记录，不持有累计内存表。
def _stage_entry(root, state, section, key, value):
    anchors._validate_key(key)
    data = anchors._read_key(root, key, state, staging=True) or anchors._record(key, state)
    if state.get('validating'):
        if key not in data['anchors']:
            raise ValueError('legacy dispatch has no anchor proof')
        expected = data['anchors'][key] if section == 'anchors' else data['root_delivery_keys'].get(key, '')
        if value != expected:
            raise ValueError('legacy alert import does not match source')
        return
    if section == 'anchors':
        anchors._validate_row(value)
        data['anchors'][key] = dict(value)
    else:
        if not isinstance(value, str):
            raise ValueError('invalid legacy original dispatch')
        if 'anchors' in state['seen'] and key not in data['anchors']:
            raise ValueError('legacy dispatch has no anchor proof')
        data['root_delivery_keys'][key] = value
    anchors._write(root, data)


# LLM: Root fields are finite parser metadata; duplicate authority maps are rejected, not merged.
# 函数用途: 处理一个顶层字段，不读取整个锚点映射。
def _top_field(reader, state):
    name = reader.value()
    if not isinstance(name, str):
        raise ValueError('invalid legacy alert field')
    reader.expect(':')
    tracked = name in {'anchors', 'root_delivery_keys', 'schema_version'}
    if tracked and name in state['seen']:
        raise ValueError('duplicate legacy alert authority field')
    if name in {'anchors', 'root_delivery_keys'}:
        reader.expect('{')
        phase, bucket = 'bucket_start', name
    else:
        value = reader.value()
        if name == 'schema_version' and value != 'alert_topic_anchors.v1':
            raise ValueError('invalid legacy alert schema')
        phase, bucket = 'top_separator', ''
    if tracked:
        state['seen'].append(name)
    state.update(phase=phase, bucket=bucket)


# LLM: Commit parser state only at complete grammar/point boundaries; a cut token never advances it.
# 函数用途: 执行一次有记录数和字节双上限的流式迁移。
def _parse_slice(root, descriptor, state, max_records, max_bytes):
    reader = _Reader(descriptor, state['cursor'], max_bytes)
    records, initial = 0, state['cursor']
    try:
        while records < max_records:
            reader.whitespace()
            state['cursor'] = reader.offset
            phase = state['phase']
            if phase == 'root':
                reader.expect('{')
                state['phase'] = 'top_start'
            elif phase in {'top_start', 'top_item'}:
                if phase == 'top_start' and reader.peek() == '}':
                    reader.expect('}')
                    state['phase'] = 'tail'
                else:
                    _top_field(reader, state)
            elif phase in {'bucket_start', 'bucket_item'}:
                if phase == 'bucket_start' and reader.peek() == '}':
                    reader.expect('}')
                    state['phase'] = 'top_separator'
                else:
                    key = reader.value()
                    reader.expect(':')
                    value = reader.value()
                    _stage_entry(root, state, state['bucket'], key, value)
                    records += 1
                    state['phase'] = 'bucket_separator'
            elif phase in {'top_separator', 'bucket_separator'}:
                token = reader.peek()
                if token not in {',', '}'}:
                    raise ValueError('invalid legacy alert JSON separator')
                reader.expect(token)
                state['phase'] = ('top_item' if token == ',' else 'tail') if phase == 'top_separator' else (
                    'bucket_item' if token == ',' else 'top_separator')
            elif phase == 'tail':
                if reader.peek() or not {'schema_version', 'anchors'} <= set(state['seen']):
                    raise ValueError('invalid legacy alert JSON tail or schema')
                if (not state.get('validating') and 'root_delivery_keys' in state['seen']
                        and state['seen'].index('root_delivery_keys') < state['seen'].index('anchors')):
                    state.update(phase='root', cursor=0, seen=[], bucket='', validating=True)
                    break
                state['phase'] = 'publish'
                state['cursor'] = reader.offset
                break
            else:
                raise ValueError('invalid alert migration parser phase')
            state['cursor'] = reader.offset
    except _SliceEnd:
        if state['cursor'] == initial:
            raise ValueError('legacy alert value exceeds slice byte bound') from None
    return records, reader.bytes_read


# LLM: Ready is published only after every point, source validation and old-writer format fence.
# 函数用途: 原子发布固定布局，绝不携带独立的发送状态。
def _publish_ready(root, state):
    layout = {'schema_version': anchors._READY_SCHEMA,
        'primary_key': state['primary_key'], 'migration_id': state['migration_id']}
    write_text_atomic_beneath(root, anchors._READY_PARTS, anchors._encode(layout, limit=4096))


# LLM: A completed import checkpoint can recover after format replacement but before ready.
# 函数用途: 完成旧格式栅栏和就绪发布，避免切换窗口被误判成空 Owner。
def _finish_publish(root, state):
    if state['kind'] == 'legacy':
        descriptor = _open_source(root)
        try:
            if _seal(descriptor) == state['source']:
                _verify_source(root, descriptor, state)
                anchors._write(root, anchors._record('', state))
            else:
                # Only this exact, bounded, already-committed cutover fence is resumable.
                if os.fstat(descriptor).st_size > 4096:
                    raise ValueError('legacy alert source changed before cutover')
                fence = anchors._read(root)
                if fence != anchors._record('', state):
                    raise ValueError('legacy alert source changed before cutover')
        finally:
            os.close(descriptor)
    else:
        fence = anchors._read(root)
        anchors._validate_record(fence, state['primary_key'], state, staging=True)
    _publish_ready(root, state)
    state['phase'] = 'complete'
    _save_progress(root, state)


# LLM: Persist source binding before importing any row; missing source with a checkpoint is not empty.
# 函数用途: 为全新 Owner 或明确存在的旧账本创建恢复检查点。
def _begin(root, primary_key):
    state = {'schema_version': _PROGRESS_SCHEMA, 'kind': 'new', 'phase': 'initializing',
        'cursor': 0, 'seen': [], 'bucket': '', 'primary_key': primary_key,
        'migration_id': uuid.uuid4().hex}
    try:
        descriptor = _open_source(root)
    except FileNotFoundError:
        descriptor = None
    if descriptor is not None:
        try:
            state.update(kind='legacy', phase='root', primary_key='', source=_seal(descriptor))
            changed = file_descriptor_change_ns(descriptor, os.fstat(descriptor))
            if changed is None:
                raise NoFollowPathError('legacy alert source generation is unavailable')
            state['source_change_ns'] = changed
        finally:
            os.close(descriptor)
    _save_progress(root, state)
    return state


# LLM: This API is one bounded maintenance step, not a background worker or an unbounded retry loop.
# 函数用途: 按预算恢复旧账本，完成前所有正常入口都明确拒绝把历史当作不存在。
def migrate_legacy(owner_home, *, max_records=256, max_bytes=_MigrationLimits.MAX_MIGRATION_BYTES, primary_key=''):
    if type(max_records) is not int or not 1 <= max_records <= _MigrationLimits.MAX_MIGRATION_RECORDS:
        raise ValueError('migration record budget is out of bound')
    if type(max_bytes) is not int or not 1 <= max_bytes <= _MigrationLimits.MAX_MIGRATION_BYTES:
        raise ValueError('migration byte budget is out of bound')
    if primary_key:
        anchors._validate_key(primary_key)
    root = anchors._storage_root(owner_home)
    if anchors._read_ready(root) is not None:
        return {'complete': True, 'records': 0, 'bytes_read': 0}
    with locked_json_path(root.joinpath(*anchors._ROOT_PARTS)):
        if anchors._read_ready(root) is not None:
            return {'complete': True, 'records': 0, 'bytes_read': 0}
        state = _load_progress(root) or _begin(root, primary_key)
        records, bytes_read = 0, 0
        if state['kind'] == 'new' and state['phase'] == 'initializing':
            anchors._write(root, anchors._record(state['primary_key'], state))
            state['phase'] = 'publish'
            _save_progress(root, state)
        if state['phase'] not in {'publish', 'complete'}:
            descriptor = _open_source(root)
            try:
                _verify_source(root, descriptor, state)
                records, bytes_read = _parse_slice(root, descriptor, state, max_records, max_bytes)
                _verify_source(root, descriptor, state)
                _save_progress(root, state)
            finally:
                os.close(descriptor)
        if state['phase'] in {'publish', 'complete'}:
            _finish_publish(root, state)
        return {'complete': state['phase'] == 'complete', 'records': records,
            'bytes_read': bytes_read, 'cursor': state['cursor']}
