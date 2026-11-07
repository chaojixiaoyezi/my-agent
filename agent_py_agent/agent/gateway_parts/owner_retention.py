# LLM: 空闲实例回收不写终态；旧回执只在原T核结算后复制到不可变段，默认维护保留原件，不替代业务权威。
# 模块用途: 回收空闲IM实例，并按预算安全封存同一Gateway的已结算旧回执。
from __future__ import annotations

import base64
import gc
import hashlib
import json
import math
import os
import sqlite3
import stat
import sys
import tempfile
import time
from collections import OrderedDict
from pathlib import Path

from ..backends.bounded_call import BoundedCallExitUnconfirmedError
from ..common.json_io import locked_json_path
from ..common.nofollow_fs import (
    open_directory_beneath,
    read_bytes_beneath,
    write_bytes_atomic_beneath,
)
from ..common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
)
from ..common.tool_write_context import ToolWriteContextUnavailable
from ..contracts.optimization_contracts import require_schema_generation
from ..runtime_errors import DataCorruptionError

# 终态回执归档单个源文件允许读取的最大字节数。
_RECEIPT_MAX_FILE_BYTES = 1_048_576
# 终态回执单个不可变归档段允许累积的最大字节数。
_RECEIPT_MAX_SEGMENT_BYTES = 8 * 1_048_576
_RECEIPT_SCANS: OrderedDict = OrderedDict()


# 上下文由有界缓存拥有，完成、错误与逐出时关闭生成器，跨片不丢位置也不无限积累FD。
def _receipt_scan_entries(directory):
    with os.scandir(directory) as entries:
        yield from entries


# LLM: 按候选版本复核在途及磁盘硬事实，异常保留实例；登记与池均重检时刻，不能擦除并发新消息。
# 函数用途: 回收一批已空闲的用户缓存，保留会话、身份、记忆、工具进程和任务文件。
def release_idle_owner_agents(supervisor) -> int:
    from ..owner_wake_discovery import _owner_has_hard_facts

    pool = getattr(supervisor, "_owner_pool", None)
    config = getattr(supervisor._base_agent, "config", None)
    seconds = float(getattr(config, "owner_agent_idle_seconds", 60.0))
    if pool is None or seconds <= 0:
        return 0
    released = 0
    for key, agent, touched in pool.idle_candidates(seconds):
        observed_at = time.monotonic()
        if supervisor._active_threads_for_owner(id(agent)):
            continue
        future = supervisor._curator_inflight.get(id(agent))
        if future is not None and not future.done():
            continue
        home = getattr(getattr(agent, "home_paths", None), "owner_home_dir", None)
        if not home:
            continue
        try:
            if _owner_has_hard_facts(Path(home)):
                continue
        except Exception:  # noqa: BLE001 无法确认事实时保留，不得误当空闲
            continue
        if (supervisor._registry.discard_idle(key, observed_at)
                and pool.evict_idle(key, agent, touched)):
            supervisor._owner_schedulers.pop(id(agent), None)
            released += 1
    if released:
        _trim_heap_after_release(supervisor, seconds)
    return released


# LLM: 只在本轮确实逐出空闲实例后执行，同一 supervisor 每个 owner_agent_idle_seconds 周期最多一次；
#   非 Linux/glibc（无 malloc_trim）或调用失败时静默跳过，回收结果与返回值不变，不读写任何业务状态。
# 函数用途: 先回收被逐出实例的循环引用，再让 glibc 把各 arena 中已释放的整页还给系统（实测 10 个长任务后约 20 MB）。
def _trim_heap_after_release(supervisor, seconds: float) -> None:
    now = time.monotonic()
    if now < float(getattr(supervisor, "_next_heap_trim_at", 0.0) or 0.0):
        return
    supervisor._next_heap_trim_at = now + seconds
    gc.collect()
    trim = _glibc_malloc_trim()
    if trim is not None:
        trim(0)


# 函数用途: 返回 glibc 的 malloc_trim；非 Linux、musl（无此符号）或动态库加载失败时返回 None。
def _glibc_malloc_trim():
    if not sys.platform.startswith("linux"):
        return None
    try:
        import ctypes

        return getattr(ctypes.CDLL(None), "malloc_trim", None)
    except OSError:
        return None


