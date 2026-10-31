"""原HTTP/W Future只运输实际收尾异常链，不因permission清理完成推断canonical核收。"""
import json
import os
import threading
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_py_agent.agent.gateway_parts import request_worker
from agent_py_agent.agent.gateway_parts.io import read_json_file
from agent_py_agent.tests.test_approval_retirement_lifecycle import (
    _foreground,
    _offline_backend,
    approval,
    claim_http_worker_input,
    install_original_approval_loop,
)


# LLM: 只建真实临时HTTP/Dispatcher/Agent.run与原ask，不替权限或结果；finally回收本测试原Thread/锁。
# 函数用途: 让测试在原handler之后制造收尾故障并观察同一个实际Future。
@contextmanager
def http_case(tmp_path, monkeypatch, lock_kind):
    from agent_py_agent.agent.gateway_parts.client_credentials import GatewayClientCredentials
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
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
    seen = {'retain_lock': True}
    tool = install_original_approval_loop(world, monkeypatch, lock_kind, seen)
    server = GatewayHTTPServer(0, world.paths, params=GatewayHTTPServerParams(agent=agent))
    dispatcher = _RequestDispatcher(GatewayRunContext(agent, world.paths, tmp_path / 'synthetic.yaml'), world.paths)
    server.start()

    def ask(message):
        headers = {'Content-Type': 'application/json', **GatewayClientCredentials(agent.home_paths.root).headers(),
                   'X-User-Id': 'ou_synthetic', 'X-Channel': 'feishu', 'X-Conversation-Id': 'oc_private_synthetic'}
        body = {'goal': '合成普通需求', 'user_id': 'ou_synthetic', 'channel': 'feishu',
                'conversation_id': 'oc_private_synthetic', 'metadata': {'message_id': message}}
        request = urllib.request.Request('http://127.0.0.1:%s/ask' % server.server.server_address[1],
            data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 202
            return json.loads(response.read())

    def submit(message):
        submitted = ask(message)
        if 'first_request' not in seen:
            seen['first_request'] = submitted['request_id']
        assert dispatcher.tick() == 1
        deadline = time.monotonic() + 5
        while True:
            rows = tuple(dispatcher._request_futures)
            if rows and rows[-1].done():
                return rows[-1], submitted
            assert time.monotonic() < deadline
            time.sleep(.01)

    try:
        yield world, seen, tool, dispatcher, submit
    finally:
        if 'held_lock' in seen:
            seen.pop('held_lock').__exit__(None, None, None)
        if 'physical' in seen:
            thread, stop = seen['physical']
            stop.set()
            thread.join(5)
            assert not thread.is_alive()
        dispatcher._executor.shutdown(wait=True)
        server.stop()


# LLM: 故障只在当前原收尾入口抛同outer；不模拟scope、重写terminal或制造批准许可。
# 函数用途: 对比finish、严格接收、handler后审计与finally失败，确认自然context和既有cause均保留。
@pytest.mark.parametrize('phase', ['projection', 'accept', 'audit', 'lease'])
@pytest.mark.parametrize('lock_kind', ['permission', 'json'])
def test_http_post_receive_outer_keeps_cause_and_unaccepted_future(tmp_path, monkeypatch, phase, lock_kind):
    with http_case(tmp_path, monkeypatch, lock_kind) as (world, seen, tool, dispatcher, submit):
        cause = LookupError('synthetic existing cause')
        outer = OSError('synthetic original finalizer failure')
        outer.__cause__ = cause
        if phase == 'projection':
            original = request_worker.write_json_file_atomic

            def fail(path, payload, **kwargs):
                if path.parent == world.paths.responses and path.stem == seen.get('first_request'):
                    raise outer
                return original(path, payload, **kwargs)

            monkeypatch.setattr(request_worker, 'write_json_file_atomic', fail)
        elif phase == 'accept':
            def fail(*args):
                raise outer

            monkeypatch.setattr(approval, 'accept_approval_retirement_terminal', fail)
        else:
            from agent_py_agent.agent.gateway_parts import request_execution
            name = '_complete_gateway_request_audit' if phase == 'audit' else '_stop_gateway_request_lease'
            original = getattr(request_execution, name)

            def fail(*args, **kwargs):
                result = original(*args, **kwargs)
                if 'error' in seen:
                    raise outer
                return result

            monkeypatch.setattr(request_execution, name, fail)
        future, submitted = submit('post-receive-' + phase)
        native = seen['error']
        observation = native._retirement_origin.observation
        assert future.exception() is outer and outer.__cause__ is cause
        assert approval.find_approval_retirement_error(outer) is native
        assert observation.pending_error is native and observation.terminal_acceptance is None
        assert native._retirement_origin.callback_closed and tool.calls == 0
        assert seen['handle'].path.exists()
        paths = [world.paths.terminal / (submitted['request_id'] + '.json'),
                 world.paths.processing / (submitted['request_id'] + '.json')]
        before = {str(path): path.read_bytes() if path.exists() else None for path in paths}
        request_worker.terminalize_unhandled_claimed_gateway_request(world.paths, paths[1], outer, agent=world.agent)
        assert before == {str(path): path.read_bytes() if path.exists() else None for path in paths}
        dispatcher._collect_request_futures()
        assert future in dispatcher._request_futures
        seen.pop('held_lock').__exit__(None, None, None)
        dispatcher._collect_request_futures()
        assert future in dispatcher._request_futures and future.exception() is outer
        assert approval.retry_approval_retirement(native).status != 'unconfirmed'
        assert not approval.approval_retirement_canonical_received(native, dispatcher._request_futures[future][0])
        followup_status = None
        if phase == 'projection':
            monkeypatch.setattr(request_worker, 'write_json_file_atomic', original)
            second, following = submit('next-after-projection-failure')
            assert second is not future and second.exception() is None
            terminal = read_json_file(world.paths.terminal / (following['request_id'] + '.json'))
            assert terminal['status'] == 'done' and terminal['ok']
            assert seen['followup_params'].task_attributes['conversation_thread_id'] == seen['thread_id']
            dispatcher._collect_request_futures()
            assert future in dispatcher._request_futures and second not in dispatcher._request_futures
            followup_status = terminal['status']
        if os.environ.get('D33_FACTS'):
            Path(os.environ['D33_FACTS']).joinpath(phase + '_' + lock_kind + '.json').write_text(json.dumps({
                'followup_status': followup_status, 'same_outer': True, 'cause_unchanged': True, 'native_context': True, 'callback_closed': True,
                'handler_calls': tool.calls, 'terminal_acceptance': False, 'future_retained_after_cleanup': True,
                'permission_exists': seen['handle'].path.exists(), 'request_status': [read_json_file(p).get('status') for p in paths],
            }, indent=2))


@pytest.mark.parametrize('lock_kind', ['permission', 'json'])
def test_worker_finish_outer_retains_same_future_p3_and_native(tmp_path, monkeypatch, lock_kind):
    from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
    from agent_py_agent.agent.gateway_parts.multiprocess_execution import execute_worker_claim
    from agent_py_agent.agent.gateway_parts.multiprocess_worker_dispatch import WorkerRequestPump
    from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool
    from agent_py_agent.agent.user_space.owner_resolver import owner_identity_from_config
    from agent_py_agent.tests import test_coordinator_private_credential_transactions as seed

    with seed.original_case(tmp_path, monkeypatch) as case:
        agent, _, _, _, server, _ = case
        _offline_backend(agent)
        binding, _gate = claim_http_worker_input(case, monkeypatch)
        params, store = agent._current_run_params, agent.conversation_store
        old = store.claims.load_report(params.task_attributes['conversation_thread_id'])[0]
        store.claims.finish({'thread_id': old['thread_id'], 'claim_id': old['claim_id'], 'status': 'finished'})
        del agent._current_run_params
        seen = {'retain_lock': True, 'first_request': binding.claim.request_id}
        tool = install_original_approval_loop(None, monkeypatch, lock_kind, seen)
        owner = owner_identity_from_config(agent.config)
        pool = OwnerScopedAgentPool(agent.config, agent.root, workspace_roots=tuple(agent.workspace_roots))

        def original_agent(_config, _root, selected, _roots):
            assert selected == owner
            return agent

        pool._builder = original_agent
        outer, cause = OSError('synthetic W projection failure'), LookupError('existing W cause')
        outer.__cause__ = cause
        original = request_worker.write_json_file_atomic

        def fail(path, payload, **kwargs):
            if path.parent == server.paths.responses:
                raise outer
            return original(path, payload, **kwargs)

        monkeypatch.setattr(request_worker, 'write_json_file_atomic', fail)
        p3 = reservation_file(server.paths, binding.claim.request_id)
        p3_before = p3.read_bytes()
        monkeypatch.setattr(mode, '_WORKER_PARTITION', binding.snapshot.bootstrap.launch.partition)
        pump = WorkerRequestPump(binding.snapshot.bootstrap, pool, 1)
        future = pump._executor.submit(execute_worker_claim, binding.snapshot, binding.claim, pool)
        pump._active[future] = binding.claim
        try:
            with pytest.raises(OSError) as caught:
                future.result(timeout=5)
            native = seen['error']
            assert caught.value is outer and outer.__cause__ is cause
            assert approval.find_approval_retirement_error(outer) is native
            assert native._retirement_origin.callback_closed and tool.calls == 0
            assert native._retirement_origin.observation.terminal_acceptance is None
            assert read_json_file(server.paths.terminal / binding.claim.path.name)['status'] == 'cancelled'
            pump.poll()
            assert future in pump._active and p3.read_bytes() == p3_before
            seen.pop('held_lock').__exit__(None, None, None)
            pump.poll()
            assert future in pump._active and p3.read_bytes() == p3_before
            assert not approval.approval_retirement_canonical_received(native, binding.claim)
            assert approval.retry_approval_retirement(native).status != 'unconfirmed'
            assert not (server.paths.inbox / binding.claim.path.name).exists()
        finally:
            if 'held_lock' in seen:
                seen.pop('held_lock').__exit__(None, None, None)
            with pytest.raises(OSError) as caught:
                pump.close()
            assert caught.value is outer


@pytest.mark.parametrize('kind', ['common', 'runner'])
def test_actual_http_outer_unknown_preserves_type_and_debt_priority(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.agent_core import runtime_mixin
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.agent.gateway_parts.request_errors import request_resource_exit_unconfirmed

    with http_case(tmp_path, monkeypatch, 'permission') as (world, seen, tool, dispatcher, submit):
        actual = {}

        def fail_terminal(*args, **kwargs):
            request = read_json_file(world.paths.processing / (seen['first_request'] + '.json'))
            if kind == 'common':
                outer = RequestExecutionOutcomeUncertainError(request, 'synthetic_terminal_prewrite')
            else:
                from agent_py_agent.agent.agent_core.runner import session_pool
                outer = OSError('synthetic actual runner stop unknown')
                stop = threading.Event()
                thread = threading.Thread(target=stop.wait, name='original-finalizer-resource')
                params = seen['params']
                origin = session_pool._capture_runner_thread(thread, 'child_runner_worker_stop', params.run_id, params.attempt_id)
                seen['physical'] = thread, stop
                thread.start()

                def not_stopped():
                    raise OSError('synthetic join not confirmed')

                actual['error'] = outer
                session_pool._finish_original_runner_thread(origin, not_stopped, outer)
            actual['error'] = outer
            raise outer

        monkeypatch.setattr(runtime_mixin, 'update_runtime_fact_terminal_if_enabled', fail_terminal)
        future, _ = submit('outer-' + kind)
        assert 'error' in actual, repr(future.exception())
        outer, native = actual['error'], seen['error']
        assert future.exception() is outer and approval.find_approval_retirement_error(outer) is native
        assert type(outer) is (RequestExecutionOutcomeUncertainError if kind == 'common' else OSError)
        assert bool(request_resource_exit_unconfirmed(outer)) is (kind == 'runner')
        assert native._retirement_origin.run_receipts == () and tool.calls == 0
        assert native._retirement_origin.callback_closed
        assert native._retirement_origin.observation.terminal_acceptance is None
        assert not tuple(world.paths.terminal.glob('*.json'))
        processing = read_json_file(world.paths.processing / (seen['first_request'] + '.json'))
        assert processing['status'] == 'processing'
        # Count0此outer不冒充原worker来源补诊断；实际物理出处只保守持processing/Future，绝不写普通终态。
        if 'execution_outcome' in processing:
            assert processing['execution_outcome']['status'] == 'unknown'
        seen.pop('held_lock').__exit__(None, None, None)
        dispatcher._collect_request_futures()
        assert future in dispatcher._request_futures and future.exception() is outer
        assert not approval.complete_approval_retirement_future(outer, dispatcher._request_futures[future][0])
