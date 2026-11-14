"""同输入坏候选有界，真实配置/检查点/观察变化自动唤醒；临时熔断不被永久隔离。"""
from dataclasses import replace
from types import SimpleNamespace

from agent_py_agent.agent.conversation.background_execution import (
    BackgroundCompactPreflightDeferred,
)
from agent_py_agent.cli.gateway_compact_retry import compact_retry_fingerprint
from agent_py_agent.cli.gateway_lane_retry import BackgroundLaneRetry
from agent_py_agent.tests.test_background_compact_recovery import _background


def test_same_capacity_failure_stops_work_but_automatically_wakes_on_change():
    lane = BackgroundLaneRetry()
    error = BackgroundCompactPreflightDeferred('synthetic candidate', code='COMPACT_CANDIDATE_TOO_LARGE')
    for _ in range(3):
        lane.failed('owner', 'thread', error, delay=0, headless=True, compact_fingerprint='same-input')
    for _ in range(1000):
        assert not lane.ready('owner', 'thread', model_ready=lambda: True,
                              compact_fingerprint=lambda: 'same-input')
    assert lane.ready('owner', 'thread', model_ready=lambda: True,
                      compact_fingerprint=lambda: 'new-model-or-checkpoint')
    assert lane.count('owner') == 0


def test_circuit_cooldown_remains_independent_from_fixed_input_isolation():
    lane = BackgroundLaneRetry()
    error = BackgroundCompactPreflightDeferred('original circuit', code='COMPACT_CIRCUIT_OPEN')
    for _ in range(32):
        lane.failed('owner', 'thread', error, delay=0, headless=True, compact_fingerprint='same')
        assert lane.ready('owner', 'thread', model_ready=lambda: True, compact_fingerprint=lambda: 'same')
    # 健康工作片成功也清原计数，不以总工作片数量卡长任务。
    for _ in range(16):
        lane.succeeded('owner', 'thread')
        assert lane.ready('owner', 'thread', model_ready=lambda: True)


def test_real_sources_ignore_failure_clock_and_wake_on_policy_checkpoint_and_observation(tmp_path):
    agent, store, thread, *_ = _background(tmp_path, backend='openai_compatible', detached=False)
    scheduler = SimpleNamespace(runtime=SimpleNamespace(agent=agent), store=store)
    original = compact_retry_fingerprint(scheduler, thread.thread_id)
    store.threads.update_atomic(thread.thread_id, lambda current: replace(current,
        compact_failure_updated_at=current.compact_failure_updated_at + 60,
        compact_consecutive_failures=3, revision=current.revision + 1, updated_at=current.updated_at + 60))
    assert compact_retry_fingerprint(scheduler, thread.thread_id) == original
    agent.config.memory_compact_auto_trigger_max_tokens = 100_000
    policy = compact_retry_fingerprint(scheduler, thread.thread_id)
    assert policy != original
    store.threads.update_atomic(thread.thread_id, lambda current: replace(current,
        compact_generation=current.compact_generation + 1, compact_checkpoint_id='synthetic-new-checkpoint'))
    checkpoint = compact_retry_fingerprint(scheduler, thread.thread_id)
    assert checkpoint != policy
    store.observations.append({'thread_id': thread.thread_id, 'event_type': 'synthetic-input',
        'summary': 'new automatic observation', 'requires_main_agent': True})
    assert compact_retry_fingerprint(scheduler, thread.thread_id) != checkpoint


def test_original_circuit_deadline_wakes_without_any_input_change(monkeypatch):
    from agent_py_agent.cli import gateway_lane_retry

    clock = [100.0]
    monkeypatch.setattr(gateway_lane_retry.time, 'monotonic', lambda: clock[0])
    lane = BackgroundLaneRetry()
    error = BackgroundCompactPreflightDeferred('original circuit', code='COMPACT_CIRCUIT_OPEN', retry_after_seconds=7)
    lane.failed('owner', 'thread', error, delay=900, headless=True, compact_fingerprint='same')
    clock[0] = 106.9
    assert not lane.ready('owner', 'thread', model_ready=lambda: True, compact_fingerprint=lambda: 'same')
    clock[0] = 107
    assert lane.ready('owner', 'thread', model_ready=lambda: True, compact_fingerprint=lambda: 'same')


def test_original_supervisor_stops_same_preflight_and_wakes_on_real_checkpoint(tmp_path):
    from agent_py_agent.cli.gateway_loops import _BackgroundMainSupervisor

    agent, store, thread, *_ = _background(tmp_path, backend='openai_compatible', detached=False)
    calls = []

    def tick(thread_id):
        calls.append(thread_id)
        raise BackgroundCompactPreflightDeferred('original first preflight', code='COMPACT_CANDIDATE_TOO_LARGE')

    scheduler = SimpleNamespace(runtime=SimpleNamespace(agent=agent), store=store, tick_thread=tick)
    supervisor = object.__new__(_BackgroundMainSupervisor)
    supervisor._lane_retry = BackgroundLaneRetry()
    for _ in range(3):
        supervisor._safe_thread_tick(scheduler, 'owner', thread.thread_id)
    for _ in range(20):
        assert not supervisor._thread_lane_can_retry(scheduler, 'owner', thread.thread_id)
    assert len(calls) == 3
    store.threads.update_atomic(thread.thread_id, lambda current: replace(current,
        compact_generation=current.compact_generation + 1, compact_checkpoint_id='original-auto-commit'))
    assert supervisor._thread_lane_can_retry(scheduler, 'owner', thread.thread_id)
