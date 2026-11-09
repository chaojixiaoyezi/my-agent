"""领域读口先按既有索引选范围，再读取真实 canonical；历史规模不进入热路径。"""
from __future__ import annotations

import json
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.session.resume import _load_subagent_context
from agent_py_agent.agent.subagents.cancellation import (
    CancelSubagentTaskRequest,
    prepare_subagent_stops,
)
from agent_py_agent.agent.subagents.kernel import SubagentKernelQuery
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.models import SubAgentBoardOptions, SubAgentTask
from agent_py_agent.agent.subagents.run_budget import (
    SubagentRunBudgetRequest,
    build_subagent_run_budget_report,
)
from agent_py_agent.agent.subagents.services.persistence.run_index import (
    SubagentRunIndexUnavailable,
)
from agent_py_agent.agent.subagents.services.recovery.orchestrator import (
    RecoveryOrchestrationRequest,
)


def _history(manager, count):
    # 使用 reader 支持的旧 canonical 和原 upsert 批量装配隔离历史，不伪造查询结果。
    with manager.persistence.run_index.connect() as db:
        for number in range(count):
            run_id = f"history-{number:05}"
            payload = {"id": run_id, "goal": "历史", "thought": "", "plan": [],
                       "status": "DONE", "created_at": 1.0, "updated_at": 2.0}
            path = manager.workspace / run_id / "task.json"
            path.parent.mkdir()
            path.write_text(json.dumps(payload), encoding="utf-8")
            manager.persistence.run_index.upsert(SubAgentTask(**payload), connection=db)


def _reads(manager, monkeypatch):
    reads = Counter()
    original_stat = Path.stat

    def observed_stat(path, *args, **kwargs):
        if path.name == "task.json" and path.parent.name.startswith("history-"):
            reads[path.parent.name] += 1
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", observed_stat)
    for name in ("load", "_cached_run_ref"):
        original = getattr(manager.persistence, name)

        def observed(run_id, *args, _original=original, **kwargs):
            reads[run_id] += 1
            return _original(run_id, *args, **kwargs)

        monkeypatch.setattr(manager.persistence, name, observed)
    return reads


@pytest.mark.parametrize("history_count", [100, 1000, 10000])
def test_scoped_domain_reads_do_not_hydrate_unrelated_history(tmp_path, monkeypatch, history_count, record_property):
    from agent_py_agent.agent.user_space.run_workspace import (
        EnsureRunWorkspaceRequest,
        activate_run_workspace,
    )

    manager = SubAgentManager(tmp_path / "runs")
    root = manager.create_run(goal="当前根", thought="", plan=[], attributes={"conversation_thread_id": "thread-current"})
    activate_run_workspace(root.task_workspace_dir, EnsureRunWorkspaceRequest(
        home=tmp_path, template="", task_name="当前根", user_prompt="当前任务", task_id=root.id, run_id=root.id, request_id=root.id))
    child = manager.create_run(goal="失败孩子", thought="", plan=[], parent_id=root.id, root_id=root.id)
    child.status = "FAILED"
    manager.save(child)
    done = manager.create_run(goal="已结束孩子", thought="", plan=[], parent_id=root.id, root_id=root.id)
    done.status = "DONE"
    manager.save(done)
    _history(manager, history_count)
    reads = _reads(manager, monkeypatch)
    operations = (
        lambda: manager.board.build_board(options=SubAgentBoardOptions(root_id=root.id)),
        lambda: manager.board.due_check(root_id=root.id),
        lambda: manager.kernel_snapshot(SubagentKernelQuery(root_id=root.id)),
        lambda: manager.kernel_snapshot(SubagentKernelQuery(scope="conversation_thread", conversation_thread_id="thread-current")),
        lambda: manager.kernel_snapshot(SubagentKernelQuery(scope="task_workspace", task_workspace_dir=root.task_workspace_dir)),
        lambda: build_subagent_run_budget_report(SubagentRunBudgetRequest(manager, root_id=root.id)),
        lambda: manager.hierarchy.orchestrate_recovery(params=RecoveryOrchestrationRequest(run_ids=[child.id])),
        lambda: prepare_subagent_stops(SimpleNamespace(subagents=manager), [CancelSubagentTaskRequest(root, "stop", False)]),
    )
    for number, operation in enumerate(operations):
        reads.clear()
        result = operation()
        assert result is not None
        if hasattr(result, "load_errors"):
            assert result.load_errors == []
        assert not {run_id for run_id in reads if run_id.startswith("history-")}
        assert sum(reads.values()) <= 40
        record_property(f"history_{history_count}_operation_{number}_canonical_reads", sum(reads.values()))
    assert manager.load(done.id).status == "DONE"


