#!/usr/bin/env python3
"""摄取召回三件套离线台:多结构数据上复现"筛"的盲区,对比基线车道 vs 抽检+反馈+倾斜。

真机背景(交接文档 Part B):3h、891 真目标,预筛只抬 26% 给模型、一个源 0/177 全瞎;
判这环 0 误报——漏在筛不在判。本台用【真引擎 + 真收件箱往返】(new_state→process→
append_confirmation→consume_feedback_inbox,全走生产代码),只把"模型判定"换成 oracle
(生产里判定归模型;台子度量的是筛的召回力学,不是判定)。

三种结构(通用非专项,字段名/取值只是数据不是判据):
  A 枚举结果端:目标取值频次涨过少数派阈值后,通用车道盲(共享结论词第 4 条起全漏)。
  B 文本结果端:结论词+高基数尾巴,目标份额超过首记号相对闸后全漏。
  C 频次盲区源(0% 源形态):目标从第一拍起就高频(≈8%),稀有度类车道从未开张,
    基线 0%;只能靠抽检撞见→确认→反馈抬同类救活。

用法:python3 scripts/offline_recall_bench.py [--out DIR]
输出:每场景 baseline(三件套关)/assisted(三件套开)的召回与账目,JSON 落盘。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent_py_agent"))

from agent_py_agent.agent.ingestion.watch_feedback import (  # noqa: E402
    append_confirmation,
    consume_feedback_inbox,
)
from agent_py_agent.agent.ingestion.watch_state import new_state, persist_state  # noqa: E402

_BASE = 1_000_000.0  # 合成时间轴起点(秒),只喂引擎滑窗,不取真实时钟


def _scenario_enum(step: int, index: int, is_target: bool) -> dict:
    status = "diverted" if is_target else ("ok", "done", "accepted")[index % 3]
    return {"kind": "login", "status": status, "user": f"u{step}-{index}", "n": index}


def _scenario_text(step: int, index: int, is_target: bool) -> dict:
    head = "diverted" if is_target else ("accepted", "processed")[index % 2]
    return {"kind": "txn", "result": f"{head} ref={step:04d}{index:04d} t={index}", "actor": f"a{step}-{index}"}


def _scenario_dense(step: int, index: int, is_target: bool) -> dict:
    # 目标从第一拍起就高频(每拍 8/100):少数派/首记号闸从未见过"稀有"的它。
    outcome = "rerouted" if is_target else "settled"
    return {"kind": "pay", "outcome": outcome, "trace": f"t{step}-{index}", "amt": index % 7}


SCENARIOS = {
    "A_enum_flood": {"make": _scenario_enum, "field": "status", "per_step": 100, "targets_per_step": 5, "steps": 24},
    "B_text_flood": {"make": _scenario_text, "field": "result", "per_step": 100, "targets_per_step": 5, "steps": 24},
    "C_dense_blind": {"make": _scenario_dense, "field": "outcome", "per_step": 100, "targets_per_step": 8, "steps": 24},
}


def _make_batch(shape: tuple, step: int, seq: int, target_pos: set) -> tuple[list, int]:
    """造一拍事件(前 2 拍纯常态预热,之后每拍前 per_targets 条是目标),登记目标坐标。"""
    make, per_step, per_targets = shape
    batch: list[tuple[int, dict]] = []
    for index in range(per_step):
        is_target = index < per_targets and step >= 2
        batch.append((seq, make(step, index, is_target)))
        if is_target:
            target_pos.add(seq)
        seq += 1
    return batch, seq


def _oracle_confirm_targets(digest, place: tuple, target_pos: set, books: tuple) -> None:
    """oracle=生产里的模型重判:候选里的真目标记浮出,首次浮出即确认(走真收件箱往返)。"""
    state, home = place
    surfaced, confirmed, assisted = books
    for candidate in digest.candidates:
        if candidate.seq_hint not in target_pos or candidate.seq_hint in surfaced:
            continue
        surfaced[candidate.seq_hint] = candidate.reason
        if assisted and candidate.seq_hint not in confirmed:
            append_confirmation(home, state.watch_id, candidate.seq_hint)
            confirmed.add(candidate.seq_hint)


def _run_scenario(name: str, spec: dict, home: Path, *, assisted: bool) -> dict:
    params: dict[str, object] = {"value_min_support": 32}
    if not assisted:
        params.update({"audit_sample_per_pull": 0, "feedback_max_candidates_per_pull": 0})
    state = new_state(home, f"http://bench.local/{name}/{'on' if assisted else 'off'}", params)
    persist_state(state)
    make, per_step, per_targets = spec["make"], spec["per_step"], spec["targets_per_step"]
    seq = 0
    target_pos: set[int] = set()
    surfaced: dict[int, str] = {}
    confirmed: set[int] = set()
    for step in range(spec["steps"]):
        # 拍距 30s、24 拍 ≈ 12 分钟:300s 滑窗始终被连续流占满(0% 盲区源的真实形态——
        # 窗口一旦稀疏,每窗前几条又会"显得稀有"漏进基线车道,失真);时长跨多个窗,
        # 反馈车道的每特征每窗洪泛闸按窗放行、长跑不截流。
        now = _BASE + step * 30.0
        batch, seq = _make_batch((make, per_step, per_targets), step, seq, target_pos)
        digest = state.engine.process(batch, now)
        _oracle_confirm_targets(digest, (state, home), target_pos, (surfaced, confirmed, assisted))
        if assisted:
            consume_feedback_inbox(state, now + 1.0)
    by_reason: dict[str, int] = {}
    for reason in surfaced.values():
        by_reason[reason] = by_reason.get(reason, 0) + 1
    totals = state.engine.totals
    return {
        "targets_total": len(target_pos),
        "targets_surfaced": len(surfaced),
        "recall_pct": round(100.0 * len(surfaced) / max(1, len(target_pos)), 1),
        "surfaced_by_reason": dict(sorted(by_reason.items())),
        "engine_totals": {k: totals[k] for k in ("events_seen", "escalated", "audit_sampled", "audit_confirmed", "escalated_feedback", "feedback_ring_miss")},
        "learned_features": len(state.engine.feedback.learned),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    out_dir = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="recall_bench_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, dict] = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    for name, spec in SCENARIOS.items():
        home = out_dir / name
        home.mkdir(parents=True, exist_ok=True)
        baseline = _run_scenario(name, spec, home, assisted=False)
        assisted = _run_scenario(name, spec, home, assisted=True)
        report[name] = {"baseline": baseline, "assisted": assisted}
        totals = assisted["engine_totals"]
        waste = totals["escalated_feedback"] - assisted["surfaced_by_reason"].get("confirmed_target_similar", 0)
        print(f"[{name}] baseline {baseline['targets_surfaced']}/{baseline['targets_total']}"
              f" ({baseline['recall_pct']}%) → assisted {assisted['targets_surfaced']}/{assisted['targets_total']}"
              f" ({assisted['recall_pct']}%)  reasons={assisted['surfaced_by_reason']}"
              f" audit={totals['audit_sampled']}/{totals['audit_confirmed']}"
              f" feedback_waste={waste} ring_miss={totals['feedback_ring_miss']}")
    report_path = out_dir / "recall_bench_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
