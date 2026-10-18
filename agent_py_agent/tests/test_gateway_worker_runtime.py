# LLM: 正例使用原租约/controller/P4/issuer；只替换物理进程与native端点，不把模拟waitid当内核验收。
# 模块用途: 验证worker组合保留真实启动门结果、原对象及关闭责任，不启动Gateway、子进程或模型。
import copy
import json
import os
import subprocess
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_drain_process as exits
from agent_py_agent.agent.gateway_parts import multiprocess_executor_identity as births
from agent_py_agent.agent.gateway_parts import multiprocess_native_runtime as native
from agent_py_agent.agent.gateway_parts import multiprocess_takeover_runtime as takeover
from agent_py_agent.agent.gateway_parts import multiprocess_worker_commit as commits
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import WorkerBootstrapError
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap_publish import read_worker_registry
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import current_coordinator
from agent_py_agent.agent.gateway_parts.multiprocess_drain_sources import durable_drain_snapshot
from agent_py_agent.agent.gateway_parts.multiprocess_records import MultiprocessRecordError
from agent_py_agent.agent.gateway_parts.multiprocess_worker_commit import WorkerCommitIssuer
from agent_py_agent.cli import gateway_worker_runtime as runtime
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context


# LLM: 仅物理边界替身；PID/状态保存在本测试，不签发lease/P4/issuer或进行真实waitid。
# 类用途: 提供Popen原接口形状，记录原命令并让真实退出观察器消费受控waitstatus。
class ControlledPopen:
    next_pid = 400000
    instances = []

    # LLM: 构造不启动进程；真实产品OriginalWorkerExitObservation仍核同一对象、PID、出生和waiter。
    # 函数用途: 保存本次模拟物理出生，供原启动及关闭合同直接观察。
    def __init__(self, args, *, stdin, env):
        self.args, self.stdin, self.env = args, stdin, env
        self.pid = type(self).next_pid
        type(self).next_pid += 1
        type(self).instances.append(self)
        self.returncode = None
        self._waitpid_lock = threading.Lock()
        self.exit_status = None
        self.wait_calls = []

    # LLM: install后仅委托原观察器，不从本替身returncode缓存生成成功。
    # 函数用途: 兼容issuer的真实存活前置检查。
    def poll(self):
        return self._internal_poll()

    # LLM: install前视为仍活；此入口不制造退出事实。
    # 函数用途: 给原观察器安装前的Popen保留未退出状态。
    def _internal_poll(self, _deadstate=None):
        return None

    # LLM: 原关闭等待必须使用被观察器安装的waiter，不绕过确认协议。
    # 函数用途: 记录等候预算，沿真实观察器判断超时或退出。
    def wait(self, timeout):
        self.wait_calls.append(timeout)
        return self._wait(timeout)


# LLM: 仅native endpoint替身；attach依然构造真实WorkerCommitIssuer并核本次P4，不签发业务能力。
# 类用途: 记录门外attach和关闭顺序，避免本组件测试启动Mach/Unix消费者。
class ControlledLane:
    instances = []

    # LLM: 无系统端点；保存不可替换的原launch以供实际drain交叉校验。
    # 函数用途: 记录本次组合根亲自创建的lane。
    def __init__(self, launch):
        self.launch = launch
        self._origin = self
        self._launch_origin = launch
        self.issuer = None
        self.closed = False
        self.attach_in_gate = None
        type(self).instances.append(self)

    # LLM: 该检查仅保守拒绝替身漂移；权限仍由实际lease/P4/issuer提供。
    # 函数用途: 模拟原lane的引用一致性要求，不生成权限。
    def _require_original(self):
        if self._origin is not self or self.launch is not self._launch_origin:
            raise ValueError("test_lane_original_required")

    # LLM: 必须在原短门外发生；用真实issuer核实际control/launch/Popen/P4并绑定原controller。
    # 函数用途: 观察组合次序，避免用空attach伪装启动成功。
    def attach(self, control, process):
        self._require_original()
        self.attach_in_gate = control.controller()._lease._lifecycle_gate._is_owned()
        assert self.attach_in_gate is False
        self.issuer = WorkerCommitIssuer(control, self.launch, process)

    # LLM: 这里只记录native close调用，不能代表真实native整树已退出。
    # 函数用途: 供组件测试核对未知退出前不得关闭端点。
    def close(self):
        self._require_original()
        self.closed = True


