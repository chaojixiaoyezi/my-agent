"""NW2 W8 regressions: supervision per-row canonical isolation and locator hygiene scale bounds."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.dispatch import capability_auto_sweep
from agent_py_agent.agent.common.audit_activation import (
    AUDIT_ATTR,
    AUDIT_SOURCE_ID_ATTR,
    AUDIT_SOURCE_OWNER_HOME_ATTR,
    AUDIT_SOURCE_WATCH_ID_ATTR,
    AUDIT_SOURCE_WORKER_ATTR,
    AUDIT_SOURCE_WORKER_KEY_ATTR,
    audit_source_worker_key,
)
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.authority import CONVERSATION_REQUEST_ID_ATTR
from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionPolicy
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.subagents import manager as managers
from agent_py_agent.agent.subagents.manager import SubAgentManager, SubagentRunIndexUnavailable
from agent_py_agent.agent.subagents.services import runtime_closeout
from agent_py_agent.agent.subagents.services.persistence import locator_hygiene as hygiene
from agent_py_agent.agent.subagents.services.persistence.service import SubAgentListRunsReport
from agent_py_agent.agent.subagents.services.runner_result_service import RecordRunnerResultParams
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.storage_lanes import (
    StorageLaneBudget,
    StorageLaneRequest,
    validate_lane_receipt,
)


def _sweep_agent(tmp_path: Path, manager: SubAgentManager, store: ConversationStore | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        config=AgentConfig(),
        tools=SimpleNamespace(workspace_root=tmp_path),
        root=tmp_path,
        subagents=manager,
        conversation_store=store,
    )


def _home_fixture(tmp_path: Path, name: str = "isolated"):
    home = home_paths(tmp_path / name)
    home.owner_home_dir.mkdir(parents=True)
    home.owner_retention_json.write_text(
        json.dumps(MemoryRetentionPolicy().to_dict()),
        encoding="utf-8",
    )
    ConversationStore(home.owner_home_dir / "conversations")
    return home, time.time()


def _write_projection(
    owner_home: Path,
    base_tmp: Path,
    now: float,
    *,
    run_id: str,
    status: str = "DONE",
    days: float = 40.0,
    dangling: bool = False,
    scratch: bool = True,
    compact_stub: bool = False,
) -> Path:
    run_dir = owner_home / "agents" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace = base_tmp / f"ws-{owner_home.parent.name}-{run_id}"
    if not dangling:
        workspace.mkdir(parents=True, exist_ok=True)
    state_doc = {
        "run_id": run_id,
        "parent_id": "parent",
        "root_id": f"root-{run_id}",
        "task_id": f"task-{run_id}",
        "status": status,
        "created_at": now - 45 * 86400,
        "updated_at": now - days * 86400,
        "result_summary": "done",
        "error_summary": "",
        "usage_summary": {"tokens": 12},
        "deliverable_paths": [str(workspace / "output.md")],
    }
    (run_dir / "state.json").write_text(json.dumps(state_doc), encoding="utf-8")
    (run_dir / "workspace_locator.json").write_text(
        json.dumps({"workspace_root": str(workspace)}),
        encoding="utf-8",
    )
    if scratch:
        (run_dir / "events.jsonl").write_bytes(b'{"event":1}\n')
        (run_dir / "tmp").mkdir(exist_ok=True)
        (run_dir / "tmp" / "scratch").write_bytes(b"scratch")
    if compact_stub:
        (run_dir / "subagent_compact.json").write_text(
            json.dumps({"run_id": run_id, "status": status}),
            encoding="utf-8",
        )
    target_mtime = now - days * 86400
    for path in sorted(run_dir.rglob("*"), reverse=True) + [run_dir]:
        os.utime(path, (target_mtime, target_mtime))
    return run_dir


def _lane_request(
    home,
    now: float,
    *,
    cursor: dict | None = None,
    max_items: int = 5,
    deadline: float | None = None,
) -> StorageLaneRequest:
    return StorageLaneRequest(
        kind="subagents",
        home=home,
        now=now,
        cursor=dict(cursor or {}),
        budget=StorageLaneBudget(max_items=max_items, max_bytes=None, deadline=deadline),
        legal_hold=False,
        coordinator=True,
        pressure=None,
        commit=lambda operation: operation(),
        config=SimpleNamespace(retention_scan_slice_ms=500),
    )


def _source_attrs(owner_home: Path, *, audit_id: str, watch_id: str, worker_pid: int, heartbeat_at: float, run_id: str) -> dict[str, object]:
    return {
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: audit_id,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: f"src-{watch_id}",
        AUDIT_SOURCE_WATCH_ID_ATTR: watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(audit_id, watch_id),
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
        "runner_session": {
            "schema_version": "runner_session_pool.v1",
            "session_id": f"sess-{run_id}",
            "run_id": run_id,
            "worker_pid": worker_pid,
            "in_process": False,
            "status": "running",
            "heartbeat_at": heartbeat_at,
            "interval_seconds": 5.0,
            "started_at": heartbeat_at - 10.0,
            "ended_at": 0.0,
        },
    }


def test_a02_supervision_isolates_corrupt_canonical_row_and_recovers_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ConversationStore(tmp_path / "conversations")
    manager = SubAgentManager(
        tmp_path / "subagents",
        owner_id="test/nw2-w8-a02",
        owner_home_dir=str(tmp_path / "owner"),
        conversation_store=store,
    )
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "owner-parent",
            "channel": "tui",
            "channel_conversation_id": "nw2-w8-session",
            "channel_user_id": "owner-parent",
        }
    )

    corrupt_task = manager.create_run(goal="corrupt sibling", thought="", plan=["run"], depth=1)
    corrupt_task.status = "PENDING"
    manager.save(corrupt_task)

    orphan_task = manager.create_run(goal="healthy orphan", thought="", plan=["run"], depth=1)
    orphan_task.status = "PENDING"
    manager.save(orphan_task)

    closeout_task = manager.create_run(goal="healthy closeout", thought="", plan=["finish"], depth=1)
    prepared = manager.lifecycle.prepare_runner_attempt(closeout_task.id)
    attempt_id = prepared.runner_active_attempt_id
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": closeout_task.id,
            "goal": "healthy closeout",
            "status": "active",
        }
    )
    loaded_closeout = manager.load(closeout_task.id)
    loaded_closeout.attributes = {
        **dict(loaded_closeout.attributes or {}),
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": closeout_task.id,
    }
    manager.save(loaded_closeout)

    def _fail_settle(**_kwargs):
        raise OSError("temporary closeout write error")

    with monkeypatch.context() as err_ctx:
        err_ctx.setattr(manager.runtime_db, "settle_agent_run", _fail_settle)
        manager.runner_result.record_runner_result(
            RecordRunnerResultParams(
                run_id=closeout_task.id,
                dry_run=False,
                ok=True,
                message="finished",
                attempt_id=attempt_id,
                status="DONE",
                turn_end_reason="completed",
                failure_type="",
            )
        )
    assert runtime_closeout.pending_closeout(manager.load(closeout_task.id)) is not None

    # Corrupt canonical task.json AFTER SQLite run_index has indexed all three runs.
    monkeypatch.setattr(manager.persistence, "_start_run_index_maintenance", lambda: None)
    (manager.workspace / corrupt_task.id / "task.json").write_text("{broken", encoding="utf-8")

    # Existing manager.py helpers remain strictly fail-closed on canonical errors.
    with pytest.raises(SubagentRunIndexUnavailable):
        managers.select_recovery_runs(manager)
    with pytest.raises(SubagentRunIndexUnavailable):
        managers.select_current_runs(manager)

    revived_calls: list[list[str]] = []
    from agent_py_agent.agent.agent_core.orchestration.background import dispatch as bg_dispatch
    from agent_py_agent.agent.ingestion import source_worker

    monkeypatch.setattr(
        bg_dispatch,
        "auto_start_tasks",
        lambda _agent, tasks, _params: revived_calls.append([str(t.id) for t in tasks])
        or {"status": "started", "run_ids": [str(t.id) for t in tasks]},
    )
    monkeypatch.setattr(source_worker, "reconcile_audit_source_workers", lambda _agent: {})

    summary = capability_auto_sweep.supervise_stalled_orphans(_sweep_agent(tmp_path, manager, store))
    assert summary["orphans_revived"] == 1
    assert revived_calls == [[orphan_task.id]]
    assert summary["runtime_closeouts_recovered"] == 1
    assert runtime_closeout.pending_closeout(manager.load(closeout_task.id)) is None
    assert int(summary["canonical_row_load_errors"]) >= 1
    assert corrupt_task.id in list(summary.get("canonical_row_load_error_ids") or [])


def test_a02_reclaim_vetoes_shared_host_pid_when_canonical_row_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ConversationStore(tmp_path / "conversations")
    manager = SubAgentManager(
        tmp_path / "subagents",
        owner_id="test/nw2-w8-host-veto",
        owner_home_dir=str(tmp_path / "owner"),
        conversation_store=store,
    )
    monkeypatch.setattr(manager.persistence, "_start_run_index_maintenance", lambda: None)
    shared_pid = 424242
    old_hb = time.time() - 3600.0
    t_healthy = manager.create_run(goal="stalled healthy", thought="", plan=["run"], depth=1)
    t_healthy.status = "RUNNING"
    t_healthy.heartbeat_at = old_hb
    t_healthy.updated_at = old_hb
    t_healthy.attributes = _source_attrs(
        tmp_path / "owner",
        audit_id="audit-1",
        watch_id="w-1",
        worker_pid=shared_pid,
        heartbeat_at=old_hb,
        run_id=t_healthy.id,
    )
    manager.save(t_healthy)

    t_corrupt = manager.create_run(goal="corrupt sibling", thought="", plan=["run"], depth=1)
    t_corrupt.status = "RUNNING"
    t_corrupt.heartbeat_at = time.time() + 1000.0
    t_corrupt.updated_at = time.time() + 1000.0
    t_corrupt.attributes = _source_attrs(
        tmp_path / "owner",
        audit_id="audit-2",
        watch_id="w-2",
        worker_pid=shared_pid,
        heartbeat_at=time.time() + 1000.0,
        run_id=t_corrupt.id,
    )
    manager.save(t_corrupt)

    captured_host_pids: list[set[int]] = []
    monkeypatch.setattr(
        "agent_py_agent.agent.subagents.process_control.is_pid_alive",
        lambda pid: pid == shared_pid,
    )
    monkeypatch.setattr(
        capability_auto_sweep,
        "_terminate_fully_stalled_source_hosts",
        lambda pids: captured_host_pids.append(set(pids)) or {},
    )
    monkeypatch.setattr(
        capability_auto_sweep,
        "conversation_lifecycle_decisions",
        lambda _agent, _tasks: {},
    )

    # First verify corrupt canonical sibling vetoes host termination.
    (manager.workspace / t_corrupt.id / "task.json").write_text("{broken", encoding="utf-8")
    agent = _sweep_agent(tmp_path, manager, store)
    capability_auto_sweep._reclaim_dead_running_runs(agent)
    assert captured_host_pids == [set()]

    # Verify manager.py default select remains fail-closed on the same canonical damage.
    with pytest.raises(SubagentRunIndexUnavailable):
        managers.select_current_runs(manager, statuses=("RUNNING",))


@pytest.mark.parametrize("damage", ["missing", "corrupt", "generation"])
def test_a02_supervision_stays_fail_closed_on_sqlite_index_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    manager = SubAgentManager(
        tmp_path / "subagents",
        owner_id="test/nw2-w8-index-fail",
        owner_home_dir=str(tmp_path / "owner"),
    )
    task = manager.create_run(goal="indexed task", thought="", plan=["run"], depth=1)
    task.status = "RUNNING"
    manager.save(task)

    service, index = manager.persistence, manager.persistence.run_index
    monkeypatch.setattr(service, "_start_run_index_maintenance", lambda: None)
    if damage == "missing":
        index.path.unlink()
    elif damage == "corrupt":
        index.path.write_bytes(b"not a sqlite database")
    else:
        with index.connect() as db:
            db.execute("UPDATE meta SET value=2 WHERE key='schema'")

    agent = _sweep_agent(tmp_path, manager)
    with pytest.raises(SubagentRunIndexUnavailable):
        capability_auto_sweep.auto_start_stalled_orphans(agent)
    with pytest.raises(SubagentRunIndexUnavailable):
        capability_auto_sweep._reclaim_dead_running_runs(agent)
    with pytest.raises(SubagentRunIndexUnavailable):
        capability_auto_sweep._reconcile_direct_parent_waits(agent)
    with pytest.raises(SubagentRunIndexUnavailable):
        capability_auto_sweep._reconcile_conversation_parent_lifecycle(agent)
    with pytest.raises(SubagentRunIndexUnavailable):
        capability_auto_sweep.supervise_stalled_orphans(agent)


def test_a02_current_index_row_mismatch_isolated_while_missing_run_id_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    healthy = manager.create_run(goal="healthy", thought="", plan=["run"], depth=1)
    healthy.status = "PENDING"
    manager.save(healthy)

    row_error = {
        "context": "subagents.current_index",
        "error_type": "ValueError",
        "message": "status mismatch",
        "run_id": "run-mismatched",
    }
    monkeypatch.setattr(
        manager,
        "list_current_runs_report",
        lambda **_kw: SubAgentListRunsReport(runs=[healthy], load_errors=[row_error]),
    )
    errors_out: list[dict[str, object]] = []
    selected = capability_auto_sweep._select_supervision_current_runs(
        manager, statuses=("PENDING",), errors_out=errors_out
    )
    assert [task.id for task in selected] == [healthy.id]
    assert errors_out == [row_error]

    index_error = {
        "context": "subagents.current_index",
        "error_type": "OperationalError",
        "message": "index unreadable",
    }
    monkeypatch.setattr(
        manager,
        "list_current_runs_report",
        lambda **_kw: SubAgentListRunsReport(runs=[], load_errors=[index_error]),
    )
    with pytest.raises(SubagentRunIndexUnavailable):
        capability_auto_sweep._select_supervision_current_runs(manager, statuses=("PENDING",))


def test_a03_maintain_subagents_lane_persists_cursor_beyond_32_owners(tmp_path: Path) -> None:
    # Single owner with 40 archived projections drained across small 5-item slices.
    single_home, now = _home_fixture(tmp_path, "single-40")
    for idx in range(40):
        _write_projection(
            single_home.owner_home_dir,
            tmp_path,
            now,
            run_id=f"run-{idx:03d}",
            status="DONE",
            days=40.0,
        )

    cursors_seen: set[str] = set()
    cursor: dict = {}
    slices = 0
    while True:
        slices += 1
        assert slices <= 80
        receipt = validate_lane_receipt(
            hygiene.maintain_subagents_lane(_lane_request(single_home, now, cursor=cursor, max_items=5))
        )
        assert receipt["items"] <= 5
        if receipt["complete"]:
            assert receipt["cursor"] == {"cycle_generation": 1}
            break
        position = receipt["cursor"].get("directory")
        if receipt["cursor"].get("phase") == "projections" and position:
            cursors_seen.add(json.dumps(position, sort_keys=True))
        cursor = receipt["cursor"]

    remaining = [p for p in (single_home.owner_home_dir / "agents").iterdir() if p.is_dir()]
    assert remaining == []
    assert len(cursors_seen) >= 7

    # 35 independent owners (>32 in-memory _SCANS limit) interleaved across ticks.
    owners = []
    for owner_idx in range(35):
        h, owner_now = _home_fixture(tmp_path, f"owner-{owner_idx:02d}")
        for run_idx in range(6):
            _write_projection(
                h.owner_home_dir,
                tmp_path,
                owner_now,
                run_id=f"p-{run_idx:02d}",
                status="DONE",
                days=40.0,
            )
        owners.append([h, owner_now, {}, set(), False])

    for _round_idx in range(25):
        if all(item[4] for item in owners):
            break
        for item in owners:
            h, owner_now, cur, seen_positions, done = item
            if done:
                continue
            receipt = validate_lane_receipt(
                hygiene.maintain_subagents_lane(_lane_request(h, owner_now, cursor=cur, max_items=2))
            )
            if receipt["complete"]:
                assert receipt["cursor"] == {"cycle_generation": 1}
                item[4] = True
                item[2] = receipt["cursor"]
            else:
                assert receipt["items"] <= 2
                position = receipt["cursor"].get("directory")
                if receipt["cursor"].get("phase") == "projections" and position:
                    seen_positions.add(json.dumps(position, sort_keys=True))
                item[2] = receipt["cursor"]

    assert all(item[4] for item in owners)
    for h, _owner_now, _cur, seen_positions, _done in owners:
        assert len(seen_positions) >= 2
        assert [p for p in (h.owner_home_dir / "agents").iterdir() if p.is_dir()] == []


def test_a03_budget_exhausted_mid_page_and_at_zero_preserves_cursor(tmp_path: Path) -> None:
    home, now = _home_fixture(tmp_path, "budget-midpage")
    for idx in range(6):
        _write_projection(
            home.owner_home_dir,
            tmp_path,
            now,
            run_id=f"r-{idx:02d}",
            status="DONE",
            days=40.0,
        )

    checks = 0

    def _stop_after_two() -> bool:
        nonlocal checks
        checks += 1
        return checks > 2

    first = hygiene.maintain_owner_agent_projections(
        home.owner_home_dir,
        now=now,
        max_dirs=10,
        dry_run=True,
        directory_position={},
        budget_exhausted=_stop_after_two,
    )
    assert first["scanned_dirs"] == 2
    assert first["would_archive_dirs"] == 2
    assert first["page_complete"] is False
    assert first["visited_entries"] == 2 and first["directory"]

    # 零预算须保留原目录续点，不能因本片无候选而完成。
    zero_step = hygiene.maintain_owner_agent_projections(
        home.owner_home_dir,
        now=now,
        max_dirs=10,
        dry_run=True,
        directory_position=first["directory"],
        budget_exhausted=lambda: True,
    )
    assert zero_step["scanned_dirs"] == 0
    assert zero_step["page_complete"] is False
    assert zero_step["directory"] == first["directory"]

    rest = hygiene.maintain_owner_agent_projections(
        home.owner_home_dir,
        now=now,
        max_dirs=10,
        dry_run=True,
        directory_position=zero_step["directory"],
        budget_exhausted=lambda: False,
    )
    assert rest["scanned_dirs"] == 4
    assert rest["would_archive_dirs"] == 4
    assert rest["page_complete"] is True
    assert rest["next_after"] == ""


def test_a03_classify_skips_runtime_and_wake_for_ineligible_and_caches_wake_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, now = _home_fixture(tmp_path, "cheap-gate")
    queue = home.owner_home_dir / "conversations/wake_queue/normal"
    queue.mkdir(parents=True, exist_ok=True)
    (queue / "wake-arch.json").write_text(
        json.dumps({"status": "pending", "source": {"run_id": "done-old-wake"}}),
        encoding="utf-8",
    )

    running_young = _write_projection(home.owner_home_dir, tmp_path, now, run_id="run-young", status="RUNNING", days=0.01)
    running_old_ws = _write_projection(home.owner_home_dir, tmp_path, now, run_id="run-old-ws", status="RUNNING", days=40.0)
    done_young = _write_projection(home.owner_home_dir, tmp_path, now, run_id="done-young", status="DONE", days=2.0)
    done_stub = _write_projection(
        home.owner_home_dir, tmp_path, now, run_id="done-stub", status="DONE", days=10.0, scratch=False, compact_stub=True
    )

    runtime_calls: list[str] = []
    wake_calls: list[str] = []
    collect_calls: list[Path] = []
    orig_runtime = hygiene.projection_runtime_facts
    orig_wake = hygiene._has_parent_wake
    orig_collect = hygiene._collect_active_wake_ids

    monkeypatch.setattr(
        hygiene,
        "projection_runtime_facts",
        lambda oh, rid, st: runtime_calls.append(rid) or orig_runtime(oh, rid, st),
    )
    monkeypatch.setattr(
        hygiene,
        "_has_parent_wake",
        lambda oh, rid, st: wake_calls.append(rid) or orig_wake(oh, rid, st),
    )
    monkeypatch.setattr(
        hygiene,
        "_collect_active_wake_ids",
        lambda oh: collect_calls.append(oh) or orig_collect(oh),
    )

    assert hygiene.classify_agent_projection_dir(home.owner_home_dir, running_young, now=now)["classification"] == "active_or_protected"
    assert hygiene.classify_agent_projection_dir(home.owner_home_dir, running_old_ws, now=now)["classification"] == "active_or_protected"
    assert hygiene.classify_agent_projection_dir(home.owner_home_dir, done_young, now=now)["classification"] == "active_or_protected"
    assert hygiene.classify_agent_projection_dir(home.owner_home_dir, done_stub, now=now)["classification"] == "terminal_compact_stub_only"

    dry = hygiene.maintain_owner_agent_projections(home.owner_home_dir, now=now, dry_run=True, after="")
    assert dry["scanned_dirs"] == 4
    assert dry["protected_dirs"] == 4
    assert runtime_calls == []
    assert wake_calls == []
    assert collect_calls == []

    # Now add 1 dangling orphan + 2 old DONE archive candidates (one protected by wake).
    _write_projection(home.owner_home_dir, tmp_path, now, run_id="orphan-missing", status="PLANNING", days=2.0, dangling=True)
    _write_projection(home.owner_home_dir, tmp_path, now, run_id="done-old-wake", status="DONE", days=40.0)
    _write_projection(home.owner_home_dir, tmp_path, now, run_id="done-old-free", status="DONE", days=40.0)

    dry2 = hygiene.maintain_owner_agent_projections(home.owner_home_dir, now=now, dry_run=True, after="")
    assert dry2["would_remove_dirs"] == 1
    assert dry2["would_archive_dirs"] == 1
    assert sorted(runtime_calls) == ["done-old-free", "done-old-wake", "orphan-missing"]
    assert len(collect_calls) == 1
    assert wake_calls == []


def test_a03_wake_index_explicit_fail_closed_and_live_recheck_before_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, now = _home_fixture(tmp_path, "wake-fail-closed")
    _write_projection(home.owner_home_dir, tmp_path, now, run_id="old-run-1", status="DONE", days=40.0)
    _write_projection(home.owner_home_dir, tmp_path, now, run_id="old-run-2", status="DONE", days=40.0)

    # Explicit wake_ref_ids protects old-run-1 without scanning wake_queue.
    monkeypatch.setattr(hygiene, "_collect_active_wake_ids", lambda _oh: pytest.fail("should not scan wake_queue"))
    report = hygiene.maintain_owner_agent_projections(
        home.owner_home_dir,
        now=now,
        dry_run=True,
        wake_ref_ids={"old-run-1"},
    )
    assert report["protected_dirs"] == 1
    assert report["would_archive_dirs"] == 1

    # Symlinked wake_queue folder causes _collect_active_wake_ids to fail closed (None) and protect all.
    monkeypatch.undo()
    target = tmp_path / "external-queue"
    target.mkdir()
    wake_normal = home.owner_home_dir / "conversations/wake_queue/normal"
    if wake_normal.is_dir() and not wake_normal.is_symlink():
        wake_normal.rmdir()
    else:
        wake_normal.parent.mkdir(parents=True, exist_ok=True)
    wake_normal.symlink_to(target)

    collect_calls: list[Path] = []
    orig_collect = hygiene._collect_active_wake_ids
    monkeypatch.setattr(
        hygiene,
        "_collect_active_wake_ids",
        lambda oh: collect_calls.append(oh) or orig_collect(oh),
    )
    protected_report = hygiene.maintain_owner_agent_projections(home.owner_home_dir, now=now, dry_run=True, after="")
    assert protected_report["protected_dirs"] == 2
    assert protected_report["would_archive_dirs"] == 0
    assert len(collect_calls) == 1

    # Exceeding >5000 wake files also fails closed to None.
    wake_normal.unlink()
    wake_normal.mkdir(parents=True)
    for i in range(5001):
        (wake_normal / f"w-{i}.json").write_text("{}", encoding="utf-8")
    assert hygiene._collect_active_wake_ids(home.owner_home_dir) is None

    # Late wake created during archive staging is still rejected by live delete recheck.
    for i in range(5001):
        (wake_normal / f"w-{i}.json").unlink()
    late_home, late_now = _home_fixture(tmp_path, "late-wake")
    late_dir = _write_projection(late_home.owner_home_dir, tmp_path, late_now, run_id="late-run", status="DONE", days=40.0)
    orig_archive = hygiene._archive_entry

    def _inject_late_wake(archive_path: Path, compact: dict) -> None:
        orig_archive(archive_path, compact)
        late_queue = late_home.owner_home_dir / "conversations/wake_queue/normal"
        late_queue.mkdir(parents=True, exist_ok=True)
        (late_queue / "late.json").write_text(json.dumps({"source_agent_id": "late-run"}), encoding="utf-8")

    monkeypatch.setattr(hygiene, "_archive_entry", _inject_late_wake)
    late_report = hygiene.maintain_owner_agent_projections(late_home.owner_home_dir, now=late_now, after="")
    assert late_dir.exists()
    assert late_report["archived_dirs"] == 0
    assert late_report["errors"]


def test_a03_agent_projection_page_unchanged_for_owner_wake_discovery(tmp_path: Path) -> None:
    home, now = _home_fixture(tmp_path, "legacy-page")
    for idx in range(5):
        _write_projection(home.owner_home_dir, tmp_path, now, run_id=f"run-{idx}", status="DONE", days=40.0)

    seen_counts: list[int] = []
    completes: list[bool] = []
    positives: list[bool] = []
    for _ in range(3):
        page, complete, positive = hygiene.agent_projection_page(
            home.owner_home_dir,
            2,
            purpose="wake",
            observe=lambda path: path.name == "run-2",
        )
        seen_counts.append(len(page))
        completes.append(complete)
        positives.append(positive)
    assert seen_counts == [2, 2, 1]
    assert completes == [False, False, True]
    assert any(positives)
