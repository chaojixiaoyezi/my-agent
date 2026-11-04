"""真实审批队列的确定性交错；不以模拟屏幕替代真实终端验收。"""
import ast
import inspect
import os
import textwrap
import threading

import pytest

from agent_py_agent.cli.chat_parts.tui_permission_queue import TuiPermissionCoordinator
from agent_py_agent.cli.chat_parts.tui_runtime import (
    TuiRuntime,
    TuiTurnEventAdapter,
    _TuiPermissionController,
)
from agent_py_agent.tests.test_tui_runtime import _approval_request_for


def fd_count():
    return len(os.listdir('/proc/self/fd' if os.path.isdir('/proc/self/fd') else '/dev/fd'))


def turn(runtime, identifier):
    runtime.enqueue_prompt(identifier, '审批锁序回归', queued=False)
    return runtime.begin_turn(identifier)


@pytest.mark.parametrize('kind', ['permission_resolved', 'turn_resumed', 'sync_external'])
def test_permission_handoff_releases_runtime_before_peer_adapter_activation(monkeypatch, kind):
    before = fd_count()
    runtime = TuiRuntime('r4-permission-interleaving')
    request = _approval_request_for('t1', 'call-t1', 'run-t1')
    sink_calls = []
    if kind == 'sync_external':
        runtime.sync_agent_permission_requests(
            [{'run_id': 'run-t1', 'request': request.to_dict()}],
            decision_writer=lambda *args: sink_calls.append(args) or {'ok': True})
        first = None
    else:
        first = turn(runtime, 't1')
        first.configure_gateway_permission_sink(lambda *args: sink_calls.append(args))
        assert first.on_gateway_event({'kind': 'permission_requested', 'permission': request.to_dict()})
        if kind == 'permission_resolved':
            runtime.publish_conversation_reset('next-thread')
            assert runtime._retired_turns['t1'] is first
    second = turn(runtime, 't2')
    next_request = _approval_request_for('t2', 'call-t2', 'run-t2')
    assert second.on_gateway_event({'kind': 'permission_requested', 'permission': next_request.to_dict()})
    assert runtime.store.snapshot().permission.permission_id == request.permission_id
    activating, peer_locked = threading.Event(), threading.Event()
    original_activate = TuiPermissionCoordinator._activate
    original_pending = _TuiPermissionController.activate_pending
    activations, ownership, errors = [], [], []

    def activate(coordinator, controller):
        if controller is second._permissions:
            activating.set()
            assert peer_locked.wait(5)
        return original_activate(coordinator, controller)

    def pending(controller):
        ownership.append(runtime._lock._is_owned())
        result = original_pending(controller)
        if controller is second._permissions and result:
            activations.append(controller)
        return result

    monkeypatch.setattr(TuiPermissionCoordinator, '_activate', activate)
    monkeypatch.setattr(_TuiPermissionController, 'activate_pending', pending)

    def finish():
        try:
            if kind == 'sync_external':
                assert runtime.sync_agent_permission_requests(
                    [], decision_writer=lambda *args: sink_calls.append(args) or {'ok': True})
            else:
                event = ({'kind': 'permission_resolved', 'permission_id': request.permission_id,
                          'decision': 'approved'} if kind == 'permission_resolved'
                         else {'kind': 'turn_resumed', 'cause': 'worker_restarted'})
                assert first.on_gateway_event(event)
        except BaseException as error:
            errors.append(error)

    def stream():
        try:
            assert activating.wait(5)
            with second._lock:
                peer_locked.set()
                for _ in range(3):
                    assert second.on_gateway_event({'kind': 'model_delta', 'text': '继续'})
        except BaseException as error:
            errors.append(error)

    # 旧实现会真实形成T1(runtime→peer) / T2(peer→runtime)等待；daemon仅确保红测进程可正常退出。
    one = threading.Thread(target=finish, daemon=True)
    two = threading.Thread(target=stream, daemon=True)
    one.start()
    two.start()
    one.join(5)
    two.join(5)
    assert not one.is_alive() and not two.is_alive(), 'permission handoff deadlocked'
    assert not errors, errors
    assert activations == [second._permissions]
    assert ownership and not any(ownership)
    assert runtime.store.snapshot().permission.permission_id == next_request.permission_id
    assert runtime._permission_coordinator.active_controller() is second._permissions
    assert list(runtime._permission_coordinator._queued) == []
    assert sink_calls == []
    assert fd_count() - before == 0