# LLM: 调用方已持维护锁；仅投影manifest连续序号覆盖的不可变段，完整提交后原子发布，失败不改原段或manifest。
# 函数用途: 从已提交分段重建私有回执定位器，不收录未提交孤儿段。
def _rebuild_receipt_index(paths, index):
    raw = read_bytes_beneath(paths.root, ('archive', 'receipts-manifest.json'),
                             max_bytes=_RECEIPT_MAX_FILE_BYTES, require_dir_fd=True)
    manifest = json.loads(raw) if raw is not None else {}
    require_schema_generation(manifest)
    sequence, count = manifest.get('next_seq'), manifest.get('segment_count')
    if type(sequence) is not int or type(count) is not int or count < 0 or sequence != count + 1:
        raise DataCorruptionError('gateway_archive_sequence_invalid')
    descriptor, temporary = tempfile.mkstemp(prefix=f'.{index.name}.rebuild-', dir=index.parent)
    os.close(descriptor)
    try:
        connection = sqlite3.connect(temporary, timeout=0.05)
        try:
            with connection:
                connection.execute('CREATE TABLE receipts(path TEXT PRIMARY KEY, segment TEXT NOT NULL, offset INTEGER NOT NULL, length INTEGER NOT NULL, digest TEXT NOT NULL)')
                for number in range(1, sequence):
                    segment = f'receipts-seg-{number:012d}.jsonl'
                    encoded = read_bytes_beneath(paths.root, ('archive', segment),
                                                 max_bytes=_RECEIPT_MAX_SEGMENT_BYTES, require_dir_fd=True)
                    if not encoded or not encoded.endswith(b'\n'):
                        raise DataCorruptionError('gateway_archive_segment_missing_or_invalid')
                    offset, records = 0, 0
                    for line in encoded.splitlines(keepends=True):
                        record = json.loads(line)
                        require_schema_generation(record)
                        body = base64.b64decode(record['body'], validate=True)
                        relative, digest = record['path'], record['digest']
                        if (type(relative) is not str or not Path(relative).parts or Path(relative).is_absolute()
                                or str(Path(relative)) != relative or '..' in Path(relative).parts
                                or len(body) > _RECEIPT_MAX_FILE_BYTES or len(line) > 2 * _RECEIPT_MAX_FILE_BYTES
                                or hashlib.sha256(body).hexdigest() != digest):
                            raise DataCorruptionError('gateway_archive_record_hash_mismatch')
                        connection.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)',
                                           (relative, segment, offset, len(line), digest))
                        offset += len(line)
                        records += 1
                    if number == sequence - 1:
                        expected = {'name': segment, 'bytes': len(encoded), 'records': records,
                                    'sha256': hashlib.sha256(encoded).hexdigest()}
                        if any(manifest['last_segment'].get(key) != value for key, value in expected.items()):
                            raise DataCorruptionError('gateway_archive_segment_manifest_mismatch')
        finally:
            connection.close()
        if index.is_symlink() or index.parent.is_symlink():
            raise DataCorruptionError('gateway_archive_index_symlink')
        os.replace(temporary, index)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise DataCorruptionError('gateway_archive_segment_invalid') from exc
    finally:
        Path(temporary).unlink(missing_ok=True)


