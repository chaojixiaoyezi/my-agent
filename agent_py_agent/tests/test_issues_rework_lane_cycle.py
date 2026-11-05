"""CI-R2：真实维护轮次、独立标记额度和有界多线程宽限的行为回归。"""
from __future__ import annotations

import gc
import json
import os
import shutil
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.conversation import ConversationStore, message_scan
from agent_py_agent.agent.conversation import compact_reachability as compact
from agent_py_agent.agent.conversation import hamt_reachability as hamt
from agent_py_agent.agent.conversation import transcript_index_gc as transcript
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic
from agent_py_agent.agent.memory_store import retention_apply, retention_scan
from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionAction
from agent_py_agent.agent.memory_store.retired_thread_journal import append_retired_thread
from agent_py_agent.agent.user_space import owner_maintenance, storage_lanes
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.storage_lanes import validate_lane_receipt
from agent_py_agent.tests.test_compact_scoped_checkpoint import _commit_transcript
from agent_py_agent.tests.test_compact_scoped_checkpoint import compact_case as _compact_case
from agent_py_agent.tests.test_issues_w3_compact_reachability import (
    aged,
    committed_pair,
    home_for,
    request_for,
)
from agent_py_agent.tests.test_issues_w3_transcript_index_gc import rotation

compact_case = _compact_case


def _rotations(case, monkeypatch, count=2):
    """使用真实消息写者产生各自的索引和孤儿，不伪造可达性证明。"""
    cases = [case]
    for number in range(1, count):
        thread = case.store.threads.get_or_create({
            "canonical_user_id": f"gc-user-{number}",
            "channel_conversation_id": f"gc-chat-{number}", "now": 1.0,
        })
        cases.append(SimpleNamespace(**{**vars(case), "thread_id": thread.thread_id}))
    for item in cases:
        rotation(item, monkeypatch)
    return sorted(cases, key=lambda item: item.thread_id)


def _finish_first_pass(request):
    deleted = 0
    for _ in range(50):
        result = transcript.maintain_transcript_index_lane(request)
        validate_lane_receipt(result)
        deleted += result["deleted_items"]
        if result["complete"]:
            return {**result, "deleted_items": deleted}
        assert result.get("deferred_reason") == "budget_exhausted"
        request = replace(request, cursor=result["cursor"])
    pytest.fail("有限线程的发现轮次未结束")


def test_mark_allowance_exception_is_not_dangling_reference():
    class Measured:
        def _load(self, reference):
            raise hamt._BudgetExceeded("mark allowance")

    with pytest.raises(hamt._BudgetExceeded, match="mark allowance"):
        hamt.mark_reachable_nodes(Measured(), [{"ref": {"ref": "node.json"}}], max_nodes=10)


@pytest.mark.parametrize("state", ["waiting", "unknown"])
def test_retired_waiting_or_unknown_finishes_pass_without_advancing_row(compact_case, state):
    case = compact_case
    home = home_for(case)
    home.owner_compact_dir.mkdir()
    append_retired_thread(
        home, thread_id="retired-a", conversation_root=case.root / "conversations",
        action_id="a", trash_container=case.root / "trash/waiting", retired_at=time.time(),
    )
    if state == "waiting":
        (case.root / "trash/waiting").mkdir(parents=True)
    else:
        journal = home.owner_data_dir / "retention/retired_threads.jsonl"
        row = json.loads(journal.read_text())
        row["schema"] = "unknown.v99"
        journal.write_text(json.dumps(row) + "\n")
    writes = []
    result = compact.maintain_compact_reachability_lane(request_for(
        home, cursor={"phase": "retired", "journal_offset": 0},
        commit=lambda operation: (writes.append(True), operation())[1],
    ))
    validate_lane_receipt(result)
    assert result["complete"] and result["cursor"]["journal_offset"] == 0
    assert result["cursor"]["phase"] == "live" and result["cursor"]["after"] == ""
    assert result["deferred_reason"] == "retired_thread_" + state and not writes
    assert bool(result["errors"]) is (state == "unknown")


