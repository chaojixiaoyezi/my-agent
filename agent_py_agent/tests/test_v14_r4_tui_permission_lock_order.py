"""真实审批队列的确定性交错；不以模拟屏幕替代真实终端验收。"""
import os
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.cli.chat_parts.tui_permission_queue import TuiPermissionCoordinator
from agent_py_agent.cli.chat_parts.tui_runtime import (
    TuiRuntime,
    _TuiPermissionController,
)
from agent_py_agent.tests.test_tui_runtime import _approval_request_for


def fd_count():
    return len(os.listdir('/proc/self/fd' if os.path.isdir('/proc/self/fd') else '/dev/fd'))


def turn(runtime, identifier):
    runtime.enqueue_prompt(identifier, '审批锁序回归', queued=False)
    return runtime.begin_turn(identifier)


# LLM: 只替换锁外取数；Gateway/本地均走真实快照、reset围栏和审批协调器，不用直接sync冒充轮询。
# 函数用途: 准备一轮可控的后台审批快照，供交错与旧代次回归复用。
def background_permission_poll(monkeypatch, runtime, kind, rows=(), before_return=None):
    from agent_py_agent.cli.chat_parts import tui_threading

    payload = dict(ok=True, thread_id='poll-thread', execution_target=None, notices=[], cursor=0,
                   transcript_events=[], event_cursor=0, event_stream_id='poll-events',
                   agent_permission_requests=list(rows))
    def fetch(*args, **kwargs):
        if before_return is not None:
            before_return()
        return payload

    agent = SimpleNamespace(request_background_notices=fetch,
                            request_agent_permission=lambda *args, **kwargs: {'ok': True})
    if kind == 'gateway_poll':
        return lambda: tui_threading._consume_gateway_background_snapshot(
            agent, runtime.session_id, runtime, [None], set(), [0])

    from agent_py_agent.agent.conversation import (
        agent_activity,
        agent_tool_approval,
        background_transcript,
        message_stream,
    )

    thread = SimpleNamespace(thread_id='poll-thread', workspace_task_id='poll-task')
    store = SimpleNamespace(storage=SimpleNamespace(root='/unused'),
                            threads=SimpleNamespace(resolve_report=lambda **kwargs: (thread, None)))
    monkeypatch.setattr(agent_activity, 'conversation_agent_activity',
                        lambda *args: SimpleNamespace(to_dict=lambda: {}))
    monkeypatch.setattr(background_transcript, 'read_background_transcript_events',
                        lambda *args, **kwargs: dict(events=[], cursor=0, stream_id='poll-events'))
    def read_page(*args, **kwargs):
        fetch()
        return [], 0, True

    monkeypatch.setattr(message_stream, 'read_background_response_page', read_page)
    monkeypatch.setattr(agent_tool_approval, 'renew_agent_tool_approval_consumer', lambda *args, **kwargs: None)
    monkeypatch.setattr(agent_tool_approval, 'list_pending_agent_tool_approvals', lambda *args, **kwargs: list(rows))
    return lambda: tui_threading._consume_local_background_snapshot(
        agent, store, runtime.session_id, runtime, [None], set(), [0])


@pytest.mark.parametrize('kind', ['permission_resolved', 'turn_resumed', 'sync_external',
                                  'gateway_poll', 'local_poll'])
def test_permission_handoff_releases_runtime_before_peer_adapter_activation(monkeypatch, kind):
    before = fd_count()
    runtime = TuiRuntime('r4-permission-interleaving')
    request = _approval_request_for('t1', 'call-t1', 'run-t1')
    sink_calls = []
    if kind in {'sync_external', 'gateway_poll', 'local_poll'}:
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
    poll = background_permission_poll(monkeypatch, runtime, kind) if kind.endswith('_poll') else None

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
            if poll is not None:
                assert poll()
            elif kind == 'sync_external':
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


# LLM: 探针仍使用真实RLock；只在后台确实遇到锁竞争时放行按键，不以sleep或概率调度制造结果。
# 类用途: 观察后台申请runtime锁的受阻点，兼容修复前的独立owner锁与修复后的共享锁。
class PollObservedRLock:
    def __init__(self, contended):
        self._lock = threading.RLock()
        self.contended = contended

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            if threading.current_thread().name == 'r2-background':
                self.contended.set()
            self._lock.acquire()
        return self

    def __exit__(self, *args):
        self._lock.release()

    def _is_owned(self):
        return self._lock._is_owned()