def test_board_explicit_empty_candidates_and_forwarded_plan(tmp_path):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="当前", thought="", plan=[])
    assert manager.board.build_board(candidate_ids=[]).summary == {"total": 0}
    assert manager.board.build_board(candidate_ids=None).summary["total"] == 1
    assert manager.board.due_check(candidate_ids=[]).issues == []
    assert manager.board.plan_actions(candidate_ids=[]).actions == []
    board = manager.board.build_board(candidate_ids=[task.id])
    assert [item.id for item in board.items] == [task.id]


def test_board_selected_parent_does_not_mislabel_real_excluded_child(tmp_path):
    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(goal="父级", thought="", plan=[])
    child = manager.create_run(goal="已结束孩子", thought="", plan=[], parent_id=parent.id, root_id=parent.id)
    child.status = "DONE"
    manager.save(child)
    board = manager.board.build_board(candidate_ids=[parent.id])
    assert board.summary["total"] == 1
    assert board.items[0].child_status_counts == {"DONE": 1}


def test_dispatch_selects_current_before_canonical_reads(tmp_path, monkeypatch):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="待派工", thought="", plan=[])
    _history(manager, 100)
    reads = _reads(manager, monkeypatch)
    assert [row.id for row in manager.indexing.select_runs(None)] == [task.id]
    assert set(reads) == {task.id}
    assert manager.indexing.select_runs([]) == []


def test_session_recent_scope_is_selected_before_hydration(tmp_path, monkeypatch):
    manager = SubAgentManager(tmp_path)
    for number in range(7):
        task = manager.create_run(goal=f"当前-{number}", thought="", plan=[],
                                  attributes={"session_id": "session-a", "conversation_thread_id": "session-a"})
    _history(manager, 100)
    reads = _reads(manager, monkeypatch)
    rows = _load_subagent_context(SimpleNamespace(subagents=manager), "session-a")
    assert [row["goal"] for row in rows] == [f"当前-{number}" for number in range(6, 1, -1)]
    assert len(reads) == 5 and task.id in reads
    assert all(not run_id.startswith("history-") for run_id in reads)


def test_resume_uses_real_session_task_references_and_retains_recent_order(tmp_path, monkeypatch):
    from agent_py_agent.agent.session.manager import SessionManager
    from agent_py_agent.agent.session.resume import resume_session
    from agent_py_agent.agent.settings import AgentConfig

    manager = SubAgentManager(tmp_path / "runs")
    root = manager.create_run(goal="本会话的根", thought="", plan=[])
    for number in range(6):
        manager.create_run(goal=str(number), thought="", plan=[], parent_id=root.id, root_id=root.id)
    expected = [task.id for task in manager.list_runs_for_root_report(root.id).runs[:5]]
    config = AgentConfig(session_workspace=str(tmp_path / "sessions"), memory_path=str(tmp_path / "memory.jsonl"))
    session = SessionManager(config).create_session(metadata={"task_id": root.id})
    _history(manager, 100)
    reads = _reads(manager, monkeypatch)
    result = resume_session(SimpleNamespace(subagents=manager, config=config), session.session_id)
    assert [row["id"] for row in result["subagent_context"]] == expected
    assert len(reads) == 5 and not any(run_id.startswith("history-") for run_id in reads)


