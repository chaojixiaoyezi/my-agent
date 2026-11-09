"""W3：真实 CoW 写者、检查点提交链和退休证明共同约束删除。"""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import compact_checkpoint as checkpoints
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.conversation.compact_summary_view import resolve_compact_summary_view
from agent_py_agent.agent.conversation.store_io import ImmutableJsonIndex
from agent_py_agent.agent.memory_store.retired_thread_journal import append_retired_thread
from agent_py_agent.agent.user_space.storage_lanes import (
    StorageLaneBudget,
    StorageLaneRequest,
    validate_lane_receipt,
)
from agent_py_agent.tests.test_compact_scoped_checkpoint import (
    _commit_transcript,
)
from agent_py_agent.tests.test_compact_scoped_checkpoint import (
    compact_case as _compact_case,
)

compact_case = _compact_case


def home_for(case):
    return SimpleNamespace(
        owner_home_dir=case.root,
        owner_workspace_dir=case.root / "workspace",
        owner_data_dir=case.root / "data",
        owner_compact_dir=case.root / "compact",
    )


def request_for(
    home, *, kind="compact_checkpoints", cursor=None, now=None, hold=False, commit=None, items=None
):
    return StorageLaneRequest(
        kind,
        home,
        time.time() if now is None else now,
        cursor or {},
        StorageLaneBudget(items, None, None),
        hold,
        commit is not None,
        None,
        commit,
    )


def aged(path, *, days=8):
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))
    return path


def committed_pair(case):
    first, _, _ = _commit_transcript(
        case, scope=THREAD_COMPACT_SCOPE, summary="第一代", content="第一条", publish=True
    )
    case.clock[0] += 1
    second, _, _ = _commit_transcript(
        case, scope=THREAD_COMPACT_SCOPE, summary="第二代", content="第二条", publish=True
    )
    return first, second


@pytest.mark.parametrize("kind", ["index", "payload", "copy"])
def test_reusing_identical_content_refreshes_mtime(tmp_path, kind):
    root = tmp_path / "compact"
    root.mkdir()
    if kind == "index":
        index = ImmutableJsonIndex(root, ("evidence", "thread-a", "indexes"))

        def save():
            return index._save({"entries": {"key": True}})

        ref = save()["ref"]
        path = root / "evidence/thread-a/indexes" / ref["ref"]
    elif kind == "payload":

        def save():
            return checkpoints._write_cold_payload(root, "thread-a", {"value": True})

        ref = save()
        path = root / "evidence/thread-a" / ref["ref"]
    else:
        row = {"thread_id": "thread-a", "checkpoint_id": "candidate-a"}

        def save():
            return checkpoints._save_checkpoint_copy(root, row)

        save()
        path = next((root / "archive/thread-a").glob("*.checkpoint.json"))
    raw = path.read_bytes()
    old = aged(path).stat().st_mtime
    save()
    assert path.stat().st_mtime > old + 7 * 86400
    assert path.read_bytes() == raw


def test_mark_walks_children_not_leaf_data_and_refuses_incomplete(tmp_path):
    from agent_py_agent.agent.conversation.hamt_reachability import mark_reachable_nodes

    index = ImmutableJsonIndex(tmp_path, ("index",))
    leaf = index._save({"entries": {"data": {"ref": {"ref": "not-a-node.json"}}}})
    root = index._save({"children": {"a": leaf}})
    marked = mark_reachable_nodes(index, [root], max_nodes=10)
    assert marked.complete and marked.visited == 2 and not marked.dangling
    assert marked.reachable == frozenset((root["ref"]["ref"], leaf["ref"]["ref"]))
    assert not mark_reachable_nodes(index, [root], max_nodes=1).complete
    (tmp_path / "index" / leaf["ref"]["ref"]).unlink()
    failed = mark_reachable_nodes(index, [root], max_nodes=10)
    assert not failed.complete and leaf["ref"]["ref"] in failed.dangling


