"""实际临时C认证HTTP重放与唯一queue去向，模型fake；普通资源故障保原部分事实，不修改坏记录造场景。"""
from agent_py_agent.agent.backends.request_scope import provider_request_budget
from agent_py_agent.agent.gateway_parts import http_handlers
from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
from agent_py_agent.agent.gateway_parts import multiprocess_input_receipt as producer
from agent_py_agent.agent.gateway_parts.io import locked_file_transition, read_json_file
from agent_py_agent.tests.test_gateway_active_input_publication import held_lock
from agent_py_agent.tests.test_gateway_background_fresh_queue import (
    bg_first_call,
    run_first,
    send_first,
)


def test_actual_http_pending_replay_recovers_after_first_queue_busy(tmp_path, monkeypatch):
    original_deadline = producer.input_resource_deadline
    def short_budget():
        with provider_request_budget(0.10):
            return original_deadline()
    monkeypatch.setattr(producer, 'input_resource_deadline', short_budget)
    with bg_first_call(tmp_path, monkeypatch) as (case, server, backend):
        gate, _claim, _agent, store, _thread, _ = case
        route = producer.CoordinatorInputReceiptSource.route_fresh
        initialize = producer.CoordinatorInputReceiptSource.__init__
        load = inputs.load_or_prepare_gateway_input_request_locked
        host_prepare = http_handlers._prepare_http_gateway_input
        statuses, hosts, sources = [], [], []
        def construct(source, handler, server):
            initialize(source, handler, server)
            sources.append(source)
        def observe(paths, bundle, *, commit_receipt=None, read_blocking=True):
            result = load(paths, bundle, commit_receipt=commit_receipt, read_blocking=read_blocking)
            statuses.append(result[1])
            return result
        def capture_host(*args, **options):
            hosts.append(True)
            return host_prepare(*args, **options)
        def busy(source, expected, scope, **options):
            canonical = store.storage.guidance_dedupe_path(expected.guidance_dedupe_key)
            with held_lock(locked_file_transition(canonical.with_name('.'+canonical.name+'.transition'))):
                return route(source, expected, scope, **options)
        monkeypatch.setattr(producer.CoordinatorInputReceiptSource, 'route_fresh', busy)
        monkeypatch.setattr(producer.CoordinatorInputReceiptSource, '__init__', construct)
        monkeypatch.setattr(inputs, 'load_or_prepare_gateway_input_request_locked', observe)
        monkeypatch.setattr(http_handlers, '_prepare_http_gateway_input', capture_host)
        def first():
            code, failed = send_first(server)
            assert code == 503 and failed['terminal'] is False
            receipt = inputs.read_gateway_input_receipt(gate.paths, failed['request_id'])
            assert receipt.state == 'pending' and receipt.transport is None
            assert not (gate.paths.inbox/(receipt.request_id+'.json')).exists()
            original = receipt.prepared_request
            code, replay = send_first(server)
            assert code == 202 and replay['input_state'] == 'queued', replay
            restored = inputs.read_gateway_input_receipt(gate.paths, receipt.request_id)
            assert restored.prepared_request == original and restored.transport is None
            assert read_json_file(gate.paths.inbox/(receipt.request_id+'.json')) == original
            assert statuses == [True, False] and hosts == [True]
            assert len(sources) == 2 and sources[0]._fresh is not None and sources[1]._fresh is None
            assert backend.calls == 0
        run_first(case, tmp_path, monkeypatch, first)


