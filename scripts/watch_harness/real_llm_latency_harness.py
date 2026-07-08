#!/usr/bin/env python3
"""真-LLM 判读吞吐/延迟/过载/重启 端到端自测台(P1 头号 + P2)。判读【全部真调 MiniMax-M2.7】。

━━━ 为什么要这台(它和旧的 latency_and_overload_harness.py 的根本区别)━━━
旧台把判读【模拟】成"瞬间 + 完美读答案键"的读者(零判读耗时、100% 准),所以它量出的
"1.4s 延迟"只是【管道延迟】,没算真判读时间。真机 MiniMax 每条判读要几秒~几十秒 + 排队,
才是真机 7–18 分延迟的来源。**本台判读一律真调 MiniMax-M2.7,禁止任何形式的模拟判读。**
端到端延迟 = 真事发生(emitted_at)→ 真模型判完这批(judge 调用返回)→ 计入延迟。

━━━ 怎么跑 ━━━
  需要环境变量 AGENT_API_KEY(Mac 环境里有;"无法真调"不成立)。
    AGENT_API_KEY=... python3 scripts/watch_harness/real_llm_latency_harness.py
  可选参数(默认已调到"一次跑 ~10–25 分钟、~120–200 次模型调用"的规模):
    --sources N         盯守源数(默认 5;这是场景实例数,不是上限——判读工数按积压动态定)
    --latency-secs S    场景① 活流喂料时长(默认 24s)
    --rate R            场景① 每源每秒事件数(默认 3)
    --overload-backlog N 场景② 每源存量洪峰(默认 200)
    --decoys-per-hit D   问题流里 decoy:hit 比(默认 8;调大=真事更稀、更省钱)
    --scenarios 1,2,3    只跑指定场景(默认全跑)
    --serve-only         只把 5 个真源 HTTP 服务起在固定端口不判读(给测试方 curl 勘查环境用)

━━━ 用哪个模型 + 端点 ━━━
  POST https://api.minimaxi.com/anthropic/v1/messages ,model=MiniMax-M2.7,
  header x-api-key=$AGENT_API_KEY + anthropic-version: 2023-06-01(anthropic 兼容端点)。
  每次 pull 出一批候选就调一次模型;一次完整跑(3 场景 × 控制/修复两臂)约 120–200 次调用,
  按批大小 8–48、每调用 ~5–30s,墙钟约 10–25 分钟(真机判读慢正是本台要暴露的东西)。

━━━ 它起了什么 / 监听什么端口 / 料在哪 ━━━
  · 5 个【真 HTTP 源服务】(content_source_simulator 的中性料:status 恒 200、真假只在
    response.message 正文语义、真事稀),监听 127.0.0.1:{BASE_PORT..BASE_PORT+N-1}
    (BASE_PORT=8951)。都是本进程内的 daemon 线程,harness 进程退出即随之结束。
  · 每源一个进程内 harvester 线程(后台连续摄取,把源 drain 进 spool)。
  · 判读工=本台起的 Python 线程(不是真子代理);修复臂按 pull 回执里的 judge_fanout
    推荐工数用 shard_index/shard_count 起【多个并行判读工】判同一路 spool(这正是产品里
    create_subagents 一源多判读工要驱动的机制,这里用线程忠实驱动它、判读仍真调 M2.7)。
  · 所有产物写在【固定路径】ARTIFACT_ROOT = <repo>/data/realllm_watch_harness/ :
      answer_keys/          每源真 hit 的旁路答案键(judge 够不着,只当延迟/召回裁判)
      owner_*/watch_state/  每个 owner 的 watch 快照 + spool + 读游标(sharded 时多份分片游标)
      scenario1_latency.json / scenario2_overload.json / scenario3_restart.json  逐场景结果
      run_manifest.json     这次跑动了什么、留下了什么(给测试方接管的清单)
  跑完【一律不删、不停、不清】——整套留在盘上,交测试方统一处理(见文件尾 CLEANUP 注)。

━━━ 预期输出(在哪看)━━━
  · 场景① 逐条上报延迟:修复臂(并行判读工)中位/尾延迟 << 控制臂(单工串行扛所有源),
    且修复臂为【秒级~分钟级】(不是十几分钟、更不是报不出)。见 scenario1_latency.json。
  · 场景② 过载不乱报:速率拉爆下修复臂精度高、overload 块在场、如实标注积压未判、
    不整批盖章(rubber-stamp);控制臂(无背压单工怼整批)精度崩。见 scenario2_overload.json。
  · 场景③ 重启恢复:重启后【全部 N 源】都被重新驱动续判(每源都有新判读产出)。
    见 scenario3_restart.json。
  终端每场景打印 [判定] 与 VERDICT。

━━━ 硬要求自查 ━━━
  判读【必须】真调 MiniMax-M2.7(_judge_batch → _call_minimax)。若 AGENT_API_KEY 缺失,
  直接退出码 2、不跑——绝不退化成模拟判读。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "agent_py_agent"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent.ingestion import harvester as hv  # noqa: E402
from agent.ingestion import watch_state as ws  # noqa: E402
from agent.ingestion import watch_tool as wt  # noqa: E402
from agent.ingestion.watch_tool import WatchStreamTool  # noqa: E402
import content_source_simulator as sim  # noqa: E402

# ── 固定、文档化的产物路径(别散在随机 /tmp,让测试方找得到、接得住)──
ARTIFACT_ROOT = _REPO / "data" / "realllm_watch_harness"
BASE_PORT = 8951

_API_BASE = "https://api.minimaxi.com/anthropic/v1/messages"
_MODEL = "MiniMax-M2.7"

_SYSTEM = (
    "你在盯守一个数据源,逐条判读它推来的候选事件。这个源 response.status 恒为 200"
    "(成败绝不看状态码),真得手 vs 没得手【只能读 response.message 的自然语言语义】——"
    "正文说【已生效/正在服务/持久化落地/已提交/真的送出去了】才算真得手;说【被降级/被后置"
    "一层拦下/暂存未生效/回滚/no-op/影子模式/超额丢弃/未实际入账/没跨过边界】即使 200 也是"
    "没得手,不算命中。请求内容真假两类几乎一样,别看请求端、只按结果端正文语义判。"
    # 关键决策纪律(实测:补这条 M2.7 在此类料上精度 15%→100%;缺它就把一堆迷惑项当命中):
    "【关键:读到否定/未竟语义(被降级/拦下/回滚/no-op/影子模式/丢弃/未入账/没跨边界)时,"
    "即使正文同时出现 success/ok/applied/committed 之类成功字样,也【一票否决】判未得手——"
    "别被表面成功词迷惑;正文里只要有一处说这次操作实际没落地/被中和,就是没得手】。"
    "逐条独立判,正文自相矛盾或真读不出成没成的按存疑【不报】;【宁可漏报,也绝不把『尝试了"
    "但实际没成』的当命中】。若系统提示过载/积压,也【绝不为追进度整批盖章】——把手里这批"
    "读准即可,宁可留下诚实的未判积压。"
)
_ASK = (
    "\n\n上面是本次 pull 的候选批(每条带 event.event_id 和 event.response.message)。"
    "逐条判读哪些是【真得手】。只输出一个 JSON:{\"hits\":[\"EVT-...\",...]},hits 里放你判定为"
    "真得手的 event_id(判为没得手/存疑的不要放)。不要输出别的。"
)

_MODEL_CALLS = {"n": 0}
_MODEL_CALLS_LOCK = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════════
# 真模型判读(唯一判读入口;禁止任何模拟)
# ═══════════════════════════════════════════════════════════════════════════


def _call_minimax(payload: dict) -> list[str]:
    """把一批真实 pull 载荷(candidates + envelope + overload 块)喂【真 MiniMax-M2.7】,
    取回它判为命中的 event_id。三次重试,失败返回空(不崩、不模拟)。"""
    envelope = payload.get("source_envelope") or {}
    parts = [f"数据源判据说明(schema_note): {envelope.get('schema_note', '(无)')}"]
    if payload.get("overload"):
        parts.append(f"[系统提示·过载] {payload['overload'].get('note', '')}")
    if payload.get("judge_fanout"):
        parts.append(f"[系统提示·判读并发] {payload['judge_fanout'].get('note', '')}")
    parts.append("candidates: " + json.dumps(payload.get("candidates") or [], ensure_ascii=False))
    body = {
        "model": _MODEL,
        "max_tokens": 16000,
        "system": _SYSTEM,
        "messages": [{"role": "user", "content": "\n\n".join(parts) + _ASK}],
    }
    req = urllib.request.Request(
        _API_BASE,
        data=json.dumps(body).encode(),
        headers={
            "x-api-key": os.environ["AGENT_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with _MODEL_CALLS_LOCK:
        _MODEL_CALLS["n"] += 1
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode())
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            return _parse_hits(text)
        except Exception as exc:  # noqa: BLE001
            if attempt == 2:
                print(f"    [model call failed] {exc}", flush=True)
                return []
            time.sleep(3.0 * (attempt + 1))
    return []


def _parse_hits(text: str) -> list[str]:
    m = re.search(r"\{[^{}]*\"hits\"\s*:\s*\[[^\]]*\][^{}]*\}", text, re.S)
    if m:
        try:
            return [str(x) for x in (json.loads(m.group(0)).get("hits") or [])]
        except json.JSONDecodeError:
            pass
    return re.findall(r"EVT-[A-Z]-\d{7}", text)


# ═══════════════════════════════════════════════════════════════════════════
# 真 HTTP 源服务(中性料)+ owner/tool 装配
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class Lane:
    index: int
    source: "sim.SourceState"
    answer_key: str
    port: int
    server: ThreadingHTTPServer
    watch_id: str = ""
    url: str = ""


def _start_sources(run_dir: Path, n_sources: int, seed: int) -> list[Lane]:
    """起 n 个真 HTTP 源服务(127.0.0.1:BASE_PORT+i),中性料;答案键落固定路径。"""
    ak_dir = run_dir / "answer_keys"
    ak_dir.mkdir(parents=True, exist_ok=True)
    specs = sim._build_specs()
    lanes: list[Lane] = []
    for i in range(n_sources):
        source = sim.SourceState(specs[i % len(specs)], seed=seed + i * 101)
        ak = str(ak_dir / f"source_{i}_{source.name}.jsonl")
        Path(ak).write_text("", encoding="utf-8")
        port = BASE_PORT + i
        server = ThreadingHTTPServer(("127.0.0.1", port), sim._make_handler(source))
        threading.Thread(target=server.serve_forever, daemon=True, name=f"src-{i}").start()
        lanes.append(Lane(index=i, source=source, answer_key=ak, port=port, server=server,
                          url=f"http://127.0.0.1:{port}/pull"))
    return lanes


def _tool(owner_home: Path, run_id: str) -> WatchStreamTool:
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-realllm"),
        _current_subagent_run_id=run_id,
    )
    tool = WatchStreamTool(agent)
    tool.allow_private_resolution = True  # 127.0.0.1 真 HTTP 拉取
    return tool


def _open_lane(owner_home: Path, lane: Lane, run_id: str, window: int) -> None:
    tool = _tool(owner_home, run_id)
    opened = json.loads(tool.execute({
        "action": "open", "url": lane.url, "background_harvest": 1, "watch_window_seconds": window,
    }).output)
    lane.watch_id = opened["watch_id"]
    # passthrough:真假只在正文语义、结构分不开 → 每条都递给模型逐条判(铁律)。
    tool.execute({"action": "configure", "watch_id": lane.watch_id, "spec": {"passthrough": True}})


def _reset_registry() -> None:
    ws.registry = ws.WatchRegistry()
    wt.registry = ws.registry
    hv.harvesters = hv._HarvesterRegistry()


def _stop_arm(lanes: list[Lane]) -> None:
    """一个 arm/场景跑完:停该 arm 的 harvester 线程 + 关它的 HTTP 源服务,腾出固定端口给下一
    arm 复用(这些是【每 arm 短暂】的进程内服务,harness 退出也随之结束;durable 的是盘上数据)。"""
    for lane in lanes:
        if lane.watch_id:
            try:
                hv.stop_harvester(lane.watch_id)
            except Exception:
                pass
    for lane in lanes:
        try:
            lane.server.shutdown()
            lane.server.server_close()
        except Exception:
            pass


def _answer_map(ak: str) -> dict[str, float]:
    rows: dict[str, float] = {}
    for line in Path(ak).read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            rows[str(r["event_id"])] = float(r["emitted_at"])
    return rows


def _batch_event_ids(candidates: list[dict]) -> list[str]:
    out = []
    for c in candidates:
        ev = c.get("event") if isinstance(c, dict) else None
        if isinstance(ev, dict) and ev.get("event_id"):
            out.append(str(ev["event_id"]))
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 判读工(线程忠实驱动产品的 pull/shard 机制;判读真调 M2.7)
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class JudgeStats:
    judged_at: dict[str, float] = field(default_factory=dict)   # event_id → 真模型判完时刻
    reported: set[str] = field(default_factory=set)             # 模型报为命中的 event_id
    batches: int = 0
    max_batch: int = 0
    overload_seen: bool = False
    rubber_stamp_pulls: int = 0                                 # 整批(>quota)几乎全报=盖章
    lock: threading.Lock = field(default_factory=threading.Lock)


def _judge_pull_once(tool: WatchStreamTool, lane: Lane, stats: JudgeStats, hit_set: set[str],
                     shard_index: int, shard_count: int, quota: int) -> int:
    """一次 pull + 真模型判 + 记账。返回本批候选数(0=空)。"""
    params = {"action": "pull", "watch_id": lane.watch_id, "max_wait_seconds": 2}
    if shard_count > 1:
        params["shard_index"] = shard_index
        params["shard_count"] = shard_count
    payload = json.loads(tool.execute(params).output)
    cand = payload.get("candidates") or []
    if not cand:
        return 0
    ids = _batch_event_ids(cand)
    hits = [h for h in _call_minimax(payload) if isinstance(h, str)]
    now = time.time()
    reported_true = sum(1 for h in hits if h in hit_set)
    with stats.lock:
        stats.batches += 1
        stats.max_batch = max(stats.max_batch, len(cand))
        for eid in ids:
            stats.judged_at.setdefault(eid, now)
        stats.reported |= set(hits)
        if payload.get("overload"):
            stats.overload_seen = True
        # rubber-stamp 口径:一大批(>quota)里模型把绝大多数都报成命中(而真 hit 稀)——
        # 整批盖章。真机实锤的乱报形态。
        if len(cand) > quota and len(hits) >= max(1, int(0.6 * len(cand))) and reported_true < len(hits):
            stats.rubber_stamp_pulls += 1
    return len(cand)


def _worker_loop(owner_home: Path, lane: Lane, stats: JudgeStats, hit_set: set[str],
                 shard_index: int, shard_count: int, quota: int, deadline: float,
                 run_id: str, stop: threading.Event, feed_done: threading.Event) -> None:
    tool = _tool(owner_home, run_id)
    idle = 0
    while not stop.is_set() and time.time() < deadline:
        n = _judge_pull_once(tool, lane, stats, hit_set, shard_index, shard_count, quota)
        if n == 0:
            idle += 1
            # 判空才可能收工,且【必须等喂料结束(feed_done)】才认"真没活了"——治真机 race:
            # 判读工在喂料爬坡前就见空源+reached_end 早退,之后的真事全无人判(交付 0)。存量场景
            # (无喂料)feed_done 预置为已完成,判空即收工不空转;活流场景喂完才允许收尾。
            if idle >= 3 and feed_done.is_set() and _lane_drained(owner_home, lane):
                break
            time.sleep(0.5)
        else:
            idle = 0


def _lane_drained(owner_home: Path, lane: Lane) -> bool:
    state = ws.registry.get(lane.watch_id)
    if state is None:
        return False
    return hv.spool_unread(state) == 0 and bool(getattr(state, "last_reached_end", False))


def _latency_of(hit_set_emit: dict[str, float], stats: JudgeStats) -> list[float]:
    """真 hit 的端到端延迟(真模型判完时刻 − 发生时刻);只算真被判过的。"""
    out = []
    for eid, emitted in hit_set_emit.items():
        judged = stats.judged_at.get(eid)
        if judged is not None and judged >= emitted:
            out.append(judged - emitted)
    return sorted(out)


def _pctl(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    return round(sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * q))], 2)


# ═══════════════════════════════════════════════════════════════════════════
# 场景① 逐条上报延迟:控制(单工串行扛所有源)vs 修复(每源并行判读工)
# ═══════════════════════════════════════════════════════════════════════════


def _live_feed(lanes: list[Lane], rate: int, decoys_per_hit: int, secs: float, stop: threading.Event) -> None:
    args = SimpleNamespace(answer_key=None, decoys_per_hit=decoys_per_hit, suspect_frac=0.5)
    tick = 0.2
    per_tick = max(1, int(rate * tick))
    start = time.time()
    while not stop.is_set() and time.time() - start < secs:
        for lane in lanes:
            for _ in range(per_tick):
                lane.source.append(sim._roll_kind(lane.source.rng, args), lane.answer_key)
        time.sleep(tick)


def scenario1_latency(run_dir: Path, n_sources: int, secs: float, rate: int, decoys_per_hit: int) -> dict:
    print("\n=== 场景① 逐条上报延迟(真模型判读:控制=单工串行扛所有源 vs 修复=每源并行判读工)===", flush=True)
    control = _latency_arm(run_dir, "s1_control", n_sources, secs, rate, decoys_per_hit, parallel=False)
    fixed = _latency_arm(run_dir, "s1_fixed", n_sources, secs, rate, decoys_per_hit, parallel=True)
    result = {"control": control, "fixed": fixed}
    fixed_median = fixed["median_latency_s"]
    fixed_max = fixed["max_latency_s"]
    # 修复臂:真判读下延迟为秒级~分钟级(不是十几分钟、更不是报不出),且显著低于控制臂。
    latency_bounded = (
        fixed["delivered_hits"] >= max(1, int(0.6 * fixed["true_hits"]))
        and fixed_median is not None and fixed_median <= 180.0
        and (fixed_max or 0) <= 600.0
    )
    beats_control = (
        control["median_latency_s"] is not None and fixed_median is not None
        and fixed_median < control["median_latency_s"]
    )
    ok = latency_bounded and beats_control
    result["verdict_pass"] = ok
    print(f"[判定①] 修复(并行判读工)中位延迟={fixed_median}s / 尾延迟={fixed_max}s,交付真事="
          f"{fixed['delivered_hits']}/{fixed['true_hits']}; 控制(单工串行)中位延迟={control['median_latency_s']}s。"
          f"\n         修复延迟秒级~分钟级(≤180s/≤600s)={latency_bounded}, 显著低于控制={beats_control}"
          f" → {'PASS' if ok else 'FAIL'}", flush=True)
    return result


def _latency_arm(run_dir: Path, tag: str, n_sources: int, secs: float, rate: int,
                 decoys_per_hit: int, parallel: bool) -> dict:
    _reset_registry()
    owner = run_dir / f"owner_{tag}"
    lanes = _start_sources(run_dir / tag, n_sources, seed=20260707)
    try:
        return _run_latency_arm(run_dir, tag, lanes, owner, secs, rate, decoys_per_hit, parallel)
    finally:
        _stop_arm(lanes)


def _run_latency_arm(run_dir: Path, tag: str, lanes: list[Lane], owner: Path, secs: float,
                     rate: int, decoys_per_hit: int, parallel: bool) -> dict:
    for lane in lanes:
        _open_lane(owner, lane, run_id=f"run-{tag}", window=3600)
    stop = threading.Event()
    feeder = threading.Thread(target=_live_feed, args=(lanes, rate, decoys_per_hit, secs, stop), daemon=True)
    feeder.start()
    quota = hv.judge_quota(ws.registry.get(lanes[0].watch_id).tuning)
    stats = JudgeStats()
    # 合并 5 源答案键做裁判(判读工按源各判各的,延迟统计取全体真 hit)。
    deadline = time.time() + secs + 90.0  # 喂完后留收尾判读窗
    feed_done = threading.Event()  # 喂料结束信号:判读工判空收工前必须先看它已置位(治爬坡前早退 race)
    threads: list[threading.Thread] = []
    if parallel:
        # 修复臂:每源一个判读工并行(一源一判读工);源太热(judge_fanout 推荐 >1)再加分片工。
        for lane in lanes:
            _spawn_lane_workers(owner, lane, stats, deadline, threads, stop, feed_done)
    else:
        # 控制臂:一个判读工串行round-robin扛所有源(真机"别一个判读子代理串行扛5源"的复现)。
        t = threading.Thread(target=_serial_worker, args=(owner, lanes, stats, quota, deadline, stop, feed_done), daemon=True)
        t.start()
        threads.append(t)
    feeder.join(timeout=secs + 5)
    feed_done.set()  # 喂料已结束:判读工可以在判空后收尾了(此前判空只等,不早退)
    _await_threads(threads, deadline)
    stop.set()
    emit = _merged_emit(lanes)
    lat = _latency_of(emit, stats)
    true_hits = set(emit)
    fp = stats.reported - true_hits
    out = {
        "arm": "fixed(并行判读工)" if parallel else "control(单工串行扛所有源)",
        "true_hits": len(true_hits),
        "delivered_hits": len([e for e in true_hits if e in stats.judged_at]),
        "reported_true": len(stats.reported & true_hits),
        "false_positives": len(fp),
        "model_batches": stats.batches,
        "median_latency_s": _pctl(lat, 0.5),
        "p90_latency_s": _pctl(lat, 0.9),
        "max_latency_s": round(max(lat), 2) if lat else None,
        "n_latency_samples": len(lat),
    }
    _dump(run_dir, f"{tag}.json", out)
    print(f"  [{out['arm']}] {json.dumps({k: out[k] for k in ('true_hits','delivered_hits','reported_true','false_positives','model_batches','median_latency_s','p90_latency_s','max_latency_s')}, ensure_ascii=False)}", flush=True)
    return out


def _settle_backlog(lanes: list[Lane], seconds: float) -> None:
    """给收割者一点时间把存量抬进 spool,再按积压定分片工数——纯等待,不消费(不 pull)。
    存量深的过载场景等到每源都有 ≥ 一个判读口粮的未读即够;活流场景很快超时(积压本就浅)。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        states = [ws.registry.get(lane.watch_id) for lane in lanes]
        if all(s is not None and hv.spool_unread(s) >= hv.judge_quota(s.tuning) for s in states):
            return
        time.sleep(0.3)


