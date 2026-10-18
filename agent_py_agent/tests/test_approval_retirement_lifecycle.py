"""审批纯退休债务与真实run生命周期分开；真实执行器ask门前计数为零。"""
import threading
from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core import runtime_mixin
from agent_py_agent.agent.agent_core.tool_loop import round_execution
from agent_py_agent.agent.agent_core.tool_only_execution import _tool_only_params
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.common.cancellation import CancellationToken
from agent_py_agent.agent.conversation import agent_tool_approval as approval
from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest
from agent_py_agent.agent.tooling.models import ApprovalPolicy, ToolRuntime, ToolRuntimeSnapshot
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.tests.test_approval_waiter_retirement import held_original_lock
from agent_py_agent.tests.test_host_command_operation_replay import _CountingTool
from agent_py_agent.tests.test_tool_approval_foreground_scope import _foreground


# LLM: 只替模型循环产出合成call，原Agent.run/参数绑定/ToolExecutor ask/原审批发布等待均真执行；不造写源。
# 函数用途: 在真实run里触发一次尚未进handler的审批取消，故障仅为另一线程持原文件锁。
def install_original_approval_loop(world, monkeypatch, lock_kind, seen):
    tool = _CountingTool()
    tool.runtime_policy = replace(tool.runtime_policy, approval_policy=ApprovalPolicy('always'), promotes_task=True)

    import os
    from pathlib import Path
    assert Path(runtime_mixin.__file__).resolve().is_relative_to(Path(os.environ["PYTHONPATH"]).resolve())
    original_loop = runtime_mixin._execute_runtime_loop

    def fake_loop(agent, loop_params):
        params = agent._current_run_params
        if seen.pop('create_grandchild', False):
            parent = agent.subagents.load(params.run_id)
            child = agent.subagents.create_run(goal='合成普通孙任务', root_id=parent.root_id, parent_id=parent.id,
                allowed_tools=['manage_sample'], attributes={
                    'conversation_thread_id': params.task_attributes['conversation_thread_id'],
                    'conversation_task_id': parent.root_id})
            seen['grandchild'] = child.id
            return agent.run_subagent(child.id, dry_run=False, probe=False)
        if seen.get("first_request") and params.request_id != seen["first_request"]:
            seen["followup_params"] = params
            if "followup_started" in seen:
                seen["followup_started"].set()
                assert seen["followup_release"].wait(5)
            return original_loop(agent, loop_params)
        seen['params'] = params
        seen['thread_id'] = params.task_attributes.get('agent_thread_id') or params.task_attributes['conversation_thread_id']
        attempt = agent.subagents.runtime_db.get_attempt(params.attempt_id)
        seen['original_exec_lock'] = {'attempt_id': params.attempt_id, 'attempt_generation': attempt['attempt_generation']}
        from agent_py_agent.agent.runtime_context import current_subagent_run_id
        approval_run = current_subagent_run_id(agent) or loop_params.task_id
        call = ToolCall('lifecycle-call', tool.model_spec.name, {}, 'native', tool.model_spec.schema_hash,
                        approval_run, 'lifecycle-turn', params.attempt_id)
        runtime = ToolRuntime(tool.model_spec, tool.runtime_policy, tool)
        snapshot = ToolRuntimeSnapshot(approval_run, (runtime,), frozenset({call.tool_name}), (), None)
        from agent_py_agent.agent.agent_core.tool_call_runtime import (
            ToolCallRuntimeRequest,
            _prepare_traced_tool_runtime_request,
        )
        prepared_loop = _tool_only_params(params, snapshot, CancellationToken(), call.tool_name)
        _, blocked = _prepare_traced_tool_runtime_request(ToolCallRuntimeRequest(agent,
            round_execution.ToolCallExecuteParams(prepared_loop, 1, 1, call), call, lambda _: None))
        assert blocked is None
        from agent_py_agent.agent.agent_core.tool_loop.recovery import runtime_run_scope
        original = ToolExecutor().execute(ToolExecutorRequest(call, snapshot, agent.root,
            trusted_run_context={'run_scope': runtime_run_scope(agent, prepared_loop).to_dict()},
            write_boundary=params.write_boundary,
            operation_store=ManagedOperationStore(agent.subagents.runtime_db), operation_store_required=True,
            operation_owner_id=agent.home_paths.owner_id))
        assert original.decision.status == 'ask' and original.result.handler_executed is False, original.result.error_code
        assert tool.calls == 0

        class Consumer:
            def request_permission(self, value, **kwargs):
                try:
                    handle = approval.publish_agent_tool_approval(agent, run_id=approval_run,
                        thread_id=seen['thread_id'], request_value=value)
                except Exception as failure:
                    seen['publish_error'] = (type(failure).__name__, str(failure))
                    raise
                seen['handle'] = handle
                path = approval._transition_path(handle.path) if lock_kind == 'permission' else handle.path
                manager = held_original_lock(path)
                manager.__enter__()
                seen['held_lock'] = manager
                kwargs['cancellation_token'].cancel()
                try:
                    approval.wait_for_agent_tool_approval(handle, **kwargs)
                except approval.ApprovalRetirementUnconfirmedError as error:
                    seen['error'] = error
                    if seen.get('physical_unknown'):
                        row = agent.subagents.runtime_db.get_agent_run_exec_lock(
                            agent.subagents.runtime_db.get_attempt(params.attempt_id)['agent_run_id'])
                        seen['physical_exec_lock'] = None if row is None else dict(row)
                        from agent_py_agent.agent.agent_core.runner import session_pool
                        stop = threading.Event()
                        thread = threading.Thread(target=stop.wait, name='original-retirement-resource')
                        origin = session_pool._capture_runner_thread(thread, 'child_runner_worker_stop', params.run_id, params.attempt_id)
                        seen['physical'] = (thread, stop)
                        thread.start()

                        def unconfirmed():
                            raise OSError('synthetic original stop not confirmed')

                        session_pool._finish_original_runner_thread(origin, unconfirmed, error)
                    raise
                finally:
                    if not seen.get('retain_lock'):
                        manager.__exit__(None, None, None)
                        seen.pop('held_lock', None)

        from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot
        round_params = replace(_tool_only_params(params, snapshot, CancellationToken(), call.tool_name),
            effective_on_chunk=Consumer(), tool_protocol_snapshot=make_test_protocol_snapshot(run_id=params.run_id))
        request = round_execution.ToolRoundExecutionRequest(agent, round_params, 1,
            ModelResponse('', 'fake', tool_use_blocks=[{'id': call.call_id, 'name': call.tool_name, 'input': {}}],
                assistant_content_blocks=[{'type': 'tool_use', 'id': call.call_id, 'name': call.tool_name, 'input': {}}]),
            [call], lambda _: original, lambda result: pytest.fail(str({'seen': seen.get('publish_error'), 'result': result.result.error_code})))
        seen['original_round_params'] = round_params
        result = round_execution.execute_tool_round(request)
        seen['resolver_return'] = result
        return result

    monkeypatch.setattr(runtime_mixin, '_execute_runtime_loop', fake_loop)
    return tool


