from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.technical_continuation import (
    WAKE_REASON,
    claim_wake,
    publish_pending,
    receipt_for,
    record_closeout,
    suppress_intermediate,
)
from agent_py_agent.agent.tooling.models import EffectResolverPolicy, ToolRuntimePolicy


@pytest.fixture
def setup(tmp_path):
    store = ConversationStore(tmp_path / 'conversations')
    thread = store.threads.create(canonical_user_id='user', owner_id='owner')
    store.messages.append({'thread_id': thread.thread_id, 'role': 'user', 'content': '普通用户任务',
                           'metadata': {'conversation_request_id': 'request-1'}})
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'task-1', 'goal': '任务'})
    policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy(default_effect='mutating'))
    tools = SimpleNamespace(runtime_snapshot=lambda: SimpleNamespace(runtime=lambda _name: SimpleNamespace(runtime_policy=policy)))
    agent = SimpleNamespace(conversation_store=store, home_paths=SimpleNamespace(owner_id='owner'),
        config=SimpleNamespace(technical_cutoff_continuation_enabled=True), tools=tools)
    ctx = SimpleNamespace(do_save=True, context_scope='conversation', source='gateway',
        request_id='request-1', run_id='run-1', attempt_id='attempt-1',
        task_attributes={'conversation_task_id': 'task-1', 'conversation_thread_id': thread.thread_id},
        archive_tool_calls=[{'tool': 'write', 'ok': True, 'handler_executed': True}],
        final_response=SimpleNamespace(runtime_reason='TOOL_ROUND_LIMIT_REACHED',
            runtime_source='tool_loop', runtime_status='unfinished'))
    return agent, store, thread, ctx


def _wake(store):
    return next(row for row in store.wakes.pending(limit=20) if row.reason == WAKE_REASON)


@pytest.mark.parametrize('goal', [False, True])
def test_cutoff_closeout_wake_claim_next_slice_is_bounded_and_durable(setup, goal):
    agent, store, thread, ctx = setup
    if goal:
        created = store.goals.create({'thread_id': thread.thread_id, 'objective': '明确目标', 'task_id': 'task-1'})
        assert created.task_id == 'task-1'
    receipt = record_closeout(agent, ctx)
    assert receipt.status == 'pending' and receipt.sequence == 1
    assert receipt.turn_end_reason == 'interrupted'
    assert record_closeout(agent, ctx) == receipt
    assert len(store.wakes.pending(limit=20)) == 1
    restarted = ConversationStore(store.storage.root)
    agent.conversation_store = restarted
    signal = _wake(restarted)
    assert claim_wake(agent, restarted, signal)
    assert claim_wake(agent, restarted, signal)
    for sequence in range(2, 5):
        ctx.source, ctx.do_save = 'background_main_agent', False
        ctx.run_id, ctx.attempt_id = f'run-{sequence}', f'attempt-{sequence}'
        current = record_closeout(agent, ctx)
        assert current.status == ('pending' if sequence <= 3 else 'stopped')
        if sequence <= 3:
            assert publish_pending(agent, restarted, 'task-1')
            signals = restarted.wakes.pending(limit=20)
            next_signal = next(row for row in signals if row.metadata.get('cutoff_receipt_key') == current.key)
            assert claim_wake(agent, restarted, next_signal)
    assert not publish_pending(agent, restarted, 'task-1')


@pytest.mark.parametrize('reason,source,status', [
    ('TASK_PROGRESS_OPEN', 'tool_loop', 'unfinished'),
    ('REPEATED_TOOL_FAILURE', 'tool_loop', 'unfinished'),
    ('CAPACITY_WAIT', 'resource_capacity', 'unfinished'),
    ('SUBAGENTS_ACTIVE', 'subagent_lifecycle', 'unfinished'),
    ('MODEL_RESPONSE_TRUNCATED', 'other', 'unfinished'),
    ('TOOL_ROUND_LIMIT_REACHED', 'tool_loop', 'cancelled'),
])
def test_non_cutoff_does_not_create_authority(setup, reason, source, status):
    agent, store, _, ctx = setup
    ctx.final_response = SimpleNamespace(runtime_reason=reason, runtime_source=source, runtime_status=status)
    assert record_closeout(agent, ctx) is None
    assert store.wakes.pending(limit=20) == []


