"""普通实际C/W runner与BG安全点；模型边界fake，原core/claim/guidance提交账和来源不替换。"""
import json
from dataclasses import replace
from uuid import uuid4

import pytest

from agent_py_agent.agent.agent_core import runtime_mixin as core
from agent_py_agent.agent.agent_core.runtime import guidance
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.conversation import background_claim as claims
from agent_py_agent.agent.conversation import background_execution as execution
from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
from agent_py_agent.agent.gateway_parts.input_guidance_payload import input_guidance_payload
from agent_py_agent.agent.gateway_parts.input_transport import validated_input_transport
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_admission import session_scope_for_request
from agent_py_agent.agent.gateway_parts.multiprocess_background_request import (
    enqueue_source,
    freeze_source,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_claim import (
    freeze_coordinator_claim,
)
from agent_py_agent.agent.gateway_parts.queue_service import claim_request
from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_background_claim_transport_reference import (
    actual as actual,
)
from agent_py_agent.tests.test_background_claim_transport_reference import (
    c_actual as c_actual,
)
from agent_py_agent.tests.test_background_claim_transport_reference import (
    case as case,
)
from agent_py_agent.tests.test_background_claim_transport_reference import (
    context as context,
)
from agent_py_agent.tests.test_background_claim_transport_reference import (
    gateway_scope,
    install_core,
)
from agent_py_agent.tests.test_background_claim_transport_reference import (
    transport_case as transport_case,
)
from agent_py_agent.tests.test_background_execution_ports import claimed
from agent_py_agent.tests.test_gateway_c_http_wait import c_http
from agent_py_agent.tests.test_gateway_c_http_wait import request as http_request
from agent_py_agent.tests.test_runtime_guidance import _tool_loop_params


# LLM: 使用原input prepare及实际producer已发布reference构造合法v2数据；数据不创建BG source，消费者来自原binder。
# 函数用途: 为实际runner安全点准备一条同owner/线程的普通输入与查找投影。
def prepared_input(case, bound, *, message='ordinary-bg-input'):
    _origin, gateway_claim, agent, store, thread, *_ = case
    claim = store.claims.load(thread.thread_id)
    paths = bound.pending_turn_input_source.paths
    transport = bound.pending_turn_input_source._transport(claim)[0]
    from agent_py_agent.agent.gateway_parts.request_owner_binding import (
        REQUEST_OWNER_KEY,
        frozen_request_owner,
    )
    owner = frozen_request_owner(read_json_file(gateway_claim.path))
    from dataclasses import asdict

    from agent_py_agent.agent.gateway_parts.control_service import (
        GatewayControlScope,
        active_turn_guidance_dedupe_key,
    )
    scope = GatewayControlScope(user_id='owner', channel='internal', conversation_id='transport',
        resolved_owner=owner, metadata={'message_id': message})
    payload = {'id': message, 'kind': 'ask', 'goal': '补充这条普通说明', 'user_id': 'owner',
        REQUEST_OWNER_KEY: asdict(owner), 'conversation': {'enabled': True, 'channel': 'internal',
        'channel_user_id': 'owner', 'channel_conversation_id': 'transport', 'canonical_user_id': 'owner'},
        'metadata': {'channel': 'internal', 'message_id': message, 'client_input_digest': 'digest-' + message}}
    with inputs.gateway_input_transition(paths, message):
        receipt, created = inputs.load_or_prepare_gateway_input_locked(paths, request_id=message,
            client_input_digest='digest-' + message, client_message_id=message,
            guidance_dedupe_key=active_turn_guidance_dedupe_key(scope), prepared_request=payload)
        assert created
        receipt = replace(receipt, transport=transport)
        write_json_file_atomic(inputs.gateway_input_receipt_path(paths, message), receipt.to_dict())
        inputs._write_active_turn_ref(paths, receipt, target_turn_id=bound.request_id)
    return receipt


# LLM: 原task/domain writer创建link，原core发布真实run/attempt；fake仅拦模型请求，不直接调用BG binder。
# 函数用途: 安装可调用真实安全点的模型边界。
def model_runner(case, tmp_path, monkeypatch, action):
    _origin, _claim, agent, store, thread, *_ = case
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'ports-task', 'status': 'active'})
    agent.subagents = type('ModelRuntime', (), {'runtime_db': RuntimeRepository(tmp_path / 'bg-runtime.sqlite3')})()
    monkeypatch.setattr(core, 'attach_run_task_workspace_context', lambda _agent, params, _prompt: params)
    def run(prompt, *, params):
        bound = core._bind_main_agent_turn_params(agent, prompt, params)
        return action(bound)
    monkeypatch.setattr(agent, 'run', run, raising=False)
    return RunParams(request_id='bg-invocation', run_id='bg-invocation', task_id='ports-task')


