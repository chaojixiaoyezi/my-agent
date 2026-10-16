"""后台便宜检查与当前主线的原子快照、监控规则、取消和事件派发边界。"""

from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.scheduler import dispatch
from agent_py_agent.agent.scheduler import repository as scheduler_module
from agent_py_agent.agent.scheduler.repository import (
    SchedulerJobCreateRequest,
    SchedulerRepository,
    SchedulerStateError,
)
from agent_py_agent.agent.scheduler.service import SchedulerService
from agent_py_agent.agent.tooling import process_session_store as process_store_module
from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore
from agent_py_agent.tests.test_background_tick_cheap_checks import (
    _age_path,
    _repository,
    _reservation,
    _store_with_waiting,
    _TransactionCounter,
    _wanting_ids,
    _write_store,
)
from agent_py_agent.tests.test_scheduler_event_sources import event, repository, schedule


@pytest.fixture
def projections():
    scheduler_module._WAITING_PROJECTION_CACHE.clear()
    scheduler_module._STORE_PROJECTION_CACHE.clear()
    yield scheduler_module
    scheduler_module._WAITING_PROJECTION_CACHE.clear()
    scheduler_module._STORE_PROJECTION_CACHE.clear()


@pytest.fixture
def process_projections():
    process_store_module._PROCESS_RECORDS_PROJECTION_CACHE.clear()
    yield process_store_module
    process_store_module._PROCESS_RECORDS_PROJECTION_CACHE.clear()


@pytest.mark.parametrize("cheap", [False, True])
@pytest.mark.parametrize("replacement", ["symlink", "hardlink"])
def test_cached_scheduler_still_rejects_links_to_the_same_inode(tmp_path, projections, cheap, replacement):
    repo = _repository(tmp_path / "scheduler")
    _write_store(repo.store_path, _store_with_waiting(1))
    _age_path(repo.store_path)
    repo.waiting_runs(cheap_checks=True)
    if replacement == "symlink":
        moved = repo.store_path.with_name("moved.json")
        repo.store_path.replace(moved)
        repo.store_path.symlink_to(moved.name)
    else:
        os.link(repo.store_path, repo.store_path.with_name("linked.json"))
    with pytest.raises(SchedulerStateError) as caught:
        repo.waiting_runs(cheap_checks=cheap)
    assert caught.value.error_code == "SCHEDULER_STORE_UNREADABLE"


@pytest.mark.parametrize("cheap", [False, True])
def test_cached_process_directory_rechecks_link_through_original_transaction(tmp_path, process_projections, monkeypatch, cheap):
    store = ProcessSessionStore(tmp_path / "processes")
    store.write(_reservation())
    _age_path(store.root)
    records, errors = store.list_records(cheap_checks=True)
    assert len(records) == 1 and errors == []
    moved = store.root.with_name("moved-processes")
    store.root.replace(moved)
    store.root.symlink_to(moved.name, target_is_directory=True)
    counter = _TransactionCounter()
    counter.install(monkeypatch)
    records, errors = store.list_records(cheap_checks=cheap)
    assert len(records) == 1 and errors == []
    assert counter.calls == 1


def test_same_inode_same_size_coarse_timestamp_rewrite_is_read_in_guard_window(tmp_path, projections):
    repo = _repository(tmp_path / "scheduler")
    _write_store(repo.store_path, _store_with_waiting(1, total=1))
    coarse_ns = int(time.time()) * 1_000_000_000
    os.utime(repo.store_path, ns=(coarse_ns, coarse_ns))
    before = repo.store_path.stat()
    first, _ = repo.waiting_runs(cheap_checks=True)
    assert _wanting_ids(first) == ["srun_000"]
    assert repo.waiting_projection_cache_key() not in projections._STORE_PROJECTION_CACHE
    payload = json.loads(repo.store_path.read_text())
    payload["runs"]["srun_000"]["status"] = "running"
    repo.store_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    os.utime(repo.store_path, ns=(coarse_ns, coarse_ns))
    after = repo.store_path.stat()
    assert (before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_ino, after.st_size, after.st_mtime_ns)
    assert repo.waiting_runs(cheap_checks=True) == ([], [])


@pytest.mark.parametrize("cheap", [False, True])
def test_waiting_reads_keep_main_lock_free_snapshot_path(tmp_path, projections, monkeypatch, cheap):
    repo = _repository(tmp_path / "scheduler")
    _write_store(repo.store_path, _store_with_waiting(1))
    _age_path(repo.store_path)

    def forbidden(*_args, **_kwargs):
        pytest.fail("只读waiting投影不能恢复来源旧写锁")

    monkeypatch.setattr(scheduler_module, "locked_json_path", forbidden)
    first = repo.waiting_runs(cheap_checks=cheap)
    assert repo.waiting_runs(cheap_checks=cheap) == first
    assert _wanting_ids(first[0]) == ["srun_000"]