@pytest.fixture
def physical(context, monkeypatch):
    ControlledPopen.instances = []
    ControlledLane.instances = []
    real_birth = births.process_birth

    # LLM: 只有本测试创建的fake PID使用合成出生；当前C身份继续读取原内核出生。
    # 函数用途: 在不创建子进程的情况下保留真实C及受控W的身份区别。
    def birth(pid):
        return f"controlled:{pid}" if any(p.pid == pid for p in ControlledPopen.instances) else real_birth(pid)

    # LLM: 模拟原waitid两阶段结果；None表示仍活，cached returncode不是本入口的证据。
    # 函数用途: 让真实OriginalWorkerExitObservation消费一个受控退出状态。
    def waitid(pid, *, nowait):
        process = next(p for p in ControlledPopen.instances if p.pid == pid)
        if process.exit_status is None:
            return None
        return SimpleNamespace(si_pid=pid, si_code=1, si_status=process.exit_status)

    monkeypatch.setattr(subprocess, "Popen", ControlledPopen)
    monkeypatch.setattr(native, "NativeWorkerLane", ControlledLane)
    monkeypatch.setattr(runtime, "process_birth", birth)
    monkeypatch.setattr(commits, "process_birth", birth)
    monkeypatch.setattr(exits, "process_birth", birth)
    monkeypatch.setattr(exits, "_waitstatus_birth", birth)
    monkeypatch.setattr(exits, "_waitstatus_supported", lambda: True)
    monkeypatch.setattr(exits, "_original_waitid", waitid)
    monkeypatch.setattr(exits, "_exited_code", lambda: 1)
    workers = runtime.CoordinatorWorkers(context, context.home_root / "config.yaml", 43123)
    yield workers
    if not any(observation._unknown for _, _, observation in workers._created_origins):
        for process in ControlledPopen.instances:
            process.exit_status = 0
        workers.close()


def test_constructor_and_empty_close_are_memory_only(tmp_path):
    control = SimpleNamespace(worker_count=0)
    workers = runtime.CoordinatorWorkers(control, tmp_path / "unused.yaml", 43123)
    assert workers.control is control and workers.processes == workers.native_lanes == []
    workers.close()
    assert list(tmp_path.iterdir()) == []


def test_process_identity_requires_birth_and_uses_current_host(monkeypatch):
    actual = runtime.worker_process_identity(os.getpid())
    assert actual["pid"] == os.getpid() and actual["start_time"] == births.process_birth(os.getpid())
    assert actual["host_id"]
    monkeypatch.setattr(runtime, "process_birth", lambda _pid: None)
    with pytest.raises(MultiprocessRecordError):
        runtime.worker_process_identity(os.getpid())


def test_real_coordinator_gate_publishes_p4_then_attaches_original_issuers(physical):
    workers = physical
    assert workers.start() is workers
    control = workers.control
    record = read_worker_registry(control.paths.root)
    assert record["worker_count"] == 2 and record["c_run_id"] == control.c_run_id
    assert [r["worker"] for r in record["workers"]] == [0, 1]
    assert workers._generation_source is current_coordinator(control.paths.root, 2)
    original, _sources, _hosts = takeover.original_drain_sources(control.controller())
    assert len(original) == 2
    for index, (lane, process, observation) in enumerate(workers._created_origins):
        assert original[index] == (index, (lane.issuer, lane.launch, process))
        assert type(lane.issuer) is WorkerCommitIssuer and observation.process is process
        assert lane.attach_in_gate is False and observation.confirmed is False
        assert process.args[:3] == [runtime.sys.executable, "-m", "agent_py_agent"]
        assert process.stdin is subprocess.DEVNULL
        assert "stdout" not in vars(process) and "stderr" not in vars(process)
        assert process.env["MY_AGENT_GATEWAY_WORKER"]
        supplied = json.loads(process.env["MY_AGENT_GATEWAY_WORKER"])
        assert supplied["partition"] == {"index": index, "count": 2, "c_run_id": control.c_run_id}
        assert supplied["gateway_root"] == str(control.paths.root)
        assert supplied["owner_root"] == str(control.home_root)
        assert supplied["instance_id"] == lane.launch.instance_id
        for flag, value in {"--config": workers.config_path, "--index": index, "--of": 2,
                            "--run": control.c_run_id, "--gateway-root": control.paths.root,
                            "--owner-root": control.home_root, "--parent-pid": os.getpid(),
                            "--instance": lane.launch.instance_id}.items():
            assert process.args[process.args.index(flag) + 1] == str(value)
    assert all(lane.closed is False for lane in workers.native_lanes)


