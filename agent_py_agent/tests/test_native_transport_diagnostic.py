# LLM: 仅纯fake transport/Future/claim；不构造实际issuer/Agent/native权限，不读真实home或运行模型，诊断不改变原协议。
# 模块用途: 验各原阶段安全关联、未知错误泄漏边界、原ACK/责任/异常对象及空poll区别。
import hashlib
import json
import threading
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import logging as diag
from agent_py_agent.agent.gateway_parts import multiprocess_native_commit_session as native
from agent_py_agent.agent.gateway_parts import multiprocess_worker_dispatch as worker
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap_resources import (
    WorkerSharedResourceError,
)
from agent_py_agent.agent.gateway_parts.multiprocess_kernel_message import KernelMessageError
from agent_py_agent.agent.gateway_parts.multiprocess_worker_commit import WorkerCommitRejected

SECRET = 'private-secret-not-a-log-value'


def diagnostic_rows(capsys):
    output = capsys.readouterr().err
    assert SECRET not in output
    return [json.loads(line.removeprefix('[gateway-native-diagnostic] ')) for line in output.splitlines()
            if line.startswith('[gateway-native-diagnostic] ')]


@pytest.fixture
def session(monkeypatch):
    value = native.NativeCommitSession.__new__(native.NativeCommitSession)
    value._lock = threading.RLock()
    value._closed, value._last_seq = False, 0
    value._handles, value._operations = {}, {}
    value._require_original = lambda: None
    value._sources = (SimpleNamespace(_require_worker=lambda peer: None), object(), object())
    request = {'op': 'materialize', 'seq': 1, 'request_id': SECRET, 'execution_attempt_id': 'attempt-' + SECRET}
    sends, receives = [], []
    encode = json.dumps
    def receive(receiver, *, timeout):
        receives.append(timeout)
        return encode(request).encode(), 17
    def dispatch(message, peer, seq, op):
        value._handles[seq] = 'original-fake-pin-retained'
        return {'op': 'applied', 'seq': seq, 'handle': seq}
    monkeypatch.setattr(native, 'receive_control_packet', receive)
    monkeypatch.setattr(native.KernelMessageSender, 'send', lambda sender, payload, *, timeout: sends.append((payload, timeout)))
    value._dispatch_request = dispatch
    return SimpleNamespace(value=value, request=request, sends=sends, receives=receives)


@pytest.mark.parametrize('stage', ['original_source', 'receive', 'authenticate', 'decode', 'identity', 'dispatch', 'send'])
def test_stage_diagnostics_preserve_original_exception_and_state(session, monkeypatch, capsys, stage):
    c = session
    error = RuntimeError(SECRET)
    error.__cause__ = ValueError('original-cause')
    def fail(*args, **kwargs):
        if stage == 'dispatch':
            c.value._handles[1] = 'partial-original-pin'
        raise error
    if stage == 'original_source':
        c.value._require_original = fail
    elif stage == 'receive':
        monkeypatch.setattr(native, 'receive_control_packet', fail)
    elif stage == 'authenticate':
        c.value._sources[0]._require_worker = fail
    elif stage == 'decode':
        monkeypatch.setattr(native, '_decode_packet', fail)
    elif stage == 'identity':
        monkeypatch.setattr(native, '_native_request_identity', fail)
    elif stage == 'dispatch':
        c.value._dispatch_request = fail
    else:
        monkeypatch.setattr(native.KernelMessageSender, 'send', fail)
    with pytest.raises(RuntimeError) as caught:
        c.value.serve_once(timeout=7)
    assert caught.value is error and type(error.__cause__) is ValueError
    assert c.sends == []
    assert c.value._last_seq == (1 if stage in {'dispatch', 'send'} else 0)
    if stage == 'dispatch':
        assert c.value._handles == {1: 'partial-original-pin'}
    if stage == 'send':
        assert c.value._handles == {1: 'original-fake-pin-retained'}
    rows = diagnostic_rows(capsys)
    assert len(rows) == 1 and rows[0]['stage'] == stage and rows[0]['error_type'] == 'RuntimeError'
    row = rows[0]
    if stage in {'dispatch', 'send'}:
        assert row['op'] == 'materialize' and row['seq'] == 1
        assert row['request'] == {'sha256': hashlib.sha256(SECRET.encode()).hexdigest(), 'chars': len(SECRET), 'bytes': len(SECRET.encode())}
        assert row['attempt']['sha256'] == hashlib.sha256(('attempt-' + SECRET).encode()).hexdigest()
    else:
        assert all(row[key] is None for key in ['op', 'seq', 'request', 'attempt'])


