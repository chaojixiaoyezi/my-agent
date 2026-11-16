"""精确后台 /stop 不扫描 SessionTask；未知目标不借其它 running claim 授权。"""
import json
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.concurrency.interrupt import register_interruptible, wait_interruptibly
from agent_py_agent.agent.conversation.claim_execution_binding import (
    ClaimExecutionBinding,
    bind_claim_execution,
    publish_claim_runtime_authority,
)
from agent_py_agent.agent.conversation.control_commands import (
    ConversationControlCommand,
    session_task_interrupt_name,
)
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
from agent_py_agent.agent.gateway_parts import control_service as controls
from agent_py_agent.tests.test_schedule_control import _agent


def _case(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    store = agent.conversation_store
    def thread(name):
        return store.threads.get_or_create({'channel': 'local', 'channel_conversation_id': name,
            'channel_user_id': 'main', 'canonical_user_id': 'main', 'owner_id': agent.home_paths.owner_id,
            'owner_home': str(agent.home_paths.owner_home_dir)})
    sender, target = thread('sender'), thread('target')
    task = store.session_tasks.create(SessionTaskDraft(sender.thread_id, target.thread_id, '合成后台目标'))
    turn_id = 'exact-bound-turn'
    task = store.session_tasks.bind_turn(task.task_id, turn_id=turn_id)
    repo = agent.subagents.runtime_db
    created = repo.record_run_creation(owner_id=agent.home_paths.owner_id, conversation_task_id=task.task_id,
        thread_id=target.thread_id, run_id=turn_id, role='main', attempt_status='pending')
    attempt = repo.create_attempt(created['agent_run_id'], reuse_pending=True)
    claim = store.claims.acquire({'thread_id': target.thread_id, 'task_id': task.task_id, 'reason': 'session_task'})
    assert claim
    binding = ClaimExecutionBinding(store.claims, target.thread_id, claim['claim_id'], target.thread_id)
    with bind_claim_execution(binding):
        publish_claim_runtime_authority({'invocation_run_id': turn_id, 'task_id': task.task_id, 'run_id': turn_id,
            'agent_run_id': created['agent_run_id'], 'attempt_id': attempt['attempt_id']})
    scope = controls.GatewayControlScope('main', 'local', 'target', metadata={'expected_turn_id': turn_id})
    monkeypatch.setattr(controls, '_request_agent_for_scope', lambda *_: agent)
    monkeypatch.setattr(store.session_tasks, 'list_report', lambda **_: pytest.fail('精确停止禁止全表扫描'))
    return SimpleNamespace(agent=agent, store=store, task=task, target=target, scope=scope, turn=turn_id)


def test_unrelated_corrupt_task_does_not_prevent_exact_registered_stop(tmp_path, monkeypatch):
    case = _case(tmp_path, monkeypatch)
    (case.store.session_tasks._dir() / 'unrelated-corrupt.json').write_text('{broken')
    ready, release, observed = threading.Event(), threading.Event(), []
    def target():
        with register_interruptible(session_task_interrupt_name(case.turn)):
            ready.set()
            try:
                while not release.is_set():
                    wait_interruptibly(.05)
            except InterruptedError:
                observed.append('target interrupted')
    worker = threading.Thread(target=target)
    worker.start()
    try:
        assert ready.wait(5)
        outcome = controls._stop_bound_background_turn(case.agent, None, case.scope, ConversationControlCommand('stop'))
        worker.join(5)
        assert outcome.ok and outcome.request_id == case.turn
        assert not worker.is_alive() and observed == ['target interrupted']
        assert (case.store.session_tasks._dir() / 'unrelated-corrupt.json').read_text() == '{broken'
    finally:
        release.set()
        worker.join(5)


@pytest.mark.parametrize('mutation', ['turn', 'task', 'thread', 'scope', 'claim', 'missing_authority', 'terminal', 'corrupt_target', 'task_turn', 'task_target'])
def test_unknown_or_changed_exact_identity_never_interrupts_another_turn(tmp_path, monkeypatch, mutation):
    case = _case(tmp_path, monkeypatch)
    claim_path = case.store.claims.storage.background_claim_path(case.target.thread_id)
    claim = json.loads(claim_path.read_text())
    if mutation == 'turn':
        claim['runtime_authority']['invocation_run_id'] = 'other-turn'
    elif mutation == 'task':
        claim['runtime_authority']['task_id'] = 'other-task'
    elif mutation == 'thread':
        claim['thread_id'] = 'other-thread'
    elif mutation == 'scope':
        claim['claim_scope_id'] = 'other-lane'
    elif mutation == 'claim':
        claim['runtime_authority']['claim_id'] = 'other-claim'
    elif mutation == 'missing_authority':
        claim.pop('runtime_authority')
    elif mutation == 'terminal':
        claim['status'] = 'finished'
    elif mutation in {'task_turn', 'task_target'}:
        path = case.store.session_tasks._path(case.task.task_id)
        task = json.loads(path.read_text())
        task['conversation_request_id' if mutation == 'task_turn' else 'target_thread_id'] = 'other-target'
        path.write_text(json.dumps(task))
    else:
        case.store.session_tasks._path(case.task.task_id).write_text('{broken')
    claim_path.write_text(json.dumps(claim))
    calls = []
    monkeypatch.setattr(controls, 'interrupt_by_name', lambda name: calls.append(name) or True)
    outcome = controls._stop_bound_background_turn(case.agent, None, case.scope, ConversationControlCommand('stop'))
    assert not outcome.ok and calls == []
