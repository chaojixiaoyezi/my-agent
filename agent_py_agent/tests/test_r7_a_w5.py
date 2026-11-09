"""派工只读当前 canonical 来源；索引不授予执行权，也不代替取消退出证明。"""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import _final_pending_runner_count
from agent_py_agent.agent.agent_core.orchestration.dispatch.mixin import (
    _dispatch_context_from_params,
    _finalize_scoped_tasks,
)
from agent_py_agent.agent.agent_core.orchestration.dispatch.params import (
    DispatchParams,
    RunnerBatchContext,
)
from agent_py_agent.agent.agent_core.orchestration.dispatch.refs import (
    _visible_run_ids,
    dispatch_scope_tasks,
)
from agent_py_agent.agent.agent_core.orchestration.dispatch.runner_batches import (
    execute_runner_jobs,
)
from agent_py_agent.agent.agent_core.orchestration.dispatch.service import (
    make_capability_route_records,
    update_pending_work_state,
)
from agent_py_agent.agent.agent_core.orchestration.tools.cancel import _resolve_run_ids
from agent_py_agent.agent.agent_core.orchestration.tools.capability import _known_children_hint
from agent_py_agent.agent.agent_core.planner_service import (
    PlannerStateParams,
    _collect_parent_planner_context,
)
from agent_py_agent.agent.agent_core.runner.dispatch import (
    RunnerCandidatePolicy,
    _dispatch_patch_review_run_ids,
    _is_dispatch_runner_candidate,
)
from agent_py_agent.agent.agent_core.tool_loop.completion import _delegated_work_facts
from agent_py_agent.agent.capability import CapabilityRouter
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.manager import SubagentRunIndexUnavailable
from agent_py_agent.tests.test_r7_a_w2 import _selected_manager, _task
from agent_py_agent.tests.test_runtime_guidance import _tool_loop_params


def _agent(manager, tmp_path):
    return SimpleNamespace(subagents=manager, root=tmp_path, config=AgentConfig(), capability_router=CapabilityRouter(),
        _current_run_params=SimpleNamespace(request_id="request", run_id="request", task_id="request", task_attributes={}),
        subagent_run_ids_for_request=lambda _: [f"child-{number}" for number in range(7)])


def _context(**scope):
    return _dispatch_context_from_params(CapabilityRouter(), CapabilityConfig(), DispatchParams(**scope))


def _batch():
    return RunnerBatchContext([], 0, 0, "", 0, True, [])


def _indexed(manager, tasks):
    with manager.persistence.run_index.connect() as db:
        for task in tasks:
            manager.persistence.run_index.upsert(task, connection=db)


def _no_history(manager, monkeypatch):
    reads, stats = [], []
    load, stat, glob = manager.persistence.load, Path.stat, Path.glob

    def observed_load(run_id):
        reads.append(run_id)
        return load(run_id)

    def observed_stat(path, *args, **kwargs):
        if path.name in {"task.json", "canonical_state.json"}:
            stats.append(path.parent.name)
        return stat(path, *args, **kwargs)

    def observed_glob(path, pattern, **kwargs):
        assert path != manager.workspace or pattern != "*/task.json", "enumerated historical bodies"
        return glob(path, pattern, **kwargs)

    monkeypatch.setattr(manager.persistence, "load", observed_load)
    monkeypatch.setattr(Path, "stat", observed_stat)
    monkeypatch.setattr(Path, "glob", observed_glob)
    return reads, stats


