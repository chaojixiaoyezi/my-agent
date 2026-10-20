# LLM: 仅真实producer/session/client方法加fake OS/endpoint，不创建Agent、服务、模型或业务账；typed负回执不是请求终态。
# 模块用途: 守住同次pre-pin拒绝、严格消息归属、未知保债及原caller仍会传播异常的边界。
import json
import os
import threading
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_native_commit_client as client
from agent_py_agent.agent.gateway_parts import multiprocess_native_commit_session as server
from agent_py_agent.agent.gateway_parts import multiprocess_native_short_operation as short
from agent_py_agent.agent.gateway_parts import multiprocess_worker_commit as producer
from agent_py_agent.agent.gateway_parts import multiprocess_worker_dispatch as dispatch
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

OWNER = OwnerIdentity.provider_user('test', 'one')
NACK = {'op': 'rejected', 'seq': 1, 'error_code': 'WORKER_COMMIT_REJECTED'}


# LLM: auth/OS是假，但begin方法保持原实现；entered与pin/effect记录只来源实际同步callback调用。
# 函数用途: 为纯合同反例提供可控原controller分支，不假称真实C授权。
@pytest.fixture
def issuer(monkeypatch):
    value = producer.WorkerCommitIssuer.__new__(producer.WorkerCommitIssuer)
    value._issuer_origin, value._creator_pid = value, os.getpid()
    value._pins, value._lease = set(), object()
    value.control = SimpleNamespace(c_run_id='generation')
    value.launch = SimpleNamespace(partition=SimpleNamespace(count=2, index=0), instance_id='instance')
    value._require_current = lambda: None
    value._require_worker = lambda peer: None
    value._controller = SimpleNamespace(launch_owner_writer=lambda owner, operation: {'status': 'blocked'})
    monkeypatch.setattr(producer, 'owner_worker', lambda owner, count: 0)
    monkeypatch.setattr(producer, 'pin_coordinator_commit', lambda *args: object())
    return value


# LLM: 不造Kernel端点权威；仅替换原接收/发送叶，session严格解析与实际dispatcher/issuer.begin仍执行。
# 函数用途: 构造一条原begin RPC的可观察单次传输。
@pytest.fixture
def channel(issuer, monkeypatch):
    value = server.NativeCommitSession.__new__(server.NativeCommitSession)
    value._lock, value._closed, value._last_seq = threading.RLock(), False, 0
    value._handles, value._operations = {}, {}
    value._sources = (issuer, object(), object())
    value._require_original = lambda: None
    request = {'op': 'begin', 'seq': 1, 'owner': {'provider': 'test', 'owner_kind': 'user', 'owner_id': 'one'}}
    sent = []
    monkeypatch.setattr(server, 'receive_control_packet', lambda *a, **k: (json.dumps(request).encode(), 17))
    monkeypatch.setattr(server.KernelMessageSender, 'send', lambda sender, payload, **kwargs: sent.append(json.loads(payload)))
    return SimpleNamespace(value=value, issuer=issuer, request=request, sent=sent)


def test_original_begin_blocked_sends_one_same_seq_nack_without_pin(channel):
    with pytest.raises(producer.WorkerCommitRejected) as caught:
        channel.value.serve_once()
    assert channel.sent == [NACK]
    assert type(caught.value) is producer.WorkerCommitNotStarted
    assert channel.value._last_seq == 1 and channel.value._handles == {} and channel.issuer._pins == set()
    with pytest.raises(producer.WorkerCommitRejected):
        channel.value.serve_once()
    assert channel.sent == [NACK]


@pytest.mark.parametrize('status', ['started', 'unknown'])
def test_nonblocked_no_callback_decision_is_not_typed(channel, status):
    channel.issuer._controller.launch_owner_writer = lambda *a: {'status': status}
    with pytest.raises(producer.WorkerCommitRejected) as caught:
        channel.value.serve_once()
    assert type(caught.value) is producer.WorkerCommitRejected
    assert channel.sent == [] and channel.issuer._pins == set()


