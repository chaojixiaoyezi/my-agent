# 模块用途: W6(#20, #21, #22, #24, #25) 端到端回归测试，覆盖审计游标早停、启动提交恢复、HOT 规则校验、租约重用回收与受管用户池钉扎生命周期。
from __future__ import annotations

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.managed_owner_runtime import managed_owner_pool_for_agent
from agent_py_agent.agent.memory_store import curator_audit_cursor as audit_cursor
from agent_py_agent.agent.memory_store import curator_inputs as curator_inputs_module
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.curator_commit import (
    CandidateObservation,
    CuratorBatchCommit,
    CuratorSuccessCommit,
)
from agent_py_agent.agent.memory_store.curator_inputs import _collect_audit
from agent_py_agent.agent.memory_store.curator_run_log import CuratorRunRecord
from agent_py_agent.agent.memory_store.curator_state import (
    MemoryCuratorStateStore,
    _local_hostname,
    build_process_identity,
    execution_lease_protected,
    stale_lease_reclaimable,
)
from agent_py_agent.agent.memory_store.lessons import (
    HotRuleRepository,
    LessonRecord,
    LessonRepository,
)
from agent_py_agent.agent.owner_scoped_pool import ActiveOwnerRegistry, _close_agent_mcp_clients
from agent_py_agent.agent.scheduler import monitor_pool as monitor
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.cli import gateway_loops
from agent_py_agent.tests.test_memory_curator import _m2_service
from agent_py_agent.tests.test_memory_curator_v2 import (
    _conversation,
    _service,
    _StaticStructuredBackend,
    _valid_output,
)


@pytest.fixture(autouse=True)
def _isolated_monitor_pool(monkeypatch):
    monkeypatch.setattr(monitor, "_SHARED_POOL", None, raising=False)
    yield
    monkeypatch.setattr(monitor, "_SHARED_POOL", None, raising=False)


# ---------------------------------------------------------------------------
# #20 Part 1: _read_open stops immediately when budget exhausts and preserves page cursor
# ---------------------------------------------------------------------------
def test_w6_20_audit_read_open_early_break_bounds_page_and_resumes_cursor(tmp_path, monkeypatch):
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True)
    shard = audit_dir / "2026-01-02.jsonl"
    rows = [
        {
            "event_id": f"ev-{index:04d}",
            "event_type": "tool_call",
            "created_at": "2026-01-02T00:00:00+00:00",
            "status": "ok",
            "tool_name": "read_file",
            "preview": f"preview-{index}",
        }
        for index in range(500)
    ]
    with shard.open("wb") as handle:
        for row in rows:
            handle.write((json.dumps(row) + "\n").encode("utf-8"))
        handle.write(b"{}\nbroken\n")

    original_parse = audit_cursor._parse_row
    parse_calls = 0

    def counting_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(audit_cursor, "_parse_row", counting_parse)

    # 第一页：只取 3 条，不得顺带解析后续 497 条 + 2 条尾行。
    page1, err1 = _collect_audit(audit_dir, after_event_id="", limit=3, max_chars=50_000)
    assert [item.event_id for item in page1] == ["ev-0000", "ev-0001", "ev-0002"]
    assert err1 == []
    assert parse_calls <= 3

    # 超大首行仍返回 1 条并在预算耗尽后立刻中断循环。
    parse_calls = 0
    oversized, err_oversized = _collect_audit(
        audit_dir,
        after_event_id="ev-0002",
        limit=5,
        max_chars=1,
    )
    assert [item.event_id for item in oversized] == ["ev-0003"]
    assert err_oversized == []
    assert parse_calls <= 5

    # 第二页延续游标，连续无缝推进。
    page2, err2 = _collect_audit(audit_dir, after_event_id="ev-0003", limit=3, max_chars=50_000)
    assert [item.event_id for item in page2] == ["ev-0004", "ev-0005", "ev-0006"]
    assert err2 == []

    # 翻到末尾有效行之后才到达损坏行并报告 2 条损坏错。
    tail, tail_err = _collect_audit(audit_dir, after_event_id="ev-0499", limit=5, max_chars=50_000)
    assert tail == []
    assert len(tail_err) == 2


