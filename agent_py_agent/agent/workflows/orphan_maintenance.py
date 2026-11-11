"""复用 Owner 维护片：到期索引只是投影，回收前重读原资产及关系版本。

没有完整引用覆盖就保留。reference_reader 是宿主的有界只读索引端口，不能调用
模型/网络、创建任务或在资产锁内获取运行执行锁；真实接线由 Owner 维护宿主提供。
"""
from __future__ import annotations

import bisect
import hashlib
import json

from ..common.nofollow_fs import read_text_beneath, write_text_atomic_beneath
from .business_scope import timestamp, workflow_state

BUSINESS_ORPHAN_AFTER_DAYS = 30
BUSINESS_MAINTENANCE_MAX_ROWS = 128
_PARTS = ('data', 'workflow_maintenance.json')


def _record(store):
    raw = read_text_beneath(store.owner_home, _PARTS)
    if raw is not None:
        value = json.loads(raw)
        if value.get('schema_version') != 'workflow-maintenance.v1':
            raise ValueError('WORKFLOW_MAINTENANCE_INVALID')
        return value
    return {'schema_version': 'workflow-maintenance.v1',
        'policy': {'enabled': True, 'orphan_after_days': BUSINESS_ORPHAN_AFTER_DAYS, 'mode': 'recycle'},
        'revision': 0, 'candidates': [], 'cursor': None, 'operations': {}, 'inflight': None}


