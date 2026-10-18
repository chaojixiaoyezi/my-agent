# LLM: 原C guard/control/P4与完整角色reader均实际运行；只替换既有物理端点并用受控线程，不启动服务、模型或native。
# 模块用途: 验当前C完整state发表与角色双读同门，以及取消重启的原锁序、失源拒绝和失败元数据收尾。
import copy
import os
import threading
from contextlib import contextmanager

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_takeover_runtime as takeover
from agent_py_agent.agent.gateway_parts import multiprocess_takeover_sources as sources
from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
from agent_py_agent.agent.gateway_parts.io import read_json_file
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import CoordinatorUnavailable
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.agent.gateway_parts.restart_service import (
    pending_restart_request,
    reset_restart_drain_state,
    submit_restart_request,
)
from agent_py_agent.cli import gateway_coordinated_run as coordinated
from agent_py_agent.cli import gateway_restart_handover as handover
from agent_py_agent.cli.models import GatewayRunContext
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context
from agent_py_agent.tests.test_gateway_role_heartbeat_stability import running as running
from agent_py_agent.tests.test_gateway_worker_runtime import physical as physical


# LLM: 只运输原实际control/paths及原RunContext，不构造业务Agent或从state恢复权限；初始state走同一真实publisher。
# 函数用途: 给完整P4/心跳组件准备一份当前C的合法starting投影。
@pytest.fixture
def current(running):
    control = running.control
    value = GatewayRunContext(None, control.paths, control.home_root / 'config.yaml',
                              coordinator_worker_count=control.worker_count, control_plane=control)
    coordinated.publish_coordinated_state(value, {'status': 'starting', 'pid': os.getpid()})
    return value


# LLM: 线程只运行本测试明确给出的原组件操作；异常原对象收集后由主线程断言，不吞成成功或授终态。
# 函数用途: 保存受控异线程完成和错误观察，所有调用方必须join。
def launch(operation):
    attempted, finished = threading.Event(), threading.Event()
    errors = []

    def work():
        attempted.set()
        try:
            operation()
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=work)
    thread.start()
    assert attempted.wait(2)
    return thread, finished, errors


def test_real_current_c_publisher_waits_for_full_role_double_read(current, monkeypatch):
    controller = current.control_plane.controller()
    original = sources._role_snapshot
    seen, spawned, blocked = [], [], []

    def snapshot(root):
        value = original(root)
        seen.append(value)
        if len(seen) == 1:
            spawned.append(launch(lambda: coordinated.publish_coordinated_state(
                current, {'status': 'running', 'pid': os.getpid()})))
            blocked.append(not spawned[0][1].wait(0.05))
        return value

    monkeypatch.setattr(sources, '_role_snapshot', snapshot)
    try:
        with controller._mutex:
            result = takeover._refresh_targets(controller)
    finally:
        for thread, _done, _errors in spawned:
            thread.join(3)
    assert result is True and controller._all_quarantined is False
    assert blocked == [True]
    assert len(seen) >= 2 and seen[0]['gateway_state.json'] == seen[1]['gateway_state.json']
    assert spawned and all(not thread.is_alive() and errors == [] for thread, _, errors in spawned)
    assert read_json_file(current.paths.state)['status'] == 'running'


def test_full_role_reader_waits_until_original_publisher_finishes(current, monkeypatch):
    controller = current.control_plane.controller()
    write = coordinated.write_json_file_atomic
    snapshot = sources._role_snapshot
    published, release, reader_entered = threading.Event(), threading.Event(), threading.Event()
    results = []

    def writing(path, payload):
        assert controller._mutex._is_owned() and controller._lease._lifecycle_gate._is_owned()
        write(path, payload)
        published.set()
        assert release.wait(3)

    def reading(root):
        reader_entered.set()
        return snapshot(root)

    def read():
        with controller._mutex:
            results.append(takeover._refresh_targets(controller))

    monkeypatch.setattr(coordinated, 'write_json_file_atomic', writing)
    monkeypatch.setattr(sources, '_role_snapshot', reading)
    writer, _done, errors = launch(lambda: coordinated.publish_coordinated_state(
        current, {'status': 'running', 'pid': os.getpid()}))
    reader = None
    try:
        assert published.wait(2)
        reader, _reader_done, reader_errors = launch(read)
        assert not reader_entered.wait(0.05)
    finally:
        release.set()
        writer.join(3)
        if reader is not None:
            reader.join(3)
    assert not writer.is_alive() and not reader.is_alive() and errors == reader_errors == []
    assert results == [True] and not controller._all_quarantined
    assert read_json_file(current.paths.state)['status'] == 'running'


# LLM: 仅原临时重启入口发结构化请求，不作真实restart/stop；本片保持B4 unsupported取消继续服务。
# 函数用途: 保存同一C需要取消的原request，供原固定writer消费。
def restart_request(current):
    reset_restart_drain_state()
    return submit_restart_request(current.paths, target_pid=os.getpid(), requester={'kind': 'cli'},
                                  reason='synthetic component', cooldown_seconds=0)['request']


