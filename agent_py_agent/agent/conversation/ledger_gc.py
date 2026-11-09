"""沿原锁处理一片旧账；持久续点由owner维护lane保存，本模块不保存第二份状态。"""
from __future__ import annotations

import logging
import stat
import time
from copy import deepcopy

from ..gateway_parts.io import cleanup_orphan_lock_files, locked_file_transition
from ..user_space.directory_paging import directory_page
from . import task_history
from .run_claim import detached_task_claim_scope_id
from .store_claims import _read_claim_report, _stale_finished_claim
from .store_progress import _read_progress_policy_report
from .store_wake_quarantine_archive import (
    WAKE_QUARANTINE_ARCHIVE_AFTER_SECONDS,
    _archive_loose,
    _archive_record,
)

COUNTERS = ('archived_policies', 'archived_claims', 'archived_wake_quarantine',
            'cleaned_orphan_locks', 'quarantined_task_links', 'pruned_task_ids')
_PHASES = ('policies', 'claims', 'quarantine', 'unreadable', 'loose_ledger', 'locks', 'threads')


class _LockWindow:
    def __init__(self, path):
        self.path = path

    def is_dir(self):
        return self.path.parent.is_dir()

    def glob(self, pattern):
        if pattern != '*.lock':
            raise ValueError('invalid_gc_lock_window')
        return (self.path,)


def _regular(path):
    info = path.lstat()
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1


def _move_policy(store, path, current, retention, write):
    policy, error = _read_progress_policy_report(path)
    if error is not None:
        raise ValueError('gc_policy_unreadable')
    if policy is None or policy.enabled or current - max(policy.last_report_at, policy.next_due_at) <= retention:
        return 0
    def apply():
        with locked_file_transition(path, blocking=False):
            policy, error = _read_progress_policy_report(path)
            if error is not None:
                raise ValueError('gc_policy_unreadable')
            if policy is None or policy.enabled or current - max(policy.last_report_at, policy.next_due_at) <= retention:
                return 0
            target = store.storage.root / '.ledger_archive' / 'policies'
            target.mkdir(parents=True, exist_ok=True)
            path.rename(target / path.name)
        return 1
    return write(apply)


def _move_claim(store, path, current, retention, write):
    claim, error = _read_claim_report(path)
    if error is not None:
        raise ValueError('gc_claim_unreadable')
    if not _stale_finished_claim(claim, current, retention):
        return 0
    # 复用唯一claim迁移写者；非阻塞失败由本片记unknown，取消证明及稳定锁合同不变。
    return int(write(lambda: store.claims._archive_released_claim(
        path, store.storage.root / '.ledger_archive' / 'claims', current=current,
        retention_seconds=retention, blocking=False)))


def _source_bytes(path, *, maximum=8 * 1024 * 1024):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 0
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
        raise ValueError('gc_source_outside_read_budget')
    return info.st_size