# LLM: ToolLoop参数承接同实际bound source/callback/attempt，随后调用原guidance reserve/submit/ACK，不自己标consumed。
# 函数用途: 将实际core参数交给原模型安全点。
def loop_params(bound, thread):
    return _tool_loop_params(request_id=bound.request_id, run_id=bound.run_id, task_id=bound.task_id,
        attempt_id=bound.attempt_id, task_attributes={'conversation_thread_id': thread.thread_id},
        active_turn_transition_callback=bound.active_turn_transition_callback,
        pending_turn_input_source=bound.pending_turn_input_source)


@pytest.mark.parametrize('close_only', [False, True])
def test_actual_c_w_bg_runner_safe_point_ack_or_final_queue(transport_case, tmp_path, monkeypatch, close_only):
    kind, case = transport_case
    _origin, gateway_claim, agent, store, thread, *_ = case
    # 原fixture C thread叫ports，W叫transport；都给同一线程增加原真实通道绑定。
    thread = store.threads.get_or_create({'canonical_user_id': 'owner', 'channel': 'internal',
        'channel_conversation_id': 'transport', 'channel_user_id': 'owner'})
    case = (*case[:4], thread, *case[5:])
    seen = []
    def action(bound):
        receipt = prepared_input(case, bound)
        seen.append(receipt)
        assert validated_input_transport(receipt.transport) == receipt.transport
        assert receipt.transport['gateway_claim']['request_id'] == gateway_claim.request_id
        assert receipt.transport['target']['turn_id'] == bound.request_id != gateway_claim.request_id
        assert input_guidance_payload(receipt)['target_type'] == 'task'
        params = loop_params(bound, thread)
        if not close_only:
            assert guidance.has_pending_turn_input(agent, params)
            assert guidance.inject_pending_turn_input(agent, params)
            assert len(params.active_turn_user_inputs) == 1
            assert not guidance.has_pending_turn_input(agent, params)
            assert guidance.mark_injected_turn_input_submitted(agent, params, provider_call_id='ordinary-fake-call') == 1
            assert guidance.acknowledge_injected_turn_input(agent, params) == 1
        return 'ordinary-model-result'
    params = model_runner(case, tmp_path, monkeypatch, action)
    deps, args, row, _policy = claimed(case, lambda _: execution._run_background_model_attempt(agent, '普通后台说明', params)[0])
    with gateway_scope(kind, case):
        assert claims.run_with_heartbeat(deps, row['claim_id'], args, claim_scope_id=row['claim_scope_id']) == 'ordinary-model-result'
        assert read_json_file(gateway_claim.path)['turn_phase'] == 'open'
    assert store.claims.load(thread.thread_id)['status'] == 'finished'
    receipt = inputs.read_gateway_input_receipt(bound_paths(case), seen[0].request_id)
    if close_only:
        assert receipt.state == 'queued'
        assert read_json_file(bound_paths(case).inbox / (receipt.request_id + '.json')) == receipt.prepared_request
    else:
        row = store.guidance.receipt(receipt.guidance_dedupe_key)
        assert row.status == 'consumed' and receipt.target_turn_id == 'bg-invocation'


# LLM: 路径只从原Gateway claim定位，不成为执行来源或权限。
# 函数用途: 读取测试实际receipt的canonical目录。
def bound_paths(case):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    return gateway_paths_from_root(case[1].path.parent.parent.parent)


