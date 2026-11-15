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
    from pathlib import Path

    idle._IDLE_APPROVAL_SCAN_CURSORS.clear()
    storage_root = tmp_path / "conv"
    owner_home = tmp_path / "owner_home"
    thread = SimpleNamespace(thread_id="thread-tail", active_task_ids=())
    store = SimpleNamespace(storage=SimpleNamespace(root=storage_root))
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=owner_home))

    total_completed = 1521
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
    with os.scandir(target_dir) as it:
        json_paths = [Path(e.path) for e in it if e.name.endswith(".json")]
    pending_file = json_paths[-1]
    pending_file.write_text(json.dumps(pending_record), encoding="utf-8")

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


def test_idle_approval_scan_multi_tick_subdir_overflow_fails_closed(tmp_path, monkeypatch):
    idle._IDLE_APPROVAL_SCAN_CURSORS.clear()
    monkeypatch.setattr(idle._IdleApprovalScanPolicy, "max_tracked_subdirs", 4)

    storage_root = tmp_path / "conv"
    owner_home = tmp_path / "owner_home"
    approvals_dir = storage_root / "subagent_tool_approvals"
    for idx in range(6):
        subdir = approvals_dir / f"root-{idx:02d}"
        subdir.mkdir(parents=True)
        for item_idx in range(15):
            (subdir / f"done-{item_idx:02d}.json").write_text(
                json.dumps({"status": "completed", "thread_id": "other-thread"})
            )

    thread = SimpleNamespace(thread_id="thr-overflow", active_task_ids=())
    store = SimpleNamespace(storage=SimpleNamespace(root=storage_root))
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=owner_home),
        config=SimpleNamespace(retention_scan_slice_entries=90),
    )
    key = (str(storage_root), str(owner_home), "thr-overflow")

    # Tick 1 scans >4 subdirs (overflowing max_tracked_subdirs=4) and pauses before round end.
    assert idle._idle_pending_approval_state(agent, store, thread) == "activity_unknown"
    saved_state = idle._IDLE_APPROVAL_SCAN_CURSORS[key]
    assert saved_state["subdir_overflow"] is True
    tracked = set(saved_state["subdir_stamps"])
    untracked_scanned = [
        p.name
        for p in sorted(approvals_dir.iterdir())
        if p.name not in tracked and p.name != saved_state["active_root"]
    ]
    assert untracked_scanned

    # Write a live pending approval inside an already-scanned untracked subdir (parent dir stamp unchanged).
    (approvals_dir / untracked_scanned[0] / "live-pending.json").write_text(
        json.dumps({"schema_version": "subagent_tool_approval.v1", "status": "pending", "thread_id": "thr-overflow"})
    )

    # Tick 2 completes round with subdir_overflow across >1 ticks -> fails closed (never ""), then fresh scan finds it.
    tick2 = idle._idle_pending_approval_state(agent, store, thread)
    assert tick2 == "activity_unknown"
    assert tick2 != ""

    discovered = None
    for _ in range(5):
        discovered = idle._idle_pending_approval_state(agent, store, thread)
        if discovered == "pending_approval":
            break
    assert discovered == "pending_approval"


