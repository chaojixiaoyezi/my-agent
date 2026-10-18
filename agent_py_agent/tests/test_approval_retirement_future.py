"""退休未结穿过原Count0 executor，必须由原Future接收且不普通终结。"""
import contextvars
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.runtime_mixin import _bind_main_agent_authority
from agent_py_agent.agent.contracts.tool_approval import build_tool_approval_request
from agent_py_agent.agent.conversation import agent_tool_approval as approval
from agent_py_agent.agent.gateway_parts import queue_service, request_execution
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.request_binding import GatewayTaskBindingWriter
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.cli import gateway_loops as loops
from agent_py_agent.cli.models import GatewayRunContext
from agent_py_agent.tests.test_approval_waiter_retirement import held_original_lock
from agent_py_agent.tests.test_tool_approval_foreground_scope import _foreground


@pytest.mark.parametrize("lock_kind", ["permission", "json"])
def test_count0_original_future_keeps_actual_waiter_retirement(tmp_path, monkeypatch, lock_kind):
    world = _foreground(tmp_path, kind='user')
    agent, paths = world.agent, world.paths
    # 重新经原queue claim创建本轮真实transport；旧helper的内存Params不拿来冒充本轮。
    assert read_json_file(world.path)["id"] == "message"
    world.path.unlink()
    queue_service.ensure_gateway_folders(paths)
    pending = paths.inbox / 'future-request.json'
    write_json_file_atomic(pending, {'id': 'future-request', 'kind': 'ask', 'goal': '合成审批取消',
        'status': 'pending', 'user_id': 'ou_synthetic', 'metadata': {'channel': 'feishu'}})
    claim = queue_service.claim_request(paths, pending)
    assert claim is not None and claim.execution_attempt_id
    original_actor = world.params.host_channel_actor
    old_claim = world.store.claims.load_report(world.thread.thread_id)[0]
    world.store.claims.finish({'thread_id': world.thread.thread_id, 'claim_id': old_claim['claim_id'], 'status': 'finished'})
    ready, begin = threading.Event(), threading.Event()
    original = {}

    def original_waiter(actual_agent, actual_paths, actual_path, worker_id, *, claim):
        assert actual_agent is agent and actual_paths is paths and actual_path == claim.path
        writer = GatewayTaskBindingWriter(claim.path, claim.request_id, execution_attempt_id=claim.execution_attempt_id)
        writer.bind_conversation_claim(world.thread.thread_id)
        world.store.claims.acquire({'thread_id': world.thread.thread_id, 'task_id': 'gateway:' + claim.request_id,
                                   'reason': 'gateway_foreground_turn'})
        params = _bind_main_agent_authority(agent, RunParams(request_id=claim.request_id, run_id='future-main-run',
            task_id='durable-task', source='gateway', context_scope='conversation',
            task_attributes={'conversation_thread_id': world.thread.thread_id, 'conversation_task_id': 'durable-task'},
            host_channel_actor=original_actor, conversation_task_binding_callback=writer))
        agent._current_run_params = params
        request = build_tool_approval_request(ToolCall(call_id='future-approval', tool_name='controlled_exec',
            arguments={'apply': True}, source_protocol='native', schema_hash='sha256:future-approval',
            run_id='durable-task', turn_id='future-turn', attempt_id=params.attempt_id),
            request_id=claim.request_id, round_number=1, call_index=1, description='原审批取消')
        handle = approval.publish_agent_tool_approval(agent, run_id='durable-task',
            thread_id=world.thread.thread_id, request_value=request)
        original['handle'] = handle
        original['context'] = contextvars.copy_context()
        ready.set()
        assert begin.wait(5)
        try:
            approval.wait_for_agent_tool_approval(handle, cancellation_token=SimpleNamespace(cancelled=True))
        except approval.ApprovalRetirementUnconfirmedError as error:
            original['error'] = error
            raise
        finally:
            del agent._current_run_params

    def actual_handler(actual_agent, actual_paths, actual_path, worker_id, *, claim):
        assert actual_agent is agent and actual_paths is paths and actual_path == claim.path
        return request_execution._handle_gateway_request(actual_agent, actual_path, worker_id=worker_id)

    def fake_business(context, on_chunk):
        # 原handler/审计/lease/observer/catch真实运行；仅无模型业务体发布原typed审批并取消。
        return original_waiter(context['agent'], paths, context['request_path'], claim.lease_owner, claim=claim)

    monkeypatch.setattr(request_execution, '_execute_gateway_request_body', fake_business)
    monkeypatch.setattr(loops, '_process_claimed_gateway_request_path', actual_handler)
    dispatcher = loops._RequestDispatcher(GatewayRunContext(agent, paths, Path('synthetic.yaml')), paths)
    submitted = []
    submit = dispatcher._executor.submit

    def observe_submit(*args, **kwargs):
        future = submit(*args, **kwargs)
        submitted.append(future)
        return future

    monkeypatch.setattr(dispatcher._executor, 'submit', observe_submit)
    try:
        dispatcher._submit(claim, 'synthetic-user-key', 'synthetic-conversation-key')
        assert ready.wait(5)
        handle = original['handle']
        lock_path = approval._transition_path(handle.path) if lock_kind == 'permission' else handle.path
        with held_original_lock(lock_path):
            begin.set()
            deadline = time.monotonic() + 5
            while not submitted[0].done():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            future = submitted[0]
            assert future.exception() is original['error']
            assert dispatcher._request_futures[future][0] is claim
            assert not (paths.terminal / claim.path.name).exists()
            assert approval.approval_retirement_obligation(future.exception()).decision.decision == 'cancelled'
            assert not approval.approval_retirement_canonical_received(future.exception(), claim)
            dispatcher._collect_request_futures()
            assert future in dispatcher._request_futures
            with pytest.raises(approval.ApprovalRetirementUnconfirmedError) as pending_error:
                dispatcher.shutdown()
            assert pending_error.value is original['error']
        dispatcher._collect_request_futures()
        # 裸wait/手动Params没有真实ask未入handler和运行终态核收；permission清理不能删除业务未知责任。
        assert future in dispatcher._request_futures
        assert not handle.path.exists()
        with pytest.raises(approval.ApprovalRetirementUnconfirmedError) as still_pending:
            dispatcher.shutdown()
        assert still_pending.value is original['error']
        canonical = read_json_file(claim.path)
        assert canonical['status'] == 'processing' and canonical['execution_outcome']['status'] == 'unknown'
        assert canonical['execution_outcome']['approval_retirement'] == approval.approval_retirement_diagnostic(original['error'])
        assert canonical['execution_outcome']['operation_entered'] is None
        assert not (paths.terminal / claim.path.name).exists()
        assert world.store.claims.load_report(world.thread.thread_id)[0]['status'] == 'running'
        from agent_py_agent.agent.gateway_parts.io import gateway_request_fingerprint
        assert not original['context'].run(approval.approval_retirement_request_matches, original['error'], claim.path,
            canonical, gateway_request_fingerprint(canonical, claim.request_id), agent=agent)
        if os.environ.get('D30B_FACTS_DIR'):
            directory = Path(os.environ['D30B_FACTS_DIR'])
            directory.mkdir(parents=True, exist_ok=True)
            (directory / ('COUNT0_' + lock_kind + '.json')).write_text(json.dumps({
                'future_original_exception': True, 'future_after_cleanup': len(dispatcher._request_futures),
                'canonical_sha256': hashlib.sha256(claim.path.read_bytes()).hexdigest(),
                'canonical_status': canonical['status'], 'execution_outcome': canonical['execution_outcome'],
                'claim_identity': [claim.request_id, claim.execution_attempt_id, claim.lease_epoch, claim.lease_owner],
                'conversation_claim_status': world.store.claims.load_report(world.thread.thread_id)[0]['status'],
                'terminal_exists': (paths.terminal / claim.path.name).exists(), 'permission_exists': handle.path.exists(),
                'late_context_observation_allowed': False, 'followup_conversation_recovery': 'not_closed_by_this_slice',
            }, ensure_ascii=False, indent=2) + '\n')
    finally:
        begin.set()
        dispatcher._executor.shutdown(wait=True)
