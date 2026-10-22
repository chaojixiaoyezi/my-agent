from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.owner_scoped_pool import ActiveOwnerRegistry
from agent_py_agent.agent.scheduler import (
    SchedulerDueIndex,
    SchedulerJobCreateRequest,
    SchedulerRepository,
)


def _owner(owner_id: str) -> dict[str, object]:
    return {"provider": "feishu", "kind": "user", "id": owner_id}


def _store(*, next_run_at: float = 0.0, run: dict[str, object] | None = None) -> dict[str, object]:
    jobs = {}
    if next_run_at:
        jobs["job-1"] = {"status": "active", "next_run_at": next_run_at}
    runs = {"run-1": run} if run is not None else {}
    return {"jobs": jobs, "runs": runs}


def test_due_index_persists_future_owner_and_claims_only_after_due(tmp_path) -> None:
    path = tmp_path / "global_index" / "scheduler_due.sqlite3"
    first = SchedulerDueIndex(path)
    first.sync_owner(_owner("alice"), _store(next_run_at=200), now=100)

    restarted = SchedulerDueIndex(path)
    assert restarted.claim_due_owners(now=199) == []
    claimed = restarted.claim_due_owners(now=200, lease_seconds=10)

    assert [(row.provider, row.owner_kind, row.owner_id, row.next_due_at) for row in claimed] == [
        ("feishu", "user", "alice", 200.0)
    ]
    assert restarted.claim_due_owners(now=205) == []
    assert [row.owner_id for row in restarted.claim_due_owners(now=211)] == ["alice"]


