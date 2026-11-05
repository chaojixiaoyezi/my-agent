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


# LLM: 等待真实文件/线程事实，有界轮询不替换被测锁或认领。
# 函数用途: 连续交互组件测试的收敛等待。
def _mp_r3_wait(predicate, *, pump=None):
    import threading

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if pump is not None:
            pump._reap()
        result = predicate()
        if result:
            return result
        threading.Event().wait(0.01)
    raise AssertionError('MP-R3 interaction did not converge before deadline')


# LLM: 只隔离provider输出与birth观察；真实SimpleAgent/ConversationStore/claim/终态/control wire及同一轮询线程不替换。
# 函数用途: 流式生成跨不可读暂停恢复后插话、停止、新会话与下一轮，防止悬空执行或重放。
@pytest.mark.parametrize('next_result', ['done', 'failed'])
def test_stream_steer_stop_new_next_conversation_survives_readiness_pause(tmp_path, monkeypatch, next_result):
    import threading
    from dataclasses import asdict

    from agent_py_agent.agent.agent_core import _tool_loop_service as tool_loop
    from agent_py_agent.agent.backends import ModelResponse, get_backend
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts import multiprocess_worker as worker
    from agent_py_agent.agent.gateway_parts.io import (
        GATEWAY_REQUEST_FINGERPRINT_SCHEMA,
        gateway_request_fingerprint,
        read_json_file,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_control_poller import ControlPollingLane
    from agent_py_agent.agent.gateway_parts.multiprocess_route import (
        RouteStamp,
        freeze_request_route,
    )
    from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
    from agent_py_agent.agent.gateway_parts.request_owner_binding import REQUEST_OWNER_KEY
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.tests.test_gateway_multiprocess_worker_dispatch import enqueue
    from agent_py_agent.tests.test_mp03_worker_survival import fd_count, readiness_case

    # 后台终态索引同样解析环境根；必须与临时owner配置一致，不触及测试进程外的真实Home。
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))
    case = readiness_case(tmp_path, monkeypatch)
    # 注册夹具的占位控制不是本交互输入，仅删除临时目录中的夹具原件。
    ops.gateway_control_operation_path(case.paths, case.receipt.operation_id).unlink()
    owner = case.scope.resolved_owner
    # LLM: 仅构造阶段隔离worker shared-home/native启动；业务阶段恢复真实冻结分区及全部执行权检查。
    with monkeypatch.context() as construction:
        construction.setattr(mode, '_WORKER_PARTITION', None)
        agent = SimpleAgent(AgentConfig(
            model_backend='echo', prompt_files=[], my_agent_home=str(case.frozen.launch.owner_root),
            my_agent_owner_provider=owner.provider, my_agent_owner_kind=owner.owner_kind,
            my_agent_owner_id=owner.owner_id, gateway_per_user_owner_scoping=False,
        ), tmp_path / 'workspace')
    # 显式注入离线模型，不依赖owner模型目录或默选模型。
    agent.backend = get_backend('echo', agent.config)
    pool = SimpleNamespace(get=lambda selected: agent if selected == owner else None,
                           pin=lambda _: nullcontext())
    ensure_gateway_folders(case.paths)
    pump = WorkerRequestPump(case.frozen, pool, 1)
    stop = threading.Event()
    lane = ControlPollingLane(pump.controls, stop)
    first_stream, release_first = threading.Event(), threading.Event()
    steered_stream, release_steered = threading.Event(), threading.Event()
    steer_acknowledged = threading.Event()
    control_committed, release_control = threading.Event(), threading.Event()
    hold_lane, lane_held, release_lane = threading.Event(), threading.Event(), threading.Event()
    paused_poll, healthy_published = threading.Event(), threading.Event()
    calls, notices = [], []
    acknowledged = []
    active, high_water = [0], [0]
    counter_lock = threading.Lock()
    original_poll = pump.controls.poll

    # LLM: 透传原poll，仅记录原线程实际经过暂停门后的事实。
    # 函数用途: 等待独立控制线程在不健康阶段完成一次轮询。
    def observed_poll():
        if threading.current_thread() is lane._thread and hold_lane.is_set():
            lane_held.set()
            assert release_lane.wait(10)
        result = original_poll()
        if threading.current_thread() is lane._thread and not healthy_published.is_set():
            paused_poll.set()
        return result

    monkeypatch.setattr(pump.controls, 'poll', observed_poll)

    original_execute = pump.controls._execute

    # LLM: 原执行及回执写入全部完成后才设屏障；不改结果，保留同一尚未完成的Future等待暂停。
    # 函数用途: 确保暂停时原steer执行Future仍在责任表，而不是让空集合通过核收断言。
    def execute_then_wait(receipt):
        result = original_execute(receipt)
        if receipt.command_kind == 'steer':
            control_committed.set()
            assert release_control.wait(10)
        return result

    monkeypatch.setattr(pump.controls, '_execute', execute_then_wait)

    # LLM: provider是唯一模型替身；原agent.run仍负责guidance确认/取消与RuntimeDB终态。
    # 函数用途: 用Events让主线程在真实模型流中插入控制与readiness故障。
    def generate(prompt, on_chunk=None, **_kwargs):
        with counter_lock:
            active[0] += 1
            high_water[0] = max(high_water[0], active[0])
            ordinal = len(calls)
            calls.append(prompt)
        try:
            if on_chunk is not None:
                on_chunk(f'offline-stream-{ordinal}\n')
            if ordinal == 0:
                first_stream.set()
                assert release_first.wait(10)
            elif ordinal == 1:
                assert 'MP-R3 precise steer' in prompt + str(_kwargs.get('messages', []))
                steered_stream.set()
            elif ordinal == 2 and next_result == 'failed':
                raise RuntimeError('MP-R3 offline model failed')
            return ModelResponse(text=f'offline-final-{ordinal}', backend='echo')
        finally:
            with counter_lock:
                active[0] -= 1

    monkeypatch.setattr(agent.backend, 'generate', generate)

    original_acknowledge = tool_loop.acknowledge_injected_turn_input

    # LLM: 真实确认消费后才观察并阻塞原future；不伪造ack，不改写guidance/终态或拦掉原收口重排。
    # 函数用途: 选择插话已消费但原流式回合尚未结束的stop窗口，避免把合法备用下一轮当作悬空。
    def acknowledge_then_wait(*args, **kwargs):
        count = original_acknowledge(*args, **kwargs)
        if count:
            acknowledged.append(count)
            steer_acknowledged.set()
            assert release_steered.wait(10)
        return count

    monkeypatch.setattr(tool_loop, 'acknowledge_injected_turn_input', acknowledge_then_wait)

    # LLM: 保留结构化owner、指纹和原route codec，业务执行仍由真实WorkerRequestPump承担。
    # 函数用途: 给连续交互排入同一冻结归属的合成请求。
    def submit(request_id):
        request = {'id': request_id, 'kind': 'ask', 'goal': 'offline continuous interaction',
                   'status': 'pending', 'user_id': 'alice', 'stream': True,
                   'metadata': {'channel': 'feishu', 'user_id': 'alice', 'chat_id': 'room',
                                'gateway_admin_binding_version': 'binding-v1'},
                   'conversation': {'channel': 'feishu', 'canonical_user_id': 'alice',
                                    'channel_user_id': 'alice', 'channel_conversation_id': 'room'},
                   REQUEST_OWNER_KEY: asdict(owner)}
        request['request_fingerprint'] = gateway_request_fingerprint(request, request_id)
        request['request_fingerprint_schema'] = GATEWAY_REQUEST_FINGERPRINT_SCHEMA
        request = freeze_request_route(request, owner, RouteStamp(
            2, case.frozen.launch.partition.c_run_id, time.time(), 'binding-v1'))
        return enqueue(case, request)

    # LLM: 同一消息号走原C控制生产入口，不手写回执或执行结果。
    # 函数用途: 提交插话、停止和新会话控制并保留去重身份。
    def control(text, message_id, *, expected_turn=''):
        scope = GatewayControlScope('alice', 'feishu', 'room',
            metadata={'message_id': message_id, **({'expected_turn_id': expected_turn} if expected_turn else {})},
            resolved_owner=owner)
        with coordinator_guard(case.paths.root, 2):
            bind_coordinator_generation(case.paths.root, 2, case.frozen.launch.partition.c_run_id)
            return ops.execute_gateway_control_operation(agent, case.paths,
                parse_conversation_control(text), scope, command_text=text)

    # LLM: 只读原codec中的完成状态，不以Future存在或调用成功推断完成。
    # 函数用途: 为有界等待返回实际完成回执。
    def completed(receipt):
        actual = ops.read_gateway_control_operation(case.paths, receipt.operation_id)
        return actual if actual.state == 'completed' else None

    # LLM: 调用真实readiness、登记检查和心跳writer，保持原二元返回。
    # 函数用途: 在连续交互中强制进行一次新鲜心跳刷新。
    def refresh():
        return worker._refresh_due_heartbeat(case.frozen.launch, 0, notices.append, pump=pump)

    original_thread = None
    before = fd_count()
    try:
        first = submit('mp-r3-stream')
        assert pump.poll() == 1
        assert first_stream.wait(10), read_json_file(case.paths.processing / first.name)
        first_payload = read_json_file(case.paths.processing / first.name)
        runtime = first_payload['conversation_claim']
        first_thread = runtime['thread_id']
        assert agent.conversation_store.claims.load(first_thread)['status'] == 'running'
        original_claims = tuple(pump._active.items())
        assert len(original_claims) == 1

        case.paths.state.write_text('{', encoding='utf-8')
        assert refresh()[1] is False
        steer = control('/btw MP-R3 precise steer', 'mp-r3-steer', expected_turn=first.stem)
        assert steer.state == 'prepared'
        lane.start()
        original_thread = lane._thread
        assert paused_poll.wait(5)
        assert not stop.is_set() and original_thread.is_alive()
        assert ops.read_gateway_control_operation(case.paths, steer.operation_id).state == 'prepared'
        assert tuple(pump._active.items()) == original_claims
        assert pump.controls.poll() == 0

        write_json_file_atomic(case.paths.state, case.coordinator)
        assert refresh()[1] is True
        healthy_published.set()
        steered = _mp_r3_wait(lambda: completed(steer))
        assert steered.result['ok'] is True
        assert control_committed.wait(5)

        # 已完成但尚未消费的真实插话仍需只读对账；暂停同时挡住扫描、缓存分支及直接观察入口。
        case.paths.state.write_text('{', encoding='utf-8')
        assert refresh()[1] is False
        hold_lane.set()
        assert lane_held.wait(5)
        with pump.controls._lock:
            retained = tuple(pump.controls._active.items())
        assert len(retained) == 1
        control_future, control_claim = retained[0]
        assert control_claim.operation_id == steer.operation_id and control_claim.state == 'claimed'
        assert not control_future.done()
        release_control.set()
        control_future.result(timeout=5)
        assert control_future.done() and pump.controls._active[control_future] is control_claim
        assert pump.controls.poll() == 0
        assert control_future not in pump.controls._active
        assert not pump.controls._active and pump.controls._reconcilable(steered)
        control_path = ops.gateway_control_operation_path(case.paths, steer.operation_id)
        assert pump.controls._cached_receipt(control_path) == steered
        original_bytes = control_path.read_bytes()
        directory_stamp = control_path.parent.stat().st_mtime_ns
        reconciled = threading.Event()
        original_reconcile = pump.controls._reconcile_delivery

        # LLM: 透传真实消费者及异常，只在真实对账完成后记录观察；不代写完成或消费结果。
        # 函数用途: 证明恢复后缓存分支也重新进入原身份校验和实际对账。
        def observe_reconciliation(receipt):
            original_reconcile(receipt)
            reconciled.set()

        monkeypatch.setattr(pump.controls, '_reconcile_delivery', observe_reconciliation)
        hold_lane.clear()
        release_lane.set()
        with monkeypatch.context() as cached_directory:
            # 只选择“不重扫目录”的观察结果；回执来自上方真实codec，缓存分支及消费者不替换。
            cached_directory.setattr(pump.controls._scan_gate, 'should_scan', lambda _directory: False)
            assert pump.controls.poll() == 0
            pump.controls._offer_reconciliation(steered)
            assert not pump.controls._active and not reconciled.is_set()
            assert control_path.read_bytes() == original_bytes
            assert control_path.parent.stat().st_mtime_ns == directory_stamp
            write_json_file_atomic(case.paths.state, case.coordinator)
            assert refresh()[1] is True
            assert reconciled.wait(5)

        assert control('/btw MP-R3 precise steer', 'mp-r3-steer', expected_turn=first.stem).operation_id == steer.operation_id
        release_first.set()
        assert steered_stream.wait(10)
        assert steer_acknowledged.wait(10)
        assert acknowledged == [1]
        assert tuple(pump._active.items()) == original_claims
        stopping = control('/stop', 'mp-r3-stop', expected_turn=first.stem)
        stopped = _mp_r3_wait(lambda: completed(stopping))
        assert stopped.result['ok'] is True
        assert control('/stop', 'mp-r3-stop', expected_turn=first.stem).operation_id == stopping.operation_id
        release_steered.set()
        _mp_r3_wait(lambda: (case.paths.terminal / first.name).exists(), pump=pump)
        first_terminal = read_json_file(case.paths.terminal / first.name)
        assert first_terminal['status'] in {'interrupted', 'cancelled'}
        assert agent.conversation_store.claims.load(first_thread)['status'] == 'cancelled'

        new = control('/new', 'mp-r3-new')
        new_result = _mp_r3_wait(lambda: completed(new))
        assert new_result.result['ok'] is True
        assert control('/new', 'mp-r3-new').operation_id == new.operation_id
        next_thread = agent.conversation_store.threads.resolve(
            channel='feishu', channel_conversation_id='room', channel_user_id='alice')
        assert next_thread.thread_id != first_thread
        next_request = submit('mp-r3-next')
        assert pump.poll() == 1
        _mp_r3_wait(lambda: (case.paths.terminal / next_request.name).exists(), pump=pump)
        next_terminal = read_json_file(case.paths.terminal / next_request.name)
        assert next_terminal['status'] == next_result
        assert next_terminal['conversation_claim']['thread_id'] == next_thread.thread_id
        assert high_water[0] == 1 and len(calls) == 3
        for _ in range(3):
            assert pump.poll() == 0
        assert read_json_file(case.paths.terminal / first.name) == first_terminal
        assert read_json_file(case.paths.terminal / next_request.name) == next_terminal
        _mp_r3_wait(lambda: pump.inflight == 0, pump=pump)
        assert not list(case.paths.processing.glob('*.json'))
        assert not list(case.paths.inbox.glob('*.json'))
        assert agent.conversation_store.claims.load(next_thread.thread_id)['status'] == (
            'finished' if next_result == 'done' else 'failed')
        assert not stop.is_set() and lane._thread is original_thread
    finally:
        release_first.set()
        release_steered.set()
        release_control.set()
        release_lane.set()
        try:
            lane.close()
        finally:
            pump.close()
    assert lane._thread is original_thread and not original_thread.is_alive()
    assert pump.inflight == 0 and fd_count() - before == 0
