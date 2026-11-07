"""普通失败指数冷却与全冷却 owner 的空闲规划回归，不调用真实模型。"""
import builtins
import io
import os
import sqlite3

import pytest

from agent_py_agent.agent.backends.errors import ModelNotConfiguredError, ProviderConnectionError
from agent_py_agent.agent.runtime_db.operations import RuntimeRecoveryRequiredError
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
