"""R3-R2: refuse unsupported plugin execution, including durable legacy input."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.guidance import (
    has_pending_request_guidance,
    inject_pending_guidance,
)
from agent_py_agent.agent.conversation.business_contracts import (
    BusinessContextIntent,
    BusinessExecutionContext,
)
from agent_py_agent.agent.conversation.models import ChannelBinding
from agent_py_agent.agent.conversation.runtime import BackgroundMainAgentRuntime
from agent_py_agent.agent.conversation.session_task_dispatch import (
    SessionTaskDispatchPlan,
    dispatch_session_task,
)
from agent_py_agent.agent.conversation.session_task_report import (
    report_task_result,
    result_report_key,
)
from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.plugin_host import composition
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot
from agent_py_agent.tests.test_mega_business_plugin_compatibility import scoped_authorized
from agent_py_agent.tests.test_plugin_host_gateway_composition import production_call
from agent_py_agent.tests.test_plugin_host_gateway_composition import (
    production_host as production_host,
)
from agent_py_agent.tests.test_plugin_host_v2_grants import http_request

DENIED_ACTIONS = {
    'background_agents.messages.send', 'tasks.submit', 'tasks.assign', 'tasks.follow_up',
    'workflows.run', 'workflow_runs.resume', 'subagents.messages.send',
    'background_agents.resume', 'background_agents.restore',
}
PAYLOADS = {
    'background_agents.messages.send': {'agent_id': 'unstarted-agent', 'text': 'work', 'input_kind': 'guidance'},
    'tasks.submit': {'space_id': 'unstarted-space', 'instruction': 'work'},
    'tasks.assign': {'agent_id': 'unstarted-agent', 'space_id': 'unstarted-space', 'instruction': 'work'},
    'tasks.follow_up': {'task_id': 'unstarted-task', 'instruction': 'work'},
    'workflows.run': {'workflow_ref': 'unstarted-workflow'},
    'workflow_runs.resume': {'workflow_run_id': 'unstarted-workflow-run'},
    'subagents.messages.send': {'run_id': 'unstarted-run', 'text': 'work'},
    'background_agents.resume': {'agent_id': 'unstarted-agent'},
    'background_agents.restore': {'agent_id': 'unstarted-agent'},
}


@pytest.mark.parametrize('action', sorted(DENIED_ACTIONS))
def test_plugin_execution_is_unready_http_direct_and_live_context(production_host, action):
    server, _, owner, *_ = production_host
    server.agent = SimpleNamespace(config=AgentConfig(business_spaces_enabled=True))
    backend = composition.business_port(server.plugin_host.composition.ports)
    if 'execute' in authorized_caps(action):
        production_call(production_host, 'users.local.create', {'user_id': 'alice'}, key='r2-create-alice-key', status=201)
    authorized = scoped_authorized(production_host, 'business.' + action,
        {**PAYLOADS[action], **({'expected_revision': 1} if action in {
            'background_agents.resume', 'background_agents.restore', 'workflow_runs.resume'} else {})},
        user='alice' if 'execute' in authorized_caps(action) else None, key='r2-refused-' + action.replace('.', '-'))
    before = sorted(str(path.relative_to(owner.root)) for path in owner.root.rglob('*'))
    assert backend.ready('business.' + action) is False
    with pytest.raises(HostAPIError) as direct:
        backend.call(authorized)
    assert (direct.value.code, direct.value.status, direct.value.outcome) == ('HOST_NOT_READY', 503, 'not_started')
    context, services = backend.context(authorized)
    assert not DENIED_ACTIONS.intersection(services.supported(context))
    with pytest.raises(HostAPIError) as projected:
        context.check(action, {'owner_id': context.owner_id})
    assert projected.value.code == 'HOST_NOT_READY'
    status, result = http_request(server, production_host[1], {
        'schema': 'plugin_host_call.v2', 'action': authorized.call.action,
        'payload': dict(authorized.call.payload), 'idempotency_key': 'r2-http-' + action.replace('.', '-'),
        **({'as_user': {'user_id': 'alice'}} if 'execute' in authorized_caps(action) else {})})
    assert status == 503 and result['error']['code'] == 'HOST_NOT_READY', result
    assert result['error']['outcome'] == 'not_started'
    assert sorted(str(path.relative_to(owner.root)) for path in owner.root.rglob('*')) == before


def authorized_caps(action):
    from agent_py_agent.agent.plugin_host.business_action_specs import business_action_spec
    return business_action_spec(action).capabilities


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))
    agent = SimpleAgent(AgentConfig(model_backend='echo', enable_tools=True,
        business_spaces_enabled=True, memory_path='memory.jsonl'), tmp_path / 'workspace')
    store, owner = agent.conversation_store, agent.home_paths.owner_id
    source = store.threads.create(owner_id=owner, owner_home=str(agent.home_paths.owner_home_dir))
    source = replace(source,
        metadata={'plugin_host_v2': {'installation_ref': 'original-installation', 'revision': 1, 'workspace_ref': 'default'}},
        channel_bindings=(ChannelBinding('plugin', 'original-installation', owner, owner, source.thread_id),))
    store.threads.write(source)
    target = store.threads.create(owner_id=owner, owner_home=str(agent.home_paths.owner_home_dir))
    calls = []
    native_run = agent.run

    def forbidden(*args, **kwargs):
        calls.append('model-admission')
        raise AssertionError('unsupported plugin work reached model admission')

    monkeypatch.setattr(agent, 'run', forbidden)
    return SimpleNamespace(agent=agent, store=store, owner=owner, source=source, target=target, calls=calls, native_run=native_run)


def queue_task(case):
    from agent_py_agent.agent.capability.config import CapabilityConfig
    outcome = dispatch_session_task(case.agent,
        SessionTaskDispatchPlan(case.source.thread_id, case.target.thread_id, 'must not execute'),
        case.target, CapabilityConfig(), business_context_intent=BusinessContextIntent(case.owner, 'space-one', None, None),
        business_context_factory=lambda tid: BusinessExecutionContext(case.owner, 'space-one', None, tid,
            0, None, (), (), 'business-context.v1'))
    assert outcome.ok, outcome.output
    task = case.store.session_tasks.require(json.loads(outcome.output)['task_id'])
    signal = next(w for w in case.store.wakes.pending(limit=0) if w.metadata.get('session_task_id') == task.task_id)
    return task, signal


def run_wake(case, signal):
    return BackgroundMainAgentRuntime(agent=case.agent, store=case.store).run_once({
        'thread_id': signal.thread_id, 'task_id': signal.root_task_id, 'reason': signal.reason,
        'wake_signal': signal.to_dict()})


def test_queued_plugin_task_closes_original_body_and_result_without_model(legacy):
    task, signal = queue_task(legacy)
    report = run_wake(legacy, signal)
    current = legacy.store.session_tasks.require(task.task_id)
    assert current.status == 'failed' and current.failure_code == 'HOST_NOT_READY'
    assert legacy.store.guidance.receipt(task.body_dedupe_key).status == 'rejected'
    assert legacy.store.guidance.receipt(result_report_key(current)) is not None
    assert report.runtime_status == 'failed' and report.runtime_reason == 'HOST_NOT_READY'
    assert report.wake_handled and report.tool_call_count == 0 and legacy.calls == []


@pytest.mark.parametrize('target_type', ['thread', 'agent_run'])
def test_pending_plugin_instruction_is_rejected_before_claim_and_probe(legacy, target_type):
    target_id = legacy.target.thread_id if target_type == 'thread' else 'child-run'
    entry = legacy.store.guidance.append_once({'target_type': target_type, 'target_id': target_id,
        'message': 'plugin instruction', 'sender': legacy.source.thread_id,
        'metadata': {'origin_kind': 'session_message', 'origin_thread_id': legacy.source.thread_id}},
        dedupe_key='plugin-instruction')
    params = SimpleNamespace(request_id='current-turn', run_id='child-run' if target_type == 'agent_run' else '', task_id='',
        context_scope='task_local' if target_type == 'agent_run' else 'default',
        task_attributes={'conversation_thread_id': legacy.target.thread_id, 'agent_thread_id': legacy.target.thread_id},
        tool_context=[], live_archive_state={})
    assert has_pending_request_guidance(legacy.agent, params) is False
    assert inject_pending_guidance(legacy.agent, params) is False
    assert legacy.store.guidance.receipt(entry.metadata['dedupe_key']).status == 'rejected'
    assert params.tool_context == [] and not getattr(params, 'active_turn_user_inputs', None)


def test_host_generated_terminal_result_from_plugin_thread_is_preserved(legacy):
    task, _signal = queue_task(legacy)
    # Reverse a separate canonical task so the report's source is a plugin thread.
    native = legacy.store.session_tasks.create(__import__(
        'agent_py_agent.agent.conversation.session_tasks', fromlist=['SessionTaskDraft']).SessionTaskDraft(
        legacy.target.thread_id, legacy.source.thread_id, 'previous native work'))
    legacy.store.session_tasks.advance(native.task_id, SessionTaskUpdate(status='accepted'))
    done = legacy.store.session_tasks.advance(native.task_id, SessionTaskUpdate(status='done', summary='historical result'))
    assert report_task_result(legacy.agent, done)
    params = SimpleNamespace(request_id='result-turn', task_id='', task_attributes={'conversation_thread_id': legacy.target.thread_id},
        tool_context=[], live_archive_state={}, tool_protocol_snapshot=make_test_protocol_snapshot())
    assert has_pending_request_guidance(legacy.agent, params)
    assert inject_pending_guidance(legacy.agent, params)
    assert legacy.store.guidance.receipt(result_report_key(done)).status == 'reserved'
    assert legacy.store.session_tasks.require(task.task_id).status == 'queued'


@pytest.mark.parametrize('failure', ['reject', 'advance', 'report'])
def test_interrupted_task_closeout_retries_only_bookkeeping(legacy, monkeypatch, failure):
    from agent_py_agent.agent.conversation import session_task_report
    task, signal = queue_task(legacy)
    if failure == 'report':
        service, method = session_task_report, 'report_task_result'
    elif failure == 'advance':
        service, method = legacy.store.session_tasks, 'advance'
    else:
        service, method = legacy.store.guidance, 'mark_status'
    original = getattr(service, method)
    remaining = [True]

    def interrupt(*args, **kwargs):
        if remaining[0]:
            remaining[0] = False
            if failure == 'report':
                assert original(*args, **kwargs)  # simulate a lost acknowledgement after durable publish
                return False
            raise OSError('injected closeout interruption')
        return original(*args, **kwargs)

    monkeypatch.setattr(service, method, interrupt)
    first = run_wake(legacy, signal)
    assert not first.wake_handled and legacy.calls == []
    second = run_wake(legacy, signal)
    assert second.wake_handled and legacy.calls == []
    current = legacy.store.session_tasks.require(task.task_id)
    assert current.status == 'failed' and current.failure_code == 'HOST_NOT_READY'
    assert legacy.store.guidance.receipt(task.body_dedupe_key).status == 'rejected'
    result = legacy.store.guidance.receipt(result_report_key(current))
    assert result is not None
    assert run_wake(legacy, signal).wake_handled
    assert legacy.store.guidance.receipt(result_report_key(current)).entry.guidance_id == result.entry.guidance_id


def test_only_exact_plugin_task_body_is_rejected(legacy):
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
    task, signal = queue_task(legacy)
    other = legacy.store.guidance.append_once({'target_type': 'thread', 'target_id': legacy.target.thread_id,
        'message': 'unrelated native input', 'sender': 'host'}, dedupe_key='unrelated-body')
    different = legacy.store.session_tasks.create(SessionTaskDraft(legacy.source.thread_id, legacy.target.thread_id, 'other'))
    assert run_wake(legacy, signal).wake_handled
    assert legacy.store.guidance.receipt(other.metadata['dedupe_key']).status == 'pending'
    assert legacy.store.session_tasks.require(different.task_id).status == 'queued'
    assert legacy.store.session_tasks.require(task.task_id).status == 'failed'


@pytest.mark.parametrize('state', ['pending', 'reserved', 'submitted', 'consumed'])
def test_old_plugin_message_wake_never_runs_and_preserves_uncertain_effects(legacy, state):
    entry = legacy.store.guidance.append_once({'target_type': 'thread', 'target_id': legacy.target.thread_id,
        'message': 'plugin instruction', 'sender': legacy.source.thread_id,
        'metadata': {'origin_kind': 'session_message', 'origin_thread_id': legacy.source.thread_id}},
        dedupe_key='legacy-message')
    if state != 'pending':
        assert legacy.store.guidance.claim_for_turn(entry, expected_turn_id='old-turn', attempt_id='old-attempt')
        entry = legacy.store.guidance.receipt('legacy-message').entry
    if state in {'submitted', 'consumed'}:
        legacy.store.guidance.submissions.mark_submitted('old-turn', [entry],
            attempt_id='old-attempt', provider_call_id='original-provider-call')
    if state == 'consumed':
        legacy.store.guidance.acknowledgements.consume_submitted('old-turn', [entry], provider_call_id='original-provider-call')
    signal = legacy.store.wakes.raise_signal({'thread_id': legacy.target.thread_id, 'reason': 'session_message',
        'dedupe_key': 'legacy-message-wake', 'metadata': {'origin_kind': 'session_message',
            'origin_thread_id': legacy.source.thread_id, 'message_dedupe_key': 'legacy-message'}})
    report = run_wake(legacy, signal)
    assert legacy.calls == [] and report.tool_call_count == 0 and report.runtime_reason == 'HOST_NOT_READY'
    assert report.wake_handled is (state in {'pending', 'consumed'})
    assert legacy.store.guidance.receipt('legacy-message').status == ('rejected' if state == 'pending' else state)


@pytest.mark.parametrize('kind', ['metadata-only', 'channel-only', 'wrong-owner', 'wrong-installation'])
def test_malformed_positive_plugin_source_never_falls_back_to_native(legacy, kind):
    from agent_py_agent.agent.conversation.host_actor import (
        HostActorError,
        is_plugin_business_source_thread,
    )
    row = legacy.source
    if kind == 'metadata-only':
        row = replace(row, channel_bindings=())
    elif kind == 'channel-only':
        row = replace(row, metadata={})
    elif kind == 'wrong-owner':
        row = replace(row, owner_id='foreign-owner')
    else:
        row = replace(row, metadata={'plugin_host_v2': {'installation_ref': 'different-installation'}})
    legacy.store.threads.write(row)
    with pytest.raises(HostActorError):
        is_plugin_business_source_thread(legacy.store, row.thread_id, owner_id=legacy.owner)


def test_source_classifier_uses_canonical_facts_not_thread_prefix_or_text(legacy):
    from agent_py_agent.agent.conversation.host_actor import is_plugin_business_source_thread
    from agent_py_agent.agent.conversation.models import ConversationThread
    from agent_py_agent.agent.conversation.store_threads import create_thread_atomic
    fake = create_thread_atomic(legacy.store.threads, ConversationThread(thread_id='thread-plugin-business-fake',
        canonical_user_id=legacy.owner, owner_id=legacy.owner, title='plugin_host_v2 original-installation'))
    assert is_plugin_business_source_thread(legacy.store, fake.thread_id, owner_id=legacy.owner) is False
    assert is_plugin_business_source_thread(legacy.store, legacy.source.thread_id, owner_id=legacy.owner) is True


@pytest.mark.parametrize('field', ['task_run_id', 'conversation_request_id'])
def test_accepted_unknown_or_foreign_execution_is_not_overwritten(legacy, field):
    task, signal = queue_task(legacy)
    current = legacy.store.session_tasks.advance(task.task_id,
        SessionTaskUpdate(status='accepted', **{field: 'original-live-execution'}))
    before = legacy.store.session_tasks._path(task.task_id).read_bytes()
    report = run_wake(legacy, signal)
    assert not report.wake_handled and legacy.calls == []
    assert legacy.store.session_tasks._path(task.task_id).read_bytes() == before
    assert legacy.store.guidance.receipt(task.body_dedupe_key).status == 'pending'
    assert legacy.store.session_tasks.require(task.task_id) == current


def test_accepted_reserved_body_requires_exact_recovery_claim_ownership(legacy):
    from agent_py_agent.agent.conversation.claim_execution_binding import (
        ClaimExecutionBinding,
        bind_claim_execution,
    )
    task, signal = queue_task(legacy)
    legacy.store.session_tasks.advance(task.task_id,
        SessionTaskUpdate(status='accepted', conversation_request_id=task.task_id))
    body = legacy.store.guidance.receipt(task.body_dedupe_key).entry
    assert legacy.store.guidance.claim_for_turn(body, expected_turn_id=task.task_id,
        attempt_id='original-attempt', owning_task_id=task.task_id)
    assert not run_wake(legacy, signal).wake_handled
    assert legacy.store.guidance.receipt(task.body_dedupe_key).status == 'reserved'
    old = legacy.store.claims.acquire({'thread_id': legacy.target.thread_id, 'task_id': task.task_id, 'reason': 'session_task'})
    assert legacy.store.claims.finish({'thread_id': legacy.target.thread_id, 'claim_id': old['claim_id'], 'status': 'failed'})
    recovery = legacy.store.claims.acquire({'thread_id': legacy.target.thread_id, 'reason': 'session_task'})
    with bind_claim_execution(ClaimExecutionBinding(legacy.store.claims, legacy.target.thread_id, recovery['claim_id'], '')):
        report = run_wake(legacy, signal)
    assert report.wake_handled and legacy.calls == []
    assert legacy.store.guidance.receipt(task.body_dedupe_key).status == 'rejected'
    assert legacy.store.session_tasks.require(task.task_id).status == 'failed'


def test_historical_terminal_task_result_and_execution_refs_are_preserved(legacy):
    task, signal = queue_task(legacy)
    legacy.store.session_tasks.advance(task.task_id, SessionTaskUpdate(status='accepted', task_run_id='historical-run'))
    ended = legacy.store.session_tasks.advance(task.task_id,
        SessionTaskUpdate(status='done', summary='actual historical result', result_refs=('artifact-old',)))
    before = legacy.store.session_tasks._path(task.task_id).read_bytes()
    report = run_wake(legacy, signal)
    assert report.wake_handled and report.task_status == 'done' and legacy.calls == []
    assert legacy.store.session_tasks._path(task.task_id).read_bytes() == before
    assert legacy.store.guidance.receipt(result_report_key(ended)).entry.metadata['session_task_status'] == 'done'


# LLM: 原生回合仍走完整Agent执行层；测试显式供应echo后端，不依赖未配置Home的自动模型回退。
# 函数用途: 确认插件停用边界不会误挡原生用户的后台执行。
def test_native_owner_background_execution_still_reaches_real_agent(legacy, monkeypatch):
    from agent_py_agent.agent.backends import get_backend

    legacy.agent.config.enable_tools = False
    legacy.agent.backend = get_backend('echo', legacy.agent.config)
    calls = []

    def native(*args, **kwargs):
        calls.append('native')
        return legacy.native_run(*args, **kwargs)

    monkeypatch.setattr(legacy.agent, 'run', native)
    report = BackgroundMainAgentRuntime(agent=legacy.agent, store=legacy.store).run_once({
        'thread_id': legacy.target.thread_id, 'reason': 'scheduled_progress_report'})
    assert calls == ['native'] and report.response and report.runtime_reason != 'HOST_NOT_READY'


def test_exact_wake_finishes_only_after_durable_task_result(legacy):
    import time

    from agent_py_agent.agent.conversation.runtime import _complete_wake_report
    task, signal = queue_task(legacy)
    unrelated = legacy.store.wakes.raise_signal({'thread_id': legacy.target.thread_id,
        'reason': 'session_message', 'dedupe_key': 'unrelated-wake'})
    report = run_wake(legacy, signal)
    scheduler = SimpleNamespace(store=legacy.store, _wake_retry_after={}, _quota_fallback_wakes=set(), scheduler_service=None)
    assert _complete_wake_report(scheduler, signal, replace(report, wake_handled=False),
        lifecycle_reason='session_task', now=time.time()) is None
    assert legacy.store.wakes.pending_one(signal.wake_signal_id) is not None
    assert _complete_wake_report(scheduler, signal, report, lifecycle_reason='session_task', now=time.time()) is report
    assert legacy.store.wakes.pending_one(signal.wake_signal_id) is None
    assert legacy.store.wakes.pending_one(unrelated.wake_signal_id) is not None
    assert legacy.store.guidance.receipt(result_report_key(legacy.store.session_tasks.require(task.task_id))) is not None


@pytest.mark.parametrize('revoked', [False, True])
@pytest.mark.parametrize('production_host', [{'max_tool_calls': 1}], indirect=True)
def test_original_http_installation_legacy_dispatch_cannot_execute_even_if_revoked(production_host, revoked):
    from agent_py_agent.agent.capability.config import CapabilityConfig
    from agent_py_agent.tests.test_mega_business_plugin_compatibility import (
        revoke_business_installation,
    )

    production_call(production_host, 'users.local.create', {'user_id': 'alice'}, key='legacy-alice-create', status=201)
    server, _, owner, *_ = production_host
    server.agent = SimpleNamespace(config=AgentConfig(model_backend='echo', business_spaces_enabled=True))
    space = production_call(production_host, 'business.spaces.create', {'name': 'legacy'},
        user='alice', key='legacy-space-create')
    params = {'space_id': space['space_id'], 'name': 'original plugin target'}
    created = production_call(production_host, 'business.background_agents.create', params,
        user='alice', key='legacy-target-create')
    authorized = scoped_authorized(production_host, 'business.background_agents.create', params,
        user='alice', key='legacy-target-create')
    backend = composition.business_port(server.plugin_host.composition.ports)
    context, services = backend.context(authorized)
    actor = context.actor_factory('background_agents.create')
    store, host = services.host.conversation_store, services.host
    target = store.threads.require(created['agent']['thread_id'])
    source = store.threads.require(actor.source_thread_id)
    assert authorized.binding.grant['execution']['max_tool_calls'] == 1
    assert set(authorized.binding.grant['execution']['tool_names']) == {'read_file', 'write_file'}
    assert source.metadata['plugin_host_v2']['installation_ref'] == authorized.binding.installation_ref
    assert host.home_paths.owner_id.endswith('/alice')
    outcome = dispatch_session_task(host, SessionTaskDispatchPlan(source.thread_id, target.thread_id,
        'Read an owner-home file outside workspace, then invoke nongranted list_files'), target, CapabilityConfig(),
        business_context_intent=BusinessContextIntent(services.owner_id, space['space_id'], None, None),
        business_context_factory=lambda tid: BusinessExecutionContext(services.owner_id, space['space_id'], None,
            tid, 0, None, (), (), 'business-context.v1'))
    assert outcome.ok, outcome.output
    task = store.session_tasks.require(json.loads(outcome.output)['task_id'])
    signal = store.wakes.pending_one(json.loads(outcome.output)['wake_signal_id'])
    calls = []

    def fail_model(*args, **kwargs):
        calls.append('model')
        raise AssertionError('legacy plugin work entered model')

    host.run = fail_model
    if revoked:
        revoke_business_installation(production_host)
    case = SimpleNamespace(agent=host, store=store)
    report = run_wake(case, signal)
    assert report.wake_handled and report.runtime_reason == 'HOST_NOT_READY'
    assert report.tool_call_count == 0 and calls == []
    current = store.session_tasks.require(task.task_id)
    assert current.status == 'failed' and current.failure_code == 'HOST_NOT_READY'
    assert store.guidance.receipt(task.body_dedupe_key).status == 'rejected'
    assert not (owner.root / 'owners/local/alice/leaked-owner-data.txt').exists()


@pytest.mark.parametrize('mismatch', ['target', 'sender_owner', 'business_owner'])
def test_canonical_task_scope_mismatch_is_not_closed_or_executed(legacy, mismatch):
    task, signal = queue_task(legacy)
    if mismatch == 'target':
        signal = replace(signal, thread_id=legacy.source.thread_id)
    elif mismatch == 'sender_owner':
        legacy.store.threads.write(replace(legacy.source, owner_id='foreign-owner'))
    else:
        from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic
        def replace_owner(data):
            data['metadata']['business_context']['owner_id'] = 'foreign-owner'
            return data
        update_json_file_atomic(legacy.store.session_tasks._path(task.task_id), replace_owner, require_existing=True)
    before = legacy.store.session_tasks._path(task.task_id).read_bytes()
    with pytest.raises(PermissionError):
        run_wake(legacy, signal)
    assert legacy.calls == [] and legacy.store.session_tasks._path(task.task_id).read_bytes() == before
    assert legacy.store.guidance.receipt(task.body_dedupe_key).status == 'pending'


@pytest.mark.parametrize('entry_target', ['thread', 'agent_run'])
def test_conflicting_guidance_sender_and_origin_is_never_injected(legacy, entry_target):
    entry = legacy.store.guidance.append_once({'target_type': entry_target,
        'target_id': legacy.target.thread_id if entry_target == 'thread' else 'child-run',
        'message': 'conflicting plugin source', 'sender': legacy.source.thread_id,
        'metadata': {'origin_kind': 'session_message', 'origin_thread_id': legacy.target.thread_id}},
        dedupe_key='conflicting-source')
    params = SimpleNamespace(request_id='turn', task_id='', run_id='child-run',
        context_scope='task_local' if entry_target == 'agent_run' else 'default',
        task_attributes={'conversation_thread_id': legacy.target.thread_id, 'agent_thread_id': legacy.target.thread_id},
        tool_context=[], live_archive_state={})
    assert not inject_pending_guidance(legacy.agent, params)
    assert not has_pending_request_guidance(legacy.agent, params)
    assert legacy.store.guidance.receipt(entry.metadata['dedupe_key']).status == 'rejected'
    assert params.tool_context == []


@pytest.mark.parametrize('target_type', ['thread', 'agent_run'])
def test_pending_probe_keeps_native_input_older_than_refused_plugin_input(legacy, target_type):
    target = legacy.target.thread_id if target_type == 'thread' else 'child-run'
    native = legacy.store.guidance.append_once({'target_type': target_type, 'target_id': target,
        'message': 'ordinary native input', 'sender': 'host'}, dedupe_key='older-native')
    plugin = legacy.store.guidance.append_once({'target_type': target_type, 'target_id': target,
        'message': 'refused plugin input', 'sender': legacy.source.thread_id,
        'metadata': {'origin_kind': 'session_message', 'origin_thread_id': legacy.source.thread_id}},
        dedupe_key='newer-plugin')
    params = SimpleNamespace(request_id='mixed-turn', task_id='', run_id='child-run',
        context_scope='task_local' if target_type == 'agent_run' else 'default',
        task_attributes={'conversation_thread_id': legacy.target.thread_id, 'agent_thread_id': legacy.target.thread_id},
        tool_context=[], live_archive_state={}, tool_protocol_snapshot=make_test_protocol_snapshot())
    assert has_pending_request_guidance(legacy.agent, params)
    assert inject_pending_guidance(legacy.agent, params)
    assert legacy.store.guidance.receipt(native.metadata['dedupe_key']).status == 'reserved'
    assert legacy.store.guidance.receipt(plugin.metadata['dedupe_key']).status == 'rejected'


@pytest.mark.parametrize('entrypoint', ['wake', 'thread', 'agent_run'])
def test_missing_declared_canonical_guidance_source_is_never_native(legacy, monkeypatch, entrypoint):
    target_type = 'agent_run' if entrypoint == 'agent_run' else 'thread'
    entry = legacy.store.guidance.append_once({'target_type': target_type,
        'target_id': 'child-run' if target_type == 'agent_run' else legacy.target.thread_id,
        'message': 'orphaned plugin input', 'sender': legacy.source.thread_id,
        'metadata': {'origin_kind': 'session_message', 'origin_thread_id': legacy.source.thread_id}},
        dedupe_key='orphaned-input')
    original = legacy.store.threads.load_report

    def missing_source(thread_id):
        return (None, None) if thread_id == legacy.source.thread_id else original(thread_id)

    monkeypatch.setattr(legacy.store.threads, 'load_report', missing_source)
    if entrypoint == 'wake':
        signal = legacy.store.wakes.raise_signal({'thread_id': legacy.target.thread_id, 'reason': 'session_message',
            'metadata': {'message_dedupe_key': 'orphaned-input', 'origin_thread_id': legacy.source.thread_id}})
        assert run_wake(legacy, signal).runtime_reason == 'HOST_NOT_READY'
    else:
        params = SimpleNamespace(request_id='orphan-turn', task_id='', run_id='child-run',
            context_scope='task_local' if target_type == 'agent_run' else 'default',
            task_attributes={'conversation_thread_id': legacy.target.thread_id, 'agent_thread_id': legacy.target.thread_id},
            tool_context=[], live_archive_state={})
        assert not has_pending_request_guidance(legacy.agent, params)
        assert not inject_pending_guidance(legacy.agent, params)
    assert legacy.store.guidance.receipt(entry.metadata['dedupe_key']).status == 'rejected'
    assert legacy.calls == []


@pytest.mark.parametrize('other_status', ['pending', 'reserved'])
def test_reserved_task_closeout_does_not_settle_other_inputs_on_same_turn(legacy, other_status):
    from agent_py_agent.agent.conversation.claim_execution_binding import (
        ClaimExecutionBinding,
        bind_claim_execution,
    )

    task, signal = queue_task(legacy)
    task = legacy.store.session_tasks.advance(task.task_id,
        SessionTaskUpdate(status='accepted', conversation_request_id=task.task_id))
    body = legacy.store.guidance.receipt(task.body_dedupe_key).entry
    assert legacy.store.guidance.claim_for_turn(body, expected_turn_id=task.task_id,
        attempt_id='original-attempt', owning_task_id=task.task_id)
    other = legacy.store.guidance.append_once({'target_type': 'thread', 'target_id': legacy.target.thread_id,
        'message': 'unrelated original native input', 'sender': 'host',
        'metadata': {'expected_turn_id': task.task_id}}, dedupe_key='same-turn-native')
    if other_status == 'reserved':
        assert legacy.store.guidance.claim_for_turn(other, expected_turn_id=task.task_id,
            attempt_id='original-attempt')
    before = legacy.store.guidance.receipt('same-turn-native')
    old = legacy.store.claims.acquire({'thread_id': legacy.target.thread_id, 'task_id': task.task_id})
    assert legacy.store.claims.finish({'thread_id': legacy.target.thread_id, 'claim_id': old['claim_id'], 'status': 'failed'})
    current = legacy.store.claims.acquire({'thread_id': legacy.target.thread_id})
    with bind_claim_execution(ClaimExecutionBinding(legacy.store.claims, legacy.target.thread_id, current['claim_id'], '')):
        report = run_wake(legacy, signal)
    assert legacy.store.guidance.receipt('same-turn-native') == before
    assert not report.wake_handled and legacy.calls == []
    assert legacy.store.guidance.receipt(task.body_dedupe_key).status == 'reserved'
    assert legacy.store.session_tasks.require(task.task_id).status == 'accepted'


def test_foreign_current_claim_cannot_close_plugin_task(legacy):
    from agent_py_agent.agent.conversation.claim_execution_binding import (
        ClaimExecutionBinding,
        bind_claim_execution,
    )

    task, signal = queue_task(legacy)
    current = legacy.store.claims.acquire({'thread_id': legacy.target.thread_id, 'task_id': 'other-root'})
    with bind_claim_execution(ClaimExecutionBinding(legacy.store.claims, legacy.target.thread_id,
            current['claim_id'], '')):
        report = run_wake(legacy, signal)
    assert not report.wake_handled and legacy.calls == []
    assert legacy.store.guidance.receipt(task.body_dedupe_key).status == 'pending'
    assert legacy.store.session_tasks.require(task.task_id).status == 'queued'