def test_board_current_approval_scope_and_filters_precede_item_construction(tmp_path, monkeypatch):
    from agent_py_agent.agent.subagents.models import CapabilityRequest
    from agent_py_agent.agent.subagents.services.board import service

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="已结束但授权未结", thought="", plan=[], owner="worker", supervisor="parent")
    task.status = "DONE"
    task.capability_requests = [CapabilityRequest("approval", task.id, "访问", "read_file")]
    manager.save(task)
    _history(manager, 100)
    ids = [row.id for row in manager.list_current_runs_report(include_open_capabilities=True).runs]
    board = manager.board.build_board(candidate_ids=ids, options=SubAgentBoardOptions(owner="parent"))
    assert board.summary["total"] == 1 and board.items[0].open_request_count == 1
    assert board.items[0].status == "DONE"
    original = service.to_board_item
    constructed = []

    def observed(manager, row, **kwargs):
        constructed.append(row.id)
        return original(manager, row, **kwargs)

    monkeypatch.setattr(service, "to_board_item", observed)
    assert manager.board.build_board(options=SubAgentBoardOptions(status="RUNNING")).summary == {"total": 0}
    assert constructed == []


def test_corrupt_selected_canonical_is_unknown_before_any_stop_signal(tmp_path, monkeypatch):
    from agent_py_agent.agent.subagents import cancellation
    from agent_py_agent.agent.subagents.services.agent_run_state import (
        canonical_state_path_from_payload,
    )

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="待查", thought="", plan=[])
    canonical = canonical_state_path_from_payload(asdict(task))
    canonical.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(cancellation, "signal_runner_attempt", lambda *args: pytest.fail("坏账不能广播停止"))
    with pytest.raises(SubagentRunIndexUnavailable):
        manager.board.build_board(candidate_ids=[task.id])
    with pytest.raises(SubagentRunIndexUnavailable):
        build_subagent_run_budget_report(SubagentRunBudgetRequest(manager, root_id=task.id))
    with pytest.raises(json.JSONDecodeError):
        prepare_subagent_stops(SimpleNamespace(subagents=manager), [CancelSubagentTaskRequest(task, "stop", False)])
    snapshot = manager.kernel_snapshot(SubagentKernelQuery(root_id=task.id))
    assert snapshot.load_errors and snapshot.warnings and snapshot.runs == []
    report = manager.hierarchy.orchestrate_recovery(params=RecoveryOrchestrationRequest(run_ids=[task.id]))
    assert report.load_errors and report.steps == []
    context = _load_subagent_context(SimpleNamespace(subagents=manager), "session", metadata={"task_id": task.id})
    assert context[0]["type"] == "subagent_context_load_error"


def test_partial_and_replaced_index_never_fall_back_to_owner_history(tmp_path, monkeypatch):
    import shutil

    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="当前", thought="", plan=[])
    _history(manager, 100)
    monkeypatch.setattr(manager.persistence, "list_runs", lambda: pytest.fail("索引未知不能全扫"))
    index = manager.persistence.run_index
    with index.connect() as db:
        db.execute("UPDATE meta SET value=0 WHERE key='complete'")
    # 禁止异步回填改变单一故障；测试不替换查询结果。
    monkeypatch.setattr(manager.persistence, "_start_run_index_maintenance", lambda: None)
    with pytest.raises(SubagentRunIndexUnavailable):
        manager.board.build_board(options=SubAgentBoardOptions(root_id=root.id))
    with index.connect() as db:
        db.execute("UPDATE meta SET value=1 WHERE key='complete'")
    original = index.request_ids

    def replaced(*args):
        ids = original(*args)
        replacement = tmp_path / "replacement.sqlite3"
        shutil.copy2(index.path, replacement)
        os.replace(replacement, index.path)
        return ids

    monkeypatch.setattr(index, "request_ids", replaced)
    with pytest.raises(SubagentRunIndexUnavailable, match="identity changed"):
        manager.board.build_board(options=SubAgentBoardOptions(root_id=root.id))