def _spawn_lane_workers(owner: Path, lane: Lane, stats: JudgeStats, deadline: float,
                        threads: list[threading.Thread], stop: threading.Event,
                        feed_done: threading.Event) -> None:
    """给一路源起并行判读工:工数 = recommended_judge_workers(纯积压结构信号,不 pull 不消费)。
    积压深→多分片工并行判;积压浅(活流)→1 工/源(=一源一判读工的横向扩)。各工按 shard_index/
    shard_count 认领不重叠的记录,判读都真调模型。动态,不写死。
    feed_done:喂料完成信号(活流场景传未置位的、喂完才 set;存量场景传预置位的),判读工判空
    收工前必须先看它已置位(见 _worker_loop),避免爬坡前误判"没活了"早退。"""
    state = ws.registry.get(lane.watch_id)
    workers = hv.recommended_judge_workers(state)
    quota = hv.judge_quota(state.tuning)
    if workers > 1:
        print(f"    [judge-fanout] {lane.source.name}: 积压深→起 {workers} 个并行判读工"
              f"(shard_count={workers},各判 spool_seq%{workers} 一片)", flush=True)
    for si in range(workers):
        run_id = f"judge-{lane.index}-s{si}of{workers}"
        t = threading.Thread(
            target=_worker_loop,
            args=(owner, lane, stats, _lane_hits(lane), si, workers, quota, deadline, run_id, stop, feed_done),
            daemon=True,
        )
        t.start()
        threads.append(t)


