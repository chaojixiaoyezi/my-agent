from __future__ import annotations

import json
import os
import threading
import time
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


# LLM: 通过真实创建入口准备普通任务或独立规则监控，后续只在临时账本注入到期/在途状态。
# 函数用途: 为调度器无锁与到期回归提供合法任务和运行夹具。
def _cpu6_case(tmp_path, *, monitor=False):
    repo = SchedulerRepository(tmp_path / "scheduler", owner_provider="feishu", owner_kind="user", owner_id="alice")
    schedule = {"kind": "every", "every_seconds": 3600, "anchor_at": 200, "timezone": "UTC"}
    rules = [{"id": key, "name": key, "prompt": key, "schedule": schedule, "use_llm": True} for key in ("busy", "free")]
    job, _created = repo.create_job(SchedulerJobCreateRequest(
        name="cpu6", prompt="tick", thread_id="thread-a", schedule=schedule,
        rules=rules if monitor else None, now=100, misfire_grace_seconds=3600,
    ))
    run = repo.reserve_manual_run(job["job_id"], rule_id="busy" if monitor else "", now=100)
    return repo, job, run


# LLM: 测试快路必须绑定真实文件指纹和完整解析投影，不伪造缓存命中。
# 函数用途: 把已写入的临时账本预热到便宜检查可复用状态。
def _cpu6_prime(repo):
    old = time.time() - 3
    os.utime(repo.store_path, (old, old))
    repo.waiting_runs(cheap_checks=True)


# LLM: 将写事务入口设为明确失败，区分不写账本与根本不取 owner/quota 锁。
# 函数用途: 禁止空轮进入调度变更事务。
def _cpu6_forbidden_scope():
    pytest.fail("空轮不应进入 _mutation_scope")


# LLM: 四种在途状态均阻挡同一普通 job；开关两态都要在持锁前排除，热投影也不能误报候选。
# 函数用途: 复现已有运行且旧周期已到期时每拍重复加锁的问题。
@pytest.mark.parametrize("status", ["queued", "claimed", "running", "waiting"])
@pytest.mark.parametrize("cheap", [False, True])
def test_cpu6_active_job_reservation_never_enters_mutation(tmp_path, monkeypatch, status, cheap):
    repo, job, run = _cpu6_case(tmp_path)
    store = json.loads(repo.store_path.read_text())
    store["jobs"][job["job_id"]]["next_run_at"] = 200
    store["runs"][run["run_id"]]["status"] = status
    repo.store_path.write_text(json.dumps(store))
    if cheap:
        _cpu6_prime(repo)
        assert repo._cheap_due_candidates_exist(now=210) is False
    monkeypatch.setattr(repo, "_mutation_scope", _cpu6_forbidden_scope)
    assert repo.reserve_due_runs(now=210, cheap_checks=cheap) == []


# LLM: 冷启动和关闭优化时同样不能为未来任务/空账本取得写锁。
# 函数用途: 冻结预约入口的无候选快照空返合同。
@pytest.mark.parametrize("cheap", [False, True])
def test_cpu6_cold_future_job_never_enters_mutation(tmp_path, monkeypatch, cheap):
    repo, _job, _run = _cpu6_case(tmp_path)
    store = json.loads(repo.store_path.read_text())
    store["runs"] = {}
    repo.store_path.write_text(json.dumps(store))
    monkeypatch.setattr(repo, "_mutation_scope", _cpu6_forbidden_scope)
    assert repo.reserve_due_runs(now=150, cheap_checks=cheap) == []


