# LLM: 原C/P4/issuer/P1/collector/publisher不替换；物理域仅复用ControlledPopen和原waitid观察的fake native端点，不运行host/native/模型。
# 模块用途: 验证合法非空v2 producer与后继同次旧scope停止，并保无整体前提的v1未知。
import os
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import daemon_metadata
from agent_py_agent.agent.gateway_parts import multiprocess_generation_metadata as history
from agent_py_agent.agent.gateway_parts import multiprocess_takeover as p2
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_drain_process import (
    OriginalWorkerExitObservation,
)
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.agent.gateway_parts.multiprocess_executor_original_source import (
    OriginalCExecutorSource,
)
from agent_py_agent.agent.gateway_parts.multiprocess_records import serialize_record
from agent_py_agent.agent.gateway_parts.multiprocess_route import owner_digest
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_gateway_role_lifecycle_closure import (
    context as context,
)
from agent_py_agent.tests.test_gateway_role_lifecycle_closure import (
    coordinator_handles as coordinator_handles,
)
from agent_py_agent.tests.test_gateway_worker_runtime import (
    ControlledPopen,
)
from agent_py_agent.tests.test_gateway_worker_runtime import (
    physical as physical,
)


# LLM: 此类只是物理native端点替身，停止来自原退出观察器的受控waitid；不签closure/P2或伪造实际内核通过。
# 类用途: 在普通组件测试中保留实际原source/P1/Popen的完整对应关系。
class ControlledDomain:
    # LLM: 保存原source与原observer；不恢复JSON来源或创建系统域。
    # 函数用途: 固定本测试已通过原issuer发布的完整记录。
    def __init__(self, source, row, process, observer):
        self.source, self.row, self.process, self.observer = source, row, process, observer
        self.reserved = row
        self._origin = self

    # LLM: 只读原来源和唯一P1，变化仍拒绝，不用returncode猜停止。
    # 函数用途: 检查受控端点没有换壳或更换记录。
    def _require_original(self):
        self.source._require_source_identity()
        assert self._origin is self and self.source.read(self.row['reservation_id']) == self.row
        self.observer.require_original()

    # LLM: 返回原观察器同process/birth的真实受控waitid结果，不提供常量True授权。
    # 函数用途: 模拟内核端点是否已结束，仅用于组件collector接线。
    def empty(self, process, row):
        self._require_original()
        return (process is self.process and row == self.row and self.observer.observe()
                and self.observer.confirmed
                and self.observer.identity_bytes == serialize_record(row['process_identity']))


# LLM: 原W退出标记是既有受控物理fixture的投影，C/P4/observer仍走原生产API，不代表真实W运行。
# 函数用途: 完成测试内原worker退出和规范heartbeat，随后停止本runtime producer。
def stop_physical(runtime, physical):
    for lane, process, _observer in physical._created_origins:
        if getattr(lane, 'executor_lane', None) is None:
            lane.executor_lane = SimpleNamespace(session=SimpleNamespace(_execution_domains=(), _hosts={}))
        process.exit_status = 0
    physical.close(defer_native=True)
    root = runtime.control.paths.root
    registry = history.deserialize_record((root / 'workers/registry.json').read_bytes())
    write_json_file_atomic(root / 'gateway_state.json', {'status': 'stopped', 'pid': os.getpid(),
                          'c_run_id': runtime.scope.c_run_id})
    for row in registry['workers']:
        write_json_file_atomic(root / 'workers' / f"w{row['worker']}.json", {
            'schema_version': 'multiprocess_worker_heartbeat.v1', 'worker': row['worker'],
            'of': runtime.scope.count, 'pid': row['process_identity']['pid'],
            'process_identity': row['process_identity'], 'c_run_id': runtime.scope.c_run_id,
            'instance_id': row['instance_id'], 'ready': False, 'loop_health': 'stopped',
            'inflight': 0, 'updated_at': 1.0, 'readiness': {'ready': False, 'reason': 'stopped'}})
    runtime.stop_producers()


