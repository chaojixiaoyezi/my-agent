#!/usr/bin/env python3
"""逐条上报延迟 + 过载不乱报 端到端自测(P1 头号:接 49d1a09b 之后两个新场景)。

中性料(content_source_simulator):5 源、response.status 恒 200、真得手 vs 没得手【只在
response.message 的自然语言语义】、真事稀、since=0 读存量。判据学不出结构差别 → 模型
配 passthrough(每条都递上来判)——这正是真机五源全 passthrough、整流洪泛的形态。

判读模型(隔离环境无法真调模型,按真机行为建模,只建模"批量→判力"这一层):
  · 每 pull 批量 ≤ careful_capacity → 逐条精读(完美读者:按答案键正确分真假,只报真 hit)。
    —— 隔离管道延迟 vs 判读准确率(P4 边界案例方差非本棒目标),故精读=100% 正确。
  · 每 pull 批量 > careful_capacity → 判力被淹 → rubber-stamp(整批当命中报出)。
    —— 真机实锤:一次 500 条正常流整车倒给模型 → 60 条同微秒批量乱报。

两场景:
  ① 逐条上报延迟:合理速率(判读跟得上),量每条真事 发生→上报 的延迟,验证秒级~分钟级。
  ② 过载不乱报:速率拉爆(存量洪泛),对照【修复前】(content_mode 记录 500 条/无背压)
     vs【修复后】(记录钳到一批 48/背压封顶 spool/overload 如实标注):
       修复后 = 每 pull 批量 ≤ capacity → 判读精读不乱报(FP≈0)、spool 有界、overload 块在场;
       对照   = 一 pull 被怼 500 条 → 判力淹 → 整批 rubber-stamp(FP 暴涨)、spool 无界。

跑法:
  python3 scripts/watch_harness/latency_and_overload_harness.py
"""

from __future__ import annotations

import json
import statistics
import sys
import tempfile
import threading
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

_CAPACITY = 48  # 模型每 pull 能精读的候选数;超此判力被淹 rubber-stamp。


def _source_handle(source):
    def handle(request):
        url = request.url
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


def _answer(ak: str) -> dict[str, float]:
    rows = {}
    for line in Path(ak).read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            rows[r["event_id"]] = float(r["emitted_at"])
    return rows


def _batch_ids(candidates: list[dict]) -> list[str]:
    out = []
    for c in candidates:
        ev = c.get("event") if isinstance(c, dict) else None
        if isinstance(ev, dict) and ev.get("event_id"):
            out.append(str(ev["event_id"]))
    return out


def _judge(candidates: list[dict], hit_set: set[str], capacity: int) -> tuple[list[str], bool]:
    """建模判读:批量≤capacity 逐条精读(只报真 hit);>capacity 判力淹→整批 rubber-stamp。"""
    ids = _batch_ids(candidates)
    if len(candidates) <= capacity:
        return [i for i in ids if i in hit_set], False
    return ids, True


def _configure_passthrough(tool: WatchStreamTool, watch_id: str) -> None:
    tool.execute({"action": "configure", "watch_id": watch_id, "spec": {"passthrough": True}})