@pytest.mark.parametrize('heartbeat', [False, True])
def test_actual_c_http_bg_reference_then_safe_point(tmp_path, monkeypatch, heartbeat):
    with c_http(tmp_path, monkeypatch) as (agent, _backend, gate, _control, _runtime, server, _close):
        store = agent.conversation_store
        thread = store.threads.get_or_create({'canonical_user_id': 'local-agent', 'channel': 'chat',
            'channel_conversation_id': 'bg-http', 'channel_user_id': 'local-agent'})
        store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'ports-task', 'status': 'active'})
        wake = store.wakes.raise_signal({'thread_id': thread.thread_id, 'reason': 'ordinary-test', 'summary': '继续普通任务'})
        ref = freeze_source(agent.home_paths.root, OwnerIdentity.local_main(), store.storage.wake_signal_path(wake), 'wake')
        path, _ = enqueue_source(gate.paths, agent.home_paths.root, ref)
        original = read_json_file(path)
        decision = gate.admit(path, (OwnerIdentity.local_main(), session_scope_for_request(original)), AdmissionLimits(8, 8))
        assert decision['status'] == 'admitted', decision
        gateway_claim = claim_request(gate.paths, path, coordinator_snapshot=freeze_coordinator_claim(gate, decision))
        case = (gate, gateway_claim, agent, store, thread, None)
        receipts, observations = [], []
        if heartbeat:
            from agent_py_agent.agent.gateway_parts import multiprocess_input_receipt as producer
            observe = producer.observe_input_target
            def renewed(*args, **kwargs):
                result = observe(*args, **kwargs)
                assert result[0] == 'exact'
                observations.append(True)
                current = store.claims.load(thread.thread_id)
                store.claims.renew({'thread_id': thread.thread_id, 'claim_scope_id': current['claim_scope_id'],
                    'claim_id': current['claim_id'], 'lease_seconds': 31})
                return result
            monkeypatch.setattr(producer, 'observe_input_target', renewed)
        def action(bound):
            code, result = http_request(server, 'POST', '/ask', {'goal': '再加一条说明', 'conversation_id': 'bg-http',
                'metadata': {'message_id': 'bg-real-http'}}, headers={'X-User-Id': 'local-agent', 'X-Channel': 'chat'})
            assert code == 202, result
            receipt = inputs.read_gateway_input_receipt(gate.paths, result['request_id'])
            receipts.append(receipt)
            if heartbeat:
                assert observations == [True] and receipt.transport is None and receipt.state == 'pending'
                assert not (gate.paths.inbox / (receipt.request_id + '.json')).exists()
                return 'done'
            assert receipt.transport is not None, result
            assert receipt.transport['schema_version'] == 'gateway_input_transport.v2', result
            loop = loop_params(bound, thread)
            assert guidance.inject_pending_turn_input(agent, loop)
            assert guidance.mark_injected_turn_input_submitted(agent, loop, provider_call_id='bg-http-fake') == 1
            assert guidance.acknowledge_injected_turn_input(agent, loop) == 1
            return 'done'
        params = model_runner(case, tmp_path, monkeypatch, action)
        deps, args, row, _policy = claimed(case, lambda _: execution._run_background_model_attempt(agent, '正常后台', params)[0])
        store.claims.renew({'thread_id': thread.thread_id, 'claim_scope_id': row['claim_scope_id'],
                            'claim_id': row['claim_id'], 'lease_seconds': 30})
        with gateway_scope('C', case):
            assert claims.run_with_heartbeat(deps, row['claim_id'], args, claim_scope_id=row['claim_scope_id']) == 'done'
        assert len(receipts) == 1 and store.claims.load(thread.thread_id)['status'] == 'finished'