# LLM: 规则退避结束前应无锁空返，时间跨过边界后同一账本必须仍能真正预约。
# 函数用途: 验证监控有效到期时间、热投影和边界重算一致。
@pytest.mark.parametrize("cheap", [False, True])
def test_cpu6_rule_backoff_is_respected_without_locks_and_expires(tmp_path, monkeypatch, cheap):
    from agent_py_agent.agent.scheduler.due_index import _earliest_due_at
    from agent_py_agent.agent.scheduler.monitor_state import _project

    repo, job, _run = _cpu6_case(tmp_path, monitor=True)
    store = json.loads(repo.store_path.read_text())
    store["runs"] = {}
    raw = store["jobs"][job["job_id"]]
    raw["next_run_at"] = 200
    raw["rules"][0].update(next_run_at=200, backoff_until=800)
    raw["rules"][1]["enabled"] = False
    repo.store_path.write_text(json.dumps(store))
    assert _project(raw)["next_run_at"] == 800
    assert _earliest_due_at(store, now=210) == 800
    if cheap:
        _cpu6_prime(repo)
        assert repo._cheap_due_candidates_exist(now=210) is False
    with monkeypatch.context() as patch:
        patch.setattr(repo, "_mutation_scope", _cpu6_forbidden_scope)
        assert repo.reserve_due_runs(now=210, cheap_checks=cheap) == []
    assert [row["rule_id"] for row in repo.reserve_due_runs(now=800, cheap_checks=cheap)] == ["busy"]


# LLM: 一条规则的 waiting/running 不能挡另一条规则；索引不沿用被阻挡规则的旧顶层最小值。
# 函数用途: 保证独立规则预约与按规则排除活动运行的语义一致。
@pytest.mark.parametrize("status", ["running", "waiting"])
@pytest.mark.parametrize("cheap", [False, True])
def test_cpu6_active_rule_keeps_other_rule_schedulable(tmp_path, status, cheap):
    from agent_py_agent.agent.scheduler.due_index import _earliest_due_at

    repo, job, run = _cpu6_case(tmp_path, monitor=True)
    store = json.loads(repo.store_path.read_text())
    raw = store["jobs"][job["job_id"]]
    raw["next_run_at"] = 100
    raw["rules"][0]["next_run_at"] = 100
    raw["rules"][1]["next_run_at"] = 200
    store["runs"][run["run_id"]].update(status=status, claim_expires_at=500 if status == "running" else 0)
    repo.store_path.write_text(json.dumps(store))
    assert _earliest_due_at(store, now=210) == 200
    if cheap:
        _cpu6_prime(repo)
    assert [row["rule_id"] for row in repo.reserve_due_runs(now=210, cheap_checks=cheap)] == ["free"]


# LLM: 唯一已到期规则被活动运行占用时，另一条未来规则不能使冷读/热投影误判本拍需要写锁。
# 函数用途: 锁定复合监控按规则排除活动运行的空轮快路。
@pytest.mark.parametrize("status", ["running", "waiting"])
@pytest.mark.parametrize("cheap", [False, True])
def test_cpu6_active_rule_with_future_sibling_is_lock_free(tmp_path, monkeypatch, status, cheap):
    from agent_py_agent.agent.scheduler.due_index import _earliest_due_at

    repo, job, run = _cpu6_case(tmp_path, monitor=True)
    store = json.loads(repo.store_path.read_text())
    raw = store["jobs"][job["job_id"]]
    raw["next_run_at"] = 200
    raw["rules"][0]["next_run_at"] = 200
    raw["rules"][1]["next_run_at"] = 800
    store["runs"][run["run_id"]].update(status=status, claim_expires_at=500 if status == "running" else 0)
    repo.store_path.write_text(json.dumps(store))
    assert _earliest_due_at(store, now=210) == (500 if status == "running" else 800)
    if cheap:
        _cpu6_prime(repo)
        assert repo._cheap_due_candidates_exist(now=210) is False
    monkeypatch.setattr(repo, "_mutation_scope", _cpu6_forbidden_scope)
    assert repo.reserve_due_runs(now=210, cheap_checks=cheap) == []