def test_actual_explicit_http_control_uses_same_input_t(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import control_service as control
    from agent_py_agent.agent.gateway_parts.http_handlers import http_input_identity
    from agent_py_agent.agent.gateway_parts.io import gateway_turn_transition
    from agent_py_agent.tests.test_gateway_active_input_transport import foreground
    from agent_py_agent.tests.test_gateway_c_http_wait import request
    with foreground(tmp_path, monkeypatch) as (agent, _b, gate, _c, _r, server, _claim, _thread, _lane):
        server.agent = agent  # 原fixture同一个真实Agent，正常ready本机业务路径。
        message = 'explicit-http-busy'
        request_id, _ = http_input_identity(user_id='local-agent', channel='chat', conversation_id='active-cid', message_id=message)
        scope = control.GatewayControlScope('local-agent', 'chat', 'active-cid', metadata={'message_id': message})
        with held_lock(gateway_turn_transition(gate.paths, request_id)):
            code, result = request(server, 'POST', '/control', {'command': '/btw 保持原格式',
                'conversation_id': 'active-cid', 'metadata': {'message_id': message}},
                headers={'X-User-Id': 'local-agent', 'X-Channel': 'chat'})
            assert code == 202 and result['error_code'] == 'BlockingIOError', result
            assert result['delivery_status'] == 'unknown'
        assert agent.conversation_store.guidance.receipt(control.active_turn_guidance_dedupe_key(scope)) is None
        assert not inputs.gateway_input_receipt_path(gate.paths, request_id).exists()
        code, result = request(server, 'POST', '/control', {'command': '/btw 保持原格式',
            'conversation_id': 'active-cid', 'metadata': {'message_id': 'explicit-http-success'}},
            headers={'X-User-Id': 'local-agent', 'X-Channel': 'chat'})
        assert code == 202 and result['ok'], result
        scope = control.GatewayControlScope('local-agent', 'chat', 'active-cid', metadata={'message_id': 'explicit-http-success'})
        row = agent.conversation_store.guidance.receipt(control.active_turn_guidance_dedupe_key(scope))
        assert row is not None


def test_explicit_append_then_actual_claim_finishes_idle_fresh_does_not_queue(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import control_service as control
    from agent_py_agent.tests.test_gateway_active_input_transport import foreground, source_for
    from agent_py_agent.tests.test_gateway_c_http_wait import request
    with foreground(tmp_path, monkeypatch) as (agent, _b, gate, _c, _r, server, claim, thread, lane):
        server.agent = agent
        scope = control.GatewayControlScope('local-agent', 'chat', 'active-cid', metadata={'message_id': 'append-before-idle'})
        code, result = request(server, 'POST', '/control', {'command': '/btw 保持原格式', 'conversation_id': 'active-cid',
            'metadata': scope.metadata}, headers={'X-User-Id': 'local-agent', 'X-Channel': 'chat'})
        assert code == 202 and result['ok'], result
        original = agent.conversation_store.guidance.receipt(control.active_turn_guidance_dedupe_key(scope))
        assert original is not None
        # 原活源封口与原ClaimStore.finish完成正常车道生命周期，未编辑processing/claim JSON。
        source_for(agent, claim, thread).finish(agent)
        finished = agent.conversation_store.claims.finish({'thread_id': thread.thread_id,
            'claim_scope_id': lane['claim_scope_id'], 'claim_id': lane['claim_id'], 'status': 'finished'})
        assert finished['status'] == 'finished'
        code, status = request(server, 'POST', '/ask', {'goal': '保持原格式', 'conversation_id': 'active-cid',
            'metadata': scope.metadata}, headers={'X-User-Id': 'local-agent', 'X-Channel': 'chat'})
        assert code == 202 and status['input_state'] == 'pending', status
        receipt = inputs.read_gateway_input_receipt(gate.paths, status['request_id'])
        assert receipt.transport is None and not (gate.paths.inbox/(receipt.request_id+'.json')).exists()
        assert agent.conversation_store.guidance.receipt(receipt.guidance_dedupe_key).entry.guidance_id == original.entry.guidance_id
        assert request(server, 'POST', '/ask', {'goal': '保持原格式', 'conversation_id': 'active-cid',
            'metadata': scope.metadata}, headers={'X-User-Id': 'local-agent', 'X-Channel': 'chat'})[1]['input_state'] == 'pending'


def test_queue_first_late_explicit_http_same_message_cannot_append(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import control_service as control
    from agent_py_agent.agent.gateway_parts.input_guidance_payload import input_scope
    from agent_py_agent.tests.test_gateway_active_input_transport import foreground, send
    from agent_py_agent.tests.test_gateway_c_http_wait import request
    with foreground(tmp_path, monkeypatch) as (agent, _b, gate, _c, _r, server, _claim, _thread, _lane):
        code, status = send(server, message='queue-before-explicit', inject=['保持格式'])
        assert code == 202 and status['input_state'] == 'queued'
        receipt = inputs.read_gateway_input_receipt(gate.paths, status['request_id'])
        before = inputs.gateway_input_receipt_path(gate.paths, receipt.request_id).read_bytes()
        server.agent = agent
        code, late = request(server, 'POST', '/control', {'command': '/btw '+receipt.prepared_request['goal'],
            'conversation_id': 'active-cid', 'metadata': {'message_id': receipt.client_message_id}},
            headers={'X-User-Id': 'local-agent', 'X-Channel': 'chat'})
        assert code == 202 and not late['ok'] and late['error_code'] == 'INPUT_IDENTITY_CONFLICT', late
        assert agent.conversation_store.guidance.receipt(control.active_turn_guidance_dedupe_key(input_scope(receipt))) is None
        assert inputs.gateway_input_receipt_path(gate.paths, receipt.request_id).read_bytes() == before


def test_unmaterialized_owner_queues_without_owner_or_m_then_real_pool_init_late_writer_respects_queue(tmp_path, monkeypatch):
    from dataclasses import replace

    from agent_py_agent.agent.conversation.control_commands import ConversationControlCommand
    from agent_py_agent.agent.gateway_parts import control_service as control
    from agent_py_agent.agent.gateway_parts.input_guidance_payload import input_scope
    from agent_py_agent.agent.gateway_parts.request_worker import _resolve_request_agent_for_owner
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
    from agent_py_agent.tests.test_gateway_c_http_wait import c_http, request
    with c_http(tmp_path, monkeypatch) as (agent, backend, gate, c, _r, server, _close):
        owner = OwnerIdentity.provider_user('local', 'new-local-user')
        home = resolve_owner_home(c.home_root, owner).home_dir
        assert not home.exists()
        code, result = request(server, 'POST', '/ask', {'goal': '保存这条普通说明', 'conversation_id': 'new-owner-chat',
            'metadata': {'message_id': 'cold-owner-message'}}, headers={'X-User-Id': 'new-local-user', 'X-Channel': 'local'})
        assert code == 202 and result['input_state'] == 'queued', result
        receipt = inputs.read_gateway_input_receipt(gate.paths, result['request_id'])
        assert not home.exists() and backend.calls == 0
        owner_agent = _resolve_request_agent_for_owner(agent, owner)
        assert owner_agent.home_paths.owner_home_dir == home and home.exists()
        store = owner_agent.conversation_store
        thread = store.threads.get_or_create({'canonical_user_id': 'new-local-user', 'channel': 'local',
            'channel_conversation_id': 'new-owner-chat', 'channel_user_id': 'new-local-user'})
        store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'late-owner-task', 'status': 'active'})
        scope = input_scope(receipt)
        active = control._active_conversation_task(agent, scope, task_id='late-owner-task')
        assert active is not None
        late = control._steer_active_request(agent, gate.paths, active,
            ConversationControlCommand('steer', receipt.prepared_request['goal']), scope)
        assert late.ok and late.details['input_state'] == 'queued' and not late.details['active_appended']
        # 显式同ID缺普通digest不得从receipt抄出；固定冲突仍不append。
        explicit = replace(scope, metadata={'message_id': receipt.client_message_id})
        late = control._steer_active_request(agent, gate.paths, active,
            ConversationControlCommand('steer', receipt.prepared_request['goal']), explicit)
        assert not late.ok and late.error_code == 'INPUT_IDENTITY_CONFLICT'
        assert store.guidance.receipt(receipt.guidance_dedupe_key) is None


def test_pending_replay_after_actual_http_stop_lease_withdraw_and_new_c_birth(tmp_path, monkeypatch):
    import os

    import pytest

    from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import (
        coordinator_for_root,
        coordinator_guard,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
    from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
        prepare_coordinator_takeover,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.tests.test_gateway_c_http_wait import c_http, request
    original_deadline = producer.input_resource_deadline
    def short_budget():
        with provider_request_budget(0.1):
            return original_deadline()
    monkeypatch.setattr(producer, 'input_resource_deadline', short_budget)
    with c_http(tmp_path, monkeypatch) as (agent, _b, gate, c, runtime, server, close):
        queue = producer.CoordinatorInputReceiptSource.queue_unbound
        def busy(source, receipt, **options):
            store = producer.input_owner_store(c, receipt)
            path = store.storage.guidance_dedupe_path(receipt.guidance_dedupe_key)
            with held_lock(locked_file_transition(path.with_name('.'+path.name+'.transition'))):
                return queue(source, receipt, **options)
        monkeypatch.setattr(producer.CoordinatorInputReceiptSource, 'queue_unbound', busy)
        body = {'goal': '保留这条普通请求', 'conversation_id': 'new-c-replay', 'metadata': {'message_id': 'new-c-message'}}
        headers = {'X-User-Id': 'local-agent', 'X-Channel': 'chat'}
        code, failed = request(server, 'POST', '/ask', body, headers=headers)
        assert code == 503
        receipt = inputs.read_gateway_input_receipt(gate.paths, failed['request_id'])
        assert receipt.state == 'pending' and receipt.transport is None
        old = coordinator_for_root(gate.paths.root, context=gate.paths)
        listener = server._thread
        born = tuple(runtime._resources['http/requests']._born_threads)
        runtime.stop_producers()
        server.stop()
        assert not listener.is_alive() and all(not thread.is_alive() for thread in born)
        close()  # 原coordinator_guard撤权/关FD，不改任何代次JSON或伪造退出事实。
        assert coordinator_for_root(gate.paths.root) is None and old._closing
        with pytest.raises(OSError):
            os.fstat(old.descriptor)
        monkeypatch.setattr(producer.CoordinatorInputReceiptSource, 'queue_unbound', queue)
        paths = gateway_paths_from_root(gate.paths.root)
        with coordinator_guard(paths.root, gate.count):
            controller = prepare_coordinator_takeover(paths, gate.count, 'new-c-after-normal-stop')
            fresh = GatewayControlPlane(agent.config, agent.home_paths.root, paths, gate.count,
                'new-c-after-normal-stop', agent_root=agent.root)
            assert fresh.controller() is controller
            new_runtime = GatewayDrainRuntime(fresh)
            new_server = GatewayHTTPServer(0, paths,
                params=GatewayHTTPServerParams(control_plane=fresh, drain_runtime=new_runtime))
            new_server.start()
            try:
                code, result = request(new_server, 'POST', '/ask', body, headers=headers)
                assert code == 202 and result['input_state'] == 'queued', result
                restored = inputs.read_gateway_input_receipt(paths, receipt.request_id)
                assert restored.prepared_request == receipt.prepared_request
                assert read_json_file(paths.inbox/(receipt.request_id+'.json')) == receipt.prepared_request
            finally:
                new_runtime.stop_producers()
                new_server.stop()


# LLM: 原queue写入后的真实processing/terminal由正式claim/失败发布API建立，输入对账不得重放或清原P3；不直接编辑旧记录。
# 函数用途: 检查末receipt失败后的同ID重放只补去向，保留已移动的请求和审计事实。
def _partial_lifecycle_case(case, receipt, phase):
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
        reservation_file,
        session_scope_for_request,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_claim import (
        freeze_coordinator_claim,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
        coordinator_execution_scope,
    )
    from agent_py_agent.agent.gateway_parts.queue_service import claim_request
    from agent_py_agent.agent.gateway_parts.request_owner_binding import frozen_request_owner
    from agent_py_agent.agent.gateway_parts.request_worker import (
        AdmissionLimits,
        terminalize_unhandled_claimed_gateway_request,
    )
    gate, _old, agent, _store, _thread, _ = case
    path = gate.paths.inbox/(receipt.request_id+'.json')
    payload = read_json_file(path)
    decision = gate.admit(path, (frozen_request_owner(payload), session_scope_for_request(payload)), AdmissionLimits(8, 8))
    assert decision['status'] == 'admitted', decision
    claim = claim_request(gate.paths, path, coordinator_snapshot=freeze_coordinator_claim(gate, decision))
    if phase == 'terminal':
        with coordinator_execution_scope(gate, claim, agent):
            terminalize_unhandled_claimed_gateway_request(gate.paths, claim.path, RuntimeError('ordinary pre-model failure'),
                agent=agent, expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch)
        assert read_json_file(gate.paths.terminal/claim.path.name)['status'] == 'failed'
    canonical = claim.path if phase == 'processing' else gate.paths.terminal/claim.path.name
    p3 = reservation_file(gate.paths, receipt.request_id)
    return canonical, canonical.read_bytes(), p3, p3.read_bytes()


def test_pending_partial_processing_and_terminal_are_not_reenqueued(tmp_path, monkeypatch):
    # 同一个普通测试逐次使用独立源；两种生命周期都由原API建立。
    from agent_py_agent.agent.common.json_io import locked_json_path
    for phase in ('processing', 'terminal'):
        with monkeypatch.context() as local:
            original_deadline = producer.input_resource_deadline
            def short_budget():
                with provider_request_budget(0.1):
                    return original_deadline()
            local.setattr(producer, 'input_resource_deadline', short_budget)
            with bg_first_call(tmp_path/phase, local) as (case, server, _backend):
                gate = case[0]
                writer = producer.write_json_file_atomic
                def busy_final(path, payload, **options):
                    if path.parent.name == 'input_receipts' and payload.get('state') == 'queued':
                        with held_lock(locked_json_path(path)):
                            return writer(path, payload, **options)
                    return writer(path, payload, **options)
                local.setattr(producer, 'write_json_file_atomic', busy_final)
                def first():
                    code, result = send_first(server)
                    assert code == 503
                    receipt = inputs.read_gateway_input_receipt(gate.paths, result['request_id'])
                    assert receipt.state == 'pending' and (gate.paths.inbox/(receipt.request_id+'.json')).exists()
                    local.setattr(producer, 'write_json_file_atomic', writer)
                    canonical, before, p3, occupancy = _partial_lifecycle_case(case, receipt, phase)
                    code, replay = send_first(server)
                    assert code == 202 and replay['input_state'] == 'queued', replay
                    assert canonical.read_bytes() == before and p3.read_bytes() == occupancy
                    assert not (gate.paths.inbox/(receipt.request_id+'.json')).exists()
                run_first(case, tmp_path/phase, local, first)


def test_count0_explicit_message_keeps_original_append_and_no_new_input_io(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.control_commands import ConversationControlCommand
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts import control_service as control
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path/'count0-home'), model_backend='echo'), tmp_path/'work')
    store = agent.conversation_store
    thread = store.threads.get_or_create({'canonical_user_id': 'local-agent', 'channel': 'chat',
        'channel_conversation_id': 'count0', 'channel_user_id': 'local-agent'})
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'count0-task', 'status': 'active'})
    scope = control.GatewayControlScope('local-agent', 'chat', 'count0', resolved_owner=OwnerIdentity.local_main(),
        metadata={'message_id': 'count0-message'})
    active = control._active_conversation_task(agent, scope, task_id='count0-task')
    assert active is not None
    original = store.guidance.append_once
    seen = []
    def append(payload, **options):
        seen.append(options)
        assert 'blocking' not in options and 'deadline' not in options
        return original(payload, **options)
    def forbidden(*_args, **_options):
        raise AssertionError('Count0 must not enter new input identity/read gate')
    monkeypatch.setattr(store.guidance, 'append_once', append)
    monkeypatch.setattr(http_handlers, 'http_input_identity', forbidden)
    read = control.read_json_file_report
    def no_input(path, **options):
        assert path.parent.name != 'input_receipts'
        return read(path, **options)
    monkeypatch.setattr(control, 'read_json_file_report', no_input)
    result = control._steer_active_request(agent, gateway_paths(agent), active, ConversationControlCommand('steer', '保留原路径'), scope)
    assert result.ok and len(seen) == 1