def test_cancel_rechecks_identity_after_event_controlled_attempt_change(tmp_path, monkeypatch):
    from agent_py_agent.agent.runtime_db.operations import RuntimeConflictError
    from agent_py_agent.agent.subagents import cancellation

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="原授权", thought="", plan=[])
    release, committed = Event(), Event()

    def replace_attempt():
        assert release.wait(5)
        with manager.creation_guard():
            current = manager.load(task.id)
            current.runner_active_attempt_id = "new-attempt"
            manager.save(current)
        committed.set()

    monkeypatch.setattr(cancellation, "signal_runner_attempt", lambda *args: pytest.fail("旧身份不能发中断"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(replace_attempt)
        release.set()
        assert committed.wait(5)
        with pytest.raises(RuntimeConflictError):
            prepare_subagent_stops(SimpleNamespace(subagents=manager), [CancelSubagentTaskRequest(task, "stop", False)])
        future.result(timeout=5)
    assert manager.load(task.id).runner_active_attempt_id == "new-attempt"


def test_cancel_keeps_new_descendant_and_terminal_exit_proof_across_cache_eviction(tmp_path):
    from agent_py_agent.agent.subagents.cancellation import cleanup_subagent_stops

    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(goal="原父任务", thought="", plan=[])
    ready, published = Event(), Event()

    def create_before_stop():
        assert ready.wait(5)
        child = manager.create_run(goal="已发表孩子", thought="", plan=[], parent_id=parent.id, root_id=parent.id)
        child.status = "DONE"
        child.attributes["task_control"] = {"cleanup": {"state": "confirmed", "attempt_id": "original-exit"}}
        manager.save(child)
        published.set()
        return child

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(create_before_stop)
        ready.set()
        assert published.wait(5)
        child = future.result(timeout=5)
        batch = prepare_subagent_stops(SimpleNamespace(subagents=manager), [CancelSubagentTaskRequest(parent, "stop", False)])
    assert {stop.report["run_id"] for stop in batch.stops} == {parent.id, child.id}
    _history(manager, manager.persistence._MAX_RUN_CACHE_ENTRIES + 1)
    manager.list_runs_by_ids_report([path.name for path in manager.workspace.iterdir() if path.name.startswith("history-")])
    assert child.id not in manager.persistence._run_cache
    result = cleanup_subagent_stops(batch)
    assert result["ok"] and manager.load(parent.id).status == "CANCELLED"
    current = manager.load(child.id)
    assert current.status == "DONE"
    assert current.attributes["task_control"]["cleanup"] == {"state": "confirmed", "attempt_id": "original-exit"}


def test_failed_parent_and_reused_takeover_remain_in_exact_recovery_scope(tmp_path, monkeypatch):
    from agent_py_agent.agent.subagents.models import SubAgentLeadershipRecoveryPlanOptions
    from agent_py_agent.agent.subagents.services.takeover.run import TakeoverRunRequest

    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="根", thought="", plan=[])
    source = manager.create_run(goal="失败协调者", thought="", plan=[], role="coordinator", parent_id=root.id, root_id=root.id)
    child = manager.create_run(goal="仍运行", thought="", plan=[], parent_id=source.id, root_id=root.id)
    source = manager.load(source.id)
    source.status = "TIMEOUT"
    manager.save(source)
    replacement = manager.create_run(goal="接替", thought="", plan=[], parent_id=root.id, root_id=root.id,
                                     attributes={"takeover_source_run_id": source.id})
    _history(manager, 100)
    reads = _reads(manager, monkeypatch)
    plan = manager.hierarchy.plan_leadership_recovery(params=SubAgentLeadershipRecoveryPlanOptions(root_id=root.id))
    assert plan.summary["failed_parent_nodes"] == 1
    assert plan.unassigned[0].coordinator_id == source.id and plan.unassigned[0].child_ids == [child.id]
    result = manager.create_takeover_run(TakeoverRunRequest(source_run_id=source.id, reason="retry"))
    assert not result.created and result.takeover_run_id == replacement.id
    assert not any(run_id.startswith("history-") for run_id in reads)


def test_retry_exhaustion_is_not_lost_when_recovery_hydrates_only_root(tmp_path):
    from agent_py_agent.agent.capability.config import CapabilityConfig
    from agent_py_agent.tests.test_opt_recovery_counters import _slice

    manager = SubAgentManager(tmp_path)
    manager.recovery_capability_config = lambda: CapabilityConfig(subagent_no_progress_attempt_limit=2)
    task = manager.create_run(goal="耗尽重试", thought="", plan=[])
    for number in (1, 2):
        _slice(manager, task.id, number, failed=True)
    report = manager.hierarchy.orchestrate_recovery(params=RecoveryOrchestrationRequest(run_ids=[task.id]))
    assert report.steps[0].strategy_snapshot["no_progress_fuse"] is True
    assert not report.steps[0].requires_dispatch


def test_automatic_recovery_keeps_terminal_closeout_candidates_and_drops_completed_obligations(tmp_path, monkeypatch):
    from agent_py_agent.agent.subagents.services.runtime_closeout import (
        RUNTIME_CLOSEOUT_ATTR,
        RUNTIME_CLOSEOUT_SCHEMA,
    )

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="失败但收口未结", thought="", plan=[])
    task.status = "FAILED"
    task.attributes[RUNTIME_CLOSEOUT_ATTR] = {"schema_version": RUNTIME_CLOSEOUT_SCHEMA}
    manager.save(task)
    _history(manager, 100)
    reads = _reads(manager, monkeypatch)
    report = manager.hierarchy.orchestrate_recovery(params=RecoveryOrchestrationRequest())
    assert [step.run_id for step in report.steps] == [task.id]
    assert not any(run_id.startswith("history-") for run_id in reads)
    manager.mutate(task.id, lambda current: current.attributes.pop(RUNTIME_CLOSEOUT_ATTR, None))
    assert manager.hierarchy.orchestrate_recovery(params=RecoveryOrchestrationRequest()).steps == []


