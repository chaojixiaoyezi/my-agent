"""沿既有存储维护通道分片维护任务遥测，不在审计查询中修复投影。"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from pathlib import Path

from ..common.nofollow_fs import (
    open_directory_beneath,
    open_readonly_file_beneath,
    unlink_file_beneath,
)
from ..local_storage.owner_read_index import _OWNER_INDEX_MAX_ROW_BYTES, OwnerReadIndex
from ..memory_store.retention_models import MemoryRetentionPolicy
from ..user_space.directory_paging import directory_page
from ..user_space.storage_lanes import StorageLaneRequest
from .task_call_inspector import TaskCallInspector
from .task_rounds import _parts, _segment_parts, _validate_frame
from .telemetry_ledger import PARTS, TELEMETRY_VOLUME_MAX_BYTES, _lock


def _policy(home, max_bytes=None):
    owner = Path(home.owner_home_dir)
    parts = Path(home.owner_retention_json).relative_to(owner).parts
    try:
        descriptor = open_readonly_file_beneath(owner, parts)
    except FileNotFoundError:
        return MemoryRetentionPolicy(), 0
    try:
        size = os.fstat(descriptor).st_size
        if size > _OWNER_INDEX_MAX_ROW_BYTES:
            raise ValueError('retention policy exceeds row byte budget')
        if max_bytes is not None and size > max_bytes:
            raise ValueError('retention policy exceeds remaining byte budget')
        raw = os.pread(descriptor, size + 1, 0)
        return MemoryRetentionPolicy.from_payload(json.loads(raw)), len(raw)
    finally:
        os.close(descriptor)


def _canonical_terminal(home, row):
    """准确运行身份缺失时保留；绝不以诊断终态代替规范执行证明。"""
    descriptor = -1
    try:
        from ..runtime_db.repository import RuntimeRepository
        from ..runtime_db.schema import runtime_db_path

        owner = Path(home.owner_home_dir)
        path = runtime_db_path(owner)
        descriptor = open_readonly_file_beneath(owner, path.relative_to(owner).parts)
        identity = os.fstat(descriptor)
        repo = RuntimeRepository(path, read_only=True, existing_only=True)
        root = str(row.get('root_run_id') or '')
        if not root:
            return False
        agent_run = repo.agent_run_for_run_id(root)
        if agent_run is None:
            return False
        task_run_id = agent_run['task_run_id']
        with repo._runtime_connection() as connection:
            if connection.execute('SELECT 1 FROM task_runs WHERE task_id=? AND closed_at=0 LIMIT 1',
                                  (row['task_id'],)).fetchone() is not None:
                return False
        task_run = repo.get_task_run(task_run_id)
        if task_run is None or task_run['task_id'] != row['task_id'] or not task_run['closed_at']:
            return False
        task = repo.get_task(row['task_id'])
        if task is None or task['owner_id'] != row.get('owner_id'):
            return False
        preview = repo.preview_task_run_agent_tree(task_run_id=task_run_id)
        current = os.stat(path, follow_symlinks=False)
        return (preview.get('eligible') is True and current.st_nlink == 1
                and (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino))
    except (ImportError, AttributeError, KeyError, OSError, ValueError, RuntimeError, sqlite3.Error):
        return False
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _advance(request, owner, domain, parts, *, validator=None, remaining_bytes):
    """每次最多解析一条且共享剩余字节/时间，不启动第二套预算或循环。"""
    def operation():
        index = OwnerReadIndex(owner)
        remaining_ms = request.budget.remaining_ms()
        max_ms = 1500 if math.isinf(remaining_ms) else max(1, int(remaining_ms))
        view = index.advance(domain, parts, validator=validator, max_rows=1,
                             max_bytes=remaining_bytes, max_ms=max_ms)
        return view, index._last_advance_work
    return request.run_write(operation)


def maintain_task_call_telemetry_lane(request: StorageLaneRequest) -> dict:
    """每个目录项一拍，游标由原 maintenance.json 发布；未知只保护相应任务。"""
    cursor = dict(request.cursor)
    result = dict(cursor=cursor, complete=False, items=0, bytes=0, protected=0, deleted_items=0, freed_bytes=0)
    if request.budget.exhausted():
        result['deferred_reason'] = 'budget'
        return result
    try:
        policy, cost = _policy(request.home, request.budget.max_bytes)
        result['bytes'] += cost
    except (OSError, ValueError, TypeError):
        result['deferred_reason'] = 'retention_policy_unknown'
        return result
    if not policy.maintenance_enabled:
        result.update(complete=True, deferred_reason='maintenance_disabled')
        return result
    if request.legal_hold or policy.legal_hold:
        result.update(complete=True, deferred_reason='legal_hold')
        return result
    owner = Path(request.home.owner_home_dir)
    if cursor.get('complete'):
        cursor.clear()
    phase = cursor.get('phase', 'tasks')
    if phase not in {'tasks', 'tools'}:
        raise ValueError('invalid task telemetry maintenance phase')
    if 'repair_summaries' not in cursor:
        try:
            descriptor = open_readonly_file_beneath(owner, (*PARTS, 'task_calls_index.jsonl'))
        except FileNotFoundError:
            cursor['repair_summaries'] = True
        else:
            os.close(descriptor)
            cursor['repair_summaries'] = False
    parts = (*PARTS, 'task_calls') if phase == 'tasks' else PARTS
    names, position, done = directory_page(owner, parts, cursor.get('scan', {}), 1)
    result['items'] = position.pop('last_scanned', len(names))
    cursor.update(phase=phase, scan=position)
    if not names:
        if done and phase == 'tasks':
            cursor.update(phase='tools', scan={})
        elif done:
            cursor['complete'] = True
            result['complete'] = True
        return result
    name = names[0]
    if phase == 'tools':
        from .telemetry_ledger import _SEALED
        summary = re.fullmatch(r'task_calls_index\.(\d{19})\.jsonl', name)
        if summary:
            if (policy.audit_days > 0 and not policy.legal_hold_task_ids
                    and int(summary[1]) / 1e9 < request.now - policy.audit_days * 86400):
                def retire_summary():
                    with _lock(owner):
                        remaining = None if request.budget.max_bytes is None else max(0, request.budget.max_bytes - result['bytes'])
                        current_policy, cost = _policy(request.home, remaining)
                        result['bytes'] += cost
                        if current_policy.legal_hold or current_policy.legal_hold_task_ids or current_policy.audit_days == 0:
                            return
                        try:
                            unlink_file_beneath(owner, (*PARTS, name))
                        except FileNotFoundError:
                            return
                        result['deleted_items'] += 1
                try:
                    request.run_write(retire_summary)
                except (OSError, ValueError):
                    result['deferred_reason'] = 'summary_retention_unavailable' 
            return result
        if name != 'tool_calls.jsonl' and _SEALED.fullmatch(name) is None:
            return result
        source = (*PARTS, name)
        domain, validator = 'telemetry', None
    elif name.endswith('.rounds.jsonl') or re.fullmatch(r'.+\.rounds\.[0-9a-f]{64}\.jsonl', name):
        source, domain, validator = (*parts, name), 'task_rounds', _validate_frame
    elif name.endswith('.json'):
        source, domain, validator = (*parts, name), None, None
    else:
        return result
    descriptor = -1
    try:
        descriptor = open_readonly_file_beneath(owner, source)
        size = os.fstat(descriptor).st_size
        allowance = None if request.budget.max_bytes is None else request.budget.max_bytes - result['bytes']
        if allowance is not None and (allowance <= 0 or domain is None and size > allowance):
            # 保留本项续点，不能因预算不足跳过它。
            cursor['scan'] = dict(request.cursor.get('scan', {}))
            result['deferred_reason'] = 'byte_budget'
            return result
        if domain:
            if not request.budget.exhausted(result['items'], result['bytes']):
                remaining = min(size, allowance if allowance is not None else size)
                view, work = _advance(request, owner, domain, source, validator=validator, remaining_bytes=max(1, remaining))
                result['bytes'] += work['bytes']
                result['items'] += work['items']
                if view['status'] == 'partial':
                    cursor['scan'] = dict(request.cursor.get('scan', {}))
                    result['deferred_reason'] = 'projection_partial'
            return result
        if size > _OWNER_INDEX_MAX_ROW_BYTES:
            result['protected'] += 1
            result['deferred_reason'] = 'task_metadata_size_limit'
            return result
        raw = os.pread(descriptor, size + 1, 0)
        result['bytes'] += len(raw)
        row = json.loads(raw)
        task_id = name[:-5]
        if row.get('task_id') != task_id or row.get('schema') != 'task_call_telemetry.v1':
            raise ValueError('task metadata identity mismatch')
        if cursor.get('repair_summaries'):
            def repair():
                with _lock(owner):
                    remaining = None if request.budget.max_bytes is None else max(0, request.budget.max_bytes - result['bytes'])
                    current = TaskCallInspector(owner, owner_id=row.get('owner_id', ''))._read(task_id, _max_bytes=remaining)
                    if current is not None:
                        result['bytes'] += len(json.dumps(current, ensure_ascii=False, allow_nan=False, sort_keys=True).encode()) + 1
                    if current is not None:
                        TaskCallInspector(owner, owner_id=row.get('owner_id', ''))._write_index(current)
            request.run_write(repair)
        terminal = row.get('status') in {'succeeded', 'failed', 'cancelled'}
        ended = row.get('ended_at')
        due = (policy.audit_days > 0 and type(ended) in (int, float)
               and ended < request.now - policy.audit_days * 86400)
        if (not terminal or not due or task_id in policy.legal_hold_task_ids):
            result['protected'] += 1
            return result
        def retire():
            with _lock(owner):
                inspector = TaskCallInspector(owner, owner_id=row.get('owner_id', ''))
                remaining = None if request.budget.max_bytes is None else max(0, request.budget.max_bytes - result['bytes'])
                current = inspector._read(task_id, _max_bytes=remaining)
                if current is not None:
                    result['bytes'] += len(json.dumps(current, ensure_ascii=False, allow_nan=False, sort_keys=True).encode()) + 1
                remaining = None if request.budget.max_bytes is None else max(0, request.budget.max_bytes - result['bytes'])
                latest_policy, policy_bytes = _policy(request.home, remaining)
                result['bytes'] += policy_bytes
                if (latest_policy.legal_hold or task_id in latest_policy.legal_hold_task_ids
                        or latest_policy.audit_days == 0 or not latest_policy.maintenance_enabled):
                    return 0
                if current != row or not _canonical_terminal(request.home, current):
                    return 0
                targets = [_parts(task_id), *[_segment_parts(task_id, item['generation'])
                    for item in current.get('_rounds', {}).get('segments', [])]]
                # 原任务元数据保留；删除的仅是已封存的诊断正文，发布明确 retired coverage。
                position = current.get('_rounds', {}).get('retire_position', 0)
                if type(position) is not int or not 0 <= position <= len(targets):
                    return 0
                if position == len(targets):
                    return 0
                try:
                    unlink_file_beneath(owner, targets[position])
                except FileNotFoundError:
                    pass
                current['_rounds'] = {**current.get('_rounds', {}), 'retired': True,
                                      'retire_position': position + 1}
                inspector._save(current, sync_index=False, include_rounds=False)
                return 1
        count = request.run_write(retire)
        result['deleted_items'] += count
        result['protected'] += int(not count)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        result['protected'] += 1
        result['deferred_reason'] = 'task_telemetry_source_' + type(error).__name__
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return result


def rotate_task_summary(owner):
    """只封存当前摘要卷，不读历史；封卷后由原维护通道分片处理。"""
    parts = (*PARTS, 'task_calls_index.jsonl')
    try:
        descriptor = open_readonly_file_beneath(owner, parts)
    except FileNotFoundError:
        return
    try:
        size = os.fstat(descriptor).st_size
    finally:
        os.close(descriptor)
    if size < TELEMETRY_VOLUME_MAX_BYTES:
        return
    import time
    target = f'task_calls_index.{time.time_ns():019d}.jsonl'
    if os.rename in os.supports_dir_fd:
        directory = open_directory_beneath(owner, PARTS)
        try:
            os.rename(parts[-1], target, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            os.close(directory)
    else:
        from ..common.nofollow_fs import _portable_directory
        directory = _portable_directory(owner, PARTS, create=False)
        os.rename(directory / parts[-1], directory / target)