def test_due_index_schema_init_is_safe_under_concurrent_owner_startup(tmp_path) -> None:
    path = tmp_path / "global_index" / "scheduler_due.sqlite3"
    barrier = threading.Barrier(12)
    failures: list[Exception] = []
    lock = threading.Lock()

    def initialize() -> None:
        barrier.wait()
        try:
            SchedulerDueIndex(path)
        except Exception as exc:
            with lock:
                failures.append(exc)

    threads = [threading.Thread(target=initialize) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert SchedulerDueIndex(path).snapshot() == []


def test_due_index_is_owner_keyed_and_removes_owner_without_active_facts(tmp_path) -> None:
    index = SchedulerDueIndex(tmp_path / "scheduler.sqlite3")
    index.sync_owner(_owner("alice"), _store(next_run_at=200), now=100)
    index.sync_owner(_owner("bob"), _store(next_run_at=150), now=100)

    assert [row.owner_id for row in index.snapshot()] == ["bob", "alice"]

    index.sync_owner(_owner("bob"), _store(), now=120)
    assert [row.owner_id for row in index.snapshot()] == ["alice"]


def test_due_index_tracks_unpublished_and_claimed_runs(tmp_path) -> None:
    index = SchedulerDueIndex(tmp_path / "scheduler.sqlite3")
    index.sync_owner(
        _owner("queued"),
        _store(run={"status": "queued", "dispatch_after": 120}),
        now=100,
    )
    index.sync_owner(
        _owner("running"),
        _store(run={"status": "running", "claim_expires_at": 130}),
        now=100,
    )

    assert [(row.owner_id, row.next_due_at) for row in index.snapshot()] == [
        ("queued", 120.0),
        ("running", 130.0),
    ]


def test_due_index_repairs_pre_index_owner_ledgers_once(tmp_path) -> None:
    owner_home = tmp_path / "owners" / "providers" / "feishu" / "users" / "legacy-user"
    scheduler = owner_home / "data" / "scheduler"
    scheduler.mkdir(parents=True)
    (scheduler / "store.json").write_text(
        json.dumps({**_store(next_run_at=200), "updated_at": 100}),
        encoding="utf-8",
    )
    index = SchedulerDueIndex(tmp_path / "scheduler.sqlite3")

    first = index.repair_legacy_owner_ledgers(tmp_path / "owners")
    second = index.repair_legacy_owner_ledgers(tmp_path / "owners")

    assert first == {"completed": True, "scanned": 1, "indexed": 1, "errors": 0}
    assert second == {"completed": True, "scanned": 0, "indexed": 0, "errors": 0}
    assert [(row.provider, row.owner_kind, row.owner_id) for row in index.snapshot()] == [
        ("feishu", "user", "legacy-user")
    ]


def test_due_index_retries_legacy_scan_after_unreadable_owner_store(tmp_path) -> None:
    owner_home = tmp_path / "owners" / "providers" / "feishu" / "users" / "broken-user"
    scheduler = owner_home / "data" / "scheduler"
    scheduler.mkdir(parents=True)
    store_path = scheduler / "store.json"
    store_path.write_text("[]", encoding="utf-8")
    index = SchedulerDueIndex(tmp_path / "scheduler.sqlite3")

    first = index.repair_legacy_owner_ledgers(tmp_path / "owners")
    store_path.write_text(
        json.dumps({**_store(next_run_at=200), "updated_at": 100}),
        encoding="utf-8",
    )
    second = index.repair_legacy_owner_ledgers(tmp_path / "owners")

    assert first == {"completed": False, "scanned": 1, "indexed": 0, "errors": 1}
    assert second == {"completed": True, "scanned": 1, "indexed": 1, "errors": 0}
    assert [row.owner_id for row in index.snapshot()] == ["broken-user"]


def test_due_index_legacy_scan_does_not_follow_owner_symlinks(tmp_path) -> None:
    outside = tmp_path / "outside-owner" / "data" / "scheduler"
    outside.mkdir(parents=True)
    (outside / "store.json").write_text(
        json.dumps({**_store(next_run_at=200), "updated_at": 100}),
        encoding="utf-8",
    )
    users = tmp_path / "owners" / "providers" / "feishu" / "users"
    users.mkdir(parents=True)
    (users / "linked-user").symlink_to(outside.parent.parent, target_is_directory=True)
    index = SchedulerDueIndex(tmp_path / "scheduler.sqlite3")

    report = index.repair_legacy_owner_ledgers(tmp_path / "owners")

    assert report == {"completed": True, "scanned": 0, "indexed": 0, "errors": 0}
    assert index.snapshot() == []


def test_owner_repository_projects_raw_identity_without_exposing_canonical_path(tmp_path) -> None:
    index = SchedulerDueIndex(tmp_path / "scheduler.sqlite3")
    repository = SchedulerRepository(
        tmp_path / "owners" / "alice" / "data" / "scheduler",
        owner_provider="feishu",
        owner_kind="user",
        owner_id="providers/feishu/users/alice",
        due_owner_id="alice",
        due_index=index,
    )

    repository.create_job(
        SchedulerJobCreateRequest(
            name="backup",
            prompt="check backup",
            thread_id="thread-a",
            schedule={"kind": "at", "at": "1970-01-01T00:03:20Z", "timezone": "UTC"},
            now=100,
        )
    )

    assert [(row.provider, row.owner_kind, row.owner_id) for row in index.snapshot()] == [
        ("feishu", "user", "alice")
    ]


def test_gateway_due_controller_records_only_claimed_scoped_owners(monkeypatch, tmp_path) -> None:
    from agent_py_agent.cli import gateway_loops

    index = SchedulerDueIndex(tmp_path / "scheduler.sqlite3")
    index.sync_owner(_owner("alice"), _store(next_run_at=100), now=50)
    index.sync_owner(
        {"provider": "local", "kind": "main", "id": "local/main"},
        _store(next_run_at=100),
        now=50,
    )
    registry = ActiveOwnerRegistry()
    base = SimpleNamespace(
        config=SimpleNamespace(
            owner_agent_pool_max_agents=64,
            background_owner_workers=8,
        ),
        scheduler_repository=SimpleNamespace(due_index=index),
        home_paths=SimpleNamespace(global_index_dir=tmp_path),
    )
    context = SimpleNamespace(agent=SimpleNamespace())
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _context: base)
    monkeypatch.setattr(gateway_loops, "shared_active_owner_registry", lambda _agent: registry)

    controller = gateway_loops._GatewaySchedulerDueController(context)
    assert controller.tick(now=100) == 1
    assert [(row.provider, row.owner_kind, row.owner_id) for row in registry.snapshot()] == [
        ("feishu", "user", "alice")
    ]


# LLM: spy只记录真实SQLite连接；同文件热构造零连接，新inode/删除/截断必须重新初始化。
# 函数用途: 验证schema缓存命中与数据库文件换代失效，不绕过实际建表和查询。
@pytest.mark.parametrize("replacement", ["deleted", "replaced", "empty"])
def test_scaling_d_schema_cache_skips_connect_and_reinitializes(tmp_path, monkeypatch, replacement):
    import sqlite3

    path = tmp_path / "due.sqlite3"
    SchedulerDueIndex(path)
    original = SchedulerDueIndex._connect
    calls = []

    # LLM: 保留真实连接与SQL执行，仅计数构造是否还访问数据库。
    # 函数用途: 记录schema初始化的连接次数。
    def connect(self):
        calls.append(self.path)
        return original(self)

    monkeypatch.setattr(SchedulerDueIndex, "_connect", connect)
    for _ in range(3):
        SchedulerDueIndex(path)
    assert calls == []
    if replacement == "deleted":
        path.unlink()
    elif replacement == "empty":
        path.write_bytes(b"")
    else:
        other = path.with_suffix(".replacement")
        conn = sqlite3.connect(other)
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
        conn.close()
        other.replace(path)
    restored = SchedulerDueIndex(path)
    assert calls == [path]
    assert restored.snapshot() == []