# LLM: 普通 job 有活动运行时只投影运行自身的派发/租约时间，waiting 无进程租约不能回退成 now。
# 函数用途: 防止活动任务的旧周期把 due owner 每秒重新唤醒。
@pytest.mark.parametrize("status,expected", [("queued", 300), ("claimed", 500), ("running", 500), ("waiting", 0)])
def test_cpu6_due_index_uses_active_run_not_stale_job_time(tmp_path, status, expected):
    from agent_py_agent.agent.scheduler.due_index import _earliest_due_at

    repo, job, run = _cpu6_case(tmp_path)
    store = json.loads(repo.store_path.read_text())
    store["jobs"][job["job_id"]]["next_run_at"] = 100
    store["runs"][run["run_id"]].update(status=status, dispatch_after=300,
        claim_expires_at=0 if status == "waiting" else 500)
    assert _earliest_due_at(store, now=210) == expected


# LLM: 冻结来源的重放只读返回且深复制，传入新 context 不能覆盖原快照。
# 函数用途: 复现 queued run 等待会话时每拍准备来源重复取锁。
def test_cpu6_prepared_context_is_returned_without_mutation(tmp_path, monkeypatch):
    repo, _job, run = _cpu6_case(tmp_path)
    frozen = {"nested": {"values": [1]}}
    repo.prepare_run_context(run["run_id"], frozen)
    before = repo.store_path.read_bytes()
    monkeypatch.setattr(repo, "_mutation_scope", _cpu6_forbidden_scope)
    replay = repo.prepare_run_context(run["run_id"], {"different": True})
    assert replay["execution_context"] == frozen
    replay["execution_context"]["nested"]["values"].append(2)
    assert repo.get_active_run(run["run_id"])["execution_context"] == frozen
    assert repo.store_path.read_bytes() == before


# LLM: 无锁来源快路只适用无迁移的幂等读取；显式迁移仍须进入原 CAS 和证明检查。
# 函数用途: 防止缓存绕过来源迁移拒绝逻辑。
def test_cpu6_origin_migration_still_uses_original_transaction(tmp_path, monkeypatch):
    from agent_py_agent.agent.scheduler.repository import SchedulerConflictError

    repo, _job, run = _cpu6_case(tmp_path)
    repo.prepare_run_context(run["run_id"], {"frozen": True})
    original, calls = repo._mutation_scope, []

    # LLM: 不替换原锁或原事务结果，只记录是否进入写方验证。
    # 函数用途: 统计真实变更事务调用次数。
    def scope():
        calls.append(1)
        return original()

    monkeypatch.setattr(repo, "_mutation_scope", scope)
    with pytest.raises(SchedulerConflictError, match="不能替换"):
        repo.prepare_run_context(run["run_id"], {}, origin_migration={"to_thread_id": "foreign"})
    assert calls == [1]


# LLM: 首次冻结来源与换成不同 wake 都是实质变更，必须沿原 owner/quota 事务并实际保存。
# 函数用途: 保证无锁快路不吞掉需要写入的新上下文或新信号。
def test_cpu6_changed_context_and_wake_still_use_original_transactions(tmp_path, monkeypatch):
    repo, _job, run = _cpu6_case(tmp_path)
    original, calls = repo._mutation_scope, []

    # LLM: 只计数，原锁顺序、配额及账本写入不做替换。
    # 函数用途: 观察实质变更是否进入原事务。
    def scope():
        calls.append(1)
        return original()

    monkeypatch.setattr(repo, "_mutation_scope", scope)
    assert repo.prepare_run_context(run["run_id"], {"first": True})["execution_context"] == {"first": True}
    repo.attach_wake_signal(run["run_id"], "first")
    repo.attach_wake_signal(run["run_id"], "second")
    assert calls == [1, 1, 1]
    saved = repo.get_active_run(run["run_id"])
    assert saved["execution_context"] == {"first": True}
    assert saved["wake_signal_id"] == "second"


