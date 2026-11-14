"""按准确执行身份读取原租约与运行事件，补齐业务执行详情的来源和恢复证据。"""
from __future__ import annotations

import json

from ..common.log_redaction import redact_sensitive_value
from .business_access import BusinessError
from .business_observability import _observation_task, _observation_thread, _timestamp
from .run_claim import detached_task_claim_scope_id


def execution_lifecycle(service, execution):
    run_id, attempt_id = execution['run_id'], execution['attempt_id']
    with service.runtime._runtime_connection() as conn:
        row = conn.execute(
            'SELECT ar.agent_run_id,t.owner_id,t.thread_id FROM agent_attempts aa '
            'JOIN agent_runs ar ON ar.agent_run_id=aa.agent_run_id '
            'JOIN task_runs tr ON tr.task_run_id=ar.task_run_id JOIN tasks t ON t.task_id=tr.task_id '
            'WHERE aa.attempt_id=? AND ar.run_id=? AND t.owner_id=?',
            (attempt_id, run_id, service.owner_id)).fetchone()
        if row is None or row['thread_id'] not in ('', execution['thread_id']):
            raise BusinessError('OBSERVATION_NOT_FOUND')
        attempts = conn.execute(
            'SELECT attempt_id,status,started_at,ended_at FROM agent_attempts '
            'WHERE agent_run_id=? ORDER BY attempt_generation DESC LIMIT 101',
            (row['agent_run_id'],)).fetchall()
        events = conn.execute(
            'SELECT event_id,event_type,created_at,substr(payload_json,1,16384) AS payload_json, '
            'length(CAST(payload_json AS BLOB)) AS payload_bytes FROM runtime_events '
            'WHERE agent_run_id=? AND attempt_id=? ORDER BY seq DESC LIMIT 101',
            (row['agent_run_id'], attempt_id)).fetchall()
    unavailable = []
    if len(attempts) > 100 or len(events) > 100:
        unavailable.append('HISTORY_WINDOW_TRUNCATED')
    source = {'sender_thread_id': None, 'input_ref': None, 'request_id': None}
    claim = None
    if service.store is not None:
        task = _observation_task(service.store, execution['task_id']) if execution['task_id'] else None
        if task is not None:
            sender = _observation_thread(service.store, task.sender_thread_id)
            if sender.owner_id != service.owner_id or task.target_thread_id != execution['thread_id']:
                raise BusinessError('BUSINESS_ACCESS_DENIED')
            source = {'sender_thread_id': task.sender_thread_id,
                      'input_ref': task.body_guidance_id or None,
                      'request_id': task.conversation_request_id or None}
        claim = _matching_claim(service, execution)
    if not any(source.values()):
        unavailable.append('SOURCE_INPUT_UNAVAILABLE')
    if claim is None:
        unavailable.append('EXACT_CLAIM_UNAVAILABLE')
    event_views, error, retry_refs = [], None, []
    canonical_success = execution.get('status') == 'done' and any(
        item['attempt_id'] == attempt_id and item['status'] == 'done' for item in attempts[:100])
    known_attempts = {item['attempt_id'] for item in attempts[:100]}
    relation_keys = ('previous_attempt_id', 'superseded_by_attempt_id', 'recovered_attempt_id')
    keys = ('reason', 'source', 'status', 'error_code', 'runtime_reason', 'effect_outcome',
            'previous_attempt_id', 'current_attempt_id', 'from_attempt_id', 'to_attempt_id',
            'recovered_attempt_id', 'superseded_by_attempt_id', 'attempt_generation',
            'retryable', 'cancel_requested')
    for event in events[:100]:
        if event['payload_bytes'] > 16384:
            unavailable.append('EVENT_PAYLOAD_TOO_LARGE')
            continue
        try:
            payload = json.loads(event['payload_json'])
        except (ValueError, TypeError):
            unavailable.append('EVENT_PAYLOAD_INVALID')
            continue
        if not isinstance(payload, dict):
            unavailable.append('EVENT_PAYLOAD_INVALID')
            continue
        details = [{'key': key, 'value': redact_sensitive_value(payload[key])}
                   for key in keys if key in payload and isinstance(payload[key], (str, int, float, bool, type(None)))]
        event_views.append({'event_id': event['event_id'], 'kind': event['event_type'],
                            'occurred_at': _timestamp(event['created_at']), 'details': details})
        for key in relation_keys:
            candidate = payload.get(key)
            if isinstance(candidate, str) and candidate != attempt_id and candidate in known_attempts:
                retry_refs.append(candidate)
        # 正常runner收尾说明仍是事件事实；准确attempt已成功时，不能将其冒充结构化错误码。
        code = payload.get('error_code')
        if not code and not canonical_success:
            code = payload.get('runtime_reason')
        if error is None and isinstance(code, str) and code:
            error = {'code': code[:128], 'message': str(redact_sensitive_value(payload.get('error_message')
                     or payload.get('message') or ''))[:4096],
                     'retryable': payload.get('retryable') is True, 'details': []}
    return {'source': source, 'claim': claim, 'events': event_views,
            'attempts': [{'attempt_id': item['attempt_id'], 'status': item['status'],
                          'started_at': _timestamp(item['started_at']), 'ended_at': _timestamp(item['ended_at'])}
                         for item in attempts[:100]],
            'coverage': 'partial' if unavailable else 'complete',
            'unavailable': list(dict.fromkeys(unavailable))}, error, list(dict.fromkeys(retry_refs))


def _matching_claim(service, execution):
    thread_id, task_id = execution['thread_id'], execution['task_id']
    scopes = [thread_id]
    if task_id:
        link, error = service.store.tasks.link_report(thread_id, task_id)
        if error:
            return None
        if link is not None and link.cancellation_scope == 'detached':
            scopes = [detached_task_claim_scope_id(thread_id, task_id)]
    current, error = service.store.claims.load_report(thread_id, claim_scope_id=scopes[0])
    if error:
        return None
    for claim in (current, current.get('previous_claim', {})):
        authority = claim.get('runtime_authority', {}) if isinstance(claim, dict) else {}
        if (not isinstance(authority, dict) or authority.get('run_id') != execution['run_id']
                or authority.get('attempt_id') != execution['attempt_id']
                or claim.get('thread_id') != thread_id):
            continue
        return {'claim_id': claim['claim_id'], 'status': claim['status'], 'phase': claim.get('phase', ''),
                'reason': claim.get('reason', ''), 'cancel_requested': claim.get('cancel_requested') is True,
                'started_at': _timestamp(claim.get('started_at')),
                'expires_at': _timestamp(claim.get('expires_at'))}
    return None
