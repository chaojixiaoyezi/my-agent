"""定位投影的增量、身份、覆盖与历史规模合同，不把索引当权限。"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_py_agent.agent.user_space.home_indexes import TaskIndexRef, register_task_ref
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)


def _locator():
    name = "agent_py_agent.agent.conversation.task_locator"
    assert importlib.util.find_spec(name) is not None, "缺少冻结的 TASK_LOCATOR 端口"
    return importlib.import_module(name)


def _home(tmp_path, identity=None):
    base = home_paths(tmp_path / "root")
    owner = resolve_owner_home(base.root, identity or OwnerIdentity.local_main())
    owner.home_dir.mkdir(parents=True, exist_ok=True)
    return home_paths_with_owner(base, owner)


def _task(home, task_id, slug, *, publish=False, owner_id=None):
    root = home.owner_tasks_dir / "2026-11-01" / slug
    state = root / "work" / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({
        "task_id": task_id, "owner_id": home.owner_id if owner_id is None else owner_id, "status": "RUNNING",
    }), encoding="utf-8")
    if publish:
        register_task_ref(home, TaskIndexRef(home.owner_id, task_id, root, "active"))
    return root


def _finish(home, task_id, *, max_slices=200):
    cursor = None
    pages = []
    for _ in range(max_slices):
        result = _locator().resolve_task_location(home, task_id, cursor=cursor)
        pages.append(result)
        if result["status"] != "partial":
            return result, pages
        cursor = result["cursor"]
    pytest.fail(f"定位分片未收敛: {pages[-1]}")


def test_existing_publisher_is_consumed_without_legacy_scan(tmp_path, monkeypatch):
    home = _home(tmp_path)
    target = _task(home, "task-current", "current", publish=True)

    def no_scan(*args, **kwargs):
        raise AssertionError("已有 publisher 的路径不能遍历 tasks")

    monkeypatch.setattr(os, "scandir", no_scan)
    result = _locator().resolve_task_location(home, "task-current")
    assert result["status"] == "found" and Path(result["path"]) == target


@pytest.mark.parametrize("mutation", ["append", "truncate", "replace"])
def test_incremental_source_invalidates_after_migration(tmp_path, mutation):
    home = _home(tmp_path)
    old = _task(home, "task-move", "old", publish=True)
    assert _finish(home, "task-move")[0]["path"] == str(old)
    new = _task(home, "task-move", "new")
    register_task_ref(home, TaskIndexRef(home.owner_id, "task-move", new, "active"))
    source = home.global_index_active_tasks_jsonl
    newest = source.read_bytes().splitlines(keepends=True)[-1]
    if mutation == "truncate":
        source.write_bytes(newest)
    elif mutation == "replace":
        replacement = source.with_suffix(".replacement")
        replacement.write_bytes(newest)
        replacement.replace(source)
    assert _finish(home, "task-move")[0]["path"] == str(new)


def test_bad_jsonl_line_is_isolated_and_tail_waits_for_newline(tmp_path):
    home = _home(tmp_path)
    target = _task(home, "task-good", "good", publish=True)
    source = home.global_index_active_tasks_jsonl
    data = source.read_bytes()
    source.write_bytes(b"{broken}\n[]\n" + data)
    assert _finish(home, "task-good")[0]["path"] == str(target)
    late = _task(home, "task-late", "late")
    record = json.dumps({"schema_version": "global-task-index.v1", "owner_id": home.owner_id,
                         "task_id": "task-late", "task_path": str(late)}).encode()
    with source.open("ab") as stream:
        stream.write(record[:-3])
    assert _locator().resolve_task_location(home, "task-late")["status"] == "partial"
    with source.open("ab") as stream:
        stream.write(record[-3:] + b"\n")
    assert _finish(home, "task-late")[0]["path"] == str(late)


def test_oversized_broken_index_line_does_not_poison_following_records(tmp_path):
    home = _home(tmp_path)
    target = _task(home, "wanted", "wanted", publish=True)
    source = home.global_index_active_tasks_jsonl
    source.write_bytes(b"x" * 70000 + b"\n" + source.read_bytes())
    result, _ = _finish(home, "wanted", max_slices=3)
    assert result["status"] == "found" and result["path"] == str(target)


def test_deleting_disposable_projection_rebuilds_without_touching_state(tmp_path):
    home = _home(tmp_path)
    target = _task(home, "wanted", "wanted")
    before = (target / "work" / "state.json").read_bytes()
    assert _finish(home, "wanted")[0]["status"] == "found"
    (home.owner_cache_dir / "task_locator.sqlite3").unlink()
    assert _finish(home, "wanted")[0]["path"] == str(target)
    assert (target / "work" / "state.json").read_bytes() == before


def test_corrupt_index_does_not_turn_unknown_custom_location_into_missing(tmp_path):
    home = _home(tmp_path)
    target = home.owner_workspace_dir / "custom-task" / "work" / "state.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"task_id": "wanted", "owner_id": home.owner_id}))
    register_task_ref(home, TaskIndexRef(home.owner_id, "other", home.owner_tasks_dir / "other", "active"))
    source = home.global_index_active_tasks_jsonl
    source.write_bytes(b"{broken}\n" + source.read_bytes())
    assert _locator().resolve_task_location(home, "wanted")["status"] == "partial"
    assert _locator().resolve_task_location(home, "wanted")["status"] == "partial"


def test_transient_state_absence_does_not_delete_published_location_hint(tmp_path):
    home = _home(tmp_path)
    root = home.owner_workspace_dir / "custom-task"
    state = root / "work" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"task_id": "wanted", "owner_id": home.owner_id}))
    register_task_ref(home, TaskIndexRef(home.owner_id, "wanted", root, "active"))
    assert _finish(home, "wanted")[0]["status"] == "found"
    before = home.global_index_active_tasks_jsonl.read_bytes()
    saved = state.with_suffix(".saved")
    state.rename(saved)
    assert _locator().resolve_task_location(home, "wanted")["status"] != "found"
    saved.rename(state)
    assert _locator().resolve_task_location(home, "wanted")["status"] == "found"
    assert home.global_index_active_tasks_jsonl.read_bytes() == before


def test_same_task_id_isolated_by_owner_and_home_root(tmp_path):
    alice = _home(tmp_path, OwnerIdentity.local_user("alice"))
    bob = _home(tmp_path, OwnerIdentity.local_user("bob"))
    a = _task(alice, "same-id", "alice", publish=True)
    b = _task(bob, "same-id", "bob", publish=True)
    other_home = _home(tmp_path / "other")
    c = _task(other_home, "same-id", "other", publish=True)
    for home, target in [(alice, a), (bob, b), (other_home, c), (alice, a)]:
        assert _finish(home, "same-id")[0]["path"] == str(target)


@pytest.mark.parametrize("kind", ["outside", "symlink", "wrong-owner", "wrong-task"])
def test_index_cannot_authorize_wrong_candidate(tmp_path, kind):
    home = _home(tmp_path)
    if kind == "outside":
        other = _home(tmp_path / "outside")
        target = _task(other, "wanted", "outside", owner_id=home.owner_id)
    else:
        target = _task(home, "other" if kind == "wrong-task" else "wanted", "candidate",
                       owner_id="local/other" if kind == "wrong-owner" else home.owner_id)
    if kind == "symlink":
        link = home.owner_tasks_dir / "2026-11-01" / "linked"
        link.symlink_to(target, target_is_directory=True)
        # 即使指向同 owner，索引里的软链接本身也不是 canonical 候选。
        target = link
    register_task_ref(home, TaskIndexRef(home.owner_id, "wanted", target, "done"))
    result = _locator().resolve_task_location(home, "wanted")
    assert result["status"] != "found" or Path(result["path"]) != target
    assert "status" in result and "coverage" in result


def test_legacy_budget_missing_and_restart_cursor(tmp_path):
    home = _home(tmp_path)
    for i in range(300):
        _task(home, f"task-{i}", f"task-{i:04d}")
    first = _locator().resolve_task_location(home, "absent")
    assert first["status"] == "partial" and first["cursor"]
    assert 0 < first["coverage"]["legacy_files_scanned"] <= 128
    repo = str(Path(__file__).resolve().parents[2])
    program = (
        "import json; from agent_py_agent.agent.conversation.task_locator import resolve_task_location; "
        f"print(json.dumps(resolve_task_location({str(home.owner_home_dir)!r}, 'absent', cursor={first['cursor']!r})))"
    )
    completed = subprocess.run([sys.executable, "-c", program], check=True, capture_output=True, text=True,
                               env={**os.environ, "PYTHONPATH": repo})
    resumed = json.loads(completed.stdout)
    assert resumed["coverage"]["legacy_total_scanned"] > first["coverage"]["legacy_total_scanned"]
    result, pages = _finish(home, "absent")
    assert result["status"] == "missing" and result["coverage"]["legacy_complete"]
    assert all(p["coverage"]["legacy_files_scanned"] <= 128 for p in pages)


def test_owner_root_symlink_does_not_rebind_lookup_to_another_home(tmp_path):
    home, other = _home(tmp_path), _home(tmp_path / "other")
    _task(other, "wanted", "foreign")
    register_task_ref(home, TaskIndexRef(home.owner_id, "wanted", home.owner_tasks_dir / "day" / "foreign", "active"))
    home.owner_home_dir.rename(home.owner_home_dir.with_name("original"))
    home.owner_home_dir.symlink_to(other.owner_home_dir, target_is_directory=True)
    assert _locator().resolve_task_location(home, "wanted")["status"] == "partial"


def test_negative_revision_check_stays_partial_until_all_date_directories_checked(tmp_path):
    home = _home(tmp_path)
    for index in range(150):
        state = home.owner_tasks_dir / f"day-{index:04d}" / "old" / "work" / "state.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"task_id": f"old-{index}", "owner_id": home.owner_id}))
    assert _finish(home, "wanted")[0]["status"] == "missing"
    new = home.owner_tasks_dir / "day-0149" / "new" / "work" / "state.json"
    new.parent.mkdir(parents=True)
    new.write_text(json.dumps({"task_id": "wanted", "owner_id": home.owner_id}))
    first = _locator().resolve_task_location(home, "wanted")
    assert first["status"] == "partial"
    result, _ = _finish(home, "wanted")
    assert result["status"] == "found" and Path(result["path"]) == new.parent.parent


def test_interrupted_slice_rolls_back_checkpoint_and_rebuild_can_resume(tmp_path, monkeypatch):
    home = _home(tmp_path)
    for index in range(150):
        _task(home, f"old-{index}", f"old-{index:04d}")
    module = _locator()
    original = module._legacy_slice

    def interrupt(scope, conn):
        original(scope, conn)
        raise OSError("controlled crash before projection commit")

    with monkeypatch.context() as context:
        context.setattr(module, "_legacy_slice", interrupt)
        assert module.resolve_task_location(home, "absent")["status"] == "partial"
    result, _ = _finish(home, "absent")
    assert result["status"] == "missing" and result["coverage"]["legacy_total_scanned"] == 150


def test_publisher_history_is_ingested_in_bounded_slices(tmp_path):
    home = _home(tmp_path)
    for index in range(300):
        register_task_ref(home, TaskIndexRef(home.owner_id, f"old-{index}", home.owner_tasks_dir / str(index), "active"))
    target = _task(home, "wanted", "wanted", publish=True)
    result, pages = _finish(home, "wanted")
    assert pages[0]["status"] == "partial"
    assert all(p["coverage"]["index_records_read"] <= 128 for p in pages)
    assert result["status"] == "found" and result["path"] == str(target)


@pytest.mark.parametrize("size", [100, 1000, 10000])
def test_warm_legacy_hit_has_constant_reads_and_no_task_walk(tmp_path, monkeypatch, size):
    home = _home(tmp_path)
    for i in range(size):
        _task(home, f"task-{i}", f"task-{i:05d}")
    target_id = f"task-{size - 1}"
    result, _ = _finish(home, target_id)
    assert result["status"] == "found"
    calls = {"read": 0, "json": 0}
    module = _locator()
    original_read, original_loads = module.read_bytes_beneath, json.loads

    def count_read(root, parts, **kwargs):
        if parts[-1] == "state.json":
            calls["read"] += 1
        return original_read(root, parts, **kwargs)

    def count_json(*args, **kwargs):
        calls["json"] += 1
        return original_loads(*args, **kwargs)

    def no_walk(*args, **kwargs):
        raise AssertionError("warm hit 不许遍历 tasks 或全量 glob")

    monkeypatch.setattr(module, "read_bytes_beneath", count_read)
    monkeypatch.setattr(json, "loads", count_json)
    monkeypatch.setattr(os, "scandir", no_walk)
    monkeypatch.setattr(Path, "glob", no_walk)
    warm = _locator().resolve_task_location(home, target_id)
    assert warm["status"] == "found" and warm["path"] == result["path"]
    assert calls == {"read": 1, "json": 1}
    assert warm["coverage"]["legacy_files_scanned"] == 0