@pytest.mark.parametrize("kind", ["compact_checkpoints", "conversation_messages"])
def test_bad_thread_finishes_pass_but_never_authorizes_deletion(compact_case, monkeypatch, kind):
    case = compact_case
    if kind == "compact_checkpoints":
        committed_pair(case)
        path = case.root / "compact/conversations" / (case.thread_id + ".jsonl")
        path.write_text("{\n")
        lane = compact.maintain_compact_reachability_lane
    else:
        _, _, _, path, _, _ = rotation(case, monkeypatch)
        path.with_suffix(".manifest.json").write_text("{")
        lane = transcript.maintain_transcript_index_lane
    writes = []
    result = lane(request_for(home_for(case), kind=kind,
                              commit=lambda operation: (writes.append(True), operation())[1]))
    validate_lane_receipt(result)
    assert result["complete"] and result["deferred_reason"] == "reachability_incomplete"
    assert result["errors"] and result["deleted_items"] == 0 and not writes


def test_first_thread_marks_150_committed_checkpoints_before_sweeping(compact_case, monkeypatch):
    case = compact_case
    versions = []
    for number in range(150):
        case.clock[0] += 1
        versions.append(_commit_transcript(
            case, scope=THREAD_COMPACT_SCOPE, summary=f"摘要-{number}", content=f"消息-{number}", publish=True,
        )[0])
    index = hamt.ImmutableJsonIndex(case.root / "compact", ("evidence", case.thread_id, "indexes"))
    reference = index._save({"entries": {"orphan": True}})
    target = aged(index.root.joinpath(*index.parts, reference["ref"]["ref"]))
    # 只控制预算时钟：每次真实读取计 0.1ms，避免机器快慢决定是否跨过常规片预算。
    clock = [0.0]
    original_read = hamt._LaneWork.read

    def measured(work, size):
        clock[0] += 0.0001
        return original_read(work, size)

    monkeypatch.setattr(storage_lanes.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(hamt._LaneWork, "read", measured)
    result = compact.maintain_compact_reachability_lane(request_for(home_for(case)))
    assert clock[0] > storage_lanes.STORAGE_LANE_SLICE_MS / 1000
    assert not result["errors"] and not result["skipped"].get("mark_budget_exceeded")
    assert not target.exists() and result["deleted_items"] == 1
    for version in (versions[0], versions[-1]):
        rows = list(compact.iter_committed_compact_checkpoints(case.agent, version, hydrate=False))
        assert rows and rows[0]["checkpoint_id"] == version.compact_checkpoint_id


@pytest.mark.parametrize("kind", ["compact_checkpoints", "conversation_messages"])
def test_mark_allowance_skip_preserves_objects_without_per_thread_error(compact_case, monkeypatch, kind):
    case = compact_case
    if kind == "compact_checkpoints":
        committed_pair(case)
        lane = compact.maintain_compact_reachability_lane
    else:
        rotation(case, monkeypatch)
        lane = transcript.maintain_transcript_index_lane
    before = {path: path.read_bytes() for path in case.root.rglob("*.json")}

    def stop(index, reference):
        raise hamt._BudgetExceeded("oversized mark")

    monkeypatch.setattr(hamt._MeasuredIndex, "_load", stop)
    result = lane(request_for(home_for(case), kind=kind))
    assert result["skipped"].get("mark_budget_exceeded") == 1 and not result["errors"]
    assert result["deleted_items"] == 0
    assert all(path.read_bytes() == raw for path, raw in before.items())


def test_all_threads_start_grace_in_same_pass_and_old_pending_is_not_blocked(compact_case, monkeypatch):
    cases = _rotations(compact_case, monkeypatch)
    now = time.time()
    request = request_for(home_for(compact_case), kind="conversation_messages", now=now)
    first = _finish_first_pass(request)
    pending = first["cursor"]["pending"]
    assert isinstance(pending, list) and {row["tid"] for row in pending} == {c.thread_id for c in cases}
    assert all(row["marked_at"] == now for row in pending)
    assert not first.get("deferred_reason") and first["deleted_items"] == 0
    young, old = pending
    old["marked_at"] -= hamt.HAMT_SWEEP_GRACE_SECONDS
    for node in (compact_case.root / "conversations/messages/archive" / old["tid"] / "index").glob("*.json"):
        aged(node, days=15)
    cursor = {**first["cursor"], "pending": [young, old]}
    second = _finish_first_pass(replace(request, cursor=cursor))
    assert second["deleted_items"] > 0
    assert [row["tid"] for row in second["cursor"]["pending"]] == [young["tid"]]
    assert second["cursor"]["pending"][0]["marked_at"] == now


def test_legacy_pending_dict_keeps_original_grace_timestamp(compact_case, monkeypatch):
    _rotations(compact_case, monkeypatch, count=1)
    request = request_for(home_for(compact_case), kind="conversation_messages")
    first = _finish_first_pass(request)
    row = dict(first["cursor"]["pending"][0])
    row.pop("root")
    legacy = {**first["cursor"], "pending": row, "root_index": 0}
    early = _finish_first_pass(replace(request, cursor=legacy, now=request.now + 86400))
    assert early["deleted_items"] == 0
    assert early["cursor"]["pending"][0]["marked_at"] == request.now
    mature = _finish_first_pass(replace(request, cursor=early["cursor"],
                                       now=request.now + hamt.HAMT_SWEEP_GRACE_SECONDS))
    assert mature["deleted_items"] > 0 and "pending" not in mature["cursor"]


def test_pending_capacity_is_bounded_and_does_not_reset_grace(compact_case, monkeypatch):
    cases = _rotations(compact_case, monkeypatch, count=3)
    monkeypatch.setattr(transcript, "TRANSCRIPT_INDEX_PENDING_MAX_COUNT", 2, raising=False)
    request = request_for(home_for(compact_case), kind="conversation_messages")
    first = _finish_first_pass(request)
    assert len(first["cursor"]["pending"]) == 2
    assert first["skipped"]["pending_full"] == 1
    second = first
    deleted = 0
    for _ in range(2):
        second = _finish_first_pass(replace(request, cursor=second["cursor"],
                                           now=request.now + hamt.HAMT_SWEEP_GRACE_SECONDS))
        deleted += second["deleted_items"]
    assert deleted > 0 and len(second["cursor"]["pending"]) == 1
    assert second["cursor"]["pending"][0]["tid"] == cases[-1].thread_id
    assert second["cursor"]["pending"][0]["marked_at"] == request.now + hamt.HAMT_SWEEP_GRACE_SECONDS


def test_first_allowance_is_independent_metered_once_and_closed(compact_case, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(storage_lanes.time, "monotonic", lambda: clock[0])
    request = replace(request_for(home_for(compact_case)),
                      budget=storage_lanes.StorageLaneBudget(3, 10, 100.005))
    work = hamt._LaneWork(request)
    work.items, work.size = 1, 7
    assert hamt.HAMT_MARK_THREAD_MS == 5000
    assert hamt.HAMT_MARK_THREAD_BYTES == 256 * 1024 * 1024
    assert hamt.HAMT_MARK_MAX_NODES == 65536
    assert work.begin_thread_allowance()
    assert work._allowance == storage_lanes.StorageLaneBudget(4, 7 + 256 * 1024 * 1024, 105.0)
    clock[0] += 0.1
    work.read(1000)
    assert not work.exhausted() and work.available_bytes() == 256 * 1024 * 1024 - 1000
    assert work.receipt({}, False)["bytes"] == 1007
    assert not work.begin_thread_allowance()
    work.end_thread_allowance()
    assert work.exhausted() and work._allowance is None and not work.begin_thread_allowance()


@pytest.mark.parametrize("items,size", [(0, 100), (100, 0)])
def test_zero_budgets_never_open_thread_allowance(compact_case, items, size):
    request = replace(request_for(home_for(compact_case)),
                      budget=storage_lanes.StorageLaneBudget(items, size, None))
    work = hamt._LaneWork(request)
    assert not work.begin_thread_allowance() and work._allowance is None
    assert work.exhausted() and work.items == 0 and work.size == 0


def test_node_cap_skip_never_sweeps_partial_mark(compact_case, monkeypatch):
    rotation(compact_case, monkeypatch)
    monkeypatch.setattr(transcript, "HAMT_MARK_MAX_NODES", 1)
    writes = []
    request = request_for(home_for(compact_case), kind="conversation_messages",
                          commit=lambda operation: (writes.append(True), operation())[1])
    result = transcript.maintain_transcript_index_lane(request)
    assert result["skipped"]["mark_budget_exceeded"] == 1 and not result["errors"]
    assert result["deleted_items"] == 0 and not writes
    assert "pending" not in result["cursor"]


def test_missing_pending_namespace_or_root_releases_slot_before_grace(compact_case, monkeypatch):
    _rotations(compact_case, monkeypatch, count=1)
    request = request_for(home_for(compact_case), kind="conversation_messages")
    first = _finish_first_pass(request)
    pending = first["cursor"]["pending"][0]
    namespace = compact_case.root / pending["root"] / "messages/archive" / pending["tid"] / "index"
    namespace.rename(namespace.with_name("saved-index"))
    cursor = {**first["cursor"], "pending": [pending, {**pending, "root": "outside-owner"}]}
    result = _finish_first_pass(replace(request, cursor=cursor))
    assert result["skipped"]["pending_root_gone"] == 2
    assert "pending" not in result["cursor"] and result["deleted_items"] == 0
    assert namespace.with_name("saved-index").is_dir()


def test_pending_identity_includes_conversation_root(compact_case, monkeypatch):
    import shutil

    _rotations(compact_case, monkeypatch, count=1)
    runtime = compact_case.root / "workspace/runtime/workspaces/other/conversations"
    runtime.parent.mkdir(parents=True)
    shutil.copytree(compact_case.root / "conversations", runtime)
    request = request_for(home_for(compact_case), kind="conversation_messages")
    result = _finish_first_pass(request)
    pending = result["cursor"]["pending"]
    assert len(pending) == 2 and {row["tid"] for row in pending} == {compact_case.thread_id}
    assert {row["root"] for row in pending} == {
        "conversations", "workspace/runtime/workspaces/other/conversations",
    }


def test_pending_cursor_reserves_space_for_long_unicode_roots(compact_case, monkeypatch):
    root = compact_case.root / "workspace/runtime/workspaces" / ("汉" * 80) / "conversations"
    (root / "messages").mkdir(parents=True)

    def names(home, messages):
        return iter(f"thread-{number:02d}" for number in range(48)) if messages == root / "messages" else iter(())

    def prepare(work, messages, tid, pending):
        work.items += 1
        return "ok", {"tid": tid, "root_sha": "a" * 64, "marked_at": work.request.now, "candidates": 1}

    monkeypatch.setattr(transcript, "_thread_names", names)
    monkeypatch.setattr(transcript, "_try_thread", prepare)
    result = transcript.maintain_transcript_index_lane(
        request_for(home_for(compact_case), kind="conversation_messages"))
    validate_lane_receipt(result)
    assert result["complete"] and 0 < len(result["cursor"]["pending"]) < 48
    assert result["skipped"]["pending_full"] > 0
    assert len(json.dumps(result["cursor"], separators=(",", ":"))) <= storage_lanes.STORAGE_LANE_CURSOR_MAX_BYTES - 1024


def test_partial_phase_two_resumes_first_and_only_one_pending_runs(compact_case, monkeypatch):
    _rotations(compact_case, monkeypatch, count=2)
    request = request_for(home_for(compact_case), kind="conversation_messages")
    first = _finish_first_pass(request)
    left, right = first["cursor"]["pending"]
    namespace = str(Path(right["root"]) / "messages/archive" / right["tid"] / "index")
    cursor = {**first["cursor"], "sweep": {"namespace": namespace, "after": "resume.json"}}
    attempted = []

    def partial(work, messages, tid, pending):
        attempted.append(tid)
        work.items += 1
        return "ok", pending

    monkeypatch.setattr(transcript, "_try_thread", partial)
    result = transcript.maintain_transcript_index_lane(replace(
        request, cursor=cursor, now=request.now + hamt.HAMT_SWEEP_GRACE_SECONDS,
    ))
    assert attempted == [right["tid"]] and not result["complete"]
    assert result["cursor"]["sweep"] == cursor["sweep"]
    assert result["cursor"]["pending"] == [left, right]


def _retire_through_writer(case, home, now):
    """真实 W2 归档提交后由原入口追加退休台账，不伪造退休状态。"""
    root = case.root / "conversations"
    for name in ("channel_bindings.json", "user_latest_threads.json"):
        update_json_file_atomic(root / name, lambda current: {
            key: value for key, value in current.items() if str(value) != case.thread_id
        })
    thread = case.store.threads.require(case.thread_id)
    related, errors = retention_scan._conversation_related_paths(root, thread)
    assert not errors
    source = case.store.storage.thread_path(case.thread_id)
    action = MemoryRetentionAction(
        action_id="r2-retired", category="conversation", operation="conversation_trash",
        path=source, reason="expired", record_id=case.thread_id, related_paths=related,
        destination=home.owner_trash_dir / "retention/conversation/r2-retired",
        cutoff_timestamp=now - 86400, authority_path=source,
        authority_fingerprint=retention_scan.file_fingerprint(source),
        source_fingerprint=retention_scan.paths_fingerprint(related),
    )
    result = retention_apply._trash_conversation(
        home=home, action=action, now=datetime.fromtimestamp(now, timezone.utc))
    assert result.status == "trashed" and action.destination.is_dir()
    journal = home.owner_data_dir / "retention/retired_threads.jsonl"
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["thread_id"] == case.thread_id
    return action


def test_waiting_retirement_closes_compact_pass(compact_case):
    case = compact_case
    home = SimpleNamespace(**vars(home_for(case)), owner_trash_dir=case.root / "trash")
    committed_pair(case)
    now = time.time()
    action = _retire_through_writer(case, home, now)
    live = case.store.threads.get_or_create({"canonical_user_id": "live-owner", "now": now})
    live_case = SimpleNamespace(**{**vars(case), "thread_id": live.thread_id})
    committed_pair(live_case)
    protected = {
        path: path.read_bytes() for directory in (home.owner_compact_dir, action.destination)
        for path in directory.rglob("*") if path.is_file()
    }
    cursor = {}
    for _ in range(20):
        result = compact.maintain_compact_reachability_lane(request_for(home, cursor=cursor, now=now))
        validate_lane_receipt(result)
        cursor = result["cursor"]
        if result["complete"]:
            break
    assert result["complete"] and result["deferred_reason"] == "retired_thread_waiting"
    assert cursor["journal_offset"] == 0 and cursor["phase"] == "live"
    assert all(path.read_bytes() == raw for path, raw in protected.items())


def test_torn_journal_tail_closes_compact_pass(compact_case, record_property):
    case = compact_case
    home = SimpleNamespace(**vars(home_for(case)), owner_trash_dir=case.root / "trash")
    committed_pair(case)
    now = time.time()
    action = _retire_through_writer(case, home, now)
    shutil.rmtree(action.destination)
    journal = home.owner_data_dir / "retention/retired_threads.jsonl"
    torn = b'{"schema": "retired_thread.v1", "thread_id": "other-thread"'
    with journal.open("ab") as handle:
        handle.write(torn)
    original = journal.read_bytes()
    protected = {path: path.read_bytes() for path in home.owner_compact_dir.rglob("*")
                 if path.is_file()}
    cursor, receipts, writes = {}, [], []
    gc.collect()
    before = _fd_count()
    for _ in range(20):
        result = compact.maintain_compact_reachability_lane(request_for(
            home, cursor=cursor, now=now + 60,
            commit=lambda operation: (writes.append(True), operation())[1],
        ))
        validate_lane_receipt(result)
        receipts.append(result)
        cursor = result["cursor"]
        if result["complete"]:
            break
    gc.collect()
    fd_delta = _fd_count() - before
    record_property("fd_delta", fd_delta)
    record_property("complete_history", json.dumps([row["complete"] for row in receipts]))
    record_property("deferred_reason", result.get("deferred_reason"))
    assert result["complete"] is True
    assert result.get("deferred_reason") == "retired_thread_unknown"
    assert any(error["code"] == "retired_thread_state_unknown"
               for row in receipts for error in row["errors"])
    assert cursor["journal_offset"] == 0 and cursor["phase"] == "live"
    assert journal.read_bytes() == original
    assert all(path.read_bytes() == raw for path, raw in protected.items())
    assert not writes and all(row["deleted_items"] == 0 for row in receipts)
    assert fd_delta == 0


def test_malformed_journal_line_is_skipped_without_purging_earlier_rows(compact_case, record_property):
    case = compact_case
    home = SimpleNamespace(**vars(home_for(case)), owner_trash_dir=case.root / "trash")
    committed_pair(case)
    now = time.time()
    action = _retire_through_writer(case, home, now)
    shutil.rmtree(action.destination)
    journal = home.owner_data_dir / "retention/retired_threads.jsonl"
    torn = b'{"schema": "retired_thread.v1", "thread_id": "other-thread"'
    with journal.open("ab") as handle:
        handle.write(torn + b"\n")
    original = journal.read_bytes()
    protected = {path: path.read_bytes() for path in home.owner_compact_dir.rglob("*")
                 if path.is_file()}
    cursor, receipts, skipped, writes = {}, [], set(), []
    gc.collect()
    before = _fd_count()
    for _ in range(20):
        result = compact.maintain_compact_reachability_lane(request_for(
            home, cursor=cursor, now=now + 60,
            commit=lambda operation: (writes.append(True), operation())[1],
        ))
        validate_lane_receipt(result)
        receipts.append(result)
        skipped.update(result["skipped"])
        cursor = result["cursor"]
        if result["complete"]:
            break
    gc.collect()
    fd_delta = _fd_count() - before
    record_property("fd_delta", fd_delta)
    record_property("complete_history", json.dumps([row["complete"] for row in receipts]))
    record_property("skipped", json.dumps(sorted(skipped)))
    assert result["complete"] is True
    assert cursor["journal_offset"] == journal.stat().st_size
    assert {"retired_thread_undecidable", "retired_thread_malformed"} <= skipped
    assert all(path.read_bytes() == raw for path, raw in protected.items())
    assert journal.read_bytes() == original
    assert not writes and all(row["deleted_items"] == 0 for row in receipts)
    assert fd_delta == 0


def _owner_case(case):
    """沿 W1 的真实 home 初始化方式准备维护输入，不替换通道或登记表。"""
    home = ensure_my_agent_home(case.root / "cycle-home")
    store = ConversationStore(home.owner_home_dir / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "cycle-owner", "now": 1.0})
    agent = SimpleNamespace(home_paths=home, backend=case.agent.backend)
    return home, SimpleNamespace(**{**vars(case), "root": home.owner_home_dir,
                                   "store": store, "thread_id": thread.thread_id, "agent": agent})


def _owner_tick(home, previous, now):
    """复制 W1 的真实调度器调用口；这里没有缺失通道或自造回执。"""
    policy = SimpleNamespace(legal_hold=False, applied=True, load_errors=())
    return owner_maintenance.run_owner_storage_maintenance_slice(
        home, previous, policy, now=now, quota_max_ms=1000, quota_max_entries=4096)


def test_dispatcher_cycle_closes_with_waiting_row_and_grace_entry(compact_case, monkeypatch):
    home, retired_case = _owner_case(compact_case)
    committed_pair(retired_case)
    now = time.time()
    action = _retire_through_writer(retired_case, home, now)
    live = retired_case.store.threads.get_or_create({"canonical_user_id": "cycle-live", "now": now})
    live_case = SimpleNamespace(**{**vars(retired_case), "thread_id": live.thread_id})
    rotation(live_case, monkeypatch)
    previous, reports, finished = {}, set(), None
    for tick in range(6):
        state = _owner_tick(home, previous, now + tick * 3600)
        assert not state["errors"]
        reports.update(state["lane_reports"])
        previous = {"storage_maintenance": state}
        if not state["continuation_pending"]:
            finished = tick
            break
    assert finished is not None
    # 全局索引留给协调者，不应阻止本地轮次结束或下一轮重新调度。
    assert set(state["pending_kinds"]) <= {"global_indexes"}
    assert state["continuation_pending"] is False
    assert reports == set(storage_lanes.LANE_BINDINGS)
    assert state["cursors"]["compact_checkpoints"]["journal_offset"] == 0
    assert state["cursors"]["conversation_messages"]["pending"]
    assert action.destination.is_dir()
    later = _owner_tick(home, previous, now + (finished + 1) * 3600)
    assert not later["errors"]
    assert set(later["lane_reports"]) == set(storage_lanes.LANE_BINDINGS)


def test_dispatcher_cycle_closes_with_torn_journal_tail(compact_case, record_property):
    home, retired_case = _owner_case(compact_case)
    committed_pair(retired_case)
    now = time.time()
    action = _retire_through_writer(retired_case, home, now)
    shutil.rmtree(action.destination)
    journal = home.owner_data_dir / "retention/retired_threads.jsonl"
    torn = b'{"schema": "retired_thread.v1", "thread_id": "other-thread"'
    with journal.open("ab") as handle:
        handle.write(torn)
    original = journal.read_bytes()
    previous, reports, pending, finished = {}, set(), [], None
    for tick in range(6):
        state = _owner_tick(home, previous, now + tick * 3600)
        assert not state["errors"]
        reports.update(state["lane_reports"])
        pending.append(state["continuation_pending"])
        previous = {"storage_maintenance": state}
        if not state["continuation_pending"]:
            finished = tick
            break
    record_property("continuation_pending_history", json.dumps(pending))
    record_property("lane_reports", json.dumps(sorted(reports)))
    assert finished is not None
    assert state["continuation_pending"] is False
    assert set(state["pending_kinds"]) <= {"global_indexes"}
    assert reports == set(storage_lanes.LANE_BINDINGS)
    later = _owner_tick(home, previous, now + (finished + 1) * 3600)
    assert not later["errors"]
    assert set(later["lane_reports"]) == set(storage_lanes.LANE_BINDINGS)
    assert journal.read_bytes() == original and journal.read_bytes().endswith(torn)


def test_first_thread_allowance_collects_orphan_with_tiny_slices(compact_case, monkeypatch):
    committed_pair(compact_case)
    index = hamt.ImmutableJsonIndex(compact_case.root / "compact",
                                    ("evidence", compact_case.thread_id, "indexes"))
    reference = index._save({"entries": {"tiny-slice-orphan": True}})
    target = aged(index.root.joinpath(*index.parts, reference["ref"]["ref"]))
    monkeypatch.setattr(hamt, "STORAGE_LANE_SLICE_MS", 0.001)
    cursor = {}
    for _ in range(3):
        result = compact.maintain_compact_reachability_lane(
            request_for(home_for(compact_case), cursor=cursor))
        cursor = result["cursor"]
        assert not result["errors"]
        if not target.exists():
            break
    assert not target.exists()


@pytest.mark.parametrize("kind", ["compact_checkpoints", "conversation_messages"])
def test_node_cap_overflow_is_skipped_not_an_error(compact_case, monkeypatch, kind):
    case = compact_case
    if kind == "compact_checkpoints":
        committed_pair(case)
        lane, module = compact.maintain_compact_reachability_lane, compact
    else:
        rotation(case, monkeypatch)
        lane, module = transcript.maintain_transcript_index_lane, transcript
    monkeypatch.setattr(module, "HAMT_MARK_MAX_NODES", 1, raising=False)
    cursor, skipped = {}, 0
    for _ in range(5):
        result = lane(request_for(home_for(case), kind=kind, cursor=cursor))
        validate_lane_receipt(result)
        assert not result["errors"] and result["deleted_items"] == 0
        skipped += result["skipped"].get("mark_budget_exceeded", 0)
        cursor = result["cursor"]
        if result["complete"]:
            break
    assert result["complete"] and skipped >= 1


def test_transcript_pending_list_runs_grace_in_parallel(compact_case, monkeypatch):
    cases = _rotations(compact_case, monkeypatch, count=3)
    originals = {}
    for case in cases:
        path = case.root / "conversations/messages" / (case.thread_id + ".jsonl")
        with message_scan.TranscriptReader(path) as reader:
            originals[path] = reader.read()
    request = request_for(home_for(compact_case), kind="conversation_messages")
    first = _finish_first_pass(request)
    assert len(first["cursor"]["pending"]) == 3
    assert first["complete"] and first["deleted_items"] == 0
    cursor = first["cursor"]
    remaining = {case.thread_id for case in cases}
    for _ in range(3):
        result = transcript.maintain_transcript_index_lane(replace(
            request, cursor=cursor, now=request.now + hamt.HAMT_SWEEP_GRACE_SECONDS + 1,
        ))
        validate_lane_receipt(result)
        cursor = result["cursor"]
        current = {row["tid"] for row in cursor.get("pending", [])}
        assert len(remaining - current) == 1 and current <= remaining
        assert result["deleted_items"] > 0
        remaining = current
        for path, original in originals.items():
            with message_scan.TranscriptReader(path) as reader:
                assert reader.read() == original
    assert not remaining


def test_pending_list_keeps_cursor_bounded(compact_case, monkeypatch):
    root = compact_case.root / "conversations"
    names = [f"thread-{number:02d}-" + "x" * 190 for number in range(49)]
    assert all(len(tid) == 200 for tid in names)
    now = time.time()
    rows = []
    for tid in names:
        (root / "messages/archive" / tid / "index").mkdir(parents=True)
        rows.append({"root": "conversations", "tid": tid, "root_sha": "a" * 64,
                     "marked_at": now, "candidates": 1})

    def prepare(work, messages, tid, pending):
        # 此单测只隔离游标入队容量；真实标记、消息读取和清扫另由三线程回归覆盖。
        work.items += 1
        return "ok", {key: value for key, value in rows[names.index(tid)].items() if key != "root"}

    monkeypatch.setattr(transcript, "_try_thread", prepare)
    result = transcript.maintain_transcript_index_lane(request_for(
        home_for(compact_case), kind="conversation_messages", now=now,
        cursor={"pending": rows[:48]},
    ))
    validate_lane_receipt(result)
    assert result["complete"] and result["skipped"]["pending_full"] >= 1
    assert names[-1] not in {row["tid"] for row in result["cursor"]["pending"]}
    assert len(json.dumps(result["cursor"], separators=(",", ":"), sort_keys=True)) <= storage_lanes.STORAGE_LANE_CURSOR_MAX_BYTES


def _fd_count():
    """读取真实进程 FD，不把调用次数或假目录列表当作资源回收证明。"""
    for path in (Path("/proc/self/fd"), Path("/dev/fd")):
        if path.is_dir():
            with os.scandir(path) as entries:
                return sum(1 for _ in entries)
    if os.name == "nt":
        import ctypes

        count = 0
        for descriptor in range(ctypes.CDLL("ucrtbase")._getmaxstdio()):
            try:
                os.fstat(descriptor)
            except OSError:
                continue
            count += 1
        return count
    raise AssertionError("当前平台没有真实 FD 计数入口")


def test_full_busy_and_mark_budget_slices_have_zero_fd_delta(compact_case, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    case = compact_case
    committed_pair(case)
    index = hamt.ImmutableJsonIndex(case.root / "compact", ("evidence", case.thread_id, "indexes"))
    first_ref = index._save({"entries": {"fd-full": True}})
    first_target = aged(index.root.joinpath(*index.parts, first_ref["ref"]["ref"]))
    request = request_for(home_for(case))
    before = _fd_count()
    full = compact.maintain_compact_reachability_lane(request)
    assert _fd_count() - before == 0 and full["deleted_items"] == 1 and not first_target.exists()
    second_ref = index._save({"entries": {"fd-busy": True}})
    second_target = aged(index.root.joinpath(*index.parts, second_ref["ref"]["ref"]))
    entered, release = Event(), Event()
    ledger = case.root / "compact/conversations" / (case.thread_id + ".jsonl")

    def hold():
        with locked_json_path(ledger):
            entered.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(hold)
        assert entered.wait(5)
        try:
            before = _fd_count()
            busy = compact.maintain_compact_reachability_lane(request)
            assert _fd_count() - before == 0
            assert busy["skipped"]["lock_busy"] == 1 and not busy["complete"]
            assert second_target.exists() and busy["deleted_items"] == 0
        finally:
            release.set()
        future.result(timeout=5)

    def exceeded(index, reference):
        raise hamt._BudgetExceeded("mark limit")

    monkeypatch.setattr(hamt._MeasuredIndex, "_load", exceeded)
    before = _fd_count()
    oversized = compact.maintain_compact_reachability_lane(request)
    assert _fd_count() - before == 0
    assert oversized["skipped"]["mark_budget_exceeded"] == 1 and not oversized["errors"]
    assert oversized["deleted_items"] == 0 and second_target.exists()