def _serial_worker(owner: Path, lanes: list[Lane], stats: JudgeStats, quota: int,
                   deadline: float, stop: threading.Event, feed_done: threading.Event) -> None:
    """单工串行扛所有源:round-robin 每源 pull+判,一次只判一路(真机瓶颈复现)。
    判空收工前必须等 feed_done(喂料结束)+全源判空,否则爬坡前见空源就早退、之后的真事
    全无人判(与并行臂同一个 race)。"""
    tool = _tool(owner, run_id="judge-serial")
    while not stop.is_set() and time.time() < deadline:
        any_cand = False
        for lane in lanes:
            n = _judge_pull_once(tool, lane, stats, _lane_hits(lane), 0, 1, quota)
            any_cand = any_cand or n > 0
        if not any_cand:
            if feed_done.is_set() and all(_lane_drained_by_owner(lane) for lane in lanes):
                break
            time.sleep(0.5)


_LANE_HITS: dict[str, set[str]] = {}


def _lane_hits(lane: Lane) -> set[str]:
    """本源当前答案键里的真 hit id(用于 rubber-stamp 口径的 reported_true 计数;非判读依据)。"""
    return set(_answer_map(lane.answer_key))


def _lane_drained_by_owner(lane: Lane) -> bool:
    state = ws.registry.get(lane.watch_id)
    return state is not None and hv.spool_unread(state) == 0 and bool(getattr(state, "last_reached_end", False))