def test_http_identity_and_digest_equal_original_observed_values():
    # 期望值来自完整06原纯入口的独立调用，不在测试中复制hash公式。
    cases = [{'arguments': [{'conversation_id': ' simple ', 'metadata': {'message_id': ' m-identity '}},
                    '普通正文',
                    ' local-agent ',
                    ' chat '],
      'expected': ['gwreq-msg-765312a4571e324c1fbf4b191cf56d53',
                   'ae29155142c197e598e4f63a00b2b9205b3a8194521d132cd4d10b303359bf43']},
     {'arguments': [{'metadata': {'message_id': '编号二', 'conversation_id': '中文会话'}, 'inject': ['原选项']},
                    '带选项的普通正文',
                    'local-user-2',
                    'local'],
      'expected': ['gwreq-msg-3f4fb672b12069cf97f2e8d0470ed7ac',
                   '9d6feda05efc427377887fbcf1a0fc79c3e3cbd17f066810047ba4a7a4914a3d']},
     {'arguments': [{'conversation_id': 'empty', 'metadata': {}}, '无消息编号', 'local-agent', 'chat'],
      'expected': ['', '']}]
    for case in cases:
        body, goal, user, channel = case['arguments']
        actual = http_handlers._http_idempotent_request_identity(body, goal=goal, user_id=user, channel=channel)
        assert actual == tuple(case['expected'])
        if actual[0]:
            request_id, identity = http_handlers.http_input_identity(user_id=user, channel=channel,
                conversation_id=http_handlers._http_conversation_id(body), message_id=body['metadata']['message_id'])
            assert request_id == actual[0] and identity['user_id'] == user.strip()