def test_limited_probe_selects_ids_before_reading_real_work_orders(tmp_path, monkeypatch):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="真实探测", thought="", plan=[])
    _history(manager, 100)
    reads = _reads(manager, monkeypatch)
    report = manager.channel_probe.probe_channels(limit=1)
    assert len(report.results) == 1 and report.results[0].run_id == task.id
    assert set(reads) == {task.id}
    assert (Path(task.logs_dir) / "last_channel_probe.json").is_file()


def test_index_scope_connections_release_descriptors_on_success_and_error(tmp_path, record_property):
    from agent_py_agent.agent.subagents.services.agent_run_state import (
        canonical_state_path_from_payload,
    )

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="fd", thought="", plan=[], attributes={"conversation_thread_id": "session"})
    fd_dir = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    before = len(os.listdir(fd_dir))
    for _ in range(4):
        manager.board.build_board(options=SubAgentBoardOptions(root_id=task.id))
        manager.kernel_snapshot(SubagentKernelQuery(root_id=task.id))
        _load_subagent_context(SimpleNamespace(subagents=manager), "session")
    canonical_state_path_from_payload(asdict(task)).write_text("{broken", encoding="utf-8")
    for _ in range(4):
        with pytest.raises(SubagentRunIndexUnavailable):
            manager.board.build_board(options=SubAgentBoardOptions(root_id=task.id))
        assert _load_subagent_context(SimpleNamespace(subagents=manager), "session")[0]["type"] == "subagent_context_load_error"
    delta = len(os.listdir(fd_dir)) - before
    record_property("fd_delta", delta)
    assert delta == 0


def test_missing_index_is_reported_by_kernel_instead_of_healthy_empty(tmp_path):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="缺索引", thought="", plan=[])
    manager.persistence.run_index.path.unlink()
    snapshot = manager.kernel_snapshot(SubagentKernelQuery(root_id=task.id))
    assert snapshot.load_errors and snapshot.warnings and snapshot.runs == []
