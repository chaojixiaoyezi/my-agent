"""Regression locks for the 2026-11-07 Gateway/TUI per-round stall hotfix.

Root cause A: every safe-point reconcile re-read and re-validated every historical process-session JSON file
under the exclusive store lock, because concurrent writers keep the directory mtime inside the 2 s trust window.
Root cause B: one owner SkillsService kept a single manifest slot, so alternating workspaces (default workspace vs.
task workspace / worktrees) rebuilt the manifest and snapshot on every call; editable skill trees were re-listed
on every signature. Root cause C: ``events_for_agent_run`` had no ``(agent_run_id, seq)`` index.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_py_agent.agent.capability.skill_service as skills
import agent_py_agent.agent.tooling.process_session_store as process_store_module
from agent_py_agent.agent.conversation.process_events import reconcile_process_completions
from agent_py_agent.agent.runtime_db import schema as runtime_schema
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore
from agent_py_agent.tests.test_background_tick_cheap_checks import _age_path, _running
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
    assert sorted(process_store_module._PROCESS_RECORD_FILES_CACHE[str(store.root)]) == ["bg-1.json", "bg-2.json"]


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
    assert process_store_module._PROCESS_RECORD_FILES_CACHE[str(store.root)] == {}


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


def test_reconcile_safe_points_do_not_reparse_settled_history(tmp_path, monkeypatch):
    agent, _params, authority = _fixture(tmp_path)
    payload = authority.load("bg-test-notice").record
    for index in range(5):
        authority.write({**payload, "session_id": f"bg-settled-{index}", "completion_notice_id": "explicit_stop"})
    assert reconcile_process_completions(agent) == 1  # 发出唯一欠报通知，此后全部记录都已结清
    _settle_files_keep_directory_hot(authority.root)
    reads = _count_record_reads(monkeypatch)

    assert reconcile_process_completions(agent) == 0
    assert len(reads) == 6  # 首次可信扫描逐文件解析一次
    for _ in range(4):  # 每个模型轮前后、工具轮前的安全点
        os.utime(authority.root, None)
        assert reconcile_process_completions(agent) == 0
    assert len(reads) == 6


# 函数用途: 构造一个 Owner 技能服务；builtin/shared 指向测试目录，策略固定全开。
def _skill_service(tmp_path: Path) -> skills.SkillsService:
    policy = SimpleNamespace(owner_id="local/main", skills_enabled=True,
        enabled_skill_sources=("workspace", "owner", "shared", "builtin"), disabled_skills=(), enabled_shared_skills=())
    home = SimpleNamespace(owner_home_dir=tmp_path / "owner", shared_skills_dir=tmp_path / "shared",
                           shared_builtin_dir=tmp_path / "builtin")
    return skills.SkillsService(home_paths=home, workspace_root=tmp_path / "owner" / "workspace",
                                policy_provider=lambda: policy)


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
    assert counts == {"manifest": 3, "snapshot": 3}
    for _ in range(3):
        for workspace in workspaces:
            assert service.snapshot_for(workspace) is first[workspace]
        assert service.snapshot_for() is service.snapshot_for()  # 默认工作区与任务工作区交替
    assert counts == {"manifest": 4, "snapshot": 4}
    assert service._manifest_cache[0] == service._skill_roots(service.workspace_root, service.policy_provider())

    # 只有真正变化的工作区重建，且新 Skill 立即可见。
    _write_skill(workspaces[0] / ".agents" / "skills", "worktree-method")
    assert service.snapshot_for(workspaces[0]).resolve("worktree-method") is not None
    assert service.snapshot_for(workspaces[1]) is first[workspaces[1]]
    assert counts == {"manifest": 5, "snapshot": 5}


def test_skill_service_caches_are_bounded_lru(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "_SERVICE_CACHE_MAX_ENTRIES", 2)
    service = _skill_service(tmp_path)
    workspaces = [tmp_path / name for name in ("wt-1", "wt-2", "wt-3")]
    for workspace in workspaces:
        workspace.mkdir()
    counts = _count_rebuilds(monkeypatch)
    for workspace in (*workspaces, workspaces[2], workspaces[1]):
        service.snapshot_for(workspace)
    assert counts["manifest"] == 3 and len(service._manifests) == 2 and len(service._cache) == 2
    service.snapshot_for(workspaces[0])  # 最久未用的工作区已被淘汰
    assert counts["manifest"] == 4


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