def test_success_ack_exactly_original_once_and_no_diagnostic(session, capsys):
    c = session
    result = c.value.serve_once(timeout=3)
    assert result == {'op': 'applied', 'seq': 1, 'handle': 1}
    assert c.sends == [(b'{"op":"applied","seq":1,"handle":1}', 3)]
    assert c.receives == [3] and c.value._last_seq == 1
    assert c.value._handles == {1: 'original-fake-pin-retained'}
    assert diagnostic_rows(capsys) == []


@pytest.mark.parametrize('stage', ['receive', 'dispatch', 'send'])
def test_only_empty_receive_timeout_silent(session, monkeypatch, capsys, stage):
    error = KernelMessageError('KERNEL_MESSAGE_TIMEOUT', 'receive', SECRET)
    def fail(*a, **k):
        raise error
    if stage == 'receive':
        monkeypatch.setattr(native, 'receive_control_packet', fail)
    elif stage == 'dispatch':
        session.value._dispatch_request = fail
    else:
        monkeypatch.setattr(native.KernelMessageSender, 'send', fail)
    with pytest.raises(KernelMessageError) as caught:
        session.value.serve_once()
    assert caught.value is error
    rows = diagnostic_rows(capsys)
    assert len(rows) == (0 if stage == 'receive' else 1)
    if rows:
        assert rows[0]['stage'] == stage and rows[0]['error_code'] == 'KERNEL_MESSAGE_TIMEOUT'


@pytest.mark.parametrize('shape', ['bad_json', 'duplicate_seq', 'bad_identity', 'extra_fields'])
def test_pre_identity_failure_never_links_input(session, monkeypatch, capsys, shape):
    c = session
    if shape == 'bad_json':
        monkeypatch.setattr(native, 'receive_control_packet', lambda *a, **k: (SECRET.encode(), 17))
    elif shape == 'duplicate_seq':
        c.value._last_seq = 1
    elif shape == 'bad_identity':
        c.request['seq'] = True
    else:
        c.request['owner'] = SECRET
    with pytest.raises((ValueError, WorkerCommitRejected)):
        c.value.serve_once()
    rows = diagnostic_rows(capsys)
    assert len(rows) == 1 and rows[0]['stage'] == ('decode' if shape == 'bad_json' else 'identity')
    assert all(rows[0][key] is None for key in ['op', 'seq', 'request', 'attempt'])
    assert c.sends == [] and c.value._handles == {}