def test_default_non_native_begin_preserves_original_exception(issuer):
    with pytest.raises(producer.WorkerCommitRejected) as caught:
        issuer.begin(OWNER, peer_pid=17)
    assert type(caught.value) is producer.WorkerCommitRejected
    assert caught.value.args == ('owner_commit_unavailable',) and issuer._pins == set()


@pytest.mark.parametrize('phase', ['commit_entered', 'pin_created', 'ordinary_same_text', 'interrupt', 'exit'])
def test_unknown_entered_or_control_flow_propagates_same_object_without_nack(channel, phase):
    error = {'interrupt': KeyboardInterrupt, 'exit': SystemExit}.get(phase, producer.WorkerCommitRejected)('owner_commit_unavailable')
    def fail(owner, operation):
        if phase == 'commit_entered':
            channel.issuer._require_current = lambda: (_ for _ in ()).throw(error)
            operation()
        elif phase == 'pin_created':
            operation()
        raise error
    channel.issuer._controller.launch_owner_writer = fail
    with pytest.raises(type(error)) as caught:
        channel.value.serve_once()
    assert caught.value is error and channel.sent == []
    assert len(channel.issuer._pins) == (1 if phase == 'pin_created' else 0)


@pytest.mark.parametrize('mismatch', ['token', 'peer', 'issuer', 'copy'])
def test_old_or_foreign_producer_event_cannot_become_current_nack(channel, mismatch):
    token = object()
    try:
        channel.issuer.begin(OWNER, peer_pid=17, rejection_origin=token)
    except producer.WorkerCommitRejected as error:
        event = error
    if mismatch == 'peer':
        event._peer = 999
    elif mismatch == 'issuer':
        event._producer = object()
    elif mismatch == 'copy':
        shallow = type(event).__new__(type(event))
        shallow.__dict__.update(event.__dict__)
        event = shallow
    def fail(*args, **kwargs):
        raise event
    channel.value._dispatch_request = fail
    with pytest.raises(type(event)) as caught:
        channel.value.serve_once()
    assert caught.value is event and channel.sent == [] and channel.value._handles == {}


@pytest.mark.parametrize('rpc', ['apply', 'materialize'])
def test_original_short_operation_pre_pin_rejection_consumes_once_without_effect(channel, monkeypatch, rpc):
    value = short.NativeShortOperation.__new__(short.NativeShortOperation)
    value._creator_pid, value._origin, value._lock, value._used = os.getpid(), value, threading.Lock(), False
    effects = []
    value._check_source = lambda: None
    value._sources = (channel.issuer, OWNER, lambda phase, operation: operation(), 'request_commit', lambda: effects.append(True))
    channel.value._operations['original'] = value
    channel.request.clear()
    channel.request.update({'op': 'apply', 'seq': 1, 'operation': 'original'})
    if rpc == 'materialize':
        from agent_py_agent.agent.gateway_parts import multiprocess_native_materialize
        monkeypatch.setattr(multiprocess_native_materialize, 'register_owner_materialization', lambda *args: 'original')
        channel.request.clear()
        channel.request.update({'op': 'materialize', 'seq': 1, 'request_id': 'request', 'execution_attempt_id': 'attempt'})
    with pytest.raises(producer.WorkerCommitRejected):
        channel.value.serve_once()
    assert channel.sent == [NACK] and value._used is True and value._sources is None
    assert effects == [] and channel.value._handles == {} and channel.issuer._pins == set()
    with pytest.raises(producer.WorkerCommitRejected):
        value.apply(17, lambda pin: None)
    assert effects == []


