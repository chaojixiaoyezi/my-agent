"""Regression locks for the 2026-11-07 Gateway/TUI per-round stall hotfix and its scale follow-up.

Root cause A: every safe-point reconcile re-read and re-validated every historical process-session JSON file
under the exclusive store lock, because concurrent writers keep the directory mtime inside the 2 s trust window.
Root cause B: one owner SkillsService kept a single manifest slot, so alternating workspaces (default workspace vs.
task workspace / worktrees) rebuilt the manifest and snapshot on every call; editable skill trees were re-listed
on every signature. Root cause C: ``events_for_agent_run`` had no ``(agent_run_id, seq)`` index.

Scale follow-up (dozens of agents per owner, hundreds to thousands of owners per Gateway): cache bounds follow the
configured owner pool / per-owner agent slots instead of fixed slot counts; concurrent misses on one owner coalesce
into one locked scan or one discovery instead of queueing on ``.process-sessions.lock`` / ``SkillsService._lock``;
the pending-completion scan only checks the active pending set; skill discovery and snapshot assembly reuse
unchanged per-root/per-skill work, so a new workspace or one changed skill no longer rebuilds everything.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace as dataclass_replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import agent_py_agent.agent.capability.skill_service as skills
import agent_py_agent.agent.tooling.process_session_store as process_store_module
from agent_py_agent.agent.conversation.process_events import reconcile_process_completions
from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool
from agent_py_agent.agent.runtime_db import schema as runtime_schema
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.settings import defaults as settings_defaults
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore
from agent_py_agent.tests.test_background_tick_cheap_checks import (
    _age_path,
    _running,
    _TransactionCounter,
)
from agent_py_agent.tests.test_process_completion_events import _fixture

_INDEX = "idx_runtime_events_agent_run_seq"


@pytest.fixture(autouse=True)
def _isolated_module_caches():
    caches = (process_store_module._PROCESS_RECORDS_PROJECTION_CACHE, process_store_module._PROCESS_RECORD_FILES_CACHE,
              skills._PROCESS_BUILTIN_ROOT_CACHE, skills._PROCESS_SKILL_DIR_CACHE)
    for cache in caches:
        cache.clear()
    yield
    for cache in caches:
        cache.clear()


# 函数用途: 统计存储层逐文件读取+解析+校验的次数（扫描与单条读取共用同一模块入口）。
def _count_record_reads(monkeypatch) -> list[str]:
    reads: list[str] = []
    original = process_store_module.read_process_record

    def counting(root, session_id):
        reads.append(session_id)
        return original(root, session_id)

    monkeypatch.setattr(process_store_module, "read_process_record", counting)
    return reads


# 函数用途: 把全部会话文件拨出 2 秒窗口，并让目录 mtime 保持"刚被写过"（多会话并发写入的稳态）。
def _settle_files_keep_directory_hot(root: Path) -> None:
    for path in root.glob("bg-*.json"):
        _age_path(path)
    os.utime(root, None)


def test_hot_directory_scan_reuses_unchanged_record_files(tmp_path, monkeypatch):
    store = ProcessSessionStore(tmp_path / "records")
    for index in range(6):
        store.write(_running(f"bg-{index}"))
    _settle_files_keep_directory_hot(store.root)
    reads = _count_record_reads(monkeypatch)

    first = store.list_records()
    assert sorted(reads) == [f"bg-{index}" for index in range(6)]
    for _ in range(3):
        os.utime(store.root, None)  # 目录始终在窗口内：目录级投影不可命中，只剩逐文件复用
        assert store.list_records() == first
        assert store.list_pending_completion_records_report().load_errors == []
    assert len(reads) == 6
    assert process_store_module._PROCESS_RECORDS_PROJECTION_CACHE == {}  # 目录级 MC4-1201 规则不变


def test_record_file_cache_rereads_changed_fresh_and_deleted_files(tmp_path, monkeypatch):
    store = ProcessSessionStore(tmp_path / "records")
    current = {index: store.write(_running(f"bg-{index}")) for index in range(3)}
    _settle_files_keep_directory_hot(store.root)
    reads = _count_record_reads(monkeypatch)
    store.list_records()
    reads.clear()

    # 原子改写：新代次文件仍在 2 秒窗口内，每次都重读且不入缓存，其余文件继续复用。
    finished = store.write({**current[0], "status": "exited", "finished_at": 3.0, "exit_code": 0})
    reads.clear()  # 写入自身的 CAS 读取不计
    for _ in range(2):
        os.utime(store.root, None)
        rows, errors = store.list_records()
        assert errors == [] and {row["session_id"]: row["status"] for row in rows}["bg-0"] == "exited"
    assert reads == ["bg-0", "bg-0"]

    # 原地重写并复原 mtime：ctime 参与文件指纹，仍判为新代次。
    path = store.root / "bg-1.json"
    _age_path(store.root / "bg-0.json")
    store.list_records()
    reads.clear()
    stamp = path.stat()
    path.write_bytes(path.read_bytes())
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    os.utime(store.root, None)
    store.list_records()
    assert reads == ["bg-1"]

    assert store.prune_finished(0) == [finished["session_id"]]
    os.utime(store.root, None)
    rows, errors = store.list_records()
    assert errors == [] and sorted(row["session_id"] for row in rows) == ["bg-1", "bg-2"]
    assert sorted(process_store_module._PROCESS_RECORD_FILES_CACHE[str(store.root)].entries) == ["bg-1.json", "bg-2.json"]


def test_record_file_cache_honours_trust_window(tmp_path, monkeypatch):
    store = ProcessSessionStore(tmp_path / "records")
    for index in range(3):
        store.write(_running(f"bg-{index}"))
    _settle_files_keep_directory_hot(store.root)
    monkeypatch.setattr(process_store_module, "cache_entry_trustworthy", lambda mtime_ns: False)
    reads = _count_record_reads(monkeypatch)
    for _ in range(2):
        os.utime(store.root, None)
        store.list_records()
    assert len(reads) == 6
    assert process_store_module._PROCESS_RECORD_FILES_CACHE[str(store.root)].entries == {}


def test_mutation_transactions_keep_fresh_caller_owned_reads(tmp_path, monkeypatch):
    store = ProcessSessionStore(tmp_path / "records")
    for index in range(3):
        store.write(_running(f"bg-{index}"))
    _settle_files_keep_directory_hot(store.root)
    store.list_records()  # 预热逐文件缓存
    reads = _count_record_reads(monkeypatch)

    with store.transaction() as transaction:
        assert transaction.reuse_listed_records is False
        records, errors = transaction.list_records()
    assert errors == [] and len(reads) == 3  # 停止/裁剪/插件事务不借用投影缓存
    records[0]["status"] = "caller-mutated"
    os.utime(store.root, None)
    rows, _errors = store.list_records()
    assert "caller-mutated" not in {row["status"] for row in rows}
    rows[1]["status"] = "public-copy-mutated"
    os.utime(store.root, None)
    assert "public-copy-mutated" not in {row["status"] for row in store.list_records()[0]}


# 函数用途: 统计逐文件 stat 指纹核对次数（待通知读取免核复用的已结清记录不计）。
def _count_stat_checks(monkeypatch) -> list[int]:
    checks: list[int] = []
    original = process_store_module.cache_stat_signature

    def counting(status):
        checks.append(status.st_ino)
        return original(status)

    monkeypatch.setattr(process_store_module, "cache_stat_signature", counting)
    return checks


# 函数用途: 建一个带 1 条待通知记录和 count 条已结清历史的 owner 记录根。
def _pending_and_settled(tmp_path: Path, count: int) -> ProcessSessionStore:
    _agent, _params, authority = _fixture(tmp_path)
    payload = authority.load("bg-test-notice").record
    for index in range(count):
        authority.write({**payload, "session_id": f"bg-settled-{index}", "completion_notice_id": "explicit_stop"})
    return authority


def test_pending_report_checks_only_the_active_pending_set(tmp_path, monkeypatch):
    authority = _pending_and_settled(tmp_path, 40)
    _settle_files_keep_directory_hot(authority.root)
    authority.list_pending_completion_records_report()  # 首轮逐个核对并登记
    checks, reads = _count_stat_checks(monkeypatch), _count_record_reads(monkeypatch)

    for _ in range(3):
        os.utime(authority.root, None)
        report = authority.list_pending_completion_records_report()
        assert [row["session_id"] for row in report.records] == ["bg-test-notice"] and report.load_errors == []
    assert len(checks) == 3 and reads == []  # 每轮只核唯一的待通知记录，40 条已结清历史免 stat

    os.utime(authority.root, None)
    assert len(authority.list_records()[0]) == 41
    assert len(checks) == 3 + 41  # 全量读口仍逐个核对

    # 60 秒兜底：核对期满的那一轮逐个核对一次，之后又只核待通知记录。
    key = str(authority.root)
    files = process_store_module._PROCESS_RECORD_FILES_CACHE[key]
    process_store_module._PROCESS_RECORD_FILES_CACHE[key] = dataclass_replace(files, validated_at=files.validated_at - 61)
    for expected in (41, 1):
        checks.clear()
        os.utime(authority.root, None)
        authority.list_pending_completion_records_report()
        assert len(checks) == expected


def test_pending_report_sees_new_replaced_and_deleted_records(tmp_path):
    authority = _pending_and_settled(tmp_path, 3)
    payload = authority.load("bg-test-notice").record
    _settle_files_keep_directory_hot(authority.root)
    authority.list_pending_completion_records_report()

    authority.write({**payload, "session_id": "bg-new"})  # 新出现的待通知记录
    (authority.root / "bg-settled-0.json").unlink()  # 已结清记录被清理
    replaced = authority.root / "bg-settled-1.json"  # 原子替换(换 inode)的文件必须重读
    staging = authority.root / "staging.tmp"
    staging.write_bytes(replaced.read_bytes().replace(b'"explicit_stop"', b'""'))
    os.chmod(staging, 0o600)
    os.replace(staging, replaced)
    os.utime(authority.root, None)

    expected = ["bg-new", "bg-settled-1", "bg-test-notice"]
    report = authority.list_pending_completion_records_report()
    assert sorted(row["session_id"] for row in report.records) == expected and report.load_errors == []
    # 被删文件出登记；2 秒窗口内的新文件只读不登记，落定后的下一轮才登记。
    assert sorted(process_store_module._PROCESS_RECORD_FILES_CACHE[str(authority.root)].entries) == [
        "bg-settled-2.json", "bg-test-notice.json"]
    _settle_files_keep_directory_hot(authority.root)
    report = authority.list_pending_completion_records_report()
    assert sorted(row["session_id"] for row in report.records) == expected and report.load_errors == []
    assert sorted(process_store_module._PROCESS_RECORD_FILES_CACHE[str(authority.root)].entries) == [
        "bg-new.json", "bg-settled-1.json", "bg-settled-2.json", "bg-test-notice.json"]


def test_unverified_pending_scan_never_becomes_the_directory_projection(tmp_path, monkeypatch):
    authority = _pending_and_settled(tmp_path, 5)
    _settle_files_keep_directory_hot(authority.root)
    authority.list_pending_completion_records_report()
    _age_path(authority.root)  # 目录离开 2 秒窗口：可信，但上一轮之后的读取有免核复用

    authority.list_pending_completion_records_report()
    assert process_store_module._PROCESS_RECORDS_PROJECTION_CACHE == {}  # 免核结果不发布给全量读口
    key = str(authority.root)
    files = process_store_module._PROCESS_RECORD_FILES_CACHE[key]
    process_store_module._PROCESS_RECORD_FILES_CACHE[key] = dataclass_replace(files, validated_at=files.validated_at - 61)
    authority.list_pending_completion_records_report()  # 逐个核对的一轮才发布目录投影
    counter = _TransactionCounter()
    counter.install(monkeypatch)
    assert len(authority.list_records()[0]) == 6
    assert counter.calls == 0


@pytest.mark.parametrize("pending_only", [False, True])
def test_concurrent_misses_share_one_scan(tmp_path, monkeypatch, pending_only):
    authority = _pending_and_settled(tmp_path, 3)
    _settle_files_keep_directory_hot(authority.root)
    entered, release, followers_ready = (threading.Event() for _ in range(3))
    original = ProcessSessionStore._scan_records_projection
    scans = []

    def gated(self, **budget):
        scans.append(self.root)
        entered.set()
        assert release.wait(10)  # 封批前等同模式调用加入，避免依赖文件锁和轮询时序。
        return original(self, **budget)

    monkeypatch.setattr(ProcessSessionStore, "_scan_records_projection", gated)
    counter = _TransactionCounter()
    counter.install(monkeypatch)
    query = authority.list_pending_completion_records_report if pending_only else authority.list_records
    arrivals, arrivals_lock = [], threading.Lock()
    with ThreadPoolExecutor(max_workers=7) as pool:
        futures = [pool.submit(query)]
        try:
            assert entered.wait(10)
            batch = process_store_module._PROCESS_RECORDS_SCAN_BATCHES[(str(authority.root), pending_only)]
            original_wait = batch.done.wait

            def joined(timeout=None):
                with arrivals_lock:
                    arrivals.append(True)
                    if len(arrivals) == 6:
                        followers_ready.set()
                return original_wait(timeout)

            monkeypatch.setattr(batch.done, "wait", joined)
            futures.extend(pool.submit(query) for _ in range(6))
            assert followers_ready.wait(10)
            assert batch.followers == 6
        finally:
            release.set()
        results = [future.result(timeout=10) for future in futures]
    assert scans == [authority.root]
    assert counter.calls == (0 if pending_only else 1)
    assert len(results) == 7
    for result in results:
        records, errors = (result.records, result.load_errors) if pending_only else result
        assert not errors
        assert len(records) == (1 if pending_only else 4)
        assert "bg-test-notice" in {row["session_id"] for row in records}


def test_root_caches_follow_resident_owner_capacity(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_defaults, "_RESIDENT_OWNER_CAPACITIES", [])
    assert settings_defaults.resident_owner_capacity() == AgentConfig.owner_agent_pool_max_agents
    OwnerScopedAgentPool(SimpleNamespace(owner_agent_pool_max_agents=3), tmp_path)  # 池构造登记配置的常驻容量
    assert settings_defaults.resident_owner_capacity() == 3

    def read_owner(index: int) -> str:
        store = ProcessSessionStore(tmp_path / f"owner-{index}")
        store.write(_running("bg-a"))
        _age_path(store.root / "bg-a.json")
        _age_path(store.root)
        assert len(store.list_records()[0]) == 1
        return str(store.root)

    roots = [read_owner(index) for index in range(5)]
    assert list(process_store_module._PROCESS_RECORDS_PROJECTION_CACHE) == roots[2:]
    assert list(process_store_module._PROCESS_RECORD_FILES_CACHE) == roots[2:]
    OwnerScopedAgentPool(SimpleNamespace(owner_agent_pool_max_agents=200), tmp_path)  # 运维调大池即放大缓存
    roots += [read_owner(index) for index in range(5, 105)]
    assert list(process_store_module._PROCESS_RECORDS_PROJECTION_CACHE) == roots[2:]  # 超过旧的 64 根仍互不挤掉


def test_owner_agent_slot_count_follows_concurrency_config():
    config = SimpleNamespace(gateway_user_inflight_limit=40, background_threads_per_owner=2,
                             background_admin_threads_per_owner=10, max_subagents=4)
    assert settings_defaults.owner_agent_slot_count(config) == (40 + 10) * (1 + 4)
    default = AgentConfig()
    background = max(default.background_threads_per_owner, default.background_admin_threads_per_owner)
    expected = (default.gateway_user_inflight_limit + background) * (1 + default.max_subagents)
    assert settings_defaults.owner_agent_slot_count(None) == expected
    broken = SimpleNamespace(gateway_user_inflight_limit="many", max_subagents=-1)
    assert settings_defaults.owner_agent_slot_count(broken) == expected


def test_reconcile_safe_points_do_not_reparse_settled_history(tmp_path, monkeypatch):
    agent, _params, authority = _fixture(tmp_path)
    payload = authority.load("bg-test-notice").record
    for index in range(5):
        authority.write({**payload, "session_id": f"bg-settled-{index}", "completion_notice_id": "explicit_stop"})
    assert reconcile_process_completions(agent) == 1  # 发出唯一欠报通知；明确结清的历史沿原事务归档。
    assert {path.stem for path in (authority.root / "finished").glob("bg-*.json")} == {
        f"bg-settled-{index}" for index in range(5)}
    assert (authority.root / "bg-test-notice.json").is_file()  # 通知未消费前仍保留在活跃集。
    _settle_files_keep_directory_hot(authority.root)
    reads = _count_record_reads(monkeypatch)

    assert reconcile_process_completions(agent) == 0
    assert reads == ["bg-test-notice"]  # 热路只解析活跃记录，不重新打开归档历史。
    for _ in range(4):  # 每个模型轮前后、工具轮前的安全点
        os.utime(authority.root, None)
        assert reconcile_process_completions(agent) == 0
    assert reads == ["bg-test-notice"]


# 函数用途: 构造一个 Owner 技能服务；builtin/shared 指向测试目录，策略固定全开。
def _skill_service(tmp_path: Path, cache_entries: int | None = None) -> skills.SkillsService:
    policy = SimpleNamespace(owner_id="local/main", skills_enabled=True,
        enabled_skill_sources=("workspace", "owner", "shared", "builtin"), disabled_skills=(), enabled_shared_skills=())
    home = SimpleNamespace(owner_home_dir=tmp_path / "owner", shared_skills_dir=tmp_path / "shared",
                           shared_builtin_dir=tmp_path / "builtin")
    return skills.SkillsService(home_paths=home, workspace_root=tmp_path / "owner" / "workspace",
                                policy_provider=lambda: policy, cache_entries=cache_entries)


# 函数用途: 写一个最小合法 Skill。
def _write_skill(root: Path, name: str) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {name} method\n---\nbody\n")
    return path


# 函数用途: 统计清单与快照重建次数。
def _count_rebuilds(monkeypatch) -> dict[str, int]:
    counts = {"manifest": 0, "snapshot": 0}
    build_manifest, build_snapshot = skills._build_manifest, skills._build_snapshot

    def manifest(roots):
        counts["manifest"] += 1
        return build_manifest(roots)

    def snapshot(*args, **kwargs):
        counts["snapshot"] += 1
        return build_snapshot(*args, **kwargs)

    monkeypatch.setattr(skills, "_build_manifest", manifest)
    monkeypatch.setattr(skills, "_build_snapshot", snapshot)
    return counts


def test_snapshot_for_does_not_thrash_across_workspaces(tmp_path, monkeypatch):
    _write_skill(tmp_path / "owner" / "skills", "owner-method")
    service = _skill_service(tmp_path)
    workspaces = [tmp_path / name for name in ("wt-1", "wt-2", "wt-3")]
    for workspace in workspaces:
        workspace.mkdir()
    counts = _count_rebuilds(monkeypatch)

    first = {workspace: service.snapshot_for(workspace) for workspace in workspaces}
    # 每个新工作区只发现自己的工作区根；内容相同的工作区共用一次快照构建。
    assert counts == {"manifest": 3, "snapshot": 1}
    for _ in range(3):
        for workspace in workspaces:
            assert service.snapshot_for(workspace) is first[workspace]
        assert service.snapshot_for() is service.snapshot_for()  # 默认工作区与任务工作区交替
    assert counts == {"manifest": 3, "snapshot": 1}
    assert [first[workspace].workspace_root for workspace in workspaces] == [str(w.resolve()) for w in workspaces]
    assert service._manifest_cache[0] == service._skill_roots(service.workspace_root, service.policy_provider())

    # 只有真正变化的工作区重建，且新 Skill 只在该工作区可见。
    _write_skill(workspaces[0] / ".agents" / "skills", "worktree-method")
    assert service.snapshot_for(workspaces[0]).resolve("worktree-method") is not None
    assert service.snapshot_for(workspaces[1]) is first[workspaces[1]]
    assert service.snapshot_for(workspaces[1]).resolve("worktree-method") is None
    assert counts == {"manifest": 4, "snapshot": 2}


def test_workspace_cache_bound_follows_owner_slots_and_releases_roots(tmp_path, monkeypatch):
    slots = settings_defaults.owner_agent_slot_count(SimpleNamespace(
        gateway_user_inflight_limit=1, background_threads_per_owner=1, background_admin_threads_per_owner=1,
        max_subagents=0))
    service = _skill_service(tmp_path, cache_entries=slots)
    workspaces = [(tmp_path / name).resolve() for name in ("wt-1", "wt-2", "wt-3")]
    for workspace in workspaces:
        _write_skill(workspace / ".agents" / "skills", f"{workspace.name}-method")  # 每个工作区内容不同
    counts = _count_rebuilds(monkeypatch)
    for workspace in (*workspaces, workspaces[2], workspaces[1]):
        service.snapshot_for(workspace)
    assert slots == 2 and counts["manifest"] == 3 and len(service._cache) == 2
    evicted = workspaces[0] / ".agents" / "skills"
    assert all(root.path != evicted for root in service._roots)  # 淘汰工作区的根结果与内容快照随之释放
    assert len(service._contents) == 2
    service.snapshot_for(workspaces[0])  # 最久未用的工作区已被淘汰
    assert counts["manifest"] == 4


def test_identical_workspaces_share_one_build_and_frozen_entries(tmp_path, monkeypatch):
    for index in range(5):
        _write_skill(tmp_path / "owner" / "skills", f"method-{index}")
    _write_skill(tmp_path / "builtin", "builtin-method")
    service = _skill_service(tmp_path)
    counts = _count_rebuilds(monkeypatch)
    snapshots = [service.snapshot_for(tmp_path / f"wt-{index}") for index in range(50)]
    assert counts == {"manifest": 50, "snapshot": 1}
    first = snapshots[0]
    assert len(first.entries) == 6
    assert all(len(snapshot.entries) == 6 and all(a is b for a, b in zip(snapshot.entries, first.entries, strict=True))
               for snapshot in snapshots)  # 冻结条目共用，新工作区不重读正文、不重算 guard/资产绑定
    assert len({snapshot.fingerprint for snapshot in snapshots}) == 50
    assert {snapshot.workspace_root for snapshot in snapshots} == {
        str((tmp_path / f"wt-{index}").resolve()) for index in range(50)}


def test_owner_skill_change_rereads_only_that_skill(tmp_path, monkeypatch):
    for index in range(6):
        _write_skill(tmp_path / "owner" / "skills", f"method-{index}")
    _settle_tree(tmp_path / "owner")
    service = _skill_service(tmp_path)
    workspaces = [tmp_path / f"wt-{index}" for index in range(4)]
    for workspace in workspaces:
        service.snapshot_for(workspace)
    read, guard = Mock(wraps=skills.read_skill_text), Mock(wraps=skills.evaluate_skill_guard_gate)
    monkeypatch.setattr(skills, "read_skill_text", read)
    monkeypatch.setattr(skills, "evaluate_skill_guard_gate", guard)

    path = tmp_path / "owner" / "skills" / "method-3" / "SKILL.md"
    path.write_text(path.read_text().replace("method-3 method", "method-3 revised"))
    snapshots = [service.snapshot_for(workspace) for workspace in workspaces]
    assert read.call_count == guard.call_count == 1  # 只重读、重审变化的那一个 Skill 目录
    assert all(snapshot.resolve("method-3").description == "method-3 revised" for snapshot in snapshots)
    assert all(snapshot.entries == snapshots[0].entries for snapshot in snapshots)


def test_unchanged_workspace_is_served_while_another_discovers(tmp_path, monkeypatch):
    service = _skill_service(tmp_path)
    ready, slow = tmp_path / "ready", tmp_path / "slow"
    ready.mkdir()
    service.snapshot_for(ready)
    blocked = _write_skill(slow / ".agents" / "skills", "slow-method").resolve()
    entered, release = threading.Event(), threading.Event()
    original = skills.read_skill_text

    def blocking(target):
        if Path(target) == blocked:
            entered.set()
            release.wait(10)  # 慢盘：发现停在读正文
        return original(target)

    monkeypatch.setattr(skills, "read_skill_text", blocking)
    discovering = threading.Thread(target=service.snapshot_for, args=(slow,))
    discovering.start()
    try:
        assert entered.wait(10)
        served = []
        reader = threading.Thread(target=lambda: served.append(service.snapshot_for(ready)))
        reader.start()
        reader.join(5)
        assert served and not release.is_set()  # 服务锁不随磁盘 I/O 持有：已缓存工作区不排在慢发现之后
        assert skills._PROCESS_SKILL_CACHE_LOCK.acquire(timeout=5)  # 进程级技能锁同样空闲
        skills._PROCESS_SKILL_CACHE_LOCK.release()
    finally:
        release.set()
        discovering.join(10)
    assert service.snapshot_for(slow).resolve("slow-method") is not None


def test_concurrent_cold_workspaces_discover_owner_skills_once(tmp_path, monkeypatch):
    for index in range(3):
        _write_skill(tmp_path / "owner" / "skills", f"method-{index}")
    service = _skill_service(tmp_path)
    read = Mock(wraps=skills.read_skill_text)
    monkeypatch.setattr(skills, "read_skill_text", read)
    workspaces = [tmp_path / f"wt-{index}" for index in range(8)]
    barrier = threading.Barrier(len(workspaces))

    def snapshot(workspace: Path):
        barrier.wait(10)
        return service.snapshot_for(workspace)

    with ThreadPoolExecutor(max_workers=len(workspaces)) as pool:
        snapshots = list(pool.map(snapshot, workspaces))
    assert read.call_count == 3  # 8 个冷工作区并发：Owner 根只发现一次
    assert all(snapshot.resolve("method-2") is not None for snapshot in snapshots)


def test_private_skill_trees_stay_in_owner_service(tmp_path):
    _write_skill(tmp_path / "owner" / "skills", "owner-method")
    _write_skill(tmp_path / "builtin", "builtin-method")
    _settle_tree(tmp_path / "owner")
    _settle_tree(tmp_path / "builtin")
    service = _skill_service(tmp_path)
    assert service.snapshot_for(tmp_path / "wt").resolve("owner-method") is not None
    owner = str(tmp_path / "owner")
    assert not any(key[0].startswith(owner) for key in skills._PROCESS_BUILTIN_ROOT_CACHE)  # 进程级只放公开树
    assert any(key[0] == str(tmp_path / "builtin") for key in skills._PROCESS_BUILTIN_ROOT_CACHE)
    assert str(tmp_path / "owner" / "skills") in service._trees  # 私有树状态随 Owner 服务释放


# 函数用途: 把整棵树(含目录)拨出 2 秒窗口，模拟已稳定的 Owner Skill 目录。
def _settle_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        _age_path(path)
    _age_path(root)


# 函数用途: 统计目录列名次数。
def _count_listings(monkeypatch) -> list[Path]:
    listed: list[Path] = []
    original = Path.iterdir

    def counting(self):
        listed.append(self)
        return original(self)

    monkeypatch.setattr(Path, "iterdir", counting)
    return listed


def test_settled_editable_skill_tree_skips_relisting_but_sees_changes(tmp_path, monkeypatch):
    root = skills.SkillRoot(tmp_path / "owner" / "skills", "owner")
    _write_skill(root.path, "method")
    _settle_tree(root.path)
    before = skills._roots_signature((root,))
    listed = _count_listings(monkeypatch)

    assert skills._roots_signature((root,)) == before
    assert listed == []  # 已稳定目录只复核 stat，不再逐目录列名

    (root.path / "method" / "helper.sh").write_text("echo safe\n")
    changed = skills._roots_signature((root,))
    assert changed != before and listed
    listed.clear()
    assert skills._roots_signature((root,)) == changed
    assert listed  # 刚改过的目录仍在 2 秒窗口内：不缓存，每次重新列名


def test_fresh_editable_skill_tree_is_never_cached(tmp_path, monkeypatch):
    root = skills.SkillRoot(tmp_path / "owner" / "skills", "owner")
    _write_skill(root.path, "method")
    listed = _count_listings(monkeypatch)
    skills._roots_signature((root,))
    skills._roots_signature((root,))
    assert len(set(listed)) == len(listed) / 2 > 0
    assert not any(state.settled for state in skills._PROCESS_BUILTIN_ROOT_CACHE.values())


# 函数用途: 记录 runtime 仓储实际执行的 SQL。
def _trace_sql(monkeypatch) -> list[str]:
    statements: list[str] = []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced)
    return statements


# 函数用途: 对仓储真实执行的 agent_run 事件窗口查询取查询计划。
def _agent_run_event_plans(path: Path, statements: list[str]) -> list[str]:
    queries = [sql for sql in statements if "FROM runtime_events WHERE agent_run_id" in sql]
    assert queries
    with sqlite3.connect(path) as conn:
        plans = []
        for sql in queries:
            parameters = ("agent-run-1", 2000) if "?" in sql else ()
            plans.append(" | ".join(row[-1] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, parameters)))
    return plans


def test_runtime_events_agent_run_index_created_and_used(tmp_path, monkeypatch):
    path = tmp_path / "runtime.db"
    statements = _trace_sql(monkeypatch)
    repository = RuntimeRepository(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (_INDEX,)).fetchone()
    assert repository.events_for_agent_run("agent-run-1") == []
    plans = _agent_run_event_plans(path, statements)
    assert all(_INDEX in plan and "SCAN runtime_events" not in plan for plan in plans), plans


def test_existing_runtime_db_gains_agent_run_index_on_upgrade(tmp_path, monkeypatch):
    path = tmp_path / "runtime.db"
    legacy = tuple(sql for sql in runtime_schema._BASE_RUNTIME_SQL if _INDEX not in sql)
    assert len(legacy) == len(runtime_schema._BASE_RUNTIME_SQL) - 1
    with monkeypatch.context() as patch:
        patch.setattr(runtime_schema, "_BASE_RUNTIME_SQL", legacy)
        RuntimeRepository(path)
    with sqlite3.connect(path) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (_INDEX,)).fetchone()
    RuntimeRepository(path)  # 结构摘要不同 → 原幂等安装补建索引
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (_INDEX,)).fetchone()