@pytest.mark.parametrize('kind', ['plain', 'unknown_reason', 'subclass', 'fake_class', 'hook', 'resource', 'resource_secret', 'resource_subclass'])
def test_exact_error_types_only_allow_known_literals(capsys, kind):
    class Subclass(WorkerCommitRejected):
        pass
    class ResourceSubclass(WorkerSharedResourceError):
        pass
    class Unknown(RuntimeError):
        error_code = SECRET
        def __str__(self):
            raise AssertionError('str must not run')
        def __repr__(self):
            raise AssertionError('repr must not run')
        def __getattribute__(self, name):
            if name in {'args', 'error_code', 'reason', 'field', '__dict__'}:
                raise AssertionError('unknown attributes must not run')
            return super().__getattribute__(name)
    error = {'plain': lambda: WorkerCommitRejected('original_native_admission_required'),
             'unknown_reason': lambda: WorkerCommitRejected(SECRET),
             'subclass': lambda: Subclass('original_native_admission_required'),
             'fake_class': lambda: type('WorkerCommitRejected', (RuntimeError,), {})(SECRET),
             'hook': lambda: Unknown(SECRET),
             'resource': lambda: WorkerSharedResourceError('resource_unsafe_path', 'owner_home'),
             'resource_secret': lambda: WorkerSharedResourceError('resource_unsafe_path', SECRET),
             'resource_subclass': lambda: ResourceSubclass('resource_unsafe_path', 'owner_home')}[kind]()
    diag.report_native_transport_diagnostic('dispatch', error)
    row = diagnostic_rows(capsys)[0]
    assert row['reason'] == ('original_native_admission_required' if kind == 'plain' else 'resource_unsafe_path' if kind == 'resource' else None)
    assert row['field'] == ('owner_home' if kind == 'resource' else None)
    if kind in {'subclass', 'fake_class', 'hook', 'resource_subclass'}:
        assert row['error_code'] is None


@pytest.mark.parametrize('stage', ['original_source', 'dispatch', 'send'])
@pytest.mark.parametrize('failure', [RuntimeError, KeyboardInterrupt, SystemExit])
def test_logger_failure_never_replaces_original_even_signals(session, monkeypatch, stage, failure):
    error = failure(SECRET)
    def fail(*a, **k):
        raise error
    def logger(*a, **k):
        raise OSError('diagnostic sink failed')
    monkeypatch.setattr(diag, 'report_native_transport_diagnostic', logger)
    if stage == 'original_source':
        session.value._require_original = fail
    elif stage == 'dispatch':
        session.value._dispatch_request = fail
    else:
        monkeypatch.setattr(native.KernelMessageSender, 'send', fail)
    with pytest.raises(failure) as caught:
        session.value.serve_once()
    assert caught.value is error and session.sends == []


@pytest.mark.parametrize('sink', ['write', 'flush', 'encoding', 'json'])
def test_diagnostic_sink_failures_are_only_omitted(session, monkeypatch, sink):
    error = RuntimeError(SECRET)
    def fail(*a, **k):
        raise error
    session.value._dispatch_request = fail
    class Sink:
        def write(self, text):
            if sink == 'write':
                raise OSError('sink failed')
        def flush(self):
            if sink == 'flush':
                raise OSError('sink failed')
    if sink in {'write', 'flush'}:
        monkeypatch.setattr(diag.sys, 'stderr', Sink())
    elif sink == 'json':
        monkeypatch.setattr(diag.json, 'dumps', fail)
    else:
        def broken_hash(*a, **k):
            raise UnicodeEncodeError('utf-8', '\ud800', 0, 1, 'synthetic')
        monkeypatch.setattr(diag.hashlib, 'sha256', broken_hash)
    with pytest.raises(RuntimeError) as caught:
        session.value.serve_once()
    assert caught.value is error


def test_worker_two_futures_bind_failed_claim_keep_all_responsibility(capsys):
    first, second = Future(), Future()
    first.set_result(True)
    error = RuntimeError(SECRET)
    second.set_exception(error)
    a = SimpleNamespace(request_id='first', execution_attempt_id='a')
    b = SimpleNamespace(request_id=SECRET, execution_attempt_id='failed-attempt')
    pump = worker.WorkerRequestPump.__new__(worker.WorkerRequestPump)
    pump._active = {first: a, second: b}
    with pytest.raises(RuntimeError) as caught:
        pump._reap()
    assert caught.value is error and pump._active == {second: b}
    row = diagnostic_rows(capsys)[0]
    assert row['stage'] == 'worker_future_result' and row['op'] is None and row['seq'] is None
    assert row['request']['sha256'] == hashlib.sha256(SECRET.encode()).hexdigest()
    assert row['attempt']['sha256'] == hashlib.sha256(b.execution_attempt_id.encode()).hexdigest()