def test_restart_cancel_waits_controller_before_lease_and_preserves_original_write_order(current, monkeypatch):
    controller = current.control_plane.controller()
    lease = controller._lease
    request = restart_request(current)
    cancel, read, publish = handover.cancel_restart_request, handover.read_json_file, coordinated.publish_coordinated_state
    order = []

    def cancelled(*args):
        assert controller._mutex._is_owned() and lease._lifecycle_gate._is_owned()
        order.append('cancel')
        return cancel(*args)

    def state_read(*args):
        assert controller._mutex._is_owned() and lease._lifecycle_gate._is_owned()
        order.append('state_read')
        return read(*args)

    def state_publish(*args):
        assert controller._mutex._is_owned() and lease._lifecycle_gate._is_owned()
        order.append('publish')
        return publish(*args)

    monkeypatch.setattr(handover, 'cancel_restart_request', cancelled)
    monkeypatch.setattr(handover, 'read_json_file', state_read)
    monkeypatch.setattr(coordinated, 'publish_coordinated_state', state_publish)
    thread = None
    try:
        with controller._mutex:
            thread, finished, errors = launch(lambda: handover._cancel_internal_restart(current, request))
            assert not finished.wait(0.05)
            assert lease._lifecycle_gate.acquire(blocking=False)
            lease._lifecycle_gate.release()
        thread.join(3)
        assert not thread.is_alive() and errors == []
        assert order == ['cancel', 'state_read', 'publish']
        assert pending_restart_request(current.paths, pid=os.getpid()) is None
        state = read_json_file(current.paths.state)
        assert state['restart_drain']['phase'] == 'cancelled'
        assert state['restart_drain']['request_id'] == request['request_id']
        assert state['restart_drain']['error_code'] == 'GATEWAY_MULTIPROCESS_RESTART_UNSUPPORTED'
    finally:
        if thread is not None:
            thread.join(3)
        reset_restart_drain_state()


# LLM: 仅临时对象来源负例，finally恢复原引用后交原guard清理；不改canonical权限文件或制造新lease/controller。
# 函数用途: 模拟等待期间来源被换或撤销，核首写前拒绝而不是跟随新source。
@contextmanager
def changed_source(current, controller, fault):
    lease = controller._lease
    choices = {'controller': (lease, 'controller', copy.copy(controller)),
               'lease': (controller, '_lease', copy.copy(lease)),
               'path': (current.paths, 'state', current.paths.root / 'unexpected.json'),
               'generation': (controller, 'c_run_id', 'other-generation'),
               'count': (controller, 'count', controller.count + 1),
               'control': (current, 'control_plane', copy.copy(current.control_plane)),
               'revoked': (lease, '_closing', True),
               'accessor_controller': (GatewayControlPlane, 'controller', lambda _self: copy.copy(controller))}
    target, key, value = choices[fault]
    original = getattr(target, key)
    assign = setattr if isinstance(target, type) else object.__setattr__
    assign(target, key, value)
    try:
        yield
    finally:
        assign(target, key, original)


@pytest.mark.parametrize('fault', ['controller', 'lease', 'path', 'generation', 'count', 'control', 'revoked'])
def test_late_publisher_rechecks_original_source_after_controller_wait(current, monkeypatch, fault):
    control = current.control_plane
    controller = control.controller()
    original = GatewayControlPlane.controller
    captured = threading.Event()
    before = current.paths.state.read_bytes()

    def capture(self):
        value = original(self)
        if self is control:
            captured.set()
        return value

    monkeypatch.setattr(GatewayControlPlane, 'controller', capture)
    thread = None
    try:
        with controller._mutex:
            thread, finished, errors = launch(lambda: coordinated.publish_coordinated_state(
                current, {'status': 'running', 'pid': os.getpid()}))
            assert captured.wait(2) and not finished.wait(0.05)
            with changed_source(current, controller, fault):
                controller._mutex.release()
                try:
                    thread.join(3)
                finally:
                    controller._mutex.acquire()
        assert not thread.is_alive() and len(errors) == 1 and isinstance(errors[0], CoordinatorUnavailable)
        assert current.paths.state.read_bytes() == before
        assert not (current.paths.root / 'unexpected.json').exists()
    finally:
        if thread is not None:
            thread.join(3)


@pytest.mark.parametrize('fault', ['accessor_controller', 'lease', 'path', 'generation'])
def test_restart_cancel_changed_source_refuses_before_first_write(current, monkeypatch, fault):
    controller = current.control_plane.controller()
    request = restart_request(current)
    before = {p.relative_to(current.paths.root).as_posix(): p.read_bytes()
              for p in current.paths.root.rglob('*') if p.is_file()}
    calls = []
    cancel = handover.cancel_restart_request

    def observe(*args):
        calls.append('cancel')
        return cancel(*args)

    monkeypatch.setattr(handover, 'cancel_restart_request', observe)
    try:
        with changed_source(current, controller, fault), pytest.raises(CoordinatorUnavailable):
            handover._cancel_internal_restart(current, request)
        assert calls == []
        assert {p.relative_to(current.paths.root).as_posix(): p.read_bytes()
                for p in current.paths.root.rglob('*') if p.is_file()} == before
        assert pending_restart_request(current.paths, pid=os.getpid())['request_id'] == request['request_id']
    finally:
        reset_restart_drain_state()


def test_failure_metadata_still_publishes_after_root_closing_quarantine_and_recheck_stop(current):
    controller = current.control_plane.controller()
    runtime = GatewayDrainRuntime(current.control_plane)
    runtime.stop_producers()
    controller._all_quarantined = True
    controller._recheck_stop.set()
    coordinated.publish_coordinated_state(current, {'status': 'failed', 'pid': os.getpid(),
                                                   'drain_complete': False, 'error_code': 'GATEWAY_DRAIN_INCOMPLETE'})
    state = read_json_file(current.paths.state)
    assert state['status'] == 'failed' and state['drain_complete'] is False
    assert state['error_code'] == 'GATEWAY_DRAIN_INCOMPLETE'
    assert state['c_run_id'] == current.control_plane.c_run_id and state['worker_count'] == 2
