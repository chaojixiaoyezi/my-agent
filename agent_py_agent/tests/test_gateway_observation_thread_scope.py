"""Gateway 后台单线程观察消费的线程级下推、有界解码与不完整异常传播回归。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    ConversationStore,
)
from agent_py_agent.agent.conversation import runtime as runtime_module
from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport
from agent_py_agent.agent.conversation.observation_index import (
    ObservationIndexError,
    ObservationIndexIncomplete,
)
from agent_py_agent.agent.settings import AgentConfig


def _warm_observation_index(store: ConversationStore) -> None:
    for _ in range(2000):
        result = store.observations._index.maintenance()
        assert not result["load_errors"], result
        if result["coverage"] == "complete":
            return
    pytest.fail("observation index did not converge")


def _new_scheduler(store: ConversationStore) -> BackgroundMainAgentScheduler:
    agent = SimpleNamespace(
        config=AgentConfig(model_backend="echo"),
        conversation_store=store,
    )
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store)
    return BackgroundMainAgentScheduler({"runtime": runtime, "store": store})


def _create_thread(store: ConversationStore, name: str):
    return store.threads.get_or_create(
        {
            "canonical_user_id": "owner",
            "channel": "internal",
            "channel_conversation_id": name,
            "channel_user_id": "owner",
            "now": 1000.0,
        }
    )


@pytest.mark.parametrize("thread_count", [20, 50])
def test_single_thread_consumption_decodes_only_target_thread_across_20_and_50_threads(
    tmp_path: Path, monkeypatch, thread_count: int
) -> None:
    store = ConversationStore(tmp_path / "conversations")
    payload = "x" * 16384
    expected_obs: dict[str, str] = {}
    for idx in range(thread_count):
        thread = _create_thread(store, f"agent-{idx:03d}")
        event = store.observations.append(
            {
                "thread_id": thread.thread_id,
                "event_type": "tool_completion",
                "summary": f"Completed {idx}: {payload}",
                "requires_main_agent": True,
                "now": 1000.0 + idx,
            }
        )
        expected_obs[thread.thread_id] = event.observation_id
    _warm_observation_index(store)
    scheduler = _new_scheduler(store)

    index = store.observations._index
    original_pending = index.pending
    per_tick_work: list[dict[str, int]] = []

    def measured_pending(*args, **kwargs):
        result = original_pending(*args, **kwargs)
        per_tick_work.append(dict(index.last_work))
        return result

    dispatched: list[tuple[str, list[str]]] = []

    def fake_run_batch(_scheduler, thread_id, observations, *, now):
        dispatched.append((thread_id, [item.observation_id for item in observations]))
        return None

    monkeypatch.setattr(index, "pending", measured_pending)
    monkeypatch.setattr(runtime_module, "_run_observation_batch", fake_run_batch)

    for thread_id in expected_obs:
        scheduler.tick_thread(thread_id, now=2000.0)

    assert len(per_tick_work) == thread_count
    assert [entry["candidate_rows"] for entry in per_tick_work] == [1] * thread_count
    assert [entry["json_decodes"] for entry in per_tick_work] == [1] * thread_count
    total_bytes = sum(entry["bytes_read"] for entry in per_tick_work)
    assert thread_count * 16384 < total_bytes < thread_count * 20480
    assert dispatched == [
        (thread_id, [obs_id]) for thread_id, obs_id in expected_obs.items()
    ]


def test_single_thread_tick_marks_only_target_thread_handled(
    tmp_path: Path, monkeypatch
) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread_ids: list[str] = []
    obs_by_thread: dict[str, str] = {}
    for idx in range(25):
        thread = _create_thread(store, f"lane-{idx:02d}")
        event = store.observations.append(
            {
                "thread_id": thread.thread_id,
                "event_type": "tool_completion",
                "summary": f"Result {idx}: " + ("y" * 4096),
                "requires_main_agent": True,
                "now": 1000.0 + idx,
            }
        )
        thread_ids.append(thread.thread_id)
        obs_by_thread[thread.thread_id] = event.observation_id
    _warm_observation_index(store)
    scheduler = _new_scheduler(store)

    def fake_run_claimed(_deps, params):
        return BackgroundMainAgentReport(
            thread_id=params["thread_id"],
            task_id=params["task_id"],
            reason=params["reason"],
            response="done",
            route_channel=params["route_channel"],
            route_target=params["route_target"],
            created_at=params["now"],
        )

    monkeypatch.setattr(runtime_module.background_claim, "run_claimed", fake_run_claimed)
    monkeypatch.setattr(
        runtime_module,
        "resolve_background_route",
        lambda _deps, _thread_id: ("internal", "owner"),
    )
    index = store.observations._index
    original_pending = index.pending
    pending_work: list[dict[str, int]] = []

    def spy_pending(*args, **kwargs):
        rows = original_pending(*args, **kwargs)
        pending_work.append(dict(index.last_work))
        return rows

    monkeypatch.setattr(index, "pending", spy_pending)

    target = thread_ids[12]
    reports = scheduler.tick_thread(target, now=2000.0)
    assert len(reports) == 1
    assert reports[0].thread_id == target
    assert len(pending_work) == 1
    assert pending_work[0]["candidate_rows"] == 1
    assert pending_work[0]["json_decodes"] == 1
    assert store.observations.unhandled_requiring_main(limit=0, thread_id=target) == []
    remaining = store.observations.unhandled_requiring_main(limit=0)
    assert len(remaining) == 24
    assert obs_by_thread[target] not in {item.observation_id for item in remaining}


@pytest.mark.parametrize("thread_count", [3, 20])
def test_thread_id_none_preserves_all_owner_behavior_and_scoped_lookup(
    tmp_path: Path, monkeypatch, thread_count: int
) -> None:
    store = ConversationStore(tmp_path / "conversations")
    created: list[tuple[str, list[str]]] = []
    for idx in range(thread_count):
        thread = _create_thread(store, f"all-{idx:02d}")
        obs_ids: list[str] = []
        for item_idx in range(2):
            event = store.observations.append(
                {
                    "thread_id": thread.thread_id,
                    "event_type": "tool_completion",
                    "summary": f"t={idx} i={item_idx}",
                    "requires_main_agent": True,
                    "now": 1000.0 + idx * 10 + item_idx,
                }
            )
            obs_ids.append(event.observation_id)
        created.append((thread.thread_id, obs_ids))
    _warm_observation_index(store)

    all_default = store.observations.unhandled_requiring_main(limit=0)
    all_explicit_none = store.observations.unhandled_requiring_main(
        limit=0, thread_id=None
    )
    assert [item.observation_id for item in all_default] == [
        item.observation_id for item in all_explicit_none
    ]
    assert len(all_default) == thread_count * 2
    assert len(store.observations.unhandled_requiring_main(limit=4, thread_id=None)) == 4

    target_tid, target_obs = created[thread_count // 2]
    scoped_all = store.observations.unhandled_requiring_main(limit=0, thread_id=target_tid)
    assert [item.observation_id for item in scoped_all] == target_obs
    scoped_one = store.observations.unhandled_requiring_main(limit=1, thread_id=target_tid)
    assert [item.observation_id for item in scoped_one] == target_obs[:1]

    with pytest.raises(ValueError):
        store.observations.unhandled_requiring_main(limit=0, thread_id="")
    with pytest.raises(ValueError):
        store.observations.unhandled_requiring_main(limit=0, thread_id="   ")

    scheduler = _new_scheduler(store)
    visited: list[str] = []
    monkeypatch.setattr(
        runtime_module,
        "_run_observation_batch",
        lambda _s, thread_id, observations, *, now: visited.append(thread_id) or None,
    )
    scheduler.tick(now=2000.0)
    assert len(visited) >= min(thread_count, 10)
    assert set(visited).issubset({tid for tid, _ in created})


def test_admitted_limit_20_applies_after_linked_wake_and_recovery_filtering(
    tmp_path: Path, monkeypatch
) -> None:
    store = ConversationStore(tmp_path / "conversations")
    target = _create_thread(store, "target-thread")
    for idx in range(20):
        distractor = _create_thread(store, f"distractor-{idx:02d}")
        store.observations.append(
            {
                "thread_id": distractor.thread_id,
                "event_type": "tool_completion",
                "summary": f"distractor-{idx}",
                "requires_main_agent": True,
                "now": 50.0 + idx,
            }
        )

    for idx in range(25):
        store.wakes.append_observation(
            {
                "thread_id": target.thread_id,
                "event_type": "tool_completion",
                "summary": f"linked-wake-{idx}",
                "root_task_id": "task-linked",
                "requires_main_agent": True,
                "now": 100.0 + idx,
            },
            {
                "thread_id": target.thread_id,
                "reason": "linked_wake",
                "dedupe_key": f"linked-{idx}",
                "now": 100.0 + idx,
            },
        )
    for idx in range(10):
        store.observations.append(
            {
                "thread_id": target.thread_id,
                "event_type": "tool_completion",
                "summary": f"blocked-task-{idx}",
                "root_task_id": "task-blocked",
                "requires_main_agent": True,
                "now": 200.0 + idx,
            }
        )
    ready_obs_ids: list[str] = []
    for idx in range(25):
        event = store.observations.append(
            {
                "thread_id": target.thread_id,
                "event_type": "tool_completion",
                "summary": f"ready-{idx}",
                "root_task_id": "task-ready",
                "requires_main_agent": True,
                "now": 300.0 + idx,
            }
        )
        ready_obs_ids.append(event.observation_id)
    _warm_observation_index(store)

    scheduler = _new_scheduler(store)
    scheduler._recovery_guard = SimpleNamespace(
        block_for_task=lambda task_id: ("blocked" if task_id == "task-blocked" else None)
    )

    store_calls: list[tuple[int, str | None]] = []
    pending_work: list[dict[str, int]] = []
    index = store.observations._index
    original_pending = index.pending
    original_unhandled = store.observations.unhandled_requiring_main

    def spy_pending(*args, **kwargs):
        rows = original_pending(*args, **kwargs)
        pending_work.append(dict(index.last_work))
        return rows

    def spy_unhandled(*, limit: int = 20, thread_id: str | None = None):
        store_calls.append((limit, thread_id))
        return original_unhandled(limit=limit, thread_id=thread_id)

    linked_checks = 0
    original_linked = runtime_module._observation_waits_for_linked_wake

    def spy_linked(obs_store, observation):
        nonlocal linked_checks
        linked_checks += 1
        return original_linked(obs_store, observation)

    admitted_batches: list[list[str]] = []

    def fake_run_batch(_scheduler, thread_id, observations, *, now):
        assert thread_id == target.thread_id
        ids = [item.observation_id for item in observations]
        admitted_batches.append(ids)
        store.observations.mark_handled(ids, now=now)
        return BackgroundMainAgentReport(
            thread_id=thread_id,
            task_id="task-ready",
            reason="observation",
            response="ok",
            route_channel="internal",
            route_target="owner",
            created_at=now,
        )

    monkeypatch.setattr(index, "pending", spy_pending)
    monkeypatch.setattr(
        store.observations, "unhandled_requiring_main", spy_unhandled
    )
    monkeypatch.setattr(
        runtime_module, "_observation_waits_for_linked_wake", spy_linked
    )
    monkeypatch.setattr(runtime_module, "_run_observation_batch", fake_run_batch)

    reports: list[BackgroundMainAgentReport] = []
    reported: set[str] = set()
    runtime_module._consume_observation_batches(
        scheduler, reports, reported, 2000.0, target_thread_id=target.thread_id
    )

    assert store_calls == [(0, target.thread_id)]
    assert len(pending_work) == 1
    assert pending_work[0]["candidate_rows"] == 60
    assert pending_work[0]["json_decodes"] == 60
    assert linked_checks == 60
    assert admitted_batches == [ready_obs_ids[:20]]
    assert len(reports) == 1 and reported == {target.thread_id}

    reports_second: list[BackgroundMainAgentReport] = []
    reported_second: set[str] = set()
    runtime_module._consume_observation_batches(
        scheduler, reports_second, reported_second, 2001.0, target_thread_id=target.thread_id
    )
    assert admitted_batches == [ready_obs_ids[:20], ready_obs_ids[20:]]


def test_incomplete_index_and_corrupt_legacy_still_propagate_on_scoped_path(
    tmp_path: Path, monkeypatch
) -> None:
    store = ConversationStore(tmp_path / "conversations")
    threads = []
    for idx in range(20):
        thread = _create_thread(store, f"cold-{idx:02d}")
        store.observations.append(
            {
                "thread_id": thread.thread_id,
                "event_type": "tool_completion",
                "summary": f"cold-{idx}",
                "requires_main_agent": True,
                "now": 100.0 + idx,
            }
        )
        threads.append(thread.thread_id)

    cold = ConversationStore(store.storage.root, initialize=False)
    target = threads[15]
    with pytest.raises(ObservationIndexIncomplete):
        cold.observations.unhandled_requiring_main(limit=0, thread_id=target)

    report_events, report_errors = cold.observations.unhandled_requiring_main_report(
        target, limit=0
    )
    assert report_events == []
    assert report_errors and report_errors[0]["coverage"] == "partial"
    assert report_errors[0]["error_type"] == "ObservationIndexIncomplete"

    cold_scheduler = _new_scheduler(cold)
    with pytest.raises(ObservationIndexIncomplete):
        cold_scheduler.tick_thread(target, now=2000.0)

    _warm_observation_index(cold)
    dispatched: list[str] = []
    monkeypatch.setattr(
        runtime_module,
        "_run_observation_batch",
        lambda _s, thread_id, _obs, *, now: dispatched.append(thread_id) or None,
    )
    cold_scheduler.tick_thread(target, now=2000.0)
    assert dispatched == [target]

    corrupt_store = ConversationStore(tmp_path / "corrupt")
    corrupt_thread = _create_thread(corrupt_store, "corrupt-thread")
    corrupt_store.observations.append(
        {
            "thread_id": corrupt_thread.thread_id,
            "event_type": "tool_completion",
            "summary": "corrupt-test",
            "requires_main_agent": True,
            "now": 100.0,
        }
    )
    _warm_observation_index(corrupt_store)
    corrupt_store.storage.observation_handled_path.write_bytes(b"{broken")

    with pytest.raises(ObservationIndexError):
        corrupt_store.observations.unhandled_requiring_main(
            limit=0, thread_id=corrupt_thread.thread_id
        )
    corrupt_scheduler = _new_scheduler(corrupt_store)
    with pytest.raises(ObservationIndexError):
        corrupt_scheduler.tick_thread(corrupt_thread.thread_id, now=2000.0)
