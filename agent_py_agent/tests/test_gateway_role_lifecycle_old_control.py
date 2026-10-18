# LLM: 原C/P4/转发/WorkerControlPump状态机真实，W物理身份只复用受控Popen/P4边界；领域为普通fake tool，不运行native或模型。
# 模块用途: 核合法已完成旧dispatch在完整原v2关闭范围中，后继停止是否仍误报旧代未知。
import json
import time
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import ConversationControlResult
from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.agent.gateway_parts import daemon_metadata
from agent_py_agent.agent.gateway_parts import multiprocess_control_worker as worker
from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import freeze_worker_bootstrap
from agent_py_agent.agent.gateway_parts.multiprocess_control_forward import prepare_remote_control
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.agent.gateway_parts.multiprocess_route import owner_digest
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_gateway_role_lifecycle_closure import (
    context as context,
)
from agent_py_agent.tests.test_gateway_role_lifecycle_closure import (
    coordinator_handles as coordinator_handles,
)
from agent_py_agent.tests.test_gateway_role_lifecycle_successor import stop_physical
from agent_py_agent.tests.test_gateway_worker_runtime import physical as physical


# LLM: 只模拟原W物理进程已运行，C/owner/dispatch/P4来自原producer；不手写completed或旧closure。
# 函数用途: 由原控制pump消费一次原转发操作，取得真实FSM完成回执。
def complete_original_control(physical, monkeypatch):
    paths, count = physical.control.paths, physical.control.worker_count
    owner = next(OwnerIdentity.provider_user('synthetic', f'control-{i}') for i in range(100)
                 if int(owner_digest(OwnerIdentity.provider_user('synthetic', f'control-{i}')), 16) % count == 0)
    scope = GatewayControlScope(user_id=owner.owner_id, channel='synthetic', conversation_id='control-room',
        metadata={'message_id': 'original-control'}, resolved_owner=owner)
    operation, digest = ops.gateway_control_operation_identity(scope, '/help')
    receipt = ops._new_control_operation_receipt(scope, 'help', '/help', operation, digest)
    receipt = prepare_remote_control(None, paths, receipt, fresh=True, control_context=physical.control)
    lane, process, observer = physical._created_origins[0]
    identity = json.loads(observer.identity_bytes)
    write_json_file_atomic(paths.root / 'workers/w0.json', {
        'schema_version': 'multiprocess_worker_heartbeat.v1', 'worker': 0, 'of': count, 'pid': process.pid,
        'process_identity': identity, 'c_run_id': physical.control.c_run_id, 'instance_id': lane.launch.instance_id,
        'ready': True, 'loop_health': 'healthy', 'inflight': 0, 'updated_at': time.time(),
        'readiness': {'ready': True, 'gateway_ports': [43123]}})
    with monkeypatch.context() as child:
        child.setattr(mode, '_WORKER_PARTITION', lane.launch.partition)
        child.setattr(worker, 'os', SimpleNamespace(getpid=lambda: process.pid))
        child.setattr(worker, 'process_birth', lambda pid: identity['start_time'] if pid == process.pid else None)
        child.setattr(ops, 'execute_gateway_conversation_control', lambda _a, _p, command, _s:
                      ConversationControlResult(command.kind, True, '普通组件工具结果'))
        pool = SimpleNamespace(get=lambda _owner: SimpleNamespace(), pin=lambda _agent: nullcontext())
        pump = worker.WorkerControlPump(freeze_worker_bootstrap(lane.launch), pool)
        try:
            assert pump.poll() == 1
        finally:
            pump.close()
    actual = ops.read_gateway_control_operation(paths, receipt.operation_id, read_only=True)
    assert actual.state == 'completed' and actual.result['ok']
    assert actual.dispatch['c_run_id'] == physical.control.c_run_id
    return actual


def test_original_completed_dispatch_in_closed_v2_does_not_block_successor_drain(
    physical, monkeypatch, coordinator_handles,
):
    from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
    from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
        prepare_coordinator_takeover,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.cli.gateway_worker_runtime import CoordinatorWorkers

    physical.start()
    receipt = complete_original_control(physical, monkeypatch)
    runtime = GatewayDrainRuntime(physical.control)
    runtime.retain_workers(physical)
    stop_physical(runtime, physical)
    runtime.seal_generation()
    coordinator_handles['close']()
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live', lambda _identity: False)
    paths, count = gateway_paths_from_root(runtime.control.paths.root), runtime.scope.count
    for generation in ['control-successor', 'control-third']:
        with coordinator_guard(paths.root, count):
            controller = prepare_coordinator_takeover(paths, count, generation)
            assert not controller._all_quarantined
            control = GatewayControlPlane(runtime.control.config, runtime.control.home_root, paths, count, generation,
                agent_root=runtime.control.agent_root, workspace_roots=runtime.control.workspace_roots)
            workers = CoordinatorWorkers(control, paths.root / 'synthetic.yaml', 43123)
            try:
                workers.start()
                successor = GatewayDrainRuntime(control)
                successor.retain_workers(workers)
                stop_physical(successor, workers)
                before = ops.gateway_control_operation_path(paths, receipt.operation_id).read_bytes()
                from agent_py_agent.agent.gateway_parts.multiprocess_drain_sources import (
                    durable_drain_snapshot,
                    durable_items,
                )
                with pytest.raises(ValueError, match="^drain_control_generation_unconfirmed$"):
                    durable_items(successor.scope, durable_drain_snapshot(paths, count))
                assessments = successor.assess()
                assert all(item.drained for item in assessments), assessments
                assert ops.gateway_control_operation_path(paths, receipt.operation_id).read_bytes() == before
                snapshot = durable_drain_snapshot(paths, count)
                closed = tuple(controller._closed_role_sources)
                assert not any(item.kind == 'control' for item in durable_items(successor.scope, snapshot, closed_sources=closed))
                with pytest.raises(ValueError, match="^drain_control_generation_unconfirmed$"):
                    durable_items(replace(successor.scope, count=count + 1), snapshot, closed_sources=closed)
                directories, namespace, targets = snapshot
                control_identity, rows = directories[2]
                changed_rows = tuple((name, content + b' ' if name.endswith('.json') else content) for name, content in rows)
                changed = (*directories[:2], (control_identity, changed_rows))
                with pytest.raises(ValueError, match="^drain_control_generation_unconfirmed$"):
                    durable_items(successor.scope, (changed, namespace, targets), closed_sources=closed)
                successor.seal_generation()
            finally:
                workers.close()
        paths = gateway_paths_from_root(paths.root)