@pytest.mark.parametrize("location", ["active_root", "confirmations"])
def test_idle_approval_scan_portable_replay_preserves_cursor_across_eviction(tmp_path, monkeypatch, location):
    from agent_py_agent.agent.user_space import directory_paging

    idle._IDLE_APPROVAL_SCAN_CURSORS.clear()
    with directory_paging._SCAN_LOCK:
        while directory_paging._SCAN_STREAMS:
            _, evicted = directory_paging._SCAN_STREAMS.popitem(last=False)
            evicted[0].close()

    monkeypatch.setattr(directory_paging, "_native_directory_supported", lambda: False)

    storage_root = tmp_path / "conv"
    owner_home = tmp_path / "owner_home"
    if location == "active_root":
        target_dir = storage_root / "subagent_tool_approvals" / "root-portable"
    else:
        target_dir = owner_home / "data" / "private_interactions"
    target_dir.mkdir(parents=True)
    for idx in range(350):
        (target_dir / f"item-{idx:04d}.json").write_text(
            json.dumps({"status": "completed", "thread_id": "other-thread"})
        )

    thread = SimpleNamespace(thread_id="thr-portable", active_task_ids=())
    store = SimpleNamespace(storage=SimpleNamespace(root=storage_root))
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=owner_home),
        config=SimpleNamespace(retention_scan_slice_entries=100),
    )
    key = (str(storage_root), str(owner_home), "thr-portable")

    before_fds = _fd_count()
    # Two bounded ticks advance visited cursor to >= 199 before stream eviction.
    assert idle._idle_pending_approval_state(agent, store, thread) == "activity_unknown"
    assert idle._idle_pending_approval_state(agent, store, thread) == "activity_unknown"
    state = idle._IDLE_APPROVAL_SCAN_CURSORS[key]
    cursor_field = "active_root_cursor" if location == "active_root" else "confirmations_cursor"
    saved_visited = state[cursor_field]["visited"]
    assert saved_visited >= 199

    # Evict _SCAN_STREAMS to force portable replay.
    with directory_paging._SCAN_LOCK:
        while directory_paging._SCAN_STREAMS:
            _, evicted = directory_paging._SCAN_STREAMS.popitem(last=False)
            evicted[0].close()

    page_calls = 0
    real_page = idle.directory_page

    def counted_page(*args, **kwargs):
        nonlocal page_calls
        page_calls += 1
        return real_page(*args, **kwargs)

    monkeypatch.setattr(idle, "directory_page", counted_page)

    # Pure-skip replay tick performs 1 directory_page call and preserves cursor in _IDLE_APPROVAL_SCAN_CURSORS.
    replay_verdict = idle._idle_pending_approval_state(agent, store, thread)
    assert replay_verdict == "activity_unknown"
    assert page_calls == 1
    assert key in idle._IDLE_APPROVAL_SCAN_CURSORS
    assert idle._IDLE_APPROVAL_SCAN_CURSORS[key][cursor_field]["visited"] == saved_visited

    # Subsequent ticks finish replay and converge cleanly to "" with no leaked FDs.
    converged = None
    for _ in range(10):
        converged = idle._idle_pending_approval_state(agent, store, thread)
        if converged == "":
            break
    assert converged == ""
    assert _fd_count() - before_fds == 0

    # Adding a pending approval is discovered across subsequent portable ticks, and resolving it converges to "".
    pending_path = target_dir / "pending-portable.json"
    write_json_file_atomic(pending_path, {"status": "pending", "thread_id": "thr-portable"})
    found = None
    for _ in range(10):
        found = idle._idle_pending_approval_state(agent, store, thread)
        if found == "pending_approval":
            break
    assert found == "pending_approval"

    write_json_file_atomic(pending_path, {"status": "completed", "thread_id": "thr-portable"})
    final_clean = None
    for _ in range(10):
        final_clean = idle._idle_pending_approval_state(agent, store, thread)
        if final_clean == "":
            break
    assert final_clean == ""
    assert _fd_count() - before_fds == 0


def _flush_portable_directory_streams() -> None:
    from agent_py_agent.agent.user_space import directory_paging

    with directory_paging._SCAN_LOCK:
        assert len(directory_paging._SCAN_STREAMS) <= 32
        while directory_paging._SCAN_STREAMS:
            _, cached = directory_paging._SCAN_STREAMS.popitem(last=False)
            cached[0].close()


