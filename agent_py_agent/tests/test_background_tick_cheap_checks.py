"""E10 一期"便宜检查"（background_tick_cheap_checks）验收测试。

被测对象（设计 docs/design/GATEWAY_BACKGROUND_TICK.md 4.1–4.3、4.7）:
  ① runtime_db.wakes_need_attention + conversation.runtime 拍内 gate:
     一条只读合并 EXISTS 探针, 条件与 reclaim/pop 的 WHERE 逐字相同;
     为假才允许本拍跳过两个写事务, 异常/缺失一律回退原两步。
  ② scheduler/repository 的 store.json 文件身份门:
     cheap 命中只做一次 stat(不读字节/不摘要), 2 秒保护窗口 + 60 秒全量兜底 +
     有界 LRU(64); 时间条件每次用新 now 重新评估。
  ③ tooling/process_session_store 的进程记录目录 stat 门:
     目录身份 (dev, ino, mtime_ns) 未变且离开保护窗口才复用; root 内任何
     新建/改写/删除都改目录 mtime; 60 秒兜底无条件全读。
另覆盖: 开关开/关外部可见结果一致(语义不变)、配置默认关且运行时动态读到、
缓存上界 LRU 逐出。
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from dataclasses import asdict
from dataclasses import replace as dataclass_replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_py_agent.agent.scheduler.repository as scheduler_module
import agent_py_agent.agent.tooling.process_session_store as process_store_module
from agent_py_agent.agent.conversation.process_events import (
    PROCESS_COMPLETION_REASON,
    process_completion_target,
)
from agent_py_agent.agent.conversation.runtime import (
    _BackgroundSchedulerThreadLaneMixin,
    _BackgroundSchedulerTickMixin,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import RuntimeSchemaMixin
from agent_py_agent.agent.scheduler.repository import (
    SchedulerJobCreateRequest,
    SchedulerRepository,
    SchedulerStateError,
)
from agent_py_agent.agent.scheduler.schedule import build_schedule
from agent_py_agent.agent.scheduler.service import SchedulerService
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.tooling.process_registry import BackgroundProcess, process_registry
from agent_py_agent.agent.tooling.process_scope import ProcessAccessScope, ProcessExecutionScope
from agent_py_agent.agent.tooling.process_session_records import (
    LEGACY_PROCESS_SESSION_SCHEMA,
    PROCESS_SESSION_SCHEMA,
)
from agent_py_agent.agent.tooling.process_session_store import (
    ProcessSessionStore,
    ProcessSessionTransaction,
    process_session_store_root,
)

_OWNER = {"provider": "local", "kind": "main", "id": "local/main"}


# --------------------------------------------------------------------------------------
# 夹具与工具
# --------------------------------------------------------------------------------------
def _repository(root: Path) -> SchedulerRepository:
    return SchedulerRepository(
        root,
        owner_provider="local",
        owner_kind="main",
        owner_id="local/main",
    )


def _run_row(
    run_id: str,
    status: str,
    *,
    waiting_since: float = 0.0,
    dispatch_after: float = 0.0,
) -> dict[str, object]:
    return {
        "scheduled_for": 1_000.0,
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
        "dispatch_after": dispatch_after,
    }


def _write_store(
    path: Path,
    runs: dict[str, dict[str, object]],
    *,
    jobs: dict | None = None,
) -> None:
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


def _wanting_ids(rows: list[dict[str, object]]) -> list[str]:
    return [str(row["run_id"]) for row in rows]


def _age_path(path: Path, *, seconds: float = 3.0) -> None:
    """把路径 mtime 拨到 seconds 秒前, 让 2 秒保护窗口判定通过。"""
    old_ns = int((time.time() - seconds) * 1_000_000_000)
    os.utime(path, ns=(old_ns, old_ns))


@pytest.fixture
def projections():
    """投影缓存是模块级: 用例前后清空两套设施(关=旧 waiting 设施, 开=新三切片设施), 避免跨用例统计串味。"""
    for cache in (
        scheduler_module._WAITING_PROJECTION_CACHE,
        scheduler_module._STORE_PROJECTION_CACHE,
    ):
        cache.clear()
    for stats in (
        scheduler_module._WAITING_PROJECTION_STATS,
        scheduler_module._STORE_PROJECTION_STATS,
    ):
        for name in list(stats):
            stats[name] = 0
    yield scheduler_module
    for cache in (
        scheduler_module._WAITING_PROJECTION_CACHE,
        scheduler_module._STORE_PROJECTION_CACHE,
    ):
        cache.clear()


@pytest.fixture
def process_projections():
    """进程记录投影缓存是模块级: 用例前后清空。"""
    process_store_module._PROCESS_RECORDS_PROJECTION_CACHE.clear()
    yield process_store_module
    process_store_module._PROCESS_RECORDS_PROJECTION_CACHE.clear()


class _ProbeCounter:
    """统计 store.json 全量读探针 (_probe_store_bytes) 的调用次数。"""

    def __init__(self) -> None:
        self.calls = 0

    def install(self, monkeypatch) -> None:
        counter = self
        original = scheduler_module._probe_store_bytes

        def counting(path, **kwargs):  # noqa: ANN001
            counter.calls += 1
            return original(path, **kwargs)

        monkeypatch.setattr(scheduler_module, "_probe_store_bytes", counting)


class _TransactionCounter:
    """统计 ProcessSessionStore.transaction 的进入次数(cheap 命中时必须为 0)。"""

    def __init__(self) -> None:
        self.calls = 0

    def install(self, monkeypatch) -> None:
        counter = self
        original = ProcessSessionStore.transaction

        @contextlib.contextmanager
        def counting(self):  # noqa: ANN001
            counter.calls += 1
            with original(self) as transaction:
                yield transaction

        monkeypatch.setattr(ProcessSessionStore, "transaction", counting)


def _prime_scheduler_hit(repository: SchedulerRepository, store_path: Path) -> tuple:
    """把账本 mtime 拨老并做一次全量读, 让下一次 cheap 调用可命中投影。"""
    _age_path(store_path)
    return repository.waiting_runs(cheap_checks=True)


# --------------------------------------------------------------------------------------
# 候选②: scheduler store.json 文件身份门 (4.2 测试清单 1–7 + 缓存上界)
# --------------------------------------------------------------------------------------
def test_cheap_hit_skips_full_read_and_matches_full_path(tmp_path, projections, monkeypatch) -> None:
    """清单 1: 文件未变 → 不发生全量读, 且与整读路径结论逐字一致。"""
    root = tmp_path / "owner" / "scheduler"
    root.mkdir(parents=True)
    store_path = root / "store.json"
    _write_store(store_path, _store_with_waiting(3))
    repository = _repository(root)
    cold_rows, cold_errors = _prime_scheduler_hit(repository, store_path)

    counter = _ProbeCounter()
    counter.install(monkeypatch)
    warm_rows, warm_errors = repository.waiting_runs(cheap_checks=True)

    assert counter.calls == 0  # 命中不读字节、不算摘要
    assert warm_rows == cold_rows and warm_errors == cold_errors
    assert projections._STORE_PROJECTION_STATS["cheap_hit"] >= 1

    full_rows, full_errors = repository.waiting_runs(cheap_checks=False)
    assert full_rows == warm_rows and full_errors == warm_errors


def test_cheap_invalidated_by_atomic_replace(tmp_path, projections) -> None:
    """清单 2: 原子替换(rename 新 inode) → 失效重读, 新内容生效。

    构造"同尺寸、同 mtime、仅 inode 不同"的替换, 锁住身份判据必须含 inode。
    """
    root = tmp_path / "owner" / "scheduler"
    root.mkdir(parents=True)
    store_path = root / "store.json"
    _write_store(store_path, _store_with_waiting(2))
    repository = _repository(root)
    _prime_scheduler_hit(repository, store_path)
    cached_stat = scheduler_module._store_stat_key(store_path)
    assert cached_stat is not None

    runs = _store_with_waiting(2)
    runs["srun_000"] = _run_row("srun_000", "running", waiting_since=1_000.0)
    replacement = tmp_path / "replacement.json"
    _write_store(replacement, runs)
    assert replacement.stat().st_size == cached_stat[1]  # 同尺寸("waiting"/"running" 等长)
    os.utime(replacement, ns=(cached_stat[0], cached_stat[0]))  # 同 mtime
    os.replace(replacement, store_path)

    rows, errors = repository.waiting_runs(cheap_checks=True)

    assert errors == []
    assert _wanting_ids(rows) == ["srun_001"]


def test_cheap_invalidated_by_in_place_rewrite(tmp_path, projections) -> None:
    """清单 3: 同 inode 原地改写(mtime_ns/size 变化) → 失效重读。"""
    root = tmp_path / "owner" / "scheduler"
    root.mkdir(parents=True)
    store_path = root / "store.json"
    _write_store(store_path, _store_with_waiting(2))
    repository = _repository(root)
    _prime_scheduler_hit(repository, store_path)

    runs = _store_with_waiting(2)
    runs["srun_000"] = _run_row("srun_000", "running")
    runs["srun_009"] = _run_row("srun_009", "waiting", waiting_since=1_009.0)
    _write_store(store_path, runs)  # 同 inode 原地改写(内容长度必变)

    rows, errors = repository.waiting_runs(cheap_checks=True)

    assert errors == []
    assert _wanting_ids(rows) == ["srun_001", "srun_009"]


def test_cheap_refuses_fresh_mtime_within_trust_window(tmp_path, projections, monkeypatch) -> None:
    """清单 4: mtime 距现在 < 2 s → 强制重读(不信缓存)。"""
    # 真实判定: 刚生成的时间戳必在保护窗口内; 3 秒前必已离开。
    assert scheduler_module.cache_entry_trustworthy(time.time_ns()) is False
    assert scheduler_module.cache_entry_trustworthy(int((time.time() - 3.0) * 1_000_000_000)) is True

    # 稳定化: 强制"窗口内"判定, 验证窗口内绝不复用缓存。
    monkeypatch.setattr(scheduler_module, "cache_entry_trustworthy", lambda mtime_ns: False)
    root = tmp_path / "owner" / "scheduler"
    root.mkdir(parents=True)
    store_path = root / "store.json"
    _write_store(store_path, _store_with_waiting(1))
    repository = _repository(root)
    repository.waiting_runs(cheap_checks=True)  # 冷读 → 写投影缓存

    counter = _ProbeCounter()
    counter.install(monkeypatch)
    rows, errors = repository.waiting_runs(cheap_checks=True)

    assert counter.calls == 1  # 窗口内 → 不信缓存 → 全量读
    assert projections._STORE_PROJECTION_STATS["cheap_hit"] == 0
    assert _wanting_ids(rows) == ["srun_000"]


def test_cheap_time_conditions_reapply_with_new_now(tmp_path, projections, monkeypatch) -> None:
    """清单 5: 文件未变但时间推进到 next_due → due 照常出现(时间条件用新 now)。"""
    root = tmp_path / "owner" / "scheduler"
    root.mkdir(parents=True)
    store_path = root / "store.json"
    repository = _repository(root)
    schedule = build_schedule(kind="every", every_seconds=3_600, now=1_000.0)
    job, _created = repository.create_job(
        SchedulerJobCreateRequest(
            name="due-probe",
            prompt="tick",
            thread_id="thread-1",
            schedule=schedule,
            now=1_000.0,
        )
    )
    job_id = str(job["job_id"])
    runs = {
        "srun_wait": _run_row("srun_wait", "waiting", waiting_since=10.0),
        "srun_q": _run_row("srun_q", "queued", dispatch_after=1_500.0),
    }
    payload = json.loads(store_path.read_text(encoding="utf-8"))
    payload["jobs"][job_id]["next_run_at"] = 1_500.0
    _write_store(store_path, runs, jobs=payload["jobs"])
    _prime_scheduler_hit(repository, store_path)

    counter = _ProbeCounter()
    counter.install(monkeypatch)
    # now=1000: 无 due 候选 → reserve 直接空且不读盘; queued 未到 → 空。
    assert repository.reserve_due_runs(now=1_000.0, cheap_checks=True) == []
    assert counter.calls == 0
    assert repository.queued_runs(now=1_000.0, cheap_checks=True) == []

    # now=1501: 同一份文件, 时间推进 → queued 出现; due 候选出现 → 走原路径预留。
    rows = repository.queued_runs(now=1_501.0, cheap_checks=True)
    assert [str(row["run_id"]) for row in rows] == ["srun_q"]
    reserved = repository.reserve_due_runs(now=1_501.0, cheap_checks=True)
    assert [str(row["job_id"]) for row in reserved] == [job_id]


def test_cheap_full_verify_fallback_after_interval(tmp_path, projections, monkeypatch) -> None:
    """清单 6: 60 s 兜底到点 → 无条件整读一次; 重核后恢复命中。"""
    root = tmp_path / "owner" / "scheduler"
    root.mkdir(parents=True)
    store_path = root / "store.json"
    _write_store(store_path, _store_with_waiting(1))
    repository = _repository(root)
    _prime_scheduler_hit(repository, store_path)
    key = repository.waiting_projection_cache_key()
    entry = scheduler_module._STORE_PROJECTION_CACHE[key]
    scheduler_module._STORE_PROJECTION_CACHE[key] = dataclass_replace(
        entry, verified_at=entry.verified_at - 61.0
    )

    counter = _ProbeCounter()
    counter.install(monkeypatch)
    repository.waiting_runs(cheap_checks=True)
    assert counter.calls == 1  # 到点 → 无条件重核

    repository.waiting_runs(cheap_checks=True)
    assert counter.calls == 1  # 重核后立即恢复命中


def test_cheap_failures_fall_back_to_full_path(tmp_path, projections, monkeypatch) -> None:
    """清单 7: stat/读/解析失败 → 回退原路径(宁多查不漏查)。"""
    # a) 账本缺失: 空账本语义, 不得抛错。
    missing_root = tmp_path / "missing" / "scheduler"
    missing_root.mkdir(parents=True)
    assert _repository(missing_root).waiting_runs(cheap_checks=True) == ([], [])

    # b) 坏 JSON: 与全量路径相同的结构化错误。
    broken_root = tmp_path / "broken" / "scheduler"
    broken_root.mkdir(parents=True)
    (broken_root / "store.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(SchedulerStateError):
        _repository(broken_root).waiting_runs(cheap_checks=True)

    # c) 便宜判定自身抛错 → 回退原路径, 结果照常。
    error_root = tmp_path / "error" / "scheduler"
    error_root.mkdir(parents=True)
    _write_store(error_root / "store.json", _store_with_waiting(1))
    repository = _repository(error_root)

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("injected cheap lookup failure")

    monkeypatch.setattr(scheduler_module, "_lookup_cheap_projection", boom)
    rows, errors = repository.waiting_runs(cheap_checks=True)
    assert errors == []
    assert _wanting_ids(rows) == ["srun_000"]


def test_cheap_projection_cache_is_bounded_lru(tmp_path, projections) -> None:
    """缓存上界: 超过 64 个 owner 逐出最旧; 命中会提级 LRU。"""
    repositories: list[SchedulerRepository] = []
    for index in range(65):
        root = tmp_path / f"owner{index:02d}" / "scheduler"
        root.mkdir(parents=True)
        store_path = root / "store.json"
        _write_store(store_path, _store_with_waiting(1))
        _age_path(store_path)
        repository = _repository(root)
        repository.waiting_runs(cheap_checks=True)
        repositories.append(repository)

    assert len(scheduler_module._STORE_PROJECTION_CACHE) == 64
    keys = set(scheduler_module._STORE_PROJECTION_CACHE)
    assert repositories[0].waiting_projection_cache_key() not in keys  # 最旧被逐出
    assert repositories[64].waiting_projection_cache_key() in keys

    # 命中提级: 访问第 2 个后, 再插入一个应逐出真正最旧的(第 3 个)。
    repositories[1].waiting_runs(cheap_checks=True)
    extra_root = tmp_path / "owner65" / "scheduler"
    extra_root.mkdir(parents=True)
    extra_store = extra_root / "store.json"
    _write_store(extra_store, _store_with_waiting(1))
    _age_path(extra_store)
    _repository(extra_root).waiting_runs(cheap_checks=True)

    keys = set(scheduler_module._STORE_PROJECTION_CACHE)
    assert repositories[1].waiting_projection_cache_key() in keys
    assert repositories[2].waiting_projection_cache_key() not in keys


# --------------------------------------------------------------------------------------
# MC4-901 返工: 关闭开关必须真正走回旧读取路径(与 624871919 逐项一致)
# --------------------------------------------------------------------------------------
def _bad_job_store(root: Path) -> Path:
    """写含坏 job(next_run_at="not-a-time")+健康 waiting+queued 的账本, 返回 store 路径。"""
    repository = _repository(root)
    schedule = build_schedule(kind="every", every_seconds=3_600, now=1_000.0)
    job, _created = repository.create_job(
        SchedulerJobCreateRequest(
            name="bad-probe",
            prompt="tick",
            thread_id="thread-1",
            schedule=schedule,
            now=1_000.0,
        )
    )
    store_path = root / "store.json"
    payload = json.loads(store_path.read_text(encoding="utf-8"))
    payload["jobs"][str(job["job_id"])]["next_run_at"] = "not-a-time"
    payload["runs"] = {
        "srun_wait": _run_row("srun_wait", "waiting", waiting_since=1_000.0),
        "srun_q": _run_row("srun_q", "queued", dispatch_after=1_400.0),
    }
    _write_store(store_path, payload["runs"], jobs=payload["jobs"])
    return store_path


def test_off_path_matches_baseline_with_bad_job(tmp_path, projections) -> None:
    """901 反例 1(基线录制对照): 坏 job 时关闭路径照常返回健康 waiting, 不解析 jobs 也不抛异常。

    录制来源: 624871919 上同一场景的输出——waiting=[srun_wait]/errors=[]、queued=[srun_q]、
    reserve 同样抛 ValueError、旧缓存 1 条; 本用例断言关闭路径逐项一致。
    """
    root = tmp_path / "owner" / "scheduler"
    root.mkdir(parents=True)
    _bad_job_store(root)
    repository = _repository(root)

    rows, errors = repository.waiting_runs(cheap_checks=False)

    assert errors == []
    assert _wanting_ids(rows) == ["srun_wait"]
    queued = repository.queued_runs(now=1_501.0, cheap_checks=False)
    assert [row["run_id"] for row in queued] == ["srun_q"]
    with pytest.raises(ValueError):
        repository.reserve_due_runs(now=1_501.0, cheap_checks=False)
    # 关闭路径只写旧 waiting 设施(1 条), 不构造三切片投影。
    assert len(scheduler_module._WAITING_PROJECTION_CACHE) == 1
    assert len(scheduler_module._STORE_PROJECTION_CACHE) == 0


def test_off_path_only_caches_waiting_rows_with_cap_four(tmp_path, projections) -> None:
    """901 反例 2(内存/结构, 基线录制对照): 关闭路径只缓存 waiting 行(旧结构), 上限仍是 4。

    录制来源: 624871919 上 5 个 owner 各读一次 → 旧缓存 4 条(上限 4); 同账本 queued 不进缓存
    (大 prompt 的 queued 完整记录不驻留)。
    """
    for index in range(5):
        root = tmp_path / f"owner{index:02d}" / "scheduler"
        root.mkdir(parents=True)
        _write_store(root / "store.json", _store_with_waiting(1))
        _age_path(root / "store.json")
        _repository(root).waiting_runs(cheap_checks=False)

    assert len(scheduler_module._WAITING_PROJECTION_CACHE) == 4
    entry = next(iter(scheduler_module._WAITING_PROJECTION_CACHE.values()))
    assert sorted(entry.__dataclass_fields__) == ["digest", "errors", "rows", "stat_key"]
    assert all(str(row.get("status")) == "waiting" for row in entry.rows)
    assert len(scheduler_module._STORE_PROJECTION_CACHE) == 0


def test_off_path_settles_finished_task_with_bad_job(tmp_path, projections) -> None:
    """901 反例 3(服务级, 基线录制对照): 坏 job 存在时关闭路径仍能读到健康 waiting 并完成结算。"""
    root = tmp_path / "owner" / "scheduler"
    root.mkdir(parents=True)
    _bad_job_store(root)
    from agent_py_agent.agent.core import SimpleAgent

    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / "home"), enable_tools=False), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 900.0,
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "srun_wait",
            "status": "completed",
            "goal": "diag",
            "now": 1_000.0,
        }
    )
    repository = _repository(root)
    # 原收口依赖真实后续工作查询；缺装配必须保留 waiting，不能当成已无待投递。
    path = root / "store.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["runs"]["srun_wait"]["thread_id"] = thread.thread_id
    _write_store(path, payload["runs"], jobs=payload["jobs"])
    service = SchedulerService(repository, conversation_store=store, follow_up=agent.scheduler_service.follow_up)

    settled = service.reconcile_waiting_runs(now=1_500.0, cheap_checks=False)

    assert settled == ["srun_wait"]
    assert repository.get_active_run("srun_wait") is None


# --------------------------------------------------------------------------------------
# MC4-902 返工: 2 秒保护窗口内的读取结果不得成为后续 stat-only 命中的可信缓存
# --------------------------------------------------------------------------------------
def test_store_projection_not_cached_within_trust_window(tmp_path, projections, monkeypatch) -> None:
    """902: 窗口内冷读不入缓存; 跨出窗口后同 stat 身份改写不被旧投影误信(受控模拟粗粒度碰撞)。"""
    root = tmp_path / "owner" / "scheduler"
    root.mkdir(parents=True)
    store_path = root / "store.json"
    _write_store(store_path, _store_with_waiting(1))
    repository = _repository(root)
    stat_key = store_path.stat()

    # 1) 窗口内冷读: 结果只返回, 不放进缓存。
    monkeypatch.setattr(scheduler_module, "cache_entry_trustworthy", lambda mtime_ns: False)
    rows, _errors = repository.waiting_runs(cheap_checks=True)
    assert _wanting_ids(rows) == ["srun_000"]
    assert rows[0]["waiting_since"] == 1_000.0
    assert len(scheduler_module._STORE_PROJECTION_CACHE) == 0

    # 2) 同 stat 身份改写(同 size/mtime/inode): waiting_since 1000.0 → 2000.0。
    runs = _store_with_waiting(1)
    runs["srun_000"]["waiting_since"] = 2_000.0
    _write_store(store_path, runs)
    os.utime(store_path, ns=(stat_key.st_mtime_ns, stat_key.st_mtime_ns))
    assert store_path.stat().st_size == stat_key.st_size

    # 3) 跨出窗口: 没有窗口内结果可误信, 必须完整重读拿到新值。
    monkeypatch.setattr(scheduler_module, "cache_entry_trustworthy", lambda mtime_ns: True)
    rows, errors = repository.waiting_runs(cheap_checks=True)
    assert errors == []
    assert rows[0]["waiting_since"] == 2_000.0


# --------------------------------------------------------------------------------------
# 候选③: 进程记录目录 stat 门 (4.3 测试清单 1–7)
# --------------------------------------------------------------------------------------
_SCOPE = ProcessExecutionScope("/owners/example", "thread-a", "task-a", "run-a", "attempt-a")


def _reservation(session_id: str = "bg-a", **changes: object) -> dict[str, object]:
    """构造一条包含真实协议全部必需字段的预留记录(与 test_process_session_store 同款)。"""
    return {
        "schema": PROCESS_SESSION_SCHEMA,
        "session_id": session_id,
        "revision": 0,
        "access_scope": {
            "owner_id": "owner-a",
            "conversation_id": "thread-a",
            "owner_home": _SCOPE.owner_home,
        },
        "execution_scope": asdict(_SCOPE),
        "activation_scope": None,
        "retain_until_consumed": False,
        "completion_target": {},
        "completion_notice_id": "",
        "command": "test command",
        "cwd": "/workspace",
        "output_file": "/workspace/command.log",
        "host_state_file": "",
        "launcher_pid": 100,
        "launcher_birth_token": "launcher-birth",
        "pid": 0,
        "pid_birth_token": "",
        "child_pid": 0,
        "child_pid_birth_token": "",
        "reserved_at": 1.0,
        "started_at": 0.0,
        "finished_at": None,
        "exit_code": None,
        "status": "starting",
        "child_launch_started": False,
        "stop_requested": False,
        "handoff_confirmed": False,
        **changes,
    }


def _running(session_id: str = "bg-a", **changes: object) -> dict[str, object]:
    return _reservation(
        session_id,
        **{
            "pid": 101,
            "pid_birth_token": "host-birth",
            "child_pid": 102,
            "child_pid_birth_token": "child-birth",
            "child_launch_started": True,
            "started_at": 2.0,
            "status": "running",
            **changes,
        },
    )


def _session_ids(records: list[dict[str, object]]) -> list[str]:
    return [str(record["session_id"]) for record in records]


def test_process_records_cheap_hit_skips_transaction_and_glob(
    tmp_path, process_projections, monkeypatch
) -> None:
    """清单 1: 目录未变 → 跳过事务+glob(进入次数 0), 结果与全量路径一致。"""
    store = ProcessSessionStore(tmp_path / "records")
    store.write(_reservation("bg-a"))
    _age_path(store.root)
    cold, cold_errors = store.list_records(cheap_checks=True)  # 冷读 → 写缓存
    assert _session_ids(cold) == ["bg-a"]

    counter = _TransactionCounter()
    counter.install(monkeypatch)
    warm, warm_errors = store.list_records(cheap_checks=True)

    assert counter.calls == 0
    assert warm == cold and warm_errors == cold_errors

    legacy, legacy_errors = store.list_records(cheap_checks=False)
    assert legacy == warm and legacy_errors == warm_errors


def test_process_records_new_record_invalidates_cache(tmp_path, process_projections) -> None:
    """清单 2: 新建记录 → 目录 mtime 变 → 原路径发现。"""
    store = ProcessSessionStore(tmp_path / "records")
    store.write(_reservation("bg-a"))
    _age_path(store.root)
    store.list_records(cheap_checks=True)  # 写缓存

    store.write(_reservation("bg-b"))
    rows, errors = store.list_records(cheap_checks=True)

    assert errors == []
    assert sorted(_session_ids(rows)) == ["bg-a", "bg-b"]


def test_process_records_rewrite_invalidates_cache(tmp_path, process_projections) -> None:
    """清单 3: 改写已有记录(原子替换+unlink) → 目录 mtime 变 → 原路径。"""
    store = ProcessSessionStore(tmp_path / "records")
    current = store.write(_running("bg-a"))
    _age_path(store.root)
    store.list_records(cheap_checks=True)

    store.write({**current, "status": "exited", "finished_at": 3.0, "exit_code": 0})
    rows, errors = store.list_records(cheap_checks=True)

    assert errors == []
    assert [(record["session_id"], record["status"]) for record in rows] == [("bg-a", "exited")]


def test_process_records_delete_invalidates_cache(tmp_path, process_projections) -> None:
    """清单 4: 删除记录 → 目录 mtime 变 → 原路径。"""
    store = ProcessSessionStore(tmp_path / "records")
    store.write(_running("bg-a", status="exited", finished_at=3.0, exit_code=0))
    _age_path(store.root)
    rows, _errors = store.list_records(cheap_checks=True)
    assert _session_ids(rows) == ["bg-a"]

    store.prune_finished(0)
    rows, errors = store.list_records(cheap_checks=True)

    assert errors == []
    assert _session_ids(rows) == []


def test_process_records_trust_window_refuses_fresh_cache(
    tmp_path, process_projections, monkeypatch
) -> None:
    """清单 5: mtime 距现在 < 2 s → 强制原路径。"""
    assert process_store_module.cache_entry_trustworthy(time.time_ns()) is False
    monkeypatch.setattr(process_store_module, "cache_entry_trustworthy", lambda mtime_ns: False)
    store = ProcessSessionStore(tmp_path / "records")
    store.write(_reservation("bg-a"))
    _age_path(store.root)
    store.list_records(cheap_checks=True)  # 冷读 → 写缓存

    counter = _TransactionCounter()
    counter.install(monkeypatch)
    rows, errors = store.list_records(cheap_checks=True)

    assert counter.calls == 1  # 窗口内 → 原路径
    assert errors == []
    assert _session_ids(rows) == ["bg-a"]


def test_process_records_full_read_fallback_after_interval(
    tmp_path, process_projections, monkeypatch
) -> None:
    """清单 6: 60 s 兜底到点 → 无条件全读一次; 重核后恢复命中。"""
    store = ProcessSessionStore(tmp_path / "records")
    store.write(_reservation("bg-a"))
    _age_path(store.root)
    store.list_records(cheap_checks=True)
    key = str(store.root)
    entry = process_store_module._PROCESS_RECORDS_PROJECTION_CACHE[key]
    process_store_module._PROCESS_RECORDS_PROJECTION_CACHE[key] = dataclass_replace(
        entry, verified_at=entry.verified_at - 61.0
    )

    counter = _TransactionCounter()
    counter.install(monkeypatch)
    store.list_records(cheap_checks=True)
    assert counter.calls == 1

    store.list_records(cheap_checks=True)
    assert counter.calls == 1  # 重核后立即恢复命中


def test_process_records_missing_dir_and_stat_failure_fall_back(
    tmp_path, process_projections, monkeypatch
) -> None:
    """清单 7: stat 失败 / 目录不存在 → 原路径。"""
    missing = ProcessSessionStore(tmp_path / "absent")
    assert missing.list_records(cheap_checks=True) == ([], [])

    store = ProcessSessionStore(tmp_path / "records")
    store.write(_reservation("bg-a"))
    _age_path(store.root)
    store.list_records(cheap_checks=True)

    monkeypatch.setattr(process_store_module, "_process_dir_stat_key", lambda root: None)
    rows, errors = store.list_records(cheap_checks=True)

    assert errors == []
    assert _session_ids(rows) == ["bg-a"]  # stat 失败 → 回退原路径


def test_process_records_projection_not_cached_within_trust_window(
    tmp_path, process_projections, monkeypatch
) -> None:
    """902(进程侧): 窗口内全读结果不入缓存; 跨出窗口后同目录身份改写不被旧缓存误信。"""
    store = ProcessSessionStore(tmp_path / "records")
    current = store.write(_running("bg-a"))
    _age_path(store.root)
    dir_stat = store.root.stat()

    monkeypatch.setattr(process_store_module, "cache_entry_trustworthy", lambda mtime_ns: False)
    cold, _errors = store.list_records(cheap_checks=True)
    assert _session_ids(cold) == ["bg-a"]
    assert cold[0]["status"] == "running"
    assert len(process_store_module._PROCESS_RECORDS_PROJECTION_CACHE) == 0

    # 同目录身份改写: 记录状态推进了, 但目录 mtime 恢复到原值(模拟同片碰撞)。
    store.write({**current, "status": "exited", "finished_at": 3.0, "exit_code": 0})
    os.utime(store.root, ns=(dir_stat.st_mtime_ns, dir_stat.st_mtime_ns))

    monkeypatch.setattr(process_store_module, "cache_entry_trustworthy", lambda mtime_ns: True)
    warm, errors = store.list_records(cheap_checks=True)
    assert errors == []
    assert [record["status"] for record in warm] == ["exited"]


def test_process_records_cold_read_result_is_isolated_from_cache(
    tmp_path, process_projections
) -> None:
    """903: 修改冷读返回值不污染缓存; 命中返回值修改同样不回写(入缓存/返回双向隔离)。"""
    store = ProcessSessionStore(tmp_path / "records")
    store.write(_reservation("bg-a"))
    _age_path(store.root)

    cold, _errors = store.list_records(cheap_checks=True)  # 冷读 → 写缓存
    assert _session_ids(cold) == ["bg-a"]
    cold[0]["completion_notice_id"] = "caller-only"  # 调用方原地修改

    warm, _errors = store.list_records(cheap_checks=True)  # cheap 命中
    assert warm[0]["completion_notice_id"] == ""
    warm[0]["completion_notice_id"] = "warm-only"  # 命中返回值同样隔离

    again, _errors = store.list_records(cheap_checks=True)
    assert again[0]["completion_notice_id"] == ""
    legacy, _errors = store.list_records(cheap_checks=False)
    assert legacy[0]["completion_notice_id"] == ""


# --------------------------------------------------------------------------------------
# MC4-1201/1202 返工: 跨窗口延迟发布不得让旧结果入缓存; 嵌套字段必须计入估算
# --------------------------------------------------------------------------------------
def test_store_projection_late_publish_after_window_stays_uncached(
    tmp_path, projections, monkeypatch
) -> None:
    """1201(scheduler): 读取时在窗口内、发布时已跨窗 → 用读取前冻结的事实判断, 不得写缓存。"""
    root = tmp_path / "owner" / "scheduler"
    root.mkdir(parents=True)
    store_path = root / "store.json"
    _write_store(store_path, _store_with_waiting(1))
    _age_path(store_path, seconds=0.5)  # 读取时 mtime 距现在 0.5 秒(窗口内)
    assert not scheduler_module.cache_entry_trustworthy(store_path.stat().st_mtime_ns)
    repository = _repository(root)

    original_read = scheduler_module._read_store_bytes
    slept = {"done": False}

    def slow_read(path):  # noqa: ANN001
        data = original_read(path)
        if not slept["done"]:
            slept["done"] = True
            time.sleep(2.1)  # 模拟读取方暂停: 发布时已跨出 2 秒窗口
        return data

    monkeypatch.setattr(scheduler_module, "_read_store_bytes", slow_read)
    rows, errors = repository.waiting_runs(cheap_checks=True)
    assert _wanting_ids(rows) == ["srun_000"]
    assert errors == []
    assert len(scheduler_module._STORE_PROJECTION_CACHE) == 0


def test_process_records_late_publish_after_window_stays_uncached(
    tmp_path, process_projections, monkeypatch
) -> None:
    """1201(进程侧): 读取时在窗口内、发布时已跨窗 → 不得写缓存。"""
    store = ProcessSessionStore(tmp_path / "records")
    store.write(_running("bg-a"))
    _age_path(store.root, seconds=0.5)
    assert not process_store_module.cache_entry_trustworthy(store.root.stat().st_mtime_ns)

    original = ProcessSessionTransaction.list_records
    slept = {"done": False}

    def slow(self):  # noqa: ANN001
        records, errors = original(self)
        if not slept["done"]:
            slept["done"] = True
            time.sleep(2.1)  # 模拟读取方暂停: 发布时已跨出 2 秒窗口
        return records, errors

    monkeypatch.setattr(ProcessSessionTransaction, "list_records", slow)
    records, errors = store.list_records(cheap_checks=True)
    assert _session_ids(records) == ["bg-a"]
    assert errors == []
    assert len(process_store_module._PROCESS_RECORDS_PROJECTION_CACHE) == 0


def test_waiting_projection_with_huge_nested_fields_is_not_cached(tmp_path, projections) -> None:
    """1202: waiting 行含超大嵌套字段(skill_refs) → 估算超门限 → 不缓存(结果照常返回)。"""
    root = tmp_path / "owner" / "scheduler"
    root.mkdir(parents=True)
    store_path = root / "store.json"
    runs = _store_with_waiting(1)
    runs["srun_000"]["skill_refs"] = [
        {"skill_id": f"skill-{index}", "blob": "x" * 32_768} for index in range(9)
    ]
    _write_store(store_path, runs)
    _age_path(store_path)  # 窗口不是拒绝原因: 唯一原因是嵌套体积超门限
    repository = _repository(root)

    rows, errors = repository.waiting_runs(cheap_checks=True)
    assert _wanting_ids(rows) == ["srun_000"]
    assert errors == []
    assert len(scheduler_module._STORE_PROJECTION_CACHE) == 0


# --------------------------------------------------------------------------------------
# 候选①: wake_queue 合并只读探针 (4.1 测试清单 1–5)
# --------------------------------------------------------------------------------------
def _wake_runtime_repo(tmp_path: Path) -> RuntimeRepository:
    return RuntimeRepository(tmp_path / "runtime.db")


def test_wake_probe_matches_writer_conditions_across_four_states(tmp_path) -> None:
    """清单 4: 四态(空/到期/租约过期/退避中)探针与写操作条件一一对应。"""
    repo = _wake_runtime_repo(tmp_path)

    # 态 A: 空库 → 两步都无事。
    assert repo.wakes_need_attention(now=100.0) is False
    assert repo.reclaim_expired_wakes(now=100.0) == 0
    assert repo.pop_due_wakes(now=100.0) == []

    # 态 B: pending 到期 → 探针 True, pop 领出。
    repo.upsert_wake(root_task_id="t-b", next_due_at=50.0)
    assert repo.wakes_need_attention(now=100.0) is True
    popped = repo.pop_due_wakes(now=100.0)
    assert [row["root_task_id"] for row in popped] == ["t-b"]
    assert repo.reclaim_expired_wakes(now=100.0) == 0

    # 态 C: claimed 且租约过期(lease_until=400) → 探针 True, reclaim 回收。
    assert repo.wakes_need_attention(now=500.0) is True
    assert repo.reclaim_expired_wakes(now=500.0) == 1
    assert repo.pop_due_wakes(now=500.0) == []  # retry_after=501 未到

    # 态 D: 退避中(retry_after 未到) → 探针 False, 两步都无事。
    assert repo.wakes_need_attention(now=500.0) is False
    assert repo.reclaim_expired_wakes(now=500.0) == 0
    assert repo.pop_due_wakes(now=500.0) == []


class _ConnectionCounter:
    """统计 RuntimeRepository 新开 sqlite 连接的次数(_runtime_connect 调用)。"""

    def __init__(self) -> None:
        self.calls = 0

    def install(self, monkeypatch) -> None:
        counter = self
        original = RuntimeSchemaMixin._runtime_connect

        def counting(self):  # noqa: ANN001
            counter.calls += 1
            return original(self)

        monkeypatch.setattr(RuntimeSchemaMixin, "_runtime_connect", counting)


def test_wake_probe_quiet_path_uses_single_connection(tmp_path, monkeypatch) -> None:
    """清单 1(连接): 无事时探针一条连接; 现状两步各一条连接(合计 2)。"""
    repo = _wake_runtime_repo(tmp_path)
    counter = _ConnectionCounter()
    counter.install(monkeypatch)

    assert repo.wakes_need_attention(now=100.0) is False
    assert counter.calls == 1

    counter.calls = 0
    repo.reclaim_expired_wakes(now=100.0)
    repo.pop_due_wakes(now=100.0)
    assert counter.calls == 2


def _cheap_agent(tmp_path: Path, *, cheap: bool) -> SimpleAgent:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            enable_tools=False,
            my_agent_home=str(tmp_path / "home"),
        ),
        tmp_path,
    )
    # 正式配置由集成根同步；这个本地矩阵直接设置冻结后的宿主事实，loader断言另保留。
    agent.config.background_tick_cheap_checks = cheap
    return agent


def _call_enqueue(agent: SimpleAgent, *, now: float) -> None:
    """复用 test_scheduler_wake_tick 的模式: 轻量 tick 对象 + 真实 agent。"""
    fake = _enqueue_fake(agent)
    _BackgroundSchedulerTickMixin._enqueue_unfinished_task_resume_wakes(fake, now=now)


def _enqueue_fake(agent: SimpleAgent) -> _BackgroundSchedulerTickMixin:
    fake = getattr(agent, "_task_resume_fake", None)
    if fake is None:
        fake = _BackgroundSchedulerTickMixin()
        fake.runtime = SimpleNamespace(agent=agent)
        fake.store = agent.conversation_store
        agent._task_resume_fake = fake
    return fake


class _StepCounter:
    """计数 gate 两个写事务步骤是否被调用。"""

    def __init__(self) -> None:
        self.reclaim = 0
        self.pop = 0

    def install(self, monkeypatch) -> None:
        counter = self
        original_reclaim = _BackgroundSchedulerTickMixin._reclaim_expired_wake_leases
        original_pop = _BackgroundSchedulerTickMixin._consume_due_wake_queue

        def reclaim(self, *, now):  # noqa: ANN001
            counter.reclaim += 1
            return original_reclaim(self, now=now)

        def pop(self, *, now):  # noqa: ANN001
            counter.pop += 1
            return original_pop(self, now=now)

        monkeypatch.setattr(_BackgroundSchedulerTickMixin, "_reclaim_expired_wake_leases", reclaim)
        monkeypatch.setattr(_BackgroundSchedulerTickMixin, "_consume_due_wake_queue", pop)


def test_gate_off_short_circuits_probe_and_keeps_legacy_path(tmp_path, monkeypatch) -> None:
    """开关关(默认): 判据不执行, 两步与改动前逐字相同。"""
    agent = _cheap_agent(tmp_path, cheap=False)
    probe_calls: list[float] = []
    original = RuntimeRepository.wakes_need_attention

    def counting(self, *, now):  # noqa: ANN001
        probe_calls.append(now)
        return original(self, now=now)

    monkeypatch.setattr(RuntimeRepository, "wakes_need_attention", counting)
    steps = _StepCounter()
    steps.install(monkeypatch)

    _call_enqueue(agent, now=100.0)

    assert probe_calls == []
    assert (steps.reclaim, steps.pop) == (1, 1)


def test_gate_on_skips_both_steps_when_queue_quiet(tmp_path, monkeypatch) -> None:
    """清单 1: 无事 → 两个写事务都不被调用, 队列保持空。"""
    agent = _cheap_agent(tmp_path, cheap=True)
    steps = _StepCounter()
    steps.install(monkeypatch)

    _call_enqueue(agent, now=100.0)

    assert (steps.reclaim, steps.pop) == (0, 0)
    assert agent.subagents.runtime_db.list_pending_wakes() == []


def test_gate_on_runs_steps_when_pending_due(tmp_path, monkeypatch) -> None:
    """清单 2: pending 到期 → pop 照常执行, 字条被消费。"""
    agent = _cheap_agent(tmp_path, cheap=True)
    agent.subagents.runtime_db.upsert_wake(root_task_id="t-due", next_due_at=50.0)
    steps = _StepCounter()
    steps.install(monkeypatch)

    _call_enqueue(agent, now=100.0)

    assert (steps.reclaim, steps.pop) == (1, 1)
    assert agent.subagents.runtime_db.list_pending_wakes() == []


def test_gate_on_runs_reclaim_when_claimed_lease_expired(tmp_path, monkeypatch) -> None:
    """清单 3: claimed 且租约过期 → reclaim 照常执行。"""
    agent = _cheap_agent(tmp_path, cheap=True)
    repo = agent.subagents.runtime_db
    repo.upsert_wake(root_task_id="t-lease", next_due_at=50.0)
    assert len(repo.pop_due_wakes(now=100.0)) == 1  # 造 claimed + lease=400
    fake = _enqueue_fake(agent)
    fake._wake_reconcile_last_at = 500.0  # 本拍不触发 5min 对账(对账会清非任务字条)
    steps = _StepCounter()
    steps.install(monkeypatch)

    _call_enqueue(agent, now=500.0)

    assert (steps.reclaim, steps.pop) == (1, 1)
    rows = repo.list_pending_wakes()
    assert [row["root_task_id"] for row in rows] == ["t-lease"]
    assert int(rows[0]["attempt_count"]) == 1  # reclaim 回 pending 重试


def test_gate_on_skips_when_only_backoff_pending(tmp_path, monkeypatch) -> None:
    """退避中(retry_after 未到): 探针 False → 两步都跳过, 字条保持原样。"""
    agent = _cheap_agent(tmp_path, cheap=True)
    repo = agent.subagents.runtime_db
    repo.upsert_wake(root_task_id="t-backoff", next_due_at=50.0)
    popped = repo.pop_due_wakes(now=100.0)
    assert len(popped) == 1
    repo.release_wake(str(popped[0]["wake_id"]), retry_after=300.0)
    steps = _StepCounter()
    steps.install(monkeypatch)

    _call_enqueue(agent, now=200.0)

    assert (steps.reclaim, steps.pop) == (0, 0)
    assert [row["root_task_id"] for row in repo.list_pending_wakes()] == ["t-backoff"]


def test_gate_probe_error_falls_back_to_legacy_steps(tmp_path, monkeypatch) -> None:
    """清单 5: 探针抛错 → 仍执行原两步(宁多查不漏查)。"""
    agent = _cheap_agent(tmp_path, cheap=True)
    repo = agent.subagents.runtime_db
    repo.upsert_wake(root_task_id="t-err", next_due_at=50.0)

    def boom(self, *, now):  # noqa: ANN001
        raise RuntimeError("injected probe failure")

    monkeypatch.setattr(RuntimeRepository, "wakes_need_attention", boom)
    steps = _StepCounter()
    steps.install(monkeypatch)

    _call_enqueue(agent, now=100.0)

    assert (steps.reclaim, steps.pop) == (1, 1)
    assert repo.list_pending_wakes() == []  # 原两步照常处理


# --------------------------------------------------------------------------------------
# 语义不变与配置生效 (4.7 总表)
# --------------------------------------------------------------------------------------
def _wake_table_state(db_path) -> list[tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT root_task_id, status, retry_after, attempt_count FROM wake_queue "
            "ORDER BY root_task_id"
        ).fetchall()
    finally:
        conn.close()
    return [tuple(row) for row in rows]


def _seed_tick_scenario(agent: SimpleAgent) -> None:
    """同一组合成输入: 退避中字条 + 过期租约字条 + 到期字条。"""
    repo = agent.subagents.runtime_db
    repo.upsert_wake(root_task_id="t-backoff", next_due_at=50.0)
    popped = repo.pop_due_wakes(now=60.0)
    assert [row["root_task_id"] for row in popped] == ["t-backoff"]
    repo.release_wake(str(popped[0]["wake_id"]), retry_after=1_000.0)
    repo.upsert_wake(root_task_id="t-lease", next_due_at=50.0)
    popped = repo.pop_due_wakes(now=60.0)
    assert [row["root_task_id"] for row in popped] == ["t-lease"]  # lease_until=360
    repo.upsert_wake(root_task_id="t-due", next_due_at=50.0)


class _TickFake(_BackgroundSchedulerThreadLaneMixin, _BackgroundSchedulerTickMixin):
    """组合两个 mixin: prepare_tick 在 ThreadLane 侧, 维护方法在 Tick 侧。"""


def _prepare_fake(agent: SimpleAgent) -> _BackgroundSchedulerTickMixin:
    fake = _TickFake()
    fake.runtime = SimpleNamespace(agent=agent)
    fake.store = agent.conversation_store
    fake.collaboration_store = None
    fake.scheduler_service = None
    return fake


def test_prepare_tick_semantics_unchanged_across_switch(tmp_path) -> None:
    """4.7 语义不变: 同一合成输入, 开关开/关的 prepare_tick 外部可见结果一致。"""
    snapshots: dict[bool, dict[str, object]] = {}
    for cheap in (False, True):
        case = tmp_path / ("on" if cheap else "off")
        case.mkdir()
        agent = _cheap_agent(case, cheap=cheap)
        _seed_tick_scenario(agent)
        fake = _prepare_fake(agent)
        fake.prepare_tick(now=500.0, include_orphan_supervision=False)
        repo = agent.subagents.runtime_db
        snapshots[cheap] = {
            "wake_table": _wake_table_state(repo.db_path),
            "signals": sorted(
                (signal.reason, signal.root_task_id)
                for signal in agent.conversation_store.wakes.pending(limit=0)
            ),
        }

    assert snapshots[True] == snapshots[False]


# --------------------------------------------------------------------------------------
# MC4-904 返工: 真实临时存储上的四维对照 (wake / scheduler 账本 / 进程通知 / 协作)
# --------------------------------------------------------------------------------------
def _seed_scheduler_case(agent: SimpleAgent, thread_id: str) -> None:
    """已到期 job + waiting run: 让 scheduler 维度在 tick 里真实走预留与排队。"""
    repository = agent.scheduler_repository
    schedule = build_schedule(kind="every", every_seconds=3_600, now=1_000.0)
    job, _created = repository.create_job(
        SchedulerJobCreateRequest(
            name="tick-job",
            prompt="tick",
            thread_id=thread_id,
            schedule=schedule,
            now=1_000.0,
        )
    )
    store_path = repository.store_path
    payload = json.loads(store_path.read_text(encoding="utf-8"))
    job_id = str(job["job_id"])
    payload["jobs"][job_id]["next_run_at"] = 100.0
    # waiting run 不挂到这个 job 上: 否则 _job_has_active_run 会判定该 job 已有活跃
    # run 而跳过预留, scheduler 维度就推不动了。
    runs = {"srun_wait": _run_row("srun_wait", "waiting", waiting_since=10.0)}
    runs["srun_wait"]["thread_id"] = thread_id
    _write_store(store_path, runs, jobs=payload["jobs"])
    _age_path(store_path)


def _seed_process_completion(agent: SimpleAgent, thread) -> None:
    """终态进程记录(带完成目标): 让进程维度在 tick 里真实发出完成通知。"""
    params = SimpleNamespace(
        context_scope="conversation",
        run_id="run-a",
        task_id="task-a",
        request_id="req-a",
        task_attributes={
            "conversation_thread_id": thread.thread_id,
            "conversation_task_id": "task-a",
        },
    )
    target = process_completion_target(agent, params)
    assert target, "进程完成目标构造失败, 先检查 agent 配置"
    root = process_session_store_root(
        agent.effective_workspace_root, agent.home_paths.owner_home_dir
    )
    record = BackgroundProcess(
        "bg-test-notice",
        "test",
        99999999,
        1.0,
        access_scope=ProcessAccessScope(
            "a", thread.thread_id, str(agent.home_paths.owner_home_dir)
        ),
        pid_birth_token="test",
        status="exited",
        exit_code=0,
        completion_target=target,
        store_root=str(root),
    )
    record.persisted_snapshot = {"schema": LEGACY_PROCESS_SESSION_SCHEMA}
    ProcessSessionStore(root).write(record.to_record())
    process_registry.clear()


def _seed_collaboration_case(case_dir: Path, thread_id: str):
    """过期协作请求: 让协作维度在 tick 里真实留下超时状态。"""
    from agent_py_agent.agent.collaboration import AgentCapability, CollaborationStore

    store = CollaborationStore(case_dir / "collaboration")
    store.register_agent(AgentCapability(agent_id="source-b", capabilities=("query",)))
    case = store.open_case(
        {
            "thread_id": thread_id,
            "task_id": "task-1",
            "title": "超时协作 case",
            "summary": "请求过期后要留下结构化状态。",
            "priority": "normal",
            "created_by": "source-a",
            "now": 3.0,
        }
    )
    store.request_collaboration(
        {
            "case_id": case.case_id,
            "requester_agent_id": "source-a",
            "target_agent_ids": ("source-b",),
            "question": "请补充证据。",
            "deadline_at": 10.0,
            "now": 4.0,
        }
    )
    return store, case.case_id


def _collaboration_snapshot(status: dict) -> dict[str, object]:
    """过滤随机 case/request id, 只留可对照的结构化计数与状态。"""
    requests = status.get("requests")
    rows = requests if isinstance(requests, list) else []
    coverage = status.get("response_coverage")
    return {
        "blocked": status.get("blocked_request_count"),
        "timed_out": status.get("timed_out_request_count"),
        "missing_targets": (coverage or {}).get("missing_target_count"),
        "requests": sorted(str(item.get("status")) for item in rows),
    }


def _four_dimension_snapshot(
    agent: SimpleAgent, conversation, collaboration, case_id: str
) -> dict[str, object]:
    """tick 后的四维快照: wake 表 / 信号 / scheduler 账本 / 协作状态(随机 id 归一化)。"""
    payload = json.loads(
        agent.scheduler_repository.store_path.read_text(encoding="utf-8")
    )
    signals = [
        (str(signal.reason), str(signal.root_task_id))
        for signal in conversation.wakes.pending(limit=0)
    ]
    return {
        "wake_table": _wake_table_state(agent.subagents.runtime_db.db_path),
        "signals": sorted(
            (reason, "<run>" if reason == "scheduled_job_due" else task_id)
            for reason, task_id in signals
        ),
        "process_notices": sorted(
            task_id
            for reason, task_id in signals
            if reason == PROCESS_COMPLETION_REASON
        ),
        "scheduler_runs": sorted(
            (str(raw.get("status")), str(raw.get("trigger") or ""))
            for raw in payload["runs"].values()
        ),
        "scheduler_next_run_at": sorted(
            float(raw.get("next_run_at") or 0.0) for raw in payload["jobs"].values()
        ),
        "collaboration": _collaboration_snapshot(collaboration.case_status(case_id)),
    }


def test_prepare_tick_four_dimensions_with_real_stores(tmp_path) -> None:
    """904 返工: 真实临时存储四维对照(开/关一致)——wake、scheduler 账本、进程通知、协作。"""
    snapshots: dict[bool, dict[str, object]] = {}
    for cheap in (False, True):
        case = tmp_path / ("on" if cheap else "off")
        case.mkdir()
        agent = _cheap_agent(case, cheap=cheap)
        conversation = agent.conversation_store
        thread = conversation.threads.get_or_create(
            {
                "canonical_user_id": "user-1",
                "channel": "internal",
                "channel_conversation_id": "thread-1",
                "channel_user_id": "user-1",
                "now": 1.0,
            }
        )
        conversation.tasks.bind(
            {
                "thread_id": thread.thread_id,
                "task_id": "task-1",
                "goal": "四维对照",
                "now": 2.0,
            }
        )
        _seed_tick_scenario(agent)
        _seed_scheduler_case(agent, thread.thread_id)
        _seed_process_completion(agent, thread)
        collaboration, case_id = _seed_collaboration_case(case, thread.thread_id)

        # tick 前的 wake 字条状态: 对账会按"未完成任务"清理合成字条, 只取 tick 后
        # 快照会退化成空==空; 先记录 seeded 状态, 让 wake 维度真的可对照。
        seeded_wake_table = _wake_table_state(agent.subagents.runtime_db.db_path)

        fake = _TickFake()
        fake.runtime = SimpleNamespace(agent=agent)
        fake.store = conversation
        fake.collaboration_store = collaboration
        fake.scheduler_service = agent.scheduler_service
        fake.prepare_tick(now=500.0, include_orphan_supervision=False)

        snapshots[cheap] = {
            "wake_table_seeded": seeded_wake_table,
            **_four_dimension_snapshot(agent, conversation, collaboration, case_id),
        }

    assert snapshots[True] == snapshots[False]


def test_scheduler_reads_switch_consistency(tmp_path, projections) -> None:
    """4.7 语义不变(store 写维度): 同一份账本, 开/关的 waiting/queued/reserve 结果一致。"""
    results: dict[bool, dict[str, object]] = {}
    for cheap in (False, True):
        case = tmp_path / ("cheap" if cheap else "full")
        root = case / "scheduler"
        root.mkdir(parents=True)
        repository = _repository(root)
        schedule = build_schedule(kind="every", every_seconds=3_600, now=1_000.0)
        job, _created = repository.create_job(
            SchedulerJobCreateRequest(
                name="probe",
                prompt="tick",
                thread_id="thread-1",
                schedule=schedule,
                now=1_000.0,
            )
        )
        job_id = str(job["job_id"])
        store_path = root / "store.json"
        payload = json.loads(store_path.read_text(encoding="utf-8"))
        payload["jobs"][job_id]["next_run_at"] = 1_500.0
        runs = {
            "srun_wait": _run_row("srun_wait", "waiting", waiting_since=10.0),
            "srun_q": _run_row("srun_q", "queued", dispatch_after=1_400.0),
        }
        _write_store(store_path, runs, jobs=payload["jobs"])
        _age_path(store_path)
        repository.waiting_runs(cheap_checks=cheap)  # cheap 时预热投影

        waiting = repository.waiting_runs(cheap_checks=cheap)
        queued = repository.queued_runs(now=1_501.0, cheap_checks=cheap)
        reserved = repository.reserve_due_runs(now=1_501.0, cheap_checks=cheap)
        updated = json.loads(store_path.read_text(encoding="utf-8"))
        results[cheap] = {
            "waiting": waiting,
            "queued": [str(row["run_id"]) for row in queued],
            "reserved": [
                (str(row["status"]), str(row["trigger"]))
                for row in reserved
            ],
            "job_next_run_at": updated["jobs"][job_id]["next_run_at"],
        }

    assert results[True] == results[False]


def test_tick_cheap_checks_reads_live_config() -> None:
    """开关在拍内动态读取: 缺配置/关 → False; 开 → True。"""
    mixin = _BackgroundSchedulerTickMixin()
    assert (
        mixin._tick_cheap_checks(
            SimpleNamespace(config=SimpleNamespace(background_tick_cheap_checks=True))
        )
        is True
    )
    assert (
        mixin._tick_cheap_checks(
            SimpleNamespace(config=SimpleNamespace(background_tick_cheap_checks=False))
        )
        is False
    )
    assert mixin._tick_cheap_checks(SimpleNamespace()) is False


def test_config_defaults_off_and_yaml_switch_is_read(tmp_path) -> None:
    """默认关; YAML 显式打开后装载读到 True。"""
    assert AgentConfig().background_tick_cheap_checks is False
    yaml_path = tmp_path / "agent_config.yaml"
    yaml_path.write_text(
        "model_backend: echo\nbackground_tick_cheap_checks: true\n",
        encoding="utf-8",
    )
    loaded = load_config(yaml_path)
    assert loaded.background_tick_cheap_checks is True