def test_actual_c_bg_compact_keeps_frozen_attempt_and_final_queue(tmp_path, monkeypatch):
    from functools import partial

    from agent_py_agent.agent.backends.errors import ProviderContextWindowError
    from agent_py_agent.agent.conversation import compact
    from agent_py_agent.agent.conversation.agent_activity import BackgroundMainActivitySink
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params
    from agent_py_agent.tests import test_gateway_background_curator_transactions as fixture_module
    from agent_py_agent.tests.test_subagent_compact_recovery import _http
    original_config = fixture_module.AgentConfig
    def config(**kwargs):
        return original_config(**(kwargs | {'model_backend': 'anthropic_compatible', 'model_name': 'MiniMax-M2.7',
            'api_base': 'https://api.minimaxi.com/anthropic', 'api_key': 'offline-fake-key',
            'stream_enabled': False, 'enable_tools': False, 'max_tool_rounds': 1,
            'model_context_window_tokens': 200_000}))
    monkeypatch.setattr(fixture_module, 'AgentConfig', config)
    with c_http(tmp_path, monkeypatch) as (agent, _backend, gate, _control, _runtime, server, _close):
        # 有 Home 时模型只由 owner 的 v6 目录决定：把同一套离线 MiniMax 连接登记为目录默认模型，传输仍由 _http 替身接住。
        profile_id = str(uuid4())
        execute_model_profile_operation(agent, 'add', {'profile_id': profile_id, 'profile': {
            'model_backend': 'anthropic_compatible', 'model_name': 'MiniMax-M2.7',
            'api_base': 'https://api.minimaxi.com/anthropic', 'api_key': 'offline-fake-key',
            'model_context_window_tokens': 200_000}})
        execute_model_profile_operation(agent, 'set_default', {'profile_id': profile_id})
        store = agent.conversation_store
        thread = store.threads.get_or_create({'canonical_user_id': 'local-agent', 'channel': 'chat',
            'channel_conversation_id': 'bg-compact', 'channel_user_id': 'local-agent'})
        store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'ports-task', 'status': 'active'})
        for role in ['user', 'assistant']:
            store.messages.append({'thread_id': thread.thread_id, 'role': role, 'content': '原先已核对的普通材料。' * 50,
                'metadata': {'conversation_request_id': 'prior-' + role, 'task_id': 'ports-task'}})
        wake = store.wakes.raise_signal({'thread_id': thread.thread_id, 'reason': 'ordinary-test'})
        ref = freeze_source(agent.home_paths.root, OwnerIdentity.local_main(), store.storage.wake_signal_path(wake), 'wake')
        path, _ = enqueue_source(gate.paths, agent.home_paths.root, ref)
        original = read_json_file(path)
        decision = gate.admit(path, (OwnerIdentity.local_main(), session_scope_for_request(original)), AdmissionLimits(8, 8))
        gateway_claim = claim_request(gate.paths, path, coordinator_snapshot=freeze_coordinator_claim(gate, decision))
        case = (gate, gateway_claim, agent, store, thread, None)
        received, bound_params, calls = [], [], []
        original_bind = core._bind_main_agent_turn_params
        def send(message):
            code, result = http_request(server, 'POST', '/ask', {'goal': '继续核对普通材料', 'conversation_id': 'bg-compact',
                'metadata': {'message_id': message}}, headers={'X-User-Id': 'local-agent', 'X-Channel': 'chat'})
            assert code == 202, result
            row = inputs.read_gateway_input_receipt(gate.paths, result['request_id'])
            assert row.transport is not None, result
            received.append(row)
        def bind(actual_agent, prompt, params):
            bound = original_bind(actual_agent, prompt, params)
            bound_params.append(bound)
            if len(bound_params) == 1:
                send('compact-appended')
            return bound
        def on_business(_wire, _number):
            calls.append({'model': _wire.get('model'), 'tools': [v.get('name') or v.get('function', {}).get('name') for v in _wire.get('tools', [])],
                'roles': [v.get('role') for v in _wire.get('messages', [])], 'max_tokens': _wire.get('max_tokens')})
            if len(calls) == 1:
                raise ProviderContextWindowError('普通测试供应商上下文溢出')
        original_attempt = execution._run_background_model_attempt
        def attempt(*args, **kwargs):
            result = original_attempt(*args, **kwargs)
            if result[0].runtime_status == 'context_overflow':
                # 原model/control已返回，claim/Gateway仍活；此时真实HTTP输入只能冻结旧attempt，不能令旧模型多跑一轮。
                send('compact-unappended')
            return result
        monkeypatch.setattr(execution, '_run_background_model_attempt', attempt)
        monkeypatch.setattr(core, '_bind_main_agent_turn_params', bind)
        _http(monkeypatch, backend='anthropic_compatible', on_business=on_business)
        monkeypatch.setattr(compact, '_summarize', lambda *a, **kw: '已核对的普通材料事实。')
        request = BackgroundRunRequest(thread_id=thread.thread_id, task_id='ports-task', reason='ordinary-test',
            conversation_turn_id='bg-compact-invocation')
        deps_run = execution.BackgroundExecutionDependencies(agent=agent, store=store,
            prepare_run=partial(_run_params, request=request, agent=agent))
        sink = BackgroundMainActivitySink(agent, thread_id=thread.thread_id, task_id='ports-task')
        def run_once(_):
            return execution.run_background_turn_with_compact(deps_run, thread, request, user_prompt='继续核对材料',
                continuation_injection=[], proactive_delivery_available=False, activity_sink=sink)
        deps, args, row, _policy = claimed(case, run_once)
        store.claims.renew({'thread_id': thread.thread_id, 'claim_scope_id': row['claim_scope_id'],
            'claim_id': row['claim_id'], 'lease_seconds': 30})
        with gateway_scope('C', case):
            result = claims.run_with_heartbeat(deps, row['claim_id'], args, claim_scope_id=row['claim_scope_id'])
        assert result.runtime_status != 'context_overflow'
        assert len(bound_params) == 2 and len(calls) == 2, (result.runtime_status, result.runtime_reason, result.runtime_source, calls, store.threads.load(thread.thread_id).compact_generation)
        assert bound_params[0].request_id == bound_params[1].request_id
        assert bound_params[0].attempt_id != bound_params[1].attempt_id
        assert store.threads.load(thread.thread_id).compact_generation == 1
        first, unseen = [inputs.read_gateway_input_receipt(gate.paths, r.request_id) for r in received]
        assert first.transport == received[0].transport and first.state != 'queued'
        assert store.guidance.receipt(first.guidance_dedupe_key).status == 'consumed'
        assert unseen.transport == received[1].transport and unseen.state == 'queued'
        assert read_json_file(gateway_claim.path)['turn_phase'] == 'open'


