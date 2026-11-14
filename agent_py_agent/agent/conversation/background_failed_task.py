"""原后台执行确已失败且处理器均未进入时，收口同一SessionTask；不重试或证明外发成功。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ..common.nofollow_fs import read_bytes_beneath


def closed_failure_without_handlers(agent, task, identity):
    """只读同Owner/线程/任务/当前attempt的原关闭证明及有界工具账；缺证保持未确认。"""
    repo = getattr(getattr(agent, 'subagents', None), 'runtime_db', None)
    if repo is None or not isinstance(identity, dict) or any(not identity.get(key) for key in ('run_id', 'attempt_id', 'task_id', 'request_id')):
        return False
    owner = agent.home_paths.owner_id
    for thread_id in (task.sender_thread_id, task.target_thread_id):
        thread = agent.conversation_store.threads.require(thread_id)
        if thread.owner_id != owner or Path(thread.owner_home) != Path(agent.home_paths.owner_home_dir):
            return False
    if task.task_id != identity['task_id'] or task.conversation_request_id != identity['request_id']:
        return False
    with repo._runtime_connection() as conn:
        row = conn.execute('SELECT ar.agent_run_id,ar.task_run_id,tr.metadata_json,t.created_at '
            'FROM agent_runs ar JOIN agent_attempts aa ON aa.attempt_id=ar.current_attempt_id AND aa.agent_run_id=ar.agent_run_id '
            'JOIN task_runs tr ON tr.task_run_id=ar.task_run_id JOIN tasks t ON t.task_id=tr.task_id '
            "WHERE ar.run_id=? AND aa.attempt_id=? AND ar.parent_agent_run_id='' AND ar.role='main' "
            "AND ar.status='failed' AND aa.status='failed' AND aa.ended_at>0 AND tr.status='failed' AND tr.closed_at>0 "
            'AND t.owner_id=? AND t.thread_id=? AND t.task_id=?',
            (identity['run_id'], identity['attempt_id'], owner, task.target_thread_id, task.task_id)).fetchone()
        if row is None or task.task_run_id != row['task_run_id']:
            return False
        metadata = json.loads(row['metadata_json'])
        if metadata.get('session_task_ref') != task.task_id or metadata.get('business_context') != task.metadata.get('business_context'):
            return False
        if conn.execute('SELECT 1 FROM agent_runs WHERE parent_agent_run_id=? LIMIT 1', (row['agent_run_id'],)).fetchone():
            return False
        if conn.execute('SELECT 1 FROM tool_operations WHERE agent_run_id=? LIMIT 1', (row['agent_run_id'],)).fetchone():
            return False
    home = Path(agent.home_paths.owner_home_dir)
    from ..agent_core.run_task_workspace_writer import canonical_run_workspace_candidate

    root = canonical_run_workspace_candidate(agent, task_id=task.task_id, run_id=identity['run_id'],
        request_id=identity['request_id'], created_at=row['created_at']).relative_to(home)
    raw = read_bytes_beneath(home, (*root.parts, 'work', 'run_workspace.json'), max_bytes=1024 * 1024)
    if raw is None:
        return False
    manifest = json.loads(raw)
    if any(manifest.get(key) != value for key, value in {'owner_id': owner, 'owner_home': str(home),
            'task_id': task.task_id, 'run_id': identity['run_id'], 'request_id': identity['request_id']}.items()):
        return False
    raw = read_bytes_beneath(home, (*root.parts, 'work', 'blobs', 'tool_outputs', 'index.jsonl'), max_bytes=4 * 1024 * 1024)
    if raw is None:
        return False
    lines = raw.splitlines()
    if not 0 < len(lines) <= 256:
        return False
    for line in lines:
        record = json.loads(line)
        if (record.get('kind') != 'tool_call' or record.get('run_id') != identity['run_id']
                or record.get('attempt_id') != identity['attempt_id'] or record.get('task_id') != task.task_id
                or (record.get('tool_execution') or {}).get('handler_executed') is not False):
            return False
    return True


def recover_failed_task_result(agent, thread_id, task_id):
    """原claim内只补确切已失败任务的结果；不进入模型，也不重新执行原工具。"""
    from .background_execution import BackgroundExecutionResult
    from .claim_execution_binding import current_claim_execution
    from .session_task_report import TaskTurnOutcome, close_out_turn

    binding = current_claim_execution()
    store = agent.conversation_store
    task = store.session_tasks.load(task_id) if task_id else None
    if (task is None or task.status != 'accepted' or task.target_thread_id != thread_id or binding is None
            or binding.claims is not store.claims or binding.thread_id != thread_id):
        return None
    claim, error = store.claims.load_report(thread_id, claim_scope_id=binding.claim_scope_id)
    if (error or claim.get('status') != 'running' or claim.get('claim_id') != binding.claim_id
            or claim.get('cancel_requested') is True or float(claim.get('expires_at') or 0) <= time.time()
            or claim.get('task_id') not in (None, '', task_id)):
        return None
    repo = agent.subagents.runtime_db
    run = repo.main_agent_run_for_task(task.task_id)
    if run is None:
        return None
    identity = {'run_id': run['run_id'], 'attempt_id': run['current_attempt_id'], 'task_id': task.task_id,
                'request_id': task.conversation_request_id}
    if not closed_failure_without_handlers(agent, task, identity):
        return None
    message_id = task.metadata.get('reply_message_id')
    message, errors = store.messages.by_id_report(thread_id, message_id) if message_id else (None, [])
    if (errors or message is None or message.role != 'assistant' or message.thread_id != thread_id
            or message.metadata.get('task_id') != task.task_id):
        return None
    close_out_turn(agent, task.conversation_request_id, TaskTurnOutcome(ok=False, summary=message.content))
    return BackgroundExecutionResult(response=message.content, tool_call_count=0, tool_success_count=0,
        material_progress_count=0, delivery_artifacts=(), message_tool_deliveries=(),
        operation_verification=message.metadata.get('operation_verification') or {},
        assistant_commentaries=(), display_snapshot={}, runtime_status='failed', execution_identity=identity)
