#!/usr/bin/env python3
"""A 项配对基准:owner 事实判定缓存 `_owner_fact_kind` 的每调用成本(三类路径)。

三类(对应 GW-03 基准里的同类形态):
  empty/hot : 空 owner home 或文件不变(应当走便宜判定 / 缓存命中)
  evict     : 每轮清缓存(强制走"签名不匹配或无条目 → 现读 + 写缓存")
  bind      : 每轮清缓存且把"值得缓存"的门槛推到 0(强制每次走完整快照绑定)
环境变量:
  FORCE_CHEAP=1  把 _FACT_MIN_CACHED_SECONDS 推到极大 → 每次都应落 cheap 分支(安静 owner 的状态)
  FORCE_BIND=1   把该阈值压到 0 → 每次都值得缓存

用法: python3 scripts/bench/bench_owner_fact_kind.py --scenario empty --rounds 60
夹具:临时目录里造一个 owner home 骨架(固定目录名与占位 JSON),不含真实会话内容或密钥。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_REPO = Path(__file__).resolve().parents[2]
SCENARIOS = ("empty", "hot", "evict", "bind", "miss")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(DEFAULT_REPO), help="被测 checkout 根目录")
    parser.add_argument("--scenario", default="empty", choices=SCENARIOS)
    parser.add_argument("--rounds", type=int, default=60)
    parser.add_argument("--fixture-root", default="", help="夹具根目录(默认系统临时目录)")
    return parser.parse_args()


ARGS = parse_args()
REPO = Path(ARGS.repo).resolve()
sys.path.insert(0, str(REPO))

import agent_py_agent.agent.owner_wake_discovery as discovery  # noqa: E402

ROOT = Path(ARGS.fixture_root).expanduser() if ARGS.fixture_root else Path(
    tempfile.mkdtemp(prefix="bench-fact-kind-")
)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def build(scenario: str) -> Path:
    home = ROOT / scenario / "providers" / "local" / "users" / "u1"
    if scenario == "empty":
        home.mkdir(parents=True, exist_ok=True)
        return home
    store_root = home / "workspace" / "runtime" / "workspaces" / "slug-x" / "conversations"
    for directory in (
        store_root / "progress_policies",
        store_root / "wake_queue" / "urgent",
        store_root / "wake_queue" / "normal",
        store_root / "tasks",
        home / "agents",
        home / "tasks",
        home / "memory",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    _write_json(
        home / "memory" / "curator" / "state.json",
        {
            "last_success_at": "2024-01-01T00:00:00+00:00",
            "last_daily_finalize_date": "2024-01-01",
            "pending_reasons": [],
            "active_lease": None,
        },
    )
    (home / "memory" / "candidates.jsonl").write_text("", encoding="utf-8")
    if scenario != "empty":
        _write_json(
            store_root / "wake_queue" / "normal" / "sig-1.json",
            {"status": "pending", "created_at": 1.0},
        )
    return home


def main() -> int:
    home = build(ARGS.scenario)
    if os.environ.get("FORCE_BIND") == "1":
        discovery._FACT_MIN_CACHED_SECONDS = 0.0
    if os.environ.get("FORCE_CHEAP") == "1":
        discovery._FACT_MIN_CACHED_SECONDS = 1e9
    discovery._OWNER_FACT_CACHE.clear()
    wake = (
        home
        / "workspace"
        / "runtime"
        / "workspaces"
        / "slug-x"
        / "conversations"
        / "wake_queue"
        / "normal"
        / "sig-1.json"
    )

    def one_call(index: int) -> None:
        if ARGS.scenario in {"evict", "bind"}:
            discovery._OWNER_FACT_CACHE.clear()
        if ARGS.scenario == "miss" and wake.exists():
            wake.write_text(
                json.dumps({"status": "pending", "created_at": float(index)}), encoding="utf-8"
            )
        discovery._owner_fact_kind(home)

    for _ in range(5):
        one_call(0)
    samples: list[float] = []
    for index in range(1, ARGS.rounds + 1):
        started = time.perf_counter()
        one_call(index)
        samples.append((time.perf_counter() - started) * 1000.0)
    print(
        json.dumps(
            {
                "repo": str(REPO),
                "scenario": ARGS.scenario,
                "median_ms": round(statistics.median(samples), 4),
                "p90_ms": round(sorted(samples)[int(len(samples) * 0.9) - 1], 4),
                "rounds": ARGS.rounds,
                "stats": dict(discovery._OWNER_FACT_STATS),
            }
        )
    )
    shutil.rmtree(ROOT, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