# ---------------------------------------------------------------------------
# #20 Part 2: run_if_due skips audit collection when interval not elapsed & turns < threshold
# ---------------------------------------------------------------------------
def test_w6_20_run_if_due_defers_audit_when_not_due_and_collects_when_due(tmp_path, monkeypatch):
    service, _thread, _message = _m2_service(tmp_path)
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    # 先标记刚刚跑过且只有 1 轮对话，低于 turn_threshold=10。
    state = service.state_store.load()
    service.state_store._write_unlocked(
        replace(
            state,
            last_run_at=now.isoformat(),
            last_success_at=now.isoformat(),
            last_daily_finalize_date=now.date().isoformat(),
            pending_reasons=[],
        )
    )
    service.config = replace(service.config, interval_seconds=3600, turn_threshold=10)

    audit_calls: list[object] = []
    original_collect_audit = curator_inputs_module._collect_audit

    def tracked_collect_audit(*args, **kwargs):
        audit_calls.append(kwargs or args)
        return original_collect_audit(*args, **kwargs)

    monkeypatch.setattr(curator_inputs_module, "_collect_audit", tracked_collect_audit)

    # 未到期且轮数不够：不得扫审计分片。
    skipped = service.run_if_due(now=now + timedelta(seconds=30))
    assert skipped.status == "not_due"
    assert audit_calls == []

    # 降低 turn_threshold 使之达标：应补采审计并执行。
    service.config = replace(service.config, interval_seconds=3600, turn_threshold=1)
    ran = service.run_if_due(now=now + timedelta(seconds=60))
    assert ran.status == "succeeded"
    assert len(audit_calls) == 1


# ---------------------------------------------------------------------------
# #21: MemoryCuratorService.__init__ runs recover_incomplete and isolates failure
# ---------------------------------------------------------------------------
def test_w6_21_curator_init_recovers_pending_journal_before_foreground_write(tmp_path):
    store, thread, message = _conversation(tmp_path)
    backend = _StaticStructuredBackend(_valid_output(thread.thread_id, message.message_id, message.content))
    first = _service(tmp_path, backend, store)
    assert not (first.state_store.path.parent / "state.json.lock").exists()
    assert list(first.runs_dir.iterdir()) == []
    assert not (tmp_path / "memory" / "candidates.jsonl.lock").exists()
    acquired = first.state_store.acquire(reason="admin", config_revision="rev1", lease_seconds=60)
    assert acquired is not None
    state, lease = acquired
    commit_request = CuratorBatchCommit(
        run_record=CuratorRunRecord(
            run_id=str(lease["run_id"]),
            lease_id=str(lease["lease_id"]),
            status="succeeded",
            reason="admin",
            provider="fake",
            model="fake",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
        ),
        state_commit=CuratorSuccessCommit(
            lease_id=str(lease["lease_id"]),
            run_id=str(lease["run_id"]),
            reason="admin",
            per_thread_cursors=dict(state.per_thread_cursors),
            last_processed_audit_event_id="",
            processed_messages=0,
            processed_audit_events=0,
            candidate_count=1,
            daily_event_count=0,
            committed_at="2026-06-01T00:00:01+00:00",
        ),
        candidate_observations=(
            CandidateObservation(
                candidate_type="long_term_fact",
                content="Preference for clean rollback recovery.",
                subject_key="fact:rollback",
                scope={"scope_type": "global", "scope_key": "global"},
                origin="model_inferred",
                observed_at="2026-06-01T00:00:01+00:00",
                confidence=0.9,
            ),
        ),
    )
    changes, _daily_events, _candidates = first.committer._build_changes(commit_request)
    # 只写事务准备日志与未完成写入（模拟进程在提交尾段中断，未清除事务日志）。
    tx = first.committer._prepare_transaction(commit_request, changes)
    assert tx is not None
    candidate_file = Path(first.candidate_service.path)
    assert first.candidate_service.list() == []

    # 重新实例化 MemoryCuratorService 应在初始化阶段自动调用 recover_incomplete 并清理未完成事务。
    recovered = _service(tmp_path, backend, store)
    assert recovered.committer.recover_incomplete() == 0
    assert Path(candidate_file).parent.exists()

    # 初始化完成后，前台直接追加候选写入不得再被陈旧未决事务阻塞。
    created = recovered.candidate_service.observe(
        CandidateObservation(
            candidate_type="long_term_fact",
            content="Foreground observation after startup recovery.",
            subject_key="fact:foreground",
            scope={"scope_type": "global", "scope_key": "global"},
            origin="tool_verified",
            observed_at="2026-06-01T00:01:00+00:00",
            confidence=0.85,
        )
    )
    assert created.subject_key == "fact:foreground"


