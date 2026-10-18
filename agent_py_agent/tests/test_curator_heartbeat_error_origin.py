"""原心跳停止异常只读出处：真实线程/租约，不补发权限或第二状态表。"""
from __future__ import annotations

import contextvars
import copy
import json
import threading
from contextlib import contextmanager
from types import MappingProxyType

import pytest

from agent_py_agent.agent.memory_store import curator as curator_module
from agent_py_agent.agent.memory_store import curator_lease
from agent_py_agent.tests.test_agents_cleanup_review33 import _setup
from agent_py_agent.tests.test_agents_phase0_guard import _curator


# LLM: 原service/state/Thread实际运行，故障仅阻住renew并中断原join；finally持原句柄真实清理，不伪造生命周期。
# 函数用途: 为来源合同提供同一原异常、活心跳及准确租约，所有文件只在pytest临时目录。
@contextmanager
def heartbeat_unknown(tmp_path, monkeypatch, *, start_error=False, primary=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    env = _setup(tmp_path)
    service = _curator(tmp_path, env.conversations, env.service.candidates)
    constructor, renew, join, start = curator_lease._LeaseHeartbeat, curator_lease.renew_curator_lease, threading.Thread.join, threading.Thread.start
    entered, release = threading.Event(), threading.Event()
    born, joins = [], []
    ack_error, stop_error = RuntimeError('synthetic_start_ack'), RuntimeError('synthetic_join_unknown')

    def capture(*args):
        heartbeat = constructor(*args)
        born.append(heartbeat)
        return heartbeat

    def hold_renew(*args):
        if born and threading.current_thread() is born[0].thread:
            entered.set()
            assert release.wait(5)
        return renew(*args)

    def fail_join(worker, *args, **kwargs):
        if born and worker is born[0].thread:
            joins.append(worker)
            raise stop_error
        return join(worker, *args, **kwargs)

    def fail_ack(worker):
        start(worker)
        assert entered.wait(3)
        raise ack_error

    def execute(context):
        assert entered.wait(3)
        if primary is not None:
            raise primary
        return service._result(status='succeeded', reason=context.reason)

    monkeypatch.setattr(curator_lease, '_LeaseHeartbeat', capture)
    monkeypatch.setattr(curator_lease, 'renew_curator_lease', hold_renew)
    monkeypatch.setattr(curator_module, '_lease_seconds', lambda _config: 0)
    monkeypatch.setattr(threading.Thread, 'join', fail_join)
    monkeypatch.setattr(service, '_execute', execute)
    if start_error:
        monkeypatch.setattr(threading.Thread, 'start', fail_ack)
    original_thread = None
    try:
        with pytest.raises(BaseException) as caught:
            service.run(reason='admin')
        heartbeat = born[0]
        original_thread = heartbeat.thread
        expected = ack_error if start_error else (primary if primary is not None else stop_error)
        assert caught.value is expected and original_thread.is_alive()
        assert joins == [original_thread]
        assert service.state_store.load().active_lease['phase'] == 'executing'
        assert service.run_log.list() == []
        yield caught.value, heartbeat, service, env
    finally:
        release.set()
        if born:
            born[0].stop.set()
            worker = original_thread if original_thread is not None else born[0].thread
            join(worker, 3)
            assert not worker.is_alive()


@pytest.mark.parametrize('start_error', [False, True])
def test_actual_stop_error_returns_original_context_and_constructed_lease(tmp_path, monkeypatch, start_error):
    marker = contextvars.ContextVar('curator-heartbeat-origin')
    original_value = object()
    token = marker.set(original_value)
    try:
        with heartbeat_unknown(tmp_path, monkeypatch, start_error=start_error) as (error, heartbeat, service, _env):
            origin = curator_lease.lease_heartbeat_error_origin(error)
            assert type(origin) is tuple and len(origin) == 2
            context, lease_id = origin
            assert context is heartbeat.context and context.get(marker) is original_value
            assert lease_id == service.state_store.load().active_lease['lease_id'] == heartbeat.lease_id
            before = service.state_store.path.read_bytes()
            marker.set(object())
            assert curator_lease.lease_heartbeat_error_origin(error) == origin
            assert context.get(marker) is original_value and heartbeat.thread.is_alive()
            assert service.state_store.path.read_bytes() == before
        # 出处不等于活许可或退出证明；已清理的原异常仍指同一历史Context，不运行它。
        assert curator_lease.lease_heartbeat_error_origin(error) == origin
    finally:
        marker.reset(token)


@pytest.mark.parametrize('fault', ['error_copy', 'marker_only', 'observer_copy', 'thread', 'context', 'lease_id', 'started', 'missing_thread', 'missing_origin', 'pid'])
def test_forged_copied_or_replaced_origin_never_binds_current_lease(tmp_path, monkeypatch, fault):
    with heartbeat_unknown(tmp_path, monkeypatch) as (error, heartbeat, _service, env):
        tested = error
        metadata = curator_lease.lease_exception_metadata(error)
        if fault == 'error_copy':
            tested = RuntimeError('copied-error')
            curator_lease.lease_exception_metadata(tested).update(metadata)
        elif fault == 'marker_only':
            tested = RuntimeError('only-marker')
            curator_lease.lease_exception_metadata(tested)['lease_heartbeat_stop_error_class'] = 'RuntimeError'
        elif fault == 'observer_copy':
            metadata['_lease_heartbeat_origin'] = copy.copy(metadata['_lease_heartbeat_origin'])
        elif fault == 'thread':
            heartbeat.thread = threading.Thread(target=lambda: None)
        elif fault == 'missing_thread':
            del heartbeat.thread
        elif fault == 'missing_origin':
            del heartbeat._origin
        elif fault == 'context':
            heartbeat.context = heartbeat.context.copy()
        elif fault == 'lease_id':
            heartbeat.lease_id = 'foreign-lease'
        elif fault == 'started':
            started = heartbeat.thread._started
            heartbeat.thread._started = threading.Event()
            try:
                assert curator_lease.lease_heartbeat_error_origin(tested) is None
            finally:
                heartbeat.thread._started = started
            return
        else:
            actual_pid = curator_lease.os.getpid()
            monkeypatch.setattr(curator_lease.os, 'getpid', lambda: actual_pid + 1)
        assert curator_lease.lease_heartbeat_error_origin(tested) is None


def test_original_anchor_cannot_be_rebound_or_serialized(tmp_path, monkeypatch):
    with heartbeat_unknown(tmp_path, monkeypatch) as (error, heartbeat, _service, env):
        observation = curator_lease.lease_exception_metadata(error)['_lease_heartbeat_origin']
        with pytest.raises(AttributeError):
            heartbeat._origin = copy.copy(observation)
        with pytest.raises(AttributeError):
            observation.error = RuntimeError('replacement')
        with pytest.raises(TypeError):
            json.dumps(curator_lease.lease_exception_metadata(error))
        assert curator_lease.lease_heartbeat_error_origin(error) == (heartbeat.context, heartbeat.lease_id)


@pytest.mark.parametrize('shadow', [False, True])
def test_actual_migration_diagnostic_whitelists_scalars_not_native_observer(tmp_path, monkeypatch, shadow):
    from agent_py_agent.agent.memory_store import agents_migration as migration

    error_type = type('ShadowPrimary', (RuntimeError,), {'__dict__': property(lambda _self: MappingProxyType({}))}) if shadow else RuntimeError
    primary = error_type('synthetic-primary')
    with heartbeat_unknown(tmp_path, monkeypatch, primary=primary) as (error, heartbeat, _service, env):
        detail = migration._release_after_primary(object(), {'lease_id': heartbeat.lease_id}, error)
        encoded = json.dumps(detail, sort_keys=True)
        assert json.loads(encoded) == {
            'lease_id': heartbeat.lease_id, 'released': False, 'retired': False,
            'heartbeat_stop_error_class': 'RuntimeError', 'recovery': 'manual_recovery_required',
        }
        assert '_lease_heartbeat_origin' not in encoded and 'Thread' not in encoded
        context = migration._CleanupContext(env.home, env.service.persona, env.service.candidates, env.request, heartbeat.lease_id)
        directory = migration._migration_dir(env.home, env.request.migration_id)
        migration._write_record(directory, {'migration_id': env.request.migration_id, 'status': 'applying'})
        migration._cleanup_failed_migration(context, _service.state_store, {'lease_id': heartbeat.lease_id}, error)
        persisted = migration._read_record(directory)
        assert persisted['lease_cleanup'] == detail and persisted['lease_cleanup_pending'] is False
        assert '_lease_heartbeat_origin' not in json.dumps(persisted)
        assert curator_lease.lease_exception_metadata(error)['__notes__'] == [
            'agents migration lease cleanup: ' + json.dumps(detail, sort_keys=True)]
        assert curator_lease.lease_heartbeat_error_origin(error) == (heartbeat.context, heartbeat.lease_id)


def test_old_original_error_cannot_be_rebound_to_a_second_heartbeat(tmp_path, monkeypatch):
    with monkeypatch.context() as first_patch:
        with heartbeat_unknown(tmp_path / 'first', first_patch) as (error, _heartbeat, _service, _env):
            original = curator_lease.lease_heartbeat_error_origin(error)
    with monkeypatch.context() as second_patch:
        with heartbeat_unknown(tmp_path / 'second', second_patch, primary=error) as (reused, second, _service, _env):
            assert reused is error
            assert curator_lease.lease_heartbeat_error_origin(reused) == original
            assert original[0] is not second.context and original[1] != second.lease_id


def test_unstarted_or_fabricated_error_cannot_claim_original_heartbeat(tmp_path):
    env = _setup(tmp_path)
    heartbeat = curator_lease._LeaseHeartbeat(env.store, 'synthetic-unstarted-lease', 30)
    error = RuntimeError('not-issued-from-cleanup')
    curator_lease.lease_exception_metadata(error).update(
        _lease_heartbeat_origin=heartbeat._origin, lease_heartbeat_stop_error_class='RuntimeError')
    assert curator_lease.lease_heartbeat_error_origin(error) is None
    assert heartbeat.thread.ident is None
