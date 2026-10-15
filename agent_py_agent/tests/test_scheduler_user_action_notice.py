"""认证拒绝通知沿原宿主事实、原后台领取和原冻结投递，不测试自然语言关键词。"""
from dataclasses import replace

import pytest

from agent_py_agent.agent.tooling.runtime_contracts import ToolResult


# LLM: 仅构造宿主typed回执替身，不提供秘密、真实HTTP或生产记录；归档和实际调度另走原入口测试。
# 函数用途: 为通知投影提供可独立破坏字段的准确认证拒绝事实。
def rejected_result(**changes):
    authentication = {
        'status': 'rejected', 'http_status': 401, 'record_updated': True,
        'user_action_required': True, 'credential_ref': 'cred_' + 'a' * 32, 'generation': 1,
    }
    authentication.update(changes)
    return ToolResult('call-auth', 'web_fetch', 'failed', error_code='PERMISSION_BLOCKED',
        handler_executed=True, metadata={'handler_details': {'http_status': 401,
            'credential_authentication': authentication, 'body': 'Synthetic-Secret-Canary'}})


def test_typed_rejection_survives_original_archive_projection():
    from agent_py_agent.agent.agent_core.tool_call_archive_record import _compact_result_envelope
    projection = _compact_result_envelope(rejected_result())
    assert projection['required_user_action']['credential_ref'] == 'cred_' + 'a' * 32
    assert projection['required_user_action']['generation'] == 1
    assert 'Synthetic-Secret-Canary' not in str(projection)


@pytest.mark.parametrize('change', [
    {'status': 'unverified'}, {'http_status': 403}, {'http_status': True},
    {'record_updated': False}, {'record_updated': 1}, {'user_action_required': False},
    {'user_action_required': 1}, {'credential_ref': '../escape'}, {'credential_ref': ''},
    {'generation': True}, {'generation': 0}, {'generation': 1.0}, {'generation': '1'},
])
def test_bad_or_stale_host_rejection_does_not_create_notice(change):
    from agent_py_agent.agent.conversation.required_user_action import required_action_from_result
    assert required_action_from_result(rejected_result(**change)) is None


@pytest.mark.parametrize('change', [
    {'handler_executed': False}, {'tool_name': 'untrusted_tool'}, {'status': 'succeeded'},
])
def test_only_executed_original_typed_tool_can_create_obligation(change):
    from agent_py_agent.agent.conversation.required_user_action import required_action_from_result
    assert required_action_from_result(replace(rejected_result(), **change)) is None


def test_model_dictionary_and_body_do_not_create_obligation():
    from agent_py_agent.agent.conversation.required_user_action import required_action_from_result
    assert required_action_from_result(rejected_result().to_dict()) is None
    assert required_action_from_result({'output': 'HTTP401 password rejected'}) is None