def test_idle_approval_scan_4097_terminal_roots_converges_while_catching_4097th_pending(tmp_path):
    from agent_py_agent.agent.conversation import ConversationStore
    from agent_py_agent.agent.conversation.models import ThreadTaskLink
    from agent_py_agent.agent.subagents.services.persistence.service import SubAgentListRunsReport

    idle._IDLE_APPROVAL_SCAN_CURSORS.clear()
    _flush_portable_directory_streams()
    before_fds = _fd_count()
    owner_dir = tmp_path / "owner"
    storage_root = owner_dir / "conversations"
    store = ConversationStore(storage_root)
    thread = store.threads.create(canonical_user_id="user")
    approvals_dir = store.storage.root / "subagent_tool_approvals"
    approvals_dir.mkdir(parents=True, exist_ok=True)
    total_roots = idle._IdleApprovalScanPolicy.max_tracked_subdirs + 1
    assert total_roots == 4097
    first_task_path = store.storage.task_path("root-0000")
    first_task_path.parent.mkdir(parents=True, exist_ok=True)
    for idx in range(total_roots):
        root_id = f"root-{idx:04d}"
        link = ThreadTaskLink(thread_id=thread.thread_id, task_id=root_id, goal="done", status="completed")
        store.storage.task_path(root_id).write_text(json.dumps(link.to_dict()), encoding="utf-8")
        (approvals_dir / root_id).mkdir()

    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=owner_dir),
        config=AgentConfig(retention_scan_slice_entries=128),
        subagents=SimpleNamespace(
            list_runs_for_root_report=lambda _rid: SubAgentListRunsReport(runs=[], load_errors=[]),
        ),
    )

    state_result = "activity_unknown"
    for _ in range(256):
        state_result = idle._idle_pending_approval_state(agent, store, thread)
        if state_result != "activity_unknown":
            break
    assert state_result == ""
    key = (str(store.storage.root), str(owner_dir), str(thread.thread_id))
    assert idle._IDLE_APPROVAL_SCAN_CURSORS[key]["phase"] == "done"
    assert idle._IDLE_APPROVAL_SCAN_CURSORS[key]["subdir_overflow"] is False
    assert idle._IDLE_APPROVAL_SCAN_CURSORS[key]["subdir_stamps"] == {}
    assert idle._idle_pending_approval_state(agent, store, thread) == ""

    idle._IDLE_APPROVAL_SCAN_CURSORS.clear()
    with os.scandir(approvals_dir) as it:
        tail_root = [e.name for e in it if not e.name.startswith(".")][-1]
    late_pending = approvals_dir / tail_root / "req-late.json"
    late_pending.write_text(json.dumps({"status": "pending", "thread_id": thread.thread_id}), encoding="utf-8")
    caught = "activity_unknown"
    for _ in range(256):
        caught = idle._idle_pending_approval_state(agent, store, thread)
        if caught != "activity_unknown":
            break
    assert caught == "pending_approval"

    late_pending.unlink()
    idle._IDLE_APPROVAL_SCAN_CURSORS.clear()
    assert idle._idle_pending_approval_state(agent, store, thread) == "activity_unknown"
    mid_dir = approvals_dir / "root-9999-mid"
    mid_dir.mkdir()
    (mid_dir / "req-mid.json").write_text(
        json.dumps({"status": "pending", "thread_id": thread.thread_id}),
        encoding="utf-8",
    )
    mid_caught = "activity_unknown"
    for _ in range(256):
        mid_caught = idle._idle_pending_approval_state(agent, store, thread)
        if mid_caught != "activity_unknown":
            break
    assert mid_caught == "pending_approval"
    _flush_portable_directory_streams()
    assert _fd_count() - before_fds == 0


