"""私卡只提交原决定输入；原等待线程重核实际P3/P5、TLS和原审批行。"""
import json
import time
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.contracts.tool_approval import build_tool_approval_request
from agent_py_agent.agent.conversation.agent_tool_approval import (
    publish_agent_tool_approval,
    wait_for_agent_tool_approval,
)
from agent_py_agent.agent.conversation.tool_approval_scope import tool_approval_scope
from agent_py_agent.agent.delivery import ChannelCapabilities
from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import freeze_worker_bootstrap
from agent_py_agent.agent.gateway_parts.multiprocess_execution import worker_execution_scope
from agent_py_agent.agent.gateway_parts.request_transactions import request_transaction_ports_scope
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.tests.test_coordinator_approval_cancel import approval_case, seed
from agent_py_agent.tests.test_coordinator_private_credential_transactions import post
from agent_py_agent.tests.test_tool_approval_foreground_scope import _foreground


@pytest.mark.parametrize('decision', ['approved', 'denied'])
def test_cold_c_private_decision_prepares_original_control_receipt(tmp_path, monkeypatch, decision):
    with approval_case(tmp_path, monkeypatch) as (case, pending):
        _agent, _peer, credential, _body, server, _runtime = case
        before = pending['path'].read_bytes()
        status, result = seed.post(server, credential, {**pending['body'], 'decision': decision})
        assert status == 200 and result['status'] == 'queued', (status, result)
        assert result['decision'] == 'pending' and result['operation_id'].startswith('gwctl-msg-')
        assert pending['path'].read_bytes() == before and server.agent is None


