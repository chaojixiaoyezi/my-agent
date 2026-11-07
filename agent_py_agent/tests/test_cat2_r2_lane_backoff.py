"""普通失败指数冷却与全冷却 owner 的空闲规划回归，不调用真实模型。"""
import builtins
import io
import os
import sqlite3
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.errors import ModelNotConfiguredError, ProviderConnectionError
from agent_py_agent.agent.runtime_db.operations import RuntimeRecoveryRequiredError
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.settings.model_profiles import ModelProfileError
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaUnavailable
from agent_py_agent.cli import gateway_lane_retry, gateway_loops
from agent_py_agent.tests.test_gateway_owner_idle_backoff import _supervisor


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(gateway_lane_retry.time, 'monotonic', lambda: now[0])
    return now


def test_generic_failures_double_to_cap_and_success_resets(clock):
    retries = gateway_lane_retry.BackgroundLaneRetry()
    gaps = []
    for _ in range(7):
        retries.failed('o', 't', RuntimeError(), delay=30)
        deadline = retries._failures[('o', 't')].retry_at
        gaps.append(deadline - clock[0])
        clock[0] = deadline
    assert gaps == [30, 60, 120, 240, 480, 900, 900]
    retries.succeeded('o', 't')
    retries.failed('o', 't', RuntimeError(), delay=30)
    assert retries._failures[('o', 't')].retry_at - clock[0] == 30


@pytest.mark.parametrize('condition', [ProviderConnectionError(), ModelNotConfiguredError(),
                                      OwnerQuotaUnavailable(), RuntimeRecoveryRequiredError('needs input')])
def test_condition_to_generic_restarts_cooldown(clock, condition):
    retries = gateway_lane_retry.BackgroundLaneRetry()
    retries.failed('o', 't', condition, delay=30)
    gaps = []
    for _ in range(2):
        retries.failed('o', 't', RuntimeError(), delay=30)
        deadline = retries._failures[('o', 't')].retry_at
        gaps.append(deadline - clock[0])
        clock[0] = deadline
    assert gaps == [30, 60]


def test_generic_to_missing_model_waits_for_configuration(clock):
    retries = gateway_lane_retry.BackgroundLaneRetry()
    retries.failed('o', 't', RuntimeError(), delay=30)
    retries.failed('o', 't', ModelNotConfiguredError(), delay=30)
    assert retries._failures[('o', 't')].waiting_for_model
    clock[0] += 3600
    assert not retries.ready('o', 't', model_ready=lambda: False)
    assert retries.ready('o', 't', model_ready=lambda: True)


def test_cooling_snapshot_excludes_due_conditions_and_other_owner_without_io(clock, monkeypatch):
    retries = gateway_lane_retry.BackgroundLaneRetry()
    for owner, thread, error, delay in (
            ('o', 'cooling', RuntimeError(), 30), ('o', 'due', RuntimeError(), 0),
            ('other', 'other-owner', RuntimeError(), 30),
            ('o', 'model', ModelNotConfiguredError(), 30),
            ('o', 'environment', ProviderConnectionError(), 30),
            ('o', 'storage', OwnerQuotaUnavailable(), 30),
            ('o', 'input', RuntimeRecoveryRequiredError('needs input'), 30)):
        retries.failed(owner, thread, error, delay=delay)

    def unexpected_io(*args, **kwargs):
        pytest.fail('cooling() 只能读取有界内存记录和单调时钟')

    with monkeypatch.context() as guard:
        for module, name in ((builtins, 'open'), (io, 'open'), (os, 'open'),
                             (os, 'stat'), (os, 'scandir'), (sqlite3, 'connect')):
            guard.setattr(module, name, unexpected_io)
        snapshot = retries.cooling('o')
    assert snapshot == frozenset({'cooling'})
    assert retries.cooling('other') == frozenset({'other-owner'})
    assert retries.cooling('missing') == frozenset()
    clock[0] += 30
    assert retries.cooling('o') == frozenset()
    assert snapshot == frozenset({'cooling'})


def test_all_ready_lanes_cooling_back_off_planner_until_exact_deadline(tmp_path, monkeypatch):
    supervisor, scheduler, now, calls, submitted = _supervisor(tmp_path, monkeypatch)
    scheduler.ready = ('t1',)
    start = now[0]
    supervisor._lane_retry.failed('base', 't1', RuntimeError('poison'), delay=30)
    for offset in range(30):
        now[0] = start + offset
        supervisor._submit_ready_thread_ticks()
    assert 1 < len(calls) <= 8
    assert submitted == []
    planned = len(calls)
    now[0] = start + 30
    supervisor._submit_ready_thread_ticks()
    assert len(calls) == planned + 1
    assert [row[2] for row in submitted] == [['t1']]


def test_missing_model_lane_still_plans_every_tick(tmp_path, monkeypatch):
    supervisor, scheduler, now, calls, submitted = _supervisor(tmp_path, monkeypatch)
    scheduler.ready = ('t1',)
    supervisor._lane_retry.failed('base', 't1', ModelNotConfiguredError(), delay=30)
    monkeypatch.setattr(gateway_loops, 'thread_model_is_configured', lambda *args: False)
    start = now[0]
    for offset in range(10):
        now[0] = start + offset
        supervisor._submit_ready_thread_ticks()
    assert len(calls) == 10
    assert submitted == []


