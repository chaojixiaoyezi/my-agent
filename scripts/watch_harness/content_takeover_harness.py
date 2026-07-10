#!/usr/bin/env python3
"""接管换人零孤儿 端到端自测(问题1:接管丢 spool 缓冲区孤儿)。

复测第二轮定格的头号 bug:盯守子代理中途被接管换人时,继任者只从数据源游标往后续读,
【没接手前任已 surface 进 spool、还没判完的候选】→ 已抬升的真事成孤儿(3 条真事躺在
spool 里没人判、没上报)。这台把这个场景端到端跑出来,量【零孤儿】。

关键:这是【管道】属性,与模型判读质量无关——量的是"引擎 surface 进 spool 的真 hit,
有没有跨接管边界全部交付到某个消费者手里"。交付=出现在某次 pull 的 candidates 里。
判读准不准是另一层(真 LLM 另测),这里用答案键当交付去向的裁判,不需要模型。

跑法(真管线:真 harvester 线程 + 真 WatchStreamTool.pull 消费路,料来自
content_source_simulator 的静态 ring):
  python3 scripts/watch_harness/content_takeover_harness.py

场景:
  1. 每源真 harvester 线程把静态 ring(存量+活流,含稀疏真 hit)drain 进 spool。
  2. 消费者 A(run-a)pull 几批(逐批 ack),判读到一半——最后一批【拉走没确认】= 在途。
  3. 【杀掉 A】:A 再不来 pull(模拟子代理终态/被接管)。
  4. 消费者 B(run-b·继任者)open 同源 + pull 到清空:先拿到 A 的在途批(redelivery),
     再续读未读积压和新流。
  5. 裁判:引擎 surface 进 spool 的真 hit,全部被 A 或 B 交付过 → 零孤儿。
     对照量出"A 的在途批里有几条真 hit"——那正是旧 at-most-once 下会被吞掉的孤儿集。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "agent_py_agent"))

from agent.ingestion import harvester as hv  # noqa: E402
from agent.ingestion import watch_state as ws  # noqa: E402
from agent.ingestion import watch_tool as wt  # noqa: E402
from agent.ingestion.watch_tool import WatchStreamTool  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import content_source_simulator as sim  # noqa: E402


def _source_handle(source: sim.SourceState):
    def handle(url: str):
        q = parse_qs(urlsplit(url).query)
        since = int((q.get("since") or ["0"])[0] or 0)
        limit = max(1, min(500, int((q.get("limit") or ["50"])[0] or 50)))
        return True, source.pull(since, limit), ""
    return handle


def _tool(owner_home: Path, source, run_id: str) -> WatchStreamTool:
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-selftest"),
        _current_subagent_run_id=run_id,
    )
    tool = WatchStreamTool(agent)
    tool.allow_private_resolution = True
    tool._fetch_json = _source_handle(source)
    return tool


def _hit_ids(rows: list[dict], hit_set: set[str]) -> set[str]:
    out: set[str] = set()
    for row in rows:
        ev = row.get("event") or {}
        eid = str(ev.get("event_id") or "") if isinstance(ev, dict) else ""
        if eid in hit_set:
            out.add(eid)
    return out


def _pull_once(tool: WatchStreamTool, watch_id: str, max_wait: float) -> dict:
    return json.loads(tool.execute({"action": "pull", "watch_id": watch_id, "max_wait_seconds": max_wait}).output)


def _build_source(index: int, backlog: int, decoys_per_hit: int, live: int, seed: int):
    spec = sim._build_specs()[index]
    source = sim.SourceState(spec, seed=seed)
    ak = str(Path(tempfile.mkdtemp(prefix="tk-ans-")) / "answer.jsonl")
    Path(ak).write_text("", encoding="utf-8")
    args = SimpleNamespace(answer_key=ak, decoys_per_hit=decoys_per_hit, suspect_frac=0.5)
    sim._seed_backlog(source, backlog, decoys_per_hit, ak)
    for _ in range(live):
        for _ in range(20):
            source.append(sim._roll_kind(source.rng, args), ak)
    hits = {json.loads(l)["event_id"] for l in Path(ak).read_text().splitlines() if l.strip()}
    return source, hits


def run_one_source(index: int) -> dict:
    """一路源:harvester 灌 spool → A pull 一批(判读中途)被杀 → B 接管清空 → 量零孤儿。

    孤儿口径(关键):A 死时【最后一批 pull 走、还没 ack】的候选,在旧 at-most-once 下
    读游标已在交付时推进过去 → 继任者 B 永远看不到 = 孤儿。所以"安全交付"= A 死前真正
    ack 掉的(a_acked)∪ B 收到的(b_delivered);orphan = surface 进 spool 的真 hit −
    安全交付。A 只 pull 一次即被杀 → a_acked=0 → 全靠 B 兜(必须重投 A 的在途批)。"""
    ws.registry = ws.WatchRegistry()
    wt.registry = ws.registry
    hv.harvesters = hv._HarvesterRegistry()
    owner = Path(tempfile.mkdtemp(prefix=f"tk-owner-{index}-"))
    source, hit_set = _build_source(index, backlog=800, decoys_per_hit=60, live=6, seed=20260707 + index * 101)

    tool_a = _tool(owner, source, run_id=f"run-a-{index}")
    opened = _pull_once_open(tool_a, index)
    watch_id = opened["watch_id"]

    # harvester 线程把静态 ring 抽干进 spool(源不再增长 → 追平即 reached_end,线程随后 idle)。
    _settle_harvester(tool_a, watch_id)

    # A 只 pull 一次(判读到一半),这一批停在【在途·未 ack】;随即被杀(不再 pull)。
    # 真机=A 子代理这一轮 turn 结束进终态/被接管,它取走的候选还没上报就换人了。
    a_pull = _pull_once(tool_a, watch_id, max_wait=2.0)
    a_inflight_hits = _hit_ids(a_pull.get("candidates") or [], hit_set)
    a_acked_hits: set[str] = set()  # A 只 pull 一次、没有第二次来确认 → 这一批全未 ack
    # 【杀掉 A】

    tool_b = _tool(owner, source, run_id=f"run-b-{index}")
    reopened = _pull_once_open(tool_b, index)  # 继任者 open 同源:回执应亮 backlog
    resumed_backlog = int(reopened.get("spool_backlog_candidates") or 0)

    b_delivered: set[str] = set()
    redelivered_first = 0
    for r in range(80):
        payload = _pull_once(tool_b, watch_id, max_wait=2.0)
        cand = payload.get("candidates") or []
        b_delivered |= _hit_ids(cand, hit_set)
        if r == 0:
            redelivered_first = int(payload.get("redelivered_candidates") or 0)
        backlog = int((payload.get("coverage") or {}).get("spool_backlog_candidates") or 0)
        if not cand and backlog == 0 and payload.get("coverage", {}).get("reached_stream_end"):
            break
    # B 也要 ack 掉自己最后一批(再空 pull 一次),让账面 unjudged 归零。
    _pull_once(tool_b, watch_id, max_wait=1.0)

    state = ws.registry.get(watch_id)
    surfaced_hits = _spool_surfaced_hits(state, hit_set)
    safely_delivered = a_acked_hits | b_delivered
    orphaned = surfaced_hits - safely_delivered
    final_unjudged = _final_unjudged(state)
    return {
        "source": sim._build_specs()[index].name,
        "true_hits_total": len(hit_set),
        "surfaced_into_spool": len(surfaced_hits),
        "A_inflight_at_kill": len(a_inflight_hits),
        "redelivered_first_batch": redelivered_first,
        "delivered_B": len(b_delivered),
        "safely_delivered": len(safely_delivered),
        "orphaned_hits": len(orphaned),
        "orphan_ids": sorted(orphaned),
        "resumed_open_backlog_shown": resumed_backlog,
        "final_unjudged_backlog": final_unjudged,
    }


def _pull_once_open(tool: WatchStreamTool, index: int) -> dict:
    port = 8911 + index
    return json.loads(tool.execute({
        "action": "open", "url": f"http://127.0.0.1:{port}/pull", "background_harvest": 1,
    }).output)


def _settle_harvester(tool: WatchStreamTool, watch_id: str, *, timeout: float = 8.0) -> None:
    """等 harvester 把静态 ring 抽干进 spool(轮询 spool_seq 稳定 + reached_end)。"""
    state = ws.registry.get(watch_id)
    hv.ensure_harvester(state, tool._harvester_fetch())
    stable = 0
    last = -1
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.3)
        seq = int(getattr(state, "spool_seq", 0) or 0)
        if seq == last and bool(getattr(state, "last_reached_end", False)):
            stable += 1
            if stable >= 2:
                return
        else:
            stable = 0
        last = seq


def _spool_surfaced_hits(state, hit_set: set[str]) -> set[str]:
    """扫 spool 文件里所有候选事件,交出其中的真 hit id(= 引擎 surface 进 spool 的真 hit)。"""
    out: set[str] = set()
    path = hv.spool_path(state)
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        for row in rec.get("candidates") or []:
            ev = row.get("event") or {}
            eid = str(ev.get("event_id") or "") if isinstance(ev, dict) else ""
            if eid in hit_set:
                out.add(eid)
    return out


def _final_unjudged(state) -> int:
    written = int(state.totals.get("spool_candidates", 0) or 0)
    if written <= 0:
        return 0
    return max(0, written - hv.acked_candidates(hv.read_spool_cursor(state)))


def _atmost_once_read(state, *, max_candidates: int, consumer: str = "", shard_index: int = 0, shard_count: int = 1):
    """旧 at-most-once 语义的忠实重放:交付即推进读游标,无在途/无重投(=修复前的
    read_spool_records)。用于对照——同一"A 判读中途被杀"场景下,旧码把 A 取走没判完的
    批当已消费,继任者永远看不到 = 孤儿。仅本 harness 的控制组用,不进产品。"""
    cursor = hv.read_spool_cursor(state)
    offset = int(cursor.get("offset") or 0)
    if int(cursor.get("generation") or 0) != state.spool_generation:
        offset = 0
    read_seq = int(cursor.get("read_seq") or 0)
    try:
        with hv.spool_path(state).open("r", encoding="utf-8") as handle:
            handle.seek(offset)
            records, end_offset, taken = hv._collect_spool_rows(handle, offset, read_seq, max_candidates)
    except OSError:
        return [], {}
    if records:
        hv._write_spool_cursor(state, {
            "read_seq": int(records[-1].get("spool_seq") or 0),
            "offset": end_offset,
            "generation": state.spool_generation,
            "candidates_consumed": int(cursor.get("candidates_consumed") or 0) + taken,
            "updated_at": 0,
        })
    return records, {}


def run_control_old_behavior(index: int) -> int:
    """同场景跑旧 at-most-once 语义,返回跨接管孤儿真 hit 数(应 >0,证明场景真能触发 bug)。"""
    import agent.ingestion.harvester as hmod
    original = hmod.read_spool_records
    hmod.read_spool_records = _atmost_once_read
    try:
        r = run_one_source(index)
        return r["orphaned_hits"]
    finally:
        hmod.read_spool_records = original


def main() -> None:
    # 控制组:同样"A 判读中途被杀"场景跑【旧 at-most-once 语义】,坐实这套料真能制造孤儿
    # (不是场景太温和白测)。修复组紧接着跑,量应归零。
    control_orphans = [run_control_old_behavior(i) for i in range(5)]
    print(f"[控制组·旧 at-most-once] 同场景跨接管孤儿真 hit(逐源)={control_orphans} 合计={sum(control_orphans)} "
          f"(>0 = 场景确实触发 bug,修复组须把它清零)", flush=True)

    results = [run_one_source(i) for i in range(5)]
    print("\n=== 接管换人零孤儿(A 判读中途被杀,继任者 B 是否兜住 surface 进 spool 的全部真 hit)===", flush=True)
    hdr = f"{'source':<16}{'hits':>6}{'surfaced':>10}{'A_infl':>8}{'redeliv':>9}{'B':>5}{'safe':>6}{'orphan':>8}{'open_bl':>9}{'unjudg':>8}"
    print(hdr, flush=True)
    total_orphans = 0
    total_surfaced = 0
    total_inflight = 0
    total_redeliv = 0
    for r in results:
        total_orphans += r["orphaned_hits"]
        total_surfaced += r["surfaced_into_spool"]
        total_inflight += r["A_inflight_at_kill"]
        total_redeliv += r["redelivered_first_batch"]
        print(f"{r['source']:<16}{r['true_hits_total']:>6}{r['surfaced_into_spool']:>10}"
              f"{r['A_inflight_at_kill']:>8}{r['redelivered_first_batch']:>9}"
              f"{r['delivered_B']:>5}{r['safely_delivered']:>6}{r['orphaned_hits']:>8}"
              f"{r['resumed_open_backlog_shown']:>9}{r['final_unjudged_backlog']:>8}", flush=True)
        if r["orphan_ids"]:
            print(f"    ORPHAN ids on {r['source']}: {r['orphan_ids']}", flush=True)
    all_unjudged_zero = all(r["final_unjudged_backlog"] == 0 for r in results)
    all_open_showed = all(r["resumed_open_backlog_shown"] > 0 for r in results)
    # 场景有效性:①控制组(旧语义)在同场景下真的产生了孤儿(不是场景太温和);
    # ②修复组把 A 杀在"在途批里有真 hit"的当口且 B 首批确有重投——否则这台没证明任何东西。
    control_total = sum(control_orphans)
    scenario_valid = total_inflight > 0 and total_redeliv > 0 and control_total > 0
    ok = total_orphans == 0 and all_unjudged_zero and total_surfaced > 0 and scenario_valid and all_open_showed
    print(f"[对照] 旧语义孤儿合计={control_total} → 修复后孤儿合计={total_orphans}", flush=True)
    print(f"\n[汇总] surface 进 spool 真 hit={total_surfaced}, 跨接管孤儿={total_orphans}, "
          f"A 被杀时在途真 hit={total_inflight}(旧 at-most-once 下这些即孤儿), "
          f"B 首批重投候选合计={total_redeliv}", flush=True)
    print(f"[汇总] 继任者 open 均亮出积压={all_open_showed}, 收尾账面未判归零={all_unjudged_zero}, "
          f"场景有效(在途含真hit且发生重投)={scenario_valid}", flush=True)
    print("VERDICT:", "PASS 零孤儿 & 在途批被重投接手 & 收尾账清零" if ok else "FAIL 见上", flush=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