@pytest.mark.parametrize('kind', ['gateway_poll', 'local_poll'])
def test_background_permission_poll_and_keyboard_selection_do_not_invert_locks(monkeypatch, kind):
    runtime = TuiRuntime('r2-poll-keyboard')
    keyboard_locked, proceed = threading.Event(), threading.Event()
    runtime._lock = PollObservedRLock(proceed)
    request = _approval_request_for('external', 'external-call', 'external-run')
    writes, results, errors = [], [], []
    assert runtime.sync_agent_permission_requests(
        [{'run_id': 'external-run', 'request': request.to_dict()}],
        decision_writer=lambda *args: writes.append(args) or {'ok': True})
    controller = runtime._permission_coordinator.active_controller()
    original_publish = _TuiPermissionController._publish
    original_cancel = _TuiPermissionController.cancel_pending
    poll = background_permission_poll(monkeypatch, runtime, kind)

    def publish(self, *args, **kwargs):
        if self is controller and threading.current_thread().name == 'r2-keyboard':
            keyboard_locked.set()
            assert proceed.wait(5)
        return original_publish(self, *args, **kwargs)

    def cancel(self, **kwargs):
        if self is controller and threading.current_thread().name == 'r2-background':
            proceed.set()
        return original_cancel(self, **kwargs)

    def keyboard():
        try:
            results.append(('keyboard', runtime.move_permission_selection(1)))
        except BaseException as error:
            errors.append(error)

    def background():
        try:
            assert keyboard_locked.wait(5)
            results.append(('poll', poll()))
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(_TuiPermissionController, '_publish', publish)
    monkeypatch.setattr(_TuiPermissionController, 'cancel_pending', cancel)
    before = fd_count()
    ui = threading.Thread(target=keyboard, name='r2-keyboard', daemon=True)
    worker = threading.Thread(target=background, name='r2-background', daemon=True)
    ui.start()
    worker.start()
    ui.join(5)
    worker.join(5)
    assert not ui.is_alive() and not worker.is_alive(), 'background polling/keyboard deadlocked'
    assert not errors, errors
    assert sorted(results) == [('keyboard', True), ('poll', True)]
    assert writes == []
    assert controller._pending is None and runtime.store.snapshot().permission is None
    assert runtime._permission_coordinator.active_controller() is None
    assert not runtime._permission_coordinator._queued
    assert fd_count() == before


@pytest.mark.parametrize('kind', ['gateway_poll', 'local_poll'])
def test_stale_background_permission_snapshot_keeps_current_panel(monkeypatch, kind):
    runtime = TuiRuntime('r2-stale-poll')
    old = _approval_request_for('old', 'old-call', 'old-run')
    new = _approval_request_for('late', 'late-call', 'late-run')
    writes = []
    assert runtime.sync_agent_permission_requests(
        [{'run_id': 'old-run', 'request': old.to_dict()}],
        decision_writer=lambda *args: writes.append(args) or {'ok': True})
    controller = runtime._permission_coordinator.active_controller()

    def reset_during_io():
        assert not runtime._lock._is_owned()
        runtime.publish_conversation_reset('new-thread')

    poll = background_permission_poll(monkeypatch, runtime, kind,
        [{'run_id': 'late-run', 'request': new.to_dict()}], before_return=reset_during_io)
    assert poll()
    assert runtime.conversation_reset_generation == 1
    assert runtime.background_message_thread_id == 'new-thread'
    assert runtime.background_message_cursor == 0 and runtime.background_event_stream_id == ''
    assert runtime._permission_coordinator.active_controller() is controller
    assert controller._pending.request.permission_id == old.permission_id
    assert runtime.store.snapshot().permission.permission_id == old.permission_id
    assert set(runtime._permission_coordinator._external) == {f'old-run:{old.permission_id}'}
    assert not runtime._permission_coordinator._queued and writes == []


@pytest.mark.parametrize('kind', ['gateway_poll', 'local_poll'])
def test_poll_removing_multiple_permissions_does_not_resolve_unopened_panel(monkeypatch, kind):
    runtime = TuiRuntime('r2-many-vanished')
    requests = [_approval_request_for(name, f'{name}-call', f'{name}-run') for name in ('one', 'two')]
    events = []
    original = runtime._publish

    def publish(event_kind, phase, block_id, payload=None, **kwargs):
        if event_kind in {'permission_requested', 'permission_resolved'}:
            events.append((event_kind, payload['permission_id']))
        return original(event_kind, phase, block_id, payload, **kwargs)

    monkeypatch.setattr(runtime, '_publish', publish)
    assert runtime.sync_agent_permission_requests(
        [{'run_id': f'{name}-run', 'request': request.to_dict()}
         for name, request in zip(('one', 'two'), requests)],
        decision_writer=lambda *args: pytest.fail('poll must not write an approval decision'))
    assert background_permission_poll(monkeypatch, runtime, kind)()
    opened = set()
    for event_kind, identifier in events:
        if event_kind == 'permission_requested':
            opened.add(identifier)
        else:
            assert identifier in opened, 'resolved event was published for an unopened panel'
            opened.remove(identifier)
    assert not opened and runtime.store.snapshot().permission is None
    assert runtime._permission_coordinator.active_controller() is None
    assert not runtime._permission_coordinator._queued and not runtime._permission_coordinator._external


@pytest.mark.parametrize('replace_in_peer', [False, True])
def test_snapshot_activation_deferral_is_nested_thread_local_and_exception_safe(replace_in_peer):
    runtime = TuiRuntime('r2-defer-scope')
    coordinator = runtime._permission_coordinator
    activations, errors = [], []

    def activate(label):
        activations.append((label, runtime._lock._is_owned()))
        return True

    first = SimpleNamespace(activate_pending=lambda: activate('first'))
    second = SimpleNamespace(activate_pending=lambda: activate('second'))

    def replace():
        try:
            coordinator.release(first)
            coordinator.enqueue(second)
        except BaseException as error:
            errors.append(error)

    with pytest.raises(RuntimeError, match='snapshot-failed'):
        with coordinator.deferred_activation():
            # 真实快照再嵌套一层作用域；内层退出不能在外层锁仍可能持有时抢先激活。
            runtime.apply_conversation_snapshot(0, lambda: coordinator.enqueue(first))
            assert not activations
            if replace_in_peer:
                peer = threading.Thread(target=replace, daemon=True)
                peer.start()
                peer.join(5)
                assert not peer.is_alive() and not errors
                assert activations == [('second', False)]
            raise RuntimeError('snapshot-failed')
    assert activations == [('second' if replace_in_peer else 'first', False)]
    assert coordinator._activation_defer.pending is None
    assert coordinator.active_controller() is (second if replace_in_peer else first)


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