@pytest.mark.parametrize('lock_kind', ['permission', 'json'])
def test_actual_agent_run_retires_call_and_releases_exact_exec_lock(tmp_path, monkeypatch, lock_kind):
    world = _foreground(tmp_path, kind='user')
    prior, seen = world.agent._current_run_params, {}
    tool = install_original_approval_loop(world, monkeypatch, lock_kind, seen)
    with pytest.raises(approval.ApprovalRetirementUnconfirmedError) as caught:
        world.agent.run('合成普通需求', params=replace(world.params, run_id='lifecycle-run', attempt_id='', save=False))
    assert caught.value is seen['error'] and tool.calls == 0
    assert world.agent._current_run_params is prior
    repo = world.agent.subagents.runtime_db
    attempt = repo.get_attempt(seen['params'].attempt_id)
    run = repo.get_agent_run(attempt['agent_run_id'])
    assert attempt['status'] == run['status'] == 'cancelled'
    assert attempt['ended_at'] > 0 and not repo.has_active_exec_lock(run['agent_run_id'])
    assert approval.retry_approval_retirement(caught.value).status == 'no_input'


@pytest.mark.parametrize('lock_kind', ['permission', 'json'])
def test_actual_http_dispatcher_allows_same_thread_followup_while_cleanup_busy(tmp_path, monkeypatch, lock_kind):
    import json
    import time
    import urllib.request

    from agent_py_agent.agent.gateway_parts.client_credentials import GatewayClientCredentials
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.io import read_json_file
    from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
    from agent_py_agent.cli.gateway_loops import _RequestDispatcher
    from agent_py_agent.cli.models import GatewayRunContext

    world = _foreground(tmp_path, kind='user')
    agent, repo = world.agent, world.agent.subagents.runtime_db
    old = repo.get_attempt(world.params.attempt_id)
    repo.settle_agent_run(agent_run_id=old['agent_run_id'], attempt_id=world.params.attempt_id,
                          expected_attempt_status='running', status='cancelled')
    world.store.claims.finish({'thread_id': world.thread.thread_id, 'claim_id': world.claim['claim_id'], 'status': 'finished'})
    del agent._current_run_params
    ensure_gateway_folders(world.paths)
    seen = {'retain_lock': True, 'followup_started': threading.Event(), 'followup_release': threading.Event()}
    provider_messages = []
    generate = agent.backend.generate

    def observe_provider(*args, **kwargs):
        provider_messages.append(kwargs.get('messages'))
        return generate(*args, **kwargs)

    monkeypatch.setattr(agent.backend, 'generate', observe_provider)
    tool = install_original_approval_loop(world, monkeypatch, lock_kind, seen)
    server = GatewayHTTPServer(0, world.paths, params=GatewayHTTPServerParams(agent=agent))
    dispatcher = _RequestDispatcher(GatewayRunContext(agent, world.paths, tmp_path / 'synthetic.yaml'), world.paths)
    server.start()

    def ask(message):
        headers = {'Content-Type': 'application/json', **GatewayClientCredentials(agent.home_paths.root).headers(),
                   'X-User-Id': 'ou_synthetic', 'X-Channel': 'feishu',
                   'X-Conversation-Id': 'oc_private_synthetic'}
        body = {'goal': '合成普通需求', 'user_id': 'ou_synthetic', 'channel': 'feishu',
                'conversation_id': 'oc_private_synthetic', 'metadata': {'message_id': message}}
        request = urllib.request.Request('http://127.0.0.1:%s/ask' % server.server.server_address[1],
            data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 202
            return json.loads(response.read())

    def done_future():
        deadline = time.monotonic() + 5
        while True:
            with dispatcher._future_gate:
                pending = list(dispatcher._request_futures)
            if pending and pending[-1].done():
                return pending[-1]
            assert time.monotonic() < deadline
            time.sleep(.01)

    try:
        submitted = ask('first-retirement')
        seen['first_request'] = submitted['request_id']
        assert dispatcher.tick() == 1
        future = done_future()
        assert 'error' in seen, {key: seen.get(key) for key in ('publish_error', 'resolver_return')}
        assert future.exception() is seen['error'] and tool.calls == 0
        from agent_py_agent.agent.backends.tool_ir import AssistantTurn
        original_blocks = [block for item in seen['original_round_params'].tool_ir_history
            if isinstance(item, AssistantTurn) for block in item.content_blocks if block.get('type') == 'tool_use']
        assert [block['id'] for block in original_blocks] == ['lifecycle-call']
        canonical = read_json_file(world.paths.terminal / (submitted['request_id'] + '.json'))
        assert canonical['schema_version'] == 'gateway_terminal_request.v1'
        assert canonical['status'] == 'cancelled' and canonical['turn_phase'] == 'closed'
        assert 'execution_outcome' not in canonical
        actual = repo.get_attempt(seen['params'].attempt_id)
        assert actual['status'] == 'cancelled' and not repo.has_active_exec_lock(actual['agent_run_id'])
        assert world.store.claims.load_report(seen['thread_id'])[0]['status'] != 'running'
        dispatcher._collect_request_futures()
        assert future in dispatcher._request_futures
        following = ask('next-same-thread')
        assert following['request_id'] != submitted['request_id']
        assert dispatcher.tick() == 1
        assert seen['followup_started'].wait(5)
        new_attempt = repo.get_attempt(seen['followup_params'].attempt_id)
        new_lock = dict(repo.get_agent_run_exec_lock(new_attempt['agent_run_id']))
        assert new_attempt['status'] == 'running' and new_lock['attempt_id'] == new_attempt['attempt_id']
        from agent_py_agent.agent.gateway_parts.queue_service import GatewayClaim
        old_claim = dispatcher._request_futures[future][0]
        assert type(old_claim) is GatewayClaim
        with monkeypatch.context() as scoped:
            scoped.setattr(repo, 'get_agent_run_exec_lock', lambda _: seen['original_exec_lock'])
            assert not approval.approval_retirement_canonical_received(seen['error'], old_claim)
        with monkeypatch.context() as scoped:
            scoped.setattr(repo, 'get_agent_run_exec_lock', lambda _: {**new_lock, 'attempt_generation': new_lock['attempt_generation'] + 1})
            assert not approval.approval_retirement_canonical_received(seen['error'], old_claim)
        manager = seen.pop('held_lock')
        manager.__exit__(None, None, None)
        dispatcher._collect_request_futures()
        assert future not in dispatcher._request_futures and len(dispatcher._request_futures) == 1
        assert dict(repo.get_agent_run_exec_lock(new_attempt['agent_run_id'])) == new_lock
        assert repo.get_attempt(new_attempt['attempt_id'])['status'] == 'running'
        seen['followup_release'].set()
        followup = done_future()
        assert followup is not future and followup.exception() is None
        next_terminal = read_json_file(world.paths.terminal / (following['request_id'] + '.json'))
        assert next_terminal['status'] == 'done' and next_terminal['ok']
        next_run = repo.get_attempt(seen['followup_params'].attempt_id)
        assert next_run['status'] == 'done' and not repo.has_active_exec_lock(next_run['agent_run_id'])
        assert seen['handle'].scope.thread_id == seen['thread_id']
        assert seen['followup_params'].task_attributes['conversation_thread_id'] == seen['thread_id']
        assert provider_messages and all(type(messages) is list for messages in provider_messages)
        for messages in provider_messages:
            pending, consumed = set(), set()
            for message in messages:
                tool_calls = [call['id'] for call in message.get('tool_calls', ())]
                tool_results = [message['tool_call_id']] if message.get('role') == 'tool' else []
                content = message.get('content')
                if isinstance(content, list):
                    tool_calls += [block['id'] for block in content if isinstance(block, dict) and block.get('type') == 'tool_use']
                    tool_results += [block['tool_use_id'] for block in content if isinstance(block, dict) and block.get('type') == 'tool_result']
                for call_id in tool_calls:
                    assert call_id not in pending and call_id not in consumed
                    pending.add(call_id)
                for call_id in tool_results:
                    assert call_id in pending and call_id not in consumed
                    pending.remove(call_id)
                    consumed.add(call_id)
            assert not pending
        if __import__('os').environ.get('D32_FACTS_DIR'):
            import hashlib
            from pathlib import Path
            directory = Path(__import__('os').environ['D32_FACTS_DIR'])
            directory.mkdir(parents=True, exist_ok=True)
            (directory / ('COUNT0_' + lock_kind + '.json')).write_text(json.dumps({
                'initial_request_id': submitted['request_id'], 'next_request_id': following['request_id'],
                'same_thread': seen['thread_id'], 'old_attempt': actual['attempt_id'],
                'next_attempt': next_run['attempt_id'], 'old_terminal_status': canonical['status'],
                'next_terminal_status': next_terminal['status'], 'new_lock_unchanged_while_old_future_removed': True,
                'provider_payload_sha256': [hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest() for messages in provider_messages],
                'provider_message_roles': [[message['role'] for message in messages] for messages in provider_messages],
                'tool_pairs': [{'call_ids': sorted({call['id'] for message in messages for call in message.get('tool_calls', ())}),
                                'result_ids': sorted({message['tool_call_id'] for message in messages if message.get('role') == 'tool'})}
                               for messages in provider_messages],
                'dangling_calls': [], 'duplicate_old_calls': [], 'actual_assistant_round_archiver_used': True,
                'original_unexecuted_assistant_tool_use_ids': [block['id'] for block in original_blocks],
            }, ensure_ascii=False, indent=2))
        dispatcher._collect_request_futures()
        assert not dispatcher._request_futures, {
            'accepted': seen['error']._retirement_origin.observation.terminal_acceptance is not None,
            'closed': approval.approval_retirement_runs_closed(seen['error']),
            'receipt': [(r[1], r[2], r[3], r[4], dict(r[0].get_agent_run(r[1])), dict(r[0].get_attempt(r[3]))) for r in seen['error']._retirement_origin.run_receipts]}
        assert not tuple(world.paths.inbox.glob('*.json'))
    finally:
        seen['followup_release'].set()
        if 'held_lock' in seen:
            seen.pop('held_lock').__exit__(None, None, None)
        dispatcher._executor.shutdown(wait=True)
        server.stop()


@pytest.mark.parametrize('physical_unknown', [False, True])
@pytest.mark.parametrize('lock_kind', ['permission', 'json'])
def test_actual_worker_agent_run_closes_canonical_but_does_not_release_p3(tmp_path, monkeypatch, lock_kind, physical_unknown):
    import time

    from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
    from agent_py_agent.agent.gateway_parts.io import read_json_file
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
    from agent_py_agent.agent.gateway_parts.multiprocess_execution import execute_worker_claim
    from agent_py_agent.agent.gateway_parts.multiprocess_worker_dispatch import WorkerRequestPump
    from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool
    from agent_py_agent.agent.user_space.owner_resolver import owner_identity_from_config
    from agent_py_agent.tests import test_coordinator_private_credential_transactions as seed

    with seed.original_case(tmp_path, monkeypatch) as case:
        agent, _peer, _credential, _body, server, _runtime = case
        binding, gate = claim_http_worker_input(case, monkeypatch)
        params, repo, store = agent._current_run_params, agent.subagents.runtime_db, agent.conversation_store
        assert not params.attempt_id  # 旧卡seed只建原任务/会话；本轮W入口才创建实际运行attempt。
        old_claim = store.claims.load_report(params.task_attributes['conversation_thread_id'])[0]
        store.claims.finish({'thread_id': old_claim['thread_id'], 'claim_id': old_claim['claim_id'], 'status': 'finished'})
        del agent._current_run_params
        seen = {'retain_lock': True, 'first_request': binding.claim.request_id, 'physical_unknown': physical_unknown}
        tool = install_original_approval_loop(None, monkeypatch, lock_kind, seen)
        owner = owner_identity_from_config(agent.config)
        pool = OwnerScopedAgentPool(agent.config, agent.root, workspace_roots=tuple(agent.workspace_roots))

        def original_agent(_config, _root, selected, _roots):
            assert selected == owner
            return agent

        pool._builder = original_agent
        p3 = reservation_file(server.paths, binding.claim.request_id)
        p3_before = p3.read_bytes()
        monkeypatch.setattr(mode, '_WORKER_PARTITION', binding.snapshot.bootstrap.launch.partition)
        pump = WorkerRequestPump(binding.snapshot.bootstrap, pool, 1)
        future = pump._executor.submit(execute_worker_claim, binding.snapshot, binding.claim, pool)
        pump._active[future] = binding.claim
        try:
            deadline = time.monotonic() + 5
            while not future.done():
                assert time.monotonic() < deadline
                time.sleep(.01)
            assert 'error' in seen, {'future_error': repr(future.exception()), 'terminal': read_json_file(server.paths.terminal / binding.claim.path.name), 'seen': seen}
            assert future.exception() is seen['error'] and tool.calls == 0
            if physical_unknown:
                assert not (server.paths.terminal / binding.claim.path.name).exists()
                canonical = read_json_file(binding.claim.path)
                assert canonical['status'] == 'processing' and canonical['execution_outcome']['status'] == 'unknown'
                assert canonical['execution_outcome']['error_code'] == 'TASK_RESOURCE_STOP_UNCONFIRMED'
                assert canonical['execution_outcome']['operation_entered'] is None
                attempt = repo.get_attempt(seen['params'].attempt_id)
                assert attempt['status'] == 'running'
                lock = repo.get_agent_run_exec_lock(attempt['agent_run_id'])
                assert (None if lock is None else dict(lock)) == seen['physical_exec_lock']
                seen.pop('held_lock').__exit__(None, None, None)
                pump.poll()
                assert future in pump._active and p3.read_bytes() == p3_before
                assert not approval.approval_retirement_canonical_received(seen['error'], binding.claim)
                return
            canonical = read_json_file(server.paths.terminal / binding.claim.path.name)
            assert canonical['status'] == 'cancelled' and canonical['turn_phase'] == 'closed'
            assert 'execution_outcome' not in canonical
            attempt = repo.get_attempt(seen['params'].attempt_id)
            assert attempt['status'] == 'cancelled' and repo.get_agent_run_exec_lock(attempt['agent_run_id']) is None
            assert store.claims.load_report(seen['thread_id'])[0]['status'] != 'running'
            assert approval.approval_retirement_canonical_received(seen['error'], binding.claim)
            pump.poll()
            assert future in pump._active and p3.read_bytes() == p3_before
            seen.pop('held_lock').__exit__(None, None, None)
            pump.poll()
            assert not pump._active and p3.read_bytes() == p3_before
            assert not (server.paths.inbox / binding.claim.path.name).exists()
            # 只有原C读同一canonical terminal后释放P3；Future本身从不放行下一业务。
            from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
                release_reservation_if_terminal,
            )
            with monkeypatch.context() as coordinator:
                coordinator.setattr(mode, '_WORKER_PARTITION', None)
                assert release_reservation_if_terminal(gate, binding.claim.request_id) == {'released': True}
                following, _ = claim_http_worker_input(case, monkeypatch, message='worker-same-thread-followup')
            second = pump._executor.submit(execute_worker_claim, following.snapshot, following.claim, pool)
            pump._active[second] = following.claim
            assert second.result(timeout=5) is True
            next_terminal = read_json_file(server.paths.terminal / following.claim.path.name)
            assert next_terminal['status'] == 'done' and next_terminal['ok']
            assert seen['followup_params'].task_attributes['conversation_thread_id'] == seen['thread_id']
            pump.poll()
            assert not pump._active and tool.calls == 0
        finally:
            if 'held_lock' in seen:
                seen.pop('held_lock').__exit__(None, None, None)
            if 'physical' in seen:
                thread, stop = seen['physical']
                stop.set()
                thread.join(5)
                assert not thread.is_alive()
                with pytest.raises(approval.ApprovalRetirementUnconfirmedError) as unconfirmed:
                    pump.close()
                assert unconfirmed.value is seen['error']
            else:
                pump.close()


