"""用真实运行索引和规范文件约束恢复集合的读取量及错误边界。"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.dispatch import capability_auto_sweep as sweep
from agent_py_agent.agent.conversation.background_context import (
    BackgroundContextLoad,
    _task_context_ids,
)
from agent_py_agent.agent.subagents import manager as managers
from agent_py_agent.agent.subagents.services import runtime_closeout
from agent_py_agent.tests.test_audit_source_worker import _agent, _bind_audit_workspace, _watch
from agent_py_agent.tests.test_closeout_recovery_dependencies import _pending_task
from agent_py_agent.tests.test_subagent_listruns_cache import _selected_manager, _selected_task_file


@pytest.mark.parametrize("history", [100, 1000, 10000])
def test_current_reads_ignore_terminal_history(tmp_path, monkeypatch, record_property, history):
    manager = _selected_manager(tmp_path)
    index = manager.persistence.run_index
    with index.connect() as db:
        for number in range(history + 7):
            status = "RUNNING" if number < 7 else ("DONE", "FAILED", "BLOCKED", "TAKEN_OVER")[number % 4]
            _selected_task_file(manager, str(number), status)
            index.upsert(manager.load(str(number)), connection=db)
    calls, original = [], manager.persistence._cached_run_ref

    def observed(run_id, path):
        calls.append(run_id)
        return original(run_id, path)

    stats, original_stat = [], Path.stat

    def observed_stat(path, *args, **kwargs):
        if path.name in {"task.json", "canonical_state.json"}:
            stats.append(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(manager.persistence, "_cached_run_ref", observed)
    monkeypatch.setattr(Path, "stat", observed_stat)
    reader = getattr(manager, "list_recovery_runs_report", lambda: manager.list_runs_selected_report(lambda row: row.status == "RUNNING"))
    report = reader()
    assert not report.load_errors and {row.id for row in report.runs} == {str(i) for i in range(7)}
    assert len(calls) == 7
    assert len(manager.persistence._run_cache) <= manager.persistence._MAX_RUN_CACHE_ENTRIES
    assert len(stats) <= 7 * 6 and {path.parent.name for path in stats} == set(calls)
    record_property("canonical_stats", len(stats))
    calls.clear()
    agent = SimpleNamespace(subagents=manager)
    assert sweep.auto_start_stalled_orphans(agent)["started"] == 0
    assert sweep._reconcile_direct_parent_waits(agent)["direct_parent_waits_checked"] == 0
    assert len(calls) == 7
    ids, after = [], ""
    while True:
        page, after = index.recovery_ids(after=after, limit=3)
        assert len(page) <= 3
        ids.extend(page)
        if not after:
            break
    assert len(ids) == len(set(ids)) == 7


@pytest.mark.parametrize("status", ["DONE", "FAILED", "BLOCKED", "TAKEN_OVER"])
def test_terminal_delivery_exits_candidates_only_after_ack(tmp_path, status):
    manager = _selected_manager(tmp_path)
    task = manager.create_run(goal="真实未结交付")
    pending, _, _, _ = _pending_task(tmp_path, task.id)
    task.status, task.attributes = status, pending.attributes
    manager.save(task)
    assert [row.id for row in managers.select_recovery_runs(manager)] == [task.id]
    report = runtime_closeout.recover_pending_closeouts(
        None, load_task=manager.load, save_task=manager.save,
        list_tasks=lambda: managers.select_recovery_runs(manager), notify_result=lambda *a, **k: "delivered",
    )
    assert report["runtime_closeouts_recovered"] == 1 and managers.select_recovery_runs(manager) == []
    assert manager.load(task.id).status == status


@pytest.mark.parametrize("damage", ["missing", "corrupt", "generation", "canonical"])
def test_index_unknown_never_becomes_healthy_empty(tmp_path, monkeypatch, damage):
    manager = _selected_manager(tmp_path)
    task = manager.create_run(goal="索引未知保持可见")
    service, index = manager.persistence, manager.persistence.run_index
    monkeypatch.setattr(service, "_start_run_index_maintenance", lambda: None)
    if damage == "missing":
        index.path.unlink()
    elif damage == "corrupt":
        index.path.write_bytes(b"not a sqlite database")
    elif damage == "generation":
        with index.connect() as db:
            db.execute("UPDATE meta SET value=2 WHERE key='schema'")
    else:
        (manager.workspace / task.id / "task.json").write_text("{broken")
    assert service.list_recovery_runs_report().load_errors
    with pytest.raises(managers.SubagentRunIndexUnavailable):
        managers.select_recovery_runs(manager)
    if damage in {"missing", "generation"}:
        assert service.backfill_run_index(batch_size=1)["complete"]
        assert [row.id for row in managers.select_recovery_runs(manager)] == [task.id]


def test_background_lineage_uses_exact_scope_and_reports_bad_canonical(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    root = manager.create_run(goal="root")
    child = manager.create_run(goal="child", root_id=root.id, parent_id=root.id)
    manager.create_run(goal="other")
    state = BackgroundContextLoad(agent=SimpleNamespace(subagents=manager), store=None, thread=None,
                                  task_id=child.id, config=None, policy_request=None, load_errors=[])
    monkeypatch.setattr(manager, "list_run_summaries", lambda: pytest.fail("historical scan"))
    assert _task_context_ids(state) == {root.id, child.id} and not state.load_errors
    (manager.workspace / root.id / "task.json").write_text("{broken")
    # 同一次准备冻结范围；下一次准备才读取后来发生的canonical损坏。
    state = BackgroundContextLoad(agent=state.agent, store=None, thread=None, task_id=child.id,
                                  config=None, policy_request=None, load_errors=[])
    assert _task_context_ids(state) == {root.id, child.id} and state.load_errors


def test_old_generation_backfill_restores_terminal_closeout_membership(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    task = manager.create_run(goal="旧投影重建终态待交付")
    pending, _, _, _ = _pending_task(tmp_path, task.id)
    task.status, task.attributes = "TAKEN_OVER", pending.attributes
    manager.save(task)
    service, index = manager.persistence, manager.persistence.run_index
    monkeypatch.setattr(service, "_start_run_index_maintenance", lambda: None)
    with index.connect() as db:
        db.execute("DELETE FROM membership WHERE kind='closeout'")
        db.execute("INSERT INTO inventory(run_id,done) VALUES (?,1)", (task.id,))
        db.execute("UPDATE meta SET value=2 WHERE key='schema'")
    assert service.list_recovery_runs_report().load_errors
    assert service.backfill_run_index(batch_size=1)["complete"]
    assert [row.id for row in managers.select_recovery_runs(manager)] == [task.id]


def test_pending_canonical_commit_uses_existing_exact_repair(tmp_path):
    manager = _selected_manager(tmp_path)
    task = manager.create_run(goal="中断的索引提交")
    index = manager.persistence.run_index
    index.mark_pending(task.id, task=task)
    with pytest.raises(managers.SubagentRunIndexUnavailable):
        index.recovery_ids(limit=1)
    assert [row.id for row in managers.select_recovery_runs(manager)] == [task.id]
    assert index.coverage()["pending"] == 0


def test_ended_parent_wait_clears_proof_without_reopening_parent(tmp_path):
    from agent_py_agent.agent.subagents.direct_parent_lifecycle import (
        mark_parent_waiting_for_direct_children,
        parent_wait_blocks_dispatch,
    )

    manager = _selected_manager(tmp_path)
    parent = manager.create_run(goal="parent")
    child = manager.create_run(goal="child", parent_id=parent.id, root_id=parent.id)
    assert mark_parent_waiting_for_direct_children(manager, parent.id, [child.id]) == (child.id,)
    parent = manager.load(parent.id)
    parent.status = "DONE"
    manager.save(parent)
    report = sweep._reconcile_direct_parent_waits(SimpleNamespace(subagents=manager))
    assert report["direct_parent_waits_checked"] == 1 and report["direct_parent_waits_released"] == 0
    assert manager.load(parent.id).status == "DONE" and not parent_wait_blocks_dispatch(manager.load(parent.id))
    assert [row.id for row in managers.select_recovery_runs(manager)] == [child.id]


def test_terminal_source_link_is_acknowledged_before_candidate_retirement(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration import lifecycle
    from agent_py_agent.agent.ingestion import source_worker
    from agent_py_agent.agent.ingestion.watch_state import persist_state

    agent, owner_home = _agent(tmp_path)
    _bind_audit_workspace(agent, owner_home, "audit-source-test", tmp_path)
    watch = _watch(owner_home, "audit-source-test", 1)
    monkeypatch.setattr(lifecycle, "auto_start_tasks", lambda *a, **k: {"status": "not_needed", "run_ids": []})
    worker_id = source_worker.ensure_audit_source_worker(agent, watch)["run_id"]
    task = agent.subagents.load(worker_id)
    task.status, task.verification_status = "DONE", "VERIFIED"
    agent.subagents.save(task)
    parent = agent.conversation_store.tasks.load_report("audit-source-test")[0]
    agent.conversation_store.tasks.bind({"task_id": task.id, "thread_id": parent.thread_id, "status": "active"})
    watch.opened_at, watch.watch_window_seconds, watch.window_finalized_at = 1.0, 1, 2.0
    persist_state(watch)
    assert [row.id for row in managers.select_recovery_runs(agent.subagents)] == [task.id]
    assert sweep._reconcile_conversation_parent_lifecycle(agent)["source_workers_completed"] == 1
    assert agent.conversation_store.tasks.load_report(task.id)[0].status == "completed"
    sweep._reconcile_conversation_parent_lifecycle(agent)
    assert managers.select_recovery_runs(agent.subagents) == []
    assert agent.subagents.load(task.id).status == "DONE"