def test_idle_approval_scan_terminal_exemption_still_enforces_non_terminal_and_open_subagent_overflow(
    tmp_path, monkeypatch
):
    from agent_py_agent.agent.conversation import ConversationStore
    from agent_py_agent.agent.conversation.models import ThreadTaskLink
    from agent_py_agent.agent.subagents.model_capabilities import CapabilityRequest
    from agent_py_agent.agent.subagents.models import TaskStatus
    from agent_py_agent.agent.subagents.services.persistence.service import SubAgentListRunsReport

    idle._IDLE_APPROVAL_SCAN_CURSORS.clear()
    owner_dir = tmp_path / "owner"
    store = ConversationStore(owner_dir / "conversations")
    thread = store.threads.create(canonical_user_id="user")
    approvals_dir = store.storage.root / "subagent_tool_approvals"
    approvals_dir.mkdir(parents=True, exist_ok=True)
    store.storage.task_path("seed").parent.mkdir(parents=True, exist_ok=True)

    for root_id, link_status in (
        ("root-active", "active"),
        ("root-interrupted", "interrupted"),
        ("root-done-1", "completed"),
        ("root-done-2", "completed"),
        ("root-done-3", "completed"),
        ("root-open-run", "completed"),
        ("root-open-cap", "completed"),
        ("root-err-run", "completed"),
        ("root-settled", "completed"),
    ):
        link = ThreadTaskLink(thread_id=thread.thread_id, task_id=root_id, goal="g", status=link_status)
        store.storage.task_path(root_id).write_text(json.dumps(link.to_dict()), encoding="utf-8")

    reports = {
        "root-open-run": SubAgentListRunsReport(
            runs=[SimpleNamespace(status=TaskStatus.RUNNING, capability_requests=())],
            load_errors=[],
        ),
        "root-open-cap": SubAgentListRunsReport(
            runs=[
                SimpleNamespace(
                    status=TaskStatus.DONE,
                    capability_requests=[
                        CapabilityRequest(
                            id="cap-1",
                            from_run_id="sub-1",
                            problem="need",
                            needed_capability="tool",
                            status="OPEN",
                        )
                    ],
                )
            ],
            load_errors=[],
        ),
        "root-err-run": SubAgentListRunsReport(runs=[], load_errors=[{"error": "corrupt"}]),
        "root-settled": SubAgentListRunsReport(
            runs=[
                SimpleNamespace(
                    status=TaskStatus.DONE,
                    capability_requests=[
                        CapabilityRequest(
                            id="cap-closed",
                            from_run_id="sub-2",
                            problem="need",
                            needed_capability="tool",
                            status="CLOSED",
                        )
                    ],
                )
            ],
            load_errors=[],
        ),
    }
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=owner_dir),
        config=AgentConfig(retention_scan_slice_entries=1),
        subagents=SimpleNamespace(
            list_runs_for_root_report=lambda rid: reports.get(rid, SubAgentListRunsReport(runs=[], load_errors=[])),
        ),
    )

    assert idle._root_is_settled_terminal_history(agent, store, "root-active") is False
    assert idle._root_is_settled_terminal_history(agent, store, "root-interrupted") is False
    assert idle._root_is_settled_terminal_history(agent, store, "root-missing") is False
    assert idle._root_is_settled_terminal_history(agent, store, "root-open-run") is False
    assert idle._root_is_settled_terminal_history(agent, store, "root-open-cap") is False
    assert idle._root_is_settled_terminal_history(agent, store, "root-err-run") is False
    assert idle._root_is_settled_terminal_history(agent, store, "root-settled") is True

    monkeypatch.setattr(idle._IdleApprovalScanPolicy, "max_tracked_subdirs", 2)
    for root_id in ("root-done-1", "root-done-2", "root-done-3", "root-active", "root-interrupted"):
        (approvals_dir / root_id).mkdir()

    converged = "activity_unknown"
    for _ in range(40):
        converged = idle._idle_pending_approval_state(agent, store, thread)
        if converged != "activity_unknown":
            break
    assert converged == ""

    (approvals_dir / "root-missing").mkdir()
    idle._IDLE_APPROVAL_SCAN_CURSORS.clear()
    for _ in range(40):
        overflow_state = idle._idle_pending_approval_state(agent, store, thread)
        if overflow_state != "activity_unknown":
            break
    assert idle._idle_pending_approval_state(agent, store, thread) == "activity_unknown"


