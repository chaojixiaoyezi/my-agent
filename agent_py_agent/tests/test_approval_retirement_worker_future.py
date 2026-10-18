"""原W executor/claim/source/handler的退休义务，只在原canonical已收未知且清理成功后去Future。"""
import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.runtime_mixin import _bind_main_agent_authority
from agent_py_agent.agent.common.cancellation import CancellationToken
from agent_py_agent.agent.contracts.tool_approval import build_tool_approval_request
from agent_py_agent.agent.conversation import agent_tool_approval as approval
from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.gateway_parts.io import read_json_file
from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
from agent_py_agent.agent.gateway_parts.multiprocess_execution import execute_worker_claim
from agent_py_agent.agent.gateway_parts.multiprocess_worker_dispatch import WorkerRequestPump
from agent_py_agent.agent.gateway_parts.request_binding import GatewayTaskBindingWriter
from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.agent.user_space.owner_resolver import owner_identity_from_config
from agent_py_agent.tests.test_approval_waiter_retirement import held_original_lock
from agent_py_agent.tests.test_coordinator_approval_decision import mode, waiting_case


@pytest.mark.parametrize('physical_unknown', [False, True])
@pytest.mark.parametrize('lock_kind', ['permission', 'json'])
def test_real_worker_future_cleans_permission_but_keeps_canonical_unknown_and_p3(tmp_path, monkeypatch, lock_kind, physical_unknown):
    with waiting_case(tmp_path, monkeypatch) as (case, binding, old_handle, _body):
        agent, _peer, _credential, _body, server, _runtime = case
        saved_params = agent._current_run_params
        ready, begin = threading.Event(), threading.Event()
        original = {}
        physical_stop = threading.Event()
        physical_threads = []
        claim = binding.claim
        p3 = reservation_file(server.paths, claim.request_id)
        before_p3 = p3.read_bytes()

        def business(context, on_chunk):
            writer = GatewayTaskBindingWriter(claim.path, claim.request_id, execution_attempt_id=claim.execution_attempt_id)
            writer.bind_conversation_claim(old_handle.scope.thread_id)
            params = _bind_main_agent_authority(agent, RunParams(request_id=claim.request_id, run_id='worker-retire-main',
                task_id=old_handle.scope.root_task_id, source='gateway', context_scope='conversation',
                task_attributes={'conversation_thread_id': old_handle.scope.thread_id,
                    'conversation_task_id': old_handle.scope.root_task_id},
                host_channel_actor=saved_params.host_channel_actor, conversation_task_binding_callback=writer))
            agent._current_run_params = params
            request = build_tool_approval_request(ToolCall(call_id='worker-retire-call', tool_name='controlled_exec',
                arguments={'apply': True}, source_protocol='native', schema_hash='sha256:worker-retire',
                run_id=old_handle.scope.root_task_id, turn_id='original-worker-turn', attempt_id=params.attempt_id),
                request_id=claim.request_id, round_number=1, call_index=1, description='合成原W取消')
            handle = approval.publish_agent_tool_approval(agent, run_id=old_handle.scope.root_task_id,
                thread_id=old_handle.scope.thread_id, request_value=request)
            original['handle'] = handle
            ready.set()
            assert begin.wait(5)
            try:
                token = CancellationToken()
                token.cancel()
                approval.wait_for_agent_tool_approval(handle, cancellation_token=token)
            except approval.ApprovalRetirementUnconfirmedError as error:
                original['error'] = error
                if physical_unknown:
                    from agent_py_agent.agent.agent_core.runner import session_pool
                    thread = threading.Thread(target=physical_stop.wait, name='original-retirement-physical-thread')
                    origin = session_pool._capture_runner_thread(thread, 'child_runner_worker_stop',
                        old_handle.scope.root_task_id, params.attempt_id)
                    physical_threads.append(thread)
                    thread.start()

                    def stop_unconfirmed():
                        raise OSError('ordinary fixture stop not confirmed')

                    session_pool._finish_original_runner_thread(origin, stop_unconfirmed, error)
                raise
            finally:
                del agent._current_run_params

        pool = OwnerScopedAgentPool(agent.config, agent.root, workspace_roots=tuple(agent.workspace_roots))
        owner = owner_identity_from_config(agent.config)

        def original_agent(_config, _root, selected, _roots):
            assert selected == owner
            return agent

        pool._builder = original_agent
        monkeypatch.setattr(request_execution, '_execute_gateway_request_body', business)
        with monkeypatch.context() as scoped:
            scoped.setattr(mode, '_WORKER_PARTITION', binding.snapshot.bootstrap.launch.partition)
            pump = WorkerRequestPump(binding.snapshot.bootstrap, pool, 1)
            # claim已由原C/P3→原P5取得；本片只测该同claim原executor完成点，不再次认领或模拟Future。
            future = pump._executor.submit(execute_worker_claim, binding.snapshot, claim, pool)
            pump._active[future] = claim
            try:
                assert ready.wait(5)
                handle = original['handle']
                lock = approval._transition_path(handle.path) if lock_kind == 'permission' else handle.path
                with held_original_lock(lock):
                    begin.set()
                    deadline = time.monotonic() + 5
                    while not future.done():
                        assert time.monotonic() < deadline
                        time.sleep(0.01)
                    assert future.exception() is original['error']
                    assert not approval.approval_retirement_canonical_received(original['error'], claim)
                    # 原control poll实际执行；退休busy不能让原_reap提前抛出而饿死独立控制槽。
                    controls = []
                    poll = pump.controls.poll

                    def observe_control():
                        controls.append(True)
                        return poll()

                    scoped.setattr(pump.controls, 'poll', observe_control)
                    assert pump.poll() == 0 and controls == [True]
                    assert pump._active[future] is claim and p3.read_bytes() == before_p3
                    pump.stop_claiming()
                    with pytest.raises(approval.ApprovalRetirementUnconfirmedError) as error:
                        pump.close()
                    assert error.value is original['error'] and future in pump._active
                assert pump.poll() == 0
                # 无真实ask/原run终态核收的裸wait仍未结；物理未知与pure都不能借清permission丢掉原责任。
                assert future in pump._active
                with pytest.raises(approval.ApprovalRetirementUnconfirmedError) as still_unknown:
                    pump.close()
                assert still_unknown.value is original['error']
                assert not handle.path.exists()
                canonical = read_json_file(claim.path)
                assert canonical['execution_outcome']['status'] == 'unknown'
                assert canonical['execution_outcome']['operation_entered'] is None
                assert canonical['status'] == 'processing' and p3.read_bytes() == before_p3
                assert not (server.paths.terminal / claim.path.name).exists()
                assert not pool._pins
                if os.environ.get('D30B_FACTS_DIR'):
                    directory = Path(os.environ['D30B_FACTS_DIR'])
                    directory.mkdir(parents=True, exist_ok=True)
                    (directory / ('WORKER_' + lock_kind + ('_PHYSICAL' if physical_unknown else '') + '.json')).write_text(json.dumps({
                        'future_original_exception': True, 'future_after_cleanup': len(pump._active),
                        'canonical_sha256': hashlib.sha256(claim.path.read_bytes()).hexdigest(),
                        'canonical_status': canonical['status'], 'execution_outcome': canonical['execution_outcome'],
                        'claim_identity': [claim.request_id, claim.execution_attempt_id, claim.lease_epoch, claim.lease_owner],
                        'p3_before_sha256': hashlib.sha256(before_p3).hexdigest(),
                        'p3_after_sha256': hashlib.sha256(p3.read_bytes()).hexdigest(),
                        'terminal_exists': (server.paths.terminal / claim.path.name).exists(),
                        'permission_exists': handle.path.exists(), 'pool_pins': len(pool._pins),
                        'controls_during_busy': len(controls), 'physical_unknown': physical_unknown, 'followup_conversation_recovery': 'not_closed_by_this_slice',
                    }, ensure_ascii=False, indent=2) + '\n')
            finally:
                begin.set()
                physical_stop.set()
                for thread in physical_threads:
                    thread.join(5)
                    assert not thread.is_alive()
                pump._executor.shutdown(wait=True)
                pump.controls.close()
