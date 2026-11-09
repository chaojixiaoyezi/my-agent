"""分片退役输入回合引用；只接受原请求终态与已消费 canonical 双重证明。"""
from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import time
from contextlib import ExitStack, contextmanager
from functools import wraps
from pathlib import Path

from ..common import json_io
from ..common.nofollow_fs import (
    ensure_private_dir,
    open_directory_beneath,
    read_bytes_beneath,
    write_bytes_atomic_beneath,
)
from .io import (
    _cleanup_orphan_lock_at,
    try_existing_file_transition_at,
    try_gateway_turn_transition,
)
from .runtime_file_retention import _close_scan, _RuntimeScans


@contextmanager
def turn_ref_birth(paths, *, blocking=True, exclusive=False, create=False):
    """现存命名空间目录FD是出生门；读者不出生共享锁文件，EX仅用于删除空turn目录。"""
    if create:
        ensure_private_dir(paths.root / 'input_receipts' / 'by_turn')
    if json_io.fcntl is None:
        yield not exclusive  # 无目录flock的平台不删除空目录，避免与新ref出生竞态。
        return
    try:
        descriptor = open_directory_beneath(paths.root, ('input_receipts', 'by_turn'), require_dir_fd=True)
    except FileNotFoundError:
        yield True  # 无目录即无引用；读口继续原空目录语义，不创建共享状态。
        return
    try:
        mode = json_io.fcntl.LOCK_EX if exclusive else json_io.fcntl.LOCK_SH
        json_io.fcntl.flock(descriptor, mode | (0 if blocking else json_io.fcntl.LOCK_NB))
        yield True
    finally:
        os.close(descriptor)


def guard_turn_ref_birth(function):
    @wraps(function)
    def guarded(paths, *args, **kwargs):
        blocking = kwargs.get('blocking', function.__name__ != 'transport_turn_input_references')
        with turn_ref_birth(paths, blocking=blocking, create=function.__name__ == '_write_active_turn_ref'):
            return function(paths, *args, **kwargs)
    return guarded


def _terminal(paths, request_id, before):
    from .owner_retention import lookup_sealed_receipt

    raw = read_bytes_beneath(paths.root, (*paths.terminal.relative_to(paths.root).parts, request_id + '.json'), max_bytes=1048576, require_dir_fd=True)
    if raw is None:
        raw = lookup_sealed_receipt(paths, str((paths.terminal / (request_id + '.json')).relative_to(paths.root)))
    if raw is None:
        return False
    row = json.loads(raw)
    ended = row.get('completed_at')
    return (row.get('schema_version') == 'gateway_terminal_request.v1' and row.get('id') == request_id
        and row.get('turn_phase') == 'closed' and row.get('status') in ('done', 'completed', 'failed', 'cancelled')
        and type(ended) in (int, float) and math.isfinite(ended) and 0 < ended < before
        and not any(row.get(key) for key in ('execution_unknown', 'replay_pending', 'outbox_pending', 'delivery_pending', 'unconsumed'))
        and not any((folder / (request_id + '.json')).exists() for folder in (paths.inbox, paths.processing)))


def _retire_ref(paths, path, before):
    from .input_delivery_service import read_gateway_input_receipt
    from .input_transport import transport_gateway_request_id

    parts = path.relative_to(paths.root).parts
    raw = read_bytes_beneath(paths.root, parts, max_bytes=1048576, require_dir_fd=True)
    if raw is None:
        return 0
    ref = json.loads(raw)
    request_id, turn_id = ref.get('request_id'), ref.get('target_turn_id')
    if (ref.get('schema_version') != 'gateway_input_turn_ref.v1' or type(request_id) is not str
            or not request_id or '/' in request_id or '\\' in request_id or type(turn_id) is not str
            or not turn_id or '/' in turn_id or '\\' in turn_id
            or path.name != hashlib.sha256(request_id.encode()).hexdigest() + '.json'
            or path.parent.name != hashlib.sha256(turn_id.encode()).hexdigest()):
        return 0
    # 与生产者相同顺序：input T -> target T -> 原ref JSON锁。只借现存ref锁，禁止造新孤儿锁。
    with ExitStack() as stack:
        if not stack.enter_context(try_gateway_turn_transition(paths, request_id)):
            return 0
        receipt = read_gateway_input_receipt(paths, request_id, blocking=False)
        if (receipt is None or receipt.state != 'consumed' or receipt.reconcile_error
                or receipt.target_turn_id != turn_id or receipt.client_message_id != ref.get('client_message_id')
                or receipt.updated_at >= before):
            return 0
        target = transport_gateway_request_id(receipt.transport) if receipt.transport else turn_id
        if target != request_id and not stack.enter_context(try_gateway_turn_transition(paths, target)):
            return 0
        if not _terminal(paths, target, before):
            return 0
        descriptor = open_directory_beneath(paths.root, parts[:-1], require_dir_fd=True)
        stack.callback(os.close, descriptor)
        stack.enter_context(turn_ref_birth(paths, blocking=False))
        with try_existing_file_transition_at(descriptor, path) as acquired:
            if not acquired:
                return 0
            if read_bytes_beneath(paths.root, parts, max_bytes=1048576, require_dir_fd=True) != raw:
                return 0
            os.unlink(path.name, dir_fd=descriptor)
        # 仍持input/target原T；退出ref锁后借原inode退役，身份复核拒绝出生交错。
        if not _cleanup_orphan_lock_at(descriptor, path.name + '.lock', current=time.time()):
            # 年轻锁或读者在退出窗口借走原锁时，保留定位前像，下一片仍有明确终态来源可重试。
            write_bytes_atomic_beneath(paths.root, parts, raw)
            return 0
        return len(raw)