def _make_watch(prefix: str, backlog: int):
    """新建隔离 owner + passthrough 盯守(冷启动重置进程内注册表);返回 (source, ak, tool, watch_id)。"""
    ws.registry = ws.WatchRegistry()
    wt.registry = ws.registry
    hv.harvesters = hv._HarvesterRegistry()
    owner = Path(tempfile.mkdtemp(prefix=f"{prefix}-owner-"))
    source = sim.SourceState(sim._build_specs()[0], seed=20260707)
    ak = str(Path(tempfile.mkdtemp(prefix=f"{prefix}-ak-")) / "answer.jsonl")
    Path(ak).write_text("", encoding="utf-8")
    if backlog:
        sim._seed_backlog(source, backlog, 8, ak)  # since=0 存量洪峰
    tool = _tool(owner, source, run_id=f"run-{prefix}")
    opened = json.loads(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull?since=<next>&limit=<limit>", "watch_window_seconds": 3600}).output)
    _configure_passthrough(tool, opened["watch_id"])
    return source, ak, tool, opened["watch_id"]


def _pull_judge(tool: WatchStreamTool, watch_id: str, hit_set: set[str], capacity: int) -> dict:
    """一 pull + 判读:返回该拍结构化结果。ids=本批全部候选事件 ID(交付计量,不受答案键
    写入滞后影响——模拟器 ring 先于 answer-key 落,按判读时的 hit_set 计交付会漏计)。"""
    payload = json.loads(tool.execute({"action": "pull", "watch_id": watch_id, "max_wait_seconds": 0.5}).output)
    cand = payload.get("candidates") or []
    reports, stamped = _judge(cand, hit_set, capacity)
    return {
        "n": len(cand),
        "ids": _batch_ids(cand),
        "fp": sum(1 for i in reports if i not in hit_set),
        "stamped": stamped,
        "overload": bool(payload.get("overload")),
    }


# --- 场景①:逐条上报延迟(合理速率) --------------------------------------


def _reasonable_feed(source, ak: str, args, stop: threading.Event) -> None:
    # 合理速率:每 ~0.25s 灌 ~3 条(≈12/s),判读 48/pull、亚秒 pull → 判读跟得上。
    while not stop.is_set():
        for _ in range(3):
            source.append(sim._roll_kind(source.rng, args), ak)
        time.sleep(0.25)


def scenario_latency() -> dict:
    source, ak, tool, watch_id = _make_watch("lat", backlog=0)
    args = SimpleNamespace(answer_key=ak, decoys_per_hit=8, suspect_frac=0.5)
    stop = threading.Event()
    feeder = threading.Thread(target=_reasonable_feed, args=(source, ak, args, stop), daemon=True)
    feeder.start()
    delivered_at: dict[str, float] = {}
    fp = 0
    loop_start = time.time()
    while time.time() - loop_start < 15.0:
        fp += _drain_once(tool, watch_id, ak, delivered_at)
    stop.set()
    feeder.join(timeout=2.0)
    # 收尾续拉:冻结活流后把在途尾巴拉完再量(监控本就持续 pull;不留人为截断的假漏)。
    tail = time.time()
    while time.time() - tail < 5.0:
        fp += _drain_once(tool, watch_id, ak, delivered_at)
    return _latency_stats(ak, delivered_at, fp, loop_start)


def _drain_once(tool: WatchStreamTool, watch_id: str, ak: str, delivered_at: dict[str, float]) -> int:
    """一 pull:记录本批每条事件的交付时刻(首次;真假到最后用最终答案键判),返回本拍误报数。"""
    res = _pull_judge(tool, watch_id, set(_answer(ak)), _CAPACITY)
    now = time.time()
    for eid in res["ids"]:
        delivered_at.setdefault(eid, now)
    return res["fp"]


def _latency_stats(ak: str, delivered_at: dict[str, float], fp: int, since: float) -> dict:
    """只量【活流】hit(since 之后发生的):live 才是"合理速率实时上报"的度量。真假用最终
    答案键判(delivered_at 记的是全部候选交付时刻,规避模拟器答案键写入滞后的计量假漏)。"""
    emit = _answer(ak)
    live = [e for e in emit if emit[e] >= since]
    lat = sorted(delivered_at[e] - emit[e] for e in live if e in delivered_at and delivered_at[e] >= emit[e])
    return {
        "live_hits": len(live),
        "delivered_live": len([e for e in live if e in delivered_at]),
        "false_positives": fp,
        "median_latency": round(statistics.median(lat), 2) if lat else None,
        "p90_latency": round(lat[min(len(lat) - 1, int(len(lat) * 0.9))], 2) if lat else None,
        "max_latency": round(max(lat), 2) if lat else None,
        "n_lat": len(lat),
    }


# --- 场景②:过载不乱报(修复后 vs 修复前对照) ---------------------------


def scenario_overload(control: bool) -> dict:
    # 对照=修复前:content_mode 记录 500 条/无背压(is_content_mode 关掉即回到旧行为)。
    original_cm = hv.is_content_mode
    if control:
        hv.is_content_mode = lambda state: False
    try:
        return _run_overload()
    finally:
        hv.is_content_mode = original_cm


def _run_overload() -> dict:
    source, ak, tool, watch_id = _make_watch("ovl", backlog=2000)
    state = ws.registry.get(watch_id)
    hit_set = set(_answer(ak))
    acc = {"fp": 0, "stamped": 0, "overload": False, "max_batch": 0, "peak": 0, "hits": set()}
    for _ in range(60):
        res = _pull_judge(tool, watch_id, hit_set, _CAPACITY)
        acc["fp"] += res["fp"]
        acc["stamped"] += 1 if res["stamped"] else 0
        acc["overload"] = acc["overload"] or res["overload"]
        acc["hits"] |= {i for i in res["ids"] if i in hit_set}
        acc["max_batch"] = max(acc["max_batch"], res["n"])
        acc["peak"] = max(acc["peak"], hv.spool_unread(state))
        if res["n"] == 0 and hv.spool_unread(state) == 0:
            break
    return {
        "true_hits": len(hit_set),
        "delivered_hits": len(acc["hits"]),
        "false_positives": acc["fp"],
        "rubber_stamp_pulls": acc["stamped"],
        "max_batch_to_model": acc["max_batch"],
        "peak_spool_backlog": acc["peak"],
        "overload_note_seen": acc["overload"],
    }


def main() -> None:
    print("=== 场景① 逐条上报延迟(合理速率,判读跟得上)===", flush=True)
    lat = scenario_latency()
    print(json.dumps(lat, ensure_ascii=False), flush=True)
    lat_ok = (
        lat["delivered_live"] >= max(1, int(lat["live_hits"] * 0.6))
        and lat["false_positives"] == 0
        and lat["median_latency"] is not None
        and lat["median_latency"] <= 20.0
        and (lat["max_latency"] or 0) <= 60.0
    )
    print(f"[判定①] 活流真事多数被上报={lat['delivered_live']}/{lat['live_hits']}, 零误报={lat['false_positives']==0}, "
          f"中位延迟≤20s={lat['median_latency']}, 峰值延迟≤60s={lat['max_latency']} → {'PASS' if lat_ok else 'FAIL'}", flush=True)

    print("\n=== 场景② 过载不乱报(修复后 vs 修复前对照)===", flush=True)
    fixed = scenario_overload(control=False)
    control = scenario_overload(control=True)
    print(f"修复后: {json.dumps(fixed, ensure_ascii=False)}", flush=True)
    print(f"对照(修复前): {json.dumps(control, ensure_ascii=False)}", flush=True)

    fixed_ok = (
        fixed["max_batch_to_model"] <= _CAPACITY
        and fixed["rubber_stamp_pulls"] == 0
        and fixed["false_positives"] == 0
        and fixed["peak_spool_backlog"] <= 8 * _CAPACITY + 500  # 背压封顶(ceiling+一拍余量)
        and fixed["overload_note_seen"] is True
    )
    control_floods = (
        control["max_batch_to_model"] > _CAPACITY
        and control["rubber_stamp_pulls"] > 0
        and control["false_positives"] > fixed["false_positives"]
    )
    print(f"[判定②] 修复后:每pull批量≤{_CAPACITY}({fixed['max_batch_to_model']})·不乱报(FP={fixed['false_positives']},乱报轮={fixed['rubber_stamp_pulls']})·"
          f"spool有界({fixed['peak_spool_backlog']})·overload标注在场({fixed['overload_note_seen']}) → {'PASS' if fixed_ok else 'FAIL'}", flush=True)
    print(f"[判定②对照] 修复前:批量{control['max_batch_to_model']}>capacity·rubber-stamp {control['rubber_stamp_pulls']}轮·FP={control['false_positives']}(远超修复后) → {'复现乱报' if control_floods else '未复现'}", flush=True)

    ok = lat_ok and fixed_ok and control_floods
    print("\nVERDICT:", "PASS 延迟低·过载优雅降级不乱报·对照坐实修复前乱报" if ok else "FAIL 见上", flush=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