def test_initial_gateway_permission_activation_also_occurs_outside_runtime_lock(monkeypatch):
    runtime = TuiRuntime('r4-first-permission')
    adapter = turn(runtime, 'first')
    request = _approval_request_for('first', 'first-call', 'first-run')
    original = _TuiPermissionController.activate_pending
    owned = []

    def activate(controller):
        owned.append(runtime._lock._is_owned())
        return original(controller)

    before = fd_count()
    monkeypatch.setattr(_TuiPermissionController, 'activate_pending', activate)
    assert adapter.on_gateway_event({'kind': 'permission_requested', 'permission': request.to_dict()})
    assert owned == [False]
    assert runtime.store.snapshot().permission.permission_id == request.permission_id
    assert fd_count() - before == 0


def test_old_ui_release_preserves_next_approval_on_same_controller(monkeypatch):
    runtime = TuiRuntime('r4-next-approval')
    adapter = turn(runtime, 'same-turn')
    old = _approval_request_for('same-turn', 'old-call', 'same-run')
    new = _approval_request_for('same-turn', 'new-call', 'same-run')
    writes = []
    adapter.configure_gateway_permission_sink(lambda request, decision: writes.append(decision.permission_id))
    assert adapter.on_gateway_event({'kind': 'permission_requested', 'permission': old.to_dict()})
    entered, proceed = threading.Event(), threading.Event()
    original = _TuiPermissionController._release
    result, errors = [], []

    def delayed(controller):
        if threading.current_thread().name == 'r4-local-decision':
            entered.set()
            assert proceed.wait(5)
        return original(controller)

    def resolve():
        try:
            result.append(adapter._permissions.resolve(old.permission_id, 'approved'))
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(_TuiPermissionController, '_release', delayed)
    before = fd_count()
    ui = threading.Thread(target=resolve, name='r4-local-decision', daemon=True)
    ui.start()
    try:
        assert entered.wait(5)
        assert adapter.on_gateway_event({'kind': 'permission_resolved', 'permission_id': old.permission_id,
                                         'decision': 'approved'})
        assert adapter.on_gateway_event({'kind': 'permission_requested', 'permission': new.to_dict()})
    finally:
        proceed.set()
        ui.join(5)
    assert not ui.is_alive() and not errors and result == [True]
    assert writes == [old.permission_id]
    assert adapter._permissions._pending.request.permission_id == new.permission_id
    assert runtime._permission_coordinator.active_controller() is adapter._permissions
    assert runtime.store.snapshot().permission.permission_id == new.permission_id
    assert list(runtime._permission_coordinator._queued) == []
    assert fd_count() - before == 0


def test_nested_deferred_release_is_thread_local_and_exception_safe():
    runtime = TuiRuntime('r4-nested-release')
    first, second = turn(runtime, 'nested-first'), turn(runtime, 'nested-second')
    for adapter in (first, second):
        request = _approval_request_for(adapter.request_id, adapter.request_id + '-call', 'nested-run')
        assert adapter.on_gateway_event({'kind': 'permission_requested', 'permission': request.to_dict()})
    controller = first._permissions
    results = []
    before = fd_count()
    with pytest.raises(RuntimeError, match='leave-scope'):
        with controller.deferred_release():
            with controller.deferred_release():
                first.on_gateway_event({'kind': 'turn_resumed', 'cause': 'worker_restarted'})
            assert runtime._permission_coordinator.active_controller() is controller
            peer = threading.Thread(target=lambda: (controller._release(), results.append(True)), daemon=True)
            peer.start()
            peer.join(5)
            assert not peer.is_alive() and results == [True]
            assert runtime._permission_coordinator.active_controller() is second._permissions
            raise RuntimeError('leave-scope')
    assert controller._release_defer.depth == 0 and controller._release_defer.due is False
    assert runtime._permission_coordinator.active_controller() is second._permissions
    assert fd_count() - before == 0


def test_gateway_permission_deferral_static_guard_rejects_locked_release_fixture():
    def guarded(source):
        body = ast.parse(textwrap.dedent(source)).body[0].body
        return (isinstance(body[0], ast.With)
                and isinstance(body[0].items[0].context_expr, ast.Call)
                and getattr(body[0].items[0].context_expr.func, 'attr', '') == 'deferred_release'
                and isinstance(body[0].body[0], ast.With)
                and len(body[0].body[0].items) == 2)

    assert guarded(inspect.getsource(TuiTurnEventAdapter.on_gateway_event))
    assert not guarded('def event(self):\n with self.runtime._lock:\n  self._permissions._release()\n')
    for method in ('resolve', 'accept_gateway_resolution', 'cancel_pending'):
        tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(_TuiPermissionController, method))))
        assert any(isinstance(n, ast.Call) and getattr(n.func, 'attr', '') == '_release' for n in ast.walk(tree))
        assert not any(isinstance(n, ast.Call) and getattr(n.func, 'attr', '') == 'release' for n in ast.walk(tree))
