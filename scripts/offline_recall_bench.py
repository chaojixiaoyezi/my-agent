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
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent_py_agent"))

from agent_py_agent.agent.ingestion.harvester import (  # noqa: E402
    _spool_append,
    judge_headroom,
    read_spool_records,
)
from agent_py_agent.agent.ingestion.watch_feedback import (  # noqa: E402
    append_confirmation,
    consume_feedback_inbox,
)
from agent_py_agent.agent.ingestion.watch_payloads import order_candidate_rows  # noqa: E402
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


# ──────────────────────────── 判读吞吐台(B 回炉:真机洪泛净负的离线复现) ────────────────────────────
# 上一版离线台只量"筛"(oracle 零成本秒判全部候选),没建模主代理判读吞吐——真机上
# audit 抽检把有限判力淹没(funnel A 涨、funnel B 156→18-29 崩)这里测不到。本台补上:
# 候选走【真 spool 往返】(harvester._spool_append → read_spool_records 推进真读游标),
# 消费者每拍只判得动 judge_per_step 行(FIFO 批内按生产同款车道排序);funnel B=判到
# 并确认的真目标。对比 backpressure 关(旧行为,洪泛应复现)/开(judge_headroom 反压)。

_TP = {
    "per_step": 150, "targets_per_step": 3, "steps": 30, "judge_per_step": 4,
    "params": {
        "value_min_support": 32, "low_cardinality_limit": 8,
        "audit_sample_per_pull": 6, "audit_sample_per_minute": 600, "audit_tilt_per_minute": 1200,
    },
}


def _refill_pending(state, books: dict) -> bool:
    """pending 空了就从 spool 拉下一批(真读游标),批内按生产同款车道排序;无批可拉返回 False。"""
    records, _backlog = read_spool_records(state, max_candidates=1)
    rows = [row for record in records for row in (record.get("candidates") or [])]
    if not rows:
        return False
    books["pending"].extend(order_candidate_rows(rows))
    return True


def _throughput_consume(state, place: tuple, books: dict) -> None:
    """一拍的消费(严格判读信用制):每拍 judge_per_step 个信用,判一行花一个;
    当前批(pending)没判完不拉新批——大批=长turn,跨拍慢慢嚼。
    这才是真机的形态:判力不随批量白涨,洪泛批会吃掉后续几拍的全部判力。"""
    home, target_pos = place
    for _credit in range(_TP["judge_per_step"]):
        if not books["pending"] and not _refill_pending(state, books):
            return
        row = books["pending"].pop(0)
        books["judged"] += 1
        pos = int(row.get("stream_pos") or -1)
        if pos in target_pos and pos not in books["confirmed"]:
            books["confirmed"].add(pos)
            append_confirmation(home, state.watch_id, pos)


def _run_throughput_variant(home: Path, *, backpressure: bool) -> dict:
    state = new_state(home, f"http://bench.local/throughput/{'bp' if backpressure else 'flood'}", dict(_TP["params"]))
    persist_state(state)
    seq, target_pos, surfaced = 0, set(), set()
    books: dict = {"judged": 0, "confirmed": set(), "pending": []}
    shape = (_scenario_enum, _TP["per_step"], _TP["targets_per_step"])
    for step in range(_TP["steps"]):
        now = _BASE + step * 30.0
        consume_feedback_inbox(state, now)
        batch, seq = _make_batch(shape, step, seq, target_pos)
        digest = state.engine.process(batch, now, judge_headroom=judge_headroom(state) if backpressure else None)
        if digest.candidates:
            surfaced.update(c.seq_hint for c in digest.candidates if c.seq_hint in target_pos)
            _spool_append(state, SimpleNamespace(cursor=seq), digest)
        _throughput_consume(state, (home, target_pos), books)
    totals = state.engine.totals
    return {
        "targets_total": len(target_pos),
        "funnel_a_surfaced": len(surfaced),
        "funnel_b_confirmed": len(books["confirmed"]),
        "judged_rows": books["judged"],
        # 未判积压 = 已抬进 spool(state.totals 账) - 已判(含已拉出还没判完的 pending,不双算)。
        "spool_backlog_end": max(0, int(state.totals.get("spool_candidates", 0)) - books["judged"]),
        "audit_sampled": totals["audit_sampled"],
        "audit_throttled": totals["audit_throttled"],
        "escalated_feedback": totals["escalated_feedback"],
    }


def _run_throughput_bench(out_dir: Path) -> dict:
    flood_home = out_dir / "T_judge_flood"
    bp_home = out_dir / "T_judge_bp"
    flood_home.mkdir(parents=True, exist_ok=True)
    bp_home.mkdir(parents=True, exist_ok=True)
    flood = _run_throughput_variant(flood_home, backpressure=False)
    bp = _run_throughput_variant(bp_home, backpressure=True)
    for tag, row in (("flood(反压关=旧行为)", flood), ("bp(反压开)", bp)):
        print(f"[T_throughput/{tag}] funnelB {row['funnel_b_confirmed']}/{row['targets_total']}"
              f" funnelA {row['funnel_a_surfaced']} judged={row['judged_rows']}"
              f" backlog_end={row['spool_backlog_end']} audit={row['audit_sampled']}"
              f" throttled={row['audit_throttled']} feedback={row['escalated_feedback']}")
    return {"flood": flood, "backpressure": bp}


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
    report["T_throughput"] = _run_throughput_bench(out_dir)
    report_path = out_dir / "recall_bench_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
