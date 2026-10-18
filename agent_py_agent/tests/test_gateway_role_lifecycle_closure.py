# LLM: 原C/Root/P4/collector/publisher真实；只复用既有受控Popen/native端点，不构造关闭JSON当成功，不启动服务/native/模型。
# 模块用途: 验证正常producer一次封存和新C角色消费，未知/漂移/缺原来源必须继续拒绝；同包.pyc运行兼容。
import copy
import hashlib
import json
import os

import pytest

from agent_py_agent.agent.gateway_parts import daemon_metadata
from agent_py_agent.agent.gateway_parts import multiprocess_generation_metadata as history
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.agent.gateway_parts.multiprocess_records import (
    deserialize_record,
    serialize_record,
)
from agent_py_agent.agent.gateway_parts.multiprocess_takeover_sources import require_role_coverage
from agent_py_agent.tests.test_gateway_worker_runtime import physical as physical


# LLM: 本测试context亲自取得原C guard及control，不构造替身租约；句柄仅供fixture准确关闭，不给产品新增字段。
# 函数用途: 覆盖N=1/2的原正常组合与同home新C，隔离所有配置与workspace。
@pytest.fixture(params=[1, 2])
def context(tmp_path, monkeypatch, request, coordinator_handles):
    from contextlib import ExitStack

    from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
    from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
        prepare_coordinator_takeover,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.settings import AgentConfig

    home = tmp_path / 'home'
    monkeypatch.setenv('MY_AGENT_HOME', str(home))
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    config = AgentConfig(my_agent_home=str(home))
    with ExitStack() as stack:
        stack.enter_context(coordinator_guard(paths.root, request.param))
        controller = prepare_coordinator_takeover(paths, request.param, 'fixture-c')
        control = GatewayControlPlane(config, home, paths, request.param, 'fixture-c',
                                      agent_root=tmp_path, workspace_roots=(tmp_path,))
        assert controller.active()
        coordinator_handles['close'] = stack.close
        yield control


# LLM: 仅本测试的原guard关闭句柄，不进入产品或持久化，不能当租约/权限来源。
# 函数用途: 让同home重启用例准确结束自己创建的原C上下文。
@pytest.fixture
def coordinator_handles():
    return {}


# LLM: 物理端点替身仅表示测试内未创建业务域；退出仍由原observer及受控waitid生产，不签P2或真实native通过。
# 函数用途: 用既有真实C/worker组合准备完整正常关闭，marker只是该受控物理W的原格式投影。
@pytest.fixture
def closing(physical):
    from types import SimpleNamespace

    physical.start()
    runtime = GatewayDrainRuntime(physical.control)
    runtime.retain_workers(physical)
    root = physical.control.paths.root
    for lane, process, _observer in physical._created_origins:
        lane.executor_lane = SimpleNamespace(session=SimpleNamespace(_execution_domains=(), _hosts={}))
        process.exit_status = 0
    physical.close(defer_native=True)
    registry = history.deserialize_record((root / 'workers/registry.json').read_bytes())
    write_json_file_atomic(root / 'gateway_state.json', {'status': 'stopped', 'pid': os.getpid(),
                            'c_run_id': physical.control.c_run_id})
    for row in registry['workers']:
        heartbeat = {'schema_version': 'multiprocess_worker_heartbeat.v1', 'worker': row['worker'],
            'of': registry['worker_count'], 'pid': row['process_identity']['pid'],
            'process_identity': row['process_identity'], 'c_run_id': registry['c_run_id'],
            'instance_id': row['instance_id'], 'ready': False, 'loop_health': 'stopped',
            'inflight': 0, 'updated_at': 1.0, 'readiness': {'ready': False, 'reason': 'stopped'}}
        write_json_file_atomic(root / 'workers' / f"w{row['worker']}.json", heartbeat)
    runtime.stop_producers()
    yield runtime


def test_original_normal_producer_seals_once_and_keeps_full_role_scope(closing):
    root = closing.control.paths.root
    before = history.read_generation_metadata(root, closing.scope.c_run_id)
    assert before[0]['closure'] is None
    reference = closing.seal_generation()
    record, raw = history.read_generation_metadata(root, closing.scope.c_run_id)
    assert reference == '/'.join(history.generation_metadata_parts(closing.scope.c_run_id))
    assert record['closure']['source_files'] == {}
    assert set(record['closure']['role_markers']) == {'gateway_state.json', *(f'workers/w{i}.json' for i in range(closing.scope.count))}
    assert len(record['closure']['registry']['workers']) == closing.scope.count
    assert all(lane.closed and observer.confirmed for lane, _process, observer in closing._workers._created_origins)
    with pytest.raises(ValueError):
        closing.seal_generation()
    assert history.read_generation_metadata(root, closing.scope.c_run_id) == (record, raw)


def test_sealed_history_consumer_requires_all_exact_old_births_ended(closing, monkeypatch):
    closing.seal_generation()
    paths = closing.control.paths
    with pytest.raises(ValueError, match='old_role_lifecycle_unproven'):
        require_role_coverage(paths, 'next-c', count=closing.scope.count)
    # 仅模拟本夹具受控C/W物理观察边界；原publisher/完整闭合检查仍是上面的实际执行。
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live', lambda _identity: False)
    captured = require_role_coverage(paths, 'next-c', count=closing.scope.count)
    assert len(captured) == 1 and captured[0][0] == closing.scope.c_run_id
    assert captured[0][1] == history.read_generation_metadata(paths.root, closing.scope.c_run_id)[1]


