#!/usr/bin/env python3
"""A 项配对交替 A/B:两个 checkout 上交替采样 `_owner_fact_kind` 的各条路径成本。

为什么要交替:机器负载会漂移,**跨组数字不可相除**。本脚本每轮先跑 A 再跑 B、多轮取中位,
因此每一行只在同一轮组内可比。R273 记录过两组独立采样(定位回退组 / 改造后复测组),
两组各自成立,不能把一组的数字除以另一组的数字。

用法:
  python3 scripts/bench/paired_owner_fact_kind.py \
      --repo-a /path/to/before --repo-b /path/to/after --case cheap --rounds 5

--case 取值:
  cheap  安静 owner(FORCE_CHEAP=1,强制走 cheap 分支)
  evict  每轮清缓存(现读 + 写缓存)
  bind   每轮清缓存且门槛为 0(完整快照绑定)
  hit    有硬事实且不变(缓存命中)
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CASES = {
    "cheap": [("empty", {"FORCE_CHEAP": "1"})],
    "evict": [("evict", {})],
    "bind": [("bind", {"FORCE_BIND": "1"})],
    "hit": [("hot", {})],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-a", required=True)
    parser.add_argument("--repo-b", required=True)
    parser.add_argument("--case", default="cheap", choices=sorted(CASES))
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--samples", type=int, default=60, help="子进程内每次调用的计时轮数")
    return parser.parse_args()


ARGS = parse_args()


def run(repo: str, scenario: str, env_extra: dict[str, str]) -> dict:
    raw = subprocess.run(
        [
            sys.executable,
            str(HERE / "bench_owner_fact_kind.py"),
            "--repo",
            repo,
            "--scenario",
            scenario,
            "--rounds",
            str(ARGS.samples),
        ],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **env_extra},
    ).stdout.strip().splitlines()[-1]
    return json.loads(raw)


def main() -> int:
    trees = {"A": ARGS.repo_a, "B": ARGS.repo_b}
    for scenario, env_extra in CASES[ARGS.case]:
        samples: dict[str, list[float]] = {name: [] for name in trees}
        stats: dict[str, dict] = {}
        for _ in range(ARGS.rounds):
            for name, repo in trees.items():
                payload = run(repo, scenario, env_extra)
                samples[name].append(payload["median_ms"])
                stats[name] = payload["stats"]
        a = statistics.median(samples["A"])
        b = statistics.median(samples["B"])
        print(
            f"case={ARGS.case} scenario={scenario} rounds={ARGS.rounds} samples={ARGS.samples}\n"
            f"  A={trees['A']}\n    median={a:.4f}ms stats={stats['A']}\n"
            f"  B={trees['B']}\n    median={b:.4f}ms stats={stats['B']}\n"
            f"  组内比值 B/A={b / a:.2f}x（仅本组内可比;跨组数字不可相除）"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
