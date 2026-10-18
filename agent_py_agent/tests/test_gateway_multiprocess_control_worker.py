# LLM: 使用真实原回执与W队列泵，内核出生和原登记是认领事实；业务观察替身不做分派。
# 模块用途: 验证C准备的控制在所属W执行一次，并保留完整目标和锁外执行。
import os
import time
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import (
    ConversationControlResult,
    parse_conversation_control,
)
from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.execution_target import GatewayExecutionTarget
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import freeze_worker_bootstrap
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap_ready import (
    registered_worker_heartbeat,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import (
    bind_coordinator_generation,
    coordinator_guard,
)
from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition, owner_worker
from agent_py_agent.agent.gateway_parts.multiprocess_worker_dispatch import WorkerRequestPump
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


def prepared_case(tmp_path, monkeypatch, text="/btw 合成控制"):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    owner = OwnerIdentity.provider_user("feishu", "alice")
    scope = GatewayControlScope(user_id="alice", channel="feishu", conversation_id="room",
                                metadata={"message_id": "control-a", "expected_turn_id": "original-turn",
                                          "expected_execution": GatewayExecutionTarget('original-thread', 'task', 'original-turn', 'original-claim', task_id='original-turn').to_dict()},
                                resolved_owner=owner, workspace=str(tmp_path / "workspace"))
    with coordinator_guard(paths.root, 2):
        bind_coordinator_generation(paths.root, 2, "control-generation")
        receipt = ops.execute_gateway_control_operation(SimpleNamespace(), paths,
                    parse_conversation_control(text), scope, command_text=text)
    launch = mode.WorkerLaunch(WorkerPartition(owner_worker(owner, 2), 2, "control-generation"),
                              paths.root, tmp_path / "home", os.getppid(), "control-instance")
    monkeypatch.setattr(mode, "_WORKER_PARTITION", launch.partition)
    heartbeat = registered_worker_heartbeat(launch, launch.instance_id, {"ready": True, "gateway_ports": [43210]}, time.time())
    registry = {"schema_version": "multiprocess_worker_registry.v1", "c_run_id": launch.partition.c_run_id,
                "worker_count": 2, "process_identity": heartbeat["process_identity"], "http_port": 43210,
                "workers": [{"worker": launch.partition.index, "instance_id": launch.instance_id,
                             "process_identity": heartbeat["process_identity"],
                             "executor_ledger_ref": f"workers/executors/w{launch.partition.index}"}]}
    write_json_file_atomic(paths.root / "workers" / "registry.json", registry)
    write_json_file_atomic(paths.root / "workers" / f"w{launch.partition.index}.json", heartbeat)
    owners = []
    pool = SimpleNamespace(get=lambda selected: (owners.append(selected), SimpleNamespace())[1], pin=lambda _: nullcontext())
    return paths, receipt, scope, freeze_worker_bootstrap(launch), pool, owners


@pytest.mark.parametrize("text", ["/stop", "/btw 合成控制", "/approve synthetic-token", "/admin status"])
def test_worker_pump_consumes_original_control_once_outside_operation_lock(tmp_path, monkeypatch, text):
    paths, receipt, scope, frozen, pool, owners = prepared_case(tmp_path, monkeypatch, text)
    seen = []

    def execute(_agent, _paths, command, bound):
        with ops.try_gateway_control_operation_transition(paths, receipt.operation_id) as free:
            assert free, "control operation lock held during effect"
        assert bound.resolved_owner == scope.resolved_owner
        assert bound.workspace == scope.workspace
        assert bound.metadata["expected_turn_id"] == "original-turn"
        assert bound.metadata["expected_execution"] == GatewayExecutionTarget('original-thread', 'task', 'original-turn', 'original-claim', task_id='original-turn').to_dict()
        seen.append(command.kind)
        return ConversationControlResult(command.kind, True, "已受理", request_id="original-turn")

    monkeypatch.setattr(ops, "execute_gateway_conversation_control", execute)
    pump = WorkerRequestPump(frozen, pool, 1)
    try:
        pump.poll()
    finally:
        pump.close()
    actual = ops.read_gateway_control_operation(paths, receipt.operation_id)
    assert actual.state == "completed"
    assert actual.result["request_id"] == "original-turn"
    assert owners == [scope.resolved_owner] and seen == [receipt.command_kind]
    assert actual.dispatch["executor"]["pid"] == os.getpid()
    second = WorkerRequestPump(frozen, pool, 1)
    second.poll()
    second.close()
    assert seen == [receipt.command_kind]


def test_control_in_foreign_partition_is_not_materialized(tmp_path, monkeypatch):
    paths, receipt, scope, frozen, pool, owners = prepared_case(tmp_path, monkeypatch)
    foreign = replace(frozen.launch.partition, index=1 - frozen.launch.partition.index)
    launch = replace(frozen.launch, partition=foreign)
    monkeypatch.setattr(mode, "_WORKER_PARTITION", foreign)
    pump = WorkerRequestPump(freeze_worker_bootstrap(launch), pool, 1)
    pump.poll()
    pump.close()
    assert owners == []
    assert ops.read_gateway_control_operation(paths, receipt.operation_id).state == "prepared"


@pytest.mark.parametrize('change', ['generation', 'instance', 'birth'])
def test_revoked_registration_never_constructs_owner(tmp_path, monkeypatch, change):
    from agent_py_agent.agent.gateway_parts.io import read_json_file
    from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import WorkerBootstrapError

    paths, receipt, _scope, frozen, pool, owners = prepared_case(tmp_path, monkeypatch)
    registry_path = paths.root / 'workers' / 'registry.json'
    registry = read_json_file(registry_path)
    if change == 'generation':
        registry['c_run_id'] = 'new-generation'
    elif change == 'instance':
        registry['workers'][0]['instance_id'] = 'new-instance'
    else:
        registry['workers'][0]['process_identity']['start_time'] = 'different-birth'
    write_json_file_atomic(registry_path, registry)
    pump = WorkerRequestPump(frozen, pool, 1)
    try:
        with pytest.raises(WorkerBootstrapError):
            pump.poll()
    finally:
        pump.close()
    assert owners == []
    assert ops.read_gateway_control_operation(paths, receipt.operation_id).state == 'prepared'


def test_partial_control_failure_is_unknown_and_not_replayed(tmp_path, monkeypatch):
    paths, receipt, _scope, frozen, pool, owners = prepared_case(tmp_path, monkeypatch)
    seen = []

    def fail(*_args):
        seen.append('partial-effect')
        raise RuntimeError('synthetic-control-failure')

    monkeypatch.setattr(ops, 'execute_gateway_conversation_control', fail)
    pump = WorkerRequestPump(frozen, pool, 1)
    pump.poll()
    pump.close()
    actual = ops.read_gateway_control_operation(paths, receipt.operation_id)
    assert actual.state == 'terminal_unknown' and actual.result == {}
    second = WorkerRequestPump(frozen, pool, 1)
    assert second.poll() == 0
    second.close()
    assert seen == ['partial-effect'] and len(owners) == 1


def test_revoked_executor_cannot_seal_completed_control(tmp_path, monkeypatch):
    import threading

    from agent_py_agent.agent.gateway_parts.io import read_json_file
    from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import WorkerBootstrapError

    paths, receipt, _scope, frozen, pool, _owners = prepared_case(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def effect(_agent, _paths, command, _scope):
        entered.set()
        assert release.wait(5)
        return ConversationControlResult(command.kind, True, 'synthetic-result')

    monkeypatch.setattr(ops, 'execute_gateway_conversation_control', effect)
    pump = WorkerRequestPump(frozen, pool, 1)
    try:
        pump.poll()
        assert entered.wait(5)
        registry_path = paths.root / 'workers' / 'registry.json'
        registry = read_json_file(registry_path)
        registry['workers'][0]['instance_id'] = 'replacement-instance'
        write_json_file_atomic(registry_path, registry)
    finally:
        release.set()
        with pytest.raises(WorkerBootstrapError):
            pump.close()
    actual = ops.read_gateway_control_operation(paths, receipt.operation_id)
    assert actual.state == 'executing' and actual.result == {}


# LLM: 共享目录中的坏原件不被修复/删除；真读取损坏，健康控制仍执行且独立轮询不触发整W停机。
# 函数用途: 覆盖独审指出的无关坏回执扩散及重复诊断问题。
@pytest.mark.parametrize('bad_kind', ['json', 'schema', 'digest', 'filename', 'claim_race'])
def test_bad_shared_receipt_does_not_stop_healthy_partition(tmp_path, monkeypatch, bad_kind):
    import threading

    from agent_py_agent.agent.gateway_parts import logging as gateway_logging
    from agent_py_agent.agent.gateway_parts import multiprocess_control_worker as controls
    from agent_py_agent.agent.gateway_parts.io import read_json_file
    from agent_py_agent.agent.gateway_parts.multiprocess_control_poller import ControlPollingLane

    paths, receipt, scope, frozen, pool, owners = prepared_case(tmp_path, monkeypatch)
    directory = paths.root / 'control_operations'
    bad_path = directory / ('invalid name.json' if bad_kind == 'filename' else 'gwctl-msg-00000000000000000000000000000000.json')
    if bad_kind in {'json', 'filename', 'claim_race'}:
        bad_path.write_text('{')
    else:
        data = receipt.to_dict()
        data['operation_id'] = bad_path.stem
        if bad_kind == 'schema':
            data['schema_version'] = 'invalid'
        else:
            data['input_digest'] = 'wrong-digest'
        write_json_file_atomic(bad_path, data)
    before = bad_path.read_bytes()
    original_read = ops.read_gateway_control_operation
    calls = {}

    if bad_kind == 'claim_race':
        # 初扫时坏件尚未出现；原操作锁内复读时才遇到损坏。
        def racing_read(actual_paths, operation_id, *, read_only=False):
            calls[operation_id] = calls.get(operation_id, 0) + 1
            if operation_id == bad_path.stem and calls[operation_id] == 1:
                return replace(receipt, operation_id=operation_id)
            return original_read(actual_paths, operation_id, read_only=read_only)

        monkeypatch.setattr(ops, 'read_gateway_control_operation', racing_read)

    errors, effects = [], []
    monkeypatch.setattr(gateway_logging, '_report_gateway_side_effect_error', lambda *args: errors.append(args))

    def effect(_agent, _paths, command, _scope):
        effects.append(command.kind)
        return ConversationControlResult(command.kind, True, '已受理')

    monkeypatch.setattr(ops, 'execute_gateway_conversation_control', effect)
    pump = controls.WorkerControlPump(frozen, pool)
    stop = threading.Event()
    lane = ControlPollingLane(pump, stop)
    try:
        lane.start()
        lane._thread.join(0.05)
        deadline = time.monotonic() + 5
        while not stop.is_set() and ops.read_gateway_control_operation(paths, receipt.operation_id).state != 'completed' and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not stop.is_set(), 'one damaged receipt stopped the whole worker'
        assert ops.read_gateway_control_operation(paths, receipt.operation_id).state == 'completed'
        pump.poll()
        pump.poll()
        assert len(errors) == 1 and errors[0][1] == bad_path.stem
        assert effects == ['steer'] and owners == [scope.resolved_owner]
        assert bad_path.read_bytes() == before
        assert read_json_file(ops.gateway_control_operation_path(paths, receipt.operation_id))['state'] == 'completed'
    finally:
        try:
            lane.close()
        finally:
            pump.close()