# LLM: 真SimpleAgent/Registry/Executor/scheduler tick；仅认证末端与收件运输、模型是合成替身，不接生产或外网。
# 函数用途: 复现失败跨真实工具、预检、原wake、自然表达和来源投递的完整路径。
def notice_case(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
    from agent_py_agent.tests.test_scheduler_execution import execution_case

    ref = 'cred_' + 'a' * 32
    case = execution_case(tmp_path, precheck={'tool_name': 'web_fetch',
        'arguments': {'url': 'https://example.com/api', 'credential_ref': ref}})
    case.calls, case.fresh, case.expression_hook = [], True, None

    def authenticate(agent, reference, request, *, send):
        assert reference == ref
        case.calls.append(request.method)
        return ToolHandlerOutcome('web_fetch', False, '合成认证拒绝', error_code='PERMISSION_BLOCKED',
            result_envelope={'http_status': 401, 'credential_authentication': {
                'status': 'rejected', 'http_status': 401, 'record_updated': True,
                'user_action_required': True, 'credential_ref': ref, 'generation': 1}}), ()

    def current(agent, reference, generation):
        assert agent._current_run_params.task_id == case.signal.root_task_id
        return case.fresh and reference == ref and generation == 1

    def generate(prompt, *args, **kwargs):
        case.backend.prompts.append(prompt)
        if case.expression_hook:
            case.expression_hook()
        return ModelResponse(text='这次认证被拒绝，请决定重新填写或停用这项认证；计划设置没有自动改变。', backend='synthetic')

    monkeypatch.setattr('agent_py_agent.agent.user_space.task_credential_http.credential_authenticated_request', authenticate)
    monkeypatch.setattr('agent_py_agent.agent.user_space.task_credentials.credential_rejection_current', current)
    case.agent.tools.tools['web_fetch'].credential_authenticator = lambda ref, request, send: authenticate(case.agent, ref, request, send=send)
    case.backend.generate = generate
    case.channels.status = 'sent'
    return case


def test_precheck_real_executor_failure_notifies_original_user_once(tmp_path, monkeypatch):
    case = notice_case(tmp_path, monkeypatch)
    case.scheduler.tick(now=1000)
    history, errors = case.agent.scheduler_repository.history()
    assert not errors and history[0]['status'] == 'failed'
    assert history[0]['delivery_status'] == 'sent'
    assert len(case.backend.prompts) == len(case.channels.attempts) == len(case.calls) == 1
    context, _, receipt = case.channels.attempts[0]
    assert context.thread_id == case.thread.thread_id and context.target == 'open-1'
    assert receipt.delivery_status == 'sent'
    case.scheduler.tick(now=1031)
    assert len(case.calls) == len(case.channels.attempts) == 1


def test_missing_optional_capability_keeps_ordinary_finalization(monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.required_user_action import (
        defer_required_action_completion,
        notice_enabled,
    )
    monkeypatch.setattr('agent_py_agent.agent.capability.runtime_config_reload.capability_config_for_agent', lambda agent: None)
    assert not notice_enabled(object())
    assert not defer_required_action_completion(object(), SimpleNamespace(source='background_main_agent', archive_tool_calls=[]))


# LLM: 使用原私人卡保存、原监控与原owner/actor，HTTP只替换末端运输；freshness绝不mock。
# 函数用途: 在真实凭据scope与调度tick之间检查通知不会错回管理员或跳过原任务权威。
def real_notice_case(tmp_path, monkeypatch, kind='user', *, work='precheck'):
    import time
    from types import SimpleNamespace

    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.conversation import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )
    from agent_py_agent.agent.tooling.web_fetch_runtime import RawResponseParts
    from agent_py_agent.tests.test_ingestion_task_credential_scope import (
        _active,
        _agent,
        _credential,
        _scheduled_execution,
    )
    from agent_py_agent.tests.test_scheduler_runtime import _Backend, _RetryDelivery

    agent = _agent(tmp_path, kind)
    agent.config.enable_tools = False
    thread, actor, _claim = _active(agent)
    job_id = _scheduled_execution(agent)
    reference = _credential(agent, 'https://example.com/api', job_id=job_id)
    repo = agent.scheduler_repository
    job = repo.get_job(job_id)
    arguments = {'url': 'https://example.com/api', 'credential_ref': reference}
    patch = {'delivery': {'mode': 'local'}}
    if work == 'precheck':
        patch['precheck'] = {'tool_name': 'web_fetch', 'arguments': arguments}
    elif work == 'no':
        patch['rules'] = [{'id': 'probe', 'name': '合成检查', 'use_llm': False,
            'tool': {'name': 'web_fetch', 'arguments': arguments}, 'schedule': job['schedule']}]
    repo.update_job(job_id, patch=patch, expected_version=job['version'])
    run = repo.reserve_manual_run(job_id)
    now = max(time.time(), run['dispatch_after']) + 1
    agent.scheduler_service.enqueue_ready_runs(now=now)
    signal = next(s for s in agent.conversation_store.wakes.pending() if s.root_task_id == run['run_id'])
    channels, backend = _RetryDelivery(), _Backend()
    channels.status = 'sent'
    agent.backend = backend
    case = SimpleNamespace(agent=agent, store=agent.conversation_store, thread=thread, actor=actor,
        signal=signal, job=job, channels=channels, backend=backend, calls=[], now=now,
        reference=reference, expression_hook=None, arguments=arguments)
    def send(request, **kwargs):
        case.calls.append(request.method)
        return RawResponseParts(401, {}, b'UnexpectedSecretCanary', request.url)
    def generate(prompt, **kwargs):
        backend.prompts.append(prompt)
        if case.expression_hook:
            case.expression_hook()
        return ModelResponse(text='认证被拒绝，请决定重新填写或停用这项认证。计划没有自动停用。', backend=backend.name)
    monkeypatch.setattr('agent_py_agent.agent.tooling.web_fetch_tools.fetch_raw_response', send)
    backend.generate = generate
    runtime = BackgroundMainAgentRuntime(agent=agent, store=case.store, channels=channels)
    case.scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': case.store,
        'scheduler_service': agent.scheduler_service})
    return case