@pytest.mark.parametrize('reason,source,status', [
    ('TOOL_ROUND_LIMIT_REACHED', 'tool_loop', 'unfinished'),
    ('MODEL_RESPONSE_TRUNCATED', 'tool_loop', 'unfinished'),
    ('TOOL_CALL_UNCLOSED', 'tool_protocol_adapter', 'unfinished'),
    ('CONTEXT_OVERFLOW', 'preflight', 'context_overflow'),
])
def test_precise_cutoffs_create_receipt(setup, reason, source, status):
    agent, _, _, ctx = setup
    ctx.final_response = SimpleNamespace(runtime_reason=reason, runtime_source=source, runtime_status=status)
    assert record_closeout(agent, ctx).status == 'pending'


@pytest.mark.parametrize('invalidation', ['new_user', 'cancelled', 'interrupted', 'superseded', 'owner', 'disabled', 'forged'])
def test_admission_revalidates_current_authority(setup, invalidation):
    agent, store, thread, ctx = setup
    record_closeout(agent, ctx)
    signal = _wake(store)
    if invalidation == 'new_user':
        store.messages.append({'thread_id': thread.thread_id, 'role': 'user', 'content': '新消息'})
    elif invalidation in {'cancelled', 'interrupted', 'superseded'}:
        store.tasks.update_status({'task_id': 'task-1', 'status': invalidation})
    elif invalidation == 'owner':
        agent.home_paths.owner_id = 'different'
    elif invalidation == 'disabled':
        agent.config.technical_cutoff_continuation_enabled = False
    else:
        signal = replace(signal, metadata={'cutoff_receipt_key': 'forged'})
    assert not claim_wake(agent, store, signal)


def test_no_progress_stops_and_normal_final_does_not_restart(setup):
    agent, store, _, ctx = setup
    record_closeout(agent, ctx)
    assert claim_wake(agent, store, _wake(store))
    ctx.source, ctx.do_save, ctx.attempt_id = 'background_main_agent', False, 'attempt-2'
    ctx.archive_tool_calls = []
    assert record_closeout(agent, ctx).status == 'stopped'
    assert not publish_pending(agent, store, 'task-1')
    assert store.threads.require(ctx.task_attributes['conversation_thread_id']).pending_host_notices


def test_final_clears_chain_and_projection_only_suppresses_exact_attempt(setup):
    agent, store, _, ctx = setup
    record_closeout(agent, ctx)
    assert suppress_intermediate(store, task_id='task-1', run_id='run-1', attempt_id='attempt-1')
    assert not suppress_intermediate(store, task_id='task-1', run_id='run-2', attempt_id='attempt-1')
    assert claim_wake(agent, store, _wake(store))
    ctx.source, ctx.attempt_id = 'background_main_agent', 'attempt-final'
    ctx.final_response = SimpleNamespace(runtime_status='ok', runtime_reason='', runtime_source='')
    receipt = record_closeout(agent, ctx)
    assert receipt.status == 'finished' and receipt.sequence == 0
    assert not publish_pending(agent, store, 'task-1')


def test_assistant_append_keeps_user_anchor_and_task_cache_cannot_mutate_receipt(setup):
    agent, store, thread, ctx = setup
    original = store.threads.require(thread.thread_id).last_user_message_id
    store.messages.append({'thread_id': thread.thread_id, 'role': 'assistant', 'content': '进展'})
    assert store.threads.require(thread.thread_id).last_user_message_id == original
    record_closeout(agent, ctx)
    link = store.tasks.load('task-1')
    link.cutoff_continuation['status'] = 'corrupted'
    assert receipt_for(store.tasks.load('task-1')).status == 'pending'


def _real_cutoff_setup(tmp_path, with_goal=False):
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.backends.base import EchoBackend
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend='echo', enable_tools=True, max_tool_rounds=1), tmp_path)
    store = agent.conversation_store
    owner = agent.home_paths.owner_id
    thread = store.threads.create(canonical_user_id='user', owner_id=owner)
    store.messages.append({'thread_id': thread.thread_id, 'role': 'user', 'content': '写文件然后报告',
                           'metadata': {'conversation_request_id': 'request-real'}})
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'task-real', 'goal': '写文件然后报告'})

    if with_goal:
        store.goals.create({'thread_id': thread.thread_id, 'task_id': 'task-real', 'objective': '明确目标'})

    class WritesThenFinishes(EchoBackend):
        calls = 0
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            if self.calls <= 2:
                return ModelResponse(text='', backend=self.name, tool_use_blocks=[{
                    'id': 'write-real', 'name': 'write_file',
                    'input': {'path': str(tmp_path / 'result.txt'), 'content': '实际工具结果'}}])
            return ModelResponse(text='已经写好并核对。', backend=self.name)

    agent.backend = WritesThenFinishes()
    return agent, store, thread