@pytest.mark.parametrize("history", [100, 1000, 10000])
def test_selected_dispatch_completion_and_cleanup_ignore_history(tmp_path, monkeypatch, record_property, history):
    manager = _selected_manager(tmp_path)
    selected = {f"child-{number}" for number in range(7)}
    _indexed(manager, (_task(manager, run_id, root="request", parent="request", thread="") for run_id in sorted(selected)))
    _indexed(manager, (_task(manager, f"old-{number}", "DONE", root="other") for number in range(history)))
    agent, ctx = _agent(manager, tmp_path), _context(root_id="request")
    reads, stats = _no_history(manager, monkeypatch)
    results = {}
    for name, call in {
        "completion": lambda: _delegated_work_facts(agent, _tool_loop_params(task_id="request")),
        "runner": lambda: execute_runner_jobs(agent, ctx, _batch()),
        "finalize": lambda: _finalize_scoped_tasks(agent, DispatchParams(root_id="request")),
        "pending": lambda: update_pending_work_state(agent, ctx=ctx),
        "loop_pending": lambda: _final_pending_runner_count(agent),
        "visible": lambda: _visible_run_ids(agent),
        "hint": lambda: _known_children_hint(agent),
        "capability": lambda: make_capability_route_records(agent, ctx),
        "cancel_selection": lambda: _resolve_run_ids(agent, {"root_id": "request"}),
    }.items():
        reads.clear()
        stats.clear()
        results[name] = call()
        assert set(reads) <= selected and set(stats) <= selected, name
        assert len(reads) <= 14 and len(stats) <= 100, (name, reads, stats)
        record_property(name + "_loads", len(reads))
        record_property(name + "_stats", len(stats))
    assert results["completion"]["total"] == 7
    assert {task.id for task in results["finalize"]} == selected
    assert set(results["visible"][0]) == set(results["cancel_selection"].run_ids) == selected
    assert results["pending"] is False and results["loop_pending"] == (0, {})
    assert results["runner"] == results["capability"] == [] and all(run_id in results["hint"] for run_id in selected)


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_completion_retains_partial_topology_and_read_errors(tmp_path, monkeypatch, damage):
    manager = _selected_manager(tmp_path)
    tasks = [_task(manager, f"child-{number}", root="request", parent="request" if number < 6 else "child-0")
             for number in range(7)]
    _indexed(manager, tasks)
    complete = _delegated_work_facts(_agent(manager, tmp_path), _tool_loop_params(task_id="request"))
    assert complete["total"] == 7 and complete["topology"]["descendant_count"] == 1 and "coverage" not in complete
    path = manager.workspace / tasks[-1].id / "task.json"
    path.unlink() if damage == "missing" else path.write_text("{broken")
    _no_history(manager, monkeypatch)
    facts = _delegated_work_facts(_agent(manager, tmp_path), _tool_loop_params(task_id="request"))
    assert facts["total"] == 6 and facts["status_counts"] == {"RUNNING": 6}
    assert facts["coverage"] == "partial" and facts["total_is_minimum"] is True
    assert facts["load_errors"][0]["run_id"] == "child-6"
    assert facts["topology"]["direct_child_count"] == 6