def _heal_read_budget(store, thread, cursor, limit, available=None):
    """按精确本片来源大小保守预扣重读上界；不解析超预算正文，也不计作物理释放。"""
    active = cursor.get('active', 0)
    history = cursor.get('history', 0)
    if any(type(value) is not int or value < 0 for value in (active, history)):
        raise ValueError('invalid_task_link_heal_cursor')
    cost = sum(4 * _source_bytes(store.storage.task_path(identifier))
               for identifier in thread.active_task_ids[active:active + limit])
    path = store.storage.thread_path(thread.thread_id)
    root = task_history.history_root(path)
    cost += _source_bytes(root / 'index.json', maximum=4096) * (6 * limit + 2)
    if available is not None and cost > available:
        return cost
    manifest = task_history._read(root, ('index.json',))
    if manifest is None:
        identifiers = thread.task_ids[history:history + limit]
        # 首次旧库存发表后会读取新页/头；这是一次性迁移的读预算，后继使用真实文件大小。
        if thread.task_ids and thread.task_history_version == 0:
            cost += 2 * 65536 + (6 * limit + 2) * 4096
    else:
        identifiers = []
        stop = min(manifest['count'], history + limit)
        for number in range(history // 128, (stop - 1) // 128 + 1) if stop > history else ():
            parts = ('pages', f'{number}.json')
            cost += 2 * _source_bytes(root.joinpath(*parts), maximum=65536)
            if available is not None and cost > available:
                return cost
            rows = task_history._read(root, parts)
            if not isinstance(rows, list):
                raise ValueError('gc_task_history_page_unavailable')
            begin, end = max(history, number * 128), min(stop, (number + 1) * 128)
            identifiers.extend(rows[begin - number * 128:end - number * 128])
    for identifier in identifiers:
        cost += 6 * _source_bytes(root.joinpath(*task_history._member_parts(identifier)), maximum=65536)
        scope = detached_task_claim_scope_id(thread.thread_id, identifier)
        cost += 2 * _source_bytes(store.storage.background_claim_path(scope))
    return cost


def ledger_gc_page(store, *, current, retention_seconds, cursor=None, max_items=128, deadline=None, run_write=None, max_bytes=None):
    """最多128个物理候选；完整性与续点随回执返回，不把坏账当空。"""
    if type(max_items) is not int or not 0 < max_items <= 128:
        raise ValueError('invalid_ledger_gc_budget')
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
        raise ValueError('invalid_ledger_gc_byte_budget')
    state = deepcopy(cursor or {})
    phase = state.get('phase', 'policies')
    if phase not in _PHASES:
        raise ValueError('invalid_ledger_gc_phase')
    state['phase'] = phase
    selected_thread = state.get('thread', '')
    if (not isinstance(selected_thread, str) or selected_thread in {'.', '..'}
            or any(value in selected_thread for value in ('/', '\\', '\x00'))):
        raise ValueError('invalid_ledger_gc_thread_cursor')
    result = {**dict.fromkeys(COUNTERS, 0), 'cursor': state, 'items': 0,
              'complete': False, 'errors': [], 'bytes': 0}
    if max_bytes == 0:
        result['deferred_reason'] = 'budget'
        return result
    write = run_write or (lambda operation: operation())
    storage = store.storage
    directories = {'policies': storage.policies_dir, 'claims': storage.background_claims_dir,
                   'quarantine': storage.wake_quarantine_dir,
                   'unreadable': storage.wake_quarantine_unreadable_dir,
                   'loose_ledger': storage.wake_quarantine_ledger_dir,
                   'threads': storage.threads_dir}
    while result['items'] < max_items and (deadline is None or time.monotonic() < deadline):
        phase = state['phase']
        locks = storage.lock_sidecar_dirs()
        lock_index = state.get('lock_index', 0)
        if type(lock_index) is not int or not 0 <= lock_index <= len(locks):
            raise ValueError('invalid_ledger_gc_lock_cursor')
        if phase == 'locks' and lock_index >= len(locks):
            state.update(phase='threads', scan={})
            continue
        directory = locks[lock_index] if phase == 'locks' else directories[phase]
        names, position, complete = [], state.get('scan', {}), False
        try:
            if phase == 'threads' and state.get('thread'):
                names, position, complete = [state['thread']], state.get('scan', {}), state.get('directory_complete', False)
            else:
                names, position, complete = directory_page(storage.root, directory.relative_to(storage.root).parts,
                                                           state.get('scan', {}), 1, require_dir_fd=False)
                result['items'] += max(1, position.pop('last_scanned', len(names)))
            if names:
                name = names[0]
                path = directory / name
                if not name.endswith('.lock' if phase == 'locks' else '.json'):
                    state['scan'] = position
                    continue
                source_cost = 3 * _source_bytes(path) if phase != 'locks' else 0
                if max_bytes is not None and source_cost > max_bytes:
                    raise ValueError('gc_item_exceeds_slice_byte_budget')
                if max_bytes is not None and result['bytes'] + source_cost > max_bytes:
                    result['deferred_reason'] = 'budget'
                    return result
                result['bytes'] += source_cost
                if phase == 'threads' and name.endswith('.json') and _regular(path):
                    state.update(thread=name, scan=position, directory_complete=complete)
                    thread, error = store.threads.load_report(path.stem)
                    if error is not None or thread is None:
                        raise ValueError('gc_thread_unreadable')
                    remaining = max_items - result['items']
                    if remaining <= 0:
                        break
                    read_budget = _heal_read_budget(store, thread, state.get('task', {}), min(16, remaining),
                        None if max_bytes is None else max_bytes - result['bytes'])
                    if max_bytes is not None and source_cost + read_budget > max_bytes:
                        raise ValueError('gc_item_exceeds_slice_byte_budget')
                    if max_bytes is not None and result['bytes'] + read_budget > max_bytes:
                        result['deferred_reason'] = 'budget'
                        return result
                    result['bytes'] += read_budget
                    page = store.tasks.heal_broken_links(
                        thread, cursor=state.get('task', {}), limit=min(16, remaining), run_write=write)
                    result['items'] += page['items']
                    for key in ('quarantined_task_links', 'pruned_task_ids'):
                        result[key] += page[key]
                    state['task'] = page['cursor']
                    if not page['complete']:
                        continue
                    for key in ('thread', 'task', 'directory_complete'):
                        state.pop(key, None)
                elif phase == 'locks':
                    if (name.endswith('.lock') and _regular(path)
                            and current - path.stat().st_mtime >= 86400
                            and not path.with_name(name[:-5]).exists()):
                        result['cleaned_orphan_locks'] += cleanup_orphan_lock_files(_LockWindow(path), now=current, commit_transition=write)
                elif name.endswith('.json') and _regular(path):
                    if phase == 'policies':
                        result['archived_policies'] += _move_policy(store, path, current, retention_seconds, write)
                    elif phase == 'claims':
                        result['archived_claims'] += _move_claim(store, path, current, retention_seconds, write)
                    elif phase == 'quarantine':
                        result['archived_wake_quarantine'] += write(lambda: _archive_record(storage, path, current,
                            WAKE_QUARANTINE_ARCHIVE_AFTER_SECONDS, blocking=False))
                    elif phase == 'unreadable':
                        result['archived_wake_quarantine'] += write(lambda: _archive_loose(path,
                            storage.wake_quarantine_archive_dir / 'unreadable' / name, current,
                            WAKE_QUARANTINE_ARCHIVE_AFTER_SECONDS))
                    elif phase == 'loose_ledger' and not (storage.wake_quarantine_dir / name).exists():
                        result['archived_wake_quarantine'] += write(lambda: _archive_loose(path,
                            storage.wake_quarantine_archive_dir / 'ledger' / name, current,
                            WAKE_QUARANTINE_ARCHIVE_AFTER_SECONDS))
            state['scan'] = position
        except FileNotFoundError:
            # 并发删除是缺失事实；仍推进已枚举的物理位置，后继整轮会重核目录身份。
            state['scan'] = locals().get('position', state.get('scan', {}))
        except (OSError, ValueError, RuntimeError) as error:
            result['errors'].append({'code': 'LEDGER_GC_UNAVAILABLE', 'phase': phase,
                                     'error_type': type(error).__name__})
            logging.getLogger('agent.conversation.store').warning(
                'TASK_LINK_HEAL_FAILED thread=%s error_type=%s', state.get('thread', ''), type(error).__name__)
            state['unknown_seen'] = True
            state['scan'] = locals().get('position', state.get('scan', {}))
            for key in ('thread', 'task', 'directory_complete'):
                state.pop(key, None)
            if not names:
                result['deferred_reason'] = 'directory_unknown'
                result['scan_complete'] = True
                return result
            # 单条坏账不饿死其他线程；整轮未知只报告扫描结束，不签完整。
        if complete:
            state['scan'] = {}
            if phase == 'locks':
                state['lock_index'] = lock_index + 1
                continue
            index = _PHASES.index(phase) + 1
            if index == len(_PHASES):
                result['complete'] = not state.get('unknown_seen', False)
                result['scan_complete'] = True
                result['cursor'] = {}
                return result
            state['phase'] = _PHASES[index]
    result['deferred_reason'] = 'budget'
    return result