def test_w6_21_curator_init_startup_recovery_error_logs_structured_warning(tmp_path, monkeypatch, caplog):
    store, thread, message = _conversation(tmp_path)
    backend = _StaticStructuredBackend(_valid_output(thread.thread_id, message.message_id, message.content))
    from agent_py_agent.agent.memory_store import curator_commit

    def fail_recovery(self, **_kwargs):
        raise OSError("journal dir locked")

    monkeypatch.setattr(curator_commit.CuratorBatchCommitter, "recover_incomplete", fail_recovery)
    (tmp_path / "memory" / "curator" / "transactions" / "run-pending").mkdir(parents=True)
    with caplog.at_level(logging.WARNING):
        service = _service(tmp_path, backend, store)
    assert service is not None
    assert any(
        "CURATOR_STARTUP_RECOVERY_FAILED" in record.message
        and "effect_outcome=not_started" in record.message
        and "next_action=" in record.message
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# #22: HotRuleRepository archive check without archive/ subdir + scope parity check
# ---------------------------------------------------------------------------
def test_w6_22_hot_promote_rejects_missing_lesson_without_archive_dir_and_cross_scope(tmp_path):
    memory_dir = tmp_path / "memory"
    lessons_dir = memory_dir / "lessons"
    lessons_dir.mkdir(parents=True)
    # 故意不创建 lessons/archive/ 子目录，验证仅凭 lessons_dir 存在也会查核目标活跃 lesson。
    assert not (lessons_dir / "archive").exists()

    routing_path = lessons_dir / "routing.json"
    lesson_repo = LessonRepository(lessons_dir, routing_path)
    hot_repo = HotRuleRepository(memory_dir / "HOT.md")
    candidates = CandidateService(tmp_path / "candidates.jsonl")

    cand_global = candidates.observe(
        CandidateObservation(
            candidate_type="lesson",
            content="Always verify directory existence directly before archive assumption.",
            subject_key="lesson:hot-scope-check",
            scope={"scope_type": "global", "scope_key": "global"},
            origin="tool_verified",
            evidence_refs=({"kind": "message", "message_id": "m1"}, {"kind": "message", "message_id": "m2"}, {"kind": "message", "message_id": "m3"}),
            source_task_ids=("t1", "t2", "t3"),
            source_run_ids=("r1", "r2", "r3"),
            observed_at="2026-06-01T00:00:00+00:00",
            confidence=0.95,
        )
    )
    cand_global = replace(cand_global, occurrence_count=3)
    cand_approved_missing = replace(
        cand_global,
        candidate_type="hot_rule",
        promotion_target="hot",
        status="approved",
        target_entry_id="lesson-missing-01",
    )

    # 1. 目标 lesson 不在 lessons/ 活跃文件中（即便没有 archive/ 目录也必须拒绝）。
    stale_memory_lesson = LessonRecord(
        schema_version="my-agent.lesson.v1",
        lesson_id="lesson-missing-01",
        candidate_id=cand_approved_missing.candidate_id,
        subject_key=cand_approved_missing.subject_key,
        scope={"scope_type": "global", "scope_key": "global"},
        content=cand_approved_missing.content,
        occurrence_count=3,
        evidence_groups=("g1", "g2", "g3"),
        source_task_ids=("t1", "t2", "t3"),
        source_run_ids=("r1", "r2", "r3"),
        created_at="2026-06-01T00:00:00+00:00",
        updated_at="2026-06-01T00:00:00+00:00",
        path="memory/lessons/lesson-missing-01.md",
        status="active",
    )
    with pytest.raises(ValueError, match="HOT_TARGET_ARCHIVED_OR_MISSING") as err_missing:
        hot_repo.promote_with_receipt(cand_approved_missing, lesson=stale_memory_lesson)
    assert "effect_outcome=not_started" in str(err_missing.value)
    # 即使传入 lessons_dir 下绝对路径且没有 archive/ 目录也一样拦下。
    with pytest.raises(ValueError, match="HOT_TARGET_ARCHIVED_OR_MISSING"):
        hot_repo.promote_with_receipt(
            cand_approved_missing,
            lesson=replace(stale_memory_lesson, path=str(lessons_dir / "lesson-missing-01.md")),
        )

    # 2. 先正常晋升一条 global scope 活跃 lesson。
    active_lesson, promoted = lesson_repo.promote_with_receipt(
        replace(cand_global, status="approved", promotion_target="lesson"),
        min_occurrences=1,
    )
    assert promoted is True
    assert (lessons_dir / Path(active_lesson.path).name).is_file()

    # 3. 构造不同 scope 的 candidate（project scope vs global lesson）：必须被 scope 门控拦截。
    cand_approved_same = replace(
        cand_global,
        candidate_type="hot_rule",
        promotion_target="hot",
        status="approved",
        target_entry_id=active_lesson.lesson_id,
    )
    cand_other_scope = replace(
        cand_approved_same,
        candidate_id="cand-other-scope",
        scope={"scope_type": "project", "scope_key": "ws-99"},
    )
    with pytest.raises(ValueError, match="hot_candidate_scope_mismatch") as err_scope:
        hot_repo.promote_with_receipt(cand_other_scope, lesson=active_lesson)
    assert "effect_outcome=not_started" in str(err_scope.value)
    assert not hot_repo.path.exists()

    # 4. 同 scope candidate 可以正常晋升进 HOT.md。
    hot_entry, hot_created = hot_repo.promote_with_receipt(cand_approved_same, lesson=active_lesson)
    assert hot_created is True
    assert hot_entry.lesson_id == active_lesson.lesson_id


# ---------------------------------------------------------------------------
# #24: Expired executing lease with recycled PID or missing identity is reclaimed
# ---------------------------------------------------------------------------
def test_w6_24_expired_executing_lease_reclaimed_on_identity_mismatch_or_missing_and_protected_when_live(tmp_path):
    store = MemoryCuratorStateStore(tmp_path / "curator_state.json")
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    past_iso = (now - timedelta(minutes=10)).isoformat()

    # 1. PID 碰巧与当前存活进程一致，但 process_identity 启动时间不匹配（同号 PID 重用）。
    host = _local_hostname()
    live_identity = build_process_identity(os.getpid())
    reused_identity = dict(live_identity)
    reused_identity["start_time"] = "Thu Jan  1 00:00:00 1970"
    stale_reused_lease = {
        "lease_id": "lease-reused-pid",
        "run_id": "run-reused-pid",
        "host": host,
        "pid": os.getpid(),
        "phase": "executing",
        "acquired_at": (now - timedelta(minutes=20)).isoformat(),
        "expires_at": past_iso,
        "process_identity": reused_identity,
    }
    state = store.load()
    store._write_unlocked(replace(state, active_lease=stale_reused_lease))
    assert stale_lease_reclaimable(stale_reused_lease) is True
    assert execution_lease_protected(stale_reused_lease) is False
    reclaimed_reused = store.acquire(reason="admin", config_revision="rev1", lease_seconds=60, now=now)
    assert reclaimed_reused is not None
    assert reclaimed_reused[1]["lease_id"] != "lease-reused-pid"

    # 2. 已过期 executing 租约缺少 process_identity 且 pid 仍等于 getpid()：不能永久保护造成死锁。
    legacy_expired_lease = {
        "lease_id": "lease-legacy-expired",
        "run_id": "run-legacy-expired",
        "host": host,
        "pid": os.getpid(),
        "phase": "executing",
        "acquired_at": (now - timedelta(minutes=20)).isoformat(),
        "expires_at": past_iso,
    }
    store._write_unlocked(replace(store.load(), active_lease=legacy_expired_lease))
    assert execution_lease_protected(legacy_expired_lease) is False
    reclaimed_legacy = store.acquire(reason="admin", config_revision="rev1", lease_seconds=60, now=now)
    assert reclaimed_legacy is not None
    assert reclaimed_legacy[1]["lease_id"] != "lease-legacy-expired"

    # 3. 同一真实存活进程且 process_identity 完全匹配时，过期 executing 仍受保护不被误抢。
    genuine_live_lease = {
        "lease_id": "lease-genuine-live",
        "run_id": "run-genuine-live",
        "host": host,
        "pid": os.getpid(),
        "phase": "executing",
        "acquired_at": (now - timedelta(minutes=20)).isoformat(),
        "expires_at": past_iso,
        "process_identity": live_identity,
    }
    store._write_unlocked(replace(store.load(), active_lease=genuine_live_lease))
    assert stale_lease_reclaimable(genuine_live_lease) is False
    assert execution_lease_protected(genuine_live_lease) is True
    assert store.acquire(reason="admin", config_revision="rev1", lease_seconds=60, now=now) is None


# ---------------------------------------------------------------------------
# #25: ManagedOwnerPool routing + ExitStack pin protection in watch/curator/orphan loops
# ---------------------------------------------------------------------------
def test_w6_25_managed_owner_watch_recovery_curator_pinning_and_orphan_reconcile(tmp_path, monkeypatch):
    root = tmp_path / "home"
    LocalUserLifecycle(LocalUserStore(root)).create("alice", idempotency_key="w6_25_managed_alice_001")
    alice = OwnerIdentity.local_user("alice")
    bob = OwnerIdentity.provider_user("feishu", "bob")

    with ExitStack() as cleanup:
        base_agent = SimpleAgent(
            AgentConfig(
                model_backend="echo",
                enable_tools=False,
                memory_path="memory.jsonl",
                gateway_per_user_owner_scoping=True,
                my_agent_home=str(root),
                memory_curator_enabled=True,
                owner_agent_idle_seconds=0.01,
            ),
            tmp_path / "workspace",
            defer_mcp_connect=True,
        )
        cleanup.callback(_close_agent_mcp_clients, base_agent)
        managed = managed_owner_pool_for_agent(base_agent)
        assert managed.handles(alice) is True
        assert managed.handles(bob) is False

        context = SimpleNamespace(agent=base_agent, config_path=tmp_path / "agent_config.yaml")
        monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _ctx: base_agent)
        monkeypatch.setattr(gateway_loops, "managed_owner_pool_for_agent", lambda _agent: managed, raising=False)

        # ---- 1) _recover_active_watch_harvesters 覆盖受管 hard_agents 并持 pin ----
        with managed.acquire_pinned(alice, hard=True) as hard_alice:
            pass
        assert managed.gauges()["pinned"] == 0
        assert any(item is hard_alice for item in managed.hard_agents())

        supervisor = gateway_loops._BackgroundMainSupervisor(context)
        cleanup.callback(supervisor.shutdown)
        supervisor._managed_owner_pool = managed

        watch_pins_seen: list[tuple[str, int]] = []

        def fake_recover_harvesters(agent):
            if agent is hard_alice:
                watch_pins_seen.append(("alice", managed.gauges()["pinned"]))
                assert managed.release_idle(0, busy=lambda _a: False) == []
            return 1

        monkeypatch.setattr(gateway_loops, "recover_active_audit_harvesters", fake_recover_harvesters)
        recovered_count = supervisor._recover_active_watch_harvesters(now=100.0)
        assert recovered_count >= 2
        assert watch_pins_seen == [("alice", 1)]
        assert managed.gauges()["pinned"] == 0

        # 清空缓存，模拟 alice 为冷软 owner。
        managed.release_idle(0, busy=lambda _a: False)
        assert managed.gauges()["cached"] == 0

        # ---- 2) curator soft owner 经 managed_pool 物化、执行期持 pin 防 release_idle 逐出，结束后卸 pin 逐出 ----
        cold_calls: list[object] = []
        supervisor._registry.record(alice, hard=False)
        monkeypatch.setattr(gateway_loops, "_cold_curator_precheck", lambda _sup, owner: ("due", True))
        monkeypatch.setattr(gateway_loops, "_curator_run_allowed", lambda _sup, agent: True)

        started = threading.Event()
        release_worker = threading.Event()
        executor = ThreadPoolExecutor(max_workers=1)
        cleanup.callback(executor.shutdown, wait=True)
        supervisor._curator_executor = executor

        def blocking_curator(agent, owner_id):
            cold_calls.append((agent, owner_id))
            started.set()
            assert release_worker.wait(timeout=10)
            return None

        monkeypatch.setattr(supervisor, "_safe_run_curator", blocking_curator)
        candidates = gateway_loops._curator_candidates(supervisor, pool=None)
        assert ("owner", alice) in candidates

        soft_ids: set[object] = set()
        gateway_loops._submit_due_curators(
            supervisor,
            [("owner", alice)],
            available=1,
            soft_agent_ids=soft_ids,
            pool=None,
        )
        assert started.wait(timeout=10)
        # 在途期间必须持有 pin：主动触发 release_idle 也绝不能把正在执行的 alice 逐出。
        assert managed.gauges()["pinned"] >= 1
        assert managed.release_idle(0, busy=lambda _a: False) == []

        release_worker.set()
        for future in list(supervisor._curator_inflight.values()):
            future.result(timeout=10)

        gateway_loops._retire_finished_curators(supervisor, soft_ids, pool=None)
        assert supervisor._curator_inflight == {}
        assert supervisor._curator_pin_stacks == {}
        assert managed.gauges()["pinned"] == 0
        # 软代理在 retire 后已被逐出或可立即清空。
        managed.release_idle(0, busy=lambda _a: False)
        assert managed.gauges()["cached"] == 0

        # ---- 2b) submit 失败与准入拒绝都必须先卸 pin 再逐出软代理 ----
        monkeypatch.setattr(gateway_loops, "_curator_run_allowed", lambda _sup, _a: False)
        monkeypatch.setattr(gateway_loops, "_curator_maintenance_due", lambda _a: False)
        monkeypatch.setattr(gateway_loops, "_skill_learning_pending", lambda _a: False)
        rejected_agent, is_soft = gateway_loops._resolve_curator_candidate("owner", alice, pool=None, supervisor=supervisor)
        assert rejected_agent is not None and is_soft is True
        assert managed.gauges()["pinned"] == 1
        assert gateway_loops._curator_candidate_is_admitted(supervisor, rejected_agent, is_soft=True, pool=None) is False
        assert managed.gauges()["pinned"] == 0
        assert supervisor._curator_pin_stacks == {}
        managed.release_idle(0, busy=lambda _a: False)

        # ---- 3) _GatewayOrphanReconciler._sweep_owner 和 tick 走 managed_pool 并持 pin ----
        reconciler = gateway_loops._GatewayOrphanReconciler(context)
        reconciler._managed_owner_pool = managed
        # 使用全新登记表确保冷状态下仅存在未实例化软 owner 时 tick 不会主动构造它。
        reconciler._registry = ActiveOwnerRegistry()
        reconciler._registry.record(alice, hard=False)
        sweep_pins: list[tuple[str, int]] = []

        def fake_sweep(agent, label):
            if label != "base":
                sweep_pins.append((label, managed.gauges()["pinned"]))
                assert managed.release_idle(0, busy=lambda _a: False) == []
            return {"owner": label, "state": "ok"}

        monkeypatch.setattr(reconciler, "_sweep", fake_sweep)
        monkeypatch.setattr(reconciler, "_ensure_owner_pool", lambda: None)
        monkeypatch.setattr(reconciler, "_seed_owner_registry", lambda: None)

        # 只有 soft owner（无 hard_snapshot）：tick 不得物化 alice。
        reconciler.tick()
        assert sweep_pins == []
        assert managed.gauges()["cached"] == 0

        # 记录为 hard owner 后：_sweep_owner 与 tick 经 managed_pool 巡检并持 pin。
        reconciler._registry.record(alice, hard=True)
        report = reconciler._sweep_owner(alice, "local/user/alice")
        assert report == {"owner": "local/user/alice", "state": "ok"}
        assert managed.gauges()["pinned"] == 0
        reports = reconciler.tick()
        assert any(r.get("owner") == "local/user/alice" for r in reports)
        assert len(sweep_pins) == 2 and all(pins >= 1 for _, pins in sweep_pins)
        assert managed.gauges()["pinned"] == 0

        # 测试期 executor future 提交异常时的 pin 清理路径。
        class _FailingExecutor:
            def submit(self, *_args, **_kwargs):
                raise RuntimeError("executor saturated")

            def shutdown(self, **_kwargs):
                return None

        monkeypatch.setattr(gateway_loops, "_curator_run_allowed", lambda _sup, _a: True)
        supervisor._curator_executor = _FailingExecutor()
        supervisor._curator_cold_builds = 0
        resolved, is_soft2 = gateway_loops._resolve_curator_candidate("owner", alice, pool=None, supervisor=supervisor)
        assert resolved is not None and is_soft2 is True
        with pytest.raises(RuntimeError, match="executor saturated"):
            gateway_loops._submit_curator_candidate(
                supervisor,
                resolved,
                is_soft=True,
                soft_agent_ids=soft_ids,
                pool=None,
            )
        assert managed.gauges()["pinned"] == 0
        assert supervisor._curator_pin_stacks == {}
        managed.release_idle(0, busy=lambda _a: False)
        assert managed.gauges() == {"building": 0, "pinned": 0, "cached": 0}
