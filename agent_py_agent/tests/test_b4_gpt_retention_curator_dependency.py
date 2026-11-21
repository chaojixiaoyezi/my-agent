import json

import pytest

from agent_py_agent.agent.memory_store.curator_state import new_curator_date_work
from agent_py_agent.tests.test_mem_wave1_s4_bounded_reads_retention import (
    _NOW,
    _ROWS,
    _old_thread,
    _plan_all,
    _retention_owner,
    _write_curator_state,
)


@pytest.mark.parametrize("dependency", ["ordinary_audit", "detached_audit", "detached_message"])
def test_retention_protects_unfinished_curator_conversation_dependencies(tmp_path, dependency):
    home, service = _retention_owner(tmp_path, profile="aux-profile")
    service.config.retention_scan_slice_ms = 5000
    thread, rows = _old_thread(home, _ROWS)
    state = {"per_thread_cursors": {thread.thread_id: rows[-1].message_id}}
    day = _NOW.date().isoformat()
    if dependency.endswith("audit"):
        home.owner_audit_dir.mkdir(parents=True, exist_ok=True)
        (home.owner_audit_dir / f"{day}.jsonl").write_text(json.dumps({
            "event_id": "audit-1", "thread_id": thread.thread_id, "created_at": _NOW.isoformat(),
        }) + "\n", encoding="utf-8")
    if dependency == "detached_audit":
        state["last_processed_audit_event_id"] = "audit-1"
        state["pending_local_dates"] = [new_curator_date_work(
            day, {"kind": "input", "messages": {}, "audit": "audit-1"}, {"messages": {}, "audit": ""},
        )]
    elif dependency == "detached_message":
        state["pending_local_dates"] = [new_curator_date_work(
            day, {"kind": "input", "messages": {thread.thread_id: rows[-1].message_id}, "audit": ""},
            {"messages": {thread.thread_id: ""}, "audit": ""},
        )]
    _write_curator_state(home, raw=json.dumps(state))

    report, actions, _ = _plan_all(service)

    assert not report.scan_partial
    assert actions == []


def test_live_curator_dependency_recovers_committed_trash_before_purge(tmp_path):
    from dataclasses import replace

    from agent_py_agent.agent.memory_store.retention_apply import execute_retention_plan

    home, service = _retention_owner(tmp_path, profile="")
    service.config.retention_scan_slice_ms = 5000
    thread, rows = _old_thread(home, _ROWS)
    _write_curator_state(home, {thread.thread_id: rows[-1].message_id})
    report, [action], _ = _plan_all(service)
    source_files = {path: path.read_bytes() for path in action.related_paths}
    applied = execute_retention_plan(home=home, candidates=service.candidates,
                                      plan=replace(report, actions=(action,)), now=_NOW, config=service.config)
    assert [row.status for row in applied.actions] == ["trashed"]
    assert action.destination.is_dir() and not action.path.exists()

    # The audit row survives independently of its canonical thread.
    service.config.memory_curator_model_profile = "aux-profile"
    home.owner_audit_dir.mkdir(parents=True, exist_ok=True)
    (home.owner_audit_dir / "late.jsonl").write_text(json.dumps({
        "event_id": "late", "thread_id": thread.thread_id, "created_at": _NOW.isoformat(),
    }) + "\n", encoding="utf-8")
    report, _, _ = _plan_all(service)

    assert [item.operation for item in report.actions if item.path == action.destination] == ["conversation_restore"]
    applied = execute_retention_plan(home=home, candidates=service.candidates, plan=report,
                                      now=_NOW, config=service.config)
    assert [(item.record_id, item.status) for item in applied.actions if item.path == action.destination] == [
        (thread.thread_id, "restored"),
    ]
    assert {path: path.read_bytes() for path in source_files} == source_files
    assert not action.destination.exists()


def test_purge_revalidates_curator_dependencies_after_planning(tmp_path):
    from dataclasses import replace
    from datetime import timedelta

    from agent_py_agent.agent.memory_store.retention_apply import execute_retention_plan

    home, service = _retention_owner(tmp_path, profile="aux-profile")
    service.config.retention_scan_slice_ms = 5000
    thread, rows = _old_thread(home, _ROWS)
    _write_curator_state(home, {thread.thread_id: rows[-1].message_id})
    plan, [action], _ = _plan_all(service)
    moved = execute_retention_plan(home=home, candidates=service.candidates,
                                  plan=replace(plan, actions=(action,)), now=_NOW, config=service.config)
    assert [item.status for item in moved.actions] == ["trashed"]
    later = _NOW + timedelta(days=31)
    cursor = None
    for _ in range(256):
        plan = service.plan(now=later, scan_cursor=cursor)
        purge = tuple(item for item in plan.actions if item.path == action.destination)
        if purge or not plan.scan_partial:
            break
        cursor = plan.scan_cursor
    assert [item.operation for item in purge] == ["delete_tree"]

    home.owner_audit_dir.mkdir(parents=True, exist_ok=True)
    (home.owner_audit_dir / "pending.jsonl").write_text(json.dumps({
        "event_id": "audit-pending", "thread_id": thread.thread_id, "created_at": later.isoformat(),
    }) + "\n", encoding="utf-8")
    for _ in range(8):
        result = execute_retention_plan(home=home, candidates=service.candidates,
                                        plan=replace(plan, actions=purge), now=later, config=service.config)
        assert action.destination.is_dir()
        if result.actions and result.actions[0].status != "not_started":
            break
    assert [item.status for item in result.actions] == ["state_changed"]

    # Represent an older deployment that purged the payload before this fix.
    import shutil

    shutil.rmtree(action.destination)
    report, _, _ = _plan_all(service)
    assert any(error.error_code == "MEMORY_RETENTION_CURATOR_SOURCE_PURGED" for error in report.errors)
    assert not action.path.exists()
