"""W2：恢复交接、领取版本、存储等待和分拍预算；仅使用临时库与假时钟。"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from agent_py_agent.agent.agent_core import runtime_mixin
from agent_py_agent.agent.conversation import background_claim, runtime
from agent_py_agent.agent.conversation.background_recovery import BackgroundRecoveryGuard
from agent_py_agent.tests.test_q4_foreground_recovery_regressions import _q4_case


def _scheduler(agent):
    from agent_py_agent.agent.settings.config import AgentConfig

    config = AgentConfig()
    agent.config = SimpleNamespace(**{**vars(config), **vars(agent.config)})
    store = agent.conversation_store
    executor = runtime.BackgroundMainAgentRuntime(agent=agent, store=store)
    calls = []
    executor.run_once = lambda request: calls.append(request) or SimpleNamespace()
    scheduler = runtime.BackgroundMainAgentScheduler({"runtime": executor, "store": store})
    return scheduler, calls


def test_unknown_and_pending_handoff_keep_responsibility_for_100_ticks(tmp_path, monkeypatch):
    agent, repo, rec, _operation, params = _q4_case(tmp_path)
    scheduler, calls = _scheduler(agent)
    signal = scheduler.store.wakes.raise_signal({
        "thread_id": params.task_attributes["conversation_thread_id"],
        "root_task_id": params.task_id, "reason": "thread_goal_continue", "now": 1.0,
    })
    for state in ("unknown", "pending_handoff"):
        if state == "pending_handoff":
            params = runtime_mixin._auto_recover_unknown_for_foreground_user_turn(agent, repo,
                repo.get_agent_run(rec["agent_run_id"]), params)
        for tick in range(100):
            assert runtime._skip_pending_wake_signal(scheduler, signal, current=10.0 + tick,
                reported=set(), handled=set(), attempted=set())
        assert scheduler.store.wakes.pending_one(signal.wake_signal_id) is not None
        assert calls == []
        block = scheduler._recovery_guard.block_for_task(params.task_id)
        assert block is not None and block.get("eligible") is False
        assert block.get("source_revision") and "next_check_at" in block and "handoff_ref" in block


def test_handoff_is_structured_and_claims_once_after_notice_copy_changes(tmp_path, monkeypatch):
    agent, repo, rec, _operation, params = _q4_case(tmp_path)
    row = repo.get_agent_run(rec["agent_run_id"])
    bound = runtime_mixin._auto_recover_unknown_for_foreground_user_turn(agent, repo, row, params)
    handoff = getattr(bound, "foreground_recovery_handoff_ref", None)
    assert isinstance(handoff, dict) and handoff["attempt_ids"] == [rec["attempt_id"]]
    assert handoff["thread_id"] == params.task_attributes["conversation_thread_id"]
    changed = replace(bound, source="background", compact_continuation_injection="说明已改写", inject=["说明已改写"])
    assert runtime_mixin._auto_recover_unknown_for_foreground_user_turn(agent, repo, row, changed) is changed
    runtime_mixin._acknowledge_foreground_recovery_notice(agent, changed, SimpleNamespace(runtime_status="ok"))
    assert repo.main_agent_recovery_block_for_task(params.task_id) is None
    scheduler, calls = _scheduler(agent)
    signal = scheduler.store.wakes.raise_signal({"thread_id": handoff["thread_id"],
        "root_task_id": params.task_id, "reason": "thread_goal_continue", "now": 1.0})
    scheduler.runtime.run_once = lambda request: (calls.append(request), scheduler.store.wakes.mark_handled(
        signal.wake_signal_id, now=10.0), SimpleNamespace())[2]
    monkeypatch.setattr(background_claim, "ConversationRunClaimHeartbeat", lambda _: SimpleNamespace(start=lambda: None, stop=lambda: None))
    request = {"thread_id": handoff["thread_id"], "task_id": params.task_id,
               "reason": signal.reason, "wake_signal": signal, "now": 10.0}
    dependencies = runtime._background_claim_dependencies(scheduler)
    assert background_claim.run_claimed(dependencies, request) is not None
    assert background_claim.run_claimed(dependencies, request) is None
    assert len(calls) == 1


def test_stale_discovery_revision_is_refused_and_healthy_task_can_run(tmp_path, monkeypatch):
    agent, repo, rec, _operation, params = _q4_case(tmp_path)
    scheduler, calls = _scheduler(agent)
    guard = BackgroundRecoveryGuard(lambda: repo.main_agent_recovery_block_for_task)
    block = guard.block_for_task(params.task_id)
    assert block is not None and block.get("source_revision")
    runtime_mixin._auto_recover_unknown_for_foreground_user_turn(agent, repo,
        repo.get_agent_run(rec["agent_run_id"]), params)
    stale = guard.block_for_task(params.task_id, expected_revision=block["source_revision"])
    assert stale["reason_code"] == "source_revision_changed"
    repo.record_run_creation(owner_id=agent.home_paths.owner_id, run_id="healthy-run",
        conversation_task_id="healthy-task", thread_id=params.task_attributes["conversation_thread_id"], role="main")
    scheduler.store.tasks.bind({"thread_id": params.task_attributes["conversation_thread_id"],
        "task_id": "healthy-task", "goal": "健康任务"})
    monkeypatch.setattr(background_claim, "ConversationRunClaimHeartbeat", lambda _: SimpleNamespace(start=lambda: None, stop=lambda: None))
    assert background_claim.run_claimed(runtime._background_claim_dependencies(scheduler), {
        "thread_id": params.task_attributes["conversation_thread_id"], "task_id": "healthy-task",
        "reason": "ordinary_task_resume", "now": 10.0}) is not None
    assert len(calls) == 1


def test_scheduler_quota_storage_wait_is_bounded_and_preserves_due(tmp_path, monkeypatch, caplog):
    from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaUnavailable

    agent, _repo, _rec, _op, _params = _q4_case(tmp_path)
    scheduler, _calls = _scheduler(agent)
    service = SimpleNamespace(attempts=0, available=False, due=["original-due"])

    def reconcile(**_kwargs):
        service.attempts += 1
        if not service.available:
            raise OwnerQuotaUnavailable("quota ledger not ready")

    def enqueue(**_kwargs):
        return service.due.pop()

    scheduler.scheduler_service = SimpleNamespace(reconcile_waiting_runs=reconcile, enqueue_ready_runs=enqueue)
    scheduler._tick_cheap_checks = lambda _: False
    with caplog.at_level("WARNING"):
        for tick in range(100):
            scheduler._enqueue_scheduler_runs(now=10.0 + tick / 10)
        assert service.due == ["original-due"]
        assert service.attempts <= 5
        assert len(caplog.records) == 1 and caplog.records[0].exc_info is None
        wait = scheduler._scheduler_storage_wait.snapshot()
        assert wait["status"] == "storage_wait" and wait["next_check_at"] > 19
        assert wait["wait_count"] == service.attempts
        service.available = True
        scheduler._enqueue_scheduler_runs(now=wait["next_check_at"])
    assert service.due == []
    assert scheduler._scheduler_storage_wait.snapshot()["status"] == "ready"


def test_quota_storage_wait_lane_does_not_block_other_thread(monkeypatch, capsys):
    from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaLockContention
    from agent_py_agent.cli import gateway_lane_retry

    clock = [10.0]
    monkeypatch.setattr(gateway_lane_retry.time, "monotonic", lambda: clock[0])
    retry = gateway_lane_retry.BackgroundLaneRetry()
    retry.failed("owner", "waiting", OwnerQuotaLockContention(), delay=0.0)
    for _ in range(100):
        assert not retry.ready("owner", "waiting", model_ready=lambda: True)
        assert retry.ready("owner", "healthy", model_ready=lambda: True)
    assert "storage_wait" in capsys.readouterr().out
    clock[0] += 60
    assert retry.ready("owner", "waiting", model_ready=lambda: True)
    retry.succeeded("owner", "waiting")
    assert "storage_resumed" in capsys.readouterr().out


def test_claim_rechecks_eligible_discovery_revision_before_acquiring(tmp_path):
    agent, repo, rec, _operation, params = _q4_case(tmp_path)
    bound = runtime_mixin._auto_recover_unknown_for_foreground_user_turn(agent, repo,
        repo.get_agent_run(rec["agent_run_id"]), params)
    runtime_mixin._acknowledge_foreground_recovery_notice(agent, bound, SimpleNamespace(runtime_status="ok"))
    discovered = repo.recovery_eligibility_for_task(params.task_id)
    assert discovered["eligible"]
    with repo.transaction() as conn:
        conn.execute("UPDATE agent_attempts SET status='unknown' WHERE attempt_id=?", (rec["attempt_id"],))
    scheduler, calls = _scheduler(agent)
    acquired = []
    dependencies = replace(runtime._background_claim_dependencies(scheduler),
        claims=SimpleNamespace(acquire=lambda request: acquired.append(request)))
    for tick in range(100):
        assert background_claim.run_claimed(dependencies, {"thread_id": discovered.get("thread_id", "thread"),
            "task_id": params.task_id, "recovery_source_revision": discovered["source_revision"],
            "now": float(tick)}) is None
    assert not acquired and not calls


def test_history_source_page_does_not_materialize_whole_directory():
    from pathlib import Path

    from agent_py_agent.agent.gateway_parts.multiprocess_background_discovery import _source_page

    seen = []

    class Folder:
        def __str__(self):
            return "history"

        def __fspath__(self):
            return "history"

        def glob(self, _pattern):
            for index in range(10000):
                seen.append(index)
                yield Path("history") / f"{index:05d}.json"

    discovery = SimpleNamespace(_source_cursors={})
    folder = Folder()
    first = _source_page(discovery, folder, "*.json")
    second = _source_page(discovery, folder, "*.json")
    assert len(first) == len(second) == 64
    assert not set(first) & set(second)
    assert len(seen) <= 130


def test_derived_prune_resumes_bounded_scan_and_keeps_cross_batch_active_group(tmp_path):
    from agent_py_agent.agent.local_storage import LocalStore
    from agent_py_agent.tests.test_local_store_derived_ttl_wave6 import CUTOFF, record

    path = tmp_path / "local.db"
    store = LocalStore(path)
    protected = record(store, "old-related", metadata={"status": "done", "run_id": "shared"})
    expired = [record(store, f"old-{index}") for index in range(5)]
    active = record(store, "active", metadata={"status": "running", "run_id": "shared"})
    assert "max_scan" in __import__("inspect").signature(store.prune_derived_records).parameters
    deleted = []
    positions = []
    for _ in range(100):
        report = LocalStore(path).prune_derived_records(before=CUTOFF, protected_refs=(), max_scan=2, max_delete=2)
        assert report["scanned"] <= 2
        deleted.extend(report["deleted_ids"])
        positions.append(report["cursor"])
        if not report["partial"]:
            break
    else:
        raise AssertionError("bounded derived maintenance never completed")
    assert set(deleted) == {item.id for item in expired}
    assert store.get_record(protected.id) and store.get_record(active.id)
    assert len({str(position) for position in positions}) > 1


def test_event_cleanup_resumes_jsonl_budget_and_separates_page_reuse_from_shrink(tmp_path):
    import json

    from agent_py_agent.agent.local_storage import LocalStore

    path = tmp_path / "local.db"
    store = LocalStore(path)
    for index in range(20):
        store.record_event("test", payload={"index": index, "padding": "x" * 80})
    step = getattr(store, "prune_events_step", None)
    assert callable(step), "background event pruning must expose a bounded step"
    deleted = 0
    reports = []
    for _ in range(200):
        report = LocalStore(path).prune_events_step(before=2_000_000_000, keep_last=2, max_delete=5, max_bytes=256)
        assert report["scanned_bytes"] <= 256
        deleted += report["deleted_db"]
        reports.append(report)
        if not report["partial"]:
            break
    else:
        raise AssertionError("event JSONL cursor did not complete")
    assert deleted == 5
    remaining = [json.loads(line) for line in store.events_path.read_text().splitlines()]
    assert len(remaining) == 15
    assert all("expired_rows" in result and "free_page_reuse" in result and "physical_shrink" in result for result in reports)
    assert all(result["physical_shrink"]["performed"] is False for result in reports)


def test_background_progress_and_final_share_original_topic_context(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.background_execution import BackgroundExecutionResult
    from agent_py_agent.agent.conversation.background_im_progress import BackgroundIMProgress
    from agent_py_agent.tests.test_background_im_progress import _scheduled_followup

    agent, work, _job, run, _sent, _edits = _scheduled_followup(tmp_path, {'mode': 'origin', 'reply_policy': 'always'})
    snapshot = {'reply_to': 'om_original', 'reply_policy': 'thread', 'provider_thread_id': 'omt_provider',
                'root_id': 'om_root', 'parent_id': 'om_parent'}
    agent.conversation_store.threads.update_atomic(work.thread_id,
        lambda row: replace(row, metadata={**row.metadata, 'delivery_context': snapshot}))
    observer = BackgroundIMProgress(agent, run['run_id'])
    try:
        progress = observer.context
        assert progress.reply_to == snapshot['reply_to']
        host = runtime.BackgroundMainAgentRuntime(agent=agent, store=agent.conversation_store)
        host._run_agent = lambda *_a, **_kw: BackgroundExecutionResult('合成结果', 0, 0, 0, (), (), {}, (), {})
        monkeypatch.setattr(runtime, '_complete_background_slice', lambda _host, plan: plan.delivery_context)
        # 该旧progress夹具的scheduler run没有execution_context，只覆盖进度准入；最终入口使用同执行来源的普通根任务。
        agent.conversation_store.tasks.bind({'thread_id': work.thread_id, 'task_id': 'ordinary-final', 'goal': '合成最终回复'})
        final = host.run_once({'thread_id': work.thread_id, 'task_id': 'ordinary-final', 'reason': 'ordinary_resume',
                              'route_channel': 'feishu', 'route_target': 'ou_test'})
        for key, value in snapshot.items():
            assert getattr(progress, key) == getattr(final, key) == value
        assert final.thread_id != snapshot['provider_thread_id']
        assert final.target == progress.target == 'ou_test'
    finally:
        observer.close()


def test_background_run_request_freezes_topic_and_old_call_defaults():
    from agent_py_agent.agent.conversation.background_im_progress import background_delivery_context

    snapshot = {'reply_to': 'om_old', 'reply_policy': 'thread', 'provider_thread_id': 'provider-only'}
    request = runtime._run_request({'thread_id': 'internal-thread', 'delivery_context': snapshot})
    snapshot['reply_to'] = 'om_new'
    context = background_delivery_context(SimpleNamespace(), SimpleNamespace(thread_id='internal-thread'),
        channel='feishu', target='oc_frozen', request=request)
    assert context.reply_to == 'om_old' and context.provider_thread_id == 'provider-only'
    assert context.thread_id == 'internal-thread' and context.target == 'oc_frozen'
    old = background_delivery_context(SimpleNamespace(), SimpleNamespace(thread_id='legacy'),
        channel='internal', target='')
    assert old.reply_to == ''


def test_event_cleanup_recovers_after_publish_before_cursor_commit(tmp_path, monkeypatch):
    import json

    from agent_py_agent.agent.local_storage import LocalStore, maintenance

    store = LocalStore(tmp_path / 'local.db')
    for index in range(5):
        store.record_event('test', payload={'number': index})
    original = maintenance._event_step_save
    failed = []

    def crash(conn, job, targets):
        if job is None and targets and not failed:
            failed.append(True)
            raise OSError('crash after file publication')
        return original(conn, job, targets)

    monkeypatch.setattr(maintenance, '_event_step_save', crash)
    import pytest

    with pytest.raises(OSError):
        for _ in range(100):
            store.prune_events_step(before=2_000_000_000, keep_last=2, max_delete=3, max_bytes=48)
    for _ in range(100):
        result = LocalStore(store.db_path).prune_events_step(before=2_000_000_000, keep_last=2, max_delete=3, max_bytes=48)
        if not result['partial']:
            break
    assert not result['partial'] and len([json.loads(line) for line in store.events_path.read_text().splitlines()]) == 2


def test_real_source_directory_page_uses_bounded_scandir(tmp_path, monkeypatch):
    from pathlib import Path

    from agent_py_agent.agent.gateway_parts.multiprocess_background_discovery import _source_page

    for index in range(150):
        (tmp_path / f'{index}.json').write_text('{}')
    monkeypatch.setattr(Path, 'glob', lambda *_: (_ for _ in ()).throw(AssertionError('glob materializes directory')))
    discovery = SimpleNamespace(_source_cursors={})
    pages = [_source_page(discovery, tmp_path, '*.json') for _ in range(3)]
    assert all(len(page) <= 64 for page in pages)
    assert len({item for page in pages for item in page}) == 150


def test_bounded_derived_file_hash_stays_outside_sql_commit_gate(tmp_path, monkeypatch):
    from agent_py_agent.agent.local_storage import LocalStore, records
    from agent_py_agent.tests.test_local_store_derived_ttl_wave6 import CUTOFF, record

    store = LocalStore(tmp_path / 'local.db')
    item = record(store, 'expired')
    active = []
    original_transaction = records._derived_transaction
    original_hash = records._derived_file_hash
    hashed = []

    def transaction(store, operation):
        active.append(True)
        try:
            return original_transaction(store, operation)
        finally:
            active.pop()

    def file_hash(*args):
        assert not active, 'file read in SQLite/C commit gate'
        hashed.append(True)
        return original_hash(*args)

    monkeypatch.setattr(records, '_derived_transaction', transaction)
    monkeypatch.setattr(records, '_derived_file_hash', file_hash)
    for _ in range(30):
        result = store.prune_derived_records(before=CUTOFF, protected_refs=(), max_scan=2)
        if not result['partial']:
            break
    assert hashed and not result['partial'] and store.get_record(item.id) is None