# LLM: 预留/登记只由原OriginalCExecutorSource和实际issuer完成；mock端点仅替物理创建与waitid，不手写closure或P2。
# 函数用途: 在停止producer前创建一条非空原P1和可独立消费的物理停止来源。
def attach_original_domain(physical, monkeypatch):
    from agent_py_agent.agent.gateway_parts import multiprocess_drain_trees as trees
    from agent_py_agent.agent.gateway_parts import multiprocess_native_linux_domain as native

    monkeypatch.setattr(native, 'NativePidNamespaceDomain', ControlledDomain)
    monkeypatch.setattr(trees, 'sys', SimpleNamespace(platform='linux'))
    lane = physical.native_lanes[0]
    owner = next(OwnerIdentity.provider_user('synthetic', f'domain-{i}') for i in range(100)
                 if int(owner_digest(OwnerIdentity.provider_user('synthetic', f'domain-{i}')), 16) % physical.control.worker_count == 0)
    source = OriginalCExecutorSource(lane.issuer, owner)
    row = source.reserve({'schema_version': 'multiprocess_executor_reservation.v1',
        'reservation_id': 'component-' + physical.control.c_run_id, 'kind': 'shell',
        'c_run_id': physical.control.c_run_id, 'owner_sha': owner_digest(owner),
        'request_id': 'component-request', 'execution_attempt_id': 'component-attempt',
        'lease_epoch': 0, 'tool_call_id': 'component-tool', 'state': 'reserved',
        'process_identity': None, 'pgid': None, 'sid': None})
    process = source.perform_short_effect(row, 'executor_spawn', lambda: ControlledPopen(
        ('controlled-endpoint',), stdin=None, env={}))
    identity = {'schema_version': 'multiprocess_process_identity.v1', 'host_id': daemon_metadata.process_host_id(),
                'pid': process.pid, 'start_time': f'controlled:{process.pid}'}
    observer = OriginalWorkerExitObservation(process, identity)
    observer.install()
    row = source.advance(row, 'registered', {'process_identity': identity, 'pgid': process.pid, 'sid': process.pid})
    domain = ControlledDomain(source, row, process, observer)
    process.exit_status = 0
    lane.executor_lane = SimpleNamespace(session=SimpleNamespace(_execution_domains=(domain,), _hosts={}))
    return row


def test_legal_nonempty_v2_producer_then_successor_fresh_old_scope_seals(
    physical, monkeypatch, coordinator_handles,
):
    from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
    from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
        prepare_coordinator_takeover,
    )
    from agent_py_agent.cli.gateway_worker_runtime import CoordinatorWorkers

    physical.start()
    row = attach_original_domain(physical, monkeypatch)
    runtime = GatewayDrainRuntime(physical.control)
    runtime.retain_workers(physical)
    stop_physical(runtime, physical)
    assessments = runtime.assess()
    assert all(a.drained for a in assessments), assessments
    runtime.seal_generation()
    original = history.read_generation_metadata(runtime.control.paths.root, runtime.scope.c_run_id)
    assert original[0]['closure']['source_files'] and original[0]['closure']['tree_evidence']
    coordinator_handles['close']()
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live', lambda _identity: False)
    samples = []

    def observe():
        samples.append(True)
        return {'platform': 'linux', 'host_id': row['process_identity']['host_id'], 'rows': {}, 'failure': ''}

    monkeypatch.setattr(p2, 'observe_processes', observe)
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    paths, count = gateway_paths_from_root(runtime.control.paths.root), runtime.scope.count
    with coordinator_guard(paths.root, count):
        controller = prepare_coordinator_takeover(paths, count, 'successor-c')
        assert not controller._all_quarantined and not controller._quarantined
        control = GatewayControlPlane(runtime.control.config, runtime.control.home_root, paths, count, 'successor-c',
            agent_root=runtime.control.agent_root, workspace_roots=runtime.control.workspace_roots)
        workers = CoordinatorWorkers(control, config_path=paths.root / 'synthetic.yaml', http_port=43123)
        try:
            workers.start()
            attach_original_domain(workers, monkeypatch)
            successor = GatewayDrainRuntime(control)
            successor.retain_workers(workers)
            stop_physical(successor, workers)
            before = len(samples)
            assessments = successor.assess()
            assert all(a.drained for a in assessments), assessments
            assert len(samples) >= before + 2
            observation = successor._generation_observation
            assert len(observation[4]) == 1 and observation[4][0][0].scope['c_run_id'] == runtime.scope.c_run_id
            fresh_count = len(samples)
            successor.prepare_generation_closure()
            assert successor._generation_closing_sources[0] is observation and len(samples) == fresh_count
            successor.seal_generation()
            closed = history.read_generation_metadata(paths.root, 'successor-c')[0]['closure']
            assert original[0]['closure']['source_files'].items() <= closed['source_files'].items()
            assert len(closed['source_files']) == 2
            assert set(original[0]['closure']['tree_evidence']) <= set(closed['tree_evidence'])
        finally:
            workers.close()
    third_paths = gateway_paths_from_root(paths.root)
    with coordinator_guard(third_paths.root, count):
        controller = prepare_coordinator_takeover(third_paths, count, 'third-c')
        assert not controller._all_quarantined and not controller._quarantined
        control = GatewayControlPlane(runtime.control.config, runtime.control.home_root, third_paths, count, 'third-c',
            agent_root=runtime.control.agent_root, workspace_roots=runtime.control.workspace_roots)
        third_workers = CoordinatorWorkers(control, third_paths.root / 'synthetic.yaml', 43123)
        try:
            third_workers.start()
            attach_original_domain(third_workers, monkeypatch)
            third = GatewayDrainRuntime(control)
            third.retain_workers(third_workers)
            stop_physical(third, third_workers)
            assert all(a.drained for a in third.assess())
            assert {target.scope['c_run_id'] for target, *_rest in third._generation_observation[4]} == {
                runtime.scope.c_run_id, 'successor-c'}
            third.seal_generation()
            final = history.read_generation_metadata(third_paths.root, 'third-c')[0]['closure']
            assert len(final['source_files']) == 3
            assert closed['source_files'].items() <= final['source_files'].items()
            assert closed['tree_evidence'].items() <= final['tree_evidence'].items()
        finally:
            third_workers.close()


