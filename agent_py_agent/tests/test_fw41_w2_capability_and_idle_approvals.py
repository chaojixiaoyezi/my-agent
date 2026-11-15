"""FW41 W2 regressions: root-scoped descendant capability closeout & resumable idle approval discovery."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import agent_activity
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import compact_operations as idle
from agent_py_agent.agent.gateway_parts import recovery
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.capability_request_expiry import (
    capability_request_selection_boundary,
    close_existing_capability_requests,
    close_stale_capability_requests,
    existing_capability_closeout_confirmed,
    existing_capability_request_snapshot,
)
from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityRequestParams
from agent_py_agent.cli import home_runtime_commands as cli_commands


def _fd_count() -> int:
    return len(os.listdir("/dev/fd"))


def _make_blocked_run(
    agent: SimpleAgent,
    *,
    parent_id: str,
    root_id: str,
    depth: int,
    owner: str | None = None,
    active_attempt: str = "",
    status: str = "BLOCKED",
    failure_type: str = "capability_request",
    request_status: str = "OPEN",
):
    manager = agent.subagents
    task = manager.create_run(
        goal=f"task-{parent_id}-{depth}",
        parent_id=parent_id,
        root_id=root_id,
        depth=depth,
        role="worker",
    )
    request = manager.lifecycle.record_capability_request(
        task.id,
        RecordCapabilityRequestParams(
            problem="missing scope",
            needed_capability="filesystem",
            requested_tools=["write_file"],
        ),
    )

    def _update(current):
        if owner is not None:
            current.owner = owner
        current.status = status
        current.failure_type = failure_type
        current.runner_active_attempt_id = active_attempt
        current.capability_requests[0].status = request_status

    manager.mutate(task.id, _update)
    return agent.subagents.load(task.id), request.id


@pytest.mark.parametrize("ended_status", ["DONE", "FAILED", "CANCELLED"])
def test_root_closeout_expires_orphaned_grandchild_and_leaves_idle_clean(tmp_path, monkeypatch, ended_status):
    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / "home"), model_backend="echo"), tmp_path / "workspace")
    store = agent.conversation_store
    thread = store.threads.create()
    monkeypatch.setattr(idle, "list_background_processes", lambda *_, **__: {"projection_ok": True, "items": []})

    root_id = "req-root-1"
    # Ended direct child that left a blocked grandchild behind.
    child_ended = agent.subagents.create_run(
        goal="ended child", parent_id=root_id, root_id=root_id, depth=1, role="worker"
    )
    agent.subagents.mutate(child_ended.id, lambda cur: setattr(cur, "status", ended_status))
    grandchild_orphan, _ = _make_blocked_run(agent, parent_id=child_ended.id, root_id=root_id, depth=2)

    # Live child still running -> its blocked grandchild must remain untouched.
    child_live = agent.subagents.create_run(
        goal="live child", parent_id=root_id, root_id=root_id, depth=1, role="worker"
    )
    agent.subagents.mutate(child_live.id, lambda cur: setattr(cur, "status", "RUNNING"))
    grandchild_live_parent, _ = _make_blocked_run(agent, parent_id=child_live.id, root_id=root_id, depth=2)

    # Active-attempt grandchild, already-granted request, other owner, and other root stay untouched.
    grandchild_active, _ = _make_blocked_run(
        agent, parent_id=child_ended.id, root_id=root_id, depth=2, active_attempt="att-live"
    )
    grandchild_granted, _ = _make_blocked_run(
        agent, parent_id=child_ended.id, root_id=root_id, depth=2, request_status="GRANTED", status="DONE", failure_type=""
    )
    other_owner_child, _ = _make_blocked_run(
        agent, parent_id=child_ended.id, root_id=root_id, depth=2, owner="other-owner"
    )
    other_root_child = agent.subagents.create_run(
        goal="other root ended", parent_id="req-root-2", root_id="req-root-2", depth=1, role="worker"
    )
    agent.subagents.mutate(other_root_child.id, lambda cur: setattr(cur, "status", "DONE"))
    other_root_grandchild, _ = _make_blocked_run(
        agent, parent_id=other_root_child.id, root_id="req-root-2", depth=2
    )

    # Selection boundary selects only the orphaned descendant belonging to this owner & root.
    report = agent.subagents.list_runs_for_root_report(root_id)
    boundary = capability_request_selection_boundary(report.runs, root_id, agent.home_paths.owner_id)
    assert set(boundary) == {grandchild_orphan.id}
    assert boundary[grandchild_orphan.id]["root_id"] == root_id
    selected = existing_capability_request_snapshot(agent.subagents.workspace, boundary)

    # Snapshot + closeout via conversation boundary.
    snapshot = agent_activity.parent_capability_request_snapshot(agent, root_id)
    assert dict(snapshot.request_items) == selected
    closed = agent_activity.close_parent_capability_requests(
        agent, root_id, reason="parent_turn_ended", run_ids=snapshot
    )
    assert closed == [grandchild_orphan.id]
    assert existing_capability_closeout_confirmed(
        agent.subagents.workspace, root_id, selected, selection_metadata=boundary
    )
    # Idempotent re-run changes nothing.
    assert close_stale_capability_requests(agent.subagents, parent_task_id=root_id, reason="parent_turn_ended") == []

    updated_orphan = agent.subagents.load(grandchild_orphan.id)
    assert updated_orphan.status == "CANCELLED"
    assert updated_orphan.capability_requests[0].status == "CLOSED"
    assert updated_orphan.capability_requests[0].constraints["resolution_reason"] == "parent_turn_ended"

    # Non-orphaned / foreign records remain unchanged.
    assert agent.subagents.load(grandchild_live_parent.id).capability_requests[0].status == "OPEN"
    assert agent.subagents.load(grandchild_active.id).capability_requests[0].status == "OPEN"
    assert agent.subagents.load(other_owner_child.id).capability_requests[0].status == "OPEN"
    assert agent.subagents.load(other_root_grandchild.id).capability_requests[0].status == "OPEN"
    assert agent.subagents.load(grandchild_granted.id).capability_requests[0].status == "GRANTED"

    # Once non-target runs are settled cleanly, orphan closeout leaves idle clean.
    def _finish_fixture(current):
        current.status = "DONE"
        current.failure_type = ""
        for req in current.capability_requests:
            req.status = "CLOSED"

    for extra in (grandchild_live_parent.id, grandchild_active.id, other_owner_child.id, other_root_grandchild.id,
                  child_live.id):
        agent.subagents.mutate(extra, _finish_fixture)
    assert idle.idle_compact_busy_state(agent, store, thread) == ""


def test_direct_close_stale_and_recovery_boundary_validation(tmp_path):
    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / "home"), model_backend="echo"), tmp_path / "workspace")
    root_id = "req-rec-1"
    child = agent.subagents.create_run(goal="child", parent_id=root_id, root_id=root_id, depth=1, role="worker")
    agent.subagents.mutate(child.id, lambda cur: setattr(cur, "status", "FAILED"))
    grandchild, req_id = _make_blocked_run(agent, parent_id=child.id, root_id=root_id, depth=2)

    runs = agent.subagents.list_runs_for_root_report(root_id).runs
    children_boundary = capability_request_selection_boundary(runs, root_id, agent.home_paths.owner_id)
    terminal_response = {"id": root_id, "status": "done", "ok": True, "response": "ok"}
    proof = {
        "schema_version": "gateway_capability_closeout.v1",
        "request_id": root_id,
        "parent_task_id": root_id,
        "owner_id": agent.home_paths.owner_id,
        "execution_attempt_id": "exec-1",
        "lease_epoch": 1,
        "lease_owner": "worker-1",
        "reason": "parent_turn_ended",
        "acknowledged": False,
        "snapshot_pending": True,
        "requests": {},
        "terminal_response": terminal_response,
    }
    proof["selection_boundary"] = {
        "schema_version": "gateway_capability_selection.v1",
        "scope": recovery._capability_selection_scope(proof),
        "children": children_boundary,
    }
    record_data = {
        "id": root_id,
        "execution_attempt_id": "exec-1",
        "lease_epoch": 1,
        "lease_owner": "worker-1",
        "terminal_response": terminal_response,
        "capability_closeout": proof,
    }
    validated = recovery._validated_capability_closeout(record_data, owner_id=agent.home_paths.owner_id)
    assert validated is not None

    # Mismatched root_id in descendant boundary fails validation deterministically.
    bad_closeout = json.loads(json.dumps(record_data))
    bad_closeout["capability_closeout"]["selection_boundary"]["children"][grandchild.id]["root_id"] = "other-root"
    with pytest.raises(DataCorruptionError):
        recovery._validated_capability_closeout(bad_closeout, owner_id=agent.home_paths.owner_id)

    # Recovery helper settles snapshot_pending and closes the orphaned grandchild.
    published = []
    updated = recovery._recover_parent_capability_closeout(record_data, agent, publish=published.append)
    assert updated["capability_closeout"]["acknowledged"] is True
    assert updated["capability_closeout"]["requests"] == {grandchild.id: [req_id]}
    assert agent.subagents.load(grandchild.id).status == "CANCELLED"
    assert agent.subagents.load(grandchild.id).capability_requests[0].status == "CLOSED"

    # Also test direct close_stale_capability_requests with explicit run_ids against a live parent child.
    live_child = agent.subagents.create_run(goal="live", parent_id=root_id, root_id=root_id, depth=1, role="worker")
    agent.subagents.mutate(live_child.id, lambda cur: setattr(cur, "status", "RUNNING"))
    protected_gc, _ = _make_blocked_run(agent, parent_id=live_child.id, root_id=root_id, depth=2)
    assert close_stale_capability_requests(
        agent.subagents, parent_task_id=root_id, reason="parent_turn_ended", run_ids=(protected_gc.id,)
    ) == []
    assert agent.subagents.load(protected_gc.id).capability_requests[0].status == "OPEN"


def test_runtime_stale_attempts_settle_orphan_capabilities_and_protects_unproven(tmp_path, monkeypatch, capsys):
    agent = SimpleAgent(
        AgentConfig(my_agent_home=str(tmp_path / "home"), model_backend="echo"),
        tmp_path / "workspace",
    )
    paths = gateway_paths(agent)
    ensure_gateway_folders(paths)
    paths.terminal.mkdir(parents=True, exist_ok=True)

    # Root 1 has a canonical terminal record on disk -> orphaned grandchild + direct child are provably stale.
    root_done = "gw-req-done"
    write_json_file_atomic(
        paths.terminal / f"{root_done}.json",
        {
            "schema_version": "gateway_terminal_request.v1",
            "id": root_done,
            "status": "done",
            "turn_phase": "closed",
        },
    )
    ended_child = agent.subagents.create_run(
        goal="ended parent", parent_id=root_done, root_id=root_done, depth=1, role="worker"
    )
    agent.subagents.mutate(ended_child.id, lambda cur: setattr(cur, "status", "DONE"))
    settled_gc, _ = _make_blocked_run(agent, parent_id=ended_child.id, root_id=root_done, depth=2)
    settled_direct, _ = _make_blocked_run(agent, parent_id=root_done, root_id=root_done, depth=1)

    # Even with terminal root, a grandchild whose direct parent is STILL RUNNING stays OPEN.
    live_parent = agent.subagents.create_run(
        goal="running parent", parent_id=root_done, root_id=root_done, depth=1, role="worker"
    )
    agent.subagents.mutate(live_parent.id, lambda cur: setattr(cur, "status", "RUNNING"))
    unproven_live_parent_gc, _ = _make_blocked_run(agent, parent_id=live_parent.id, root_id=root_done, depth=2)

    # Root 2 is still in processing (no terminal record) -> left OPEN with PARENT_TERMINAL_UNPROVEN.
    root_processing = "gw-req-processing"
    write_json_file_atomic(
        paths.processing / f"{root_processing}.json",
        {"id": root_processing, "status": "running"},
    )
    unproven_processing_child, _ = _make_blocked_run(
        agent, parent_id=root_processing, root_id=root_processing, depth=1
    )

    # Symlink terminal receipt must fail closed -> left OPEN with PARENT_TERMINAL_UNPROVEN.
    root_symlink = "gw-req-symlink"
    (paths.terminal / f"{root_symlink}.json").symlink_to(paths.terminal / f"{root_done}.json")
    unproven_symlink_child, _ = _make_blocked_run(
        agent, parent_id=root_symlink, root_id=root_symlink, depth=1
    )

    monkeypatch.setattr(cli_commands, "make_agent", lambda args: agent)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("unexpected interactive prompt"))

    args = SimpleNamespace(settle=True, older_than_days=1.0, json=True)
    assert cli_commands.cmd_runtime_stale_attempts(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert set(out["settled_capability_run_ids"]) == {settled_gc.id, settled_direct.id}
    unproven_by_id = {row["run_id"]: row["reason_code"] for row in out["unproven_capability_requests"]}
    assert unproven_by_id[unproven_live_parent_gc.id] == "PARENT_TERMINAL_UNPROVEN"
    assert unproven_by_id[unproven_processing_child.id] == "PARENT_TERMINAL_UNPROVEN"
    assert unproven_by_id[unproven_symlink_child.id] == "PARENT_TERMINAL_UNPROVEN"

    assert agent.subagents.load(settled_gc.id).status == "CANCELLED"
    assert agent.subagents.load(settled_gc.id).capability_requests[0].status == "CLOSED"
    assert agent.subagents.load(settled_direct.id).status == "CANCELLED"
    assert agent.subagents.load(unproven_live_parent_gc.id).capability_requests[0].status == "OPEN"
    assert agent.subagents.load(unproven_processing_child.id).capability_requests[0].status == "OPEN"
    assert agent.subagents.load(unproven_symlink_child.id).capability_requests[0].status == "OPEN"

    # Idempotent second run reports empty settled list cleanly.
    assert cli_commands.cmd_runtime_stale_attempts(args) == 0
    out_second = json.loads(capsys.readouterr().out)
    assert out_second["settled_capability_run_ids"] == []


@pytest.mark.parametrize("location", ["approvals", "private_interactions"])
def test_idle_approval_scan_resumes_beyond_1024_and_converges(tmp_path, monkeypatch, location):
    idle._IDLE_APPROVAL_SCAN_CURSORS.clear()
    storage_root = tmp_path / "conv"
    owner_home = tmp_path / "owner_home"
    thread = SimpleNamespace(thread_id="thread-tail", active_task_ids=())
    store = SimpleNamespace(storage=SimpleNamespace(root=storage_root))
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=owner_home))

    total_completed = 1520
    if location == "approvals":
        views_dir = storage_root / "subagent_tool_approvals" / ".views"
        views_dir.mkdir(parents=True)
        (views_dir / "ignored.json").write_text("{corrupt-views-ignored")
        target_dir = storage_root / "subagent_tool_approvals" / "root-tail"
        target_dir.mkdir(parents=True)
        pending_record = {"schema_version": "subagent_tool_approval.v1", "status": "pending", "thread_id": "thread-tail"}
    else:
        target_dir = owner_home / "data" / "private_interactions"
        target_dir.mkdir(parents=True)
        pending_record = {"kind": "model_configuration", "status": "active", "thread_id": "thread-tail"}

    for idx in range(total_completed):
        (target_dir / f"a-{idx:04d}.json").write_text(
            json.dumps({"status": "completed", "thread_id": "other-thread"})
        )
    pending_file = target_dir / "zzz-pending-tail.json"
    pending_file.write_text(json.dumps(pending_record))

    before = _fd_count()
    tick1 = idle._idle_pending_approval_state(agent, store, thread)
    assert _fd_count() - before == 0
    assert tick1 == "activity_unknown"

    states = [tick1]
    for _ in range(10):
        state = idle._idle_pending_approval_state(agent, store, thread)
        assert _fd_count() - before == 0
        states.append(state)
        if state == "pending_approval":
            break
    assert states[-1] == "pending_approval"

    # Resolve the tail pending record and ensure subsequent resumed ticks converge to "".
    write_json_file_atomic(pending_file, {"status": "completed", "thread_id": "thread-tail"})
    final_state = None
    for _ in range(10):
        final_state = idle._idle_pending_approval_state(agent, store, thread)
        assert _fd_count() - before == 0
        if final_state == "":
            break
    assert final_state == ""


def test_idle_approval_scan_detects_mid_round_mutation_and_tail_corruption(tmp_path):
    idle._IDLE_APPROVAL_SCAN_CURSORS.clear()
    storage_root = tmp_path / "conv"
    owner_home = tmp_path / "owner_home"
    thread = SimpleNamespace(thread_id="thread-mut", active_task_ids=())
    store = SimpleNamespace(storage=SimpleNamespace(root=storage_root))
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=owner_home))

    interactions = owner_home / "data" / "private_interactions"
    interactions.mkdir(parents=True)
    for idx in range(1200):
        (interactions / f"c-{idx:04d}.json").write_text(json.dumps({"status": "completed", "thread_id": "t"}))

    before = _fd_count()
    # Tick 1 scans first batch, returns activity_unknown.
    assert idle._idle_pending_approval_state(agent, store, thread) == "activity_unknown"
    assert _fd_count() - before == 0

    # Inject a new pending entry between ticks -> stamp change resets scan and discovers it instead of false idle.
    write_json_file_atomic(interactions / "0000-new-pending.json", {"status": "waiting_confirmation", "thread_id": "thread-mut"})
    observed = None
    for _ in range(10):
        observed = idle._idle_pending_approval_state(agent, store, thread)
        assert _fd_count() - before == 0
        if observed == "pending_approval":
            break
    assert observed == "pending_approval"

    # Replace with corrupt JSON at tail beyond 1024 -> fails closed with activity_unknown.
    (interactions / "0000-new-pending.json").unlink()
    write_json_file_atomic(interactions / "zzzz-corrupt-tail.json", {"status": "completed"})
    (interactions / "zzzz-corrupt-tail.json").write_text("{corrupt")
    outcomes = []
    for _ in range(5):
        outcomes.append(idle._idle_pending_approval_state(agent, store, thread))
        assert _fd_count() - before == 0
    assert "" not in outcomes
    assert all(item == "activity_unknown" for item in outcomes)


def test_idle_approval_scan_lru_cursor_cache_remains_bounded(tmp_path):
    idle._IDLE_APPROVAL_SCAN_CURSORS.clear()
    storage_root = tmp_path / "conv"
    owner_home = tmp_path / "owner_home"
    interactions = owner_home / "data" / "private_interactions"
    interactions.mkdir(parents=True)
    for idx in range(700):
        (interactions / f"m-{idx:04d}.json").write_text(json.dumps({"status": "completed", "thread_id": "t"}))
    store = SimpleNamespace(storage=SimpleNamespace(root=storage_root))
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=owner_home))

    for i in range(idle._IdleApprovalScanPolicy.max_cursors + 20):
        thread = SimpleNamespace(thread_id=f"thr-{i}", active_task_ids=())
        assert idle._idle_pending_approval_state(agent, store, thread) == "activity_unknown"
    assert len(idle._IDLE_APPROVAL_SCAN_CURSORS) <= idle._IdleApprovalScanPolicy.max_cursors
