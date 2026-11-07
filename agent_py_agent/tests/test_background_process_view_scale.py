"""用真实临时记录验证持续写入、多 owner 容量及历史目录的只读缓存边界。"""
from __future__ import annotations

import errno
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import background_process_view as bg


@pytest.fixture(autouse=True)
def clear_caches():
    reset_capacity = getattr(bg.cache_capacity, "reset_for_tests", lambda: None)
    reset_capacity()
    bg._clear_background_process_view_cache()
    yield
    bg._clear_background_process_view_cache()
    reset_capacity()


def write_record(root, session_id, *, running=False, command="original", trusted=True, **extra):
    root.mkdir(parents=True, exist_ok=True)
    payload = {"session_id": session_id, "status": "running" if running else "exited",
               "thread_id": "scale-thread", "command": command,
               "metadata": {"marker": "original", "unused": list(range(32))}, **extra}
    path = root / f"{session_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    if trusted:
        old = time.time() - 10
        os.utime(path, (old, old))
    return path


def count_json_reads(monkeypatch):
    original, reads = json.loads, []

    def loads(*args, **kwargs):
        reads.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(bg.json, "loads", loads)
    return reads


def count_directory_scans(monkeypatch, root):
    original, scans = os.scandir, []

    def scandir(path):
        if path == root:
            scans.append(path)
        return original(path)

    monkeypatch.setattr(bg.os, "scandir", scandir)
    return scans


def configure_capacity(monkeypatch, config):
    configure = getattr(bg.cache_capacity, "configure_from_config", None)
    if configure is not None:
        configure(config)
        return
    # R0 独立开发期间只注入它的结构化容量接口，不复制或提交 R0 模块。
    owners = (getattr(config, "gateway_owner_inflight_limit", 0)
              or getattr(config, "owner_agent_pool_max_agents", 0))
    monkeypatch.setattr(bg.cache_capacity, "owners_scaled", lambda floor: floor * max(1, owners))