def test_idle_compact_maintenance_tick_preserves_in_progress_cursor_across_256_plus_threads(
    tmp_path, monkeypatch
):
    from agent_py_agent.agent.conversation import ConversationStore
    from agent_py_agent.agent.conversation.models import ThreadTaskLink
    from agent_py_agent.agent.subagents.services.persistence.service import SubAgentListRunsReport

    idle._IDLE_APPROVAL_SCAN_CURSORS.clear()
    _flush_portable_directory_streams()
    before_fds = _fd_count()
    owner_dir = tmp_path / "owner"
    store = ConversationStore(owner_dir / "conversations")
    approvals_dir = store.storage.root / "subagent_tool_approvals"
    approvals_dir.mkdir(parents=True, exist_ok=True)
    store.storage.task_path("seed").parent.mkdir(parents=True, exist_ok=True)

    threads = []
    for _ in range(idle._IdleApprovalScanPolicy.max_cursors + 14):
        thr = store.threads.create(canonical_user_id="user")
        thr = store.threads.record_user_activity(thr.thread_id, now=100.0)
        threads.append(thr)

    for idx in range(12):
        root_id = f"active-root-{idx:02d}"
        link = ThreadTaskLink(thread_id=threads[0].thread_id, task_id=root_id, goal="work", status="active")
        store.storage.task_path(root_id).write_text(json.dumps(link.to_dict()), encoding="utf-8")
        (approvals_dir / root_id).mkdir()

    def _fake_prepare_factory(recorded: list[str], *, capture_storage_root: bool = False):
        def _fake_prepare(_agent, conv_store, thread, *, options=None):
            recorded.append(str(conv_store.storage.root) if capture_storage_root else thread.thread_id)
            return SimpleNamespace(
                compacted=True,
                thread=conv_store.threads.require(thread.thread_id),
                messages=(),
                projected_tokens=800_000,
                trigger_tokens=600_000,
            )

        return _fake_prepare

    compacted_ids: list[str] = []
    monkeypatch.setattr(
        idle,
        "list_background_processes",
        lambda *_a, **_k: {"projection_ok": True, "items": []},
    )
    monkeypatch.setattr(
        idle,
        "inspect_conversation_context",
        lambda *_a, **_k: SimpleNamespace(projected_tokens=800_000, context_window_tokens=1_000_000),
    )
    monkeypatch.setattr(idle, "prepare_conversation_context", _fake_prepare_factory(compacted_ids))
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=owner_dir),
        config=AgentConfig(
            retention_scan_slice_entries=4,
            memory_compact_idle_max_per_tick=2,
            memory_compact_idle_trigger_percent=50,
            memory_compact_idle_seconds=1800,
        ),
        conversation_store=store,
        backend=object(),
        subagents=SimpleNamespace(
            list_runs=lambda: [],
            list_runs_report=lambda: SimpleNamespace(runs=[], load_errors=[]),
            list_runs_for_root_report=lambda _rid: SubAgentListRunsReport(runs=[], load_errors=[]),
        ),
    )

    tick_cursor: dict[str, object] = {}
    for _ in range(32):
        report = idle.idle_compact_maintenance_tick(agent, cursor=tick_cursor, now=7400.0)
        assert report["failed"] == 0
        if compacted_ids:
            break
    assert compacted_ids, "continuation priority must converge threads before >256 cursor eviction"
    assert all(isinstance(tid, str) and tid.startswith("thread-") for tid in compacted_ids)

    first_thread_id = threads[0].thread_id
    idle._IDLE_APPROVAL_SCAN_CURSORS.clear()
    idle._save_idle_scan_state(
        (str(store.storage.root), str(owner_dir), first_thread_id),
        {
            "phase": "approvals",
            "approvals_stamp": (0, 0),
            "confirmations_stamp": None,
            "subdir_stamps": {},
            "subdir_overflow": False,
            "roots_cursor": {},
            "roots_complete": False,
            "active_root": None,
            "active_root_stamp": None,
            "active_root_cursor": {},
            "active_root_complete": False,
            "pending_files": [],
            "visited_count": 1,
            "ticks": 1,
            "hit_path": None,
        },
    )
    store.threads.record_user_activity(first_thread_id, now=7399.0)
    compacted_after = len(compacted_ids)
    for _ in range(32):
        idle.idle_compact_maintenance_tick(agent, cursor=tick_cursor, now=7400.0)
        if len(compacted_ids) > compacted_after:
            break
    assert len(compacted_ids) > compacted_after
    key_first = (str(store.storage.root), str(owner_dir), first_thread_id)
    assert key_first not in idle._IDLE_APPROVAL_SCAN_CURSORS

    scans = tick_cursor.get("directories", {})
    for scan in scans.values():
        scan.close()
    _flush_portable_directory_streams()
    assert _fd_count() - before_fds == 0