@pytest.mark.parametrize('failure', ['effect', 'nested_begin'])
def test_short_operation_pin_and_effect_failure_never_nacked(channel, failure):
    value = short.NativeShortOperation.__new__(short.NativeShortOperation)
    value._creator_pid, value._origin, value._lock, value._used = os.getpid(), value, threading.Lock(), False
    value._check_source = lambda: None
    entered = []
    error = producer.WorkerCommitRejected('owner_commit_unavailable')
    def effect():
        entered.append(True)
        if failure == 'nested_begin':
            channel.issuer._controller.launch_owner_writer = lambda *args: {'status': 'blocked'}
            channel.issuer.begin(OWNER, peer_pid=17, rejection_origin=object())
        raise error
    value._sources = (channel.issuer, OWNER, lambda phase, operation: operation(), 'request_commit', effect)
    def launch(owner, operation):
        operation()
        return {'status': 'started'}
    channel.issuer._controller.launch_owner_writer = launch
    channel.value._operations['original'] = value
    channel.request.clear()
    channel.request.update({'op': 'apply', 'seq': 1, 'operation': 'original'})
    with pytest.raises(producer.WorkerCommitRejected) as caught:
        channel.value.serve_once()
    if failure == 'effect':
        assert caught.value is error
    assert entered == [True] and channel.sent == [] and value._used is True
    assert len(channel.issuer._pins) == 1 and 1 in channel.value._handles


@pytest.mark.parametrize('bad', ['auth', 'old_seq', 'bool_seq', 'extra', 'bad_json', 'duplicate', 'fd'])
def test_untrusted_or_invalid_request_sends_no_nack(channel, monkeypatch, bad):
    if bad == 'auth':
        channel.issuer._require_worker = lambda peer: (_ for _ in ()).throw(producer.WorkerCommitRejected('owner_commit_unavailable'))
    elif bad == 'old_seq':
        channel.value._last_seq = 1
    elif bad == 'bool_seq':
        channel.request['seq'] = True
    elif bad == 'extra':
        channel.request['extra'] = True
    else:
        raw = b'{' if bad == 'bad_json' else b'{"op":"begin","seq":1,"seq":2}'
        if bad == 'fd':
            read_fd, write_fd = os.pipe()
            monkeypatch.setattr(server.KernelMessageReceiver, 'receive_fds', lambda *a, **k: (b'{}', 17, (read_fd,)))
            monkeypatch.setattr(server, 'receive_control_packet', __import__('agent_py_agent.agent.gateway_parts.multiprocess_native_control_receive', fromlist=['receive_control_packet']).receive_control_packet)
        else:
            monkeypatch.setattr(server, 'receive_control_packet', lambda *a, **k: (raw, 17))
    try:
        with pytest.raises((ValueError, producer.WorkerCommitRejected)):
            channel.value.serve_once()
        assert channel.sent == [] and channel.value._handles == {} and channel.issuer._pins == set()
        if bad == 'fd':
            with pytest.raises(OSError):
                os.fstat(read_fd)
    finally:
        if bad == 'fd':
            os.close(write_fd)


@pytest.mark.parametrize('mode', ['nack', 'ack'])
def test_send_failure_no_second_reply_and_no_false_success(channel, monkeypatch, mode):
    if mode == 'ack':
        def launch(owner, operation):
            operation()
            return {'status': 'started'}
        channel.issuer._controller.launch_owner_writer = launch
    error, sent = OSError('transport broken'), []
    def fail(sender, payload, **kwargs):
        sent.append(json.loads(payload))
        raise error
    monkeypatch.setattr(server.KernelMessageSender, 'send', fail)
    with pytest.raises(OSError) as caught:
        channel.value.serve_once()
    assert caught.value is error and len(sent) == 1
    assert sent[0]['op'] == ('rejected' if mode == 'nack' else 'begun')
    assert len(channel.issuer._pins) == (0 if mode == 'nack' else 1)
    assert bool(channel.value._handles) is (mode == 'ack')