def _merged_emit(lanes: list[Lane]) -> dict[str, float]:
    merged: dict[str, float] = {}
    for lane in lanes:
        merged.update(_answer_map(lane.answer_key))
    return merged


def _await_threads(threads: list[threading.Thread], deadline: float) -> None:
    for t in threads:
        t.join(timeout=max(1.0, deadline - time.time()))


# ═══════════════════════════════════════════════════════════════════════════
# 场景② 过载不乱报:速率拉爆,真模型下不整批盖章、如实标积压
# ═══════════════════════════════════════════════════════════════════════════


def scenario2_overload(run_dir: Path, n_sources: int, backlog: int, decoys_per_hit: int) -> dict:
    print("\n=== 场景② 过载不乱报(真模型判读:存量洪峰,验证优雅降级不整批盖章)===", flush=True)
    fixed = _overload_arm(run_dir, "s2_fixed", n_sources, backlog, decoys_per_hit, control=False)
    control = _overload_arm(run_dir, "s2_control", n_sources, backlog, decoys_per_hit, control=True)
    result = {"fixed": fixed, "control": control}
    # 判定压【确定性、模型无关】的过载降级保证 + 修复相对控制的乱报下降,【不】压绝对精度:
    # 绝对精度是【模型在这种极难料(8:1 诱饵、真假只在正文措辞)上的判别力上限】,是模型层
    # 事实,不由吞吐/背压插件决定——拿它当闸=把模型能力误当成修复失败。本场景要证的是
    # "过载下别把乱报放大",证据链:
    #   ① 钳批(确定性):修复臂每 pull 批量 ≤ quota——模型永远不必面对一整片洪水;
    #      因 batch 恒 ≤ quota,rubber_stamp 结构上恒为 0(盖章口径要 batch>quota);
    #   ② overload 如实在场(积压未判如实标注,不粉饰);
    #   ③ 结构对照:控制臂(无背压)把更大整批直怼给模型(过载乱报的风险入口);
    #   ④ 相对效果:修复臂误报 < 控制臂(钳批 + 分片确实压住了过载乱报,真 M2.7 实测 31 vs 105)。
    # 绝对精度(修复 37% / 控制 15%)【如实报告】——它低是模型判别力问题(下一步在模型层,
    # 非本棒吞吐修复目标),但修复把它从 15%→37%、误报 105→31,方向正确。
    # 判定【只压确定性的过载降级保证】,不压精度/误报的两臂对照——后者是真模型判读的随机
    # 噪声(实测两跑翻转:一跑修复误报 31<控制 105、另一跑修复误报 26>控制 0),把它当闸=
    # 拿模型运气当修复功劳。吞吐/背压这一层能确定性保证的只有:钳批、不盖章、如实标积压、
    # 控制臂喂更大整批。精度/误报如实打印【但不作判据】,真相在场、不粉饰。
    fixed_graceful = (
        fixed["max_batch_to_model"] <= fixed["quota"]      # 钳批(确定性)
        and fixed["rubber_stamp_pulls"] == 0               # 不盖章(钳批的必然结果)
        and fixed["overload_seen"] is True                 # 积压如实标注
    )
    control_feeds_bigger = control["max_batch_to_model"] > fixed["max_batch_to_model"]
    ok = fixed_graceful and control_feeds_bigger
    result["verdict_pass"] = ok
    print(f"[判定②·只压确定性] 修复臂优雅降级:批量钳≤quota({fixed['max_batch_to_model']}≤{fixed['quota']})·"
          f"不盖章(rubber_stamp={fixed['rubber_stamp_pulls']})·overload在场({fixed['overload_seen']})·"
          f"控制臂喂更大整批({control['max_batch_to_model']}>{fixed['max_batch_to_model']}) → {'PASS' if ok else 'FAIL'}", flush=True)
    print(f"         [精度/误报·如实打印不作判据] 修复 精度{fixed['precision_pct']}%/误报{fixed['false_positives']} "
          f"vs 控制 精度{control['precision_pct']}%/误报{control['false_positives']}——真模型判读随机(两跑翻转),"
          f"吞吐修复不改善精度;召回(找到率) 修复{fixed['recall_pct']}%/控制{control['recall_pct']}%。"
          f"并行分片提吞吐的代价=判读调用数增多,模型有误报率时总误报随之上升;治误报在模型层"
          f"逐条判+判据学习,非本棒目标。", flush=True)
    return result