def test_actual_gate_refusal_is_not_empty_success(physical):
    workers = physical
    controller = workers.control.controller()
    controller._all_quarantined = True
    with pytest.raises(WorkerBootstrapError) as error:
        workers.start()
    assert error.value.decision == {"status": "blocked", "reason": "takeover_scope_unconfirmed",
                                    "error_code": "OWNER_PARTITION_QUARANTINED"}
    assert workers.processes == workers.native_lanes == []
    assert read_worker_registry(workers.control.paths.root) is None


@pytest.mark.parametrize("decision", [None, {}, {"status": "started"},
    {"status": "started", "worker_index": 0, "c_run_id": "fixture-c"}])
def test_no_callback_cannot_become_success_even_with_start_label(physical, monkeypatch, decision):
    captured = []

    def gate(_launch, callback):
        captured.append(callback)
        return decision

    monkeypatch.setattr(runtime, "spawn_coordinator_worker", gate)
    with pytest.raises(WorkerBootstrapError) as error:
        physical.start()
    assert error.value.decision is decision
    assert physical.processes == physical.native_lanes == []
    with pytest.raises(WorkerBootstrapError):
        captured[0]()
    assert physical.processes == physical.native_lanes == []


@pytest.mark.parametrize("field,value", [("status", "blocked"), ("worker_index", 1),
    ("worker_index", True), ("c_run_id", "different-generation")])
def test_actual_created_process_cannot_wash_rejected_gate_result(physical, monkeypatch, field, value):
    actual_gate = runtime.spawn_coordinator_worker
    returned = []

    def gate(launch, callback):
        result = actual_gate(launch, callback)
        assert result["status"] == "started"
        for process in physical.processes:
            process.exit_status = 0
        result[field] = value
        returned.append(result)
        return result

    monkeypatch.setattr(runtime, "spawn_coordinator_worker", gate)
    with pytest.raises(WorkerBootstrapError) as error:
        physical.start()
    assert error.value.decision is returned[0]
    assert len(physical.processes) == len(physical.native_lanes) == 1
    assert physical.native_lanes[0].issuer is None
    assert physical._created_origins[0][2].confirmed is True
    assert len(read_worker_registry(physical.control.paths.root)["workers"]) == 1


def test_duplicate_synchronous_callback_rejected_before_second_birth(physical, monkeypatch):
    actual_gate = runtime.spawn_coordinator_worker

    def gate(launch, callback):
        def twice():
            callback()
            physical.processes[0].exit_status = 0
            callback()
        return actual_gate(launch, twice)

    monkeypatch.setattr(runtime, "spawn_coordinator_worker", gate)
    with pytest.raises(WorkerBootstrapError):
        physical.start()
    assert len(physical.processes) == len(physical.native_lanes) == 1
    assert physical.native_lanes[0].issuer is None
    assert physical._created_origins[0][2].confirmed is True


def test_successful_callback_cannot_be_replayed_later(physical, monkeypatch):
    actual_gate = runtime.spawn_coordinator_worker
    captured = []

    def gate(launch, callback):
        captured.append(callback)
        return actual_gate(launch, callback)

    monkeypatch.setattr(runtime, "spawn_coordinator_worker", gate)
    physical.start()
    with pytest.raises(WorkerBootstrapError):
        captured[0]()
    assert len(physical.processes) == 2


def test_callback_on_another_thread_cannot_spawn(physical, monkeypatch):
    errors = []

    def gate(launch, callback):
        def other_thread():
            try:
                callback()
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        return {"status": "started", "worker_index": launch.partition.index,
                "c_run_id": launch.partition.c_run_id}

    monkeypatch.setattr(runtime, "spawn_coordinator_worker", gate)
    with pytest.raises(WorkerBootstrapError):
        physical.start()
    assert len(errors) == 1 and type(errors[0]) is WorkerBootstrapError
    assert physical.processes == physical.native_lanes == []


def test_previous_slot_callback_cannot_borrow_next_slot_window(physical, monkeypatch):
    actual_gate = runtime.spawn_coordinator_worker
    captured = []

    def gate(launch, callback):
        if captured:
            with pytest.raises(WorkerBootstrapError):
                captured[0]()
            assert len(physical.processes) == 1
        captured.append(callback)
        return actual_gate(launch, callback)

    monkeypatch.setattr(runtime, "spawn_coordinator_worker", gate)
    physical.start()
    assert len(physical.processes) == 2