# LLM: 临时P3预留与P5快照沿原fixture合同，claim由原queue入口创建；不伪造WorkerExecution或Params来源。
# 函数用途: 为原真实owner Agent准备同一运输claim，字段/字节均可在断言中核验。
def _actual_claim(case, monkeypatch):
    from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
        coordinator_admission_gate,
        session_scope_for_request,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_dispatch_binding import (
        bind_claimed_request,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_dispatch_claim import (
        ClaimCandidate,
        freeze_claim_snapshot,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition, owner_worker
    from agent_py_agent.agent.gateway_parts.queue_service import (
        claim_request,
        ensure_gateway_folders,
    )
    from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits
    from agent_py_agent.agent.user_space.owner_resolver import owner_identity_from_config
    agent, _peer, credential, _body, server, _runtime = case
    owner = owner_identity_from_config(agent.config)
    ensure_gateway_folders(server.paths)
    actor = credential['actor_id']
    payload = {'id': 'actual-approval-request', 'kind': 'ask', 'goal': '合成原审批调用', 'user_id': actor,
        'metadata': {'channel': 'feishu', 'user_id': actor, 'channel_chat_type': 'p2p', 'channel_chat_id': 'oc_private_synthetic',
                     'client_input_digest': 'actual-approval-digest'},
        'conversation': {'channel': 'feishu', 'canonical_user_id': actor, 'channel_conversation_id': 'oc_private_synthetic'}}
    with delivery.gateway_input_transition(server.paths, payload['id']):
        input_receipt, _ = delivery.load_or_prepare_gateway_input_locked(server.paths,
            request_id=payload['id'], client_input_digest='actual-approval-digest', client_message_id='',
            guidance_dedupe_key='', prepared_request=payload)
        delivery.queue_gateway_input_locked(server.paths, input_receipt)
    gate = coordinator_admission_gate(agent, server.paths, SimpleNamespace(coordinator_worker_count=2))
    _runtime.retain_admission(gate)
    path = server.paths.inbox / (payload['id'] + '.json')
    decision = gate.admit(path, (owner, session_scope_for_request(input_receipt.prepared_request)), AdmissionLimits(8, 8))
    assert decision['status'] == 'admitted', decision
    request = json.loads(path.read_text())
    partition = WorkerPartition(owner_worker(owner, 2), 2, server.control_plane.c_run_id)
    launch = mode.WorkerLaunch(partition, server.paths.root, agent.home_paths.root, 101,
                              workspace_roots=tuple(agent.workspace_roots))
    with monkeypatch.context() as scoped:
        scoped.setattr(mode, '_WORKER_PARTITION', partition)
        reservation = json.loads(reservation_file(server.paths, request['id']).read_text())
        snapshot = freeze_claim_snapshot(freeze_worker_bootstrap(launch), ClaimCandidate(path, request, reservation))
        claim = claim_request(server.paths, path, worker_snapshot=snapshot)
        assert claim is not None
        binding = bind_claimed_request(snapshot, claim, json.loads(claim.path.read_text()))
    return binding


# LLM: 真实SimpleAgent在C前构造；原实际claim/typed writer/RuntimeDB及scope在同发布/等待线程持有，HTTP从不复制这些对象。
# 函数用途: 准备可重复核验的C受理→W控制输入→原W等待线程应用组件链，不宣称物理W构造或Count2验收。
@contextmanager
def waiting_case(tmp_path, monkeypatch, kind='foreground'):
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.agent_core.runtime_mixin import _bind_main_agent_authority
    from agent_py_agent.agent.gateway_parts.request_binding import GatewayTaskBindingWriter
    with seed.original_case(tmp_path, monkeypatch) as case:
        agent, _peer, _credential, _body, server, _runtime = case
        binding = _actual_claim(case, monkeypatch)
        original = agent._current_run_params
        root, store = original.task_id, agent.conversation_store
        thread_id = original.task_attributes['conversation_thread_id']
        with ExitStack() as lifetime:
            lifetime.enter_context(worker_execution_scope(binding, agent))
            with monkeypatch.context() as scoped:
                scoped.setattr(mode, '_WORKER_PARTITION', binding.snapshot.bootstrap.launch.partition)
                lifetime.enter_context(request_transaction_ports_scope(agent))
                prior = store.claims.load_report(thread_id)[0]
                store.claims.finish({'thread_id': thread_id, 'claim_id': prior['claim_id'], 'status': 'finished'})
                writer = GatewayTaskBindingWriter(binding.claim.path, binding.claim.request_id,
                    execution_attempt_id=binding.claim.execution_attempt_id)
                writer.bind_conversation_claim(thread_id)
                store.claims.acquire({'thread_id': thread_id, 'task_id': 'gateway:' + binding.claim.request_id,
                                      'reason': 'gateway_foreground_turn'})
                agent._current_run_params = _bind_main_agent_authority(agent, RunParams(request_id=binding.claim.request_id,
                    run_id='approval-real-main-run', task_id=root, source='gateway', context_scope='conversation',
                    task_attributes={'conversation_thread_id': thread_id, 'conversation_task_id': root},
                    host_channel_actor=original.host_channel_actor, conversation_task_binding_callback=writer))
                selected, thread, attempt = root, thread_id, agent._current_run_params.attempt_id
                for _ in range({'foreground': 0, 'child': 1, 'grandchild': 2}[kind]):
                    task = agent.subagents.create_run(goal='合成审批任务', root_id=root, parent_id=selected)
                    task = agent.subagents.lifecycle.prepare_runner_attempt(task.id)
                    selected, thread, attempt = task.id, task.agent_thread_id, task.runner_active_attempt_id
                request = build_tool_approval_request(ToolCall(call_id='actual-approval-call', tool_name='controlled_exec',
                    arguments={'apply': True}, source_protocol='native', schema_hash='sha256:actual-approval',
                    run_id=selected, turn_id='original-turn', attempt_id=attempt), request_id='actual-approval-request',
                    round_number=1, call_index=1, description='合成原执行审批')
                handle = publish_agent_tool_approval(agent, run_id=selected, thread_id=thread, request_value=request)
            row = json.loads(handle.path.read_text())
            body = {key: row['im_control'][key] for key in ('reference', 'message_id', 'chat_id')}
            yield case, binding, handle, body


# LLM: 原登记/claim/CAS/pool.pin均生产；builder仅返回已真实构造同owner Agent，不造执行身份；Future只提交input即退出。
# 函数用途: 有界推进一次原控制poll和其Future，不等原卡deadline。
def poll_original_control(case, binding, monkeypatch, *, prepare=False):
    import os

    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap_ready import (
        registered_worker_heartbeat,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_control_worker import WorkerControlPump
    from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool
    from agent_py_agent.agent.user_space.owner_resolver import owner_identity_from_config
    agent, _peer, _credential, _body, server, _runtime = case
    partition = binding.snapshot.bootstrap.launch.partition
    launch = replace(binding.snapshot.bootstrap.launch, parent_pid=os.getppid(), instance_id='actual-control-instance')
    owner = owner_identity_from_config(agent.config)
    pool = OwnerScopedAgentPool(agent.config, agent.root, workspace_roots=tuple(agent.workspace_roots))
    def original_agent(_config, _root, selected, _roots):
        assert selected == owner
        return agent
    pool._builder = original_agent
    with monkeypatch.context() as scoped:
        scoped.setattr(mode, '_WORKER_PARTITION', partition)
        if prepare:
            heartbeat = registered_worker_heartbeat(launch, launch.instance_id, {'ready': True, 'gateway_ports': [43210]}, time.time())
            registry = {'schema_version': 'multiprocess_worker_registry.v1', 'c_run_id': partition.c_run_id, 'worker_count': 2,
                'process_identity': heartbeat['process_identity'], 'http_port': 43210,
                'workers': [{'worker': partition.index, 'instance_id': launch.instance_id,
                    'process_identity': heartbeat['process_identity'], 'executor_ledger_ref': f'workers/executors/w{partition.index}'}]}
            write_json_file_atomic(server.paths.root / 'workers/registry.json', registry)
            write_json_file_atomic(server.paths.root / f'workers/w{partition.index}.json', heartbeat)
        pump = WorkerControlPump(freeze_worker_bootstrap(launch), pool)
        started = time.monotonic()
        try:
            pump.poll()
        finally:
            pump.close()
        assert time.monotonic() - started < 2 and not pump.inflight
        assert pool.peek(owner) is agent and not pool._pins


@pytest.mark.parametrize('kind', ['foreground', 'child', 'grandchild'])
@pytest.mark.parametrize('decision', ['approved', 'denied'])
def test_original_waiter_applies_same_private_input_with_actual_claim(tmp_path, monkeypatch, kind, decision):
    with waiting_case(tmp_path, monkeypatch, kind) as (case, binding, handle, body):
        agent, _peer, credential, _body, server, _runtime = case
        p3 = reservation_file(server.paths, binding.claim.request_id).read_bytes()
        original = json.loads(binding.claim.path.read_text())
        body['decision'] = decision
        status, queued = seed.post(server, credential, body)
        assert status == 200 and queued['status'] == 'queued', (status, queued)
        poll_original_control(case, binding, monkeypatch, prepare=True)
        receipt = ops.read_gateway_control_operation(server.paths, queued['operation_id'])
        assert receipt.state == 'executing'
        assert json.loads(handle.path.read_text())['status'] == 'pending'
        with monkeypatch.context() as scoped:
            scoped.setattr(mode, '_WORKER_PARTITION', binding.snapshot.bootstrap.launch.partition)
            assert wait_for_agent_tool_approval(handle).decision == decision
        poll_original_control(case, binding, monkeypatch)
        receipt = ops.read_gateway_control_operation(server.paths, queued['operation_id'])
        assert receipt.state == 'completed' and receipt.result['details']['decision'] == decision
        saved = handle.path.read_bytes()
        assert seed.post(server, credential, body)[1]['decision'] == decision
        assert handle.path.read_bytes() == saved and reservation_file(server.paths, binding.claim.request_id).read_bytes() == p3
        assert json.loads(binding.claim.path.read_text()) == original


# LLM: 真实G1/ACL头沿原receipt，不从query参数猜owner；只是HTTP测试运输，不读取秘密或扩访问范围。
# 函数用途: 经原状态API观察同一私卡回执，确认查询不重批或误写unknown。
def _get_original_status(server, receipt):
    import http.client
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=3)
    headers = {'X-Gateway-Token': server._local_client_credential, 'X-User-Id': receipt.user_id,
        'X-Channel': receipt.channel, 'X-Conversation-Id': receipt.conversation_id,
        'X-Channel-Chat-Type': receipt.channel_chat_type, 'X-Channel-Chat-Id': receipt.channel_chat_id}
    try:
        connection.request('GET', '/control-status/' + receipt.operation_id, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.mark.parametrize('decision', ['approved', 'denied'])
def test_count0_http_input_is_applied_only_by_original_foreground_waiter(tmp_path, monkeypatch, decision):
    from types import SimpleNamespace
    world = _foreground(tmp_path, kind='user')
    agent = world.agent
    agent.config.feishu_session_lock_enabled = False
    agent.channel_registry.register_adapter('feishu', SimpleNamespace(send_control_card=lambda *_a, **_kw:
        {'message_id': 'om_count0_foreground', 'chat_id': 'oc_private_synthetic'}),
        capabilities=ChannelCapabilities(control_cards=True, proactive=True))
    request = build_tool_approval_request(ToolCall(call_id='http-original-call', tool_name='controlled_exec',
        arguments={'apply': True}, source_protocol='native', schema_hash='sha256:http-probe', run_id='durable-task',
        turn_id='original-turn', attempt_id=world.params.attempt_id), request_id='http-original-request',
        round_number=1, call_index=1, description='合成原前台审批')
    handle = publish_agent_tool_approval(agent, run_id='durable-task', thread_id=world.thread.thread_id, request_value=request)
    assert tool_approval_scope(agent, 'durable-task').active
    row = json.loads(handle.path.read_text())
    body = {key: row['im_control'][key] for key in ('reference','message_id','chat_id')}
    body['decision'] = decision
    server = GatewayHTTPServer(0, world.paths, params=GatewayHTTPServerParams(agent=agent,
        auth_middleware=AuthMiddleware(AuthManager())))
    try:
        server.start()
        before = handle.path.read_bytes()
        status, result = post(server, {'actor_id': 'ou_synthetic'}, body)
        assert status == 200 and result['ok'], (status, result)
        assert handle.path.read_bytes() != before
        receipt_id = result['operation_id']
        pending = json.loads(handle.path.read_text())
        assert pending['status'] == 'pending'
        assert pending['decision_input']['operation_id'] == receipt_id
        from agent_py_agent.agent.gateway_parts import control_operation_service as ops
        receipt = ops.reconcile_gateway_control_operation(agent, world.paths, receipt_id)
        assert receipt.state == 'executing'
        assert _get_original_status(server, receipt)[1]['control_state'] == 'executing'
        from agent_py_agent.agent.conversation.agent_tool_approval import (
            wait_for_agent_tool_approval,
        )
        resolved = wait_for_agent_tool_approval(handle)
        assert resolved.decision == decision
        row_after = handle.path.read_bytes()
        receipt = ops.reconcile_gateway_control_operation(agent, world.paths, receipt_id)
        assert receipt.state == 'completed' and receipt.result['details']['decision'] == decision
        assert post(server, {'actor_id': 'ou_synthetic'}, body)[1]['decision'] == decision
        assert handle.path.read_bytes() == row_after
        with pytest.raises(ValueError, match='original waiter'):
            handle.decision_input()
    finally:
        server.stop()


@pytest.mark.parametrize('reason', ['cancel', 'expiry', 'claim', 'child_attempt', 'personal_lock'])
def test_pending_input_retires_only_in_original_waiter_without_later_approval(tmp_path, monkeypatch, reason):
    from agent_py_agent.agent.contracts.tool_approval import ToolApprovalDecision
    from agent_py_agent.agent.conversation import agent_tool_approval as approval
    from agent_py_agent.agent.gateway_parts import private_interaction_service as private
    kind = 'child' if reason == 'child_attempt' else 'foreground'
    with waiting_case(tmp_path, monkeypatch, kind) as (case, binding, handle, body):
        agent, _peer, credential, _body, server, _runtime = case
        body['decision'] = 'approved'
        queued = seed.post(server, credential, body)[1]
        poll_original_control(case, binding, monkeypatch, prepare=True)
        original_p3 = reservation_file(server.paths, binding.claim.request_id).read_bytes()
        token = SimpleNamespace(cancelled=reason == 'cancel')
        with monkeypatch.context() as scoped:
            scoped.setattr(mode, '_WORKER_PARTITION', binding.snapshot.bootstrap.launch.partition)
            if reason == 'expiry':
                scoped.setattr(approval, 'time', SimpleNamespace(time=lambda: handle.request.requested_at + 100000,
                                                               sleep=time.sleep))
            elif reason == 'claim':
                claims = agent.conversation_store.claims
                prior = claims.load_report(handle.scope.thread_id)[0]
                claims.finish({'thread_id': handle.scope.thread_id, 'claim_id': prior['claim_id'], 'status': 'finished'})
                claims.acquire({'thread_id': handle.scope.thread_id, 'task_id': handle.scope.run_id, 'reason': 'thread_goal_continue'})
            elif reason == 'child_attempt':
                runtime = agent.subagents.runtime_db
                run = runtime.agent_run_for_run_id(handle.scope.run_id)
                assert runtime.settle_agent_attempt(agent_run_id=run['agent_run_id'],
                    attempt_id=handle.scope.execution_attempt_id)['settled']
                agent.subagents.lifecycle.abandon_runner_attempt(handle.scope.run_id, handle.scope.execution_attempt_id)
                changed = agent.subagents.lifecycle.prepare_runner_attempt(handle.scope.run_id)
                assert changed.runner_active_attempt_id != handle.scope.execution_attempt_id
            elif reason == 'personal_lock':
                # 只替代当前密码状态查询，实际决定仍由原waiter/锁/行/typed source提交；不伪造执行scope。
                scoped.setattr(private, 'private_interaction_lock_error_from_config', lambda *_a, **_k: 'IM_UNLOCK_REQUIRED')
            decision = wait_for_agent_tool_approval(handle, cancellation_token=token)
        expected = 'cancelled' if reason == 'cancel' else 'denied' if reason == 'expiry' else 'unavailable'
        assert decision.decision == expected
        row = json.loads(handle.path.read_text())
        assert row['status'] == 'decided' and row['decision']['decision'] == expected
        assert row['decision_input']['operation_id'] == queued['operation_id']
        assert row['decision'] == decision.to_dict()
        assert ToolApprovalDecision.from_mapping(row['decision']) == decision
        poll_original_control(case, binding, monkeypatch)
        receipt = ops.read_gateway_control_operation(server.paths, queued['operation_id'])
        assert receipt.state == 'completed' and receipt.result['details']['ok'] is False
        assert receipt.result['details']['decision'] == expected
        assert reservation_file(server.paths, binding.claim.request_id).read_bytes() == original_p3
        assert not (server.paths.terminal / binding.claim.path.name).exists()


def test_real_cancel_after_recorded_approval_does_not_rewrite_user_decision(tmp_path, monkeypatch):
    with waiting_case(tmp_path, monkeypatch) as (case, binding, handle, body):
        _agent, _peer, credential, _body, server, _runtime = case
        body['decision'] = 'approved'
        queued = seed.post(server, credential, body)[1]
        poll_original_control(case, binding, monkeypatch, prepare=True)
        with monkeypatch.context() as scoped:
            scoped.setattr(mode, '_WORKER_PARTITION', binding.snapshot.bootstrap.launch.partition)
            assert handle.decision_input().decision == 'approved'
            saved = handle.path.read_bytes()
            assert wait_for_agent_tool_approval(handle, cancellation_token=SimpleNamespace(cancelled=True)).decision == 'cancelled'
        assert handle.path.read_bytes() == saved
        poll_original_control(case, binding, monkeypatch)
        receipt = ops.read_gateway_control_operation(server.paths, queued['operation_id'])
        assert receipt.state == 'completed' and receipt.result['details']['decision'] == 'approved'
        assert seed.post(server, credential, {**body, 'decision': 'cancel'})[1]['error_code'] == 'IM_INTERACTION_ALREADY_HANDLED'


def test_control_unknown_before_waiter_prevents_late_approval_without_republishing_input(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import private_approval_control as private
    with waiting_case(tmp_path, monkeypatch) as (case, binding, handle, body):
        _agent, _peer, credential, _body, server, _runtime = case
        body['decision'] = 'approved'
        queued = seed.post(server, credential, body)[1]
        submit = private._submit_approval_input
        original_error = OSError('synthetic-input-response-loss')
        def lost_response(*args, **kwargs):
            submit(*args, **kwargs)
            raise original_error
        with monkeypatch.context() as scoped:
            scoped.setattr(private, '_submit_approval_input', lost_response)
            poll_original_control(case, binding, monkeypatch, prepare=True)
        receipt = ops.read_gateway_control_operation(server.paths, queued['operation_id'])
        assert receipt.state == 'terminal_unknown'
        original = handle.path.read_bytes()
        p3 = reservation_file(server.paths, binding.claim.request_id).read_bytes()
        with monkeypatch.context() as scoped:
            scoped.setattr(mode, '_WORKER_PARTITION', binding.snapshot.bootstrap.launch.partition)
            decision = wait_for_agent_tool_approval(handle)
        assert decision.decision == 'unavailable' and decision.reason_code == 'APPROVAL_UNAVAILABLE'
        assert handle.path.read_bytes() == original
        assert seed.post(server, credential, body)[1]['status'] == 'unknown'
        assert reservation_file(server.paths, binding.claim.request_id).read_bytes() == p3


@pytest.mark.parametrize('phase', ['scope', 'request_permission'])
@pytest.mark.parametrize('actual', [True, False])
def test_original_typed_unknown_escapes_best_effort_and_samecode_remains_unavailable(tmp_path, monkeypatch, phase, actual):
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.agent.conversation import agent_tool_approval as approval
    from agent_py_agent.agent.conversation import tool_approval_scope as scopes
    with waiting_case(tmp_path, monkeypatch) as (case, binding, handle, _body):
        agent = case[0]
        payload = json.loads(binding.claim.path.read_text())
        error = RequestExecutionOutcomeUncertainError(payload, 'approval_scope_observation') if actual else RuntimeError('ordinary')
        if not actual:
            error.error_code = RequestExecutionOutcomeUncertainError.error_code
        before, p3 = handle.path.read_bytes(), reservation_file(case[4].paths, binding.claim.request_id).read_bytes()
        def raises(*_a, **_k):
            raise error
        with monkeypatch.context() as scoped:
            scoped.setattr(scopes, 'tool_approval_scope', raises)
            if phase == 'request_permission':
                scoped.setattr(approval, 'tool_approval_scope', raises)
                sink = SimpleNamespace(agent=agent, task_id=handle.scope.run_id, thread_id=handle.scope.thread_id,
                                       approval_interactive=True)
                def run():
                    return approval.AgentToolApprovalSinkMixin.request_permission(sink, handle.request.to_dict())
            else:
                def run():
                    return scopes.tool_approval_scope_is_current(agent, handle.scope)
            if actual:
                with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
                    run()
                assert caught.value is error
            else:
                result = run()
                assert result['decision'] == 'unavailable' if phase == 'request_permission' else result is False
        assert handle.path.read_bytes() == before
        assert reservation_file(case[4].paths, binding.claim.request_id).read_bytes() == p3


def test_real_original_wait_thread_cannot_apply_after_its_source_finally_closed(tmp_path, monkeypatch):
    import threading

    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionOutcomeUncertainError,
    )
    observed, failures = [], []
    def original_thread():
        try:
            with waiting_case(tmp_path, monkeypatch) as (case, binding, handle, body):
                body['decision'] = 'approved'
                assert seed.post(case[4], case[2], body)[0] == 200
                poll_original_control(case, binding, monkeypatch, prepare=True)
                saved = handle.path.read_bytes()
                p3 = reservation_file(case[4].paths, binding.claim.request_id)
                p3_saved = p3.read_bytes()
                identity = (binding.claim.request_id, binding.claim.execution_attempt_id, binding.claim.lease_epoch)
            with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
                handle.decision_input()
            error = caught.value
            assert (error.request_id, error.execution_attempt_id, error.lease_epoch) == identity
            assert error.operation_entered is False
            assert handle.path.read_bytes() == saved and p3.read_bytes() == p3_saved
            assert not (case[4].paths.terminal / binding.claim.path.name).exists()
            observed.append(error)
        except BaseException as error:
            failures.append(error)
    thread = threading.Thread(target=original_thread)
    thread.start()
    thread.join(10)
    assert not thread.is_alive() and not failures, failures
    assert len(observed) == 1


def test_four_control_slots_do_not_wait_for_five_original_approval_deadlines(tmp_path, monkeypatch):
    with waiting_case(tmp_path, monkeypatch) as (case, binding, handle, body):
        agent, _peer, credential, _body, server, _runtime = case
        handles = [handle]
        with monkeypatch.context() as scoped:
            scoped.setattr(mode, '_WORKER_PARTITION', binding.snapshot.bootstrap.launch.partition)
            for i in range(4):
                request = build_tool_approval_request(ToolCall(call_id=f'additional-call-{i}', tool_name='controlled_exec',
                    arguments={'apply': True}, source_protocol='native', schema_hash='sha256:actual-approval',
                    run_id=handle.scope.run_id, turn_id='original-turn', attempt_id=handle.scope.execution_attempt_id),
                    request_id=f'additional-request-{i}', round_number=1, call_index=i + 2, description='合成原审批槽')
                handles.append(publish_agent_tool_approval(agent, run_id=handle.scope.run_id,
                    thread_id=handle.scope.thread_id, request_value=request))
        receipts = []
        for selected in handles:
            transport = json.loads(selected.path.read_text())['im_control']
            click = {key: transport[key] for key in ('reference', 'message_id', 'chat_id')}
            status, accepted = seed.post(server, credential, {**click, 'decision': 'approved'})
            assert status == 200 and accepted['status'] == 'queued'
            receipts.append(accepted['operation_id'])
        started = time.monotonic()
        poll_original_control(case, binding, monkeypatch, prepare=True)
        poll_original_control(case, binding, monkeypatch)
        assert time.monotonic() - started < 3
        assert all(ops.read_gateway_control_operation(server.paths, key).state == 'executing' for key in receipts)
        for selected in handles:
            row = json.loads(selected.path.read_text())
            assert row['status'] == 'pending' and row['decision_input']['operation_id'] in receipts
            selected.close_input()


@pytest.mark.parametrize('field', ['message_id', 'chat_id'])
def test_first_click_requires_exact_original_delivered_card_before_receipt(tmp_path, monkeypatch, field):
    with approval_case(tmp_path, monkeypatch) as (case, pending):
        before = pending['path'].read_bytes()
        click = {**pending['body'], 'decision': 'approved', field: 'unrelated-transport'}
        assert seed.post(case[4], case[2], click)[0] == 403
        assert pending['path'].read_bytes() == before
        assert not list((case[4].paths.root / 'control_operations').glob('*.json'))


def test_original_private_cancel_wins_pending_input_and_is_not_late_approval(tmp_path, monkeypatch):
    with waiting_case(tmp_path, monkeypatch) as (case, binding, handle, body):
        body['decision'] = 'approved'
        queued = seed.post(case[4], case[2], body)[1]
        poll_original_control(case, binding, monkeypatch, prepare=True)
        assert seed.post(case[4], case[2], {**body, 'decision': 'cancel'})[1]['decision'] == 'cancelled'
        with monkeypatch.context() as scoped:
            scoped.setattr(mode, '_WORKER_PARTITION', binding.snapshot.bootstrap.launch.partition)
            assert wait_for_agent_tool_approval(handle).decision == 'cancelled'
        saved = handle.path.read_bytes()
        poll_original_control(case, binding, monkeypatch)
        receipt = ops.read_gateway_control_operation(case[4].paths, queued['operation_id'])
        assert receipt.state == 'completed' and receipt.result['details']['decision'] == 'cancelled'
        assert receipt.result['details']['ok'] is False
        assert handle.path.read_bytes() == saved


def test_decided_fact_survives_same_typed_response_loss_and_is_only_reconciled(tmp_path, monkeypatch):
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.agent.conversation import agent_tool_approval as approval
    with waiting_case(tmp_path, monkeypatch) as (case, binding, handle, body):
        body['decision'] = 'approved'
        queued = seed.post(case[4], case[2], body)[1]
        poll_original_control(case, binding, monkeypatch, prepare=True)
        original = approval._resolve_agent_tool_approval_locked
        error = RequestExecutionOutcomeUncertainError(json.loads(binding.claim.path.read_text()), 'approval_result_observation')
        calls = []
        def after_write(*args, **kwargs):
            calls.append(True)
            original(*args, **kwargs)
            raise error
        p3 = reservation_file(case[4].paths, binding.claim.request_id).read_bytes()
        with monkeypatch.context() as scoped:
            scoped.setattr(mode, '_WORKER_PARTITION', binding.snapshot.bootstrap.launch.partition)
            scoped.setattr(approval, '_resolve_agent_tool_approval_locked', after_write)
            with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
                wait_for_agent_tool_approval(handle)
            assert caught.value is error
        saved = handle.path.read_bytes()
        assert json.loads(saved)['decision']['decision'] == 'approved'
        poll_original_control(case, binding, monkeypatch)
        receipt = ops.read_gateway_control_operation(case[4].paths, queued['operation_id'])
        assert receipt.state == 'completed' and receipt.result['details']['decision'] == 'approved'
        assert seed.post(case[4], case[2], body)[1]['decision'] == 'approved'
        assert calls == [True] and handle.path.read_bytes() == saved
        assert reservation_file(case[4].paths, binding.claim.request_id).read_bytes() == p3
        assert not (case[4].paths.terminal / binding.claim.path.name).exists()


def test_busy_original_permission_transition_does_not_block_or_decide(tmp_path, monkeypatch):
    from agent_py_agent.agent.common.json_io import locked_json_path
    from agent_py_agent.agent.conversation.agent_tool_approval import _transition_path
    with waiting_case(tmp_path, monkeypatch) as (case, binding, handle, body):
        body['decision'] = 'approved'
        assert seed.post(case[4], case[2], body)[0] == 200
        poll_original_control(case, binding, monkeypatch, prepare=True)
        before = handle.path.read_bytes()
        with monkeypatch.context() as scoped:
            scoped.setattr(mode, '_WORKER_PARTITION', binding.snapshot.bootstrap.launch.partition)
            with locked_json_path(_transition_path(handle.path)):
                started = time.monotonic()
                assert handle.decision_input() is None
                assert time.monotonic() - started < .5
            assert handle.path.read_bytes() == before
            assert wait_for_agent_tool_approval(handle).decision == 'approved'


@pytest.mark.parametrize('decision', ['approved', 'denied'])
def test_ready_local_c_uses_same_actual_coordinator_waiter_without_worker_or_foreign_agent(tmp_path, monkeypatch, decision):
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.agent_core.runtime_mixin import _bind_main_agent_authority
    from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
        coordinator_admission_gate,
        session_scope_for_request,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_claim import (
        freeze_coordinator_claim,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
        coordinator_execution_scope,
    )
    from agent_py_agent.agent.gateway_parts.queue_service import (
        claim_request,
        ensure_gateway_folders,
    )
    from agent_py_agent.agent.gateway_parts.request_binding import GatewayTaskBindingWriter
    from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    with seed.original_case(tmp_path, monkeypatch, kind='main') as case:
        agent, _peer, credential, _body, server, _runtime = case
        # 与健康C原组合根同义：只交付C前已构造的原baseAgent，不新建/借foreign pool。
        server.agent = agent
        ensure_gateway_folders(server.paths)
        payload = {'id': 'actual-local-c-approval', 'kind': 'ask', 'goal': '合成本机原审批', 'user_id': 'local-agent',
            'metadata': {'channel': 'chat', 'user_id': 'local-agent', 'client_input_digest': 'local-c-digest'}}
        with delivery.gateway_input_transition(server.paths, payload['id']):
            receipt, _ = delivery.load_or_prepare_gateway_input_locked(server.paths, request_id=payload['id'],
                client_input_digest='local-c-digest', client_message_id='', guidance_dedupe_key='', prepared_request=payload)
            delivery.queue_gateway_input_locked(server.paths, receipt)
        gate = coordinator_admission_gate(agent, server.paths, SimpleNamespace(coordinator_worker_count=2))
        _runtime.retain_admission(gate)
        path = server.paths.inbox / (payload['id'] + '.json')
        admitted = gate.admit(path, (OwnerIdentity.local_main(), session_scope_for_request(receipt.prepared_request)), AdmissionLimits(8, 8))
        assert admitted['status'] == 'admitted', admitted
        claim = claim_request(server.paths, path, coordinator_snapshot=freeze_coordinator_claim(gate, admitted))
        assert claim is not None
        original = agent._current_run_params
        thread_id, task_id = original.task_attributes['conversation_thread_id'], original.task_id
        with coordinator_execution_scope(gate, claim, agent), request_transaction_ports_scope(agent):
            store = agent.conversation_store
            prior = store.claims.load_report(thread_id)[0]
            store.claims.finish({'thread_id': thread_id, 'claim_id': prior['claim_id'], 'status': 'finished'})
            writer = GatewayTaskBindingWriter(claim.path, claim.request_id, execution_attempt_id=claim.execution_attempt_id)
            writer.bind_conversation_claim(thread_id)
            store.claims.acquire({'thread_id': thread_id, 'task_id': 'gateway:' + claim.request_id, 'reason': 'gateway_foreground_turn'})
            agent._current_run_params = _bind_main_agent_authority(agent, RunParams(request_id=claim.request_id,
                run_id='original-local-c-run', task_id=task_id, source='gateway', context_scope='conversation',
                task_attributes={'conversation_thread_id': thread_id, 'conversation_task_id': task_id},
                host_channel_actor=original.host_channel_actor, conversation_task_binding_callback=writer))
            request = build_tool_approval_request(ToolCall(call_id='original-local-c-call', tool_name='controlled_exec',
                arguments={'apply': True}, source_protocol='native', schema_hash='sha256:local-c', run_id=task_id,
                turn_id='local-c-turn', attempt_id=agent._current_run_params.attempt_id), request_id='local-c-permission',
                round_number=1, call_index=1, description='合成原本机等待')
            handle = publish_agent_tool_approval(agent, run_id=task_id, thread_id=thread_id, request_value=request)
            transport = json.loads(handle.path.read_text())['im_control']
            body = {key: transport[key] for key in ('reference', 'message_id', 'chat_id')}
            body['decision'] = decision
            p3 = reservation_file(server.paths, claim.request_id).read_bytes()
            status, accepted = seed.post(server, credential, body)
            assert status == 200 and accepted['status'] == 'queued', (status, accepted)
            pending_receipt = ops.read_gateway_control_operation(server.paths, accepted['operation_id'])
            assert _get_original_status(server, pending_receipt)[1]['control_state'] == 'executing'
            assert wait_for_agent_tool_approval(handle).decision == decision
            status, observed = _get_original_status(server, pending_receipt)
            assert status == 200 and observed['control_state'] == 'completed', (status, observed)
            status, completed = seed.post(server, credential, body)
            assert status == 200 and completed['decision'] == decision
            original_receipt = ops.read_gateway_control_operation(server.paths, accepted['operation_id'])
            assert original_receipt.dispatch is None and original_receipt.state == 'completed'
            assert reservation_file(server.paths, claim.request_id).read_bytes() == p3
            assert not (server.paths.terminal / claim.path.name).exists()


def test_count0_partial_input_failure_is_unknown_and_never_silently_queued_again(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import private_approval_control as private
    world = _foreground(tmp_path, kind='user')
    agent = world.agent
    agent.config.feishu_session_lock_enabled = False
    agent.channel_registry.register_adapter('feishu', SimpleNamespace(send_control_card=lambda *_a, **_k:
        {'message_id': 'om_local_lost', 'chat_id': 'oc_private_synthetic'}),
        capabilities=ChannelCapabilities(control_cards=True, proactive=True))
    request = build_tool_approval_request(ToolCall(call_id='local-lost-call', tool_name='controlled_exec',
        arguments={'apply': True}, source_protocol='native', schema_hash='sha256:local-lost', run_id='durable-task',
        turn_id='local-turn', attempt_id=world.params.attempt_id), request_id='local-lost-request',
        round_number=1, call_index=1, description='合成输入部分失败')
    handle = publish_agent_tool_approval(agent, run_id='durable-task', thread_id=world.thread.thread_id, request_value=request)
    transport = json.loads(handle.path.read_text())['im_control']
    body = {key: transport[key] for key in ('reference', 'message_id', 'chat_id')}
    body['decision'] = 'approved'
    server = GatewayHTTPServer(0, world.paths, params=GatewayHTTPServerParams(agent=agent,
        auth_middleware=AuthMiddleware(AuthManager())))
    submit, calls = private._submit_approval_input, []
    def lost(*args):
        calls.append(True)
        submit(*args)
        raise OSError('synthetic-input-response-lost')
    try:
        server.start()
        with monkeypatch.context() as scoped:
            scoped.setattr(private, '_submit_approval_input', lost)
            assert post(server, {'actor_id': 'ou_synthetic'}, body)[0] == 503
        [path] = (world.paths.root / 'control_operations').glob('*.json')
        receipt = ops.read_gateway_control_operation(world.paths, path.stem)
        assert receipt.state == 'terminal_unknown'
        before = handle.path.read_bytes()
        assert wait_for_agent_tool_approval(handle).decision == 'unavailable'
        result = post(server, {'actor_id': 'ou_synthetic'}, body)[1]
        assert result['status'] == 'unknown' and calls == [True]
        assert handle.path.read_bytes() == before
        assert json.loads(before)['status'] == 'pending'
    finally:
        server.stop()