def test_actual_bg_final_queued_replay_preserves_original_transport_audit(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import background_claim as claims
    from agent_py_agent.agent.conversation import background_execution as execution
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
    from agent_py_agent.tests.test_background_claim_transport_reference import gateway_scope
    from agent_py_agent.tests.test_background_execution_ports import claimed
    from agent_py_agent.tests.test_gateway_background_active_input import model_runner
    with bg_first_call(tmp_path, monkeypatch) as (case, server, _backend):
        gate, original, agent, store, thread, _ = case
        occupancy = reservation_file(gate.paths, original.request_id).read_bytes()
        seen = []
        def action(_bound):
            code, result = send_first(server, message='bg-final-bound-audit')
            assert code == 202 and result['input_state'] == 'pending'
            receipt = inputs.read_gateway_input_receipt(gate.paths, result['request_id'])
            assert receipt.transport['schema_version'] == 'gateway_input_transport.v2'
            seen.append(receipt)
            return 'ordinary-bg-final'
        params = model_runner(case, tmp_path, monkeypatch, action)
        deps, args, row, _ = claimed(case, lambda _: execution._run_background_model_attempt(agent, '正常后台', params)[0])
        store.claims.renew({'thread_id': thread.thread_id, 'claim_scope_id': row['claim_scope_id'],
            'claim_id': row['claim_id'], 'lease_seconds': 30})
        with gateway_scope('C', case):
            assert claims.run_with_heartbeat(deps, row['claim_id'], args, claim_scope_id=row['claim_scope_id']) == 'ordinary-bg-final'
        queued = inputs.read_gateway_input_receipt(gate.paths, seen[0].request_id)
        assert queued.state == 'queued' and queued.transport == seen[0].transport
        code, replay = send_first(server, message='bg-final-bound-audit')
        assert code == 202 and replay['input_state'] == 'queued', replay
        after = inputs.read_gateway_input_receipt(gate.paths, seen[0].request_id)
        assert after.transport == queued.transport and after.prepared_request == queued.prepared_request
        assert reservation_file(gate.paths, original.request_id).read_bytes() == occupancy
