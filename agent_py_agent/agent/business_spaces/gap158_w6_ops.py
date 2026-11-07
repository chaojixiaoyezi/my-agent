"""管理员运维入口：共用 CLI 更新步骤、main 作业回执及既有有界维护通道。"""
from __future__ import annotations

import errno
import hashlib
import importlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from agent_py_agent.cli import update

from ..common.json_io import read_json_object_report
from ..common.nofollow_fs import open_private_lock_beneath, split_existing_anchor
from ..conversation.business_access import BusinessError
from ..gateway_parts.restart_service import hosting_gateway_pid, submit_restart_request
from ..plugin_host.errors import HostAPIError
from ..user_space.storage_lanes import (
    LANE_BINDINGS,
    StorageLaneBudget,
    StorageLaneRequest,
    resolve_storage_lane,
    validate_lane_receipt,
)
from .v13_store import v13_store_for

_run_lock = threading.Lock()


def _ring_limit():
    """W0 未合入仅使用合同默认值；缺少内部依赖不能伪装成 W0 缺失。"""
    name = __package__ + '.gap158_limits'
    try:
        limits = importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
        return 2000
    return max(1, int(limits.OPS_UPDATE_LOG_RING))


def _admin(call):
    from ..gateway_parts.business_api_v13 import v13_require_admin
    return v13_require_admin(call)


def _remote_enabled(call):
    return getattr(call.context.host.config, 'ops_remote_update_enabled', False) is True