# LLM: 同一 wake 及空输入都是只读幂等重放，不能清除原 wake，也不能共享嵌套可变对象。
# 函数用途: 复现重复挂接唤醒信号时持锁空转。
@pytest.mark.parametrize("wake_id", ["wake-1", "", None])
def test_cpu6_existing_wake_attachment_is_lock_free(tmp_path, monkeypatch, wake_id):
    repo, _job, run = _cpu6_case(tmp_path)
    repo.attach_wake_signal(run["run_id"], "wake-1")
    before = repo.store_path.read_bytes()
    monkeypatch.setattr(repo, "_mutation_scope", _cpu6_forbidden_scope)
    replay = repo.attach_wake_signal(run["run_id"], wake_id)
    assert replay["wake_signal_id"] == "wake-1"
    replay["wake_signal_id"] = "poison"
    assert repo.get_active_run(run["run_id"])["wake_signal_id"] == "wake-1"
    assert repo.store_path.read_bytes() == before


# LLM: 到期时间向后推进但已有租约仍有效时不得重置；更早的新工作及租约到期必须仍能被领取。
# 函数用途: 冻结 sync_owner 时间变化时租约保留与释放的边界。
@pytest.mark.parametrize("updated_due,updated_at,claim_at,expected", [(110, 101, 110, []), (90, 101, 101, ["alice"]), (110, 131, 131, ["alice"])])
def test_cpu6_due_sync_preserves_live_lease_unless_work_is_earlier(tmp_path, updated_due, updated_at, claim_at, expected):
    index = SchedulerDueIndex(tmp_path / "due.sqlite3")
    index.sync_owner(_owner("alice"), _store(next_run_at=100), now=90)
    assert len(index.claim_due_owners(now=100, lease_seconds=30)) == 1
    index.sync_owner(_owner("alice"), _store(next_run_at=updated_due), now=updated_at)
    assert [row.owner_id for row in index.claim_due_owners(now=claim_at)] == expected
    if not expected:
        assert [row.owner_id for row in index.claim_due_owners(now=130)] == ["alice"]


# LLM: 使用真实索引和真实登记表跨越领取租期，活跃 owner 重复领取不能重复计数/打印；退休后可重新宣告。
# 函数用途: 验证 scheduler due 循环不再持续刷同一 owner 的宣告日志。
def test_cpu6_due_controller_announces_only_new_registry_owners(tmp_path, monkeypatch, capsys):
    from agent_py_agent.agent.user_space import OwnerIdentity
    from agent_py_agent.cli import gateway_loops

    index = SchedulerDueIndex(tmp_path / "due.sqlite3")
    index.sync_owner(_owner("alice"), _store(next_run_at=100), now=50)
    registry = ActiveOwnerRegistry()
    base = SimpleNamespace(config=SimpleNamespace(owner_agent_pool_max_agents=64, background_owner_workers=8),
        scheduler_repository=SimpleNamespace(due_index=index), home_paths=SimpleNamespace(global_index_dir=tmp_path))
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _context: base)
    monkeypatch.setattr(gateway_loops, "shared_active_owner_registry", lambda _agent: registry)
    controller = gateway_loops._GatewaySchedulerDueController(SimpleNamespace(agent=base))
    assert controller.tick(now=100) == 1
    assert "due owners announced: 1" in capsys.readouterr().out
    assert controller.tick(now=131) == 0
    assert capsys.readouterr().out == ""
    owner = OwnerIdentity.provider_user("feishu", "alice")
    assert registry.discard_idle((owner.provider, owner.owner_kind, owner.owner_id), time.monotonic())
    assert controller.tick(now=162) == 1
    assert "due owners announced: 1" in capsys.readouterr().out