# LLM: 精确RPC相关状态依原client字段，真实parent核验保留，只有OS出生观察和端点读取为fake。
# 函数用途: 为顺序、乱序、错误parent和NACK形状测试提供无native副作用的客户端。
@pytest.fixture
def endpoint(monkeypatch):
    value = client.NativeCommitClient.__new__(client.NativeCommitClient)
    value._origin, value._creator_pid = value, os.getpid()
    value._parent = (os.getppid(), 'original-birth')
    value._sources = (SimpleNamespace(_check_creator=lambda: None), SimpleNamespace(_check_creator=lambda: None))
    value._lock = threading.RLock()
    value._seq, value._closed = 0, False
    value._pending, value._uncertain, value._inflight = set(), set(), {}
    value._finish_seq, value._completed_finishes, value._rejections = {}, {}, {}
    frames, waits, sent = [], [], []
    monkeypatch.setattr(client, 'process_birth', lambda pid: 'original-birth')
    def receive(receiver, *, timeout):
        waits.append(timeout)
        raw, peer = frames.pop(0)
        return (raw if type(raw) is bytes else json.dumps(raw).encode()), peer
    monkeypatch.setattr(client, 'receive_control_packet', receive)
    monkeypatch.setattr(client.KernelMessageSender, 'send', lambda sender, raw, **kwargs: sent.append(json.loads(raw)))
    return SimpleNamespace(value=value, frames=frames, waits=waits, sent=sent)


def test_same_seq_nack_clears_only_phantom_and_raises_local_typed(endpoint):
    c = endpoint
    c.frames.append((NACK, c.value._parent[0]))
    c.value._pending.add(99)
    c.value._uncertain.add(98)
    with pytest.raises(producer.WorkerCommitRejected) as caught:
        c.value.materialize_owner('request', 'attempt')
    assert type(caught.value) is getattr(client, 'NativeCommitNotStarted', None) and caught.value.seq == 1
    assert caught.value._client is c.value
    assert c.value._pending == {99} and c.value._uncertain == {98} and c.value._inflight == {}
    assert len(c.sent) == 1 and c.sent[0]['op'] == 'materialize'


def test_foreign_nack_belongs_to_original_seq_and_current_wait_uses_original_deadline(endpoint, monkeypatch):
    c = endpoint
    c.value._inflight = {1: ('applied', 1, False), 2: ('applied', 2, False)}
    c.value._uncertain = {1, 2}
    c.frames[:] = [(NACK, c.value._parent[0]), ({'op': 'applied', 'seq': 2, 'handle': 2, 'result': 'original-result'}, c.value._parent[0])]
    times = iter([0, 0, 1, 4])
    monkeypatch.setattr(client.time, 'monotonic', lambda: next(times))
    assert c.value._await_reply(2, 5)['result'] == 'original-result'
    assert c.waits == [5, 4] and c.value._pending == {2} and c.value._uncertain == set()
    with pytest.raises(producer.WorkerCommitRejected) as caught:
        c.value._await_reply(1, 5)
    assert type(caught.value) is getattr(client, 'NativeCommitNotStarted', None) and caught.value.seq == 1
    assert c.sent == []


@pytest.mark.parametrize('bad', ['wrong_peer', 'birth', 'unknown_seq', 'bool_seq', 'extra', 'missing', 'code', 'duplicate', 'finish', 'released', 'pending'])
def test_invalid_or_uncertain_nack_keeps_original_inflight(endpoint, monkeypatch, bad):
    c = endpoint
    c.value._inflight = {1: ('applied', 1, False)}
    c.value._uncertain = {1}
    frame, peer = dict(NACK), c.value._parent[0]
    if bad == 'wrong_peer':
        peer += 1
    elif bad == 'birth':
        monkeypatch.setattr(client, 'process_birth', lambda pid: 'new-birth')
    elif bad == 'unknown_seq':
        frame['seq'] = 2
    elif bad == 'bool_seq':
        frame['seq'] = True
    elif bad == 'extra':
        frame['handle'] = 1
    elif bad == 'missing':
        del frame['error_code']
    elif bad == 'code':
        frame['error_code'] = 'unknown'
    elif bad == 'duplicate':
        frame = b'{"op":"rejected","seq":1,"seq":1,"error_code":"WORKER_COMMIT_REJECTED"}'
    elif bad == 'finish':
        c.value._inflight[1] = ('finished', 1, False)
    elif bad == 'released':
        c.value._inflight[1] = ('applied', 1, True)
    elif bad == 'pending':
        c.value._pending.add(1)
    c.frames.append((frame, peer))
    before = dict(c.value._inflight)
    with pytest.raises(producer.WorkerCommitRejected) as caught:
        c.value._await_reply(1, 5)
    assert type(caught.value) is producer.WorkerCommitRejected
    assert c.value._inflight == before and c.value._uncertain == {1} and c.value._rejections == {}