# LLM: G1/HTTP本身生成完整actor/owner准备回执，后续P3/P5仍原公开函数；不以手写metadata替代真实宿主身份。
# 函数用途: 为真实W业务入口取得同一原HTTP请求的认领对象，不建立WorkerExecution替身。
def claim_http_worker_input(case, monkeypatch, *, message='original-worker-message'):
    import http.client
    import json

    from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
    from agent_py_agent.agent.gateway_parts.io import read_json_file
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
        coordinator_admission_gate,
        reservation_file,
        session_scope_for_request,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import freeze_worker_bootstrap
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
    agent, _peer, row, _body, server, _runtime = case
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=3)
    try:
        connection.request('POST', '/ask', json.dumps({'goal': '合成普通需求', 'channel': 'feishu',
            'user_id': row['actor_id'], 'conversation_id': 'oc_private_synthetic',
            'metadata': {'message_id': message}}), headers={
                'Content-Type': 'application/json', 'X-Gateway-Token': server._local_client_credential,
                'X-User-Id': row['actor_id'], 'X-Channel': 'feishu', 'X-Conversation-Id': 'oc_private_synthetic'})
        response = connection.getresponse()
        submitted = json.loads(response.read())
        assert response.status == 202, submitted
    finally:
        connection.close()
    ensure_gateway_folders(server.paths)
    path = server.paths.inbox / (submitted['request_id'] + '.json')
    request = read_json_file(path)
    owner = owner_identity_from_config(agent.config)
    from agent_py_agent.cli.models import GatewayRunContext
    context = GatewayRunContext(agent, server.paths, agent.root / 'synthetic.yaml', coordinator_worker_count=2,
                                control_plane=server.control_plane)
    gate = coordinator_admission_gate(agent, server.paths, context)
    _runtime.retain_admission(gate)
    assert gate.admit(path, (owner, session_scope_for_request(request)), AdmissionLimits(8, 8))['status'] == 'admitted'
    request = read_json_file(path)
    partition = WorkerPartition(owner_worker(owner, 2), 2, server.control_plane.c_run_id)
    launch = mode.WorkerLaunch(partition, server.paths.root, agent.home_paths.root, 101,
                              workspace_roots=tuple(agent.workspace_roots))
    with monkeypatch.context() as scoped:
        scoped.setattr(mode, '_WORKER_PARTITION', partition)
        snapshot = freeze_claim_snapshot(freeze_worker_bootstrap(launch),
            ClaimCandidate(path, request, read_json_file(reservation_file(server.paths, request['id']))))
        claim = claim_request(server.paths, path, worker_snapshot=snapshot)
        assert claim is not None
        return bind_claimed_request(snapshot, claim, read_json_file(claim.path)), gate


