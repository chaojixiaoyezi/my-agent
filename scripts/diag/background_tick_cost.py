#!/usr/bin/env python3
# LLM: 仅在TemporaryDirectory中建立显式合成存储，不修改HOME/日常配置，不调用模型或网络；测量不代表整个Gateway或容量结论。
# 模块用途: 在相同空闲存储夹具上比较后台便宜检查开关的实际查询开销与事实摘要。
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import RuntimeSchemaMixin
from agent_py_agent.agent.scheduler import repository as scheduler_module
from agent_py_agent.agent.scheduler.repository import SchedulerRepository
from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore


# LLM: 每个句柄只指向本次临时根；不构造SimpleAgent，不解析任何默认owner或私人凭据。
# 类用途: 组合一个合成owner的三类存储入口。
@dataclass(frozen=True)
class _OwnerStores:
    runtime: RuntimeRepository
    scheduler: SchedulerRepository
    process: ProcessSessionStore


# LLM: 计数器只观察本进程测量窗口，不干预SQL、连接工厂或事务；窗口外的audit hook不触碰连接。
# 类用途: 保存实际存储调用量和计时结果。
@dataclass
class _Counters:
    sqlite_connections: int = 0
    sql_statements: int = 0
    scheduler_reads: int = 0
    scheduler_read_bytes: int = 0
    process_transactions: int = 0
    wall_seconds: float = 0.0
    cpu_seconds: float = 0.0
    active: bool = False

    # LLM: audit的handle事件发生于SQLite初始化结束前，不能操作它；这里只观察connect，不换工厂或参数。
    # 函数用途: 累计当前测量窗口的真实SQLite建连数。
    def audit(self, event: str, arguments: tuple) -> None:
        if not self.active:
            return
        if event == "sqlite3.connect":
            self.sqlite_connections += 1

    # LLM: trace只计数，返回值不影响SQLite；窗口结束后已有回调保持无副作用。
    # 函数用途: 累计测量区间内实际执行的SQL语句。
    def statement(self, _sql: str) -> None:
        if self.active:
            self.sql_statements += 1


# LLM: 数据只写传入临时root；主线活动store不接受done历史，使用合法的未来queued行，不能以坏记录跳过解析。
# 函数用途: 创建指定未来排队规模的合成存储，预先让文件离开两秒可信保护窗口。
def _build_stores(root: Path, owners: int, ledger_runs: int) -> list[_OwnerStores]:
    stores = []
    aged_ns = int((time.time() - 3.0) * 1_000_000_000)
    future = time.time() + 86400
    for index in range(owners):
        owner_root = root / f"owner-{index}"
        runtime = RuntimeRepository(owner_root / "runtime.db")
        scheduler = SchedulerRepository(owner_root / "scheduler", owner_provider="local", owner_kind="user", owner_id=f"synthetic-{index}")
        runs = {f"srun_{item:08d}": {
            "schema_version": "scheduler_run.v1", "owner": scheduler.owner, "run_id": f"srun_{item:08d}",
            "job_id": f"job_{item:08d}", "status": "queued", "claim_epoch": 0, "scheduled_for": future,
            "dispatch_after": future, "trigger": "due", "prompt": "x" * 320,
        } for item in range(ledger_runs)}
        scheduler.store_path.write_text(json.dumps({"schema_version": "scheduler_store.v1", "owner": scheduler.owner,
            "jobs": {}, "runs": runs, "updated_at": 1001.0}), encoding="utf-8")
        process = ProcessSessionStore(owner_root / "processes")
        process.root.mkdir(parents=True)
        process.list_records()
        os.utime(scheduler.store_path, ns=(aged_ns, aged_ns))
        os.utime(process.root, ns=(aged_ns, aged_ns))
        stores.append(_OwnerStores(runtime, scheduler, process))
    return stores


# LLM: 只透明包装两个固定签名的读入口，finally恢复原对象；不修改生产文件或持久状态。
# 函数用途: 在测量区间累计scheduler真实读取字节和process原事务进入次数。
@contextmanager
def _observe_reads(counters: _Counters) -> Iterator[None]:
    read = scheduler_module._read_store_bytes
    transaction = ProcessSessionStore.transaction
    runtime_connect = RuntimeSchemaMixin._runtime_connect

    # LLM: 完整物理连接返回后才安装trace；不替换工厂或参数，SQL口径不含连接初始化PRAGMA。
    # 函数用途: 在原runtime连接就绪后观察业务SQL，保持连接和事务完全由原入口管理。
    def counted_runtime_connect(store: RuntimeSchemaMixin):
        connection = runtime_connect(store)
        connection.set_trace_callback(counters.statement)
        return connection

    # LLM: 始终调用原no-follow快照读取；只计算返回字节长度，不保存正文。
    # 函数用途: 计数当前账本的实际完整读取。
    def counted_read(path: Path) -> bytes | None:
        content = read(path)
        counters.scheduler_reads += 1
        counters.scheduler_read_bytes += len(content) if content is not None else 0
        return content

    # LLM: 原wait_check和事务恢复/提交入口完全透传，计数不拥有锁或停止权限。
    # 函数用途: 计数一次process目录事务并保持原退出清理。
    @contextmanager
    def counted_transaction(store: ProcessSessionStore, *, wait_check=None):
        counters.process_transactions += 1
        with transaction(store, wait_check=wait_check) as current:
            yield current

    scheduler_module._read_store_bytes = counted_read
    ProcessSessionStore.transaction = counted_transaction
    RuntimeSchemaMixin._runtime_connect = counted_runtime_connect
    try:
        yield
    finally:
        scheduler_module._read_store_bytes = read
        ProcessSessionStore.transaction = transaction
        RuntimeSchemaMixin._runtime_connect = runtime_connect