# LLM: 损坏或异 owner 的运行不能成为排除合法周期的依据；活动阻挡必须与原解析器口径一致。
# 函数用途: 复现坏 waiting 使 due_index 删掉仍可预约 owner 的漏唤醒。
@pytest.mark.parametrize("monitor", [False, True])
@pytest.mark.parametrize("corruption", ["schema", "owner", "claim_epoch", "delivery"])
def test_cpu6_invalid_waiting_cannot_hide_valid_due_job(tmp_path, monitor, corruption):
    from agent_py_agent.agent.scheduler.due_index import _earliest_due_at

    repo, job, run = _cpu6_case(tmp_path, monitor=monitor)
    store = json.loads(repo.store_path.read_text())
    raw = store["jobs"][job["job_id"]]
    raw["next_run_at"] = 200
    if monitor:
        raw["rules"][0]["next_run_at"] = 200
        raw["rules"][1]["enabled"] = False
    broken = store["runs"][run["run_id"]]
    broken.update(status="waiting", claim_expires_at=0)
    broken[{"schema": "schema_version", "owner": "owner", "claim_epoch": "claim_epoch", "delivery": "delivery"}[corruption]] = {
        "schema": "invalid", "owner": _owner("foreign"), "claim_epoch": -1, "delivery": "invalid",
    }[corruption]
    assert repo._parse_run(broken)[0] is None
    assert _earliest_due_at(store, now=210) == 200
    index = SchedulerDueIndex(tmp_path / "due.sqlite3")
    index.sync_owner(repo.due_owner, store, now=210)
    assert [row.owner_id for row in index.claim_due_owners(now=210)] == ["alice"]
    repo.store_path.write_text(json.dumps(store))
    assert len(repo.reserve_due_runs(now=210)) == 1


# LLM: 迁移监控可保留旧 job 顶层退避；只读候选必须同时遵守原预约事务的顶层门槛。
# 函数用途: 复现规则已到期但父 job 仍退避时误入写锁的空轮。
@pytest.mark.parametrize("cheap", [False, True])
def test_cpu6_parent_backoff_applies_to_monitor_candidates(tmp_path, monkeypatch, cheap):
    from agent_py_agent.agent.scheduler.due_index import _earliest_due_at

    repo, job, _run = _cpu6_case(tmp_path, monitor=True)
    store = json.loads(repo.store_path.read_text())
    store["runs"] = {}
    raw = store["jobs"][job["job_id"]]
    raw.update(next_run_at=200, backoff_until=800)
    for rule in raw["rules"]:
        rule.update(next_run_at=200, backoff_until=0)
    assert repo._parse_job(raw)[0] is not None
    repo.store_path.write_text(json.dumps(store))
    assert _earliest_due_at(store, now=210) == 800
    if cheap:
        _cpu6_prime(repo)
        assert repo._cheap_due_candidates_exist(now=210) is False
    with monkeypatch.context() as patch:
        patch.setattr(repo, "_mutation_scope", _cpu6_forbidden_scope)
        assert repo.reserve_due_runs(now=210, cheap_checks=cheap) == []
    assert {row["rule_id"] for row in repo.reserve_due_runs(now=800, cheap_checks=cheap)} == {"busy", "free"}


# LLM: waiting运行在TaskLink仍active时不每秒唤醒，但TaskLink已终态时必须被发现层判定为硬事实以收口。
# 函数用途: 回归R6-3冷owner终态waiting调度保持可发现并可由reconcile_waiting_runs收口。
def test_terminal_waiting_run_remains_discoverable(tmp_path):
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.owner_wake_discovery import (
        _has_due_scheduler_fact,
        _owner_has_hard_facts,
    )
    from agent_py_agent.agent.scheduler.service import SchedulerService

    home = tmp_path / "owner"
    index = SchedulerDueIndex(tmp_path / "due.sqlite3")
    repo = SchedulerRepository(
        home / "data/scheduler", owner_provider="feishu", owner_kind="user", owner_id="probe", due_index=index
    )
    conversation = ConversationStore(home / "conversations")
    thread = conversation.threads.get_or_create(
        {
            "canonical_user_id": "probe",
            "channel": "internal",
            "channel_conversation_id": "probe",
            "channel_user_id": "probe",
            "now": 100,
        }
    )
    repo.create_job(
        SchedulerJobCreateRequest(
            name="probe",
            prompt="probe",
            thread_id=thread.thread_id,
            schedule={"kind": "every", "every_seconds": 600, "anchor_at": 200, "timezone": "UTC"},
            now=100,
        )
    )
    run = repo.reserve_due_runs(now=200)[0]
    claimed = repo.claim_run(run["run_id"], lease_seconds=30, now=201)
    repo.mark_run_running(run["run_id"], claimed["claim_id"], now=202)
    conversation.tasks.bind({"thread_id": thread.thread_id, "task_id": run["run_id"], "goal": "probe", "now": 202})
    repo.park_run_waiting(run["run_id"], claimed["claim_id"], now=203)
    assert _has_due_scheduler_fact(home) is False
    conversation.tasks.update_status({"task_id": run["run_id"], "status": "failed", "now": 204})
    assert _has_due_scheduler_fact(home) is True
    assert _owner_has_hard_facts(home) is True
    service = SchedulerService(repo, conversation_store=conversation)
    assert service.reconcile_waiting_runs(now=800) == [run["run_id"]]
    assert len(repo.reserve_due_runs(now=800)) == 1