def test_close_needs_observed_exit_and_preserves_unknown_resources(physical):
    physical.start()
    original = tuple(physical._created_origins)
    for process in physical.processes:
        process.returncode = 0  # cached值不是waitid事实。
    with pytest.raises(RuntimeError, match="worker_exit_unconfirmed"):
        physical.close()
    assert tuple(physical._created_origins) == original
    assert all(not observation.confirmed for _, _, observation in original)
    assert all(not lane.closed for lane in physical.native_lanes)
    # 原观察器一旦发现缓存污染即保持unknown，不能靠清缓存再验当成功。
    for process in physical.processes:
        process.returncode = None
    for _lane, _process, observation in original:
        assert observation._unknown is True
    # fixture不重建或清空这些原引用；模拟进程没有真实OS资源需代为回收。
    assert tuple(physical._created_origins) == original


def test_original_exit_wait_budget_shared_and_native_can_be_deferred(physical):
    physical.start()
    deadlines = []
    for _lane, process, observation in physical._created_origins:
        process.exit_status = 0
        actual = observation.wait_until
        observation.wait_until = lambda deadline, fn=actual: deadlines.append(deadline) or fn(deadline)
    physical.close(defer_native=True)
    assert len(deadlines) == 2 and deadlines[0] == deadlines[1]
    assert all(observation.confirmed for _, _, observation in physical._created_origins)
    assert all(not lane.closed for lane in physical.native_lanes)
    assert physical.drain_assessments(durable_drain_snapshot(physical.control.paths, physical.control.worker_count), {})
    physical.close_native()
    assert all(lane.closed for lane in physical.native_lanes)


def test_missing_exit_observer_is_unknown_and_does_not_close_native(physical):
    physical.start()
    original = physical._created_origins
    for process in physical.processes:
        process.exit_status = 0
    try:
        physical._created_origins = original[:1]
        with pytest.raises(RuntimeError, match="worker_exit_unconfirmed"):
            physical.close()
        assert all(not lane.closed for lane in physical.native_lanes)
        assert len(physical.processes) == 2
    finally:
        physical._created_origins = original


def test_native_close_failure_retains_original_sources(physical, monkeypatch):
    physical.start()
    original = tuple(physical._created_origins)
    for process in physical.processes:
        process.exit_status = 0
    error = RuntimeError("controlled_native_responsibility_pending")

    def refuse():
        raise error

    with monkeypatch.context() as patch:
        patch.setattr(physical.native_lanes[0], "close", refuse)
        with pytest.raises(RuntimeError) as raised:
            physical.close()
        assert raised.value is error
    assert tuple(physical._created_origins) == original
    assert all(not lane.closed for lane in physical.native_lanes)


def test_publication_failure_keeps_original_birth_and_never_attaches(physical, monkeypatch):
    error = OSError("controlled_publication_failed")

    def refuse(*_args, **_kwargs):
        physical.processes[0].exit_status = 0
        raise error

    monkeypatch.setattr(runtime, "publish_worker_registry", refuse)
    with pytest.raises(OSError) as raised:
        physical.start()
    assert raised.value is error
    assert len(physical.processes) == len(physical._created_origins) == 1
    lane, process, observation = physical._created_origins[0]
    assert lane.issuer is None and observation.process is process and observation.confirmed is True
    assert read_worker_registry(physical.control.paths.root) is None


@pytest.mark.parametrize("mutation", ["copy", "control", "missing", "process", "observation"])
def test_drain_rejects_replaced_original_coverage(physical, mutation):
    physical.start()
    target = physical
    if mutation == "copy":
        target = copy.copy(physical)
    elif mutation == "control":
        target = copy.copy(physical)
        target.control = copy.copy(physical.control)
    elif mutation == "missing":
        target = copy.copy(physical)
        target._origin = target
        target._created_origins = physical._created_origins[:-1]
    elif mutation == "process":
        target = copy.copy(physical)
        target._origin = target
        lane, _process, observation = physical._created_origins[0]
        target._created_origins = [(lane, physical.processes[1], observation), physical._created_origins[1]]
    else:
        target = copy.copy(physical)
        target._origin = target
        lane, process, _observation = physical._created_origins[0]
        target._created_origins = [(lane, process, physical._created_origins[1][2]), physical._created_origins[1]]
    with pytest.raises(ValueError, match="drain_original_worker"):
        target.drain_assessments(durable_drain_snapshot(physical.control.paths, physical.control.worker_count), {})


def test_generation_reference_cannot_follow_new_original(physical):
    physical._generation_source = copy.copy(current_coordinator(physical.control.paths.root, 2))
    with pytest.raises(ValueError, match="generation_original_coordinator_required"):
        physical.start()
    assert physical.processes == physical.native_lanes == []