def retire_turn_refs_slice(paths, *, before, max_entries=128, max_ms=25, commit_transition=None):
    """复用有界目录游标，宽限至少七天；未知、活跃及锁忙只保留。"""
    result = {'complete': False, 'scanned_entries': 0, 'bytes': 0, 'archived_entries': 0, 'errors': []}
    root = paths.root / 'input_receipts/by_turn'
    if not root.exists():
        return {**result, 'complete': True}
    deadline = time.monotonic() + max_ms / 1000
    cutoff = min(before, time.time() - 7 * 86400)
    key = str(root.absolute())
    with _RuntimeScans.lock:
        parent = open_directory_beneath(paths.root, ('input_receipts', 'by_turn'), require_dir_fd=True)
        os.close(parent)
        iterator = _RuntimeScans.entries.get(key)
        if iterator is None:
            # 逐层枚举并在每个叶子消耗预算，绝不先列出历史全集。
            def walk():
                with os.scandir(root) as folders:
                    for folder in folders:
                        if not folder.is_dir(follow_symlinks=False):
                            yield Path(folder.path)
                            continue
                        with os.scandir(folder.path) as entries:
                            yield from (Path(entry.path) for entry in entries)
                        yield Path(folder.path)
            iterator = walk()
            _RuntimeScans.entries[key] = iterator
        _RuntimeScans.entries.move_to_end(key)
        while len(_RuntimeScans.entries) > _RuntimeScans.max_directories:
            _close_scan(next(iter(_RuntimeScans.entries)))
        while result['scanned_entries'] < max_entries and time.monotonic() < deadline:
            try:
                path = next(iterator)
            except StopIteration:
                _close_scan(key)
                result['complete'] = True
                break
            result['scanned_entries'] += 1
            try:
                if path.suffix == '.json':
                    def operation(path=path):
                        return _retire_ref(paths, path, cutoff)
                    freed = operation() if commit_transition is None else commit_transition(operation)
                    result['bytes'] += freed
                    result['archived_entries'] += bool(freed)
                elif path.parent == root and path.suffix != '.lock':
                    def retire_directory(path=path):
                        with turn_ref_birth(paths, blocking=False, exclusive=True) as acquired:
                            if not acquired:
                                return
                            parent = open_directory_beneath(paths.root, ('input_receipts', 'by_turn'), require_dir_fd=True)
                            try:
                                os.rmdir(path.name, dir_fd=parent)  # 空目录与全部ref/lock出生互斥。
                            finally:
                                os.close(parent)
                    if commit_transition is None:
                        retire_directory()
                    else:
                        commit_transition(retire_directory)
            except (OSError, ValueError, TypeError) as error:
                if isinstance(error, BlockingIOError) or isinstance(error, OSError) and error.errno in (errno.ENOTEMPTY, errno.EEXIST):
                    continue
                if len(result['errors']) < 16:
                    result['errors'].append({'name': path.name, 'error_type': type(error).__name__})
    return result


def maintain_input_turn_refs_lane(request):
    """普通及Count>0维护共用同一分片消费者，每个候选沿原同步写门提交。"""
    from ..user_space.owner_maintenance import _real_owner_directory
    from ..user_space.runtime_paths import runtime_paths_for_agent
    from ..user_space.storage_lanes import STORAGE_LANE_SLICE_ENTRIES, STORAGE_LANE_SLICE_MS
    from .paths import gateway_paths_from_root

    empty = {'cursor': {}, 'complete': True, 'items': 0, 'bytes': 0}
    if request.legal_hold:
        return {**empty, 'deferred_reason': 'legal_hold'}
    root = runtime_paths_for_agent(None, request.home.owner_home_dir, request.home)['gateway_workspace']
    if not _real_owner_directory(request.home, root):
        return empty
    remaining = request.budget.deadline
    if request.budget.exhausted():
        return {**empty, 'complete': False}
    byte_items = STORAGE_LANE_SLICE_ENTRIES if request.budget.max_bytes is None else request.budget.max_bytes // (4 * 1048576)
    if byte_items < 1:
        return {**empty, 'complete': False, 'deferred_reason': 'byte_budget'}
    max_ms = STORAGE_LANE_SLICE_MS if remaining is None else min(STORAGE_LANE_SLICE_MS, max(0.001, (remaining - time.monotonic()) * 1000))
    report = retire_turn_refs_slice(gateway_paths_from_root(root), before=request.now - 7 * 86400,
        max_entries=min(STORAGE_LANE_SLICE_ENTRIES, request.budget.max_items or STORAGE_LANE_SLICE_ENTRIES, byte_items),
        max_ms=max_ms, commit_transition=request.run_write)
    return {'cursor': {}, 'complete': report['complete'], 'items': report['scanned_entries'],
            'bytes': report['bytes'], 'deleted_items': report['archived_entries'], 'errors': report['errors']}
