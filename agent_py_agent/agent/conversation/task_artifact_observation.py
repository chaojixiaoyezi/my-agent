"""原SessionTask索引只定位；TaskLink与任务registry仍共同证明产物身份。"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

from ..artifacts.registry import REGISTRY_RELATIVE_PATH, _record_from_payload
from ..artifacts.registry_reader import latest_records_from_lines
from ..common.nofollow_fs import read_bytes_beneath
from .business_contracts import BusinessExecutionContext
from .business_observability import ObservationSourceError
from .session_tasks import SessionTask

_REGISTRY_BYTES = 2 * 1024 * 1024
_CONTENT_BYTES = 8 * 1024 * 1024


def _error(code, reason, next_action):
    return ObservationSourceError(code, details={'reason': reason, 'side_effect_state': 'none',
                                                 'next_action': next_action})


def _read_source(root, parts, budget, reason):
    try:
        return read_bytes_beneath(root, parts, max_bytes=budget + 1, snapshot_retries=1)
    except (OSError, ValueError, UnicodeError) as error:
        raise _error('HOST_NOT_READY', reason, '原只读来源无法安全核验；检查文件及目录完整性后重试。') from error


def task_artifact(store, owner_home, owner_id, artifact_id, version_ref=None):
    """返回(found, artifact, task, context)；未追平不是不存在，冲突不选第一条。"""
    if store is None or not hasattr(store, 'session_tasks'):
        return False, None, None, None
    try:
        page = store.session_tasks._list_index().artifact_tasks(artifact_id)
    except (OSError, ValueError, sqlite3.Error) as error:
        raise _error('HOST_NOT_READY', 'ARTIFACT_TASK_INDEX_UNAVAILABLE', '原任务定位索引无法读取；允许派生索引重建后重试。') from error
    candidates = []
    for raw in page['items']:
        try:
            task = SessionTask.from_dict(raw)
            target, sender = store.threads.require(task.target_thread_id), store.threads.require(task.sender_thread_id)
            context = BusinessExecutionContext.from_dict(task.metadata['business_context']) if task.metadata.get('business_context') else None
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise _error('HOST_NOT_READY', 'ARTIFACT_TASK_SOURCE_UNREADABLE', '原任务、线程或冻结归属无法核验；检查原记录后重试。') from error
        if (target.owner_id != owner_id or sender.owner_id != owner_id
                or context is not None and (context.owner_id != owner_id or context.task_id != task.task_id)):
            raise _error('SPACE_SCOPE_MISMATCH', 'ARTIFACT_TASK_OWNER_MISMATCH', '核对原任务和线程的Owner归属。')
        candidates.append((task, context))
    if len(candidates) > 1 or page.get('truncated'):
        raise _error('STATE_CONFLICT', 'ARTIFACT_TASK_ID_CONFLICT', '同一产物ID关联多个任务；核对原登记，禁止猜测归属。')
    if page['coverage'] != 'complete' or page['load_errors']:
        raise _error('HOST_NOT_READY', 'ARTIFACT_TASK_INDEX_INCOMPLETE', '原任务定位索引仍在有界补建或存在坏记录；稍后重试同一查询。')
    if not candidates:
        return False, None, None, None
    task, context = candidates[0]
    try:
        link, error = store.tasks.link_report(task.target_thread_id, task.task_id)
    except (OSError, ValueError, KeyError) as error:
        raise _error('HOST_NOT_READY', 'ARTIFACT_TASK_LINK_UNAVAILABLE', '原任务工作区关联无法读取；修复后重试。') from error
    if error or link is None or not link.task_path:
        raise _error('HOST_NOT_READY', 'ARTIFACT_TASK_LINK_UNAVAILABLE', '检查原任务工作区关联后重试，不使用其它任务目录。')
    root, owner = Path(link.task_path).resolve(), Path(owner_home).resolve()
    if root == owner or not root.is_relative_to(owner):
        raise _error('SPACE_SCOPE_MISMATCH', 'ARTIFACT_TASK_ROOT_OUTSIDE_OWNER', '原TaskLink工作区必须位于当前Owner目录内。')
    pins = [item for item in task.metadata.get('result_artifacts', [])
            if isinstance(item, dict) and item.get('artifact_id') == artifact_id and item.get('path') in task.result_refs]
    if len(pins) != 1:
        raise _error('STATE_CONFLICT', 'ARTIFACT_TASK_PIN_CONFLICT', '核对原任务结果路径和唯一产物版本绑定。')
    pin = pins[0]
    raw = _read_source(root, tuple(REGISTRY_RELATIVE_PATH.parts), _REGISTRY_BYTES, 'ARTIFACT_REGISTRY_UNREADABLE')
    if raw is None:
        raise _error('HOST_NOT_READY', 'ARTIFACT_REGISTRY_UNAVAILABLE', '恢复原任务产物登记簿后重试。')
    if len(raw) > _REGISTRY_BYTES:
        raise _error('HOST_NOT_READY', 'ARTIFACT_REGISTRY_READ_BUDGET', '当前任务登记簿超过单次只读预算，不能把截断记录当作最新版本。')
    try:
        records, errors = latest_records_from_lines(root / REGISTRY_RELATIVE_PATH, raw.decode('utf-8').splitlines(), _record_from_payload)
    except (OSError, ValueError, TypeError, UnicodeError) as error:
        raise _error('HOST_NOT_READY', 'ARTIFACT_REGISTRY_UNREADABLE', '原登记簿无法解析，修复后重试。') from error
    record = records.get(artifact_id)
    if errors:
        raise _error('HOST_NOT_READY', 'ARTIFACT_REGISTRY_UNREADABLE', '修复原登记簿坏记录后重试。')
    record_path = Path(record.path) if record is not None else Path('.')
    record_path = Path(os.path.abspath(record_path if record_path.is_absolute() else root / record_path))
    pin_path = Path(pin.get('path', ''))
    pin_path = Path(os.path.abspath(pin_path if pin_path.is_absolute() else root / pin_path))
    if (record is None or record.sha256 != pin.get('sha256') or record_path != pin_path
            or record.task_id != task.task_id):
        raise _error('STATE_CONFLICT', 'ARTIFACT_REGISTRY_PIN_DRIFTED', '原登记簿与任务冻结的ID/路径/版本不一致；核对原登记，禁止回退其它版本。')
    if version_ref is not None and version_ref != record.sha256:
        return True, None, task, context
    path = record_path
    if not path.is_absolute() or not path.is_relative_to(owner):
        raise _error('SPACE_SCOPE_MISMATCH', 'ARTIFACT_CONTENT_OUTSIDE_OWNER', '准确登记的任务交付物必须位于当前Owner目录内。')
    content = _read_source(owner, tuple(path.relative_to(owner).parts), _CONTENT_BYTES, 'ARTIFACT_CONTENT_UNREADABLE')
    if content is not None and len(content) > _CONTENT_BYTES:
        raise _error('HOST_NOT_READY', 'ARTIFACT_CONTENT_READ_BUDGET', '内容超过单次核验预算；当前接口不能声称已核验完整内容。')
    if content is not None and (len(content) != record.size_bytes or hashlib.sha256(content).hexdigest() != record.sha256):
        raise _error('STATE_CONFLICT', 'ARTIFACT_CONTENT_DRIFTED', '当前文件与冻结产物版本不同，禁止把新内容当旧版。')
    value = {'artifact_id': artifact_id, 'version_ref': record.sha256, 'name': path.name,
             'mime_type': record.mime_type or 'application/octet-stream', 'size_bytes': record.size_bytes,
             'content_sha256': record.sha256, 'task_id': task.task_id,
             'space_id': context.space_id if context else None, 'available': record.status == 'ready' and content is not None}
    return True, value, task, context