# LLM: 正常读不取维护锁；缺库/缺schema时非阻塞竞选，锁内重核，写者必须已持同一维护锁。
# 函数用途: 打开同一Gateway归档索引，必要时从已提交段一次性恢复定位投影。
def _receipt_index(paths, *, writable=False):
    index = paths.root / 'archive' / 'receipts-index.sqlite3'
    if index.is_symlink() or index.parent.is_symlink():
        raise DataCorruptionError('gateway_archive_index_symlink')
    if index.exists():
        connection = sqlite3.connect(index.absolute().as_uri() + ('?mode=rw' if writable else '?mode=ro'),
                                     uri=True, timeout=0.05)
        try:
            columns = {row[1] for row in connection.execute('PRAGMA table_info(receipts)')}
        except BaseException:
            connection.close()
            raise
        if {'path', 'segment', 'offset', 'length', 'digest'} <= columns:
            return connection
        connection.close()
    if (index.parent / 'receipts-manifest.json').exists():
        if writable:
            _rebuild_receipt_index(paths, index)
        else:
            with locked_json_path(index.parent / 'receipts-maintenance', blocking=False):
                _receipt_index(paths, writable=True).close()
        return sqlite3.connect(index.absolute().as_uri() + ('?mode=rw' if writable else '?mode=ro'),
                               uri=True, timeout=0.05)
    if not index.exists():
        if not writable:
            raise DataCorruptionError('gateway_archive_index_missing')
        descriptor = os.open(index, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(descriptor)
    connection = sqlite3.connect(str(index) if writable else index.absolute().as_uri() + '?mode=ro',
                                 uri=not writable, timeout=0.05)
    if writable:
        connection.execute('CREATE TABLE IF NOT EXISTS receipts(path TEXT PRIMARY KEY, segment TEXT NOT NULL, offset INTEGER NOT NULL, length INTEGER NOT NULL, digest TEXT NOT NULL)')
    return connection


# LLM: 查段前校验规范相对路径和有界长度，FD逐层nofollow；记录path及正文hash必须与索引一致。
# 函数用途: 从已提交分段中准确读取原文件字节，不全段扫描或创建热文件。
def lookup_sealed_receipt(paths, relative: str) -> bytes | None:
    parts = Path(relative).parts
    if not parts or any(part in ('..', '.') for part in parts) or Path(relative).is_absolute():
        raise ValueError('invalid_gateway_receipt_path')
    manifest = paths.root / 'archive' / 'receipts-manifest.json'
    if not manifest.exists():
        return None
    connection = _receipt_index(paths)
    try:
        row = connection.execute('SELECT segment,offset,length,digest FROM receipts WHERE path=?', (relative,)).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    segment, offset, length, digest = row
    if (Path(segment).name != segment or not segment.startswith('receipts-seg-')
            or type(offset) is not int or offset < 0 or type(length) is not int
            or not 0 < length <= 2 * _RECEIPT_MAX_FILE_BYTES):
        raise DataCorruptionError('gateway_archive_index_invalid')
    directory = open_directory_beneath(paths.root, ('archive',), require_dir_fd=True)
    descriptor = -1
    try:
        descriptor = os.open(segment, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise DataCorruptionError('gateway_archive_segment_not_regular')
        os.lseek(descriptor, offset, os.SEEK_SET)
        encoded = bytearray()
        while len(encoded) < length:
            block = os.read(descriptor, length - len(encoded))
            if not block:
                break
            encoded.extend(block)
        record = json.loads(encoded)
        require_schema_generation(record)
        body = base64.b64decode(record['body'], validate=True)
        if (record['path'] != relative or record['digest'] != digest
                or hashlib.sha256(body).hexdigest() != digest):
            raise DataCorruptionError('gateway_archive_record_hash_mismatch')
        return body
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory)


# LLM: 原terminal schema、指纹、投影stamp与settlement事实共同准入；未知/在途/年轻对象一律保留。
# 函数用途: 在原请求T内捕获一组可封存文件的准确前像。
def _receipt_bundle(paths, canonical, before):
    from .io import read_json_file_report, validated_gateway_request_fingerprint
    from .recovery import _terminal_projection_marker_matches, _terminal_projection_marker_path

    key = canonical.stem
    if (paths.inbox / canonical.name).exists() or (paths.processing / canonical.name).exists():
        return None
    report = read_json_file_report(canonical)
    row = report.payload
    ended = row.get('completed_at')
    if (report.load_error or row.get('id') != key or row.get('schema_version') != 'gateway_terminal_request.v1'
            or row.get('turn_phase') != 'closed' or row.get('status') not in ('done', 'completed', 'failed', 'cancelled')
            or type(ended) not in (int, float) or not math.isfinite(ended) or ended >= before
            or any(row.get(name) for name in ('execution_unknown', 'replay_pending', 'outbox_pending', 'delivery_pending', 'unconsumed'))
            or not _terminal_projection_marker_matches(paths, canonical)):
        return None
    validated_gateway_request_fingerprint(row, key)
    candidates = [canonical, paths.done / canonical.name, paths.failed / canonical.name,
                  paths.responses / canonical.name, _terminal_projection_marker_path(paths, key)]
    candidates.extend(folder / f'{key}.chunks.jsonl' for folder in (paths.done, paths.failed, paths.responses))
    bundle = []
    for path in candidates:
        relative = path.relative_to(paths.root)
        raw = read_bytes_beneath(paths.root, relative.parts, max_bytes=_RECEIPT_MAX_FILE_BYTES, require_dir_fd=True)
        if raw is not None:
            bundle.append((str(relative), raw))
    return key, bundle


# LLM: 段先fsync发布，再manifest，最后一次SQLite事务发布全部定位；任何失败之前不删原件。
# 函数用途: 将本拍多请求准确前像合并为一份不可变私有段。
def _publish_receipt_segment(paths, bundles):
    manifest_path = paths.root / 'archive' / 'receipts-manifest.json'
    raw = read_bytes_beneath(paths.root, ('archive', manifest_path.name), max_bytes=_RECEIPT_MAX_FILE_BYTES, require_dir_fd=True)
    manifest = json.loads(raw) if raw is not None else {'schema_generation': 1, 'next_seq': 1, 'segment_count': 0}
    require_schema_generation(manifest)
    sequence = manifest['next_seq']
    if type(sequence) is not int or sequence < 1:
        raise DataCorruptionError('gateway_archive_sequence_invalid')
    segment = f'receipts-seg-{sequence:012d}.jsonl'
    existing = read_bytes_beneath(paths.root, ('archive', segment), max_bytes=_RECEIPT_MAX_SEGMENT_BYTES, require_dir_fd=True)
    encoded, index_rows = bytearray(), []
    for key, bundle in bundles:
        for relative, body in bundle:
            digest = hashlib.sha256(body).hexdigest()
            record = {'schema_generation': 1, 'path': relative, 'request_id': key, 'digest': digest,
                      'body': base64.b64encode(body).decode('ascii')}
            line = (json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
            index_rows.append((relative, segment, len(encoded), len(line), digest))
            encoded.extend(line)
    if len(encoded) > _RECEIPT_MAX_SEGMENT_BYTES:
        raise ValueError('gateway_receipt_segment_budget_exceeded')
    if existing is not None and existing != bytes(encoded):
        raise DataCorruptionError('gateway_archive_unindexed_segment_conflict')
    if existing is None:
        write_bytes_atomic_beneath(paths.root, ('archive', segment), bytes(encoded))
    manifest['segment_count'] = manifest.get('segment_count', 0) + 1
    manifest['last_segment'] = {'name': segment, 'bytes': len(encoded), 'records': len(index_rows),
                               'sha256': hashlib.sha256(encoded).hexdigest()}
    manifest['next_seq'] = sequence + 1
    write_bytes_atomic_beneath(paths.root, ('archive', manifest_path.name), json.dumps(manifest, sort_keys=True).encode())
    connection = _receipt_index(paths, writable=True)
    try:
        with connection:
            connection.executemany('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)', index_rows)
    finally:
        connection.close()


# LLM: 删除前重核所有原件字节且逐层FD不跟链接；索引已发布但原件变化时保留整组，不覆盖新数据。
# 函数用途: 安全移除已确认落入段的松散文件，并沿原试锁规则清理已老化孤儿锁。
def _remove_sealed_bundle(paths, bundle):
    from .io import _cleanup_orphan_lock_at

    for relative, before in bundle:
        parts = Path(relative).parts
        current = read_bytes_beneath(paths.root, parts, max_bytes=_RECEIPT_MAX_FILE_BYTES, require_dir_fd=True)
        if current != before or lookup_sealed_receipt(paths, relative) != before:
            return False
    for relative, before in bundle:
        parts = Path(relative).parts
        directory = open_directory_beneath(paths.root, parts[:-1], require_dir_fd=True)
        try:
            os.unlink(parts[-1], dir_fd=directory)
            _cleanup_orphan_lock_at(directory, parts[-1] + '.lock', current=time.time())
        finally:
            os.close(directory)
    return True


# LLM: 每根目录只保留有界scandir句柄；重启从头枚举但已删条目无需重放，绝不持久化不稳定offset作权威。
#   生产接线传remove_loose=False，读口切换未闭合前不能自动删原件。
#   已提交manifest的定位器先在同一维护锁下恢复；无新候选也能自愈，缺分段继续失败关闭。
# 函数用途: 在总候选/时间/字节预算内封存一片原终态回执。
def seal_gateway_receipts_slice(paths, *, before: float, max_entries: int = 128, max_ms: float = 50,
                                max_bytes: int = 4 * 1_048_576, remove_loose: bool = True) -> dict:
    from .multiprocess_processing_recovery import processing_metadata_scope

    if (type(max_entries) is not int or max_entries < 1 or type(max_bytes) is not int or max_bytes < 1
            or type(max_ms) not in (int, float) or not math.isfinite(max_ms) or max_ms <= 0
            or type(before) not in (int, float) or not math.isfinite(before)):
        raise ValueError('invalid_gateway_receipt_budget')
    result = {'sealed_requests': 0, 'archived_requests': 0, 'scanned_entries': 0, 'bytes': 0, 'errors': [], 'complete': False}
    if not paths.terminal.is_dir() and not (paths.root / 'archive' / 'receipts-manifest.json').exists():
        return {**result, 'complete': True}
    deadline = time.monotonic() + max_ms / 1000
    anchor = paths.root / 'archive' / 'receipts-maintenance'
    with locked_json_path(anchor):
        if (anchor.parent / 'receipts-manifest.json').exists():
            _receipt_index(paths, writable=True).close()
        if not paths.terminal.is_dir():
            return {**result, 'complete': True}
        identity = str(paths.root.absolute())
        iterator = _RECEIPT_SCANS.get(identity)
        if iterator is None:
            iterator = _receipt_scan_entries(paths.terminal)
            _RECEIPT_SCANS[identity] = iterator
        _RECEIPT_SCANS.move_to_end(identity)
        while len(_RECEIPT_SCANS) > 32:
            _key, old = _RECEIPT_SCANS.popitem(last=False)
            old.close()
        bundles, size = [], 0
        try:
            while result['scanned_entries'] < max_entries and time.monotonic() < deadline:
                try:
                    entry = next(iterator)
                except StopIteration:
                    iterator.close()
                    _RECEIPT_SCANS.pop(identity, None)
                    result['complete'] = True
                    break
                result['scanned_entries'] += 1
                if not entry.name.endswith('.json') or not entry.is_file(follow_symlinks=False):
                    continue
                with processing_metadata_scope(paths, Path(entry.name).stem):
                    bundle = _receipt_bundle(paths, Path(entry.path), before)
                if bundle is None:
                    continue
                if not remove_loose and all(lookup_sealed_receipt(paths, relative) == raw for relative, raw in bundle[1]):
                    continue
                weight = sum(len(raw) for relative, raw in bundle[1])
                if size + weight > min(max_bytes, _RECEIPT_MAX_SEGMENT_BYTES // 2):
                    result['complete'] = False
                    break
                bundles.append(bundle)
                size += weight
            if bundles:
                _publish_receipt_segment(paths, bundles)
                result['archived_requests'] = len(bundles)
                for key, bundle in bundles:
                    with processing_metadata_scope(paths, key):
                        # 发布是门外长IO；重新取得原T后必须重核在途/结算等保护事实。
                        current = _receipt_bundle(paths, paths.terminal / f'{key}.json', before)
                        if remove_loose and current == (key, bundle) and _remove_sealed_bundle(paths, bundle):
                            result['sealed_requests'] += 1
                result['bytes'] = size
        except (RequestExecutionOutcomeUncertainError, RequestExecutionContextUnavailable,
                ToolWriteContextUnavailable, BoundedCallExitUnconfirmedError):
            iterator.close()
            _RECEIPT_SCANS.pop(identity, None)
            raise
        except Exception as exc:
            iterator.close()
            _RECEIPT_SCANS.pop(identity, None)
            result['errors'].append({'error_type': type(exc).__name__, 'side_effects_unknown': True})
            result['complete'] = False
    return result