@pytest.mark.parametrize('expires', [False, True])
def test_actual_bg_final_partial_terminal_keeps_one_finish_and_same_gateway_followup(transport_case, tmp_path, monkeypatch, expires):
    import threading
    import time

    from agent_py_agent.agent.backends.request_scope import provider_request_budget
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionOutcomeUncertainError,
        gateway_short_transaction,
    )
    from agent_py_agent.agent.gateway_parts.io import gateway_turn_transition
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
    kind, case = transport_case
    _origin, gateway_claim, agent, store, thread, *_ = case
    thread = store.threads.get_or_create({'canonical_user_id': 'owner', 'channel': 'internal',
        'channel_conversation_id': 'transport', 'channel_user_id': 'owner'})
    case = (*case[:4], thread, *case[5:])
    locked, release = threading.Event(), threading.Event()
    holders, receipts, finishes = [], [], []
    original_finish = store.claims.finish
    def finish(*args, **kwargs):
        finishes.append(kwargs.get('expected_raw'))
        return original_finish(*args, **kwargs)
    monkeypatch.setattr(store.claims, 'finish', finish)
    def action(bound):
        receipt = prepared_input(case, bound, message='partial-terminal')
        receipts.append(receipt)
        def hold():
            with gateway_turn_transition(bound_paths(case), receipt.request_id):
                locked.set()
                release.wait(5)
        holder = threading.Thread(target=hold)
        holders.append(holder)
        holder.start()
        assert locked.wait(2)
        if not expires:
            timer = threading.Timer(.12, release.set)
            timer.start()
            holders.append(timer)
        return 'final-result'
    params = model_runner(case, tmp_path, monkeypatch, action)
    deps, args, row, _policy = claimed(case, lambda _: execution._run_background_model_attempt(agent, '普通收口', params)[0])
    before_p3 = reservation_file(bound_paths(case), gateway_claim.request_id).read_bytes()
    try:
        with gateway_scope(kind, case), provider_request_budget(.45 if expires else 2):
            if expires:
                with pytest.raises(RequestExecutionOutcomeUncertainError):
                    claims.run_with_heartbeat(deps, row['claim_id'], args, claim_scope_id=row['claim_scope_id'])
            else:
                assert claims.run_with_heartbeat(deps, row['claim_id'], args, claim_scope_id=row['claim_scope_id']) == 'final-result'
            assert len(finishes) == 1 and finishes[0] is not None
            terminal = store.claims.load(thread.thread_id)
            assert terminal['status'] == 'finished'
            assert read_json_file(gateway_claim.path)['turn_phase'] == 'open'
            assert reservation_file(bound_paths(case), gateway_claim.request_id).read_bytes() == before_p3
            receipt = inputs.read_gateway_input_receipt(bound_paths(case), receipts[0].request_id)
            assert receipt.state == ('pending' if expires else 'queued')
            if not expires:
                with gateway_short_transaction('acknowledge'):
                    message = store.messages.append({'thread_id': thread.thread_id, 'role': 'assistant', 'content': '原片后继记账'})
                    wake = store.wakes.raise_signal({'thread_id': thread.thread_id, 'reason': 'ordinary-followup'})
                assert message.thread_id == wake.thread_id == thread.thread_id
                def next_run(prompt, *, params):
                    core._bind_main_agent_turn_params(agent, prompt, params)
                    return 'next-result'
                monkeypatch.setattr(agent, 'run', next_run)
                next_params = replace(params, request_id='bg-invocation-next')
                next_deps, next_args, next_row, _ = claimed(case, lambda _: execution._run_background_model_attempt(agent, '同运输下一片', next_params)[0])
                assert claims.run_with_heartbeat(next_deps, next_row['claim_id'], next_args,
                    claim_scope_id=next_row['claim_scope_id']) == 'next-result'
                assert next_row['claim_id'] != row['claim_id'] and len(finishes) == 2
    finally:
        release.set()
        for holder in holders:
            holder.join(3)
            assert not holder.is_alive()