def test_failed_consumption_cannot_fallback_to_another_assessment(physical, monkeypatch):
    physical.start()
    runtime = GatewayDrainRuntime(physical.control)
    runtime.retain_workers(physical)
    stop_physical(runtime, physical)
    assert all(a.drained for a in runtime.assess())
    calls = []

    def failed_close():
        calls.append(True)
        raise RuntimeError('controlled native close failure')

    original_close = physical.close_native
    monkeypatch.setattr(physical, 'close_native', failed_close)
    with pytest.raises(RuntimeError):
        runtime.prepare_generation_closure()
    assert runtime._generation_observation is None and runtime._generation_consume_attempted
    with pytest.raises(ValueError):
        runtime.prepare_generation_closure()
    assert calls == [True]
    monkeypatch.setattr(physical, 'close_native', original_close)


def test_late_unknown_observer_blocks_native_close_before_consumption(physical, monkeypatch):
    physical.start()
    runtime = GatewayDrainRuntime(physical.control)
    runtime.retain_workers(physical)
    stop_physical(runtime, physical)
    assert all(a.drained for a in runtime.assess())
    observer = physical._created_origins[0][2]
    observer._unknown = True
    calls = []
    original = physical.close_native
    monkeypatch.setattr(physical, 'close_native', lambda: calls.append(True))
    try:
        with pytest.raises(ValueError):
            runtime.prepare_generation_closure()
        assert not calls and runtime._generation_observation is None
        with pytest.raises(ValueError):
            runtime.prepare_generation_closure()
        assert not calls
    finally:
        observer._unknown = False
        monkeypatch.setattr(physical, 'close_native', original)


def test_failed_new_assessment_clears_previous_success(physical):
    physical.start()
    runtime = GatewayDrainRuntime(physical.control)
    runtime.retain_workers(physical)
    stop_physical(runtime, physical)
    assert all(a.drained for a in runtime.assess())
    observer = physical._created_origins[0][2]
    observer._unknown = True
    try:
        assert not all(a.drained for a in runtime.assess())
        assert runtime._generation_observation is None
        with pytest.raises(ValueError):
            runtime.prepare_generation_closure()
    finally:
        observer._unknown = False


def test_v1_local_fresh_p1_success_never_supplies_overall_role_closure(physical, monkeypatch):
    from agent_py_agent.agent.gateway_parts.multiprocess_executor_ledger import ExecutorLedger
    from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition
    from agent_py_agent.agent.gateway_parts.multiprocess_takeover_sources import (
        discover_takeover_targets,
    )

    physical.start()
    paths, count = physical.control.paths, physical.control.worker_count
    history._publish_generation_once(paths.root, {'schema_version': 'multiprocess_c_generation.v1',
        'c_run_id': 'v1-only', 'worker_count': count})
    owner = owner_digest(OwnerIdentity.local_main())
    ledger = ExecutorLedger(paths.root, WorkerPartition(int(owner, 16) % count, count, 'v1-only'))
    row = ledger.reserve({'schema_version': 'multiprocess_executor_reservation.v1', 'reservation_id': 'v1-domain',
        'kind': 'shell', 'c_run_id': 'v1-only', 'owner_sha': owner, 'request_id': 'v1-request',
        'execution_attempt_id': 'v1-attempt', 'lease_epoch': 0, 'tool_call_id': 'v1-tool', 'state': 'reserved',
        'process_identity': None, 'pgid': None, 'sid': None})
    row = ledger.advance(row, 'registered', {'process_identity': {'schema_version': 'multiprocess_process_identity.v1',
        'host_id': 'controlled-host', 'pid': 876543, 'start_time': 'original-birth'}, 'pgid': 876543, 'sid': 876543})
    ledger.advance(row, 'released')
    monkeypatch.setattr(p2, 'observe_processes', lambda: {
        'platform': 'linux', 'host_id': 'controlled-host', 'rows': {}, 'failure': ''})
    target, = discover_takeover_targets(paths, count, current_c_run_id=physical.control.c_run_id)
    result = physical.control.controller().prepare_target(target)
    assert result['error'] is None and result['proof']['status'] == 'stopped'
    runtime = GatewayDrainRuntime(physical.control)
    runtime.retain_workers(physical)
    stop_physical(runtime, physical)
    assert not all(a.drained for a in runtime.assess())
    with pytest.raises(ValueError):
        runtime.seal_generation()
    assert history.read_generation_metadata(paths.root, runtime.scope.c_run_id)[0]['closure'] is None
