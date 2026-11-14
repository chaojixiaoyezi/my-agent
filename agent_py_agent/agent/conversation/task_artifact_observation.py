"""原SessionTask索引只定位；TaskLink与任务registry仍共同证明产物身份。"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

from ..artifacts.registry import REGISTRY_RELATIVE_PATH, _record_from_payload
from ..artifacts.registry_reader import latest_records_from_lines
from ..common.nofollow_fs import open_readonly_file_beneath, read_bytes_beneath
from .artifact_access import check_artifact_access
from .business_contracts import BusinessExecutionContext
from .business_observability import ObservationRecord, ObservationSourceError
from .session_tasks import SessionTask

# 验证原任务产物登记时读取的最大登记簿字节数。
_REGISTRY_BYTES = 2 * 1024 * 1024
# 交互观察一次能读回并校验的最大产物字节数。
_CONTENT_BYTES = 8 * 1024 * 1024


def _error(code, reason, next_action):
    return ObservationSourceError(code, details={'reason': reason, 'side_effect_state': 'none',
                                                 'next_action': next_action})


def _read_source(root, parts, budget, reason):
    try:
        return read_bytes_beneath(root, parts, max_bytes=budget + 1, snapshot_retries=1)
    except FileNotFoundError:
        # 中间目录和叶缺失同为本次精确路径不存在；权限/链接错误仍保留未知。
        return None
    except (OSError, ValueError, UnicodeError) as error:
        raise _error('HOST_NOT_READY', reason, '原只读来源无法安全核验；检查文件及目录完整性后重试。') from error



def guard_artifact_content_budget(owner, path, declared_size, budget, *, exact_size=False):
    """先核预算元数据，不把超大文件的截断/拒读误记为hash漂移。"""
    if declared_size > budget:
        raise _error('HOST_NOT_READY', 'ARTIFACT_CONTENT_READ_BUDGET', '原登记尺寸超过单次读取预算；未读取完整内容，不能声称hash已核验。')
    try:
        descriptor = open_readonly_file_beneath(owner, Path(path).relative_to(owner).parts)
    except FileNotFoundError:
        return
    try:
        actual_size = os.fstat(descriptor).st_size
        if exact_size and actual_size != declared_size:
            raise _error('STATE_CONFLICT', 'ARTIFACT_CONTENT_DRIFTED', '当前文件尺寸与冻结清单不符；未返回内容，核对原版本。')
        if actual_size > budget:
            raise _error('HOST_NOT_READY', 'ARTIFACT_CONTENT_READ_BUDGET', '当前文件尺寸超过单次读取预算；未读取完整内容，不把预算拒绝记作漂移。')
    finally:
        os.close(descriptor)


def verify_artifact_content(owner, path, declared_size, expected_sha256, budget):
    """元数据只需完整hash；同一nofollow句柄分块核验，不在内存保留正文。"""
    from ..plugin_host.artifacts import _fingerprint

    guard_artifact_content_budget(owner, path, declared_size, budget, exact_size=True)
    try:
        descriptor = open_readonly_file_beneath(owner, Path(path).relative_to(owner).parts)
    except FileNotFoundError:
        return False
    try:
        before, digest, size = os.fstat(descriptor), hashlib.sha256(), 0
        if before.st_size != declared_size:
            raise _error('STATE_CONFLICT', 'ARTIFACT_CONTENT_DRIFTED', '当前归档尺寸与原清单不符；重新核对原版本，勿盲目重试。')
        while chunk := os.read(descriptor, 65536):
            size += len(chunk)
            if size > declared_size:
                raise _error('STATE_CONFLICT', 'ARTIFACT_CONTENT_DRIFTED', '归档在核验期间增长；未返回内容，核对原版本。')
            digest.update(chunk)
        if (size != declared_size or digest.hexdigest() != expected_sha256
                or _fingerprint(before) != _fingerprint(os.fstat(descriptor))):
            raise _error('STATE_CONFLICT', 'ARTIFACT_CONTENT_DRIFTED', '归档hash或身份在核验期间变更；未返回内容，核对原版本。')
        return True
    finally:
        os.close(descriptor)

def task_artifact(store, owner_home, owner_id, artifact_id, version_ref=None, *, authority=None):
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
        facts = ObservationRecord({}, owner_id, context.space_id if context else None,
            context.project_ref if context else None, task.task_id, task.target_thread_id)
        if not check_artifact_access(facts, authority=authority):
            continue
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
    guard_artifact_content_budget(owner, path, record.size_bytes, _CONTENT_BYTES)
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