def _save(store, value):
    write_text_atomic_beneath(store.owner_home, _PARTS,
        json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n', file_mode=0o600)


def _key(row):
    return row['orphan_since'], row['workflow_ref']


def update_due_index(store, definition, *, committed=False):
    """原资产锁内先登记候选再发表正文；索引失败不发表，正文失败只留可证伪候选。"""
    state = workflow_state(definition)
    record = _record(store)
    ref = definition.descriptor.id
    # canonical ACK 前不能移除旧候选；原资产事务回滚后，旧孤立义务仍必须能被找到。
    candidates = ([row for row in record['candidates'] if row['workflow_ref'] != ref]
                  if committed else list(record['candidates']))
    if state['scope']['scope_mode'] == 'unassigned' and state['lifecycle'] == 'active' and state['orphan_since'] is not None:
        from ..assets import next_asset_revision
        row = {'workflow_ref': ref, 'scope_revision': state['scope']['scope_revision'],
               'orphan_since': state['orphan_since'], 'definition_revision': (
                   definition.descriptor.revision if committed else next_asset_revision(store._read(ref)))}
        if row not in candidates:
            bisect.insort(candidates, row, key=_key)
    if candidates != record['candidates']:
        record['candidates'] = candidates
        _save(store, record)


def cleanup_status(service, definition):
    state = workflow_state(definition)
    blockers = []
    if state['protected']:
        blockers.append({'ref': definition.descriptor.id, 'kind': 'protected', 'reason_code': 'PROTECTED'})
    if definition.descriptor.source_scope != 'owner' or definition.descriptor.scope_layer != 'owner':
        blockers.append({'ref': definition.descriptor.id, 'kind': 'read_only', 'reason_code': 'READ_ONLY'})
    try:
        if callable(getattr(service, 'reference_reader', None)):
            try:
                references = service.reference_reader(definition.descriptor.id)
            except TypeError:
                try:
                    references = service.reference_reader('workflow', definition.descriptor.id, getattr(service, 'owner_id', None))
                except Exception:
                    references = None
        else:
            references = None
    except (OSError, ValueError, LookupError, TypeError):
        references = None
    if (not isinstance(references, dict) or references.get('coverage') != 'complete'
            or not isinstance(references.get('blockers'), list)):
        blockers.append({'ref': definition.descriptor.id, 'kind': 'coverage', 'reason_code': 'REFERENCE_COVERAGE_UNKNOWN'})
    else:
        for row in references['blockers']:
            if not isinstance(row, dict) or set(row) != {'ref', 'kind', 'reason_code'}:
                blockers.append({'ref': definition.descriptor.id, 'kind': 'coverage', 'reason_code': 'REFERENCE_COVERAGE_UNKNOWN'})
                break
            blockers.append(dict(row))
    policy = _record(service.store)['policy']
    eligible = state['orphan_since'] + policy['orphan_after_days'] * 86400 if state['orphan_since'] is not None else None
    return {'protected': state['protected'], 'orphan_since': timestamp(state['orphan_since']),
        'last_content_edit_at': timestamp(state['last_content_edit_at']), 'last_run_at': timestamp(state['last_run_at']),
        'last_restore_at': timestamp(state['last_restore_at']), 'eligible_at': timestamp(eligible), 'blockers': blockers[:100]}


def _operation_identity(action, request, key):
    if not isinstance(key, str) or not 0 < len(key.encode()) <= 256:
        raise ValueError('IDEMPOTENCY_KEY_REQUIRED')
    return hashlib.sha256((action + '\0' + key).encode()).hexdigest(), hashlib.sha256(
        json.dumps(request, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _policy_action(service, action, request, revision, key):
    with service.store._locked():
        record = _record(service.store)
        if action == 'workflows.cleanup.policy.get':
            return {'policy': record['policy'], 'revision': record['revision']}
        identity, fingerprint = _operation_identity(action, request, key)
        if saved := record['operations'].get(identity):
            if saved['fingerprint'] != fingerprint:
                raise ValueError('IDEMPOTENCY_CONFLICT')
            return saved['result']
        if type(revision) is not int or revision != record['revision']:
            raise ValueError('REVISION_CONFLICT')
        policy = request['policy']
        if (set(policy) != {'enabled', 'orphan_after_days', 'mode'} or type(policy['enabled']) is not bool
                or type(policy['orphan_after_days']) is not int or not 1 <= policy['orphan_after_days'] <= 3650
                or policy['mode'] != 'recycle'):
            raise ValueError('INVALID_CLEANUP_POLICY')
        record.update(policy=dict(policy), revision=record['revision'] + 1)
        result = {'policy': record['policy'], 'revision': record['revision']}
        record['operations'][identity] = {'fingerprint': fingerprint, 'result': result}
        _save(service.store, record)
        return result


def _batch(record, *, now, limit):
    candidates = record['candidates']
    cursor = record['cursor']
    start = bisect.bisect_right(candidates, tuple(cursor), key=_key) if cursor else 0
    selected = candidates[start:start + limit]
    # 索引按孤立起点排序，Owner policy 天数统一；首条未到期后无需读任何正文。
    selected = [row for row in selected if row['orphan_since'] + record['policy']['orphan_after_days'] * 86400 <= now]
    return selected, start + len(selected) < len(candidates)


def cleanup_action(service, action, request, *, actor, expected_revision, idempotency_key):
    if action in ('workflows.cleanup.policy.get', 'workflows.cleanup.policy.set'):
        return _policy_action(service, action, request, expected_revision, idempotency_key)
    if action == 'workflows.cleanup.run':
        return maintain_owner_workflows(service, actor=actor, idempotency_key=idempotency_key)
    if action != 'workflows.cleanup.preview':
        raise ValueError('UNKNOWN_WORKFLOW_ACTION')
    limit = request.get('limit', 50)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('INVALID_CLEANUP_LIMIT')
    with service.store._locked():
        record = _record(service.store)
        if request.get('cursor'):
            try:
                record['cursor'] = json.loads(request['cursor'])
            except (ValueError, TypeError):
                raise ValueError('INVALID_CLEANUP_CURSOR') from None
        rows, more = _batch(record, now=service.clock(), limit=limit)
    eligible, skipped = [], []
    for row in rows:
        definition = service._get(row['workflow_ref'])
        status = cleanup_status(service, definition)
        if status['blockers']:
            skipped.extend(status['blockers'])
        else:
            eligible.append({'workflow_ref': row['workflow_ref'], 'scope_revision': row['scope_revision'],
                'eligible_at': status['eligible_at'], 'reason_code': 'ORPHAN_EXPIRED'})
    return {'eligible': eligible, 'skipped': skipped[:100],
        'next_cursor': json.dumps(_key(rows[-1])) if more and rows else None,
        'coverage': 'partial' if more else 'complete'}


# LLM: 仅可信宿主可采用原inflight身份；行级授权和资产CAS保持，进度/完成回执须在原锁内重读比较，禁止倒退或复活。
# 函数用途: 用原操作键续跑最多128条持久化清理，手动调用仍须提供自己的原幂等键。
def maintain_owner_workflows(service, *, actor, idempotency_key, resume_inflight: bool = False):
    """现有 Owner 节拍和手动动作调用同一片；无自己的线程或轮询器。"""
    identity, fingerprint = _operation_identity('workflows.cleanup.run', {}, idempotency_key)
    with service.store._locked():
        record = _record(service.store)
        pending = record['inflight']
        # 定时器的新键可能已有旧回执，仍须先处理尚未完成的原事务。
        if resume_inflight and pending is not None:
            identity = pending['identity']
        if saved := record['operations'].get(identity):
            return saved['result']
        if pending is not None and pending['identity'] != identity:
            raise BlockingIOError('WORKFLOW_MAINTENANCE_PENDING')
        if pending is None:
            rows, more = _batch(record, now=service.clock(), limit=BUSINESS_MAINTENANCE_MAX_ROWS) if record['policy']['enabled'] else ([], False)
            pending = {'identity': identity, 'rows': rows, 'more': more, 'position': 0,
                'counts': {'recycled': 0, 'skipped': 0, 'failed': 0, 'unknown': 0}}
            record['inflight'] = pending
            _save(service.store, record)
    while True:
        with service.store._locked():
            record = _record(service.store)
            if saved := record['operations'].get(identity):
                return saved['result']
            pending = record['inflight']
            if pending is None or pending['identity'] != identity:
                raise BlockingIOError('WORKFLOW_MAINTENANCE_PENDING')
            position = pending['position']
            if position >= len(pending['rows']):
                cursor = list(_key(pending['rows'][-1])) if pending['more'] and pending['rows'] else None
                result = {'operation_id': identity, 'state': 'completed', 'counts': pending['counts'],
                          'next_cursor': json.dumps(cursor) if cursor else None}
                record.update(cursor=cursor, inflight=None)
                record['operations'][identity] = {'fingerprint': fingerprint, 'result': result}
                _save(service.store, record)
                return result
        row = pending['rows'][position]
        outcome = 'skipped'
        service._authorize(actor, 'workflows.cleanup.run', row['workflow_ref'])
        try:
            definition = service._get(row['workflow_ref'])
            state = workflow_state(definition)
            if (state['scope']['scope_revision'] == row['scope_revision']
                    and state['scope']['scope_mode'] == 'unassigned'
                    and state['orphan_since'] == row['orphan_since']):
                status = cleanup_status(service, definition)
                if any(item['reason_code'] == 'REFERENCE_COVERAGE_UNKNOWN' for item in status['blockers']):
                    outcome = 'unknown'
                elif not status['blockers']:
                    service.execute('workflows.recycle', {'workflow_ref': row['workflow_ref']}, actor=actor,
                        expected_revision=row['definition_revision'], idempotency_key='cleanup:' + identity + ':' + row['workflow_ref'])
                    outcome = 'recycled'
        except (KeyError, ValueError, PermissionError):
            # 关系/正文 CAS 或权限变化只保留候选；不能回收刚重新关联的资产。
            outcome = 'skipped'
        with service.store._locked():
            record = _record(service.store)
            if saved := record['operations'].get(identity):
                return saved['result']
            current = record['inflight']
            if current is None or current['identity'] != identity:
                raise BlockingIOError('WORKFLOW_MAINTENANCE_PENDING')
            # 同行副作用由原资产幂等键去重；其它续跑者已经提交的计数不能再加。
            if current['position'] != position:
                continue
            current['counts'][outcome] += 1
            current['position'] += 1
            _save(service.store, record)