@pytest.mark.parametrize('fault', ['copy', 'not_closing', 'missing_worker', 'unconfirmed', 'native_close', 'role_changed'])
def test_original_producer_faults_never_publish_closed_history(closing, monkeypatch, fault):
    runtime = closing
    original_origins = runtime._workers._created_origins
    original_native_close = runtime._workers.close_native
    if fault == 'copy':
        runtime = copy.copy(closing)
    elif fault == 'not_closing':
        runtime._closing = False
    elif fault == 'missing_worker':
        runtime._workers._created_origins = runtime._workers._created_origins[:-1]
    elif fault == 'unconfirmed':
        runtime._workers._created_origins[0][2]._unknown = True
    elif fault == 'native_close':
        def fail():
            raise RuntimeError('controlled_native_close_unknown')
        monkeypatch.setattr(runtime._workers, 'close_native', fail)
    else:
        path = runtime.control.paths.root / 'workers/w0.json'
        value = json.loads(path.read_bytes())
        value['instance_id'] = 'different-original-instance'
        write_json_file_atomic(path, value)
    before = history.read_generation_metadata(closing.control.paths.root, closing.scope.c_run_id)
    with pytest.raises((ValueError, RuntimeError)):
        runtime.seal_generation()
    assert history.read_generation_metadata(closing.control.paths.root, closing.scope.c_run_id) == before
    if fault == 'missing_worker':
        runtime._workers._created_origins = original_origins
    if fault == 'native_close':
        runtime._workers.close_native = original_native_close


@pytest.mark.parametrize('fault', ['missing_history', 'role_digest', 'missing_worker', 'count', 'extra_old_source', 'history_changed'])
def test_old_generation_consumer_never_discards_missing_or_changed_sources(closing, monkeypatch, fault):
    closing.seal_generation()
    paths, generation = closing.control.paths, closing.scope.c_run_id
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live', lambda _identity: False)
    captured = require_role_coverage(paths, 'next-c', count=closing.scope.count)
    path = paths.root.joinpath(*history.generation_metadata_parts(generation))
    if fault == 'missing_history':
        path.unlink()
    elif fault == 'role_digest':
        marker = paths.root / 'workers/w0.json'
        marker.write_bytes(marker.read_bytes() + b' ')
    elif fault == 'missing_worker':
        (paths.root / 'workers/w0.json').unlink()
    elif fault == 'count':
        with pytest.raises(ValueError):
            require_role_coverage(paths, 'next-c', count=closing.scope.count + 1, captured=captured)
        return
    elif fault == 'extra_old_source':
        write_json_file_atomic(paths.root / 'admission/reservations/late.json', {'route': {'c_run_id': generation}})
    else:
        value = deserialize_record(path.read_bytes())
        value['closure']['role_markers']['workers/w0.json'] = hashlib.sha256(b'changed').hexdigest()
        path.write_bytes(serialize_record(value))
    with pytest.raises((ValueError, OSError)):
        require_role_coverage(paths, 'next-c', count=closing.scope.count, captured=captured)


def test_legacy_generation_and_marker_cannot_be_upgraded_or_repaired_by_successor(tmp_path):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root(tmp_path)
    value = {'schema_version': 'multiprocess_c_generation.v1', 'c_run_id': 'legacy-c', 'worker_count': 2}
    history._publish_generation_once(tmp_path, value)
    write_json_file_atomic(tmp_path / 'gateway_state.json', {'c_run_id': 'legacy-c', 'status': 'stopped', 'pid': 999999999})
    before = history.read_generation_metadata(tmp_path, 'legacy-c')
    with pytest.raises(ValueError, match='old_role_lifecycle_unproven'):
        require_role_coverage(paths, 'next-c', count=2)
    assert history.read_generation_metadata(tmp_path, 'legacy-c') == before


def test_original_complete_stop_then_same_home_new_coordinator(closing, monkeypatch, coordinator_handles):
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
    from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
        prepare_coordinator_takeover,
    )

    closing.seal_generation()
    paths = closing.control.paths
    record = history.read_generation_metadata(paths.root, closing.scope.c_run_id)
    coordinator_handles['close']()
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live', lambda _identity: False)
    with coordinator_guard(paths.root, closing.scope.count):
        successor = prepare_coordinator_takeover(paths, closing.scope.count, 'successor-c')
        assert successor.active() and not successor._all_quarantined
        assert successor._closed_role_sources == ((closing.scope.c_run_id, record[1]),)
        assert history.read_generation_metadata(paths.root, closing.scope.c_run_id) == record


def test_old_p4_identity_without_role_markers_is_not_pure_count_metadata(tmp_path):
    from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap_publish import (
        publish_worker_registry,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.cli.gateway_worker_runtime import worker_process_identity

    paths = gateway_paths_from_root(tmp_path)
    publish_worker_registry(tmp_path, {'schema_version': 'multiprocess_worker_registry.v1',
        'c_run_id': 'old-c', 'worker_count': 1, 'process_identity': worker_process_identity(os.getpid()),
        'http_port': 1, 'workers': []}, None)
    with pytest.raises(ValueError, match='old_role_lifecycle_unproven'):
        require_role_coverage(paths, 'next-c', count=1)
