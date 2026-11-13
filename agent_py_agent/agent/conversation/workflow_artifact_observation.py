"""正规工作流RUN_OUT的精确公开读取；ID只定位，原运行与步骤清单才是来源。"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import sqlite3
from pathlib import Path, PurePosixPath

from ..artifacts.registry import ArtifactRegistryRecord
from ..common.nofollow_fs import open_readonly_file_beneath
from ..workflows.run_list_index import WorkflowArtifactLocatorIncomplete
from ..workflows.run_store import WorkflowRunStore
from .artifact_access import check_artifact_access
from .business_access import BusinessError
from .business_contracts import BusinessExecutionContext
from .business_observability import ObservationRecord, _observation_task, _observation_thread
from .task_artifact_observation import (
    _error,
    _read_source,
    guard_artifact_content_budget,
    verify_artifact_content,
)

_SOURCE_BYTES = 8 * 1024 * 1024
# 与正式RUN_OUT生产上限一致；不改变普通任务产物或上传预算。
_WORKFLOW_CONTENT_BYTES = 64 * 1024 * 1024


def _source(owner, parts):
    raw = _read_source(owner, parts, _SOURCE_BYTES, 'WORKFLOW_ARTIFACT_SOURCE_UNREADABLE')
    if raw is None:
        raise _error('OBSERVATION_NOT_FOUND', 'WORKFLOW_ARTIFACT_NOT_FOUND', '该正式运行或步骤不存在；不读取其它目录或猜测路径。')
    if len(raw) > _SOURCE_BYTES:
        raise _error('HOST_NOT_READY', 'WORKFLOW_ARTIFACT_SOURCE_READ_BUDGET', '原运行清单超过只读预算；未读取完整清单，不声称产物不存在。')
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('object required')
        return value
    except (ValueError, TypeError, UnicodeError) as error:
        raise _error('HOST_NOT_READY', 'WORKFLOW_ARTIFACT_SOURCE_UNREADABLE', '原运行清单无法解析；恢复原记录后再查询。') from error



def _attempt_candidates(steps, attempt_id):
    """只消费登记的步骤/批attempt；跨步骤或跨批重号必须拒绝，绝不选第一项。"""
    matches, batch_count = [], 0
    for step in steps:
        if not isinstance(step, dict):
            continue
        if isinstance(step.get('escalation_result'), dict) and step['escalation_result'].get('attempt_id') == attempt_id:
            matches.append((step, step))
        batches = step.get('batches', [])
        if not isinstance(batches, list):
            raise _error('HOST_NOT_READY', 'WORKFLOW_ARTIFACT_BATCH_SOURCE_INVALID', '原逐批账本无效；不能推断产物不存在。')
        batch_count += len(batches)
        if batch_count > 4096:
            raise _error('HOST_NOT_READY', 'WORKFLOW_ARTIFACT_BATCH_READ_BUDGET', '原逐批账本超出本次定位预算；未选择文件，不声称不存在。')
        for batch in batches:
            if isinstance(batch, dict) and batch.get('attempt_id') == attempt_id:
                matches.append((step, {**batch, 'step_id': step.get('step_id'), 'ended_at': batch.get('finished_at')}))
    return matches

def workflow_artifact(source, artifact_id, version_ref=None, *, with_content=True, authority=None):
    """同一解析器供元数据与下载使用，不登记新账、不扫描workflow历史。"""
    if not artifact_id.startswith('wfart-'):
        return None
    from ..workflows.run_artifacts import (
        parse_workflow_artifact_id,
        workflow_artifact_id,
        workflow_artifact_run_ids,
    )

    located = parse_workflow_artifact_id(artifact_id)
    if located is None:
        raise _error('OBSERVATION_NOT_FOUND', 'WORKFLOW_ARTIFACT_ID_INVALID', '产物编号格式无效；使用原步骤返回的完整编号。')
    run_key, attempt_id, _ = located
    try:
        run_ids = workflow_artifact_run_ids(source.home, run_key)
    except WorkflowArtifactLocatorIncomplete as error:
        raise _error('HOST_NOT_READY', error.error_code,
            '旧运行索引尚未覆盖此编号，文件状态未知；重查同一编号或等待运行列表维护推进。') from error
    except (OSError, ValueError, TypeError, sqlite3.Error) as error:
        raise _error('HOST_NOT_READY', 'WORKFLOW_ARTIFACT_RUN_LOCATOR_UNAVAILABLE', '原运行定位无法核验；未扫描历史或读取内容，恢复原索引后重试。') from error
    if not isinstance(run_ids, tuple) or any(not isinstance(value, str) or not value for value in run_ids):
        raise _error('HOST_NOT_READY', 'WORKFLOW_ARTIFACT_RUN_LOCATOR_INVALID', '原运行定位形状无效；未读取文件内容。')
    if len(run_ids) > 1:
        raise _error('HOST_NOT_READY', 'WORKFLOW_ARTIFACT_RUN_LOCATOR_CONFLICT', '编号对应多个原运行候选；禁止选择第一项，先修复原定位索引。')
    if not run_ids:
        raise _error('OBSERVATION_NOT_FOUND', 'WORKFLOW_ARTIFACT_RUN_NOT_FOUND', '产物编号没有原运行定位；不按其它任务或目录猜测。')
    run_id = run_ids[0]
    if source.store is None:
        raise _error('HOST_NOT_READY', 'WORKFLOW_ARTIFACT_TASK_SOURCE_UNAVAILABLE', '原任务与授权来源尚未就绪；不能按编号直接导出文件。')
    owner = Path(source.home)
    # 复用原Store规范定位；load_run持写锁且读全部步骤，公开点读只读取指定run及候选step。
    try:
        runs = WorkflowRunStore(owner)
        runs._folder(run_id)
    except (OSError, ValueError, TypeError) as error:
        raise _error('HOST_NOT_READY', 'WORKFLOW_ARTIFACT_RUN_LOCATOR_INVALID', '原运行位置无法安全核验；未读取内容，不能跳转其它位置。') from error
    run_parts = ('data', 'workflow_runs', run_id, 'run.json')
    run = _source(owner, run_parts)
    if run.get('workflow_run_id') != run_id or run.get('owner_id') != source.owner:
        raise _error('OBSERVATION_NOT_FOUND', 'WORKFLOW_ARTIFACT_OWNER_RUN_MISMATCH', '原运行不属于当前Owner或编号不匹配。')
    if run.get('business_context') is None or run.get('business_context') == {}:
        raise _error('OBSERVATION_NOT_FOUND', 'WORKFLOW_ARTIFACT_TASK_SCOPE_MISMATCH', '原运行没有业务订单归属；不可作为业务任务交付物。')
    try:
        context = BusinessExecutionContext.from_dict(run.get('business_context'))
    except (ValueError, TypeError) as error:
        raise _error('HOST_NOT_READY', 'WORKFLOW_ARTIFACT_CONTEXT_UNREADABLE', '原运行冻结业务上下文无效；未推断其它任务或读取内容。') from error
    try:
        task = _observation_task(source.store, context.task_id)
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise _error('HOST_NOT_READY', 'WORKFLOW_ARTIFACT_TASK_SOURCE_UNREADABLE', '原规范任务无法读取；覆盖未知，恢复原记录后重试。') from error
    if (context.owner_id != source.owner or task is None or task.task_id != context.task_id
            or task.metadata.get('business_context') != context.to_dict()):
        raise _error('OBSERVATION_NOT_FOUND', 'WORKFLOW_ARTIFACT_TASK_SCOPE_MISMATCH', '原冻结上下文未绑定同一规范业务任务；不将其它任务或纯运行级上下文冒作订单。')
    try:
        threads = [_observation_thread(source.store, identifier)
            for identifier in (task.sender_thread_id, task.target_thread_id)]
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise _error('HOST_NOT_READY', 'WORKFLOW_ARTIFACT_THREAD_SOURCE_UNREADABLE', '原发送与接收线程无法核验；未读取内容，恢复原线程记录后重试。') from error
    if any(thread.owner_id != source.owner for thread in threads):
        raise _error('OBSERVATION_NOT_FOUND', 'WORKFLOW_ARTIFACT_THREAD_OWNER_MISMATCH', '原发送或接收线程属于其它Owner；禁止导出。')
    facts = ObservationRecord({}, source.owner, context.space_id, context.project_ref,
        task.task_id, task.target_thread_id, run_id, attempt_id)
    if not check_artifact_access(facts, authority=authority):
        return None
    if run.get('ephemeral_debug') is not False:
        raise _error('OBSERVATION_NOT_FOUND', 'WORKFLOW_ARTIFACT_NOT_FORMAL', '只提供正规工作流归档，不将调试临时文件当作正式产物。')
    steps = run.get('steps')
    if not isinstance(steps, list) or len(steps) > 1000:
        raise _error('HOST_NOT_READY', 'WORKFLOW_ARTIFACT_STEP_READ_BUDGET', '原步骤清单无效或超过合同上限；不能据此选择文件。')
    candidates = _attempt_candidates(steps, attempt_id)
    if len(candidates) != 1:
        raise _error('OBSERVATION_NOT_FOUND', 'WORKFLOW_ARTIFACT_ATTEMPT_NOT_FOUND', '该原attempt没有唯一正式步骤；不从磁盘剩余目录猜测文件。')
    parent_step, step = candidates[0]
    step_id = step.get('step_id')
    if not isinstance(step_id, str) or not step_id or step_id in {'.', '..'} or '/' in step_id or '\\' in step_id:
        raise _error('HOST_NOT_READY', 'WORKFLOW_ARTIFACT_STEP_INVALID', '原步骤身份无效；恢复原运行记录后重试。')
    snapshot = _source(owner, ('data', 'workflow_runs', run_id, 'steps', step_id + '.json'))
    fields = ('step_id', 'status', 'error_code', 'escalation_result', 'output_dir', 'output_artifacts', 'batches')
    if any(snapshot.get(key) != parent_step.get(key) for key in fields):
        raise _error('HOST_NOT_READY', 'WORKFLOW_ARTIFACT_PUBLICATION_PENDING', '步骤与运行清单尚未一致发表；稍后重读，不使用旧清单或新路径替代。')
    if step.get('status') not in {'succeeded', 'escalated_recovered'} or step.get('error_code'):
        raise _error('OBSERVATION_NOT_FOUND', 'WORKFLOW_ARTIFACT_STEP_NOT_SUCCEEDED', '该attempt未成功提交正式归档；不能把残留文件当作交付。')
    manifest = step.get('output_artifacts')
    if not isinstance(manifest, list) or len(manifest) > 64:
        raise _error('HOST_NOT_READY', 'WORKFLOW_ARTIFACT_MANIFEST_READ_BUDGET', '原归档清单无效或超出单次文件数上限。')
    matches = []
    for item in manifest:
        if not isinstance(item, dict) or not isinstance(item.get('path'), str):
            continue
        relative = item['path']
        # 清单中的坏旁项不能阻塞其它精确ID；仍只验证并打开本次匹配的原路径。
        if workflow_artifact_id(run_id, attempt_id, relative) != artifact_id:
            continue
        path = PurePosixPath(relative)
        if (not relative or len(relative.encode('utf-8', 'surrogatepass')) > 4096 or path.is_absolute() or '\\' in relative
                or '\x00' in relative or '..' in path.parts or str(path) != relative or relative == '.'):
            raise _error('OBSERVATION_NOT_FOUND', 'WORKFLOW_ARTIFACT_PATH_INVALID', '此清单条目路径不安全；未读取内容，改用有效的原产物编号。')
        matches.append(item)
    if len(matches) != 1:
        raise _error('OBSERVATION_NOT_FOUND', 'WORKFLOW_ARTIFACT_NOT_IN_MANIFEST', '该编号没有唯一原清单条目；不接受stdout路径或未登记文件。')
    item = matches[0]
    digest, size = item.get('sha256'), item.get('size_bytes')
    if (not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None
            or type(size) is not int or size < 0 or item.get('artifact_id', artifact_id) != artifact_id
            or item.get('version_ref', digest) != digest):
        raise _error('HOST_NOT_READY', 'WORKFLOW_ARTIFACT_MANIFEST_INVALID', '原版本、尺寸或编号字段不一致，不能核验导出。')
    if version_ref is not None and version_ref != digest:
        raise _error('OBSERVATION_NOT_FOUND', 'ARTIFACT_VERSION_NOT_FOUND', '指定版本不等于原清单版本，不回退当前文件。')
    root = owner / 'data' / 'workflow_runs' / run_id / 'artifacts' / step_id / attempt_id
    if step.get('output_dir') != str(root):
        raise _error('STATE_CONFLICT', 'WORKFLOW_ARTIFACT_OUTPUT_ROOT_MISMATCH', '原步骤归档目录与正规run/step/attempt不一致；未读取外部路径。')
    path = root.joinpath(*PurePosixPath(item['path']).parts)
    content, present = None, False
    if size > _WORKFLOW_CONTENT_BYTES:
        raise _error('STATE_CONFLICT', 'WORKFLOW_ARTIFACT_SIZE_LIMIT', '正式归档超过64MiB上限；未返回内容，重新发布符合上限的产物，不得原样重试。')
    try:
        guard_artifact_content_budget(owner, path, size, _WORKFLOW_CONTENT_BYTES, exact_size=True)
    except BusinessError:
        raise
    except (OSError, ValueError) as error:
        raise _error('STATE_CONFLICT', 'WORKFLOW_ARTIFACT_CONTENT_PATH_REJECTED', '原归档路径无法安全打开；符号链接、目录或不安全文件不能替代原产物。') from error
    if with_content:
        try:
            present = verify_artifact_content(owner, path, size, digest, _WORKFLOW_CONTENT_BYTES)
        except BusinessError:
            raise
        except (OSError, ValueError) as error:
            raise _error('STATE_CONFLICT', 'WORKFLOW_ARTIFACT_CONTENT_PATH_REJECTED', '原归档内容无法安全核验；未读取对外内容，核对原路径及版本。') from error
    else:
        try:
            fd = open_readonly_file_beneath(owner, path.relative_to(owner).parts)
        except FileNotFoundError:
            pass
        else:
            try:
                if os.fstat(fd).st_size != size:
                    raise _error('STATE_CONFLICT', 'ARTIFACT_CONTENT_DRIFTED', '归档尺寸与原清单不符，不能导出为原版本。')
                present = True
            finally:
                os.close(fd)
    latest = _source(owner, run_parts)
    latest_steps = latest.get('steps')
    latest_matches = _attempt_candidates(latest_steps, attempt_id) if isinstance(latest_steps, list) and len(latest_steps) <= 1000 else []
    if (any(latest.get(key) != run.get(key) for key in ('workflow_run_id', 'owner_id', 'business_context', 'ephemeral_debug'))
            or len(latest_matches) != 1 or any(latest_matches[0][0].get(key) != parent_step.get(key) for key in fields)):
        raise _error('HOST_NOT_READY', 'WORKFLOW_ARTIFACT_PUBLICATION_PENDING', '原产物身份或清单在读取期间变更；丢弃本次快照后重试同一编号。')
    # 完整hash后再核本次真实授权；读取期间撤权不能以旧准入发表元数据。
    if not check_artifact_access(facts, authority=authority):
        return None
    # 仅构造现有transport所需的原清单视图，不追加第二份registry。
    record = ArtifactRegistryRecord('artifact_registry.v1', artifact_id, run_id, task.task_id, source.owner,
        'file', mimetypes.guess_type(path.name)[0] or 'application/octet-stream', str(path), digest, size,
        'ready', 'workflow_run_out', 'workflow', step.get('ended_at') or 0.0,
        {'workflow_run_id': run_id, 'step_id': step_id, 'attempt_id': attempt_id})
    value = {'artifact_id': artifact_id, 'version_ref': digest, 'name': path.name, 'mime_type': record.mime_type,
        'size_bytes': size, 'content_sha256': digest, 'task_id': task.task_id, 'space_id': context.space_id, 'available': present}
    return ObservationRecord(value, facts.owner_id, facts.space_id, facts.project_ref,
        facts.task_id, facts.thread_id, facts.run_id, facts.attempt_id), record, content