def test_actual_prehandler_physical_unknown_cannot_settle_pure_retirement(tmp_path, monkeypatch):
    world = _foreground(tmp_path, kind='user')
    seen = {'physical_unknown': True}
    tool = install_original_approval_loop(world, monkeypatch, 'permission', seen)
    try:
        with pytest.raises(approval.ApprovalRetirementUnconfirmedError) as caught:
            world.agent.run('合成普通需求', params=replace(world.params, run_id='physical-retirement', attempt_id='', save=False))
        assert caught.value is seen['error'] and tool.calls == 0
        repo = world.agent.subagents.runtime_db
        attempt = repo.get_attempt(seen['params'].attempt_id)
        assert attempt['status'] == 'running' and repo.get_agent_run_exec_lock(attempt['agent_run_id']) is not None
        assert seen['error']._retirement_origin.run_receipts == ()
        assert not approval.approval_retirement_runs_closed(seen['error'])
        assert approval.retry_approval_retirement(seen['error']).status == 'no_input'
        assert not approval.approval_retirement_runs_closed(seen['error'])
    finally:
        thread, stop = seen['physical']
        stop.set()
        thread.join(5)
        assert not thread.is_alive()


def test_outer_exception_chain_only_retains_native_debt_and_never_signs_settlement(tmp_path):
    from copy import copy

    from agent_py_agent.tests.test_approval_retirement_transport import original_retirement_error
    original = original_retirement_error(tmp_path)
    outer = RuntimeError('synthetic actual outer cleanup error')
    outer.__cause__ = original
    assert approval.find_approval_retirement_error(outer) is original
    from agent_py_agent.agent.agent_core.runner.session_pool import _raise_transport_unknown
    with pytest.raises(RuntimeError) as caught:
        _raise_transport_unknown(outer)
    assert caught.value is outer
    assert approval.retry_approval_retirement(original).status == 'no_input'
    assert not approval.complete_approval_retirement_future(outer, object())
    plain = RuntimeError('APPROVAL_RETIREMENT_UNCONFIRMED')
    plain.__context__ = plain
    assert approval.find_approval_retirement_error(plain) is None
    with pytest.raises(TypeError):
        copy(original)
    copied = BaseException.__new__(type(original))
    copied.__dict__.update(original.__dict__)
    outer.__cause__ = copied
    assert approval.find_approval_retirement_error(outer) is None
    outer.__cause__ = original
    original._retirement_origin.request = replace(original.obligation.request, request_id='other-request')
    assert approval.find_approval_retirement_error(outer) is None