@pytest.mark.parametrize('kind', ['user', 'group'])
@pytest.mark.parametrize('work', ['precheck', 'no'])
def test_real_credential_notice_overrides_local_only_to_exact_source(tmp_path, monkeypatch, kind, work):
    case = real_notice_case(tmp_path, monkeypatch, kind, work=work)
    case.scheduler.tick(now=case.now)
    history, errors = case.agent.scheduler_repository.history()
    assert not errors and history[0]['status'] == 'failed'
    assert history[0]['delivery_status'] == 'sent', history[0]['delivery_reason']
    assert len(case.calls) == len(case.backend.prompts) == len(case.channels.attempts) == 1
    context, envelope, receipt = case.channels.attempts[0]
    assert context.thread_id == case.thread.thread_id
    assert context.target == (case.agent.config.my_agent_owner_id if kind == 'group' else case.actor.actor_id)
    assert 'UnexpectedSecretCanary' not in envelope.content and receipt.delivery_status == 'sent'
    assert case.store.tasks.load(case.signal.root_task_id).status == 'abandoned'
    assert case.agent.scheduler_repository.get_job(case.job['job_id'])['status'] == 'active'


@pytest.mark.parametrize('kind', ['user', 'group'])
def test_llm_slice_defers_task_completion_and_reuses_its_public_reply(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability
    case = real_notice_case(tmp_path, monkeypatch, kind, work='llm')
    case.agent.config.enable_tools = True
    case.backend.probe_tool_capability = lambda: ProviderToolCapability(
        provider='synthetic', endpoint='local://test', model='test', stream=False,
        native_supported=True, evidence='synthetic')
    def generate(prompt, **kwargs):
        case.backend.prompts.append(prompt)
        if len(case.backend.prompts) == 1:
            return ModelResponse(text='', backend=case.backend.name, tool_use_blocks=[
                {'id': 'auth', 'name': 'web_fetch', 'input': case.arguments}])
        return ModelResponse(text='这项认证被拒绝，需要您决定重新填写或停用。', backend=case.backend.name)
    case.backend.generate = generate
    case.scheduler.tick(now=case.now)
    history, errors = case.agent.scheduler_repository.history()
    assert not errors and history[0]['delivery_status'] == 'sent'
    assert len(case.calls) == len(case.channels.attempts) == 1
    assert len(case.backend.prompts) == 2
    assert case.store.tasks.load(case.signal.root_task_id).status == 'abandoned'


@pytest.mark.parametrize('change', ['revoke', 'generation', 'disabled', 'cancelled', 'deleted', 'source'])
def test_notice_rechecks_after_expression_and_never_leaves_active_task(tmp_path, monkeypatch, change):
    from types import SimpleNamespace

    from agent_py_agent.agent.capability.config import CapabilityConfig
    from agent_py_agent.agent.common.json_io import read_json_object_report, write_json_file_atomic
    from agent_py_agent.agent.user_space.task_credentials import credential_path, manage_credential
    case = real_notice_case(tmp_path, monkeypatch)
    def change_state():
        if change == 'revoke':
            manage_credential(case.agent, case.reference, operation='revoke')
        elif change == 'generation':
            path = credential_path(case.agent.home_paths.owner_home_dir, case.reference)
            row = read_json_object_report(path, context='synthetic').payload
            write_json_file_atomic(path, {**row, 'generation': row['generation'] + 1})
        elif change == 'disabled':
            case.agent._capability_config_runtime_snapshot = SimpleNamespace(config=CapabilityConfig(
                background_user_action_notice_enabled=False))
        elif change == 'cancelled':
            case.store.tasks.update_status({'task_id': case.signal.root_task_id, 'status': 'cancelled', 'expected_status': 'active'})
        elif change == 'source':
            case.store.threads._create({'channel': 'feishu', 'channel_conversation_id': 'oc_private_synthetic',
                'channel_user_id': case.actor.actor_id, 'canonical_user_id': case.actor.actor_id,
                'owner_id': case.agent.home_paths.owner_id, 'owner_home': str(case.agent.home_paths.owner_home_dir)})
        else:
            repo = case.agent.scheduler_repository
            job = repo.get_job(case.job['job_id'])
            repo.delete_job(job['job_id'], expected_version=job['version'])
    case.expression_hook = change_state
    case.scheduler.tick(now=case.now)
    case.scheduler.tick(now=case.now + 60)
    assert len(case.calls) == len(case.backend.prompts) == 1
    assert not case.channels.attempts
    assert case.store.tasks.load(case.signal.root_task_id).status != 'active'


@pytest.mark.parametrize('retire', ['none', 'generation', 'disabled'])
def test_cold_outbox_retry_never_repeats_http_or_model(tmp_path, monkeypatch, retire):
    from agent_py_agent.agent.common.json_io import read_json_object_report, write_json_file_atomic
    from agent_py_agent.agent.conversation import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )
    from agent_py_agent.agent.user_space.task_credentials import credential_path
    case = real_notice_case(tmp_path, monkeypatch, 'group')
    case.channels.status = 'rejected'
    case.scheduler.tick(now=case.now)
    pending = case.store.wakes.pending_one(case.signal.wake_signal_id)
    assert pending.metadata['owner_delivery']['content']
    state, error = case.store.wakes.attempts.state_report(pending.wake_signal_id)
    assert error is None
    if retire == 'disabled':
        from types import SimpleNamespace

        from agent_py_agent.agent.capability.config import CapabilityConfig
        case.agent._capability_config_runtime_snapshot = SimpleNamespace(config=CapabilityConfig(
            background_user_action_notice_enabled=False))
    if retire == 'generation':
        path = credential_path(case.agent.home_paths.owner_home_dir, case.reference)
        row = read_json_object_report(path, context='synthetic').payload
        write_json_file_atomic(path, {**row, 'generation': row['generation'] + 1})
    case.channels.status = 'sent'
    runtime = BackgroundMainAgentRuntime(agent=case.agent, store=case.store, channels=case.channels)
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': case.store,
        'scheduler_service': case.agent.scheduler_service})
    scheduler.tick(now=state.next_attempt_at)
    assert len(case.calls) == len(case.backend.prompts) == 1
    assert len(case.channels.attempts) == (2 if retire == 'none' else 1)
    assert case.store.tasks.load(case.signal.root_task_id).status != 'active'
    history, errors = case.agent.scheduler_repository.history()
    assert not errors and history[0]['status'] == 'failed'
    assert history[0]['delivery_status'] == ('sent' if retire == 'none' else 'suppressed')