def test_late_duplicate_nack_never_revives_or_clears_other_seq(endpoint):
    c = endpoint
    c.value._inflight = {1: ('applied', 1, False), 2: ('applied', 2, False)}
    c.value._uncertain = {1, 2}
    c.frames[:] = [(NACK, c.value._parent[0]), (NACK, c.value._parent[0])]
    with pytest.raises(producer.WorkerCommitRejected) as caught:
        c.value._await_reply(2, 5)
    assert type(caught.value) is producer.WorkerCommitRejected
    assert c.value._inflight == {2: ('applied', 2, False)} and c.value._uncertain == {2}
    assert c.value._rejections[1].seq == 1


def test_expired_deadline_does_not_read_or_clear_nack(endpoint, monkeypatch):
    c = endpoint
    c.value._inflight = {1: ('applied', 1, False)}
    c.value._uncertain = {1}
    times = iter([0, 5])
    monkeypatch.setattr(client.time, 'monotonic', lambda: next(times))
    with pytest.raises(TimeoutError):
        c.value._await_reply(1, 5)
    assert c.waits == [] and c.value._inflight == {1: ('applied', 1, False)} and c.value._uncertain == {1}


def test_wire_only_rejection_does_not_silently_reap_or_settle_original_claim(endpoint):
    c = endpoint
    c.frames.append((NACK, c.value._parent[0]))
    try:
        c.value.materialize_owner('request', 'attempt')
    except producer.WorkerCommitRejected as error:
        original = error
    future = Future()
    future.set_exception(original)
    claim = SimpleNamespace(request_id='request', execution_attempt_id='attempt')
    pump = dispatch.WorkerRequestPump.__new__(dispatch.WorkerRequestPump)
    pump._active = {future: claim}
    with pytest.raises(producer.WorkerCommitRejected) as caught:
        pump._reap()
    assert caught.value is original and pump._active == {future: claim}


@pytest.mark.parametrize('kind', ['short', 'bounded', 'heartbeat', 'runner'])
def test_four_original_unknown_families_are_not_converted_or_reconstructed(channel, kind):
    from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
    from agent_py_agent.agent.common import runner_thread_origin
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.agent.memory_store import curator_lease

    if kind == 'short':
        error = RequestExecutionOutcomeUncertainError({'id': 'request', 'execution_attempt_id': 'attempt',
                                                      'lease_epoch': 1, 'lease_owner': 'original'}, 'original-phase')
    elif kind == 'bounded':
        error = BoundedCallExitUnconfirmedError('original-call', 'worker_exit')
    elif kind == 'heartbeat':
        error = RuntimeError('original-heartbeat-stop')
        heartbeat = curator_lease._LeaseHeartbeat(object(), 'original-lease', 30)
        curator_lease._stop_heartbeat(heartbeat, error)
        assert curator_lease.lease_heartbeat_error_origin(error) is not None
    else:
        error = RuntimeError('original-runner-stop')
        thread = threading.Thread(target=lambda: None)
        origin = runner_thread_origin._capture_runner_thread(thread, 'child_runner_worker_stop', 'run', 'attempt')
        with pytest.raises(RuntimeError) as captured:
            runner_thread_origin._finish_original_runner_thread(origin, lambda: (_ for _ in ()).throw(error))
        assert captured.value is error and runner_thread_origin.runner_thread_exit_unconfirmed(error)
    metadata = dict(BaseException.__dict__['__dict__'].__get__(error))
    def fail(*args, **kwargs):
        raise error
    channel.value._dispatch_request = fail
    with pytest.raises(type(error)) as caught:
        channel.value.serve_once()
    assert caught.value is error and channel.sent == []
    assert BaseException.__dict__['__dict__'].__get__(error) == metadata