def test_continuous_directory_writes_only_parse_changed_files_and_copy_returned_rows(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    root = home / "workspace/runtime/processes/sessions"
    for index in range(1200):
        write_record(root, f"bg-scale-{index:04d}", running=index < 5)
    monkeypatch.setattr(bg, "_managed_instance_still_live", lambda *_args: True)
    reads = count_json_reads(monkeypatch)
    original_copy, copied_sizes = bg.deepcopy, []

    def copy_rows(value):
        copied_sizes.append(len(value))
        return original_copy(value)

    monkeypatch.setattr(bg, "deepcopy", copy_rows)
    first = bg.list_background_processes(home, thread_id="scale-thread", active_only=True)
    assert first["projection_ok"] and first["total"] == 1200
    assert len(reads) == 1200 and copied_sizes == [5]

    for generation in range(5):
        for index in range(5):
            write_record(root, f"bg-scale-{index:04d}", running=True,
                         command=f"generation-{generation}", trusted=False)
        # 新增已完成记录保留旧 mtime，目录本身仍在不断变动；最近活跃文件保持不可信。
        write_record(root, f"bg-added-{generation}")
        assert not bg.cache_entry_trustworthy(root.stat().st_mtime_ns)
        reads.clear()
        copied_sizes.clear()
        result = bg.list_background_processes(home, thread_id="scale-thread", active_only=True)
        assert result["projection_ok"] and result["total"] == 1201 + generation
        assert len(result["items"]) == result["active_count"] == 5
        assert {row["command"] for row in result["items"]} == {f"generation-{generation}"}
        assert len(reads) == 6
        assert copied_sizes == [len(result["items"])]
        assert len(copied_sizes) <= len(result["items"])
        result["items"][0]["command"] = "caller-mutated"


def test_recent_directory_reuses_names_but_recent_files_are_reparsed(tmp_path, monkeypatch):
    root = tmp_path / "workspace/runtime/processes/sessions"
    write_record(root, "bg-recent", trusted=False)
    reads = count_json_reads(monkeypatch)
    scans = count_directory_scans(monkeypatch, root)
    first = bg.list_background_processes(tmp_path)
    second = bg.list_background_processes(tmp_path)
    assert first == second and first["projection_ok"]
    assert len(scans) == 1 and len(reads) == 2


def test_eight_configured_owners_keep_at_least_ninety_percent_cache_hits(tmp_path, monkeypatch):
    configure_capacity(monkeypatch, SimpleNamespace(owner_agent_pool_max_agents=8))
    monkeypatch.setattr(bg, "_PROCESS_VIEW_DIR_CACHE_MAX_ENTRIES", 1)
    homes = [tmp_path / f"owner-{index}" for index in range(8)]
    for home in homes:
        root = home / "workspace/runtime/processes/sessions"
        for index in range(300):
            write_record(root, f"bg-owner-{index:04d}")
    reads = count_json_reads(monkeypatch)
    for home in homes:
        assert bg.list_background_processes(home)["total"] == 300
    assert len(reads) == 2400
    assert len(bg._FILE_CACHE) == 2400
    assert len(bg._DIRECTORY_CACHE) == 8
    reads.clear()
    for _round in range(3):
        for home in homes:
            report = bg.list_background_processes(home)
            assert report["projection_ok"] and report["total"] == 300
    assert 1 - len(reads) / (3 * 8 * 300) >= 0.9


def test_configured_capacities_still_evict_with_lru_bounds(tmp_path, monkeypatch):
    configure_capacity(monkeypatch, SimpleNamespace(owner_agent_pool_max_agents=2))
    monkeypatch.setattr(bg, "_PROCESS_VIEW_DIR_CACHE_MAX_ENTRIES", 1)
    monkeypatch.setattr(bg, "_FILE_CACHE_MAX_ENTRIES", 1)
    homes = [tmp_path / f"owner-{index}" for index in range(3)]
    for home in homes:
        write_record(home / "workspace/runtime/processes/sessions", "bg-one")
        assert bg.list_background_processes(home)["total"] == 1
    assert len(bg._FILE_CACHE) == len(bg._DIRECTORY_CACHE) == 2
    reads = count_json_reads(monkeypatch)
    assert bg.list_background_processes(homes[-1])["total"] == 1
    assert not reads
    assert bg.list_background_processes(homes[0])["total"] == 1
    assert len(reads) == 1


@pytest.mark.parametrize("finished_exists", [False, True])
def test_active_view_never_scans_finished_and_full_view_uses_file_cache(tmp_path, monkeypatch, finished_exists):
    home = tmp_path / "owner"
    root = bg.process_session_store_root(home, home)
    finished = root / "finished"
    write_record(root, "bg-live", running=True)
    if finished_exists:
        for index in range(6):
            write_record(finished, f"bg-finished-{index}")
    monkeypatch.setattr(bg, "_managed_instance_still_live", lambda *_args: True)
    original_scandir, visited = os.scandir, []

    def scandir(path):
        visited.append(Path(path))
        return original_scandir(path)

    monkeypatch.setattr(bg.os, "scandir", scandir)
    reads = count_json_reads(monkeypatch)
    for _ in range(2):
        active = bg.list_background_processes(home, active_only=True)
        assert active["projection_ok"] and active["total"] == 1
        assert [row["session_id"] for row in active["items"]] == ["bg-live"]
    assert finished not in visited and len(reads) == 1
    full = bg.list_background_processes(home, active_only=False)
    assert full["projection_ok"] and full["total"] == (7 if finished_exists else 1)
    assert (finished in visited) is finished_exists
    parsed = len(reads)
    assert bg.list_background_processes(home, active_only=False)["items"] == full["items"]
    assert len(reads) == parsed
    visited.clear()
    assert bg.list_background_processes(home, active_only=True)["total"] == 1
    assert finished not in visited


def test_finished_detail_reads_log_from_original_store_without_writing(tmp_path):
    home = tmp_path / "owner"
    root = bg.process_session_store_root(home, home)
    finished = root / "finished"
    path = write_record(finished, "bg-finished", output_path=str(root / "bg-finished.log"))
    log = root / "bg-finished.log"
    log.write_text("first\nlast\n", encoding="utf-8")
    before = path.read_bytes(), path.stat(), log.stat()
    detail = bg.inspect_background_process(home, "bg-finished", thread_id="scale-thread", tail_lines=1)
    assert detail["status"] == "exited" and detail["log_tail"] == ["last"]
    assert (path.read_bytes(), path.stat(), log.stat()) == before


def test_finished_discovery_permission_error_stays_in_projection(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    root = bg.process_session_store_root(home, home)
    finished = root / "finished"
    write_record(root, "bg-terminal")
    original_lstat, original_key = Path.lstat, bg._directory_stat_key

    def lstat(path, *args, **kwargs):
        if path == finished:
            raise PermissionError(errno.EACCES, "history denied")
        return original_lstat(path, *args, **kwargs)

    def directory_key(path):
        if path == finished:
            raise PermissionError(errno.EACCES, "history denied")
        return original_key(path)

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(bg, "_directory_stat_key", directory_key)
    active = bg.list_background_processes(home, active_only=True)
    assert active["projection_ok"] and active["errors"] == []
    full = bg.list_background_processes(home)
    assert not full["projection_ok"] and full["total"] == 1
    assert full["errors"][0]["error_code"] == "PERMISSION_DENIED"
    assert full["errors"][0]["errno"] == errno.EACCES


def test_finished_dangling_symlink_is_rejected_instead_of_hidden(tmp_path):
    home = tmp_path / "owner"
    root = bg.process_session_store_root(home, home)
    write_record(root, "bg-terminal")
    (root / "finished").symlink_to(tmp_path / "missing-history", target_is_directory=True)
    assert bg.list_background_processes(home, active_only=True)["projection_ok"]
    full = bg.list_background_processes(home)
    assert not full["projection_ok"] and full["total"] == 1
    assert full["errors"][0]["error"] == "NoFollowPathError"