def _run_first_cutoff(agent, thread, tmp_path):
    return agent.run('写文件然后报告', request_id='request-real', source='gateway',
        task_id='task-real', allowed_tools=['write_file'],
        write_boundary={'allowed_write_roots': [str(tmp_path)]},
        task_attributes={'conversation_thread_id': thread.thread_id, 'conversation_task_id': 'task-real'})


@pytest.mark.parametrize("with_goal", [False, True])
def test_real_run_closeout_to_background_wake_returns_one_final(tmp_path, with_goal):
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )

    agent, store, thread = _real_cutoff_setup(tmp_path, with_goal)
    first = _run_first_cutoff(agent, thread, tmp_path)
    assert (tmp_path / 'result.txt').exists(), str(first.archive_tool_calls)
    assert (tmp_path / 'result.txt').read_text() == '实际工具结果'
    assert first.runtime_reason == 'TOOL_ROUND_LIMIT_REACHED'
    receipt = receipt_for(store.tasks.load('task-real'))
    assert receipt.status == 'pending'
    signal = _wake(store)
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store)
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store})
    reports = scheduler.tick_thread(thread.thread_id, now=signal.created_at + 1)
    assert len(reports) == 1
    report = reports[0]
    assert store.wakes.pending_one(signal.wake_signal_id) is None
    assert report.delivery_status == 'sent', report.delivery_reason
    assert receipt_for(store.tasks.load('task-real')).status == 'finished'
    answers = [row for row in store.messages.recent(thread.thread_id, limit=30)
               if row.role == 'assistant' and row.content == '已经写好并核对。']
    assert len(answers) == 1


@pytest.mark.parametrize('goal_status', ['paused', 'complete', 'budget_limited', 'usage_limited'])
def test_goal_stop_invalidates_pending_receipt(setup, goal_status):
    agent, store, thread, ctx = setup
    goal = store.goals.create({'thread_id': thread.thread_id, 'task_id': 'task-1', 'objective': '明确目标'})
    record_closeout(agent, ctx)
    signal = _wake(store)
    store.goals.update({'thread_id': thread.thread_id, 'goal_id': goal.goal_id, 'status': goal_status})
    assert not claim_wake(agent, store, signal)


def test_new_input_invalidates_before_append_and_failure_stays_conservative(setup, monkeypatch):
    from agent_py_agent.agent.conversation import store_messages

    agent, store, thread, ctx = setup
    record_closeout(agent, ctx)
    signal = _wake(store)
    original_append = store_messages.append_private_jsonl_records_unlocked
    def failed_append(*args, **kwargs):
        assert not claim_wake(agent, store, signal)
        raise OSError('append failed after input invalidation')
    monkeypatch.setattr(store_messages, 'append_private_jsonl_records_unlocked', failed_append)
    with pytest.raises(OSError, match='append failed'):
        store.messages.append({'thread_id': thread.thread_id, 'role': 'user', 'content': '新输入'})
    monkeypatch.setattr(store_messages, 'append_private_jsonl_records_unlocked', original_append)
    assert not claim_wake(agent, store, signal)
    assert receipt_for(store.tasks.load('task-1')).status == 'void'


def test_replayed_write_is_not_material_progress(setup):
    agent, store, _, ctx = setup
    ctx.archive_tool_calls = [{'tool': 'write', 'ok': True, 'handler_executed': False}]
    assert record_closeout(agent, ctx).status == 'stopped'
    assert not store.wakes.pending(limit=20)


def test_exact_child_event_resets_cutoff_streak(setup):
    agent, store, thread, ctx = setup
    record_closeout(agent, ctx)
    assert claim_wake(agent, store, _wake(store))
    ctx.source, ctx.do_save, ctx.attempt_id = 'background_main_agent', False, 'attempt-2'
    ctx.archive_tool_calls = []
    assert record_closeout(agent, ctx).status == 'stopped'
    signal = store.wakes.raise_signal({'thread_id': thread.thread_id, 'root_task_id': 'task-1',
                                       'reason': 'subagent_runner_finished'})
    ctx.task_attributes['background_wake_signal_id'] = signal.wake_signal_id
    ctx.attempt_id = 'child-event-attempt'
    ctx.archive_tool_calls = [{'tool': 'write', 'ok': True, 'handler_executed': True}]
    receipt = record_closeout(agent, ctx)
    assert receipt.status == 'pending' and receipt.sequence == 1