def test_retry_capability_and_terminal_patch_obligations_keep_original_gates(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    tasks = [_task(manager, status.lower(), status, root="request", parent="request", thread="")
             for status in ("FAILED", "TIMEOUT", "BLOCKED", "RUNNING", "DONE")]
    for task in tasks[:3]:
        task.failure_type = "runner_error"
        manager.save(task)
    patch = tasks[-1]
    patch.output_json = str(manager.workspace / patch.id / "output.json")
    manager.save(patch)
    Path(manager.load(patch.id).output_json).write_text(json.dumps({"patches": [{"path": "result.txt"}]}))
    tasks.append(_task(manager, "approval", "DONE", root="request", parent="request", capability="OPEN"))
    _indexed(manager, tasks)
    agent = _agent(manager, tmp_path)
    _no_history(manager, monkeypatch)
    selected = dispatch_scope_tasks(agent)
    policy = RunnerCandidatePolicy(runner_retry_limit=1)
    assert {task.id for task in selected if _is_dispatch_runner_candidate(task, policy=policy)} == {"failed", "timeout", "blocked"}
    assert not any(_is_dispatch_runner_candidate(task, policy=RunnerCandidatePolicy()) for task in selected)
    assert _dispatch_patch_review_run_ids(_finalize_scoped_tasks(agent, DispatchParams())) == ["done"]
    assert "approval(DONE,未决申请x1)" in _known_children_hint(agent)
    ctx = _context(root_id="request", max_runners=7)
    records = execute_runner_jobs(agent, ctx, _batch())
    assert {record.run_id for record in records} == {"failed", "timeout", "blocked"}
    assert all(record.dry_run and not record.applied for record in records)
    assert update_pending_work_state(agent, ctx=ctx) is True
    assert _final_pending_runner_count(agent) == (3, {})


def test_explicit_include_miss_exclusion_and_suffix_diagnostics(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    _indexed(manager, [_task(manager, "worker-actual-123", "PLANNING", root="request"),
                       _task(manager, "excluded", "PLANNING", root="request"),
                       _task(manager, "foreign", "PLANNING", root="other")])
    agent = _agent(manager, tmp_path)
    _no_history(manager, monkeypatch)
    ctx = _context(root_id="request", include_run_ids=["worker-wrong-123", "excluded", "foreign"], exclude_run_ids=["excluded"])
    records = execute_runner_jobs(agent, ctx, _batch())
    assert len(records) == 1 and records[0].action == "invalid_run_ids" and not records[0].ok
    assert "worker-actual-123" in records[0].message and "possible_corrections" in records[0].message
    assert "valid_scope_run_ids=['worker-actual-123']" in records[0].message
    assert "excluded" in records[0].message and "foreign" in records[0].message


@pytest.mark.parametrize("damage", ["missing", "partial", "corrupt"])
def test_unknown_index_is_neither_empty_success_nor_history_fallback(tmp_path, monkeypatch, damage):
    manager = _selected_manager(tmp_path)
    _indexed(manager, [_task(manager, "one", root="request")])
    manager.persistence.read_only = True  # 只读消费方不启动维护；UNKNOWN 必须原样报告。
    index = manager.persistence.run_index
    if damage == "missing":
        index.path.unlink()
    elif damage == "corrupt":
        index.path.write_bytes(b"not sqlite")
    else:
        with index.connect() as db:
            db.execute("UPDATE meta SET value=0 WHERE key='complete'")
    agent = _agent(manager, tmp_path)
    _no_history(manager, monkeypatch)
    errors = (SubagentRunIndexUnavailable, FileNotFoundError, sqlite3.DatabaseError)
    with pytest.raises(errors):
        dispatch_scope_tasks(agent)
    with pytest.raises(errors):
        execute_runner_jobs(agent, _context(root_id="request"), _batch())
    assert _visible_run_ids(agent)[1] is not None
    assert _final_pending_runner_count(agent)[1]
    selection = _resolve_run_ids(agent, {"root_id": "request"})
    assert not selection.ok and selection.error_payload["error"]


def test_display_limits_apply_before_canonical_body_reads(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    _indexed(manager, (_task(manager, f"child-{number}", root="request", parent="request") for number in range(40)))
    _indexed(manager, [_task(manager, "unrelated", root="elsewhere", parent="elsewhere")])
    agent = _agent(manager, tmp_path)
    reads, _stats = _no_history(manager, monkeypatch)
    ids, error = _visible_run_ids(agent)
    assert error is None and len(ids) == len(reads) == 20 and "unrelated" not in reads
    reads.clear()
    hint = _known_children_hint(agent)
    assert len(reads) <= 12 and hint.count("(RUNNING") == 12 and "unrelated" not in reads


def test_cancel_subtree_selection_includes_deep_children_without_siblings(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    _indexed(manager, [_task(manager, "root", root="root"), _task(manager, "branch", root="root", parent="root"),
                       _task(manager, "child", root="root", parent="branch"),
                       _task(manager, "deep", "DONE", root="root", parent="child"),
                       _task(manager, "sibling", root="root", parent="root"), _task(manager, "old", "DONE", root="other")])
    agent = _agent(manager, tmp_path)
    reads, _stats = _no_history(manager, monkeypatch)
    selection = _resolve_run_ids(agent, {"root_id": "branch"})
    assert selection.ok and set(selection.run_ids) == {"branch", "child", "deep"}
    assert set(reads) <= {"branch", "child", "deep"}
    reads.clear()
    assert _resolve_run_ids(agent, {"run_ids": ["branch"]}).run_ids == ["branch"]
    assert set(reads) <= {"branch", "root"} and reads[0] == "branch"
    assert _resolve_run_ids(agent, {"run_id": "absent"}).error_payload["reason"] == "requested_run_id_not_in_visible_scope"


@pytest.mark.parametrize("selected", [True, False])
def test_planner_forwards_same_exact_candidates_to_all_board_methods(tmp_path, monkeypatch, selected):
    from agent_py_agent.agent.agent_core.orchestration.dispatch.refs import candidate_ids_kwargs

    manager = _selected_manager(tmp_path)
    _indexed(manager, [_task(manager, "selected", root="request" if selected else "other"), _task(manager, "old", "DONE", root="other")])
    board, observed = manager.board, {}
    # W6 未集成时，仅在真实方法边界观察新 keyword；结果仍由原 board 计算。
    for name in ("build_board", "due_check", "plan_actions"):
        original = getattr(board, name)

        def supporting(*args, candidate_ids=None, _method=original, _name=name, **kwargs):
            observed.setdefault(_name, []).append(candidate_ids)
            return _method(*args, **kwargs, **candidate_ids_kwargs(_method, candidate_ids))

        monkeypatch.setattr(board, name, supporting)
    _board, ctx = _collect_parent_planner_context(_agent(manager, tmp_path), PlannerStateParams(CapabilityConfig(), 7, 20, "test", ""))
    expected = ["selected"] if selected else []
    assert all(observed[name][0] == expected for name in ("build_board", "due_check", "plan_actions"))
    assert [task.id for task in ctx.tasks] == expected


def test_legacy_board_signature_and_explicit_history_remain_available(tmp_path):
    manager = _selected_manager(tmp_path)
    _indexed(manager, [_task(manager, "selected", root="request"), _task(manager, "old", "DONE", root="other")])
    _board, ctx = _collect_parent_planner_context(_agent(manager, tmp_path), PlannerStateParams(CapabilityConfig(), 7, 20, "test", ""))
    assert [task.id for task in ctx.tasks] == ["selected"]
    historical = SimpleNamespace(subagents=manager)
    assert {task.id for task in dispatch_scope_tasks(historical)} == {"selected", "old"}


def test_display_roster_uses_direct_parent_before_limit_and_keeps_terminal_approval(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    _indexed(manager, [_task(manager, "z-child", root="request", parent="request"),
                       _task(manager, "z-approval", "DONE", root="request", parent="request", capability="OPEN")])
    _indexed(manager, (_task(manager, f"a-grandchild-{number}", root="request", parent="z-child") for number in range(40)))
    reads, _stats = _no_history(manager, monkeypatch)
    agent = _agent(manager, tmp_path)
    assert set(_visible_run_ids(agent)[0]) == {"z-child", "z-approval"}
    hint = _known_children_hint(agent)
    assert "z-child(RUNNING" in hint and "z-approval(DONE,未决申请x1)" in hint
    assert set(reads) <= {"z-child", "z-approval"}


def test_cancel_cleanup_event_preserves_terminal_exit_and_other_scopes(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.subagents import cancellation, cancellation_hosts
    from agent_py_agent.tests.test_orchestration_cancel_subagents_tool import (
        _create_cancel_tree,
        _execute_host_cancel_subagents,
        _fresh_runner_session,
    )

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    parent, child, history = _create_cancel_tree(agent)
    child.status, child.runner_active_attempt_id = "RUNNING", "original-attempt"
    session = _fresh_runner_session(worker_pid=5353, in_process=False)
    session.update(run_id=child.id, attempt_id="original-attempt", launch_id="exclusive", worker_birth_token="original-birth")
    child.attributes.update(runner_session=session, background_start={"pid": 5353, "attempt_id": "original-attempt", "launch_id": "exclusive"})
    agent.subagents.save(child)
    stale = agent.subagents.load(child.id)
    history_path = Path(agent.subagents.load(history.id).task_dir) / "canonical_state.json"
    history_before = (history_path.read_bytes(), history_path.stat().st_mtime_ns)
    entered, release = Event(), Event()
    cleanup = cancellation.cleanup_runner_stop

    def scheduled(stop):
        entered.set()
        assert release.wait(10)
        return cleanup(stop)

    monkeypatch.setattr(cancellation, "cleanup_runner_stop", scheduled)
    monkeypatch.setattr(cancellation_hosts, "is_pid_alive", lambda _pid: False)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_execute_host_cancel_subagents, agent, {"root_id": child.id, "reason": "关闭原执行轮"})
        try:
            assert entered.wait(10)
            stale.status = "PENDING"
            agent.subagents.save(stale)
            current = agent.subagents.load(child.id)
            assert current.status == "CANCELLED" and not current.runner_active_attempt_id
            assert current.attributes["runner_session"]["status"] == "cancelled"
            assert not _is_dispatch_runner_candidate(current, policy=RunnerCandidatePolicy(runner_retry_limit=1))
        finally:
            release.set()
        result = future.result(timeout=10)
    assert result.ok and json.loads(result.output)["cancelled"][0]["pid_report"]["host_exited"] is True
    assert agent.subagents.load(child.id).status == "CANCELLED"
    assert agent.subagents.load(parent.id).status == "RUNNING"
    assert (history_path.read_bytes(), history_path.stat().st_mtime_ns) == history_before


def test_stale_index_does_not_grant_scope_or_dispatch_authority(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    _indexed(manager, [_task(manager, "child", "PLANNING", root="request", parent="request")])
    path = manager.workspace / "child" / "task.json"
    payload = json.loads(path.read_text())
    payload.update(root_id="foreign", parent_id="foreign")
    path.write_text(json.dumps(payload))
    _no_history(manager, monkeypatch)
    agent = _agent(manager, tmp_path)
    with pytest.raises(SubagentRunIndexUnavailable, match="canonical relation changed"):
        execute_runner_jobs(agent, _context(root_id="request"), _batch())
    assert json.loads(path.read_text())["status"] == "PLANNING"


def test_pending_count_preserves_excluded_and_explicit_status_selection(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    _indexed(manager, [_task(manager, "pending", "PLANNING", root="request"),
                       _task(manager, "done", "DONE", root="other")])
    _no_history(manager, monkeypatch)
    agent = _agent(manager, tmp_path)
    assert update_pending_work_state(agent, ctx=_context(root_id="request", exclude_run_ids=["pending"])) is False
    result = _resolve_run_ids(agent, {"status": "DONE"})
    assert result.ok and result.run_ids == ["done"]


def test_remembered_current_run_keeps_its_canonical_root_obligations(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    _indexed(manager, [_task(manager, "remembered", root="request", parent="request"),
                       _task(manager, "approval", "DONE", root="request", parent="request", capability="OPEN"),
                       _task(manager, "old", "DONE", root="other")])
    agent = SimpleNamespace(subagents=manager, _orchestration_run_ids_seen={"remembered"})
    reads, _stats = _no_history(manager, monkeypatch)
    assert {task.id for task in dispatch_scope_tasks(agent)} == {"remembered", "approval"}
    assert set(reads) <= {"remembered", "approval"}


def test_pending_index_converges_through_original_bounded_repair(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    _indexed(manager, [_task(manager, "one", root="request")])
    path = manager.workspace / "one" / "task.json"
    before = path.read_bytes()
    with manager.persistence.run_index.connect() as db:
        db.execute("UPDATE runs SET pending=1")
    _no_history(manager, monkeypatch)
    assert [task.id for task in dispatch_scope_tasks(_agent(manager, tmp_path))] == ["one"]
    assert manager.persistence.run_index.readiness()["pending"] == 0 and path.read_bytes() == before