@pytest.mark.parametrize('depth', [1, 2])
def test_actual_child_retirement_only_closes_original_child_attempt(tmp_path, monkeypatch, depth):
    world = local_foreground(tmp_path)
    agent, repo = world.agent, world.agent.subagents.runtime_db
    root = world.params.task_id
    parent = root
    for _ in range(1):
        child = agent.subagents.create_run(goal='合成普通子任务', root_id=root, parent_id=parent,
            allowed_tools=['manage_sample'], attributes={
                'conversation_thread_id': world.thread.thread_id, 'conversation_task_id': root})
        parent = child.id
    before = dict(repo.get_attempt(world.params.attempt_id))
    seen = {'create_grandchild': depth == 2}
    tool = install_original_approval_loop(world, monkeypatch, 'permission', seen)
    from agent_py_agent.agent.agent_core.subagent import run_flow
    original_settle = run_flow._settle_subagent_approval_retirement

    def observe_settle(lifecycle, bundle, error):
        seen['child_settle'] = (bundle.options.run_id, bundle.active_attempt_id,
            error.obligation.scope.run_id, error.obligation.scope.execution_attempt_id,
            approval.approval_retirement_runs_closed(error))
        result = original_settle(lifecycle, bundle, error)
        seen['child_settle_result'] = result
        return result

    monkeypatch.setattr(run_flow, '_settle_subagent_approval_retirement', observe_settle)
    try:
        agent.run_subagent(child.id, dry_run=False, probe=False)
    except approval.ApprovalRetirementUnconfirmedError as error:
        caught = error
    else:
        task = agent.subagents.load(child.id)
        pytest.fail(str({'publish_error': seen.get('publish_error'), 'task_status': task.status,
            'runner_result': __import__('pathlib').Path(task.runner_result_file).read_text()[:850]}))
    assert caught is seen['error'] and tool.calls == 0
    assert seen['error'].obligation.scope.agent_kind == 'subagent'
    selected = seen.get('grandchild', child.id)
    assert seen['error'].obligation.scope.run_id == selected
    current = agent.subagents.load(selected)
    assert current.runner_active_attempt_id == ''
    attempt = repo.get_attempt(seen['params'].attempt_id)
    assert attempt['attempt_id'] == seen['params'].attempt_id
    assert attempt['status'] == 'failed' and attempt['ended_at'] > 0
    assert repo.get_agent_run_exec_lock(attempt['agent_run_id']) is None
    assert current.status == 'FAILED' and current.runner_result_file, (seen.get('child_settle'), seen.get('child_settle_result'))
    if depth == 2:
        parent_run = repo.agent_run_for_run_id(child.id)
        assert repo.get_attempt(parent_run['current_attempt_id'])['status'] == 'running'
        assert repo.get_agent_run_exec_lock(parent_run['agent_run_id']) is not None
    assert dict(repo.get_attempt(world.params.attempt_id)) == before
    assert agent._current_run_params is world.params
    assert approval.retry_approval_retirement(seen['error']).status == 'no_input'


