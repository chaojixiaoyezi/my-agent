"""子代理公开历史的准确线程定位与消息来源投影；不读取父线程或模型私有历史。"""
from __future__ import annotations

import json
from pathlib import Path

from ..common.nofollow_fs import read_bytes_beneath
from .background_team_lifecycle import background_identity
from .business_access import BusinessError, require_access


class SubagentTranscriptUnavailable(ValueError):
    """可公开的缺证据原因；不把缺失来源当空历史。"""


def resolve_subagent_transcript(service, run_id, actor):
    from .business_observability import _observation_thread

    page = service._source('executions', {'run_id': run_id})
    rows = [row for row in page.records if row.run_id == run_id and row.value.get('_is_current')
            and row.value.get('_parent_run_id')]
    if len(rows) != 1:
        if page.coverage != 'complete':
            raise SubagentTranscriptUnavailable('SUBAGENT_EXECUTION_COVERAGE_INCOMPLETE')
        raise BusinessError('OBSERVATION_NOT_FOUND')
    record = rows[0]
    require_access(actor, 'messages.list', service=service, owner_id=record.owner_id,
        space_id=record.space_id, project_ref=record.project_ref, task_id=record.task_id,
        thread_id=record.thread_id, run_id=record.run_id, attempt_id=record.attempt_id)
    try:
        link, error = service.store.tasks.link_report(record.thread_id, run_id)
        if error or link is None or not link.task_path:
            raise SubagentTranscriptUnavailable('SUBAGENT_TASK_LINK_UNAVAILABLE')
        home = service.owner_root.resolve()
        root = Path(link.task_path).absolute()
        parts = root.relative_to(home).parts
        if not parts:
            raise SubagentTranscriptUnavailable('SUBAGENT_TASK_ROOT_INVALID')
        def read_object(suffix):
            raw = read_bytes_beneath(home, (*parts, *suffix), max_bytes=4 * 1024 * 1024 + 1)
            if raw is None or len(raw) > 4 * 1024 * 1024:
                raise SubagentTranscriptUnavailable('SUBAGENT_CANONICAL_STATE_UNAVAILABLE')
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise SubagentTranscriptUnavailable('SUBAGENT_CANONICAL_STATE_INVALID')
            return value
        pointer = read_object(('run.json',))
        canonical = Path(pointer.get('canonical_state_ref', ''))
        state = read_object(canonical.relative_to(root).parts)
        if state.get('id') != run_id or state.get('owner') != service.owner_id:
            raise BusinessError('BUSINESS_ACCESS_DENIED')
        thread = _observation_thread(service.store, state.get('agent_thread_id', ''))
    except SubagentTranscriptUnavailable:
        raise
    except (OSError, ValueError, TypeError, KeyError) as error:
        if isinstance(error, BusinessError):
            raise
        raise SubagentTranscriptUnavailable('SUBAGENT_CANONICAL_THREAD_UNAVAILABLE') from error
    if (thread.owner_id != service.owner_id or thread.metadata.get('thread_kind') != 'agent'
            or thread.metadata.get('agent_run_id') != run_id):
        raise BusinessError('BUSINESS_ACCESS_DENIED')
    require_access(actor, 'messages.list', service=service, owner_id=record.owner_id,
        space_id=record.space_id, project_ref=record.project_ref, task_id=record.task_id,
        thread_id=thread.thread_id, run_id=record.run_id, attempt_id=record.attempt_id)
    return thread, record


def public_message_origin(service, message, actor):
    """仅在发送线程也属本人且获当前actor授权时公开sender，否则保留原消息ref。"""
    from .business_observability import ObservationRecord, _observation_cached, _observation_thread
    from .session_messaging import SESSION_MESSAGE_ORIGIN_KIND

    origin = {'kind': 'canonical_message', 'source_ref': message.message_id, 'revision': None}
    metadata = message.metadata
    sender = metadata.get('origin_thread_id') if metadata.get('origin_kind') == SESSION_MESSAGE_ORIGIN_KIND else None
    if not isinstance(sender, str) or not sender:
        return origin
    try:
        thread = _observation_thread(service.store, sender)
    except (OSError, ValueError, TypeError, KeyError):
        return origin
    identity = background_identity(thread)
    space_id, project_ref = identity.space_id if identity else None, identity.project_ref if identity else None
    if identity is None and thread.metadata.get('business_controller') is not True:
        run_id = thread.metadata.get('agent_run_id') if thread.metadata.get('thread_kind') == 'agent' else None
        if not isinstance(run_id, str) or not run_id:
            return origin
        page = _observation_cached((service, 'sender_execution', run_id),
            lambda: service._source('executions', {'run_id': run_id}))
        rows = [row for row in page.records if row.run_id == run_id and row.value.get('_is_current')]
        if len(rows) != 1:
            return origin
        space_id, project_ref = rows[0].space_id, rows[0].project_ref
    facts = ObservationRecord({}, thread.owner_id, space_id, project_ref, thread_id=thread.thread_id)
    if service._visible(facts, 'messages.list', {}, actor):
        return {'kind': SESSION_MESSAGE_ORIGIN_KIND, 'source_ref': thread.thread_id, 'revision': None}
    return origin