def test_sweep_restores_reused_candidate_and_preserves_link_metadata(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.hamt_reachability import sweep_unreachable_nodes

    namespace = tmp_path / "index"
    namespace.mkdir()
    old = namespace / "old.json"
    old.write_bytes(b"old")
    aged(old)
    link = namespace / "link.json"
    link.symlink_to(old)
    external = tmp_path / "external"
    external.write_bytes(b"keep")
    hard = namespace / "hard.json"
    hard.write_bytes(b"keep")
    aged(hard)
    # 当前执行环境禁止创建硬链接。保留真实叶与真实清扫，仅在系统调用边界注入多链接元数据。
    real_stat, real_lstat = os.stat, os.lstat

    def with_links(operation):
        def inspect(path, *args, **kwargs):
            info = operation(path, *args, **kwargs)
            if not isinstance(path, int) and Path(path).name == "hard.json":
                return SimpleNamespace(
                    st_mode=info.st_mode,
                    st_nlink=2,
                    st_mtime=info.st_mtime,
                    st_size=info.st_size,
                    st_dev=info.st_dev,
                    st_ino=info.st_ino,
                )
            return info

        return inspect

    monkeypatch.setattr(os, "stat", with_links(real_stat))
    monkeypatch.setattr(os, "lstat", with_links(real_lstat))
    commits = []

    def run_write(operation):
        commits.append(True)
        os.utime(old, None)
        return operation()

    result = sweep_unreachable_nodes(
        namespace,
        reachable=frozenset(),
        mark_started_at=time.time(),
        now=time.time(),
        max_items=20,
        run_write=run_write,
    )
    assert result["deleted"] == 0 and result["restored"] == 1
    assert old.read_bytes() == b"old" and external.read_bytes() == b"keep"
    assert link.is_symlink() and hard.exists() and len(commits) == 1


def test_compact_preserves_all_committed_generations_and_collects_only_orphan(compact_case):
    from agent_py_agent.agent.conversation.compact_reachability import (
        maintain_compact_reachability_lane,
    )
    from agent_py_agent.agent.conversation.hamt_reachability import mark_reachable_nodes

    case = compact_case
    first, second = committed_pair(case)
    root = case.root / "compact"
    index = ImmutableJsonIndex(root, ("evidence", case.thread_id, "indexes"))
    old_row = checkpoints.load_latest_committed_compact_checkpoint(case.agent, first)
    old_roots = [
        old_row["carried_view"][key]
        for key in ("messages", "tools", "scope_heads", "readable_bases", "checkpoint_lineage")
    ]
    old_nodes = mark_reachable_nodes(index, old_roots, max_nodes=2048).reachable
    orphan = index._save({"entries": {"uncommitted": True}})
    orphan_path = root / "evidence" / case.thread_id / "indexes" / orphan["ref"]["ref"]
    for path in root.rglob("*.json"):
        aged(path)
    ledger = root / "conversations" / f"{case.thread_id}.jsonl"
    before = ledger.read_bytes()
    archive = root / "archive" / case.thread_id
    sentinels = [archive / "gen-unrelated.json", archive / "gen-preserved.jsonl"]
    for sentinel in sentinels:
        sentinel.write_bytes(b"preserved archive metadata\n")
        aged(sentinel)
    commits = []
    result = maintain_compact_reachability_lane(
        request_for(home_for(case), commit=lambda operation: (commits.append(True), operation())[1])
    )
    validate_lane_receipt(result)
    assert result["deleted_items"] == 1 and not orphan_path.exists()
    from agent_py_agent.agent.conversation.immutable_node_store import ImmutableNodeStore
    node_store = ImmutableNodeStore(index.root, index.parts)
    assert all(node_store.read(name) is not None for name in old_nodes)
    assert ledger.read_bytes() == before and commits
    assert all(path.read_bytes() == b"preserved archive metadata\n" for path in sentinels)
    assert (
        checkpoints.load_latest_committed_compact_checkpoint(case.agent, second)["generation"] == 2
    )
    assert resolve_compact_summary_view(case.agent, first, THREAD_COMPACT_SCOPE).summary == "第一代"
    assert (
        resolve_compact_summary_view(case.agent, second, THREAD_COMPACT_SCOPE).summary == "第二代"
    )


@pytest.mark.parametrize("damage", ["node", "chain"])
def test_incomplete_compact_refuses_every_deletion(compact_case, damage):
    from agent_py_agent.agent.conversation.compact_reachability import (
        maintain_compact_reachability_lane,
    )

    case = compact_case
    first, _second = committed_pair(case)
    root = case.root / "compact"
    index = ImmutableJsonIndex(root, ("evidence", case.thread_id, "indexes"))
    orphan = index._save({"entries": {"orphan": True}})
    target = root.joinpath(*index.parts, orphan["ref"]["ref"])
    aged(target)
    if damage == "node":
        from agent_py_agent.agent.conversation.immutable_node_store import ImmutableNodeStore
        database = ImmutableNodeStore(root, index.parts).path
        database.write_bytes(b'corrupt node database')
    else:
        (root / "conversations" / f"{case.thread_id}.jsonl").write_bytes(b"{\n")
    commits = []
    result = maintain_compact_reachability_lane(
        request_for(home_for(case), commit=lambda operation: (commits.append(True), operation())[1])
    )
    assert target.exists() and result["deleted_items"] == 0 and not commits
    assert any(error["code"] == "compact_reachability_incomplete" for error in result["errors"])


def test_hold_empty_and_missing_thread_never_mutate(compact_case):
    from agent_py_agent.agent.conversation.compact_reachability import (
        maintain_compact_reachability_lane,
    )

    commits = []

    def commit(operation):
        commits.append(True)
        return operation()

    case = compact_case
    empty = maintain_compact_reachability_lane(request_for(home_for(case), commit=commit))
    assert empty["complete"] and not commits
    committed_pair(case)
    hold = maintain_compact_reachability_lane(request_for(home_for(case), hold=True, commit=commit))
    assert hold["deferred_reason"] == "legal_hold" and not commits
    case.store.threads.storage.thread_path(case.thread_id).unlink()
    result = maintain_compact_reachability_lane(request_for(home_for(case), commit=commit))
    assert result["skipped"]["thread_record_unavailable"] == 1
    assert result["skipped"]["untracked_trees"] >= 1 and not commits


def test_retired_thread_requires_positive_journal_and_preserves_lock(compact_case):
    from agent_py_agent.agent.conversation.compact_reachability import (
        maintain_compact_reachability_lane,
    )

    case = compact_case
    committed_pair(case)
    root = case.root / "compact"
    ledger = root / "conversations" / f"{case.thread_id}.jsonl"
    lock = Path(str(ledger) + ".lock")
    home = home_for(case)
    case.store.threads.storage.thread_path(case.thread_id).unlink()
    append_retired_thread(
        home,
        thread_id=case.thread_id,
        conversation_root=case.root / "conversations",
        action_id="retire-a",
        trash_container=case.root / "trash/purged",
        retired_at=time.time() - 10,
    )
    cursor = {}
    for _ in range(30):
        result = maintain_compact_reachability_lane(request_for(home, cursor=cursor, items=4))
        validate_lane_receipt(result)
        cursor = result["cursor"]
        if result["complete"]:
            break
    assert result["complete"] and not ledger.exists()
    assert all(path.name.endswith('.lock') for path in (root / "evidence" / case.thread_id).rglob('*') if path.is_file())
    assert not (root / "archive" / case.thread_id).exists()
    assert lock.exists() and cursor["journal_offset"] > 0


def test_busy_ledger_is_retried_without_deleting(compact_case):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from agent_py_agent.agent.common.json_io import locked_json_path
    from agent_py_agent.agent.conversation.compact_reachability import (
        maintain_compact_reachability_lane,
    )

    case = compact_case
    committed_pair(case)
    path = case.root / "compact/conversations" / f"{case.thread_id}.jsonl"
    index = ImmutableJsonIndex(case.root / "compact", ("evidence", case.thread_id, "indexes"))
    reference = index._save({"entries": {"busy-lock-candidate": True}})
    candidate = aged(index.root.joinpath(*index.parts, reference["ref"]["ref"]))
    entered, release = Event(), Event()

    def hold():
        with locked_json_path(path):
            entered.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(hold)
        assert entered.wait(5)
        try:
            result = maintain_compact_reachability_lane(request_for(home_for(case)))
            assert result["skipped"]["lock_busy"] == 1 and not result["complete"]
            assert candidate.exists()
        finally:
            release.set()
        future.result(timeout=5)


def test_small_slices_advance_past_fresh_candidates_without_starving_old_orphans(compact_case):
    from agent_py_agent.agent.conversation.compact_reachability import (
        maintain_compact_reachability_lane,
    )

    case = compact_case
    committed_pair(case)
    index = ImmutableJsonIndex(case.root / "compact", ("evidence", case.thread_id, "indexes"))
    paths, references = [], []
    for number in range(12):
        reference = index._save({"entries": {"uncommitted": number}})
        paths.append(index.root.joinpath(*index.parts, reference["ref"]["ref"]))
        references.append(reference)
    paths.sort()
    old = paths[-2:]
    for path in old:
        aged(path)
    cursor = {}
    for _ in range(30):
        result = maintain_compact_reachability_lane(
            request_for(home_for(case), cursor=cursor, items=3)
        )
        validate_lane_receipt(result)
        assert result["items"] <= 3
        cursor = result["cursor"]
        if result["complete"]:
            break
    assert result["complete"] and all(not path.exists() for path in old)
    fresh_names = {path.name for path in paths[:-2]}
    assert all(index._load(reference)['entries'] for reference in references if reference['ref']['ref'] in fresh_names)
    assert len(json.dumps(cursor).encode()) < 16384


def test_legacy_tree_without_ledger_is_preserved_and_counted(compact_case):
    from agent_py_agent.agent.conversation.compact_reachability import (
        maintain_compact_reachability_lane,
    )

    case = compact_case
    root = case.root / "compact"
    reference = checkpoints._write_cold_payload(root, "legacy-thread", {"legacy": True})
    target = aged(root / "evidence/legacy-thread" / reference["ref"])
    commits = []
    result = maintain_compact_reachability_lane(
        request_for(home_for(case), commit=lambda operation: (commits.append(True), operation())[1])
    )
    assert target.exists() and not commits
    assert result["skipped"]["untracked_trees"] == 1


def test_positive_retirement_cleans_side_tree_even_when_ledger_directory_is_missing(compact_case):
    from agent_py_agent.agent.conversation.compact_reachability import (
        maintain_compact_reachability_lane,
    )

    case = compact_case
    root = case.root / "compact"
    reference = checkpoints._write_cold_payload(root, "retired-legacy", {"legacy": True})
    target = root / "evidence/retired-legacy" / reference["ref"]
    home = home_for(case)
    append_retired_thread(
        home,
        thread_id="retired-legacy",
        conversation_root=case.root / "conversations",
        action_id="retired-legacy",
        trash_container=case.root / "trash/purged",
        retired_at=time.time(),
    )
    result = maintain_compact_reachability_lane(request_for(home))
    assert result["complete"] and not target.exists()
    assert (root / "conversations/retired-legacy.jsonl.lock").exists()


@pytest.mark.parametrize("state", ["waiting", "unknown"])
def test_retired_cursor_stops_before_waiting_or_unknown_row(compact_case, state):
    from agent_py_agent.agent.conversation.compact_reachability import (
        maintain_compact_reachability_lane,
    )
    from agent_py_agent.agent.memory_store.retired_thread_journal import iter_retired_threads

    case = compact_case
    committed_pair(case)
    home = home_for(case)
    append_retired_thread(
        home,
        thread_id=case.thread_id,
        conversation_root=case.root / "conversations",
        action_id="restored",
        trash_container=case.root / "trash/already-gone",
        retired_at=time.time(),
    )
    first = iter_retired_threads(home, max_rows=1)
    append_retired_thread(
        home,
        thread_id="waiting-thread",
        conversation_root=case.root / "conversations",
        action_id="blocked",
        trash_container=case.root / "trash/waiting",
        retired_at=time.time(),
    )
    journal = home.owner_data_dir / "retention/retired_threads.jsonl"
    if state == "waiting":
        (case.root / "trash/waiting").mkdir(parents=True)
    else:
        rows = journal.read_text().splitlines()
        row = json.loads(rows[-1])
        row["schema"] = "unknown.v99"
        journal.write_text(rows[0] + "\n" + json.dumps(row) + "\n")
    cursor, restored = {}, 0
    for _ in range(32):
        result = maintain_compact_reachability_lane(request_for(home, cursor=cursor))
        cursor = result["cursor"]
        restored += result["skipped"].get("restored", 0)
        if result["complete"]:
            break
    assert result["complete"] and result["cursor"]["journal_offset"] == first.next_offset
    assert result["cursor"]["phase"] == "live" and result["cursor"]["after"] == ""
    assert restored == 1
    assert result["skipped"]["retired_thread_" + state] == 1
    if state == "unknown":
        assert any(error["code"] == "retired_thread_state_unknown" for error in result["errors"])
    assert (case.root / "compact/conversations" / f"{case.thread_id}.jsonl").exists()


@pytest.mark.parametrize("portable", [False, True])
def test_touch_recreates_missing_leaf_and_rejects_symlink_parent(tmp_path, monkeypatch, portable):
    from agent_py_agent.agent.common import nofollow_fs as fs
    from agent_py_agent.agent.conversation.store_io import _touch_node

    if portable:
        monkeypatch.setattr(fs, "_supports_dir_fd", lambda: False)
    root = tmp_path / "root"
    index = ImmutableJsonIndex(root, ("index",))
    reference = index._save({"entries": {"kept": True}})
    parts = ("index", reference["ref"]["ref"])
    target = root.joinpath(*parts)
    raw = target.read_bytes()
    target.unlink()
    _touch_node(root, parts, raw)
    assert target.read_bytes() == raw
    external = tmp_path / "external"
    (root / "index").rename(external)
    (root / "index").symlink_to(external, target_is_directory=True)
    before = (external / parts[-1]).stat().st_mtime_ns
    with pytest.raises(OSError):
        _touch_node(root, parts, raw)
    assert (external / parts[-1]).stat().st_mtime_ns == before


def test_portable_sweep_keeps_reachable_and_deletes_only_aged_regular_leaf(tmp_path, monkeypatch):
    from agent_py_agent.agent.common import nofollow_fs as fs
    from agent_py_agent.agent.conversation.hamt_reachability import sweep_unreachable_nodes

    namespace = tmp_path / "index"
    namespace.mkdir()
    old, keep = namespace / "old.json", namespace / "keep.json"
    old.write_bytes(b"old")
    keep.write_bytes(b"keep")
    aged(old)
    aged(keep)
    monkeypatch.setattr(fs, "_supports_dir_fd", lambda: False)
    result = sweep_unreachable_nodes(
        namespace,
        reachable=frozenset({"keep.json"}),
        mark_started_at=time.time(),
        now=time.time(),
        max_items=8,
        run_write=lambda operation: operation(),
    )
    assert result["deleted"] == 1 and result["freed_bytes"] == 3
    assert not old.exists() and keep.read_bytes() == b"keep"


@pytest.mark.parametrize("kind", ["compact_checkpoints", "conversation_messages"])
def test_zero_budget_is_read_only_and_keeps_cursor_bounded(compact_case, kind):
    from agent_py_agent.agent.conversation.compact_reachability import (
        maintain_compact_reachability_lane,
    )
    from agent_py_agent.agent.conversation.transcript_index_gc import maintain_transcript_index_lane

    lane = (
        maintain_compact_reachability_lane
        if kind == "compact_checkpoints"
        else maintain_transcript_index_lane
    )
    commits = []
    result = lane(
        request_for(
            home_for(compact_case),
            kind=kind,
            items=0,
            commit=lambda operation: (commits.append(True), operation())[1],
        )
    )
    validate_lane_receipt(result)
    assert not result["complete"] and result["items"] == 0 and result["bytes"] == 0 and not commits


@pytest.mark.parametrize("nested", [False, True])
def test_conflicting_seals_of_same_name_make_mark_incomplete(tmp_path, nested):
    from agent_py_agent.agent.conversation.hamt_reachability import mark_reachable_nodes

    index = ImmutableJsonIndex(tmp_path, ("index",))
    good = index._save({"entries": {"key": True}})
    bad = {**good, "ref": {**good["ref"], "byte_length": good["ref"]["byte_length"] + 1}}
    roots = [index._save({"children": {"a": good, "b": bad}})] if nested else [good, bad]
    result = mark_reachable_nodes(index, roots, max_nodes=20)
    assert not result.complete and good["ref"]["ref"] in result.dangling


def test_previous_retirement_cannot_delete_current_waiting_generation(compact_case):
    from agent_py_agent.agent.conversation.compact_reachability import (
        maintain_compact_reachability_lane,
    )
    from agent_py_agent.agent.memory_store.retired_thread_journal import iter_retired_threads

    case = compact_case
    committed_pair(case)
    home = home_for(case)
    case.store.threads.storage.thread_path(case.thread_id).unlink()
    append_retired_thread(
        home,
        thread_id=case.thread_id,
        conversation_root=case.root / "conversations",
        action_id="old-restored",
        trash_container=case.root / "trash/old-gone",
        retired_at=time.time() - 86400,
    )
    first = iter_retired_threads(home, max_rows=1)
    append_retired_thread(
        home,
        thread_id=case.thread_id,
        conversation_root=case.root / "conversations",
        action_id="new-waiting",
        trash_container=case.root / "trash/current",
        retired_at=time.time(),
    )
    (case.root / "trash/current").mkdir(parents=True)
    before = {
        path: path.read_bytes() for path in (case.root / "compact").rglob("*") if path.is_file()
    }
    cursor = {}
    for _ in range(10):
        result = maintain_compact_reachability_lane(request_for(home, cursor=cursor))
        assert all(path.exists() and path.read_bytes() == raw for path, raw in before.items())
        cursor = result["cursor"]
        if result.get("deferred_reason") == "retired_thread_waiting":
            break
    assert cursor["journal_offset"] == first.next_offset
    assert result["complete"]


def test_live_zero_due_candidates_and_hold_make_zero_commits(compact_case):
    from agent_py_agent.agent.conversation.compact_reachability import (
        maintain_compact_reachability_lane,
    )

    case = compact_case
    committed_pair(case)
    commits = []
    request = request_for(
        home_for(case), commit=lambda operation: (commits.append(True), operation())[1]
    )
    result = maintain_compact_reachability_lane(request)
    assert result["complete"] and not commits
    held = maintain_compact_reachability_lane(
        replace(request, legal_hold=True, cursor={"after": "kept"})
    )
    assert held["complete"] and held["cursor"] == {"after": "kept"} and not commits


def test_one_item_slices_eventually_collect_compact_orphan(compact_case):
    from agent_py_agent.agent.conversation.compact_reachability import (
        maintain_compact_reachability_lane,
    )

    case = compact_case
    committed_pair(case)
    index = ImmutableJsonIndex(case.root / "compact", ("evidence", case.thread_id, "indexes"))
    reference = index._save({"entries": {"one-item-budget": True}})
    target = aged(index.root.joinpath(*index.parts, reference["ref"]["ref"]))
    cursor = {}
    for _ in range(20):
        result = maintain_compact_reachability_lane(
            request_for(home_for(case), cursor=cursor, items=1)
        )
        assert result["items"] <= 1
        cursor = result["cursor"]
        if result["complete"]:
            break
    assert result["complete"] and not target.exists()


def test_large_physical_retirement_row_is_not_undercharged_or_used_to_delete(compact_case):
    from agent_py_agent.agent.conversation.compact_reachability import (
        maintain_compact_reachability_lane,
    )

    case = compact_case
    committed_pair(case)
    home = home_for(case)
    case.store.threads.storage.thread_path(case.thread_id).unlink()
    append_retired_thread(
        home,
        thread_id=case.thread_id,
        conversation_root=case.root / "conversations",
        action_id="large",
        trash_container=case.root / "trash/gone",
        retired_at=time.time(),
    )
    journal = home.owner_data_dir / "retention/retired_threads.jsonl"
    journal.write_bytes(b" " * 50000 + journal.read_bytes())
    request = request_for(home)
    result = maintain_compact_reachability_lane(
        replace(request, budget=StorageLaneBudget(128, 1024, None))
    )
    assert result["bytes"] <= 1024 and result["deleted_items"] == 0
    assert result["cursor"]["journal_offset"] == 0
    assert (case.root / "compact/evidence" / case.thread_id).exists()


def test_retired_lookahead_with_one_item_budget_keeps_progress(compact_case):
    from agent_py_agent.agent.conversation.compact_reachability import (
        maintain_compact_reachability_lane,
    )

    case = compact_case
    committed_pair(case)
    home = home_for(case)
    case.store.threads.storage.thread_path(case.thread_id).unlink()
    for number, tid in enumerate((case.thread_id, "other-a", "other-b", "other-c")):
        append_retired_thread(
            home,
            thread_id=tid,
            conversation_root=case.root / "conversations",
            action_id=str(number),
            trash_container=case.root / "trash" / str(number),
            retired_at=time.time(),
        )
    cursor = {"phase": "retired"}
    for _ in range(80):
        result = maintain_compact_reachability_lane(request_for(home, cursor=cursor, items=1))
        validate_lane_receipt(result)
        assert result["items"] <= 1
        cursor = result["cursor"]
        if result["complete"]:
            break
    assert result["complete"]
    assert all(path.name.endswith('.lock') for path in (case.root / "compact/evidence" / case.thread_id).rglob('*') if path.is_file())


def test_retirement_generation_is_rechecked_inside_commit(compact_case):
    from agent_py_agent.agent.conversation.compact_reachability import (
        maintain_compact_reachability_lane,
    )

    case = compact_case
    committed_pair(case)
    home = home_for(case)
    case.store.threads.storage.thread_path(case.thread_id).unlink()
    append_retired_thread(
        home,
        thread_id=case.thread_id,
        conversation_root=case.root / "conversations",
        action_id="old",
        trash_container=case.root / "trash/old",
        retired_at=time.time(),
    )
    invoked = []

    def commit(operation):
        if not invoked:
            invoked.append(True)
            (case.root / "trash/new").mkdir(parents=True)
            append_retired_thread(
                home,
                thread_id=case.thread_id,
                conversation_root=case.root / "conversations",
                action_id="new",
                trash_container=case.root / "trash/new",
                retired_at=time.time(),
            )
        return operation()

    result = maintain_compact_reachability_lane(request_for(home, commit=commit))
    assert invoked and result["deleted_items"] == 0
    assert (case.root / "compact/evidence" / case.thread_id).exists()


def test_purged_record_with_no_remaining_objects_makes_zero_commits(compact_case):
    from agent_py_agent.agent.conversation.compact_reachability import (
        maintain_compact_reachability_lane,
    )

    case = compact_case
    home = home_for(case)
    (case.root / "compact/conversations").mkdir(parents=True)
    append_retired_thread(
        home,
        thread_id="empty-retired",
        conversation_root=case.root / "conversations",
        action_id="empty",
        trash_container=case.root / "trash/empty",
        retired_at=time.time(),
    )
    commits = []
    result = maintain_compact_reachability_lane(
        request_for(home, commit=lambda operation: (commits.append(True), operation())[1])
    )
    assert result["complete"] and not commits
