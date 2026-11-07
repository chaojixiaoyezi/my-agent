"""旧输入仅冷备份保热件，空客户端outbox可移走；仅写archive，不移动控制操作或锁。"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import secrets
import sqlite3
import stat
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import (
    open_directory_beneath,
    read_bytes_beneath,
    write_bytes_atomic_beneath,
)
from ..runtime_errors import DataCorruptionError
from .io import locked_file_transition, try_gateway_turn_transition

logger = logging.getLogger(__name__)


class _RuntimeScans:
    max_directories = 32
    max_file_bytes = 1048576
    lock = threading.RLock()
    entries: OrderedDict = OrderedDict()


# 生成器显式拥有上下文；跨slice只保留有界数量，完成、异常和LRU逐出均close，不积累FD。
def _directory_entries(directory):
    with os.scandir(directory) as entries:
        yield from entries


# 显式pending槽避免未启动的回放生成器close不执行finally、漏关原目录句柄。
class _RuntimeIterator:
    def __init__(self, directory):
        self.iterator = _directory_entries(directory)
        self.pending = None

    def __next__(self):
        if self.pending is not None:
            entry, self.pending = self.pending, None
            return entry
        return next(self.iterator)

    def close(self):
        self.iterator.close()


def _close_scan(key):
    iterator = _RuntimeScans.entries.pop(key, None)
    if iterator is not None:
        iterator.close()


# LLM: 冷目录必须是真目录；链接不能参与读侧降级或写侧重建，缺失与非目录由调用方按存在性处理。
# 函数用途: 返回归档冷目录，缺失或非目录返回None，符号链接按损坏拒绝。
def _cold_directory(paths, directory):
    folder = paths.root / 'archive' / directory
    try:
        mode = folder.lstat().st_mode
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(mode):
        raise DataCorruptionError('runtime_archive_symlink')
    return folder if stat.S_ISDIR(mode) else None


# LLM: 读侧返回和写侧发布前重核资格检查时绑定的目录；消失、替换或父链链接均拒绝。
# 函数用途: 验证冷目录仍是最初观察到的同一真实目录。
def _verify_cold_bindings(paths, bindings):
    for folder, before in bindings:
        if _cold_directory(paths, folder.name) is None:
            raise DataCorruptionError('runtime_archive_directory_changed')
        descriptor = open_directory_beneath(paths.root, ('archive', folder.name), require_dir_fd=True)
        try:
            if not os.path.samestat(before, os.fstat(descriptor)):
                raise DataCorruptionError('runtime_archive_directory_changed')
        finally:
            os.close(descriptor)


# LLM: 降级只经no-follow目录FD枚举月份并携带inode身份；月份打开时须核此身份，不能换成另一个真空目录。
# 函数用途: 按最新优先列出YYYYMM月份及其最初元数据。
def _cold_months(paths, directory):
    folder = _cold_directory(paths, directory)
    if folder is None:
        return []
    months = []
    descriptor = open_directory_beneath(paths.root, ('archive', directory), require_dir_fd=True)
    try:
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if len(entry.name) == 6 and entry.name.isascii() and entry.name.isdigit():
                    if entry.is_symlink():
                        raise DataCorruptionError('runtime_archive_symlink')
                    if entry.is_dir(follow_symlinks=False):
                        months.append((entry.name, entry.stat(follow_symlinks=False)))
        _cold_directory(paths, directory)
        if not os.path.samestat(os.fstat(descriptor), folder.lstat()):
            raise DataCorruptionError('runtime_archive_directory_changed')
    finally:
        os.close(descriptor)
    return sorted(months, reverse=True)


# LLM: 降级读取绑定枚举时的月份身份；只读指定名字且不枚举月份内文件，目录替换不能变成不存在。
# 函数用途: 从最新月份开始定位指定运行时文件的冷副本。
def _cold_copy(paths, directory, name):
    for month, before in _cold_months(paths, directory):
        with _cold_month_guard(paths, directory, month, before):
            raw = read_bytes_beneath(
                paths.root, ('archive', directory, month, name),
                max_bytes=_RuntimeScans.max_file_bytes, require_dir_fd=True,
            )
        if raw is not None:
            return paths.root / 'archive' / directory / month / name
    return None


# LLM: 月份首次打开须等于枚举时身份，退出前完整no-follow重开再核；本函数不扫描文件且必关FD。
# 函数用途: 为降级读取和维护重建绑定同一月份目录，并拒绝前后替换。
@contextmanager
def _cold_month_guard(paths, directory, month, before):
    parts = ('archive', directory, month)
    descriptor = open_directory_beneath(paths.root, parts, require_dir_fd=True)
    try:
        if not os.path.samestat(before, os.fstat(descriptor)):
            raise DataCorruptionError('runtime_archive_directory_changed')
        yield descriptor
        current = open_directory_beneath(paths.root, parts, require_dir_fd=True)
        try:
            if not os.path.samestat(os.fstat(descriptor), os.fstat(current)):
                raise DataCorruptionError('runtime_archive_directory_changed')
        finally:
            os.close(current)
    finally:
        os.close(descriptor)


# LLM: 维护重建仅投影原写者允许的非隐藏JSON名；锁与临时旁件不入库，重复键不被旧月份覆盖。
# 函数用途: 把两类运行时冷副本投影进一个私有SQLite并返回唯一行数。
def _populate_rebuilt_index(paths, connection):
    connection.execute(
        'CREATE TABLE IF NOT EXISTS files(directory TEXT, name TEXT, month TEXT, digest TEXT, '
        'PRIMARY KEY(directory,name))',
    )
    rows = 0
    with connection:
        for directory in ('input_receipts', 'client_outbox'):
            for month, before in _cold_months(paths, directory):
                with _cold_month_guard(paths, directory, month, before) as descriptor, os.scandir(descriptor) as entries:
                    for entry in entries:
                        if entry.name.startswith('.') or not entry.name.endswith('.json'):
                            continue
                        if entry.is_symlink():
                            raise DataCorruptionError('runtime_archive_symlink')
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        raw = read_bytes_beneath(
                            paths.root, ('archive', directory, month, entry.name),
                            max_bytes=_RuntimeScans.max_file_bytes, require_dir_fd=True,
                        )
                        if raw is None:
                            raise DataCorruptionError('runtime_archive_bytes_missing_or_changed')
                        cursor = connection.execute(
                            'INSERT OR IGNORE INTO files VALUES(?,?,?,?)',
                            (directory, entry.name, month, hashlib.sha256(raw).hexdigest()),
                        )
                        rows += cursor.rowcount
    return rows


# LLM: 私有投影发布前重核最初冷目录，硬链接发布且只以rw重开；丢失或危险赢家不能变成空库或成功日志。
# 函数用途: 一次性重建丢失的运行时归档定位器并返回可写连接。
def _rebuild_index(paths, path, bindings):
    temporary = path.with_name(f'.{path.name}.rebuild-{secrets.token_hex(8)}')
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600,
    )
    os.close(fd)
    try:
        connection = sqlite3.connect(temporary, timeout=0.01)
        try:
            rows = _populate_rebuilt_index(paths, connection)
        finally:
            connection.close()
        _verify_cold_bindings(paths, bindings)
        published = False
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        else:
            published = True
    finally:
        temporary.unlink(missing_ok=True)
    if path.is_symlink():
        raise DataCorruptionError('runtime_archive_index_symlink')
    connection = sqlite3.connect(path.absolute().as_uri() + '?mode=rw', uri=True, timeout=0.01)
    if published:
        logger.warning('GATEWAY_ARCHIVE_INDEX_REBUILT root=%s rows=%s', paths.root, rows)
    return connection


# LLM: 索引正常时保持原SQLite权威；仅维护写者可从真实冷目录重建，marker-only仍失败关闭。
# 函数用途: 打开运行时归档索引，或为维护写者创建/重建它。
def _index(paths, *, writable=False):
    archive = paths.root / 'archive'
    if archive.is_symlink():
        raise DataCorruptionError('runtime_archive_symlink')
    path = archive / 'runtime-files.sqlite3'
    if path.is_symlink():
        raise DataCorruptionError('runtime_archive_index_symlink')
    if (archive / 'runtime-files-published.json').is_symlink():
        raise DataCorruptionError('runtime_archive_marker_symlink')
    try:
        path.lstat()
    except FileNotFoundError:
        has_publication = False
        for published in (
                archive / 'runtime-files-published.json', archive / 'input_receipts', archive / 'client_outbox'):
            try:
                published.lstat()
            except FileNotFoundError:
                continue
            has_publication = True
            break
        if has_publication:
            cold_directories = [_cold_directory(paths, directory)
                                for directory in ('input_receipts', 'client_outbox')]
            if not writable or not any(folder is not None for folder in cold_directories):
                raise DataCorruptionError('runtime_archive_index_missing') from None
            bindings = [(folder, folder.lstat()) for folder in cold_directories if folder is not None]
            return _rebuild_index(paths, path, bindings)
        if not writable:
            return None
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(fd)
    connection = sqlite3.connect(str(path) if writable else path.absolute().as_uri() + '?mode=ro',
                                 uri=not writable, timeout=0.01)
    if writable:
        connection.execute('CREATE TABLE IF NOT EXISTS files(directory TEXT, name TEXT, month TEXT, digest TEXT, PRIMARY KEY(directory,name))')
    return connection


# LLM: 降级查找须绑定资格检查时的冷目录身份；目录消失或替换不能把已消费输入变成首次请求。
# 函数用途: 通过索引或丢索引时的冷副本查找一个运行时归档文件。
def lookup_runtime_file(paths, directory: str, name: str) -> Path | None:
    if directory not in {'input_receipts', 'client_outbox'} or Path(name).name != name:
        raise ValueError('invalid_runtime_archive_key')
    try:
        connection = _index(paths)
    except DataCorruptionError as exc:
        if str(exc) != 'runtime_archive_index_missing':
            raise
        cold_directories = [_cold_directory(paths, candidate)
                            for candidate in ('input_receipts', 'client_outbox')]
        if not any(folder is not None for folder in cold_directories):
            raise
        bindings = [(folder, folder.lstat()) for folder in cold_directories if folder is not None]
        copy = _cold_copy(paths, directory, name)
        _verify_cold_bindings(paths, bindings)
        return copy
    if connection is None:
        return None
    try:
        row = connection.execute('SELECT month,digest FROM files WHERE directory=? AND name=?', (directory, name)).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    month, digest = row
    if type(month) is not str or len(month) != 6 or not month.isascii() or not month.isdigit():
        raise DataCorruptionError('runtime_archive_month_invalid')
    raw = read_bytes_beneath(paths.root, ('archive', directory, month, name),
                             max_bytes=_RuntimeScans.max_file_bytes, require_dir_fd=True)
    if raw is None or hashlib.sha256(raw).hexdigest() != digest:
        raise DataCorruptionError('runtime_archive_bytes_missing_or_changed')
    return paths.root / 'archive' / directory / month / name


# 输入保留原T；TUI保留原session transition，且始终再取原JSON锁。所有取锁均非阻塞。
@contextmanager
def _entry_guard(paths, directory, path):
    if directory == 'input_receipts':
        with try_gateway_turn_transition(paths, path.stem) as acquired:
            if not acquired:
                raise BlockingIOError('runtime_retention_turn_busy')
            yield
    else:
        with locked_file_transition(path.with_name(f'.{path.name}.transition'), blocking=False):
            yield


# 客户端outbox存储的是未确认集合，不是每行终态日志；只有规范空集合才是可移动终态。
def _terminal_timestamp(paths, directory, path, row):
    if directory == 'input_receipts':
        from .input_delivery_service import GatewayInputReceipt, _validate_prepared_request
        receipt = GatewayInputReceipt.from_dict(row)
        _validate_prepared_request(receipt)
        if receipt.request_id != path.stem or receipt.state != 'consumed' or receipt.reconcile_error:
            return None
        metadata = receipt.prepared_request.get('metadata', {})
        if isinstance(metadata, dict) and any(metadata.get(key) is not None
                                              for key in ('plugin_provenance', 'plugin_execution_policy')):
            # plugin_host.requests仍按key读取原路径且不属于W1；保留其原权威，不能把已消费当无引用。
            return None
        keys = {receipt.request_id, receipt.target_turn_id} - {''}
        if any((folder / f'{key}.json').exists() for folder in (paths.inbox, paths.processing) for key in keys):
            return None
    else:
        if row.get('schema_version') not in {'tui_active_input_outbox.v1', 'tui_active_input_outbox.v2',
                                             'tui_active_input_outbox.v3', 'tui_control_operation_outbox.v1'}:
            return None
        if row.get('entries') != {}:
            return None
    timestamp = row.get('updated_at')
    if type(timestamp) not in (int, float) or not math.isfinite(timestamp) or timestamp <= 0:
        return None
    return timestamp


# 先建立首次索引，再fsync发布标记/原字节并提交定位；失败保原件，旧索引丢失禁止重新建空库。
# input的control/multiprocess/plugin等读者尚未切换，只冷备份不删原件；outbox仍重核前像后移除。
def _archive_one(paths, directory, path, *, before, max_bytes, deadline):
    with _entry_guard(paths, directory, path), locked_json_path(path, blocking=False):
        raw = read_bytes_beneath(paths.root, (directory, path.name),
                                 max_bytes=_RuntimeScans.max_file_bytes, require_dir_fd=True)
        if raw is None:
            return 0, False
        row = json.loads(raw)
        if not isinstance(row, dict):
            raise DataCorruptionError('runtime_retention_invalid_object')
        timestamp = _terminal_timestamp(paths, directory, path, row)
        if timestamp is None or timestamp >= before:
            return 0, False
        if len(raw) > max_bytes or time.monotonic() >= deadline:
            return 0, True
        month = datetime.fromtimestamp(timestamp, timezone.utc).strftime('%Y%m')
        parts = ('archive', directory, month, path.name)
        existing = read_bytes_beneath(paths.root, parts, max_bytes=_RuntimeScans.max_file_bytes, require_dir_fd=True)
        if existing is not None and existing != raw:
            raise DataCorruptionError('runtime_archive_conflict')
        if (existing is not None and directory == 'input_receipts'
                and lookup_runtime_file(paths, directory, path.name) == paths.root.joinpath(*parts)):
            return 0, False
        connection = _index(paths, writable=True)
        try:
            marker_parts = ('archive', 'runtime-files-published.json')
            marker = b'{"schema_version":"gateway_runtime_files_published.v1"}\n'
            published = read_bytes_beneath(paths.root, marker_parts,
                max_bytes=_RuntimeScans.max_file_bytes, require_dir_fd=True)
            if published is None:
                write_bytes_atomic_beneath(paths.root, marker_parts, marker)
            elif published != marker:
                raise DataCorruptionError('runtime_archive_marker_invalid')
            if existing is None:
                write_bytes_atomic_beneath(paths.root, parts, raw)
            with connection:
                connection.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?)',
                    (directory, path.name, month, hashlib.sha256(raw).hexdigest()))
        finally:
            connection.close()
        if lookup_runtime_file(paths, directory, path.name) is None:
            raise DataCorruptionError('runtime_archive_publication_missing')
        if directory == 'input_receipts':
            return len(raw), False
        if read_bytes_beneath(paths.root, (directory, path.name),
                              max_bytes=_RuntimeScans.max_file_bytes, require_dir_fd=True) != raw:
            return 0, False
        parent = open_directory_beneath(paths.root, (directory,), require_dir_fd=True)
        try:
            os.unlink(path.name, dir_fd=parent)
        finally:
            os.close(parent)
        return len(raw), False


# LLM: 维护锁内先修复缺失冷索引，即使没有新终态；修复失败保留源文件并按原报告协议返回错误。
# 函数用途: 有界封存旧运行时文件，并在维护路径修复丢失的归档定位器。
def archive_terminal_slice(paths, *, directory: str, before: float, max_entries: int = 128,
                           max_ms: float = 50, retention_days: float = 7,
                           max_bytes: int = 4 * 1048576) -> dict:
    """封存一片旧终态，input保留原件；before不越过七天，跨slice继续原目录枚举。"""
    if directory not in {'input_receipts', 'client_outbox'}:
        raise ValueError('runtime_retention_directory_not_enabled')
    if (type(max_entries) is not int or max_entries < 1 or type(max_bytes) is not int or max_bytes < 1
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in (before, max_ms, retention_days))
            or max_ms <= 0 or retention_days < 0):
        raise ValueError('invalid_runtime_retention_budget')
    cutoff = min(before, time.time() - retention_days * 86400)
    result = {'archived_entries': 0, 'scanned_entries': 0, 'bytes': 0, 'errors': [], 'complete': False}
    if directory == 'input_receipts':
        result.update(deferred_reason='input_receipt_reader_cutover_required', reader_cutover_required=True)
    folder = paths.root / directory
    if not folder.exists():
        return {**result, 'complete': True}
    # 逐层nofollow验证规范根，不能经目录链接把维护扩展到其它空间。
    fd = open_directory_beneath(paths.root, (directory,), require_dir_fd=True)
    os.close(fd)
    deadline = time.monotonic() + max_ms / 1000
    key = str(folder.absolute())
    with _RuntimeScans.lock, locked_json_path(paths.root / 'archive' / f'{directory}-maintenance', blocking=False):
        try:
            if not (paths.root / 'archive/runtime-files.sqlite3').exists() and any(
                    _cold_directory(paths, candidate) is not None
                    for candidate in ('input_receipts', 'client_outbox')):
                _index(paths, writable=True).close()
        except (OSError, ValueError, DataCorruptionError, sqlite3.Error) as exc:
            _close_scan(key)
            result['errors'].append({'name': 'runtime-files.sqlite3', 'error_type': type(exc).__name__})
            return {**result, 'complete': True}
        iterator = _RuntimeScans.entries.get(key)
        if iterator is None:
            iterator = _RuntimeIterator(folder)
            _RuntimeScans.entries[key] = iterator
        _RuntimeScans.entries.move_to_end(key)
        while len(_RuntimeScans.entries) > _RuntimeScans.max_directories:
            _close_scan(next(iter(_RuntimeScans.entries)))
        try:
            while result['scanned_entries'] < max_entries and time.monotonic() < deadline:
                try:
                    entry = next(iterator)
                except StopIteration:
                    _close_scan(key)
                    result['complete'] = True
                    break
                result['scanned_entries'] += 1
                if entry.name.startswith('.') or not entry.name.endswith('.json') or not entry.is_file(follow_symlinks=False):
                    continue
                try:
                    size, exhausted = _archive_one(paths, directory, Path(entry.path), before=cutoff,
                        max_bytes=max_bytes - result['bytes'], deadline=deadline)
                    result['bytes'] += size
                    result['archived_entries'] += bool(size)
                    if exhausted:
                        iterator.pending = entry
                        break
                except (OSError, ValueError, DataCorruptionError, sqlite3.Error) as exc:
                    result['errors'].append({'name': entry.name, 'error_type': type(exc).__name__})
        except BaseException:
            _close_scan(key)
            raise
    return result