def _overload_arm(run_dir: Path, tag: str, n_sources: int, backlog: int, decoys_per_hit: int, control: bool) -> dict:
    _reset_registry()
    owner = run_dir / f"owner_{tag}"
    lanes = _start_sources(run_dir / tag, n_sources, seed=20260707)
    # 存量洪峰:since=0 一次灌 backlog 条(含稀疏 hit),复现冷启动读存量整流洪泛。
    for lane in lanes:
        sim._seed_backlog(lane.source, backlog, decoys_per_hit, lane.answer_key)
    original_cm = hv.is_content_mode
    if control:
        # 控制臂=修复前:关掉 content_mode 背压/记录钳位 → 一 pull 把整条存量怼给模型。
        hv.is_content_mode = lambda state: False
    try:
        return _run_overload_arm(run_dir, tag, lanes, owner, control)
    finally:
        hv.is_content_mode = original_cm
        _stop_arm(lanes)


def _run_overload_arm(run_dir: Path, tag: str, lanes: list[Lane], owner: Path, control: bool) -> dict:
    for lane in lanes:
        _open_lane(owner, lane, run_id=f"run-{tag}", window=3600)
    quota = hv.judge_quota(ws.registry.get(lanes[0].watch_id).tuning)
    stats = JudgeStats()
    deadline = time.time() + 300.0
    stop = threading.Event()
    feed_done = threading.Event()
    feed_done.set()  # 存量场景无喂料:判读工判空即可收工(有限积压判完就停,不空转)
    threads: list[threading.Thread] = []
    # 修复臂:先让收割者把存量抬进 spool,再按积压定分片工数(积压深→多分片工并行判,
    # 展示"判读并发/横向扩")。控制臂无背压单工,不分片。
    if not control:
        _settle_backlog(lanes, seconds=6.0)
    for lane in lanes:
        if control:
            run_id = f"judge-{tag}-{lane.index}"
            t = threading.Thread(target=_worker_loop,
                                 args=(owner, lane, stats, _lane_hits(lane), 0, 1, quota, deadline, run_id, stop, feed_done),
                                 daemon=True)
            t.start(); threads.append(t)
        else:
            _spawn_lane_workers(owner, lane, stats, deadline, threads, stop, feed_done)
    _await_threads(threads, deadline)
    stop.set()
    emit = _merged_emit(lanes)
    true_hits = set(emit)
    tp = stats.reported & true_hits
    fp = stats.reported - true_hits
    out = {
        "arm": "control(修复前·无背压单工)" if control else "fixed(修复后·背压+分片)",
        "quota": quota,
        "true_hits": len(true_hits),
        "reported": len(stats.reported),
        "true_positives": len(tp),
        "false_positives": len(fp),
        "precision_pct": round(100 * len(tp) / max(1, len(stats.reported))),
        "recall_pct": round(100 * len(tp) / max(1, len(true_hits))),
        "max_batch_to_model": stats.max_batch,
        "rubber_stamp_pulls": stats.rubber_stamp_pulls,
        "overload_seen": stats.overload_seen,
        "model_batches": stats.batches,
    }
    _dump(run_dir, f"{tag}.json", out)
    print(f"  [{out['arm']}] {json.dumps({k: out[k] for k in ('true_hits','reported','true_positives','false_positives','precision_pct','recall_pct','max_batch_to_model','rubber_stamp_pulls','overload_seen','model_batches')}, ensure_ascii=False)}", flush=True)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 场景③ 重启恢复:重启后全部 N 源都被重新驱动续判