def test_worker_not_true_original_error_and_active_preserved(capsys):
    future = Future()
    future.set_result(False)
    pump = worker.WorkerRequestPump.__new__(worker.WorkerRequestPump)
    claim = SimpleNamespace(request_id='not-processed', execution_attempt_id='a')
    pump._active = {future: claim}
    with pytest.raises(RuntimeError, match='worker_request_not_processed'):
        pump._reap()
    assert pump._active == {future: claim}
    assert diagnostic_rows(capsys)[0]['stage'] == 'worker_future_result'


def test_fd_unexpected_original_cleanup_no_ack(session, monkeypatch, capsys):
    from agent_py_agent.agent.gateway_parts import multiprocess_native_control_receive as receive
    closed = []
    monkeypatch.setattr(receive.KernelMessageReceiver, 'receive_fds', lambda *a, **k: (b'{}', 17, (999,)))
    monkeypatch.setattr(receive, 'close_fds', lambda descriptors: closed.extend(descriptors))
    monkeypatch.setattr(native, 'receive_control_packet', receive.receive_control_packet)
    with pytest.raises(WorkerCommitRejected):
        session.value.serve_once()
    assert closed == [999] and session.sends == [] and session.value._last_seq == 0
    row = diagnostic_rows(capsys)[0]
    assert row['stage'] == 'receive' and row['reason'] == 'native_control_fds_unexpected'
    assert row['request'] is None and row['seq'] is None


def test_begin_dispatch_failure_never_logs_owner_or_invent_request(session, capsys):
    session.request.clear()
    session.request.update(op='begin', seq=1, owner={'provider': 'synthetic', 'owner_kind': 'user', 'owner_id': SECRET})
    error = WorkerCommitRejected('original_native_admission_required')
    def fail(*a):
        raise error
    session.value._dispatch_request = fail
    with pytest.raises(WorkerCommitRejected) as caught:
        session.value.serve_once()
    assert caught.value is error
    row = diagnostic_rows(capsys)[0]
    assert row['op'] == 'begin' and row['seq'] == 1
    assert row['request'] is None and row['attempt'] is None


@pytest.mark.parametrize('shape', ['unknown_code', 'subclass', 'fake_type'])
def test_kernel_code_exact_type_only_no_empty_poll_spoof(capsys, shape):
    if shape == 'unknown_code':
        error = KernelMessageError(SECRET, 'receive', SECRET)
    elif shape == 'subclass':
        class Child(KernelMessageError):
            pass
        error = Child('KERNEL_MESSAGE_TIMEOUT', 'receive', SECRET)
    else:
        error = type('KernelMessageError', (RuntimeError,), {'error_code': 'KERNEL_MESSAGE_TIMEOUT'})(SECRET)
    diag.report_native_transport_diagnostic('receive', error)
    row = diagnostic_rows(capsys)[0]
    assert row['error_code'] is None and row['request'] is None


def test_seq_display_bound_does_not_change_original_sequence_admission(session, capsys):
    session.request['seq'] = 2**80
    error = RuntimeError(SECRET)
    def fail(*a):
        raise error
    session.value._dispatch_request = fail
    with pytest.raises(RuntimeError) as caught:
        session.value.serve_once()
    assert caught.value is error and session.value._last_seq == 2**80
    assert diagnostic_rows(capsys)[0]['seq'] is None


@pytest.mark.parametrize('failure', [RuntimeError, KeyboardInterrupt, SystemExit])
def test_worker_logger_failure_and_signal_preserve_original_claim(monkeypatch, failure):
    error = failure(SECRET)
    future = Future()
    future.set_exception(error)
    claim = SimpleNamespace(request_id='worker-failure', execution_attempt_id='worker-attempt')
    pump = worker.WorkerRequestPump.__new__(worker.WorkerRequestPump)
    pump._active = {future: claim}
    def broken(*a, **k):
        raise OSError('diagnostic sink failed')
    monkeypatch.setattr(diag, 'report_native_transport_diagnostic', broken)
    with pytest.raises(failure) as caught:
        pump._reap()
    assert caught.value is error and pump._active == {future: claim}