def test_failed_original_run_settlement_readback_does_not_accept_cleanup(tmp_path, monkeypatch):
    world = _foreground(tmp_path, kind='user')
    seen = {}
    tool = install_original_approval_loop(world, monkeypatch, 'permission', seen)
    monkeypatch.setattr(world.agent.subagents.runtime_db, 'settle_agent_run', lambda **_: {'settled': False})
    with pytest.raises(approval.ApprovalRetirementUnconfirmedError) as caught:
        world.agent.run('合成普通需求', params=replace(world.params, run_id='failed-settle', attempt_id='', save=False))
    assert caught.value is seen['error'] and tool.calls == 0
    repo = world.agent.subagents.runtime_db
    attempt = repo.get_attempt(seen['params'].attempt_id)
    assert attempt['status'] == 'running' and repo.get_agent_run_exec_lock(attempt['agent_run_id']) is not None
    assert seen['error']._retirement_origin.run_receipts == ()
    assert not approval.approval_retirement_runs_closed(seen['error'])


def test_outer_actual_cleanup_error_preserves_same_native_debt_without_settling(tmp_path, monkeypatch):
    world = _foreground(tmp_path, kind='user')
    seen = {}
    tool = install_original_approval_loop(world, monkeypatch, 'permission', seen)
    error = OSError('synthetic original terminal writer unavailable')

    def unavailable(*_args):
        raise error

    monkeypatch.setattr(runtime_mixin, 'update_runtime_fact_terminal_if_enabled', unavailable)
    with pytest.raises(OSError) as caught:
        world.agent.run('合成普通需求', params=replace(world.params, run_id='outer-error', attempt_id='', save=False))
    assert caught.value is error and tool.calls == 0
    assert approval.find_approval_retirement_error(error) is seen['error']
    repo = world.agent.subagents.runtime_db
    attempt = repo.get_attempt(seen['params'].attempt_id)
    assert attempt['status'] == 'running' and repo.get_agent_run_exec_lock(attempt['agent_run_id']) is not None
    assert not approval.complete_approval_retirement_future(error, object())