def test_idle_compact_maintenance_tick_retains_continuation_after_single_mid_scan_mutation(
    tmp_path, monkeypatch
):
    from agent_py_agent.agent.conversation import ConversationStore
    from agent_py_agent.agent.conversation.models import ThreadTaskLink
    from agent_py_agent.agent.subagents.services.persistence.service import SubAgentListRunsReport

    idle._IDLE_APPROVAL_SCAN_CURSORS.clear()
    _flush_portable_directory_streams()
    before_fds = _fd_count()
    owner_dir = tmp_path / "owner"
    store = ConversationStore(owner_dir / "conversations")
    approvals_dir = store.storage.root / "subagent_tool_approvals"
    approvals_dir.mkdir(parents=True, exist_ok=True)
    store.storage.task_path("seed").parent.mkdir(parents=True, exist_ok=True)

    threads = []
    for _ in range(idle._IdleApprovalScanPolicy.max_cursors + 14):
        thr = store.threads.create(canonical_user_id="user")
        thr = store.threads.record_user_activity(thr.thread_id, now=100.0)
        threads.append(thr)

    for idx in range(12):
        root_id = f"active-root-{idx:02d}"
        link = ThreadTaskLink(thread_id=threads[0].thread_id, task_id=root_id, goal="work", status="active")
        store.storage.task_path(root_id).write_text(json.dumps(link.to_dict()), encoding="utf-8")
        (approvals_dir / root_id).mkdir()

    compacted_ids: list[str] = []
    monkeypatch.setattr(
        idle,
        "list_background_processes",
        lambda *_a, **_k: {"projection_ok": True, "items": []},
    )
    monkeypatch.setattr(
        idle,
        "inspect_conversation_context",
        lambda *_a, **_k: SimpleNamespace(projected_tokens=800_000, context_window_tokens=1_000_000),
    )
    monkeypatch.setattr(
        idle,
        "prepare_conversation_context",
        lambda _agent, conv_store, thread, *, options=None: (
            compacted_ids.append(thread.thread_id)
            or SimpleNamespace(
                compacted=True,
                thread=conv_store.threads.require(thread.thread_id),
                messages=(),
                projected_tokens=800_000,
                trigger_tokens=600_000,
            )
        ),
    )

    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=owner_dir),
        config=AgentConfig(
            retention_scan_slice_entries=4,
            memory_compact_idle_max_per_tick=1,
            memory_compact_idle_trigger_percent=50,
            memory_compact_idle_seconds=1800,
        ),
        conversation_store=store,
        backend=object(),
        subagents=SimpleNamespace(
            list_runs=lambda: [],
            list_runs_report=lambda: SimpleNamespace(runs=[], load_errors=[]),
            list_runs_for_root_report=lambda _rid: SubAgentListRunsReport(runs=[], load_errors=[]),
        ),
    )

    tick_cursor: dict[str, object] = {}
    for _ in range(16):
        assert idle.idle_compact_maintenance_tick(agent, cursor=tick_cursor, now=7400.0)["failed"] == 0
        if idle._IDLE_APPROVAL_SCAN_CURSORS:
            break
    assert len(idle._IDLE_APPROVAL_SCAN_CURSORS) == 1
    target_key = next(iter(idle._IDLE_APPROVAL_SCAN_CURSORS))
    assert idle._IDLE_APPROVAL_SCAN_CURSORS[target_key]["restarts"] == 0

    (approvals_dir / "active-root-extra").mkdir()
    assert idle.idle_compact_maintenance_tick(agent, cursor=tick_cursor, now=7400.0)["failed"] == 0
    assert target_key in idle._IDLE_APPROVAL_SCAN_CURSORS
    assert idle._IDLE_APPROVAL_SCAN_CURSORS[target_key]["restarts"] == 1
    assert idle._IDLE_APPROVAL_SCAN_CURSORS[target_key]["phase"] != "done"

    for _ in range(24):
        assert idle.idle_compact_maintenance_tick(agent, cursor=tick_cursor, now=7400.0)["failed"] == 0
        if target_key[2] in compacted_ids:
            break
    assert target_key[2] in compacted_ids

    for scan in tick_cursor.get("directories", {}).values():
        scan.close()
    _flush_portable_directory_streams()
    assert _fd_count() - before_fds == 0


