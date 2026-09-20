#!/usr/bin/env python3
# LLM: 基准只在临时夹具测调度读取；上下文退出必须还原进程内替换函数，不能污染后续采样。
# 模块用途: 比较调度仓库的解析次数和锁内耗时，不读写用户任务。
"""GW-03 配对基准:scheduler 只读路径的"锁内全量解析"成本(N 条 run 的 owner store)。

度量口径(与 R265/R267/R274 的验收同源):
  · total_median_ms : 调用墙钟中位
  · hold_median_ms  : 锁临界区持有时长中位(最关键指标:它决定会不会卡住同一 owner 的其它工作)
  · parse_per_call  : 每次调用的 _parse_run 次数(是否全量逐条解析)
  · load_per_call   : 每次调用的 _load_store_unlocked 次数(是否在锁内读盘)

用法:
  # 单树(当前工作树)
  python3 scripts/bench/measure_scheduler_reads.py --runs 10000 --rounds 5
  # 配对 A/B:分别指向两个 checkout,交替多轮取中位(机器负载漂移下唯一可信的比法)
  python3 scripts/bench/measure_scheduler_reads.py --repo /path/to/before --runs 10000 --rounds 5

夹具:临时目录里造一份 owner store.json(默认 10000 条 run + 50 个 job),
只含结构化字段与占位正文,不含任何真实会话内容或密钥。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_REPO = Path(__file__).resolve().parents[2]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(DEFAULT_REPO), help="被测 checkout 根目录")
    parser.add_argument("--runs", type=int, default=10000, help="夹具里的 run 条数")
    parser.add_argument("--rounds", type=int, default=5, help="每个路径的计时轮数")
    return parser.parse_args()


ARGS = parse_args()
REPO = Path(ARGS.repo).resolve()
sys.path.insert(0, str(REPO))

import agent_py_agent.agent.scheduler.repository as repo_mod  # noqa: E402
from agent_py_agent.agent.scheduler.repository import SchedulerRepository  # noqa: E402

OWNER = {"provider": "local", "kind": "main", "id": "local/main"}


def build_store(path: Path, runs_total: int) -> int:
    runs: dict[str, object] = {}
    now = time.time()
    queued = 0
    for index in range(runs_total):
        status = "queued" if index % 4 == 0 else ("running" if index % 4 == 1 else "claimed")
        if status == "queued":
            queued += 1
        runs[f"srun_{index:06d}"] = {
            "schema_version": "scheduler_run.v1",
            "owner": dict(OWNER),
            "run_id": f"srun_{index:06d}",
            "job_id": f"job_{index:06d}",
            "thread_id": f"thread-{index}",
            "status": status,
            "attempt": 1,
            "started_at": now - index,
            "scheduled_for": now + index,
            "waiting_since": 0.0,
            "updated_at": now,
        }
    jobs: dict[str, object] = {}
    for index in range(50):
        jobs[f"job_{index:06d}"] = {
            "schema_version": "scheduler_job.v1",
            "owner": dict(OWNER),
            "job_id": f"job_{index:06d}",
            "name": f"job-{index}",
            "prompt": "do work",
            "thread_id": f"thread-{index}",
            "source_task_id": "task-1",
            "status": "active" if index % 2 == 0 else "paused",
            "schedule": {
                "kind": "every",
                "every_seconds": 600,
                "anchor_at": 1000.0,
                "timezone": "UTC",
            },
            "skill_refs": [],
            "version": 1,
            "next_run_at": now + 600,
        }
    store = {
        "schema_version": "scheduler_store.v1",
        "owner": dict(OWNER),
        "jobs": jobs,
        "runs": runs,
        "updated_at": now,
    }
    path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return queued


class Instrument:
    """统计 _parse_run / _load_store_unlocked 次数与锁临界区持有时长。"""

    def __init__(self, module) -> None:
        self.module = module
        self.parse_runs = 0
        self.load_store = 0
        self.holds: list[float] = []
        self._orig_parse = module.SchedulerRepository._parse_run
        self._orig_load = module.SchedulerRepository._load_store_unlocked
        self._orig_lock = module.locked_json_path

    def __enter__(self):
        instrument = self

        def parse(self, raw, *, terminal=False):
            instrument.parse_runs += 1
            return instrument._orig_parse(self, raw, terminal=terminal)

        def load(self):
            instrument.load_store += 1
            return instrument._orig_load(self)

        @contextlib.contextmanager
        def timed_lock(path):
            with instrument._orig_lock(path):
                started = time.perf_counter()
                try:
                    yield
                finally:
                    instrument.holds.append(time.perf_counter() - started)

        self.module.SchedulerRepository._parse_run = parse
        self.module.SchedulerRepository._load_store_unlocked = load
        self.module.locked_json_path = timed_lock
        return self

    # LLM: 离开基准采样时必须还原三个被替换的入口；标准上下文参数不改变异常传播。
    # 函数用途: 恢复进程内原函数，基准失败时也不吞掉错误。
    def __exit__(self, exc_type, exc_value, traceback):
        self.module.SchedulerRepository._parse_run = self._orig_parse
        self.module.SchedulerRepository._load_store_unlocked = self._orig_load
        self.module.locked_json_path = self._orig_lock
        return False

    def reset(self) -> None:
        self.parse_runs = 0
        self.load_store = 0
        self.holds.clear()


def cases() -> dict[str, object]:
    return {
        "queued_runs(every tick)": lambda repo: repo.queued_runs(limit=64),
        "active_runs_by_job(tool)": lambda repo: repo.active_runs_by_job(),
        "list_jobs(tool)": lambda repo: repo.list_jobs(),
        "get_job(tool)": lambda repo: repo.get_job("job_000000"),
        "corruption_report(diag)": lambda repo: repo.corruption_report(),
        "reserve_due_runs(tick write)": lambda repo: repo.reserve_due_runs(
            now=time.time() + 10_000, limit=32
        ),
    }


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix=f"bench-sched-reads-{ARGS.runs}-"))
    store_path = root / "store.json"
    queued = build_store(store_path, ARGS.runs)
    repo = SchedulerRepository(
        root, owner_provider="local", owner_kind="main", owner_id="local/main"
    )
    print(
        f"repo={REPO}\nruns={ARGS.runs} queued={queued} "
        f"bytes={store_path.stat().st_size} rounds={ARGS.rounds}"
    )
    print(
        f"{'path':<28}{'total_median_ms':>17}{'hold_median_ms':>16}"
        f"{'hold_max_ms':>13}{'parse/call':>12}{'load/call':>11}"
    )
    with Instrument(repo_mod) as instrument:
        for label, call in cases().items():
            for _ in range(2):
                call(repo)
            instrument.reset()
            samples: list[float] = []
            for _ in range(ARGS.rounds):
                started = time.perf_counter()
                call(repo)
                samples.append((time.perf_counter() - started) * 1000.0)
            holds = sorted(instrument.holds)
            print(
                f"{label:<28}{statistics.median(samples):>17.2f}"
                f"{statistics.median(instrument.holds) * 1000:>16.3f}"
                f"{holds[-1] * 1000:>13.3f}"
                f"{instrument.parse_runs // ARGS.rounds:>12}"
                f"{instrument.load_store // ARGS.rounds:>11}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