# LLM: 本机chat原Thread/TaskLink和真实RuntimeDB/main writer由唯一API建立；None actor只来自结构化本机来源，不伪造外部身份。
# 函数用途: 为两层本机子代理提供真实根任务，不以外部聊天的不完整手写attrs代替宿主来源。
def local_foreground(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.io import write_json_file
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.request_binding import GatewayTaskBindingWriter
    from agent_py_agent.agent.settings.config import AgentConfig
    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / 'home'), model_backend='echo'), tmp_path / 'workspace')
    store = agent.conversation_store
    thread = store.threads.get_or_create({'channel': 'chat', 'canonical_user_id': 'local', 'channel_conversation_id': 'parent-local'})
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'durable-task', 'status': 'active'})
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    path = paths.processing / 'local-message.json'
    write_json_file(path, {'id': 'local-message', 'status': 'processing', 'turn_phase': 'open',
        'execution_attempt_id': 'local-transport'})
    writer = GatewayTaskBindingWriter(path, 'local-message', execution_attempt_id='local-transport')
    writer.bind_conversation_claim(thread.thread_id)
    claim = store.claims.acquire({'thread_id': thread.thread_id, 'task_id': 'gateway:local-message', 'reason': 'gateway_foreground_turn'})
    params = runtime_mixin._bind_main_agent_authority(agent, RunParams(request_id='local-message', run_id='local-main',
        task_id='durable-task', source='gateway', context_scope='conversation',
        task_attributes={'conversation_thread_id': thread.thread_id, 'conversation_task_id': 'durable-task'},
        conversation_task_binding_callback=writer))
    agent._current_run_params = params
    return SimpleNamespace(agent=agent, store=store, paths=paths, path=path, params=params, claim=claim, thread=thread)