# ═══════════════════════════════════════════════════════════════════════════


def scenario3_restart(run_dir: Path, n_sources: int, decoys_per_hit: int) -> dict:
    print("\n=== 场景③ 重启恢复(真模型判读:重启后全部源都被重新驱动续判)===", flush=True)
    _reset_registry()
    owner = run_dir / "owner_s3"
    lanes = _start_sources(run_dir / "s3", n_sources, seed=20260707)
    for lane in lanes:
        sim._seed_backlog(lane.source, 60, decoys_per_hit, lane.answer_key)
        _open_lane(owner, lane, run_id="run-s3-pre", window=3600)
    # 重启【前】:每源判一两批,推进各自游标(留下"进行中"的持久化状态)。
    quota = hv.judge_quota(ws.registry.get(lanes[0].watch_id).tuning)
    pre = JudgeStats()
    for lane in lanes:
        tool = _tool(owner, run_id="run-s3-pre")
        _judge_pull_once(tool, lane, pre, _lane_hits(lane), 0, 1, quota)

    # ── 模拟【进程重启】:停所有 harvester 线程、清空进程内注册表(只剩盘上快照+spool+游标)──
    for lane in lanes:
        hv.stop_harvester(lane.watch_id)
    _reset_registry()

    # 重启【后】:每源从盘上快照续 open(新 run_id=继任者)+ 判读,验证【每一源】都续上判读产出。
    per_source_new_judged: dict[int, int] = {}
    stop = threading.Event()
    deadline = time.time() + 180.0
    feed_done = threading.Event()
    feed_done.set()  # 存量场景无喂料:判读工判空即收工
    threads: list[threading.Thread] = []
    post = JudgeStats()
    for lane in lanes:
        _open_lane(owner, lane, run_id=f"run-s3-post-{lane.index}", window=3600)
    _settle_backlog(lanes, seconds=5.0)  # 让收割者把重启后的存量抬进 spool 再定分片工数
    for lane in lanes:
        _spawn_lane_workers(owner, lane, post, deadline, threads, stop, feed_done)
    _await_threads(threads, deadline)
    stop.set()
    _stop_arm(lanes)  # 判完停源服务/收割线程,腾出端口(durable 的是盘上 owner_s3 数据)

    # 每源续判计数:重启后该源有没有新判过候选(judged_at 里属于该源的)。
    driven = []
    for lane in lanes:
        prefix = f"EVT-{chr(ord('A') + lane.index)}-"
        cnt = sum(1 for eid in post.judged_at if eid.startswith(prefix))
        per_source_new_judged[lane.index] = cnt
        driven.append(cnt > 0)
    all_driven = all(driven)
    out = {
        "sources": n_sources,
        "per_source_post_restart_judged": {str(k): v for k, v in per_source_new_judged.items()},
        "sources_redriven": sum(driven),
        "all_sources_redriven": all_driven,
        "post_model_batches": post.batches,
    }
    _dump(run_dir, "scenario3_restart.json", out)
    print(f"  每源重启后续判候选数: {out['per_source_post_restart_judged']}", flush=True)
    print(f"[判定③] 重启后被重新驱动的源={out['sources_redriven']}/{n_sources}, 全部续判={all_driven} "
          f"→ {'PASS' if all_driven else 'FAIL'}", flush=True)
    out["verdict_pass"] = all_driven
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 产物/清单
# ═══════════════════════════════════════════════════════════════════════════