@pytest.mark.parametrize('status', ['cancelled', 'aborted'])
def test_expression_typed_cancellation_never_retries_or_sends(tmp_path, monkeypatch, status):
    from agent_py_agent.agent.backends import ModelResponse
    case = notice_case(tmp_path, monkeypatch)
    def generate(prompt, **kwargs):
        case.backend.prompts.append(prompt)
        return ModelResponse(text='', backend=case.backend.name, runtime_status=status, turn_end_reason='aborted')
    case.backend.generate = generate
    case.scheduler.tick(now=1000)
    assert len(case.calls) == len(case.backend.prompts) == 1
    assert not case.channels.attempts
    assert case.store.tasks.load(case.signal.root_task_id).status != 'active'


def test_already_sent_notice_completes_canonical_after_generation_change_without_resend(tmp_path, monkeypatch):
    from agent_py_agent.agent.common.json_io import read_json_object_report, write_json_file_atomic
    from agent_py_agent.agent.user_space.task_credentials import credential_path
    case = real_notice_case(tmp_path, monkeypatch)
    case.channels.status = 'rejected'
    case.scheduler.tick(now=case.now)
    pending = case.store.wakes.pending_one(case.signal.wake_signal_id)
    frozen = {**pending.metadata['owner_delivery'], 'external_sent': True,
        'receipt_id': 'original-synthetic-receipt'}
    # 复用原重投测试的崩溃窗口：已存在准确receipt但canonical尚未追加，只改临时原wake夹具。
    payload = pending.to_dict()
    payload['metadata'] = {**payload['metadata'], 'owner_delivery': frozen}
    write_json_file_atomic(case.store.wakes._find_path(pending.wake_signal_id), payload)
    path = credential_path(case.agent.home_paths.owner_home_dir, case.reference)
    row = read_json_object_report(path, context='synthetic').payload
    write_json_file_atomic(path, {**row, 'generation': row['generation'] + 1})
    state, error = case.store.wakes.attempts.state_report(pending.wake_signal_id)
    assert error is None
    case.scheduler.tick(now=state.next_attempt_at)
    assert len(case.calls) == len(case.backend.prompts) == len(case.channels.attempts) == 1
    assert case.store.wakes.pending_one(pending.wake_signal_id) is None
    assert case.store.tasks.load(case.signal.root_task_id).status == 'abandoned'
    messages = case.store.messages.recent(case.thread.thread_id, limit=0)
    assert sum(m.content == frozen['content'] for m in messages) == 1


