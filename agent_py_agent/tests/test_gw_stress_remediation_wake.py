"""GW-01 / MAX1-C8：唤醒发现只缓存 state.json / workspace_locator.json 的解析字段，从不缓存唤醒结论。

不变量：
- 缓存门与 common/cache_freshness 同源：普通文件 + (dev,ino,size,mtime_ns,ctime_ns) + mtime 离开 2 秒窗口；
- 新增、删除、改写 state/locator、删除工作区都立即改变签名；同尺寸原地改写并回拨 mtime 也由 ctime 识别；
- closeout、目标存在性、runtime 库与 BLOCKED 静止判据每轮现判；不稳定读、链接、admin 对账上下文不入缓存。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest

from agent_py_agent.agent import owner_wake_discovery as discovery
from agent_py_agent.agent.subagents.services.persistence import locator_hygiene


@pytest.fixture(autouse=True)
def _isolated_caches(monkeypatch):
    monkeypatch.setattr(locator_hygiene, "_WAKE_PROJECTION_RECORDS", type(locator_hygiene._WAKE_PROJECTION_RECORDS)())
    monkeypatch.setattr(locator_hygiene, "_SCANS", type(locator_hygiene._SCANS)())
    monkeypatch.setattr(discovery, "_OWNER_FACT_CACHE", type(discovery._OWNER_FACT_CACHE)())


# 函数用途: 把文件 mtime 回拨到粗粒度窗口之外（ctime 无法回拨，仍随任何改写变化）。
def _age(path: Path, seconds: float = 100.0) -> None:
    old = time.time_ns() - int(seconds * 1e9)
    os.utime(path, ns=(old, old))


def _write_run(home: Path, run_id: str, status: str, *, workspace: Path | None = None, aged: bool = True,
               **extra) -> Path:
    run_dir = home / "agents" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": "owner-agent-projection.v1", "agent_id": run_id, "run_id": run_id,
               "status": status, **extra}
    state = run_dir / "state.json"
    state.write_text(json.dumps(payload), encoding="utf-8")
    if workspace is not None:
        workspace.mkdir(parents=True, exist_ok=True)
        locator = run_dir / "workspace_locator.json"
        locator.write_text(json.dumps({"workspace_root": str(workspace)}), encoding="utf-8")
        if aged:
            _age(locator)
    if aged:
        _age(state)
    return run_dir


# 函数用途: 统计签名路径按 nofollow 入口解析投影 JSON 的次数，不改变读取结果。
@pytest.fixture
def projection_reads(monkeypatch):
    calls: list[Path] = []
    real = locator_hygiene.read_projection_object

    def read(path, *args, **kwargs):
        calls.append(Path(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(locator_hygiene, "read_projection_object", read)
    return calls


# 函数用途: 统计唤醒判定读取 state.json 正文的次数。
@pytest.fixture
def state_reads(monkeypatch):
    calls: list[Path] = []
    real = Path.read_text

    def read_text(path, *args, **kwargs):
        if path.name == "state.json" and path.parent.parent.name == "agents":
            calls.append(path)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    return calls


def _digest(home: Path) -> str:
    return discovery._owner_fact_signature_digest(home)[0]


def test_signature_reuses_parsed_records_for_unchanged_runs(tmp_path, projection_reads):
    home = tmp_path / "home"
    for index in range(6):
        _write_run(home, f"run-{index}", "DONE", workspace=tmp_path / f"ws-{index}")
    baseline = _digest(home)
    assert len(projection_reads) == 12, "冷启动必须按原入口解析 state 与 locator"
    projection_reads.clear()
    for _ in range(3):
        assert _digest(home) == baseline
    assert projection_reads == []


def test_signature_invalidates_on_add_remove_edit_locator_and_workspace_loss(tmp_path, projection_reads):
    home = tmp_path / "home"
    for index in range(4):
        _write_run(home, f"run-{index}", "DONE", workspace=tmp_path / f"ws-{index}")
    baseline = _digest(home)

    _write_run(home, "run-new", "RUNNING")
    added = _digest(home)
    assert added != baseline

    shutil.rmtree(home / "agents" / "run-new")
    removed = _digest(home)
    assert removed != added

    projection_reads.clear()
    _write_run(home, "run-1", "DONE", note="edited")
    edited = _digest(home)
    assert edited != removed
    # 只有被改写的 state.json 重新解析；其余运行（含 run-1 未变的 locator）全部复用解析记录。
    assert projection_reads == [home / "agents" / "run-1" / "state.json"]

    locator = home / "agents" / "run-2" / "workspace_locator.json"
    (tmp_path / "moved").mkdir()
    locator.write_text(json.dumps({"workspace_root": str(tmp_path / "moved")}), encoding="utf-8")
    _age(locator)
    relocated = _digest(home)
    assert relocated != edited

    shutil.rmtree(tmp_path / "ws-3")
    assert _digest(home) != relocated


def test_same_size_in_place_rewrite_with_restored_mtime_is_reread(tmp_path, state_reads):
    home = tmp_path / "home"
    run_dir = _write_run(home, "run-0", "DONE")
    state = run_dir / "state.json"
    state.write_text('{"run_id": "run-0", "status": "DONE", "runtime_closeout_pending": false}', encoding="utf-8")
    _age(state)
    before = os.stat(state)
    assert discovery._has_unfinished_subagent_run(home) is False
    assert discovery._has_unfinished_subagent_run(home) is False
    assert len(state_reads) == 1
    # 同 inode、同尺寸原地改写并回拨到同一 mtime：只有 ctime 变化，仍必须现读。
    state.write_text('{"run_id": "run-0", "status": "DONE", "runtime_closeout_pending": true }', encoding="utf-8")
    os.utime(state, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = os.stat(state)
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (before.st_ino, before.st_size, before.st_mtime_ns)
    assert discovery._has_unfinished_subagent_run(home) is True
    assert len(state_reads) == 2


def test_wake_path_reuses_parsed_state_for_terminal_runs(tmp_path, state_reads):
    home = tmp_path / "home"
    for index in range(6):
        _write_run(home, f"run-{index}", "DONE")
    assert discovery._has_unfinished_subagent_run(home) is False
    assert len(state_reads) == 6
    state_reads.clear()
    for _ in range(3):
        assert discovery._has_unfinished_subagent_run(home) is False
    assert state_reads == []

    # 原子换代成活跃：inode 变化立即重读，结论随字段现判。
    replacement = home / "agents" / "run-2" / "state.next"
    replacement.write_text(json.dumps({"run_id": "run-2", "status": "RUNNING"}), encoding="utf-8")
    _age(replacement)
    os.replace(replacement, home / "agents" / "run-2" / "state.json")
    assert discovery._has_unfinished_subagent_run(home) is True
    assert [path.parent.name for path in state_reads] == ["run-2"]


def test_runtime_facts_and_verdict_are_recomputed_every_pass(tmp_path, monkeypatch, state_reads):
    home = tmp_path / "home"
    _write_run(home, "run-0", "RUNNING", workspace=tmp_path / "ws")
    runtime = [("running", False), ("done", False), ("running", False)]
    calls = []

    def facts(owner_home, run_id, payload):
        calls.append((run_id, payload.get("status")))
        return runtime[len(calls) - 1]

    monkeypatch.setattr(locator_hygiene, "projection_runtime_facts", facts)
    outcomes = [discovery._has_unfinished_subagent_run(home) for _ in range(3)]
    # 解析字段只读一次，但 runtime 库每轮现查；终态 runtime 行立即把同一份字段判成无活，下一轮又恢复。
    assert len(state_reads) == 1
    assert calls == [("run-0", "RUNNING")] * 3
    assert outcomes == [True, False, True]


def test_blocked_quiescence_is_probed_every_pass(tmp_path, monkeypatch, state_reads):
    home = tmp_path / "home"
    _write_run(home, "run-0", "BLOCKED", workspace=tmp_path / "ws")
    answers = [True, False, True]
    calls = []

    def quiescent(owner_home, run_id, payload):
        calls.append(run_id)
        return answers[len(calls) - 1]

    monkeypatch.setattr(discovery, "_blocked_subagent_is_quiescent", quiescent)
    monkeypatch.setattr(locator_hygiene, "projection_runtime_facts", lambda *a, **k: ("blocked", False))
    assert [discovery._has_unfinished_subagent_run(home) for _ in range(3)] == [False, True, False]
    assert calls == ["run-0"] * 3
    assert len(state_reads) == 1


def test_orphan_target_existence_is_checked_every_pass(tmp_path, monkeypatch, state_reads):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    _write_run(home, "run-0", "RUNNING", workspace=workspace)
    monkeypatch.setattr(locator_hygiene, "projection_runtime_facts", lambda *a, **k: ("", False))
    assert discovery._has_unfinished_subagent_run(home) is True
    assert discovery._has_unfinished_subagent_run(home) is True
    # state/locator 均未改：只删工作区，下一轮即按悬空孤儿跳过；再建回又恢复唤醒。
    shutil.rmtree(workspace)
    assert discovery._has_unfinished_subagent_run(home) is False
    workspace.mkdir()
    assert discovery._has_unfinished_subagent_run(home) is True
    assert len(state_reads) == 1


def test_recent_writes_are_never_cached(tmp_path, state_reads):
    home = tmp_path / "home"
    for index in range(3):
        _write_run(home, f"run-{index}", "DONE", aged=False)
    assert discovery._has_unfinished_subagent_run(home) is False
    assert discovery._has_unfinished_subagent_run(home) is False
    assert len(state_reads) == 6
    assert len(locator_hygiene._WAKE_PROJECTION_RECORDS) == 0


def test_unstable_read_is_returned_but_not_cached(tmp_path):
    run_dir = _write_run(tmp_path / "home", "run-0", "DONE")
    state = run_dir / "state.json"
    reads = []

    def racing_read(path):
        reads.append(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if len(reads) == 1:
            # 读与读后 lstat 之间被改写：本次结果照常返回，但不能成为缓存记录。
            path.write_text(json.dumps({"run_id": "run-0", "status": "RUNNING"}), encoding="utf-8")
            _age(path)
        return payload

    first = locator_hygiene.wake_projection_fields(state, os.lstat(state), racing_read)
    assert first["status"] == "DONE"
    assert locator_hygiene._WAKE_PROJECTION_RECORDS == {}
    second = locator_hygiene.wake_projection_fields(state, os.lstat(state), racing_read)
    assert second["status"] == "RUNNING" and len(reads) == 2


def test_symlinked_state_is_never_cached(tmp_path):
    home = tmp_path / "home"
    run_dir = _write_run(home, "run-0", "DONE")
    real = tmp_path / "real-state.json"
    real.write_text(json.dumps({"run_id": "run-0", "status": "RUNNING"}), encoding="utf-8")
    _age(real)
    link = run_dir / "state.json"
    link.unlink()
    link.symlink_to(real)
    reads = []

    def read(path):
        reads.append(path)
        return json.loads(path.read_text(encoding="utf-8"))

    for _ in range(2):
        locator_hygiene.wake_projection_fields(link, os.lstat(link), read)
    assert len(reads) == 2
    assert locator_hygiene._WAKE_PROJECTION_RECORDS == {}
    # 签名路径仍走原 nofollow 入口：链接投影照旧让签名失败、拒绝缓存 owner 结论。
    with pytest.raises(OSError):
        discovery._subagent_page_signature(home)


def test_admin_reconcile_context_bypasses_cache(tmp_path):
    run_dir = _write_run(tmp_path / "home", "run-0", "DONE")
    state = run_dir / "state.json"
    reads = []

    def read(path):
        reads.append(path)
        return locator_hygiene.read_projection_object(path)

    token = locator_hygiene._ADMIN_RECONCILE.set(("probe", None, {"bytes": 0}))
    try:
        for _ in range(2):
            locator_hygiene.wake_projection_fields(state, os.lstat(state), read)
    finally:
        locator_hygiene._ADMIN_RECONCILE.reset(token)
    assert len(reads) == 2
    assert locator_hygiene._WAKE_PROJECTION_RECORDS == {}


def test_expired_records_are_reverified_from_disk(tmp_path, monkeypatch, state_reads):
    home = tmp_path / "home"
    _write_run(home, "run-0", "DONE")
    monkeypatch.setattr(locator_hygiene, "_WAKE_PROJECTION_REVERIFY_SECONDS", -1.0)
    for _ in range(3):
        assert discovery._has_unfinished_subagent_run(home) is False
    assert len(state_reads) == 3


def test_cached_fields_are_copies_without_unused_payload(tmp_path):
    run_dir = _write_run(tmp_path / "home", "run-0", "DONE", last_progress_summary="x" * 4096,
                         task_id="task-1", root_id="task-1")
    state = run_dir / "state.json"
    first = locator_hygiene.wake_projection_fields(state, os.lstat(state), locator_hygiene.read_projection_object)
    assert "last_progress_summary" not in first and first["task_id"] == "task-1"
    first["status"] = "RUNNING"
    second = locator_hygiene.wake_projection_fields(state, os.lstat(state), locator_hygiene.read_projection_object)
    assert second["status"] == "DONE"


def test_owner_fact_cache_invalidates_immediately_on_run_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "_FACT_MIN_CACHED_SECONDS", 0)
    monkeypatch.setattr(discovery, "_owner_has_soft_facts", lambda *a, **k: False)
    home = tmp_path / "home"
    for index in range(5):
        _write_run(home, f"run-{index}", "DONE")
    assert [discovery._owner_fact_kind(home) for _ in range(3)] == ["none"] * 3

    _write_run(home, "run-active", "RUNNING")
    assert discovery._owner_fact_kind(home) == "hard"
    shutil.rmtree(home / "agents" / "run-active")
    assert discovery._owner_fact_kind(home) == "none"
    _write_run(home, "run-3", "PENDING")
    assert discovery._owner_fact_kind(home) == "hard"
    _write_run(home, "run-3", "DONE")
    assert discovery._owner_fact_kind(home) == "none"


def test_parsed_record_cache_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(locator_hygiene, "_WAKE_PROJECTION_RECORD_MAX_ENTRIES", 3)
    home = tmp_path / "home"
    for index in range(7):
        _write_run(home, f"run-{index}", "DONE", workspace=tmp_path / f"ws-{index}")
    _digest(home)
    discovery._has_unfinished_subagent_run(home)
    assert len(locator_hygiene._WAKE_PROJECTION_RECORDS) == 3
