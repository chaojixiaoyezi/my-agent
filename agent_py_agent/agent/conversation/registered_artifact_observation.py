"""原run登记产物的公开观察；派生索引只定位，原registry/TaskLink/运行归属共同核验。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..artifacts import registry_locator
from ..artifacts.registry import REGISTRY_RELATIVE_PATH, bounded_registry_report
from ..common.nofollow_fs import open_readonly_file_beneath
from ..user_space.home_layout import task_workspace_path
from .artifact_access import check_artifact_access
from .business_contracts import BusinessExecutionContext
from .business_observability import (
    ObservationRecord,
    _observation_cached,
    _observation_request_data,
    _observation_task,
    _observation_thread,
    observation_request,
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
        main = registry_locator.discover_main(source.home, source.store, source.owner, getattr(source, 'repo', None))
        coverage = ('partial' if 'partial' in (report['coverage'], main['coverage']) else
            'complete' if report['coverage'] == main['coverage'] == 'complete' else 'unknown')
        report = {**report, 'coverage': coverage, 'main_coverage': main['coverage']}
        return report
    except (OSError, ValueError, TypeError, sqlite3.Error, RuntimeError):
        return {'coverage': 'unknown', 'after': 0}


def _declared_output_root(source, root, run_id, task):
    """宿主配置和原任务时间决定输出位置；可写 manifest 只能复核，不能扩权。"""
    template = getattr(source, 'workspace_task_path_template', None)
    if not isinstance(template, str) or not template or task.created_at <= 0:
        raise _error('HOST_NOT_READY', 'ARTIFACT_OUTPUT_AUTHORITY_UNAVAILABLE',
            '宿主任务路径配置或原任务创建时间缺失；恢复权威配置后重试，不能用工作区JSON授权。')
    try:
        day = datetime.fromtimestamp(task.created_at, timezone.utc).date().isoformat()
        expected = task_workspace_path(source.home, template, date=day, task_name=task.task_id) / 'output'
        if (not expected.is_absolute() or '..' in expected.parts or expected == source.home
                or not expected.is_relative_to(source.home)):
            raise ValueError('canonical task output outside owner')
    except (ValueError, TypeError, KeyError, OverflowError, OSError) as error:
        raise _error('SPACE_SCOPE_MISMATCH', 'ARTIFACT_OUTPUT_AUTHORITY_INVALID',
            '宿主模板和原任务无法生成当前Owner内的规范输出目录；核对配置，不从manifest扩大权限。') from error
    raw = _read_source(source.home, (*root.relative_to(source.home).parts, 'work', 'run_workspace.json'),
        _REGISTRY_BYTES, 'ARTIFACT_WORKSPACE_CONTRACT_UNREADABLE')
    if raw is None:
        return None
    if len(raw) > _REGISTRY_BYTES:
        raise _error('HOST_NOT_READY', 'ARTIFACT_WORKSPACE_CONTRACT_READ_BUDGET',
            '原运行工作区合同超过核验预算；请检查原合同，不按截断内容授权。')
    try:
        value = json.loads(raw)
        if (value['schema_version'] != 'run_workspace.v1' or value['owner_id'] != source.owner
                or value['owner_home'] != str(source.home) or value['task_root'] != str(root)
                or value['run_id'] != run_id or value['task_id'] != task.task_id):
            raise ValueError('run workspace identity mismatch')
        contract = value.get('delivery_contract') or {}
        output = (contract.get('task_workspace') or {}).get('output_dir')
        if output is None:
            return None
        path = Path(output)
        if path != expected:
            raise ValueError('declared output differs from canonical task output')
        return expected
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise _error('SPACE_SCOPE_MISMATCH', 'ARTIFACT_WORKSPACE_CONTRACT_MISMATCH',
            '原工作区合同的schema、owner、run、task或输出路径不符；核对原合同，不扩大读取范围。') from error


def _unassigned_session_root(source, row, task, task_id):
    """只认原委派SessionTask和runtime祖先；无业务DTO不等于无来源，也不借目录认领。"""
    from types import SimpleNamespace

    from .channels import supports_transcript_delivery
    from .execution_threads import delegated_session_task
    from .host_actor import HostChannelActor, _validate_scope

    if (task is None or task.metadata.get('business_context') is not None
            or row['conversation_task_id'] != task_id or task.target_thread_id != row['thread_id']):
        return None
    try:
        if delegated_session_task(source.store, task.target_thread_id) != task:
            return None
        current, seen = row, set()
        with source.repo._runtime_connection() as db:
            for _ in range(64):
                if current['agent_run_id'] in seen:
                    raise ValueError('runtime ancestry cycle')
                seen.add(current['agent_run_id'])
                if (current['owner_id'] != source.owner or current['conversation_task_id'] != task_id
                        or current['task_run_id'] != row['task_run_id'] or current['thread_id'] != task.target_thread_id):
                    raise ValueError('runtime ancestry scope mismatch')
                if not current['parent_agent_run_id']:
                    break
                current = db.execute('SELECT ar.agent_run_id,ar.task_run_id,ar.run_id,ar.role,ar.parent_agent_run_id,'
                    't.owner_id,t.thread_id,t.conversation_task_id FROM agent_runs ar '
                    'JOIN task_runs tr ON tr.task_run_id=ar.task_run_id JOIN tasks t ON t.task_id=tr.task_id '
                    'WHERE ar.agent_run_id=?', (current['parent_agent_run_id'],)).fetchone()
                if current is None:
                    raise ValueError('runtime parent missing')
            else:
                raise ValueError('runtime ancestry budget')
        if current['role'] != 'main' or current['run_id'] != task_id:
            raise ValueError('runtime root mismatch')
        link, error = source.store.tasks.link_report(task.target_thread_id, task_id)
        if error or link is None or not link.task_path:
            raise ValueError('root task link missing')
        raw_actor = getattr(link, 'host_channel_actor', None)
        if raw_actor is not None:
            actor = HostChannelActor.from_record(raw_actor)
            if actor.owner_id != source.owner or actor.source_thread_id != task.sender_thread_id:
                raise ValueError('host actor task mismatch')
            adapter = SimpleNamespace(conversation_store=source.store,
                home_paths=SimpleNamespace(owner_id=source.owner, owner_kind='group' if '/groups/' in source.owner else 'user'))
            _validate_scope(adapter, task.target_thread_id, actor)
        else:
            sender = _observation_thread(source.store, task.sender_thread_id)
            if (not source.owner.startswith('local/')
                    or any(not supports_transcript_delivery(binding.channel) for binding in sender.channel_bindings)):
                raise ValueError('original host actor missing')
        root = Path(link.task_path).absolute()
        if root == source.home or not root.is_relative_to(source.home) or '..' in root.parts:
            raise ValueError('root task path outside owner')
        return root, current['run_id']
    except (ValueError, TypeError, KeyError, AttributeError, OSError, RuntimeError) as error:
        raise _error('SPACE_SCOPE_MISMATCH', 'ARTIFACT_SESSION_TASK_ORIGIN_MISMATCH',
            '原委派任务、发起人、runtime谱系或父根关联不一致；核对原宿主记录，不用工作区路径补权。') from error


@observation_request()
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
            return db.execute('SELECT t.owner_id,t.thread_id,t.conversation_task_id,tr.metadata_json,ar.agent_run_id,ar.task_run_id,ar.run_id,ar.role,ar.parent_agent_run_id,ar.current_attempt_id AS attempt_id '
                'FROM agent_runs ar JOIN task_runs tr ON tr.task_run_id=ar.task_run_id '
                'JOIN tasks t ON t.task_id=tr.task_id WHERE ar.run_id=? AND t.owner_id=? LIMIT 2',
                (run_id, source.owner)).fetchall()
    row = _observation_cached((source.owner, str(source.home), 'artifact_run', run_id), binding)
    if len(row) != 1:
        raise _error('HOST_NOT_READY', 'ARTIFACT_RUN_BINDING_UNAVAILABLE', '原登记run无法唯一关联当前Owner，禁止猜测归属。')
    row = row[0]
    meta = json.loads(row['metadata_json'])
    context = BusinessExecutionContext.from_dict(meta['business_context']) if meta.get('business_context') else None
    task = _observation_task(source.store, task_id)
    native_main = (task is None and row['role'] == 'main' and not row['parent_agent_run_id']
        and row['conversation_task_id'] == task_id
        and meta.get('session_task_ref') is None and meta.get('business_context') is None)
    session_root = (_unassigned_session_root(source, row, task, task_id)
                    if context is None and meta.get('session_task_ref') is None else None)
    if not native_main and session_root is None and (context is None or context.owner_id != source.owner or context.task_id != task_id
            or meta.get('session_task_ref') != task_id or task is None):
        raise _error('SPACE_SCOPE_MISMATCH', 'ARTIFACT_PARENT_TASK_MISMATCH', '原run和登记必须绑定同一规范父任务及业务范围。')
    thread_ids = (row['thread_id'],) if native_main else (row['thread_id'], task.sender_thread_id, task.target_thread_id)
    for thread_id in thread_ids:
        if _observation_thread(source.store, thread_id).owner_id != source.owner:
            raise _error('SPACE_SCOPE_MISMATCH', 'ARTIFACT_OWNER_MISMATCH', '原运行和父任务线程必须属于当前Owner。')
    space_id, project_ref = (context.space_id, context.project_ref) if context else (None, None)
    scope = ObservationRecord({}, source.owner, space_id, project_ref,
        task_id, row['thread_id'], run_id)
    if not check_artifact_access(scope, authority=authority):
        return None
    link, error = _observation_cached((source.store.tasks, 'artifact_link', row['thread_id'], run_id),
        lambda: source.store.tasks.link_report(row['thread_id'], run_id))
    if link is None and not error and task is not None:
        # 主任务沿既有SessionTask关联，子run沿自己的关联；两者都必须精确同根。
        link, error = _observation_cached((source.store.tasks, 'artifact_link', task.target_thread_id, task_id),
            lambda: source.store.tasks.link_report(task.target_thread_id, task_id))
    if native_main and link is not None:
        origin = getattr(link, 'host_user_origin', None)
        thread = _observation_thread(source.store, row['thread_id'])
        if origin is None and not thread.metadata.get('background_agent'):
            raise _error('HOST_NOT_READY', 'ARTIFACT_MAIN_ORIGIN_UNAVAILABLE',
                '主会话原始请求来源尚未由宿主结案固定；等待原运行结案后查询，不把暂缺证明当产物不存在。')
        if (not isinstance(origin, dict) or origin.get('owner_id') != source.owner
                or origin.get('thread_id') != row['thread_id'] or origin.get('task_id') != task_id
                or origin.get('request_id') != run_id or thread.metadata.get('background_agent')):
            raise _error('SPACE_SCOPE_MISMATCH', 'ARTIFACT_MAIN_ORIGIN_MISMATCH',
                '主会话产物必须核对宿主原始用户请求、线程、任务和运行；不借无上下文后台记录扩大范围。')
    if error or link is None or Path(link.task_path).absolute() != root:
        raise _error('HOST_NOT_READY', 'ARTIFACT_RUN_ROOT_UNAVAILABLE', '原run工作区关联与定位索引不一致；等待原索引自动重建。')
    def registry():
        budget = _observation_request_data.get().registry_budget
        limits = budget.limits(_REGISTRY_BYTES, 8192, 8)

        def unavailable(reason):
            error = _error('HOST_NOT_READY', reason,
                '原登记未完整核验，仍为partial。保留原artifact_id/version_ref，检查原登记后再读；不要当作不存在或重做覆盖。')
            error.details['coverage'] = 'partial'
            return error

        if limits is None:
            raise unavailable(budget.exhausted_reason)
        limit, rows, diagnostics = limits
        descriptor = -1
        try:
            descriptor = open_readonly_file_beneath(source.home, (*Path(relative).parts, *REGISTRY_RELATIVE_PATH.parts))
            before = os.fstat(descriptor)
            if before.st_size > limit:
                raise unavailable('ARTIFACT_REGISTRY_READ_BUDGET_EXCEEDED')
            raw = bytearray()
            while len(raw) <= limit:
                chunk = os.read(descriptor, min(65536, limit + 1 - len(raw)))
                budget.remaining -= len(chunk)
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > limit:
                raise unavailable('ARTIFACT_REGISTRY_READ_BUDGET_EXCEEDED')
            after = os.fstat(descriptor)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink')
            if len(raw) != before.st_size or any(getattr(before, key) != getattr(after, key) for key in fields):
                raise unavailable('ARTIFACT_REGISTRY_SNAPSHOT_CHANGED')
        except FileNotFoundError:
            raise unavailable('ARTIFACT_REGISTRY_MISSING') from None
        except OSError as error:
            problem = unavailable('ARTIFACT_REGISTRY_UNREADABLE')
            problem.details['exception_type'] = type(error).__name__
            raise problem from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        report = bounded_registry_report(bytes(raw), max_rows=rows, max_diagnostics=diagnostics)
        budget.observe(report)
        if report.errors:
            reason = report.errors[-1].get('error_code', 'ARTIFACT_REGISTRY_UNREADABLE')
            raise unavailable(reason)
        return report.records, []
    records, errors = _observation_cached((source.owner, str(source.home), 'artifact_registry', relative), registry)
    record = records.get(artifact_id)
    if errors or record is None:
        raise _error('HOST_NOT_READY', 'ARTIFACT_REGISTRY_RECORD_UNAVAILABLE', '原登记无法确认；定位索引不作为登记权威。')
    if record.run_id != run_id or record.task_id != task_id:
        raise _error('STATE_CONFLICT', 'ARTIFACT_RUN_REGISTRY_DRIFTED', '登记run/task与原定位不同；核对正式登记，禁止盲目重试。')
    if version_ref is not None and version_ref != record.sha256:
        raise _error('OBSERVATION_NOT_FOUND', 'ARTIFACT_VERSION_NOT_FOUND', '该精确登记版本不存在；不回退当前版本。')
    path = Path(record.path)
    output_root = None
    if path.is_absolute() and not path.is_relative_to(root):
        output_root = (_declared_output_root(source, session_root[0], session_root[1], task) if session_root
                       else _declared_output_root(source, root, run_id, link if native_main else task))
    if (not path.is_absolute() or '..' in path.parts
            or not (path.is_relative_to(root) or output_root is not None and path.is_relative_to(output_root))):
        raise _error('SPACE_SCOPE_MISMATCH', 'ARTIFACT_CONTENT_OUTSIDE_RUN', '原run登记内容必须位于其正式工作区，不扩大到Owner其它文件。')
    if output_root is not None and task is not None:
        pins = task.metadata.get('result_artifacts')
        if task.result_refs and record.path not in task.result_refs:
            raise _error('STATE_CONFLICT', 'ARTIFACT_TASK_PIN_MISMATCH',
                '登记路径不在原任务冻结result_refs内；核对原任务，不能由工作区manifest追加结果。')
        if pins is not None:
            matches = [pin for pin in pins if isinstance(pin, dict) and pin.get('artifact_id') == artifact_id] if isinstance(pins, list) else []
            if (len(matches) != 1 or matches[0].get('path') != record.path
                    or matches[0].get('sha256') != record.sha256
                    or matches[0].get('size_bytes') != record.size_bytes or record.path not in task.result_refs):
                raise _error('STATE_CONFLICT', 'ARTIFACT_TASK_PIN_MISMATCH',
                    '原任务冻结的产物ID、路径、hash或result_refs与登记不符；核对原任务，不用可写manifest替换冻结结果。')
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
        'content_sha256': record.sha256, 'task_id': task_id, 'space_id': space_id,
        'available': record.status == 'ready' and present}
    facts = ObservationRecord(value, source.owner, space_id, project_ref,
        task_id, row['thread_id'], run_id)
    return facts, record, content
