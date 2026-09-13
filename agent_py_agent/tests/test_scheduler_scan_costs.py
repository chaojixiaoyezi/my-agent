"""扫描成本收口:waiting_runs 投影缓存与 owner 事实判定缓存的失效/一致性验收。

两组被测对象:
  ① ``SchedulerRepository.waiting_runs`` 的 store.json 投影缓存(repository.py):
     命中不得重复解析、外部改写必须立刻可见、内容摘要守卫必须兜住"同 stat 不同内容"、
     缓存必须只是投影(命中/清缓存/外部改写三态结论一致)。
  ② ``owner_wake_discovery._owner_fact_kind`` 的事实判定缓存(owner_wake_discovery.py):
     键是 owner home 下相关文件的结构化签名,新增/删除/改写 wake、policy、子代理 run、
     任务账本、调度账本都必须立刻重新判定;时间边界之外的复用必须落在 TTL 内;
     签名异常一律回退现读。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import agent_py_agent.agent.owner_wake_discovery as discovery
import agent_py_agent.agent.scheduler.repository as repository_module
from agent_py_agent.agent.owner_wake_discovery import (
    _FACT_MAX_ENTRIES,
    _FACT_TTL_SECONDS,
    _owner_fact_signature,
    _owner_fact_signature_digest,
    discover_wake_pending_owner_page,
    discover_wake_pending_owners,
)
from agent_py_agent.agent.scheduler.repository import (
    SchedulerRepository,
    SchedulerStateError,
)

_OWNER = {"provider": "local", "kind": "main", "id": "local/main"}


# --------------------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------------------
def _repository(root: Path) -> SchedulerRepository:
    return SchedulerRepository(
        root,
        owner_provider="local",
        owner_kind="main",
        owner_id="local/main",
    )


def _run_row(run_id: str, status: str, *, waiting_since: float = 0.0) -> dict[str, object]:
    return {
        "schema_version": "scheduler_run.v1",
        "owner": dict(_OWNER),
        "run_id": run_id,
        "job_id": "job_1",
        "thread_id": "thread-1",
        "status": status,
        "attempt": 1,
        "started_at": 1_000.0,
        "waiting_since": waiting_since,
        "updated_at": 1_000.0,
    }


def _write_store(path: Path, runs: dict[str, dict[str, object]], *, jobs: dict | None = None) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "scheduler_store.v1",
                "owner": dict(_OWNER),
                "jobs": jobs or {},
                "runs": runs,
                "updated_at": 1_000.0,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _store_with_waiting(count: int, *, total: int = 8) -> dict[str, dict[str, object]]:
    runs = {f"srun_{index:03d}": _run_row(f"srun_{index:03d}", "queued") for index in range(total)}
    for index in range(count):
        run_id = f"srun_{index:03d}"
        runs[run_id] = _run_row(run_id, "waiting", waiting_since=1_000.0 + index)
    return runs


class _ParseCounter:
    """统计 _parse_run 调用次数:命中投影缓存时必须是 0。"""

    def __init__(self) -> None:
        self.calls = 0

    def install(self, monkeypatch) -> None:
        counter = self
        original = SchedulerRepository._parse_run

        def counting(self, raw, *, terminal=False):  # noqa: ANN001
            counter.calls += 1
            return original(self, raw, terminal=terminal)

        monkeypatch.setattr(SchedulerRepository, "_parse_run", counting)


@pytest.fixture
def waiting_projection():
    """投影缓存是模块级:用例前后清空,避免跨用例统计串味。"""
    repository_module._WAITING_PROJECTION_CACHE.clear()
    for name in list(repository_module._WAITING_PROJECTION_STATS):
        repository_module._WAITING_PROJECTION_STATS[name] = 0
    yield repository_module
    repository_module._WAITING_PROJECTION_CACHE.clear()


@pytest.fixture
def fact_cache(monkeypatch):
    """事实判定缓存是模块级:用例前后清空,并强制"值得缓存"分支。

    缓存行为用例必须真的走缓存路径才有意义(否则判定便宜时会自适应跳过缓存,用例变成
    空转);自适应阈值本身由 test_fact_cache_is_adaptive_for_cheap_evaluations 单独覆盖。
    """
    discovery._OWNER_FACT_CACHE.clear()
    for name in list(discovery._OWNER_FACT_STATS):
        discovery._OWNER_FACT_STATS[name] = 0
    monkeypatch.setattr(discovery, "_FACT_MIN_CACHED_SECONDS", 0.0)
    yield discovery
    discovery._OWNER_FACT_CACHE.clear()


def _wanting_ids(rows: list[dict[str, object]]) -> list[str]:
    return [str(row["run_id"]) for row in rows]


# --------------------------------------------------------------------------------------
# ① waiting_runs 投影
# --------------------------------------------------------------------------------------
def test_waiting_runs_reuses_projection_without_reparsing(tmp_path, waiting_projection, monkeypatch) -> None:
    """store.json 未变:第二次调用解析 0 条,结果逐字相同,并记一次命中。"""
    root = tmp_path / "owner" / "scheduler"
    root.mkdir(parents=True)
    _write_store(root / "store.json", _store_with_waiting(3))
    repository = _repository(root)
    counter = _ParseCounter()
    counter.install(monkeypatch)

    first_rows, first_errors = repository.waiting_runs()
    first_parses = counter.calls
    second_rows, second_errors = repository.waiting_runs()

    assert first_parses == 8
    assert counter.calls == first_parses  # 命中不再解析任何 run
    assert _wanting_ids(second_rows) == _wanting_ids(first_rows) == [
        "srun_000",
        "srun_001",
        "srun_002",
    ]
    assert second_errors == first_errors == []
    assert waiting_projection._WAITING_PROJECTION_STATS["hit"] >= 1


def test_waiting_runs_sees_external_rewrite_immediately(tmp_path, waiting_projection) -> None:
    """外部改写 store.json(非本进程写):下一次调用必须看到新状态,不得读陈旧投影。"""
    root = tmp_path / "owner" / "scheduler"
    root.mkdir(parents=True)
    store_path = root / "store.json"
    _write_store(store_path, _store_with_waiting(2))
    repository = _repository(root)
    assert _wanting_ids(repository.waiting_runs()[0]) == ["srun_000", "srun_001"]

    runs = _store_with_waiting(2)
    runs["srun_000"] = _run_row("srun_000", "running")
    _write_store(store_path, runs)

    assert _wanting_ids(repository.waiting_runs()[0]) == ["srun_001"]


def test_waiting_runs_guard_catches_same_stat_different_content(
    tmp_path, waiting_projection, monkeypatch
) -> None:
    """粗粒度时间戳文件系统上的同尺寸原地改写:内容摘要守卫必须失效并重新解析。

    这里显式把 mtime_ns/size/inode 全部还原成缓存里的那一份,模拟"stat 键完全相同、内容已变"。
    """
    root = tmp_path / "owner" / "scheduler"
    root.mkdir(parents=True)
    store_path = root / "store.json"
    _write_store(store_path, _store_with_waiting(2))
    repository = _repository(root)
    assert _wanting_ids(repository.waiting_runs()[0]) == ["srun_000", "srun_001"]

    stat_before = store_path.stat()
    payload = json.loads(store_path.read_text(encoding="utf-8"))
    payload["runs"]["srun_000"]["status"] = "running"
    rewritten = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    assert len(rewritten.encode("utf-8")) == stat_before.st_size  # 同尺寸(等长状态词)
    store_path.write_text(rewritten, encoding="utf-8")
    os.utime(store_path, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))

    rows, _errors = repository.waiting_runs()

    assert _wanting_ids(rows) == ["srun_001"]
    assert waiting_projection._WAITING_PROJECTION_STATS["guard"] >= 1


def test_waiting_runs_three_state_consistency(tmp_path, waiting_projection) -> None:
    """三态一致:缓存命中 / 清缓存 / 外部改写 的结论必须与全新实例(无缓存)逐个相等。"""
    root = tmp_path / "owner" / "scheduler"
    root.mkdir(parents=True)
    store_path = root / "store.json"
    _write_store(store_path, _store_with_waiting(3))
    repository = _repository(root)

    hit_rows, hit_errors = repository.waiting_runs()  # 冷启动 → 写入投影
    warm_rows, warm_errors = repository.waiting_runs()  # 命中
    waiting_projection._WAITING_PROJECTION_CACHE.clear()
    cold_rows, cold_errors = repository.waiting_runs()  # 清缓存后重解析
    fresh_rows, fresh_errors = _repository(root).waiting_runs()  # 全新实例(不共享内存投影)

    assert hit_rows == warm_rows == cold_rows == fresh_rows
    assert hit_errors == warm_errors == cold_errors == fresh_errors == []

    runs = _store_with_waiting(3)
    runs["srun_001"] = _run_row("srun_001", "done")
    _write_store(store_path, runs)
    rewritten_rows, _errors = repository.waiting_runs()
    expected_rows, _errors = _repository(root).waiting_runs()

    assert rewritten_rows == expected_rows
    assert _wanting_ids(rewritten_rows) == ["srun_000", "srun_002"]


def test_waiting_runs_in_process_mutation_is_visible(tmp_path, waiting_projection) -> None:
    """本进程内 park/finish 走同一把权威写:下一轮对账必须立刻看到新状态。"""
    root = tmp_path / "owner" / "scheduler"
    root.mkdir(parents=True)
    _write_store(
        root / "store.json",
        {"srun_001": _run_row("srun_001", "claimed")},
        jobs={"job_1": {"status": "active", "next_run_at": 1_100.0}},
    )
    repository = _repository(root)
    assert repository.waiting_runs()[0] == []

    claim = repository.claim_run("srun_001", lease_seconds=60, now=1_000.0)
    assert claim is not None
    parked = repository.park_run_waiting(
        "srun_001",
        str(claim["claim_id"]),
        response="waiting for child",
        now=1_001.0,
    )
    assert parked["status"] == "waiting"
    assert _wanting_ids(repository.waiting_runs()[0]) == ["srun_001"]

    from agent_py_agent.agent.scheduler.repository import SchedulerRunFinish

    repository.finish_waiting_run(
        "srun_001",
        SchedulerRunFinish(status="done", response="child finished", now=1_002.0),
    )
    assert repository.waiting_runs()[0] == []


def test_waiting_runs_corrupt_store_never_serves_stale_projection(tmp_path, waiting_projection) -> None:
    """坏 store:结构化报错而不是回退到旧投影;修好后立刻恢复正确结论。"""
    root = tmp_path / "owner" / "scheduler"
    root.mkdir(parents=True)
    store_path = root / "store.json"
    _write_store(store_path, _store_with_waiting(1))
    repository = _repository(root)
    assert _wanting_ids(repository.waiting_runs()[0]) == ["srun_000"]

    store_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SchedulerStateError):
        repository.waiting_runs()

    _write_store(store_path, _store_with_waiting(2))
    assert _wanting_ids(repository.waiting_runs()[0]) == ["srun_000", "srun_001"]


def test_waiting_runs_missing_store_is_empty_and_recovers(tmp_path, waiting_projection) -> None:
    """store 不存在 = 空账本;文件随后出现必须立刻可见(缓存不绑定"缺失"状态)。"""
    root = tmp_path / "owner" / "scheduler"
    root.mkdir(parents=True)
    repository = _repository(root)
    assert repository.waiting_runs() == ([], [])

    _write_store(root / "store.json", _store_with_waiting(1))
    assert _wanting_ids(repository.waiting_runs()[0]) == ["srun_000"]


def test_waiting_runs_projection_is_owner_scoped(tmp_path, waiting_projection) -> None:
    """同一路径、不同 owner 身份不得共享投影(逐条解析结果取决于 self.owner)。"""
    root = tmp_path / "owner" / "scheduler"
    root.mkdir(parents=True)
    _write_store(root / "store.json", _store_with_waiting(1))
    matching = _repository(root)
    foreign = SchedulerRepository(
        root,
        owner_provider="feishu",
        owner_kind="user",
        owner_id="u-9",
    )

    assert _wanting_ids(matching.waiting_runs()[0]) == ["srun_000"]
    with pytest.raises(SchedulerStateError):
        foreign.waiting_runs()  # owner 身份不符 → 结构化拒绝,不能被别人的投影喂饱


def test_waiting_runs_returns_isolated_deep_copies(tmp_path, waiting_projection) -> None:
    """返回行是投影的 deepcopy:调用方改写不得污染缓存内容。"""
    root = tmp_path / "owner" / "scheduler"
    root.mkdir(parents=True)
    _write_store(root / "store.json", _store_with_waiting(1))
    repository = _repository(root)

    rows, _errors = repository.waiting_runs()
    rows[0]["status"] = "tampered"
    rows[0]["run_id"] = "srun_tampered"

    assert _wanting_ids(repository.waiting_runs()[0]) == ["srun_000"]


def test_waiting_runs_keeps_error_rows_and_order(tmp_path, waiting_projection) -> None:
    """坏 run 仍进 errors、waiting 排序不变(投影只缓存结果,不改语义)。"""
    root = tmp_path / "owner" / "scheduler"
    root.mkdir(parents=True)
    runs = {
        "srun_003": _run_row("srun_003", "waiting", waiting_since=1_030.0),
        "srun_001": _run_row("srun_001", "waiting", waiting_since=1_010.0),
        "srun_bad": {"schema_version": "scheduler_run.v1", "owner": dict(_OWNER)},
        "srun_002": _run_row("srun_002", "waiting", waiting_since=1_010.0),
    }
    _write_store(root / "store.json", runs)
    repository = _repository(root)

    rows, errors = repository.waiting_runs()
    again, again_errors = repository.waiting_runs()

    assert _wanting_ids(rows) == ["srun_001", "srun_002", "srun_003"]
    assert errors == ["SCHEDULER_RUN_INVALID"]
    assert (again, again_errors) == (rows, errors)


def test_waiting_runs_ignores_stale_probe_and_reparses(tmp_path, waiting_projection, monkeypatch) -> None:
    """锁外探针与锁内 stat 不一致(读盘后账本又被改写): 绝不当作命中,回退权威读取路径。"""
    root = tmp_path / "owner" / "scheduler"
    root.mkdir(parents=True)
    store_path = root / "store.json"
    _write_store(store_path, _store_with_waiting(2))
    repository = _repository(root)
    assert _wanting_ids(repository.waiting_runs()[0]) == ["srun_000", "srun_001"]

    stale = repository_module._StoreBytesProbe(stat_key=(-1, -1, -1, -1), digest="stale", data=b"{}")
    monkeypatch.setattr(repository_module, "_probe_store_bytes", lambda path: stale)

    rows, errors = repository.waiting_runs()

    assert errors == []
    assert _wanting_ids(rows) == ["srun_000", "srun_001"]  # 权威路径给的是真实账本,不是 {} 探针


def test_waiting_runs_skips_caching_when_store_changes_during_parse(
    tmp_path, waiting_projection, monkeypatch
) -> None:
    """解析期间账本被再次改写: 本次结论照常返回,但不写缓存(下次重新解析)。"""
    root = tmp_path / "owner" / "scheduler"
    root.mkdir(parents=True)
    store_path = root / "store.json"
    _write_store(store_path, _store_with_waiting(1))
    repository = _repository(root)
    original = SchedulerRepository._load_store_from_bytes_unlocked

    def racing(self, data):  # noqa: ANN001
        store = original(self, data)
        runs = _store_with_waiting(1)
        runs["srun_000"] = _run_row("srun_000", "running")
        _write_store(store_path, runs)  # 模拟并发写: 解析完成后文件已变
        return store

    monkeypatch.setattr(SchedulerRepository, "_load_store_from_bytes_unlocked", racing)

    rows, _errors = repository.waiting_runs()

    assert _wanting_ids(rows) == ["srun_000"]  # 本次返回解析到的那一份
    assert repository.waiting_projection_cache_key() not in waiting_projection._WAITING_PROJECTION_CACHE
    assert waiting_projection._WAITING_PROJECTION_STATS["skip"] >= 1
    monkeypatch.undo()
    assert _wanting_ids(repository.waiting_runs()[0]) == []  # 下一次按新内容解析


def test_waiting_projection_cache_is_bounded(tmp_path, waiting_projection) -> None:
    """条目上界:多个账本不会让投影缓存无限增长。"""
    limit = waiting_projection._WAITING_PROJECTION_MAX_ENTRIES
    for index in range(limit + 2):
        root = tmp_path / f"owner-{index}" / "scheduler"
        root.mkdir(parents=True)
        _write_store(root / "store.json", _store_with_waiting(1))
        _repository(root).waiting_runs()

    assert len(waiting_projection._WAITING_PROJECTION_CACHE) <= limit


def test_byte_parse_matches_authoritative_reader(tmp_path) -> None:
    """缓存路径的字节解析与 read_json_object_report 的错误语义一致(不漂移)。"""
    from agent_py_agent.agent.common.json_io import read_json_object_report

    cases = {
        "object": b'{"a": 1}',
        "scalar": b"3",
        "list": b"[]",
        "broken": b"{oops",
        "non-utf8": b"\xff\xfe",
        "empty": b"",
    }
    for name, data in cases.items():
        path = tmp_path / f"{name}.json"
        path.write_bytes(data)
        payload, error = repository_module._parse_json_object_bytes(path, data)
        report = read_json_object_report(path, context="scheduler.store.read")
        assert payload == report.payload, name
        assert (error is not None) == (report.load_error is not None), name


# --------------------------------------------------------------------------------------
# ② owner 事实判定缓存
# --------------------------------------------------------------------------------------
def _owner_home(owners: Path, owner_id: str) -> Path:
    home = owners / "providers" / "feishu" / "users" / owner_id
    (home / "workspace" / "runtime" / "workspaces" / "slug-x" / "conversations").mkdir(
        parents=True, exist_ok=True
    )
    return home


def _store_root(home: Path) -> Path:
    return home / "workspace" / "runtime" / "workspaces" / "slug-x" / "conversations"


def _write_policy(store_root: Path, name: str, *, enabled: bool, next_due_at: float | None = None) -> None:
    payload: dict[str, object] = {"enabled": enabled}
    if next_due_at is not None:
        payload["next_due_at"] = next_due_at
    policies = store_root / "progress_policies"
    policies.mkdir(parents=True, exist_ok=True)
    (policies / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_wake(store_root: Path, name: str, kind: str = "normal") -> None:
    queue = store_root / "wake_queue" / kind
    queue.mkdir(parents=True, exist_ok=True)
    (queue / f"{name}.json").write_text(json.dumps({"status": "pending"}), encoding="utf-8")


def _write_subagent_run(home: Path, run_id: str, status: str) -> None:
    path = home / "agents" / run_id / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": run_id, "status": status}), encoding="utf-8")


def _write_task_ledger(home: Path, task_id: str) -> None:
    link = _store_root(home) / "tasks" / f"{task_id}.json"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.write_text(json.dumps({"task_id": task_id, "status": "active"}), encoding="utf-8")
    ledger = home / "tasks" / "2024-01-01" / task_id / "work" / "state.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"task_id": task_id, "status": "RUNNING"}), encoding="utf-8")


class _EvaluateCounter:
    """统计现读判定的调用次数(_owner_has_hard_facts / _owner_has_soft_facts)。"""

    def __init__(self) -> None:
        self.hard = 0
        self.soft = 0

    def install(self, monkeypatch) -> None:
        counter = self
        original_hard = discovery._owner_has_hard_facts
        original_soft = discovery._owner_has_soft_facts

        def hard(owner_home, *, deadline_out=None):  # noqa: ANN001
            counter.hard += 1
            return original_hard(owner_home, deadline_out=deadline_out)

        def soft(owner_home, *, deadline_out=None):  # noqa: ANN001
            counter.soft += 1
            return original_soft(owner_home, deadline_out=deadline_out)

        monkeypatch.setattr(discovery, "_owner_has_hard_facts", hard)
        monkeypatch.setattr(discovery, "_owner_has_soft_facts", soft)


def _page(owners: Path, limit: int = 8):
    return discover_wake_pending_owner_page(owners, limit=limit)


def test_fact_cache_hit_skips_live_predicates(fact_cache, monkeypatch, tmp_path) -> None:
    """owner 文件没变:第二次分页判定不再现读谓词,结论逐字相同,并记一次命中。"""
    owners = tmp_path / "owners"
    _write_policy(_store_root(_owner_home(owners, "u1")), "p", enabled=True, next_due_at=time.time() - 1)
    counter = _EvaluateCounter()
    counter.install(monkeypatch)

    first = _page(owners)
    hard_after_first = counter.hard
    second = _page(owners)

    assert hard_after_first == 1
    assert counter.hard == 1  # 命中:没有第二次现读
    assert [o.owner_id for o in first.owners] == [o.owner_id for o in second.owners] == ["u1"]
    assert first.hard_owners == second.hard_owners
    assert fact_cache._OWNER_FACT_STATS["hit"] >= 1


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda home: _write_wake(_store_root(home), "sig-1"), id="wake-added"),
        pytest.param(
            lambda home: _write_policy(_store_root(home), "p", enabled=True, next_due_at=time.time() - 1),
            id="policy-added",
        ),
        pytest.param(lambda home: _write_subagent_run(home, "run-1", "PENDING"), id="subagent-run-added"),
        pytest.param(lambda home: _write_task_ledger(home, "task-1"), id="task-ledger-added"),
    ],
)
def test_fact_cache_invalidated_when_hard_fact_appears(fact_cache, tmp_path, mutate) -> None:
    """新增硬事实(wake/policy/子代理 run/任务账本)后必须重新判定,不能吃旧结论。"""
    owners = tmp_path / "owners"
    home = _owner_home(owners, "u1")
    assert discover_wake_pending_owners(owners) == []  # 先缓存 none

    mutate(home)

    found = discover_wake_pending_owners(owners)
    assert [owner.owner_id for owner in found] == ["u1"]


def test_fact_cache_invalidated_when_hard_fact_disappears(fact_cache, tmp_path) -> None:
    """删除硬事实后必须重新判定(缓存不得把已消失的事实继续当权威)。"""
    owners = tmp_path / "owners"
    home = _owner_home(owners, "u1")
    _write_wake(_store_root(home), "sig-1")
    _write_subagent_run(home, "run-1", "PENDING")
    assert [owner.owner_id for owner in discover_wake_pending_owners(owners)] == ["u1"]

    for path in sorted((_store_root(home) / "wake_queue" / "normal").glob("*.json")):
        path.unlink()
    assert [owner.owner_id for owner in discover_wake_pending_owners(owners)] == ["u1"]  # 仍剩 run

    (home / "agents" / "run-1" / "state.json").unlink()
    assert discover_wake_pending_owners(owners) == []


def test_fact_cache_invalidated_when_policy_rewritten_in_place(fact_cache, tmp_path) -> None:
    """原地改写 policy(enabled true→false)必须立刻失效——只看目录 mtime 会漏掉这种变化。"""
    owners = tmp_path / "owners"
    home = _owner_home(owners, "u1")
    _write_policy(_store_root(home), "p", enabled=True, next_due_at=time.time() - 1)
    assert [owner.owner_id for owner in discover_wake_pending_owners(owners)] == ["u1"]

    _write_policy(_store_root(home), "p", enabled=False, next_due_at=time.time() - 1)

    assert discover_wake_pending_owners(owners) == []


def test_fact_cache_invalidated_when_subagent_run_settles_in_place(fact_cache, tmp_path) -> None:
    """子代理 run 原地从 PENDING 变 DONE:签名必须变(否则 owner 会被永久当硬活驱动)。"""
    owners = tmp_path / "owners"
    home = _owner_home(owners, "u1")
    _write_subagent_run(home, "run-1", "PENDING")
    assert [owner.owner_id for owner in discover_wake_pending_owners(owners)] == ["u1"]

    _write_subagent_run(home, "run-1", "DONE")

    assert discover_wake_pending_owners(owners) == []


def test_fact_cache_invalidated_when_scheduler_store_changes(fact_cache, tmp_path) -> None:
    """调度账本变化(到点 job)必须重新判定。"""
    owners = tmp_path / "owners"
    home = _owner_home(owners, "u1")
    now = time.time()
    for next_run_at, expected in ((now + 3_600, []), (now - 1, ["u1"])):
        path = home / "data" / "scheduler" / "store.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": "scheduler_store.v1",
                    "jobs": {"job_1": {"status": "active", "next_run_at": next_run_at}},
                    "runs": {},
                }
            ),
            encoding="utf-8",
        )
        assert [owner.owner_id for owner in discover_wake_pending_owners(owners)] == expected


def test_fact_cache_respects_policy_due_deadline(fact_cache, tmp_path) -> None:
    """未到期 policy:缓存有效期不得超过它到点的时刻,到点后必须重新现读。"""
    owners = tmp_path / "owners"
    home = _owner_home(owners, "u1")
    now = time.time()
    _write_policy(_store_root(home), "p", enabled=True, next_due_at=now + 30.0)

    assert discover_wake_pending_owners(owners) == []

    entry = fact_cache._OWNER_FACT_CACHE[str(home)]
    assert entry.kind == "none"
    # 有效期被"policy 到点"收紧(而不是吃满兜底 TTL)
    assert entry.valid_until - time.monotonic() <= 31.0
    assert entry.valid_until - time.monotonic() <= _FACT_TTL_SECONDS

    # 时间推进到 policy 到点之后:即使文件没变,也必须重新判定并判活。
    _write_policy(_store_root(home), "p", enabled=True, next_due_at=time.time() - 1)
    assert [owner.owner_id for owner in discover_wake_pending_owners(owners)] == ["u1"]


def test_fact_cache_ttl_bound_expires_without_file_change(fact_cache, monkeypatch, tmp_path) -> None:
    """兜底 TTL:文件一个字节没变,超过 TTL 也必须重新现读(不得无限复用)。

    把兜底 TTL 设为 0 等价于"写完就到期":第二次调用必须重新现读而不是吃旧结论。
    """
    owners = tmp_path / "owners"
    _owner_home(owners, "u1")
    counter = _EvaluateCounter()
    counter.install(monkeypatch)
    monkeypatch.setattr(fact_cache, "_FACT_TTL_SECONDS", 0.0)

    assert discover_wake_pending_owners(owners) == []
    assert counter.hard == 1
    assert discover_wake_pending_owners(owners) == []
    assert counter.hard == 2  # 到期即重新现读
    assert fact_cache._OWNER_FACT_STATS["miss"] >= 2


def test_fact_cache_falls_back_to_live_read_when_signature_fails(
    fact_cache, monkeypatch, tmp_path
) -> None:
    """签名不可读:一律回退现读,结论不变且不写缓存(异常路径不得改变结论)。"""
    owners = tmp_path / "owners"
    home = _owner_home(owners, "u1")
    _write_wake(_store_root(home), "sig-1")
    monkeypatch.setattr(fact_cache, "_FACT_MIN_CACHED_SECONDS", 0.0)  # 强制走"值得缓存"的分支
    monkeypatch.setattr(
        fact_cache,
        "_owner_fact_signature_digest",
        lambda owner_home: (_ for _ in ()).throw(OSError("stat failed")),
    )

    assert [owner.owner_id for owner in discover_wake_pending_owners(owners)] == ["u1"]
    assert fact_cache._OWNER_FACT_CACHE == {}
    assert fact_cache._OWNER_FACT_STATS["fallback"] >= 1


def test_fact_cache_is_adaptive_for_cheap_evaluations(
    fact_cache, monkeypatch, tmp_path
) -> None:
    """自适应阈值:判定便宜时不为它维护签名(否则签名开销比判定本身还大)。

    阈值与判定耗时都是时间量,随机器负载等比变化;这里用"把阈值推到两端"验证机制本身,
    不依赖被测机器的绝对速度。
    """
    owners = tmp_path / "owners"
    _owner_home(owners, "u1")
    counter = _EvaluateCounter()
    counter.install(monkeypatch)

    monkeypatch.setattr(fact_cache, "_FACT_MIN_CACHED_SECONDS", 1e9)  # 任何判定都"便宜"
    assert discover_wake_pending_owners(owners) == []
    assert discover_wake_pending_owners(owners) == []
    assert counter.hard == 2  # 每次都现读,不吃缓存
    assert fact_cache._OWNER_FACT_CACHE == {}
    assert fact_cache._OWNER_FACT_STATS["cheap"] >= 2

    monkeypatch.setattr(fact_cache, "_FACT_MIN_CACHED_SECONDS", 0.0)  # 任何判定都"值得缓存"
    assert discover_wake_pending_owners(owners) == []
    assert discover_wake_pending_owners(owners) == []
    assert counter.hard == 3  # 第二次命中,不再现读
    assert str(_owner_home(owners, "u1")) in fact_cache._OWNER_FACT_CACHE


def test_fact_cache_three_state_consistency(fact_cache, tmp_path) -> None:
    """三态一致:命中 / 清缓存 / 外部改写 的页面结论与"清缓存现读"逐个相等。

    这条用例同时证明缓存不是第二套权威:清掉缓存后的结论只由磁盘事实决定,不因缓存状态而变。
    """
    owners = tmp_path / "owners"
    quiet = _owner_home(owners, "u-quiet")
    hard = _owner_home(owners, "u-hard")
    _write_policy(_store_root(hard), "p", enabled=True, next_due_at=time.time() - 1)
    soft = _owner_home(owners, "u-soft")
    state_path = soft / "memory" / "curator" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "my-agent.memory-curator-state.v1",
                "last_processed_message_id": "",
                "last_processed_audit_event_id": "",
                "per_thread_cursors": {},
                "last_run_at": "",
                "last_success_at": "",
                "last_failure_at": "",
                "last_failure_code": "",
                "active_lease": {},
                "processed_count": 0,
                "candidate_count": 0,
                "daily_event_count": 0,
                "config_revision": "",
                "pending_reasons": ["turn_threshold"],
                "pending_requested_at": "",
                "last_daily_finalize_date": "",
            }
        ),
        encoding="utf-8",
    )
    assert _store_root(quiet).is_dir()

    page_hit = _page(owners)  # 冷启动写入投影
    page_warm = _page(owners)  # 命中投影缓存
    fact_cache._OWNER_FACT_CACHE.clear()
    page_cleared = _page(owners)  # 清缓存后现读

    for page in (page_warm, page_cleared):
        assert page.owners == page_hit.owners
        assert page.hard_owners == page_hit.hard_owners
        assert page.soft_owners == page_hit.soft_owners
    assert [owner.owner_id for owner in page_hit.hard_owners] == ["u-hard"]
    assert [owner.owner_id for owner in page_hit.soft_owners] == ["u-soft"]

    # 外部改写:硬事实消失 + 新的 wake 出现在 quiet owner 上
    (_store_root(hard) / "progress_policies" / "p.json").unlink()
    _write_wake(_store_root(quiet), "sig-1")
    rewritten = _page(owners)
    fact_cache._OWNER_FACT_CACHE.clear()
    rewritten_live = _page(owners)

    assert rewritten.owners == rewritten_live.owners
    assert [owner.owner_id for owner in rewritten.hard_owners] == ["u-quiet"]
    assert [owner.owner_id for owner in rewritten.soft_owners] == ["u-soft"]


def test_fact_cache_is_bounded_and_signature_covers_read_paths(fact_cache, tmp_path) -> None:
    """条目上界 + 签名确实覆盖判定读到的路径(wake/policy/agents/账本/调度/策展/db)。"""
    owners = tmp_path / "owners"
    home = _owner_home(owners, "u1")
    names = {name for name, _payload in _owner_fact_signature(home)}
    assert {
        "store_roots",
        "agents",
        "task_links",
        "ledger:runs",
        "ledger:tasks",
        "audit",
        "watch_state",
        "memory/curator/state.json",
        "memory/candidates.jsonl",
        "memory_policy.json",
        "data/scheduler/store.json",
        "runtime_db",
        "runtime_db-wal",
        "runtime_db-shm",
    } <= names
    assert any(name.startswith("wake_normal:") for name in names)

    for index in range(_FACT_MAX_ENTRIES + 2):
        extra = _owner_home(owners, f"u{index:05d}")
        discover_wake_pending_owners(owners)
        del extra
    assert len(fact_cache._OWNER_FACT_CACHE) <= _FACT_MAX_ENTRIES


def test_fact_signature_digest_changes_on_every_relevant_edit(fact_cache, tmp_path) -> None:
    """签名摘要:新增/删除/改写四类事实文件都必须改变摘要(逐项对照,不靠结论反推)。"""
    owners = tmp_path / "owners"
    home = _owner_home(owners, "u1")
    baseline, files = _owner_fact_signature_digest(home)
    assert files >= 0

    def digest() -> str:
        return _owner_fact_signature_digest(home)[0]

    _write_wake(_store_root(home), "sig-1")
    after_wake = digest()
    assert after_wake != baseline

    _write_policy(_store_root(home), "p", enabled=True, next_due_at=time.time() - 1)
    after_policy = digest()
    assert after_policy != after_wake

    _write_subagent_run(home, "run-1", "PENDING")
    after_agent = digest()
    assert after_agent != after_policy

    _write_task_ledger(home, "task-1")
    after_ledger = digest()
    assert after_ledger != after_agent

    settled = home / "agents" / "run-1" / "state.json"
    settled.write_text(json.dumps({"run_id": "run-1", "status": "DONE"}), encoding="utf-8")
    assert digest() != after_ledger