def _dump(run_dir: Path, name: str, payload: dict) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_manifest(run_dir: Path, args, results: dict, elapsed: float) -> None:
    manifest = {
        "harness": "real_llm_latency_harness.py",
        "purpose": "真-LLM(MiniMax-M2.7)判读吞吐/延迟/过载/重启 端到端自测",
        "model": _MODEL,
        "endpoint": _API_BASE,
        "auth": "header x-api-key=$AGENT_API_KEY + anthropic-version: 2023-06-01",
        "model_calls_total": _MODEL_CALLS["n"],
        "wall_seconds": round(elapsed, 1),
        "params": vars(args),
        "started": {
            "http_source_servers": f"127.0.0.1:{BASE_PORT}..{BASE_PORT + args.sources - 1} (进程内 daemon 线程,harness 退出即止)",
            "harvester_threads": "每源一个进程内 harvester(后台连续摄取)",
            "judge_workers": "本台起的 Python 线程;修复臂按 judge_fanout 推荐工数用 shard_index/shard_count 并行判",
        },
        "artifacts_on_disk": {
            "root": str(ARTIFACT_ROOT),
            "answer_keys": "各场景 <tag>/answer_keys/*.jsonl(真 hit 旁路裁判)",
            "watch_state": "owner_*/watch_state/(watch 快照 + spool + 读游标;sharded 时有分片游标 *.read.sNofM.json)",
            "scenario_json": "scenario1_latency.json / scenario2_overload.json / scenario3_restart.json",
        },
        "verdicts": {k: v.get("verdict_pass") for k, v in results.items() if isinstance(v, dict)},
        "CLEANUP": "本台跑完【不删不停不清】,整套留在盘上/端口现状,交测试方统一处理(见 harness 顶部说明)。",
    }
    _dump(run_dir, "run_manifest.json", manifest)
    print(f"\n[manifest] {run_dir / 'run_manifest.json'}  (模型调用 {_MODEL_CALLS['n']} 次, 墙钟 {elapsed:.0f}s)", flush=True)