def test_existing_delivery_give_up_closes_exact_notice_without_repeating_business(tmp_path, monkeypatch):
    case = real_notice_case(tmp_path, monkeypatch)
    case.channels.status = 'rejected'
    monkeypatch.setattr('agent_py_agent.agent.conversation.wake_poison.WAKE_REDELIVERY_GIVE_UP_SECONDS', 0)
    case.scheduler.tick(now=case.now)
    assert case.store.wakes.pending_one(case.signal.wake_signal_id) is None
    assert case.store.tasks.load(case.signal.root_task_id).status == 'abandoned'
    history, errors = case.agent.scheduler_repository.history()
    assert not errors and history[0]['status'] == 'failed'
    assert history[0]['delivery_status'] != 'sent'
    assert len(case.calls) == len(case.backend.prompts) == len(case.channels.attempts) == 1
    assert case.agent.scheduler_repository.get_job(case.job['job_id'])['status'] == 'active'


def test_expression_interrupt_preserves_original_cancelled_attempt_settlement(tmp_path, monkeypatch):
    case = notice_case(tmp_path, monkeypatch)
    statuses = []
    settle = __import__('agent_py_agent.agent.agent_core.runtime_mixin', fromlist=['_settle_main_agent_run_status'])._settle_main_agent_run_status
    def observe(agent, **kwargs):
        statuses.append(kwargs['facts'].runtime_status)
        return settle(agent, **kwargs)
    monkeypatch.setattr('agent_py_agent.agent.agent_core.runtime_mixin._settle_main_agent_run_status', observe)
    def interrupted(*args, **kwargs):
        raise InterruptedError('synthetic stop')
    monkeypatch.setattr('agent_py_agent.agent.agent_core._tool_loop_service._request_tool_loop_model_response', interrupted)
    case.scheduler.tick(now=1000)
    assert statuses[-1] == 'cancelled'
    assert not case.channels.attempts


@pytest.mark.parametrize('boundary', ['other_claim', 'wrong_thread', 'terminal'])
def test_quarantine_closeout_does_not_steal_authority_or_revive_terminal(tmp_path, monkeypatch, boundary):
    from agent_py_agent.agent.conversation.wake_domain_closeout import _close_required_notice
    case = real_notice_case(tmp_path, monkeypatch)
    case.channels.status = 'rejected'
    case.scheduler.tick(now=case.now)
    signal = replace(case.store.wakes.pending_one(case.signal.wake_signal_id),
        status='failed_permanently', handled_at=case.now + 1)
    repository = case.agent.scheduler_repository
    held = None
    if boundary == 'other_claim':
        held = repository.claim_run(signal.root_task_id, lease_seconds=60, now=case.now)
        assert held is not None
    elif boundary == 'wrong_thread':
        signal = replace(signal, thread_id='unrelated-thread')
    else:
        case.store.tasks.update_status({'task_id': signal.root_task_id, 'status': 'cancelled', 'expected_status': 'active'})
    before = repository.store_path.read_bytes()
    _close_required_notice(case.agent, signal)
    assert repository.store_path.read_bytes() == before
    link = case.store.tasks.load(signal.root_task_id)
    assert link.status == ('cancelled' if boundary == 'terminal' else 'active')
    assert len(case.calls) == len(case.backend.prompts) == len(case.channels.attempts) == 1


def test_new_closeout_failure_does_not_suppress_original_quarantine_notice(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation import wake_domain_closeout as closeout
    case = real_notice_case(tmp_path, monkeypatch)
    notices = []
    def failed(*args):
        raise OSError('synthetic write failure')
    monkeypatch.setattr(closeout, '_close_required_notice', failed)
    monkeypatch.setattr(closeout, '_log', failed)
    monkeypatch.setattr(closeout, '_notify', lambda *args: notices.append(args))
    closeout.close_out_quarantined_wake(case.agent, case.signal,
        SimpleNamespace(same_cause_count=5, reason_code='CHANNEL_UNAVAILABLE'))
    assert len(notices) == 1