def test_healthy_ready_lane_is_not_delayed_by_cooling_peer(tmp_path, monkeypatch):
    supervisor, scheduler, now, calls, submitted = _supervisor(tmp_path, monkeypatch)
    scheduler.ready = ('cooling', 'healthy')
    supervisor._lane_retry.failed('base', 'cooling', RuntimeError(), delay=30)
    supervisor._submit_ready_thread_ticks()
    assert calls == [now[0]]
    assert [row[2] for row in submitted] == [['healthy']]


@pytest.mark.parametrize('owner', ['base', 'owner-a'])
def test_invalid_model_reference_uses_cooldown_in_every_lane(clock, owner):
    retries = gateway_lane_retry.BackgroundLaneRetry()
    retries.failed(owner, 'broken', ModelProfileError('引用已删除', reason='profile_not_found'), delay=30)
    assert retries.cooling(owner) == frozenset({'broken'})
    assert not retries.ready(owner, 'broken', model_ready=lambda: True)
    assert retries.ready(owner, 'healthy', model_ready=lambda: False)
    clock[0] += 30
    assert retries.ready(owner, 'broken', model_ready=lambda: False)


@pytest.mark.parametrize('error_type', [ModelProfileError, DataCorruptionError, RuntimeError])
@pytest.mark.parametrize('source', ['wake', 'policy'])
def test_base_lane_failure_backs_off_planning_with_pending_sources(tmp_path, monkeypatch, error_type, source):
    supervisor, scheduler, now, calls, submitted = _supervisor(tmp_path, monkeypatch)
    scheduler.ready = ('broken',)
    if source == 'wake':
        scheduler.store.wakes = SimpleNamespace(pending=lambda **kwargs: [SimpleNamespace(thread_id='broken')])
    else:
        scheduler.store.progress = SimpleNamespace(list_report=lambda **kwargs: (
            [SimpleNamespace(thread_id='broken', next_due_at=0.0)], []))
    monkeypatch.setattr(gateway_loops, 'thread_model_is_configured', lambda *args: False)

    def fail(thread_id):
        raise error_type('原线程无法执行')

    scheduler.tick_thread = fail
    assert supervisor._safe_thread_tick(scheduler, 'base', 'broken') == []
    start = now[0]
    for offset in range(30):
        now[0] = start + offset
        supervisor._submit_ready_thread_ticks()
    assert 1 < len(calls) <= 8
    assert submitted == []
    before = len(calls)
    now[0] = start + 30
    supervisor._submit_ready_thread_ticks()
    assert len(calls) == before + 1
    assert [row[2] for row in submitted] == [['broken']]


def test_owner_backoff_never_outlasts_lane_cooldown(tmp_path, monkeypatch):
    supervisor, scheduler, now, calls, submitted = _supervisor(tmp_path, monkeypatch, cap=100.0)
    scheduler.ready = ('broken',)
    scheduler.store.wakes = SimpleNamespace(pending=lambda **kwargs: [SimpleNamespace(thread_id='broken')])
    start = now[0]
    supervisor._lane_retry.failed('base', 'broken', RuntimeError(), delay=3.25)
    for offset in (0.0, 1.0, 3.0):
        now[0] = start + offset
        supervisor._submit_ready_thread_ticks()
    before = len(calls)
    assert submitted == []
    now[0] = start + 3.25
    supervisor._submit_ready_thread_ticks()
    assert len(calls) == before + 1
    assert [row[2] for row in submitted] == [['broken']]


def test_corrupt_inbound_marker_still_reaches_guarded_thread_consumer(tmp_path, monkeypatch):
    supervisor, scheduler, _now, _calls, _submitted = _supervisor(tmp_path, monkeypatch)
    consumed = []

    def corrupt(*args):
        raise DataCorruptionError('线程记录损坏')

    def consume(thread_id):
        consumed.append(thread_id)
        raise DataCorruptionError('线程记录损坏')

    scheduler.store.threads = SimpleNamespace(require=corrupt)
    scheduler.tick_thread = consume
    assert supervisor._safe_thread_tick(scheduler, 'base', 'broken') == []
    assert consumed == ['broken']
    assert supervisor._lane_retry.cooling('base') == frozenset({'broken'})


def test_unexpected_inbound_marker_error_records_base_lane_failure(tmp_path, monkeypatch):
    supervisor, scheduler, _now, _calls, _submitted = _supervisor(tmp_path, monkeypatch)

    def broken(*args):
        raise RuntimeError('读入站标记失败')

    monkeypatch.setattr(gateway_loops, '_thread_inbound_marker', broken)
    assert supervisor._safe_thread_tick(scheduler, 'base', 'broken') == []
    assert supervisor._lane_retry.cooling('base') == frozenset({'broken'})


def test_new_healthy_wake_is_immediate_while_broken_thread_cools(tmp_path, monkeypatch):
    supervisor, scheduler, now, calls, submitted = _supervisor(tmp_path, monkeypatch)
    scheduler.ready = ('broken',)
    scheduler.store.wakes = SimpleNamespace(pending=lambda **kwargs: [SimpleNamespace(thread_id='broken')])
    supervisor._lane_retry.failed('base', 'broken', RuntimeError(), delay=30)
    supervisor._submit_ready_thread_ticks()
    now[0] += 0.1
    scheduler.ready = ('broken', 'healthy')
    (scheduler.store.wake_queue_dir / 'healthy.json').write_text('{}')
    supervisor._submit_ready_thread_ticks()
    assert len(calls) == 2
    assert [row[2] for row in submitted] == [['healthy']]
