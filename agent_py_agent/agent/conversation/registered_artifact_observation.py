"""原run登记产物的公开观察；派生索引只定位，原registry/TaskLink/运行归属共同核验。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from ..artifacts import registry_locator
from ..artifacts.registry import REGISTRY_RELATIVE_PATH, _record_from_payload
from ..artifacts.registry_reader import latest_records_from_lines
from ..common.nofollow_fs import open_readonly_file_beneath
from .artifact_access import check_artifact_access
from .business_contracts import BusinessExecutionContext
from .business_observability import (
    ObservationRecord,
    _observation_cached,
    _observation_task,
    _observation_thread,
)
from .task_artifact_observation import (
    _CONTENT_BYTES,
    _REGISTRY_BYTES,
    _error,
    _read_source,
    guard_artifact_content_budget,
)


def discover_registered(source):
    if source.store is None:
        return {'coverage': 'unknown', 'after': 0}
    try:
        report = registry_locator.discover(source.home, source.store, source.owner)
        if not isinstance(report, dict):
            return {'coverage': 'unknown', 'after': 0}
        if report['coverage'] == 'complete':
            # 尚未覆盖旧主任务registry时保持unknown；只有已证无主任务来源才能认定全局无漏。
            index = source.store.session_tasks._list_index()
            with index._connection() as db:
                ready = index._refresh(db)
                has_tasks = db.execute('SELECT 1 FROM entries LIMIT 1').fetchone() is not None
            if not ready or has_tasks:
                report = {**report, 'coverage': 'unknown', 'reason': 'LEGACY_MAIN_REGISTRY_COVERAGE_UNPROVEN'}
        return report
    except (OSError, ValueError, TypeError, sqlite3.Error, RuntimeError):
        return {'coverage': 'unknown', 'after': 0}


def registered_artifact(source, artifact_id, version_ref=None, *, discover=True, with_content=True, authority=None):
    try:
        located = registry_locator.lookup(source.home, artifact_id)
        if located is None and discover:
            discover_registered(source)
            located = registry_locator.lookup(source.home, artifact_id)
    except (OSError, ValueError, sqlite3.Error) as error:
        raise _error('HOST_NOT_READY', 'ARTIFACT_LOCATOR_UNAVAILABLE', '原登记定位索引无法核验；保留原产物，稍后重试有界查询。') from error
    if located is None:
        return None
    relative, run_id, task_id = located
    root = source.home / relative
    if root == source.home or not root.is_relative_to(source.home) or '..' in Path(relative).parts:
        raise _error('SPACE_SCOPE_MISMATCH', 'ARTIFACT_ROOT_OUTSIDE_OWNER', '核对原登记位置，不允许跨Owner读取。')
    if source.repo is None or source.store is None:
        raise _error('HOST_NOT_READY', 'ARTIFACT_RUNTIME_UNAVAILABLE', '恢复原运行归属与任务来源后重试。')
    def binding():
        with source.repo._runtime_connection() as db:
            return db.execute('SELECT t.owner_id,t.thread_id,tr.metadata_json,ar.run_id,ar.current_attempt_id AS attempt_id '
                'FROM agent_runs ar JOIN task_runs tr ON tr.task_run_id=ar.task_run_id '
                'JOIN tasks t ON t.task_id=tr.task_id WHERE ar.run_id=? AND t.owner_id=? LIMIT 2',
                (run_id, source.owner)).fetchall()
    row = _observation_cached((source, 'artifact_run', run_id), binding)
    if len(row) != 1:
        raise _error('HOST_NOT_READY', 'ARTIFACT_RUN_BINDING_UNAVAILABLE', '原登记run无法唯一关联当前Owner，禁止猜测归属。')
    row = row[0]
    meta = json.loads(row['metadata_json'])
    context = BusinessExecutionContext.from_dict(meta['business_context']) if meta.get('business_context') else None
    task = _observation_task(source.store, task_id)
    if (context is None or context.owner_id != source.owner or context.task_id != task_id
            or meta.get('session_task_ref') != task_id or task is None):
        raise _error('SPACE_SCOPE_MISMATCH', 'ARTIFACT_PARENT_TASK_MISMATCH', '原run和登记必须绑定同一规范父任务及业务范围。')
    for thread_id in (row['thread_id'], task.sender_thread_id, task.target_thread_id):
        if _observation_thread(source.store, thread_id).owner_id != source.owner:
            raise _error('SPACE_SCOPE_MISMATCH', 'ARTIFACT_OWNER_MISMATCH', '原运行和父任务线程必须属于当前Owner。')
    scope = ObservationRecord({}, source.owner, context.space_id, context.project_ref,
        task_id, row['thread_id'], run_id)
    if not check_artifact_access(scope, authority=authority):
        return None
    link, error = _observation_cached((source.store.tasks, 'artifact_link', row['thread_id'], run_id),
        lambda: source.store.tasks.link_report(row['thread_id'], run_id))
    if link is None and not error:
        # 主任务沿既有SessionTask关联，子run沿自己的关联；两者都必须精确同根。
        link, error = _observation_cached((source.store.tasks, 'artifact_link', task.target_thread_id, task_id),
            lambda: source.store.tasks.link_report(task.target_thread_id, task_id))
    if error or link is None or Path(link.task_path).absolute() != root:
        raise _error('HOST_NOT_READY', 'ARTIFACT_RUN_ROOT_UNAVAILABLE', '原run工作区关联与定位索引不一致；等待原索引自动重建。')
    def registry():
        raw = _read_source(source.home, (*Path(relative).parts, *REGISTRY_RELATIVE_PATH.parts),
            _REGISTRY_BYTES, 'ARTIFACT_REGISTRY_UNREADABLE')
        if raw is None or len(raw) > _REGISTRY_BYTES:
            raise _error('HOST_NOT_READY', 'ARTIFACT_REGISTRY_READ_BUDGET', '原登记簿缺失或超过单次核验预算；不能把截断记录当当前版本。')
        return latest_records_from_lines(root / REGISTRY_RELATIVE_PATH, raw.decode('utf-8').splitlines(), _record_from_payload)
    records, errors = _observation_cached((source, 'artifact_registry', relative), registry)
    record = records.get(artifact_id)
    if errors or record is None:
        raise _error('HOST_NOT_READY', 'ARTIFACT_REGISTRY_RECORD_UNAVAILABLE', '原登记无法确认；定位索引不作为登记权威。')
    if record.run_id != run_id or record.task_id != task_id:
        raise _error('STATE_CONFLICT', 'ARTIFACT_RUN_REGISTRY_DRIFTED', '登记run/task与原定位不同；核对正式登记，禁止盲目重试。')
    if version_ref is not None and version_ref != record.sha256:
        raise _error('OBSERVATION_NOT_FOUND', 'ARTIFACT_VERSION_NOT_FOUND', '该精确登记版本不存在；不回退当前版本。')
    path = Path(record.path)
    if not path.is_absolute() or not path.is_relative_to(root):
        raise _error('SPACE_SCOPE_MISMATCH', 'ARTIFACT_CONTENT_OUTSIDE_RUN', '原run登记内容必须位于其正式工作区，不扩大到Owner其它文件。')
    content, present = None, False
    if with_content:
        guard_artifact_content_budget(source.home, path, record.size_bytes, _CONTENT_BYTES)
        content = _read_source(source.home, path.relative_to(source.home).parts, _CONTENT_BYTES, 'ARTIFACT_CONTENT_UNREADABLE')
        present = content is not None
    else:
        try:
            descriptor = open_readonly_file_beneath(source.home, path.relative_to(source.home).parts)
        except FileNotFoundError:
            pass
        else:
            try:
                present = os.fstat(descriptor).st_size == record.size_bytes
            finally:
                os.close(descriptor)
    if content is not None and len(content) > _CONTENT_BYTES:
        raise _error('HOST_NOT_READY', 'ARTIFACT_CONTENT_READ_BUDGET', '内容超过单次核验预算，未声称已核验全量字节；请使用有界运输入口。')
    if content is not None and (len(content) != record.size_bytes
            or hashlib.sha256(content).hexdigest() != record.sha256):
        raise _error('STATE_CONFLICT', 'ARTIFACT_CONTENT_DRIFTED', '原文件与登记版本/hash不符，不把修改后字节当旧版本下载。')
    # 只有原登记及归属已重核后才修复派生定位；失败仍不覆盖原产物或伪造第二份登记。
    try:
        registry_locator.repair_record(root, record)
    except (OSError, ValueError, sqlite3.Error):
        pass
    value = {'artifact_id': artifact_id, 'version_ref': record.sha256, 'name': path.name,
        'mime_type': record.mime_type or 'application/octet-stream', 'size_bytes': record.size_bytes,
        'content_sha256': record.sha256, 'task_id': task_id, 'space_id': context.space_id,
        'available': record.status == 'ready' and present}
    facts = ObservationRecord(value, source.owner, context.space_id, context.project_ref,
        task_id, row['thread_id'], run_id)
    return facts, record, content