# LLM: 只对齐wake/scheduler/process三个存储阶段，不包含事件预留、取消恢复、权限、通知或模型，不得声称完整tick收益。
# 函数用途: 用真实产品读口检查一次相同空闲夹具，时间条件每轮使用当前时刻。
def _storage_round(stores: list[_OwnerStores], cheap: bool) -> None:
    for store in stores:
        current = time.time()
        if not cheap or store.runtime.wakes_need_attention(now=current):
            assert store.runtime.reclaim_expired_wakes(now=current) == 0
            assert store.runtime.pop_due_wakes(now=current) == []
        assert store.scheduler.waiting_runs(cheap_checks=cheap) == ([], [])
        assert store.scheduler.reserve_due_runs(now=current, cheap_checks=cheap) == []
        assert store.scheduler.queued_runs(now=current, cheap_checks=cheap) == []
        assert store.process.list_records(cheap_checks=cheap) == ([], [])


# LLM: 比较规范事实内容，不把缓存命中数当成正确性；每个测量臂都必须得到相同摘要。
# 函数用途: 读取本次合成存储的事实摘要，确认计量没有丢失或增加业务状态。
def _facts(stores: list[_OwnerStores]) -> str:
    values = [{"scheduler": store.scheduler.store_path.read_text(encoding="utf-8"),
               "pending_wakes": store.runtime.list_pending_wakes(), "process_records": store.process.list_records()}
              for store in stores]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


# LLM: 预热在计量外，audit hook按active隔离；开关臂之间不修改夹具，事实不一致即失败而不是生成收益报告。
# 函数用途: 测量指定轮数的实际存储开销，并在退出时停止所有计数。
def _measure(stores: list[_OwnerStores], cheap: bool, ticks: int) -> dict:
    _storage_round(stores, cheap)
    counters = _Counters()
    sys.addaudithook(counters.audit)
    try:
        with _observe_reads(counters):
            wall, cpu = time.perf_counter(), time.process_time()
            counters.active = True
            for _tick in range(ticks):
                _storage_round(stores, cheap)
            counters.wall_seconds = time.perf_counter() - wall
            counters.cpu_seconds = time.process_time() - cpu
    finally:
        counters.active = False
    result = asdict(counters)
    result.pop("active")
    return {"cheap_checks": cheap, **result, "facts_sha256": _facts(stores)}


# LLM: 所有产品构造均使用本次临时路径；仅--out指定的JSON会持久保存，退出不留下模拟owner，不改环境变量。
# 函数用途: 运行off/on实测并输出结构化结果，不做容量外推或配置切换。
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="后台便宜检查的临时存储实测；不改HOME、不调用模型或网络。")
    parser.add_argument("--owners", type=int, default=4, help="合成owner数，默认4")
    parser.add_argument("--ticks", type=int, default=5, help="每个开关臂的测量轮数，默认5")
    parser.add_argument("--ledger-runs", type=int, default=0, help="每owner的未来排队行数，超过512会触发投影上限")
    parser.add_argument("--out", type=Path, help="可选JSON输出路径")
    args = parser.parse_args(argv)
    if args.owners < 1 or args.ticks < 1 or args.ledger_runs < 0:
        parser.error("owners/ticks必须大于0，ledger-runs不能为负数")
    with tempfile.TemporaryDirectory(prefix="background-tick-cost-") as temporary:
        stores = _build_stores(Path(temporary), args.owners, args.ledger_runs)
        initial = _facts(stores)
        results = [_measure(stores, cheap, args.ticks) for cheap in (False, True)]
        assert all(row["facts_sha256"] == initial for row in results)
    report = {"schema": "background_tick_storage_cost.v1", "scope": "storage_phases_only",
              "sql_scope": "runtime_statements_after_connection_setup", "fixture_removed": True,
              "owners": args.owners, "ticks": args.ticks, "ledger_runs": args.ledger_runs, "results": results}
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
