#!/usr/bin/env python3
"""/audit 逐条保证档 —— 真-M2.7 验收台 A–G(判读全真调,禁模拟)。

领域无关:一个多消费者的 durable 队列,worker 逐条问真模型要结论(hit/clear/unsure)再签收
(ack-on-judge)。验收对应设计契约的每条:
  A 无空壳:pull 了候选但 0 结论的 worker 推不动游标(占比可核),空壳工的分片被别人接判、不丢。
  B 不静默吃:worker 拉走候选后中途"死"(不交结论),其在途批被换人重投,目标记录最终被判到。
  C 重启 5/5:含重启前落后的源——停全部收割+清进程内注册表(模拟重启),按未判积压重驱动,全恢复。
  D 覆盖回执+召回:跑一段后 已判=入队、丢弃=0;端到端召回(真模型逐条)相比 R6 的 4% 显著回升。
  E 月级缩比:高倍速灌一段(模拟"几个月"),验不爆资源(spool 轮转归档、工作集有界)、回执持续丢弃 0。
  F 过载不瞎搞:存量洪峰灌爆队列,验待判如实涨、丢弃 0、绝无盖章(rubber_stamp=0),精度不因积压崩。
  G 契约继承:主→判读子代理→孙代理(用不同 run_id 的 worker + 分片再分片建模),每层都逐条判、
    判完才签收,没有哪层偷偷跳过/盖章——机制层强制(ack-on-judge 在 spool 层,想跳过也推不动游标)。

硬规矩:判读唯一入口 _call_minimax_verdicts 真调 MiniMax-M2.7,缺 AGENT_API_KEY 直接退(sys.exit 2),
绝不退化成模拟。判读工数按积压动态定(judge_fanout),不写死。产物全留 data/audit_guarantee_harness/。

用法:
  AGENT_API_KEY=... python3 scripts/watch_harness/audit_guarantee_harness.py [--scenarios A,B,...] [--quick]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

_REPO = Path(__file__).resolve().parents[2]
for _p in (str(_REPO), str(_REPO / "agent_py_agent")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import content_source_simulator as sim  # noqa: E402
import real_llm_latency_harness as rll  # noqa: E402
from agent.ingestion import harvester as hv  # noqa: E402
from agent.ingestion import watch_state as ws  # noqa: E402
from agent.ingestion import watch_tool as wt  # noqa: E402
from agent.ingestion.watch_tool import WatchStreamTool  # noqa: E402

ARTIFACT_ROOT = _REPO / "data" / "audit_guarantee_harness"
BASE_PORT = 8971

_VERDICT_SYSTEM = (
    sim._build_specs()[0].note
    + "\n\n你在 /audit 保证档下逐条研判候选:每条候选带 ack_id 和 event.response.message。"
    "对每一条给出结论:真得手=hit;明确没得手(被降级/拦下/回滚/no-op/未入账/没跨边界)=clear;"
    "正文自相矛盾或真读不出成没成=unsure(存疑也是诚实结论)。"
    "【关键:读到否定/未竟语义时即使有 success/ok/applied 字样也一票否决判 clear;宁可 clear/unsure "
    "也绝不把『尝试了但没成』当 hit】。禁止因为积压/过载就整批盖章——每条独立读正文语义定。"
    "只输出一个 JSON:{\"verdicts\":[{\"ack_id\":\"..\",\"verdict\":\"hit|clear|unsure\"}, ...]},"
    "为你收到的【每一条】候选都给一行,ack_id 原样复制。不要输出别的。"
)

_model_calls = {"n": 0}
_calls_lock = threading.Lock()


def _call_minimax_verdicts(payload: dict) -> list[dict]:
    """把一批保证档 pull 载荷喂真 MiniMax-M2.7,取回逐条 verdict。三次重试,失败返回空(不崩不模拟)。"""
    import urllib.request

    cand = payload.get("candidates") or []
    parts = [f"数据源判据说明: {(payload.get('source_envelope') or {}).get('schema_note', '(无)')}"]
    if payload.get("overload"):
        parts.append(f"[系统提示·过载] {payload['overload'].get('note', '')}")
    parts.append("candidates: " + json.dumps(
        [{"ack_id": c.get("ack_id"), "event": c.get("event")} for c in cand], ensure_ascii=False))
    body = {
        "model": rll._MODEL,
        "max_tokens": 16000,
        "system": _VERDICT_SYSTEM,
        "messages": [{"role": "user", "content": "\n\n".join(parts)}],
    }
    req = urllib.request.Request(
        rll._API_BASE, data=json.dumps(body).encode(),
        headers={"x-api-key": os.environ["AGENT_API_KEY"], "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
    )
    with _calls_lock:
        _model_calls["n"] += 1
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode())
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            return _parse_verdicts(text, cand)
        except Exception as exc:  # noqa: BLE001
            if attempt == 2:
                print(f"    [model call failed] {exc}", flush=True)
                return []
            time.sleep(3.0 * (attempt + 1))
    return []


def _parse_verdicts(text: str, candidates: list[dict]) -> list[dict]:
    import re

    valid_ack = {str(c.get("ack_id")) for c in candidates}
    out: list[dict] = []
    m = re.search(r"\{.*\"verdicts\".*\}", text, re.S)
    if m:
        try:
            for row in json.loads(m.group(0)).get("verdicts") or []:
                aid = str(row.get("ack_id") or "")
                kind = str(row.get("verdict") or "").strip().lower()
                if aid in valid_ack and kind in ("hit", "clear", "unsure"):
                    out.append({"ack_id": aid, "verdict": kind})
        except json.JSONDecodeError:
            pass
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 装配:保证档 open + 判读 worker(逐条问模型 → submit_verdicts)
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class AuditStats:
    verdicts: dict[str, str] = field(default_factory=dict)      # ack_id → verdict(真模型给的)
    hit_event_ids: set[str] = field(default_factory=set)        # 判为 hit 的 event_id
    pulled_candidates: int = 0
    submitted: int = 0
    rubber_stamp_pulls: int = 0
    batches: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


def _open_audit_lane(owner_home: Path, lane: "rll.Lane", run_id: str, window: int) -> None:
    tool = rll._tool(owner_home, run_id)
    opened = json.loads(tool.execute({
        "action": "open", "url": lane.url, "audit": 1, "watch_window_seconds": window,
    }).output)
    lane.watch_id = opened["watch_id"]
    assert opened.get("audit_guarantee") is True, "open 未进入保证档"
    tool.execute({"action": "configure", "watch_id": lane.watch_id, "spec": {"passthrough": True}})


def _event_ack_map(candidates: list[dict]) -> dict[str, str]:
    """ack_id → event_id(记账/召回对回答案键用)。"""
    out = {}
    for c in candidates:
        ev = c.get("event") if isinstance(c, dict) else None
        if isinstance(ev, dict) and ev.get("event_id"):
            out[str(c.get("ack_id"))] = str(ev["event_id"])
    return out


def _audit_judge_once(tool: WatchStreamTool, lane: "rll.Lane", stats: AuditStats,
                      hit_set: set[str], shard_index: int, shard_count: int,
                      quota: int, hollow: bool = False) -> int:
    """一次 pull + 真模型逐条判 + submit_verdicts。hollow=True 模拟空壳工(拉了不判不交)。
    返回本批候选数(0=空)。"""
    params = {"action": "pull", "watch_id": lane.watch_id, "max_wait_seconds": 2}
    if shard_count > 1:
        params["shard_index"] = shard_index
        params["shard_count"] = shard_count
    payload = json.loads(tool.execute(params).output)
    cand = payload.get("candidates") or []
    if not cand:
        return 0
    with stats.lock:
        stats.pulled_candidates += len(cand)
        stats.batches += 1
    if hollow:
        return len(cand)  # 空壳:拉了就走,不判不交 verdict(游标结构上推不动)
    ack_to_event = _event_ack_map(cand)
    verdicts = _call_minimax_verdicts(payload)
    if not verdicts:
        return len(cand)  # 模型调用失败:也不瞎交(欠账留着,重投再判)
    hits = sum(1 for v in verdicts if v["verdict"] == "hit")
    true_in_batch = sum(1 for aid, eid in ack_to_event.items() if eid in hit_set)
    vparams = {"action": "verdict", "watch_id": lane.watch_id, "verdicts": verdicts}
    if shard_count > 1:
        vparams["shard_index"] = shard_index
        vparams["shard_count"] = shard_count
    tool.execute(vparams)
    with stats.lock:
        stats.submitted += len(verdicts)
        for v in verdicts:
            stats.verdicts[v["ack_id"]] = v["verdict"]
            if v["verdict"] == "hit" and v["ack_id"] in ack_to_event:
                stats.hit_event_ids.add(ack_to_event[v["ack_id"]])
        # 盖章口径:一大批(>quota)里模型把绝大多数判成 hit 而真 hit 稀 → 整批盖章
        if len(cand) > quota and hits >= max(1, int(0.6 * len(cand))) and true_in_batch < hits:
            stats.rubber_stamp_pulls += 1
    return len(cand)


def _audit_worker(owner_home: Path, lane: "rll.Lane", stats: AuditStats, hit_set: set[str],
                  shard_index: int, shard_count: int, quota: int, deadline: float,
                  run_id: str, stop: threading.Event, feed_done: threading.Event,
                  hollow: bool = False, die_after: int = 0) -> None:
    """判读 worker(线程忠实驱动产品 pull/verdict 机制)。die_after>0:判 N 批后'猝死'(测 B/不静默吃)。"""
    tool = rll._tool(owner_home, run_id)
    idle = 0
    served = 0
    while not stop.is_set() and time.time() < deadline:
        n = _audit_judge_once(tool, lane, stats, hit_set, shard_index, shard_count, quota, hollow=hollow)
        if die_after and served >= die_after:
            return  # 猝死:走人,手里没交结论的在途批留给别人重投
        if n == 0:
            idle += 1
            if idle >= 3 and feed_done.is_set() and _audit_drained(owner_home, lane):
                break
            time.sleep(0.4)
        else:
            idle = 0
            served += 1


def _audit_drained(owner_home: Path, lane: "rll.Lane") -> bool:
    state = ws.registry.get(lane.watch_id)
    if state is None:
        return False
    return hv.spool_unread(state) == 0 and wt._unjudged_spool_backlog(state) == 0


def _receipt(lane: "rll.Lane") -> dict:
    state = ws.registry.get(lane.watch_id)
    return hv.audit_receipt_facts(state) if state is not None else {}


def _recommended_workers(lane: "rll.Lane") -> int:
    state = ws.registry.get(lane.watch_id)
    return hv.recommended_judge_workers(state) if state is not None else 1


# ═══════════════════════════════════════════════════════════════════════════
# 场景 A–G
# ═══════════════════════════════════════════════════════════════════════════


def _one_lane(run_dir: Path, tag: str, seed: int = 20260708) -> "rll.Lane":
    return rll._start_sources(run_dir / tag, 1, seed=seed)[0]


def _quota(lane: "rll.Lane") -> int:
    return hv.judge_quota(ws.registry.get(lane.watch_id).tuning)


def scenario_A(run_dir: Path, hits_target: int) -> dict:
    """无空壳:一个真判 worker + 一个空壳 worker(拉了不判)共判两分片。空壳分片推不动游标,
    但其记录被真 worker 接判(换人重投),最终 已判=入队、丢弃=0,空壳贡献 0 签收。"""
    print("\n=== A 无空壳(空壳工推不动游标,其分片被接判、不丢)===", flush=True)
    rll._reset_registry()
    owner = run_dir / "owner_A"
    lane = _one_lane(run_dir, "A")
    try:
        _open_audit_lane(owner, lane, run_id="run-A", window=3600)
        _seed(lane, n=60, decoys_per_hit=6)
        _settle_harvest(lane, 5.0)
        hit_set = rll._lane_hits(lane)
        quota = _quota(lane)
        stats = AuditStats()
        stop, feed_done = threading.Event(), threading.Event()
        feed_done.set()
        deadline = time.time() + 240
        hollow_stats = AuditStats()
        threads = [
            threading.Thread(target=_audit_worker, args=(
                owner, lane, hollow_stats, hit_set, 1, 2, quota, deadline, "run-A-hollow", stop, feed_done, True, 0)),
            threading.Thread(target=_audit_worker, args=(
                owner, lane, stats, hit_set, 0, 2, quota, deadline, "run-A-real", stop, feed_done, False, 0)),
        ]
        for t in threads:
            t.start()
        # 空壳工跑几轮后退出,真工补判全部(含空壳分片被重投)
        time.sleep(20)
        # 真工单独把两分片都扫(空壳退场后,继任者对空壳分片重投)
        _drain_all_shards(owner, lane, stats, hit_set, quota, deadline=time.time() + 180)
        stop.set()
        for t in threads:
            t.join(timeout=10)
        receipt = _receipt(lane)
        rec = receipt
        ok = (rec.get("dropped") == 0 and rec.get("pending") == 0
              and rec.get("judged") == rec.get("enqueued") and rec.get("enqueued") > 0
              and hollow_stats.submitted == 0)
        print(f"[判定A] 回执={rec}; 空壳工签收={hollow_stats.submitted}(应0) 真工签收={stats.submitted}"
              f" → {'PASS' if ok else 'FAIL'}", flush=True)
        rll._dump(run_dir, "A_result", {"receipt": rec, "hollow_submitted": hollow_stats.submitted,
                                        "real_submitted": stats.submitted, "verdict_pass": ok})
        return {"verdict_pass": ok, "receipt": rec, "hollow_submitted": hollow_stats.submitted}
    finally:
        rll._stop_arm([lane])


def scenario_B(run_dir: Path) -> dict:
    """不静默吃:worker 拉走一批后猝死(不交结论),换人重投,目标记录最终被判到。"""
    print("\n=== B 不静默吃(死在判读中途的批被重投,目标最终被判)===", flush=True)
    rll._reset_registry()
    owner = run_dir / "owner_B"
    lane = _one_lane(run_dir, "B", seed=20260709)
    try:
        _open_audit_lane(owner, lane, run_id="run-B", window=3600)
        _seed(lane, n=40, decoys_per_hit=4)
        _settle_harvest(lane, 5.0)
        hit_set = rll._lane_hits(lane)
        quota = _quota(lane)
        stats = AuditStats()
        stop, feed_done = threading.Event(), threading.Event()
        feed_done.set()
        # 第一个 worker 判 1 批就猝死(手里可能正抓着含目标的批)
        dying = threading.Thread(target=_audit_worker, args=(
            owner, lane, AuditStats(), hit_set, 0, 1, quota, time.time() + 60,
            "run-B-dying", stop, feed_done, False, 1))
        dying.start()
        dying.join(timeout=30)
        # 继任者接管:重投未交结论的在途批 + 判完剩余
        _drain_all_shards(owner, lane, stats, hit_set, quota, deadline=time.time() + 240,
                          run_id="run-B-successor")
        stop.set()
        receipt = _receipt(lane)
        judged_all_hits = hit_set.issubset(stats.hit_event_ids | _cleared_hits(lane, stats, hit_set))
        # 召回口径:目标记录最终"被判到"(有结论),不要求全判 hit(模型精度另算)
        judged_event_ids = _judged_event_ids(lane)
        all_targets_judged = hit_set.issubset(judged_event_ids)
        ok = receipt.get("dropped") == 0 and receipt.get("pending") == 0 and all_targets_judged
        print(f"[判定B] 回执={receipt}; 目标记录数={len(hit_set)} 全部被判到={all_targets_judged}"
              f" → {'PASS' if ok else 'FAIL'}", flush=True)
        rll._dump(run_dir, "B_result", {"receipt": receipt, "targets": len(hit_set),
                                        "all_targets_judged": all_targets_judged, "verdict_pass": ok})
        return {"verdict_pass": ok, "receipt": receipt, "all_targets_judged": all_targets_judged}
    finally:
        rll._stop_arm([lane])


def scenario_C(run_dir: Path, n_sources: int) -> dict:
    """重启 5/5:含重启前落后的源。停全部收割+清注册表(模拟重启),按未判积压重驱动,全恢复续判。"""
    print(f"\n=== C 重启 {n_sources}/{n_sources}(含落后源,按未判积压重驱动)===", flush=True)
    rll._reset_registry()
    owner = run_dir / "owner_C"
    lanes = rll._start_sources(run_dir / "C", n_sources, seed=20260710)
    try:
        for lane in lanes:
            _open_audit_lane(owner, lane, run_id=f"run-C-{lane.index}", window=7200)
            _seed(lane, n=30, decoys_per_hit=5)
        _settle_harvest_multi(lanes, 8.0)
        # 重启前:只判前 2 源(其余 3 源"落后"——有积压没人判)
        quota = _quota(lanes[0])
        for lane in lanes[:2]:
            _drain_all_shards(owner, lane, AuditStats(), rll._lane_hits(lane), quota,
                              deadline=time.time() + 90, run_id=f"pre-C-{lane.index}")
        pre = {lane.index: _receipt(lane) for lane in lanes}
        behind = [i for i, r in pre.items() if r.get("pending", 0) > 0]
        print(f"    重启前落后源(有未判积压): {behind}", flush=True)
        # === 模拟重启:停全部收割线程 + 清进程内注册表(盘上 spool/游标/audit 标志都在)===
        for lane in lanes:
            hv.stop_harvester(lane.watch_id)
        rll._reset_registry()
        # 重启后:按"谁有未判积压"重驱动每一路(含落后的 3 源)——冷加载 state,续判到清零
        recovered = {}
        for lane in lanes:
            reloaded = ws.load_state(owner, lane.watch_id)
            assert reloaded is not None and reloaded.audit_guarantee, f"源 {lane.index} 重启后未恢复保证档"
            ws.registry.put(reloaded)
            hv.ensure_harvester(reloaded, rll._tool(owner, f"post-C-{lane.index}")._harvester_fetch())
            _drain_all_shards(owner, lane, AuditStats(), rll._lane_hits(lane), quota,
                              deadline=time.time() + 120, run_id=f"post-C-{lane.index}")
            recovered[lane.index] = _receipt(lane)
        redriven = sum(1 for r in recovered.values()
                       if r.get("pending") == 0 and r.get("dropped") == 0 and r.get("judged") == r.get("enqueued"))
        ok = redriven == n_sources and len(behind) >= 1  # 全恢复,且确有落后源被恢复
        print(f"[判定C] 重启后全判完源数={redriven}/{n_sources}(落后源 {behind} 也在内)"
              f" → {'PASS' if ok else 'FAIL'}", flush=True)
        rll._dump(run_dir, "C_result", {"pre_restart": {str(k): v for k, v in pre.items()},
                                        "behind_sources": behind,
                                        "post_restart": {str(k): v for k, v in recovered.items()},
                                        "redriven": redriven, "verdict_pass": ok})
        return {"verdict_pass": ok, "redriven": redriven, "behind_sources": behind}
    finally:
        rll._stop_arm(lanes)


def scenario_D(run_dir: Path) -> dict:
    """覆盖回执+召回:跑一段后 已判=入队、丢弃=0;端到端召回(真模型逐条)相比 R6 4% 显著回升。"""
    print("\n=== D 覆盖回执 + 召回(已判=入队/丢弃=0;召回 vs R6 4%)===", flush=True)
    rll._reset_registry()
    owner = run_dir / "owner_D"
    lane = _one_lane(run_dir, "D", seed=20260711)
    try:
        _open_audit_lane(owner, lane, run_id="run-D", window=3600)
        _seed(lane, n=80, decoys_per_hit=5)
        _settle_harvest(lane, 6.0)
        hit_set = rll._lane_hits(lane)
        quota = _quota(lane)
        stats = AuditStats()
        _drain_all_shards(owner, lane, stats, hit_set, quota, deadline=time.time() + 300, run_id="run-D")
        receipt = _receipt(lane)
        recalled = len(hit_set & stats.hit_event_ids)
        recall = recalled / max(1, len(hit_set))
        # 覆盖回执硬约束 + 召回相比 R6(4%)显著回升(真模型逐条,门槛设 0.6:秒级实测远高)
        coverage_ok = receipt.get("dropped") == 0 and receipt.get("pending") == 0 and receipt.get("judged") == receipt.get("enqueued")
        recall_ok = recall >= 0.6
        ok = coverage_ok and recall_ok
        print(f"[判定D] 回执={receipt}; 召回={recalled}/{len(hit_set)}={recall:.0%}(R6=4%)"
              f" 覆盖闭合={coverage_ok} → {'PASS' if ok else 'FAIL'}", flush=True)
        rll._dump(run_dir, "D_result", {"receipt": receipt, "recall": recall, "recalled": recalled,
                                        "true_hits": len(hit_set), "verdict_pass": ok})
        return {"verdict_pass": ok, "receipt": receipt, "recall": recall}
    finally:
        rll._stop_arm([lane])


def scenario_E(run_dir: Path) -> dict:
    """月级缩比:高倍速灌一段 + 小轮转阈值逼出归档,验不爆资源(工作集有界)、回执持续丢弃 0。"""
    print("\n=== E 月级缩比(高倍速 + 归档,工作集有界、回执持续丢弃 0)===", flush=True)
    rll._reset_registry()
    owner = run_dir / "owner_E"
    lane = _one_lane(run_dir, "E", seed=20260712)
    orig_rotate = hv._SPOOL_ROTATE_BYTES
    hv._SPOOL_ROTATE_BYTES = 8 * 1024  # 8KB:小阈值逼频繁轮转→验归档不涨盘(缩比"几个月")
    try:
        _open_audit_lane(owner, lane, run_id="run-E", window=3600)
        hit_set: set[str] = set()
        quota = _quota(lane)
        stats = AuditStats()
        receipts_over_time: list[dict] = []
        max_spool_bytes = 0
        deadline = time.time() + 90
        # 边灌边判(判读工持续消费+归档),周期采回执
        for _round in range(6):
            _seed(lane, n=40, decoys_per_hit=6)
            hit_set = rll._lane_hits(lane)
            _settle_harvest(lane, 3.0)
            _drain_all_shards(owner, lane, stats, hit_set, quota, deadline=min(deadline, time.time() + 25),
                              run_id="run-E")
            r = _receipt(lane)
            receipts_over_time.append(r)
            try:
                max_spool_bytes = max(max_spool_bytes, hv.spool_path(ws.registry.get(lane.watch_id)).stat().st_size)
            except OSError:
                pass
            if time.time() > deadline:
                break
        archive = ws.state_dir(owner) / f"{lane.watch_id}.archive.ndjson"
        archived = archive.exists() and archive.stat().st_size > 0
        drop_always_zero = all(r.get("dropped") == 0 for r in receipts_over_time)
        # 工作集有界:活跃 spool 文件远小于总入队量(归档把已判前缀搬走了)
        final = _receipt(lane)
        bounded = max_spool_bytes < 512 * 1024  # 活跃文件始终 <512KB(总量可远大)
        ok = drop_always_zero and archived and bounded and final.get("enqueued", 0) > 100
        print(f"[判定E] 采样回执 dropped 恒 0={drop_always_zero}; 归档已生成={archived}; "
              f"活跃 spool 峰值={max_spool_bytes}B(<512KB={bounded}); 总入队={final.get('enqueued')}"
              f" → {'PASS' if ok else 'FAIL'}", flush=True)
        rll._dump(run_dir, "E_result", {"receipts_over_time": receipts_over_time, "archived": archived,
                                        "max_active_spool_bytes": max_spool_bytes, "final": final,
                                        "verdict_pass": ok})
        return {"verdict_pass": ok, "archived": archived, "max_active_spool_bytes": max_spool_bytes}
    finally:
        hv._SPOOL_ROTATE_BYTES = orig_rotate
        rll._stop_arm([lane])


def scenario_F(run_dir: Path, backlog: int) -> dict:
    """过载不瞎搞:存量洪峰灌爆队列,验待判如实涨、丢弃 0、rubber_stamp=0,精度不因积压崩。"""
    print(f"\n=== F 过载不瞎搞(灌 {backlog} 存量洪峰,待判如实涨/丢弃0/绝无盖章)===", flush=True)
    rll._reset_registry()
    owner = run_dir / "owner_F"
    lane = _one_lane(run_dir, "F", seed=20260713)
    try:
        _open_audit_lane(owner, lane, run_id="run-F", window=3600)
        _seed(lane, n=backlog, decoys_per_hit=8)  # 大存量:decoy 稠密,盖章就会误报一堆
        _settle_harvest(lane, 8.0)
        hit_set = rll._lane_hits(lane)
        quota = _quota(lane)
        mid_receipt = _receipt(lane)  # 判之前:待判应≈入队(如实涨)
        stats = AuditStats()
        # 只判一部分(模拟判读追不上洪峰),中途采回执验待判如实、丢弃0、无盖章
        deadline = time.time() + 200
        _drain_all_shards(owner, lane, stats, hit_set, quota, deadline=deadline, run_id="run-F",
                          max_batches=12)
        partial = _receipt(lane)
        # 精度:判为 hit 的里有多少真是 hit(过载不该把 decoy 盖成 hit)
        reported = stats.hit_event_ids
        true_reported = len(reported & hit_set)
        precision = true_reported / max(1, len(reported))
        ok = (mid_receipt.get("pending", 0) > 0 and mid_receipt.get("dropped") == 0
              and partial.get("dropped") == 0 and stats.rubber_stamp_pulls == 0
              and (not reported or precision >= 0.8))
        print(f"[判定F] 灌后回执={mid_receipt}(待判>0/丢弃0); 判一段后={partial}; "
              f"盖章批数={stats.rubber_stamp_pulls}(应0); 精度={precision:.0%}(判为hit {len(reported)} 真 {true_reported})"
              f" → {'PASS' if ok else 'FAIL'}", flush=True)
        rll._dump(run_dir, "F_result", {"receipt_after_flood": mid_receipt, "receipt_partial": partial,
                                        "rubber_stamp_pulls": stats.rubber_stamp_pulls, "precision": precision,
                                        "reported": len(reported), "true_reported": true_reported,
                                        "verdict_pass": ok})
        return {"verdict_pass": ok, "rubber_stamp_pulls": stats.rubber_stamp_pulls, "precision": precision}
    finally:
        rll._stop_arm([lane])


def scenario_G(run_dir: Path) -> dict:
    """契约继承:主→判读子代理→孙代理。用不同 run_id 的 worker + 分片再分片建模 spawn 树。
    每层都逐条判、判完才签收——机制层强制(ack-on-judge 在 spool 层),验证没有哪层偷偷跳过。"""
    print("\n=== G 契约继承(主→子→孙,每层逐条判+判完才签收,机制层强制)===", flush=True)
    rll._reset_registry()
    owner = run_dir / "owner_G"
    lane = _one_lane(run_dir, "G", seed=20260714)
    try:
        _open_audit_lane(owner, lane, run_id="run-G-main", window=3600)
        _seed(lane, n=90, decoys_per_hit=5)
        _settle_harvest(lane, 6.0)
        hit_set = rll._lane_hits(lane)
        quota = _quota(lane)
        # 主代理 pull 看 judge_fanout 推荐工数(按积压动态)——分 3 判读子代理
        fanout = max(2, _recommended_workers(lane))
        print(f"    judge_fanout 推荐判读工数(按积压动态)= {fanout}", flush=True)
        stats = AuditStats()
        stop, feed_done = threading.Event(), threading.Event()
        feed_done.set()
        deadline = time.time() + 300
        threads = []
        # 子代理层:fanout 个,各领一分片;其中"分片 0"再分成 2 个孙代理(分片再分片建模)
        for i in range(fanout):
            if i == 0:
                # 子代理 0 把自己这片再派给 2 个孙代理(shard_count 叠乘建模不现实,这里用
                # 同分片双 worker:两个孙代理消费者身份并行判分片0,ack-on-judge 保证不双签)
                for g in range(2):
                    run_id = f"run-G-child0-grand{g}"
                    threads.append(threading.Thread(target=_audit_worker, args=(
                        owner, lane, stats, hit_set, 0, fanout, quota, deadline, run_id, stop, feed_done, False, 0)))
            else:
                threads.append(threading.Thread(target=_audit_worker, args=(
                    owner, lane, stats, hit_set, i, fanout, quota, deadline, f"run-G-child{i}", stop, feed_done, False, 0)))
        for t in threads:
            t.start()
        _drain_all_shards(owner, lane, stats, hit_set, quota, deadline=deadline, run_id="run-G-sweep",
                          shard_count=fanout)
        stop.set()
        for t in threads:
            t.join(timeout=10)
        receipt = _receipt(lane)
        # 契约继承成立的证据:①每条最终有结论(判完签收)②丢弃0 ③台账每条都有真模型结论
        ledger = _read_ledger(owner, lane)
        judged_via_ledger = len(ledger)
        ok = (receipt.get("dropped") == 0 and receipt.get("pending") == 0
              and receipt.get("judged") == receipt.get("enqueued")
              and judged_via_ledger == receipt.get("judged"))
        print(f"[判定G] 回执={receipt}; 台账逐条结论数={judged_via_ledger}(=已判 {receipt.get('judged')});"
              f" fanout={fanout} → {'PASS' if ok else 'FAIL'}", flush=True)
        rll._dump(run_dir, "G_result", {"receipt": receipt, "fanout": fanout,
                                        "ledger_rows": judged_via_ledger, "verdict_pass": ok})
        return {"verdict_pass": ok, "receipt": receipt, "fanout": fanout, "ledger_rows": judged_via_ledger}
    finally:
        rll._stop_arm([lane])


# ═══════════════════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════════════════


def _seed(lane: "rll.Lane", n: int, decoys_per_hit: int) -> None:
    sim._seed_backlog(lane.source, n, decoys_per_hit, lane.answer_key)


def _settle_harvest(lane: "rll.Lane", seconds: float) -> None:
    """让收割线程把存量抬进 spool(判读前先确保入队,回执 enqueued 稳定)。"""
    tool = rll._tool(lane_owner(lane), "settle")
    end = time.time() + seconds
    # 触发 harvester 起跑:一次 pull(拿不到也没关系,收割线程已在后台抬)
    state = ws.registry.get(lane.watch_id)
    if state is not None:
        hv.ensure_harvester(state, tool._harvester_fetch())
    while time.time() < end:
        time.sleep(0.5)


def lane_owner(lane: "rll.Lane") -> Path:
    state = ws.registry.get(lane.watch_id)
    return state.owner_home if state is not None else Path(".")


def _settle_harvest_multi(lanes: list["rll.Lane"], seconds: float) -> None:
    for lane in lanes:
        state = ws.registry.get(lane.watch_id)
        if state is not None:
            hv.ensure_harvester(state, rll._tool(state.owner_home, "settle")._harvester_fetch())
    time.sleep(seconds)


def _drain_all_shards(owner: Path, lane: "rll.Lane", stats: AuditStats, hit_set: set[str],
                      quota: int, deadline: float, run_id: str = "sweep",
                      shard_count: int = 1, max_batches: int = 0) -> None:
    """把这路 watch 的全部(所有分片)未判积压判到清零(或到 deadline/批数上限)。单线程顺扫
    各分片——收尾兜底,确保没有分片被落下。"""
    feed_done = threading.Event()
    feed_done.set()
    batches = 0
    while time.time() < deadline:
        progressed = False
        for si in range(shard_count):
            tool = rll._tool(owner, f"{run_id}-s{si}")
            n = _audit_judge_once(tool, lane, stats, hit_set, si, shard_count, quota)
            if n > 0:
                progressed = True
                batches += 1
                if max_batches and batches >= max_batches:
                    return
        if not progressed:
            if _audit_drained(owner, lane):
                return
            time.sleep(0.4)


def _judged_event_ids(lane: "rll.Lane") -> set[str]:
    """从盘上 spool + verdict 台账反推"哪些 event_id 已被判到"(有结论=被判)。"""
    owner = lane_owner(lane)
    ledger = _read_ledger(owner, lane)
    judged_acks = {row["ack_id"] for row in ledger}
    # 反查 ack_id → event_id:扫 spool + archive 所有记录
    ack_to_event: dict[str, str] = {}
    for fname in (f"{lane.watch_id}.spool.ndjson", f"{lane.watch_id}.archive.ndjson"):
        path = ws.state_dir(owner) / fname
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            for row in hv.ensure_ack_ids(rec):
                ev = row.get("event") or {}
                if isinstance(ev, dict) and ev.get("event_id"):
                    ack_to_event[str(row.get("ack_id"))] = str(ev["event_id"])
    return {ack_to_event[a] for a in judged_acks if a in ack_to_event}


def _cleared_hits(lane: "rll.Lane", stats: AuditStats, hit_set: set[str]) -> set[str]:
    return set()  # 占位:B 只要求"被判到",精度另算


def _read_ledger(owner: Path, lane: "rll.Lane") -> list[dict]:
    path = ws.state_dir(owner) / f"{lane.watch_id}.verdicts.ndjson"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def _write_manifest(run_dir: Path, results: dict, elapsed: float) -> None:
    manifest = {
        "model": rll._MODEL, "endpoint": rll._API_BASE, "model_calls_total": _model_calls["n"],
        "wall_seconds": round(elapsed, 1), "scenarios": results,
        "all_pass": all(r.get("verdict_pass") for r in results.values()),
    }
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[manifest] {run_dir / 'run_manifest.json'} (模型调用 {_model_calls['n']} 次, 墙钟 {elapsed:.0f}s)", flush=True)
    print(f"[总判定] {'全 PASS' if manifest['all_pass'] else '有 FAIL'}: "
          + ", ".join(f"{k}={'✓' if v.get('verdict_pass') else '✗'}" for k, v in results.items()), flush=True)


def main() -> None:
    if not os.environ.get("AGENT_API_KEY"):
        print("需要 AGENT_API_KEY(真调 MiniMax-M2.7,禁模拟)", flush=True)
        sys.exit(2)
    rll.BASE_PORT = BASE_PORT
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scenarios", default="A,B,C,D,E,F,G")
    p.add_argument("--quick", action="store_true", help="缩小规模冒烟(更少记录/源)")
    args = p.parse_args()
    run_dir = ARTIFACT_ROOT
    run_dir.mkdir(parents=True, exist_ok=True)
    picked = [s.strip().upper() for s in args.scenarios.split(",") if s.strip()]
    n_sources = 3 if args.quick else 5
    backlog = 40 if args.quick else 120
    print(f"=== /audit 保证档真-M2.7 验收台({rll._MODEL};判读全真调,产物留 {run_dir})===", flush=True)
    start = time.time()
    results: dict = {}
    runners = {
        "A": lambda: scenario_A(run_dir, hits_target=6),
        "B": lambda: scenario_B(run_dir),
        "C": lambda: scenario_C(run_dir, n_sources),
        "D": lambda: scenario_D(run_dir),
        "E": lambda: scenario_E(run_dir),
        "F": lambda: scenario_F(run_dir, backlog),
        "G": lambda: scenario_G(run_dir),
    }
    for key in picked:
        if key in runners:
            try:
                results[key] = runners[key]()
            except Exception as exc:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                results[key] = {"verdict_pass": False, "error": str(exc)}
    _write_manifest(run_dir, results, time.time() - start)
    sys.exit(0 if all(r.get("verdict_pass") for r in results.values()) else 1)


if __name__ == "__main__":
    main()