def test_first_background_cutoff_requires_real_foreground_origin(setup):
    agent, store, thread, ctx = setup
    cutoff = ctx.final_response
    ctx.final_response = SimpleNamespace(runtime_status='ok', runtime_reason='', runtime_source='')
    assert record_closeout(agent, ctx) is None
    assert receipt_for(store.tasks.load('task-1')) is None
    assert not store.wakes.pending(limit=20)
    origin = store.tasks.load('task-1').host_user_origin
    assert origin['request_id'] == 'request-1'
    ctx.final_response = cutoff
    ctx.source, ctx.do_save, ctx.attempt_id = 'background_main_agent', False, 'first-background-cutoff'
    receipt = record_closeout(agent, ctx)
    assert receipt.status == 'pending' and receipt.sequence == 1
    assert receipt.origin_request_id == 'request-1'
    assert publish_pending(agent, store, 'task-1')
    assert claim_wake(agent, store, _wake(store))


def test_internal_background_cannot_invent_user_origin(setup):
    agent, store, _, ctx = setup
    ctx.source, ctx.do_save = 'background_main_agent', False
    assert record_closeout(agent, ctx) is None
    assert store.tasks.load('task-1').host_user_origin is None
    assert not store.wakes.pending(limit=20)


def test_receipt_commit_requires_durable_wake_and_replays_prepared_publication(setup, monkeypatch):
    from agent_py_agent.agent.conversation import store_wake_publication

    agent, store, _, ctx = setup
    original = store_wake_publication._install
    class PublicationCrash(BaseException):
        pass
    def crash(*args, **kwargs):
        raise PublicationCrash()
    monkeypatch.setattr(store_wake_publication, '_install', crash)
    with pytest.raises(PublicationCrash):
        record_closeout(agent, ctx)
    assert receipt_for(store.tasks.load('task-1')) is None
    assert not store.wakes.pending(limit=20)
    monkeypatch.setattr(store_wake_publication, '_install', original)
    receipt = record_closeout(agent, ctx)
    assert receipt.status == 'pending'
    recovered = _wake(store)
    assert record_closeout(agent, ctx) == receipt
    assert _wake(store).wake_signal_id == recovered.wake_signal_id
    assert claim_wake(agent, store, recovered)


def test_prepublished_wake_waits_for_receipt_commit_under_same_guard(setup, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from agent_py_agent.agent.conversation import technical_continuation
    from agent_py_agent.agent.conversation.runtime import _wake_signal_should_skip

    agent, store, _, ctx = setup
    published, release, consumer_started, consumer_finished = Event(), Event(), Event(), Event()
    original = technical_continuation._publish_receipt
    def hold(store, receipt):
        original(store, receipt)
        published.set()
        assert release.wait(3)
    monkeypatch.setattr(technical_continuation, '_publish_receipt', hold)
    def consume(signal):
        consumer_started.set()
        result = _wake_signal_should_skip(agent, store, signal, WAKE_REASON)
        consumer_finished.set()
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        producer = pool.submit(record_closeout, agent, ctx)
        try:
            assert published.wait(3)
            signal = _wake(store)
            consumer = pool.submit(consume, signal)
            assert consumer_started.wait(3)
            assert not consumer_finished.wait(0.05)
        finally:
            release.set()
        assert producer.result(timeout=3).status == 'pending'
        assert consumer.result(timeout=3) is False
    assert receipt_for(store.tasks.load('task-1')).status == 'claimed'


def test_claimed_receipt_survives_crash_until_original_execution_lease_expires(tmp_path):
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )

    agent, store, thread = _real_cutoff_setup(tmp_path)
    _run_first_cutoff(agent, thread, tmp_path)
    signal = _wake(store)
    assert claim_wake(agent, store, signal)
    claim = store.claims.acquire({'thread_id': thread.thread_id, 'task_id': 'task-real',
        'reason': WAKE_REASON, 'lease_seconds': 1, 'now': signal.created_at})
    assert claim is not None
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store)
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store})
    assert scheduler.tick_thread(thread.thread_id, now=signal.created_at + 0.5) == []
    assert store.wakes.pending_one(signal.wake_signal_id) is not None
    reports = scheduler.tick_thread(thread.thread_id, now=signal.created_at + 2)
    assert len(reports) == 1 and reports[0].delivery_status == 'sent'
    assert receipt_for(store.tasks.load('task-real')).status == 'finished'
    assert store.claims.load(thread.thread_id)['previous_claim']['claim_id'] == claim['claim_id']