def test_idle_compact_maintenance_tick_preserves_in_progress_cursor_across_256_plus_active_owners(
    tmp_path, monkeypatch
):
    from contextlib import nullcontext

    from agent_py_agent.agent.conversation import ConversationStore
    from agent_py_agent.agent.subagents.services.persistence.service import SubAgentListRunsReport

    idle._IDLE_APPROVAL_SCAN_CURSORS.clear()
    _flush_portable_directory_streams()
    before_fds = _fd_count()

    compacted_owners: list[str] = []
    monkeypatch.setattr(
        idle,
        "list_background_processes",
        lambda *_a, **_k: {"projection_ok": True, "items": []},
    )
    monkeypatch.setattr(
        idle,
        "inspect_conversation_context",
        lambda *_a, **_k: SimpleNamespace(projected_tokens=800_000, context_window_tokens=1_000_000),
    )

    def _fake_prepare(_agent, conv_store, thread, *, options=None):
        compacted_owners.append(str(conv_store.storage.root))
        return SimpleNamespace(
            compacted=True,
            thread=conv_store.threads.require(thread.thread_id),
            messages=(),
            projected_tokens=800_000,
            trigger_tokens=600_000,
        )

    monkeypatch.setattr(idle, "prepare_conversation_context", _fake_prepare)

    shared_config = AgentConfig(
        retention_scan_slice_entries=4,
        memory_compact_idle_max_per_tick=2,
        memory_compact_idle_trigger_percent=50,
        memory_compact_idle_seconds=1800,
    )
    shared_subagents = SimpleNamespace(
        list_runs=lambda: [],
        list_runs_report=lambda: SimpleNamespace(runs=[], load_errors=[]),
        list_runs_for_root_report=lambda _rid: SubAgentListRunsReport(runs=[], load_errors=[]),
    )

    owners = []
    total_owners = idle._IdleApprovalScanPolicy.max_cursors + 14
    for idx in range(total_owners):
        owner_dir = tmp_path / f"owner-{idx:03d}"
        store = ConversationStore(owner_dir / "conversations")
        thr = store.threads.create(canonical_user_id=f"user-{idx}")
        store.threads.record_user_activity(thr.thread_id, now=100.0)
        approvals_dir = store.storage.root / "subagent_tool_approvals"
        approvals_dir.mkdir(parents=True, exist_ok=True)
        for sub_idx in range(8):
            (approvals_dir / f"sub-{sub_idx:02d}").mkdir()
        owners.append(
            SimpleNamespace(
                home_paths=SimpleNamespace(owner_home_dir=owner_dir),
                config=shared_config,
                conversation_store=store,
                backend=object(),
                subagents=shared_subagents,
            )
        )

    base_agent = owners[0]
    base_agent._owner_pool = SimpleNamespace(
        active_agents=lambda: owners[1:],
        pin=lambda *_a, **_k: nullcontext(),
    )

    tick_cursor: dict[str, object] = {}
    for _ in range(24):
        report = idle.idle_compact_maintenance_tick(base_agent, cursor=tick_cursor, now=7400.0)
        assert report["failed"] == 0
        if compacted_owners:
            break
    assert compacted_owners, "multi-owner pool must finish active continuation cursors before >256 eviction"
    assert len(idle._IDLE_APPROVAL_SCAN_CURSORS) < 16

    for scan in tick_cursor.get("directories", {}).values():
        scan.close()
    _flush_portable_directory_streams()
    assert _fd_count() - before_fds == 0