def test_process_projection_lru_evicts_oldest_owner_and_hit_moves_entry(tmp_path, process_projections):
    stores = []
    for index in range(65):
        store = ProcessSessionStore(tmp_path / f"owner-{index}")
        store.write(_reservation())
        _age_path(store.root)
        store.list_records(cheap_checks=True)
        stores.append(store)
    cache = process_projections._PROCESS_RECORDS_PROJECTION_CACHE
    assert len(cache) == 64 and str(stores[0].root) not in cache
    stores[1].list_records(cheap_checks=True)
    extra = ProcessSessionStore(tmp_path / "owner-extra")
    extra.write(_reservation())
    _age_path(extra.root)
    extra.list_records(cheap_checks=True)
    assert len(cache) == 64
    assert str(stores[1].root) in cache and str(stores[2].root) not in cache


@pytest.mark.parametrize("cheap", [False, True])
def test_hot_projection_keeps_rule_due_time_creator_delivery_and_cancellation(tmp_path, projections, cheap):
    repo = SchedulerRepository(tmp_path / "scheduler", owner_provider="feishu", owner_kind="group", owner_id="test-group")
    creator = {"channel": "feishu", "actor_id": "test-human"}
    rules = [{"id": name, "name": name, "prompt": "读取既有事实", "use_llm": True,
              "schedule": {"kind": "every", "every_seconds": 600, "anchor_at": stamp, "timezone": "UTC"}}
             for name, stamp in [("first", 1000), ("later", 1600)]]
    job, _ = repo.create_job(SchedulerJobCreateRequest(
        name="隔离监控", prompt="", schedule={}, thread_id="thread-1", rules=rules,
        creator=creator, delivery={"mode": "origin", "reply_policy": "explicit"}, now=900,
    ))
    _age_path(repo.store_path)
    repo.waiting_runs(cheap_checks=cheap)
    assert repo.reserve_due_runs(now=999, cheap_checks=cheap) == []
    [run] = repo.reserve_due_runs(now=1000, cheap_checks=cheap)
    assert run["rule_id"] == "first"
    assert run["creator"] == creator and run["delivery"]["reply_policy"] == "explicit"
    claim = repo.claim_run(run["run_id"], lease_seconds=60, now=1001)
    repo.mark_run_running(run["run_id"], claim["claim_id"], now=1002)
    _age_path(repo.store_path)
    repo.waiting_runs(cheap_checks=cheap)
    current = repo.get_job(job["job_id"])
    repo.delete_job(job["job_id"], expected_version=current["version"], now=1003)
    assert repo.queued_runs(now=1600, cheap_checks=cheap) == []
    assert repo.reserve_due_runs(now=1600, cheap_checks=cheap) == []
    [pending] = repo.pending_cancellation_runs()
    assert pending["run_id"] == run["run_id"] and pending["cancel_requested_at"] == 1003


@pytest.mark.parametrize("cheap", [False, True])
def test_empty_due_projection_does_not_skip_cancel_recovery_or_event_reservation(cheap):
    calls = []
    repo = SimpleNamespace(
        pending_cancellation_runs=lambda: [{"run_id": "cancel-me"}],
        recover_interrupted_executions=lambda **kwargs: calls.append(("recovery", kwargs["now"])),
        reserve_due_runs=lambda **kwargs: calls.append(("due", kwargs["cheap_checks"])),
        reserve_event_runs=lambda **kwargs: calls.append(("event", kwargs["now"])),
        queued_runs=lambda **kwargs: [],
    )
    service = SimpleNamespace(repository=repo, cancellation_handler=lambda run: calls.append(("cancel", run["run_id"])))
    assert dispatch.enqueue_ready_runs(service, now=1000, cheap_checks=cheap) == []
    assert calls == [("cancel", "cancel-me"), ("recovery", 1000), ("due", cheap), ("event", 1000)]


@pytest.mark.parametrize("cheap", [False, True])
def test_group_event_dispatch_and_retirement_keep_main_creator_and_explicit_delivery(tmp_path, projections, cheap):
    from agent_py_agent.agent.conversation.store import ConversationStore

    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "human-1", "channel": "feishu",
        "channel_user_id": "human-1", "channel_conversation_id": "group-1", "now": 900})
    repo = repository(tmp_path)
    creator = {"channel": "feishu", "actor_id": "human-1"}
    job, _ = repo.create_job(SchedulerJobCreateRequest(name="隔离事件订阅", prompt="核对告警",
        thread_id=thread.thread_id, schedule=schedule(), creator=creator,
        delivery={"mode": "origin", "reply_policy": "explicit"}, now=900))
    service = SchedulerService(repo, conversation_store=store)
    _age_path(repo.store_path)
    repo.waiting_runs(cheap_checks=cheap)
    assert repo.accept_source_event(event(), now=1000)["status"] == "accepted"
    [wake_id] = service.enqueue_ready_runs(now=1000, cheap_checks=cheap)
    [signal] = store.wakes.pending()
    assert signal.wake_signal_id == wake_id
    run = repo.get_active_run(signal.root_task_id)
    assert run["creator"] == creator and run["event_batch"][0]["message_id"] == "message-1"
    assert run["delivery"] == {"mode": "origin", "reply_policy": "explicit"}
    current = repo.get_job(job["job_id"])
    repo.delete_job(job["job_id"], expected_version=current["version"], now=1001)
    assert service.enqueue_ready_runs(now=1061, cheap_checks=cheap) == []
    assert service.claim_wake(signal, now=1061).status == "stale"