# LLM: 进程级缓存丢失时，已初始化数据库只需验证版本和表；不能重复CREATE或赋值user_version。
# 函数用途: 模拟另一进程冷启动，用真实SQL trace证明schema读验证不写DDL。
def test_scaling_d_existing_schema_cold_start_has_no_ddl(tmp_path, monkeypatch):
    from collections import OrderedDict

    from agent_py_agent.agent.scheduler import due_index

    path = tmp_path / "due.sqlite3"
    SchedulerDueIndex(path)
    monkeypatch.setattr(due_index, "_SCHEMA_INITIALIZED_DBS", OrderedDict(), raising=False)
    statements = []
    original = SchedulerDueIndex._connect

    # LLM: trace来自真实SQLite连接，不模拟PRAGMA或sqlite_master查询结果。
    # 函数用途: 收集冷构造实际执行的SQL。
    def connect(self):
        conn = original(self)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(SchedulerDueIndex, "_connect", connect)
    SchedulerDueIndex(path)
    writes = [sql for sql in statements if sql.lstrip().upper().startswith("CREATE ")
              or "USER_VERSION=" in sql.replace(" ", "").upper()]
    assert writes == []


# LLM: SQLite连接上下文只提交事务，不自动close；schema短连接必须释放，避免空文件重建仍被旧WAL锁住。
# 函数用途: 捕获实际初始化连接，验证构造返回时句柄已关闭而非等待GC。
def test_scaling_d_schema_initializer_closes_its_connection(tmp_path, monkeypatch):
    import sqlite3

    original = SchedulerDueIndex._connect
    connections = []

    # LLM: 保存真实连接供构造后的只读句柄检查，不替换SQLite事务行为。
    # 函数用途: 观测初始化连接的关闭时机。
    def connect(self):
        conn = original(self)
        connections.append(conn)
        return conn

    monkeypatch.setattr(SchedulerDueIndex, "_connect", connect)
    SchedulerDueIndex(tmp_path / "due.sqlite3")
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        connections[0].execute("SELECT 1")


# LLM: 使用真实SQLite连接和文件替换，在连接关闭后/缓存发布前换入未初始化库，不能缓存新库假成功。
# 函数用途: 复现跨进程替换的时序窗口，要求重新验证当前文件而非给新inode沿用旧schema结论。
def test_scaling_d_schema_replacement_during_cache_publication(tmp_path, monkeypatch):
    import sqlite3
    from collections import OrderedDict

    from agent_py_agent.agent.scheduler import due_index

    path = tmp_path / "due.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    SchedulerDueIndex(path)
    original = sqlite3.connect
    conn = original(replacement)
    conn.execute("CREATE TABLE unrelated (id INTEGER)")
    conn.close()
    swapped = False

    # LLM: 包装真实 sqlite3.Connection，在 close 时注入一次性数据库文件替换，模拟跨进程并发换代。
    # 类用途: 提供可拦截关闭时序的测试 SQLite 连接子类。
    class ReplacingConnection(sqlite3.Connection):
        # LLM: 先关闭已验证的真实连接，再替换路径；模拟另一个进程在发布窗口换文件。
        # 函数用途: 注入一次确定性的数据库文件换代。
        def close(self):
            nonlocal swapped
            super().close()
            if not swapped:
                replacement.replace(path)
                swapped = True

    # LLM: 原SQLite工厂负责全部读写，只让close具有上述一次性时序钩子。
    # 函数用途: 让产品初始化使用可观察关闭窗口的真实连接。
    def connect(*args, **kwargs):
        return original(*args, factory=ReplacingConnection, **kwargs)

    monkeypatch.setattr(due_index, "_SCHEMA_INITIALIZED_DBS", OrderedDict())
    monkeypatch.setattr(sqlite3, "connect", connect)
    restored = SchedulerDueIndex(path)
    conn = original(path)
    tables = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table' "
                          "AND name IN ('scheduler_due_owners', 'scheduler_due_meta')").fetchone()[0]
    conn.close()
    assert swapped and tables == 2
    assert restored.snapshot() == []