def _digest(call):
    return hashlib.sha256(json.dumps([call.action, call.params], sort_keys=True,
        separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _transaction(store, mutate, *, key=None, digest='job-progress'):
    return store.transaction('ops_update_jobs', mutate,
        idempotency_key=key or ('ops-' + uuid.uuid4().hex), params_digest=digest)


class _RunLease:
    """线程锁和同一 no-follow 文件锁一起转交工作者，直到副作用真实结束。"""

    def __init__(self, descriptor):
        self.descriptor = descriptor

    def close(self):
        descriptor, self.descriptor = self.descriptor, None
        if descriptor is None:
            return
        try:
            if os.name == 'nt':
                import msvcrt
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            try:
                os.close(descriptor)
            finally:
                _run_lock.release()


def _acquire_run(store):
    if not _run_lock.acquire(blocking=False):
        raise BusinessError('MAINTENANCE_BUSY')
    descriptor = None
    try:
        anchor, parts = split_existing_anchor(Path(store.owner_home))
        descriptor = open_private_lock_beneath(anchor,
            (*parts, 'data', 'business_v13', '.ops-maintenance.lock'))
        if os.name == 'nt':
            import msvcrt
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _RunLease(descriptor)
    except BaseException as exc:
        try:
            if descriptor is not None:
                os.close(descriptor)
        finally:
            _run_lock.release()
        if isinstance(exc, OSError) and exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            raise BusinessError('MAINTENANCE_BUSY') from None
        raise


def _job_for(call, rows, job_id):
    row = rows.get(job_id)
    if row is None or row.get('owner_id') != call.owner_id:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    return row


def _git_preflight():
    source = update.detect_install_source()
    if source.kind != 'git' or source.repo is None:
        raise BusinessError('STATE_CONFLICT', details={'reason': 'source_not_git', 'source': source.kind})
    repo = Path(source.repo).resolve()
    code, dirty = update._git(repo, ['status', '--porcelain'])
    if code or dirty:
        raise BusinessError('STATE_CONFLICT', details={
            'reason': 'worktree_status_unavailable' if code else 'worktree_dirty'})
    code, commit = update._git(repo, ['rev-parse', '--verify', 'HEAD'])
    if code or re.fullmatch(r'[0-9a-fA-F]{40,64}', commit) is None:
        raise BusinessError('STATE_CONFLICT', details={'reason': 'commit_unavailable'})
    return repo, commit


def _update_job(store, job_id, mutate):
    def change(rows):
        mutate(rows[job_id])
        return {}
    return _transaction(store, change)


def _log_step(call, store, job_id, step):
    """回调逐步写入有界日志；序号从一开始，裁剪不重置序号。"""
    text = str(step.get('text') or step.get('cli') or step.get('step') or '')
    stream = step.get('stream', 'status')
    if stream not in {'stdout', 'stderr', 'status'}:
        stream = 'status'
    # 单条公开文本最多 2000 字符；大输出只保留本 ring 尾部，不构造无界分行数组。
    size = 2000
    chunks = max(1, (len(text) + size - 1) // size)
    limit = _ring_limit()

    def append(row):
        first = max(0, chunks - limit)
        sequence = row['last_seq']
        lines = list(row['lines']) if not first else []
        for position in range(first, chunks):
            lines.append({'seq': sequence + position + 1, 'at': float(call.clock()),
                'stream': stream, 'text': text[position * size:(position + 1) * size]})
        row.update(last_seq=sequence + chunks, lines=lines[-limit:])
    _update_job(store, job_id, append)


def _start_worker(target):
    worker = threading.Thread(target=target, name='ops-update', daemon=True)
    worker.start()
    return worker


def _restart(call):
    _admin(call)
    paths = getattr(call.context.host, 'business_v13_gateway_paths', None)
    if paths is None:
        raise HostAPIError('HOST_NOT_READY', 503)
    return submit_restart_request(paths, target_pid=hosting_gateway_pid() or os.getpid(),
        requester={'kind': 'business_ops', 'owner_id': call.owner_id,
            'subject_id': call.subject_id, 'request_id': call.request_id},
        reason='管理员运维更新完成',
        cooldown_seconds=float(getattr(call.context.host.config, 'gateway_restart_cooldown_seconds', 30)),
        now=float(call.clock()))


def _run_update(call, store, job, lease):
    job_id = job['job_id']
    try:
        _admin(call)
        if not _remote_enabled(call):
            raise HostAPIError('HOST_NOT_READY', 503)
        _update_job(store, job_id, lambda row: row.update(state='running'))
        actual_repo, actual_commit = _git_preflight()
        if str(actual_repo) != job['repo'] or actual_commit != job['start_commit']:
            raise BusinessError('STATE_CONFLICT', details={'reason': 'checkout_changed_before_start'})

        def report(step):
            _admin(call)
            if not _remote_enabled(call):
                raise HostAPIError('HOST_NOT_READY', 503)
            _log_step(call, store, job_id, step)

        repo = Path(job['repo'])
        if job['operation'] == 'apply':
            result = update.apply_update_steps(repo, pre_commit=job['pre_commit'],
                refuse_dirty=True, on_step=report)
        else:
            result = update.rollback_update_steps(repo, job['pre_commit'], on_step=report)
        state = ('rolled_back' if job['operation'] == 'rollback' else 'succeeded') if result['returncode'] == 0 else 'failed'

        def finish(rows):
            rows[job_id].update(state=state, finished_at=float(call.clock()),
                returncode=result['returncode'], post_commit=result.get('post_commit', ''),
                reason=result.get('reason', ''))
            if state == 'rolled_back':
                rows[job['rollback_of']].update(state='rolled_back', rollback_job_id=job_id)
            return {}
        _transaction(store, finish)
        if state == 'succeeded' and job['restart']:
            receipt = _restart(call)
            _log_step(call, store, job_id, {'step': 'restart', 'text': str(receipt['status'])})
    except Exception as exc:
        # 不把 git/pip 的敏感环境或异常链写入公开日志；异常不自动重放副作用。
        code = getattr(exc, 'code', type(exc).__name__)
        try:
            _log_step(call, store, job_id, {'stream': 'stderr', 'text': str(code)})
            _update_job(store, job_id, lambda row: row.update(state='failed',
                finished_at=float(call.clock()), reason=str(code)))
        except Exception:
            # 存储或授权失效时保持原耐久状态，不伪造一条成功终态。
            pass
    finally:
        lease.close()


def _queue_update(call):
    if not _remote_enabled(call):
        raise HostAPIError('HOST_NOT_READY', 503)
    if call.params.get('restart') and getattr(call.context.host, 'business_v13_gateway_paths', None) is None:
        raise HostAPIError('HOST_NOT_READY', 503)
    store = v13_store_for(call, 'main')
    created = []
    leases = []
    operation = call.action.rsplit('.', 1)[1]

    def create(rows):
        _admin(call)
        leases.append(_acquire_run(store))
        repo, current = _git_preflight()
        pre_commit = current
        rollback_of = ''
        if operation == 'rollback':
            old = _job_for(call, rows, call.params['job_id'])
            if old['operation'] != 'apply' or old['state'] not in {'succeeded', 'failed'}:
                raise BusinessError('STATE_CONFLICT', details={'state': old['state'], 'reason': 'job_not_rollbackable'})
            if old['repo'] != str(repo):
                raise BusinessError('STATE_CONFLICT', details={'reason': 'install_source_changed'})
            pre_commit, rollback_of = old['pre_commit'], old['job_id']
        identifier = 'ops-' + uuid.uuid4().hex
        # Store 对此集合使用快照；保留有界摘要，永久同键回执仍由 Store 的逐键日志承担。
        completed = sorted((row for row in rows.values() if row['state'] not in {'queued', 'running'}
                            and row['job_id'] != rollback_of), key=lambda row: (row['created_at'], row['job_id']))
        for row in completed[:max(0, len(rows) - _ring_limit() + 1)]:
            del rows[row['job_id']]
        row = {'job_id': identifier, 'owner_id': call.owner_id, 'operation': operation,
            'state': 'queued', 'repo': str(repo), 'pre_commit': pre_commit, 'start_commit': current, 'rollback_of': rollback_of,
            'restart': bool(call.params.get('restart', False)), 'created_at': float(call.clock()),
            'lines': [], 'last_seq': 0}
        rows[identifier] = row
        created.append(row)
        result = {'job_id': identifier, 'state': 'queued'}
        if operation == 'rollback':
            result['pre_commit'] = pre_commit
        return result

    try:
        result = _transaction(store, create, key=call.idempotency_key, digest=_digest(call))
        if created:
            lease = leases[0]
            try:
                _start_worker(lambda: _run_update(call, store, created[0], lease))
            except Exception:
                _update_job(store, created[0]['job_id'], lambda row: row.update(state='failed', reason='worker_not_started'))
                raise HostAPIError('HOST_NOT_READY', 503) from None
            leases.clear()
        return result
    finally:
        for lease in leases:
            lease.close()


def _stream(call):
    store = v13_store_for(call, 'main')
    row = _job_for(call, {call.params['job_id']: store.get('ops_update_jobs', call.params['job_id'])},
        call.params['job_id'])
    after = call.params.get('after_seq', 0)
    lines = [line for line in row['lines'] if line['seq'] > after][:call.params.get('limit', 50)]
    first = row['lines'][0]['seq'] if row['lines'] else row['last_seq'] + 1
    return {'job_id': row['job_id'], 'state': row['state'], 'lines': lines,
        'next_seq': lines[-1]['seq'] if lines else after, 'overflow': after < first - 1}


class _DryRunWrite(RuntimeError):
    """既有通道没有独立预演协议；写门只拒绝、不猜测 operation 的返回值。"""


def _gc_receipt(call, kind, dry_run):
    lane = resolve_storage_lane(kind)
    empty = {'lane': kind, 'state': 'skipped', 'scanned': 0, 'removed': 0, 'bytes_freed': 0}
    if lane is None:
        return empty
    config, home = call.context.host.config, call.context.host.home_paths
    policy = read_json_object_report(home.owner_retention_json, context='ops.gc.retention')
    if policy.load_error is not None or type(policy.payload.get('legal_hold', False)) is not bool:
        raise HostAPIError('HOST_NOT_READY', 503)
    hold = policy.payload.get('legal_hold', False)
    entries = getattr(config, 'retention_scan_slice_entries', 128)
    size = getattr(config, 'retention_scan_slice_bytes', 8388608)
    milliseconds = getattr(config, 'retention_scan_slice_ms', 25)
    if any(type(value) is not int or value <= 0 for value in (entries, size, milliseconds)):
        raise HostAPIError('HOST_NOT_READY', 503)
    budget = StorageLaneBudget(entries, size, time.monotonic() + milliseconds / 1000)

    def commit(operation):
        _admin(call)
        if dry_run or hold:
            raise _DryRunWrite()
        return operation()

    # 原通道 legal_hold 是既有的零删除路径；预演回执必须标 skipped，不能冒充已扫描/释放。
    request = StorageLaneRequest(kind=kind, home=home, now=float(call.clock()), cursor={},
        budget=budget, legal_hold=dry_run or hold, coordinator=True, pressure=None, commit=commit)
    try:
        raw = validate_lane_receipt(lane(request))
    except _DryRunWrite:
        return empty
    except (BusinessError, HostAPIError):
        raise
    except (OSError, sqlite3.Error, RuntimeError, ValueError, TypeError, KeyError):
        return {**empty, 'state': 'failed', 'error_code': 'HOST_NOT_READY'}
    if dry_run or hold:
        return {**empty, 'scanned': raw['items']}
    state = 'done' if raw['complete'] else 'partial'
    if raw.get('errors'):
        state = 'failed'
    elif raw.get('deferred_reason') and not raw.get('deleted_items', 0):
        state = 'skipped'
    result = {'lane': kind, 'state': state, 'scanned': raw['items'],
        'removed': raw.get('deleted_items', 0), 'bytes_freed': raw.get('freed_bytes', 0)}
    if state == 'failed':
        result['error_code'] = 'HOST_NOT_READY'
    return result


def _gc(call):
    kinds = call.params.get('lanes', list(LANE_BINDINGS))
    if len(set(kinds)) != len(kinds) or any(kind not in LANE_BINDINGS for kind in kinds):
        raise BusinessError('INVALID_SCHEMA')
    store = v13_store_for(call, 'main')

    def run(rows):
        lease = _acquire_run(store)
        try:
            _admin(call)
            # 每个通道只推进一片，不循环追平；领域原保留策略和写门保持不变。
            return {'receipt': [_gc_receipt(call, kind, call.params.get('dry_run', False)) for kind in kinds]}
        finally:
            lease.close()
    return _transaction(store, run, key=call.idempotency_key, digest=_digest(call))


def _validate_params(call):
    """注册器负责完整 JSON Schema；直调边界也拒绝额外字段及 bool 冒充整数。"""
    allowed = {'ops.update.check': (), 'ops.update.apply': ('restart',),
        'ops.update.rollback': ('job_id',), 'ops.update.stream': ('job_id', 'after_seq', 'limit'),
        'ops.gc.run': ('lanes', 'dry_run')}
    params = call.params
    if call.action not in allowed or type(params) is not dict or params.keys() - set(allowed[call.action]):
        raise BusinessError('INVALID_SCHEMA')
    if call.action in {'ops.update.rollback', 'ops.update.stream'}:
        identifier = params.get('job_id')
        if type(identifier) is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', identifier) is None:
            raise BusinessError('INVALID_SCHEMA')
    if any(key in params and type(params[key]) is not bool for key in ('restart', 'dry_run')):
        raise BusinessError('INVALID_SCHEMA')
    if 'after_seq' in params and (type(params['after_seq']) is not int or params['after_seq'] < 0):
        raise BusinessError('INVALID_SCHEMA')
    if 'limit' in params and (type(params['limit']) is not int or not 1 <= params['limit'] <= 100):
        raise BusinessError('INVALID_SCHEMA')
    if 'lanes' in params and (type(params['lanes']) is not list or len(params['lanes']) > 16
            or any(type(kind) is not str or re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', kind) is None
                   for kind in params['lanes'])):
        raise BusinessError('INVALID_SCHEMA')
    if call.action in {'ops.update.apply', 'ops.update.rollback', 'ops.gc.run'}:
        if type(call.idempotency_key) is not str or re.fullmatch(r'[A-Za-z0-9_-]{16,128}', call.idempotency_key) is None:
            raise BusinessError('INVALID_SCHEMA')


def handle_ops(call):
    """只接可信 V13Call；注册动作及 ready= 开关由 W6 slice 负责。"""
    _admin(call)
    _validate_params(call)
    if call.action == 'ops.update.check':
        status = update.update_status()
        return {key: status[key] for key in ('source', 'current', 'latest', 'update_available', 'dirty', 'notes')}
    if call.action in {'ops.update.apply', 'ops.update.rollback'}:
        return _queue_update(call)
    if call.action == 'ops.update.stream':
        return _stream(call)
    return _gc(call)


__all__ = ['handle_ops']
