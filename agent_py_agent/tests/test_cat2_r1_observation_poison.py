"""观察车道复用唤醒毒丸判定，确定性失败有界结案，等待与瞬时失败保持待处理。"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.errors import ProviderConnectionError
from agent_py_agent.agent.conversation import runtime as rt
from agent_py_agent.agent.conversation.host_actor import HostActorError
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.settings.model_profiles import ModelProfileError


@pytest.fixture
def observation_lane(monkeypatch):
    observation = SimpleNamespace(
        observation_id="obs-1", thread_id="thread-x", task_id="task-1",
    )
    pending = [observation]
    handled = []
    notices = []

    def unhandled_requiring_main(*, limit=0):
        assert limit == 0
        return list(pending)

    def mark_handled(ids, *, now=None):
        handled.append((list(ids), now))
        pending[:] = [item for item in pending if item.observation_id not in ids]

    def record_notice(store, thread_id, notice, *, replace_same_code=False):
        notices.append((store, thread_id, notice, replace_same_code))
        return True

    store = SimpleNamespace(observations=SimpleNamespace(
        unhandled_requiring_main=unhandled_requiring_main,
        mark_handled=mark_handled,
    ))
    scheduler = SimpleNamespace(
        store=store,
        _supply_backoff=None,
        _recovery_guard=SimpleNamespace(block_for_task=lambda task_id: None),
    )
    monkeypatch.setattr(
        rt, "observations_by_thread_and_task",
        lambda items: {("thread-x", "task-1"): list(items)} if items else {},
    )
    monkeypatch.setattr(rt, "_observation_waits_for_linked_wake", lambda *args: False)
    monkeypatch.setattr(rt, "consume_with_supply_guard", lambda backoff, thread_id, now, run: run())
    # 基线也可运行同一探针；缺少新增导入时仍要在第五次失败处证伪。
    monkeypatch.setattr(rt, "queue_host_notice", record_notice, raising=False)
    return SimpleNamespace(
        scheduler=scheduler, observation=observation, pending=pending,
        handled=handled, notices=notices,
    )


def _consume(lane, attempt):
    reports, reported = [], set()
    rt._consume_observation_batches(lane.scheduler, reports, reported, 1000.0 + attempt)
    return reports, reported


def _raise_batch(monkeypatch, error):
    def run(*args, **kwargs):
        raise error

    monkeypatch.setattr(rt, "_run_observation_batch", run)


def test_host_actor_failure_quarantines_on_fifth_attempt(observation_lane, monkeypatch, caplog):
    lane = observation_lane
    error = HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID")
    _raise_batch(monkeypatch, error)
    for attempt in range(1, 5):
        with pytest.raises(HostActorError) as raised:
            _consume(lane, attempt)
        assert raised.value is error
        assert lane.handled == []
        assert lane.notices == []
    with caplog.at_level(logging.WARNING, logger="agent.conversation.background_claim_heartbeat"):
        assert _consume(lane, 5) == ([], set())
    assert lane.handled == [(["obs-1"], 1005.0)]
    assert len(lane.notices) == 1
    store, thread_id, notice, replace_same_code = lane.notices[0]
    assert store is lane.scheduler.store
    assert thread_id == "thread-x"
    assert notice.source == "wake_poison"
    assert notice.code.startswith("error:")
    assert replace_same_code is True
    assert sum("OBSERVATION_BATCH_QUARANTINED" in row.message for row in caplog.records) == 1
    assert lane.scheduler._observation_poison == {}
    # 再次调度不重试已确认的观察，也不重复提示。
    assert _consume(lane, 6) == ([], set())
    assert len(lane.handled) == len(lane.notices) == 1


def test_missing_thread_quarantines_with_programmer_bug_code(observation_lane, monkeypatch):
    lane = observation_lane
    _raise_batch(monkeypatch, KeyError("unknown conversation thread: thread-x"))
    for attempt in range(1, 5):
        with pytest.raises(KeyError):
            _consume(lane, attempt)
    assert _consume(lane, 5) == ([], set())
    assert lane.handled == [(["obs-1"], 1005.0)]
    assert lane.notices[0][2].code == "error:programmer_bug:KeyError"
    assert lane.scheduler._observation_poison == {}


def test_connection_errors_never_count_or_handle_observations(observation_lane, monkeypatch):
    lane = observation_lane
    error = ProviderConnectionError("down")
    _raise_batch(monkeypatch, error)
    for attempt in range(1, 21):
        with pytest.raises(ProviderConnectionError) as raised:
            _consume(lane, attempt)
        assert raised.value is error
    assert lane.pending == [lane.observation]
    assert lane.handled == lane.notices == []
    assert getattr(lane.scheduler, "_observation_poison", {}) == {}


def test_success_report_restarts_failure_count(observation_lane, monkeypatch):
    lane = observation_lane
    error = HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID")
    _raise_batch(monkeypatch, error)
    for attempt in range(1, 4):
        with pytest.raises(HostActorError):
            _consume(lane, attempt)
    report = SimpleNamespace(thread_id="thread-x")
    monkeypatch.setattr(rt, "_run_observation_batch", lambda *args, **kwargs: report)
    assert _consume(lane, 4) == ([report], {"thread-x"})
    assert lane.scheduler._observation_poison == {}
    _raise_batch(monkeypatch, error)
    for attempt in range(5, 9):
        with pytest.raises(HostActorError):
            _consume(lane, attempt)
        assert lane.handled == []
    assert _consume(lane, 9) == ([], set())
    assert lane.handled == [(["obs-1"], 1009.0)]


def test_mark_handled_failure_retains_state_and_original_error(observation_lane, monkeypatch, caplog):
    lane = observation_lane
    error = HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID")
    _raise_batch(monkeypatch, error)
    original_mark = lane.scheduler.store.observations.mark_handled
    calls = []

    def fail_once(ids, *, now=None):
        calls.append((list(ids), now))
        if len(calls) == 1:
            raise OSError("simulated observation bookkeeping error")
        original_mark(ids, now=now)

    monkeypatch.setattr(lane.scheduler.store.observations, "mark_handled", fail_once)
    for attempt in range(1, 5):
        with pytest.raises(HostActorError):
            _consume(lane, attempt)
    with caplog.at_level(logging.WARNING, logger="agent.conversation.background_claim_heartbeat"):
        with pytest.raises(HostActorError) as raised:
            _consume(lane, 5)
    assert raised.value is error
    assert "OBSERVATION_QUARANTINE_FAILED" in caplog.text
    assert lane.scheduler._observation_poison[("thread-x", "task-1")].same_cause_count == 5
    assert lane.pending == [lane.observation]
    assert lane.handled == lane.notices == []
    assert _consume(lane, 6) == ([], set())
    assert calls == [(["obs-1"], 1005.0), (["obs-1"], 1006.0)]
    assert lane.handled == [(["obs-1"], 1006.0)]
    assert len(lane.notices) == 1
    assert lane.scheduler._observation_poison == {}


def test_none_report_keeps_existing_failure_count(observation_lane, monkeypatch):
    lane = observation_lane
    error = HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID")
    _raise_batch(monkeypatch, error)
    for attempt in range(1, 4):
        with pytest.raises(HostActorError):
            _consume(lane, attempt)
    before = dict(lane.scheduler._observation_poison)
    monkeypatch.setattr(rt, "_run_observation_batch", lambda *args, **kwargs: None)
    for attempt in range(4, 24):
        assert _consume(lane, attempt) == ([], set())
        assert lane.scheduler._observation_poison == before
        assert lane.handled == lane.notices == []
    _raise_batch(monkeypatch, error)
    with pytest.raises(HostActorError):
        _consume(lane, 24)
    assert _consume(lane, 25) == ([], set())
    assert lane.handled == [(["obs-1"], 1025.0)]


def test_neutral_error_does_not_change_previous_failure_state(observation_lane, monkeypatch):
    lane = observation_lane
    _raise_batch(monkeypatch, HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID"))
    with pytest.raises(HostActorError):
        _consume(lane, 1)
    before = dict(lane.scheduler._observation_poison)
    _raise_batch(monkeypatch, ProviderConnectionError("down"))
    for attempt in range(2, 22):
        with pytest.raises(ProviderConnectionError):
            _consume(lane, attempt)
    assert lane.scheduler._observation_poison == before
    assert lane.pending == [lane.observation]
    assert lane.handled == lane.notices == []


def test_alternating_causes_quarantine_at_shared_total_limit(observation_lane, monkeypatch):
    lane = observation_lane
    for attempt in range(1, 13):
        error = (HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID") if attempt % 2
                 else KeyError("unknown conversation thread: thread-x"))
        _raise_batch(monkeypatch, error)
        if attempt < 12:
            with pytest.raises(type(error)):
                _consume(lane, attempt)
            assert lane.handled == []
        else:
            assert _consume(lane, attempt) == ([], set())
    assert lane.handled == [(["obs-1"], 1012.0)]
    assert len(lane.notices) == 1
    assert lane.scheduler._observation_poison == {}


def test_notice_failure_does_not_undo_quarantine(observation_lane, monkeypatch, caplog):
    lane = observation_lane
    _raise_batch(monkeypatch, HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID"))

    def fail_notice(*args, **kwargs):
        raise OSError("simulated notice bookkeeping error")

    monkeypatch.setattr(rt, "queue_host_notice", fail_notice)
    for attempt in range(1, 5):
        with pytest.raises(HostActorError):
            _consume(lane, attempt)
    with caplog.at_level(logging.WARNING, logger="agent.conversation.background_claim_heartbeat"):
        assert _consume(lane, 5) == ([], set())
    assert "OBSERVATION_QUARANTINE_NOTICE_FAILED" in caplog.text
    assert lane.handled == [(["obs-1"], 1005.0)]
    assert lane.scheduler._observation_poison == {}
    assert _consume(lane, 6) == ([], set())


@pytest.mark.parametrize("error", [
    ModelProfileError("任务原模型已删除", reason="profile_not_found"),
    ModelProfileError("模型目录损坏"),
    DataCorruptionError("线程记录损坏"),
], ids=["missing-profile", "corrupt-profile", "corrupt-thread"])
def test_deterministic_model_and_thread_errors_quarantine(observation_lane, monkeypatch, error):
    lane = observation_lane
    _raise_batch(monkeypatch, error)
    for attempt in range(1, 5):
        with pytest.raises(type(error)):
            _consume(lane, attempt)
        assert lane.handled == lane.notices == []
    assert _consume(lane, 5) == ([], set())
    assert lane.handled == [(["obs-1"], 1005.0)]
    assert lane.notices[0][2].code.endswith(":" + type(error).__name__)
    assert lane.scheduler._observation_poison == {}


@pytest.mark.parametrize("lane", ["observation", "wake", "continuation"])
@pytest.mark.parametrize("error_type", [ModelProfileError, DataCorruptionError])
def test_real_pending_sources_leave_ready_set_after_quarantine(tmp_path, monkeypatch, lane, error_type):
    from agent_py_agent.agent.conversation import ConversationStore
    from agent_py_agent.agent.settings import AgentConfig

    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"channel": "internal", "channel_conversation_id": "poison"})
    agent = SimpleNamespace(config=AgentConfig(model_backend="echo"), conversation_store=store)
    runtime = rt.BackgroundMainAgentRuntime(agent=agent, store=store)
    scheduler = rt.BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    request = {"thread_id": thread.thread_id, "event_type": "test", "requires_main_agent": True}
    if lane == "observation":
        observation = store.observations.append(request)
        wake = None
    else:
        observation, wake = store.wakes.append_observation(request, {
            "thread_id": thread.thread_id,
            "reason": "thread_goal_continue" if lane == "continuation" else "test",
        })
    calls = []

    def broken(*args, **kwargs):
        calls.append(kwargs["now"])
        raise error_type("原持久引用不可用")

    if lane == "observation":
        monkeypatch.setattr(rt, "_run_observation_batch", broken)
    else:
        monkeypatch.setattr(scheduler, "_run_wake_signal", broken)
    for attempt in range(1, 6):
        now = 1000.0 * attempt
        assert scheduler.ready_thread_ids(now=now) == (thread.thread_id,)
        if lane != "observation" or attempt < 5:
            with pytest.raises(error_type):
                scheduler.tick_thread(thread.thread_id, now=now)
        else:
            assert scheduler.tick_thread(thread.thread_id, now=now) == []
        if attempt < 5:
            assert store.observations.unhandled_requiring_main() == [observation]
            if wake is not None:
                assert store.wakes.pending_one(wake.wake_signal_id) is not None
    assert store.wakes.pending() == []
    assert store.observations.unhandled_requiring_main() == []
    assert scheduler.ready_thread_ids(now=10000.0) == ()
    assert scheduler.tick_thread(thread.thread_id, now=10000.0) == []
    assert len(calls) == 5
    if wake is not None:
        rows, errors = store.wakes.attempts.quarantined()
        assert errors == []
        assert [row["wake_signal_id"] for row in rows] == [wake.wake_signal_id]
        assert rows[0]["reason_code"].endswith(":" + error_type.__name__)


def test_goal_continuation_keeps_original_model_then_quarantines(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import FakeDeliveryService
    from agent_py_agent.agent.conversation.goal_runtime import raise_goal_continuation_wake
    from agent_py_agent.tests.test_conversation_goal_tools import _goal_agent

    agent, thread, goal = _goal_agent(tmp_path)
    store = agent.conversation_store
    runtime = rt.BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = rt.BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    signal = raise_goal_continuation_wake(store, goal, now=1000.0)
    calls = []

    def unavailable(request):
        calls.append(request)
        raise ModelProfileError("任务原模型已删除", reason="profile_not_found")

    monkeypatch.setattr(runtime, "run_once", unavailable)
    for attempt in range(1, 6):
        with pytest.raises(ModelProfileError):
            scheduler.tick_thread(thread.thread_id, now=1000.0 * attempt)
    assert len(calls) == 5
    assert store.wakes.pending() == []
    rows, errors = store.wakes.attempts.quarantined()
    assert errors == [] and [row["wake_signal_id"] for row in rows] == [signal.wake_signal_id]
    # 相同 Goal 的来源再次发布也不能复活已隔离的唤醒。
    raise_goal_continuation_wake(store, store.goals.load(thread.thread_id), now=10000.0)
    assert store.wakes.pending() == []
    assert scheduler.ready_thread_ids(now=10000.0) == ()