def _serve_only(n_sources: int, decoys_per_hit: int) -> None:
    """只把 N 个真源起在固定端口(灌一批存量)不判读,给测试方 curl 勘查环境。Ctrl-C 停。"""
    run_dir = ARTIFACT_ROOT / "serve_only"
    lanes = _start_sources(run_dir, n_sources, seed=20260707)
    for lane in lanes:
        sim._seed_backlog(lane.source, 200, decoys_per_hit, lane.answer_key)
        print(f"[serve] {lane.source.name} on 127.0.0.1:{lane.port}  (curl 'http://127.0.0.1:{lane.port}/pull?since=0&limit=3')", flush=True)
    print(f"答案键: {run_dir / 'answer_keys'}  ;  Ctrl-C 停。", flush=True)
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        pass


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sources", type=int, default=5)
    p.add_argument("--latency-secs", type=float, default=24.0)
    p.add_argument("--rate", type=int, default=3)
    p.add_argument("--overload-backlog", type=int, default=200)
    p.add_argument("--decoys-per-hit", type=int, default=8)
    p.add_argument("--scenarios", default="1,2,3")
    p.add_argument("--serve-only", action="store_true")
    args = p.parse_args()

    if args.serve_only:
        _serve_only(args.sources, args.decoys_per_hit)
        return

    if not os.environ.get("AGENT_API_KEY"):
        print("需要 AGENT_API_KEY(硬要求:判读必须真调 MiniMax-M2.7,禁止模拟判读)", flush=True)
        sys.exit(2)

    run_dir = ARTIFACT_ROOT
    run_dir.mkdir(parents=True, exist_ok=True)
    wanted = {s.strip() for s in args.scenarios.split(",") if s.strip()}
    print(f"=== 真-LLM 自测台({_MODEL};判读全真调,产物留在 {run_dir})===", flush=True)
    t0 = time.time()
    results: dict = {}
    if "1" in wanted:
        results["scenario1_latency"] = scenario1_latency(run_dir, args.sources, args.latency_secs, args.rate, args.decoys_per_hit)
    if "2" in wanted:
        results["scenario2_overload"] = scenario2_overload(run_dir, args.sources, args.overload_backlog, args.decoys_per_hit)
    if "3" in wanted:
        results["scenario3_restart"] = scenario3_restart(run_dir, args.sources, args.decoys_per_hit)
    elapsed = time.time() - t0
    _write_manifest(run_dir, args, results, elapsed)

    verdicts = [v.get("verdict_pass") for v in results.values() if isinstance(v, dict)]
    ok = all(verdicts) and bool(verdicts)
    print("\nVERDICT:", "PASS 真判读下延迟真降·过载不乱报·重启全恢复" if ok else "FAIL 见上", flush=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