# LLM: 保持真实_connect返回值及事务语义，强引用连接避免GC代劳；SQL授权器只为异常分支注入真实SQLite拒绝。
# 函数用途: 验证六个索引入口正常返回或抛错后立即关闭全部连接，包括迁移检查和迁移写入两段。
@pytest.mark.parametrize("operation", ["allocate_job_alias", "sync_owner", "claim_due_owners",
    "repair_legacy_owner_ledgers", "snapshot", "_migration_complete"])
@pytest.mark.parametrize("fail_sql", [False, True])
def test_opt6_operations_close_connections_immediately(tmp_path, monkeypatch, operation, fail_sql):
    import sqlite3

    from agent_py_agent.agent.scheduler.due_index import SchedulerDueIndexError

    index = SchedulerDueIndex(tmp_path / "due.sqlite3")
    original, connections = index._connect, []
    denied = False

    # LLM: 仅拒绝一次SQL；后续SELECT探针恢复正常，避免把授权器拒绝误当成句柄已关闭。
    # 函数用途: 注入真实SQL异常后放行连接关闭状态探针。
    def deny_once(*_args):
        nonlocal denied
        if denied:
            return sqlite3.SQLITE_OK
        denied = True
        return sqlite3.SQLITE_DENY

    # LLM: 返回原始sqlite3.Connection；迁移失败延至第二个连接，确保测试触及迁移事务而非仅前置查询。
    # 函数用途: 捕获各次真实连接，并按用例在其返回前安装SQL拒绝器。
    def connect():
        conn = original()
        connections.append(conn)
        if fail_sql and (operation != "repair_legacy_owner_ledgers" or len(connections) == 2):
            conn.set_authorizer(deny_once)
        return conn

    monkeypatch.setattr(index, "_connect", connect)
    actions = {
        "allocate_job_alias": index.allocate_job_alias,
        "sync_owner": lambda: index.sync_owner(_owner("alice"), _store(next_run_at=200), now=100),
        "claim_due_owners": lambda: index.claim_due_owners(now=200),
        "repair_legacy_owner_ledgers": lambda: index.repair_legacy_owner_ledgers(tmp_path / "owners"),
        "snapshot": index.snapshot,
        "_migration_complete": lambda: index._migration_complete("probe"),
    }
    try:
        if fail_sql:
            expected = sqlite3.Error if operation == "allocate_job_alias" else SchedulerDueIndexError
            with pytest.raises(expected):
                actions[operation]()
        else:
            actions[operation]()
        assert len(connections) == (2 if operation == "repair_legacy_owner_ledgers" else 1)
        for conn in connections:
            assert isinstance(conn, sqlite3.Connection)
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                conn.execute("SELECT 1")
    finally:
        # 旧实现预期失败也须释放测试自己强引用的句柄；断言发生在此处兜底之前。
        for conn in connections:
            conn.close()
