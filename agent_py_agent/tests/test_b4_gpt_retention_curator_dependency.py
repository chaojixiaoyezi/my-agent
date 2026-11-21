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


@pytest.mark.parametrize("publication", ["ok", "before_append", "after_append", "torn_append", "legacy_missing"])
def test_live_curator_dependency_recovers_committed_trash_before_purge(tmp_path, monkeypatch, publication):
    from agent_py_agent.agent.memory_store import retention_apply
    from agent_py_agent.agent.memory_store.retired_thread_journal import iter_retired_threads

    append = retention_apply.append_retired_thread

    def publish(*args, **kwargs):
        if publication in {"ok", "after_append"}:
            append(*args, **kwargs)
        if publication == "torn_append":
            journal = args[0].owner_data_dir / "retention" / "retired_threads.jsonl"
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_bytes(b'{"schema":"retired_thread.v1",')
        if publication != "ok":
            raise OSError("injected retirement journal failure")

    with monkeypatch.context() as patch:
        patch.setattr(retention_apply, "append_retired_thread", publish)
        home, service, action, original = _retire_thread_with_late_audit(tmp_path)

    if publication == "legacy_missing":
        tombstone = action.destination / "tombstone.json"
        metadata = json.loads(tombstone.read_text(encoding="utf-8"))
        metadata.pop("members", None)
        tombstone.write_text(json.dumps(metadata), encoding="utf-8")

    report, _, _ = _plan_all(service)
    assert [item.operation for item in report.actions if item.path == action.destination] == ["conversation_restore"]
    _assert_restored(home, service, report, action, original)
    assert [row["action_id"] for row in iter_retired_threads(home).rows] == [action.action_id]


def _retire_thread_with_late_audit(tmp_path):
    from dataclasses import replace

    from agent_py_agent.agent.memory_store.retention_apply import execute_retention_plan

    home, service = _retention_owner(tmp_path, profile="")
    service.config.retention_scan_slice_ms = 5000
    thread, rows = _old_thread(home, _ROWS)
    _write_curator_state(home, {thread.thread_id: rows[-1].message_id})
    report, [action], _ = _plan_all(service)
    original = {path: path.read_bytes() for path in action.related_paths}
    applied = execute_retention_plan(home=home, candidates=service.candidates,
                                      plan=replace(report, actions=(action,)), now=_NOW, config=service.config)
    assert [row.status for row in applied.actions] == ["trashed"]
    assert action.destination.is_dir() and not action.path.exists()

    service.config.memory_curator_model_profile = "aux-profile"
    home.owner_audit_dir.mkdir(parents=True, exist_ok=True)
    (home.owner_audit_dir / "late.jsonl").write_text(json.dumps({
        "event_id": "late", "thread_id": thread.thread_id, "created_at": _NOW.isoformat(),
    }) + "\n", encoding="utf-8")
    return home, service, action, original


def _execute_restore(home, service, report, action):
    from dataclasses import replace

    from agent_py_agent.agent.memory_store.retention_apply import execute_retention_plan

    plan = replace(report, actions=tuple(item for item in report.actions if item.path == action.destination))
    assert len(plan.actions) == 1
    for _ in range(16):
        applied = execute_retention_plan(home=home, candidates=service.candidates, plan=plan,
                                          now=_NOW, config=service.config)
        if applied.actions[0].status != "not_started":
            return applied
    pytest.fail("reference pins did not settle for the restore")


def _assert_restored(home, service, report, action, original):
    applied = _execute_restore(home, service, report, action)
    assert [(item.record_id, item.status) for item in applied.actions] == [(action.record_id, "restored")], applied.errors
    assert {path: path.read_bytes() for path in original} == original
    assert not action.destination.exists()
    followup, _, _ = _plan_all(service)
    assert all(item.path != action.destination for item in followup.actions)


@pytest.mark.parametrize("interrupt", ["first_move", "metadata_move", "legacy_marker", "ordinary_io", "metadata_ack"])
def test_curator_restore_recovers_interrupted_family(tmp_path, monkeypatch, interrupt):
    from agent_py_agent.agent.memory_store import retention_apply

    class Interrupted(BaseException):
        pass

    home, service, action, original = _retire_thread_with_late_audit(tmp_path)
    report, _, _ = _plan_all(service)
    [restore] = [item for item in report.actions if item.path == action.destination]
    move = retention_apply._move_retention_payload
    if interrupt == "legacy_marker":
        marker = action.destination / retention_apply._TRASH_COMMIT_PENDING
        marker.write_text(restore.action_id + "\n", encoding="utf-8")
        tombstone = action.destination / "tombstone.json"
        metadata = json.loads(tombstone.read_text(encoding="utf-8"))
        metadata.pop("members", None)
        tombstone.write_text(json.dumps(metadata), encoding="utf-8")
        messages = next(path for path in original if path.parent.name == "messages")
        move(action.destination / "payload" / messages.relative_to(restore.destination), messages)
    else:
        def interrupt_after_move(source, target):
            if interrupt == "ordinary_io" and target == action.path:
                raise OSError("injected restore error")
            move(source, target)
            if interrupt == "metadata_ack" and target == action.path:
                raise OSError("injected metadata move acknowledgment loss")
            if interrupt == "first_move" or interrupt == "metadata_move" and target == action.path:
                raise Interrupted

        with monkeypatch.context() as patch:
            patch.setattr(retention_apply, "_move_retention_payload", interrupt_after_move)
            if interrupt in {"ordinary_io", "metadata_ack"}:
                failed = _execute_restore(home, service, report, action)
                assert failed.errors
                assert all(path.exists() == (interrupt == "metadata_ack") for path in original)
            else:
                with pytest.raises(Interrupted):
                    _execute_restore(home, service, report, action)
                assert action.path.exists() == (interrupt == "metadata_move")
                assert action.destination.is_dir()

    service = type(service)(home_paths=home, candidates=service.candidates, config=service.config)
    reopened, _, _ = _plan_all(service)
    assert [item.operation for item in reopened.actions if item.path == action.destination] == ["conversation_restore"]
    _assert_restored(home, service, reopened, action, original)


def test_curator_restore_preserves_conflicting_canonical_member(tmp_path):
    home, service, action, original = _retire_thread_with_late_audit(tmp_path)
    messages = next(path for path in original if path.parent.name == "messages")
    unrecognized = b"canonical data from an independent writer\n"
    messages.write_bytes(unrecognized)
    plan, _, _ = _plan_all(service)

    result = _execute_restore(home, service, plan, action)
    assert any("CURATOR_RETENTION_RESTORE_UNRESOLVED" in error.message for error in result.errors)
    assert messages.read_bytes() == unrecognized
    assert not action.path.exists()
    payload = action.destination / "payload"
    root = action.path.parent.parent
    assert {path: (payload / path.relative_to(root)).read_bytes() for path in original} == original


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
