#!/usr/bin/env python3
"""根因1/2/3 确定性引擎自测:把 content_source_simulator 的料喂进【真引擎管线】
(WatchStreamTool 的 open→configure→pull inline 路,走 StreamDigestEngine + cold_start +
passthrough + 候选渲染),量【引擎抓取率】=真 hit 被 surface 到模型眼前的比例。

复测头号结论是"漏的 15 件里 14 件是引擎从没 surface"——所以这一层量的就是 surface 召回,
它不需要 LLM、确定性可复现,直接坐实根因1/2/3 的修没修好。报准(洞2)靠真 LLM 另测。

对照多套"模型学出的 spec",证明:
  A 旧-response-normal:判据配在响应端(共享词汇 normal 压制)→ 复现低召回(hit 被压)。
  B 旧-status-normal :判据配在 status=200(全 200)→ 几乎全灭(全被当常态)。
  C 请求端窄化(默认名额8):normal 配 routine、suspect 走 outside_normal → 高压比下 hit
     挤爆 spec 名额落 overflow → 仍漏(证明"光narrow不够,名额cap会吃掉hit")。
  D passthrough(本棒修):每条都递、无名额cap → suspect 全 surface → 满召回。
  E 冷启动无spec(本棒修):存量批 cold_start 无条件全读 → 存量 hit 全 surface。

用法: python3 scripts/watch_harness/content_engine_harness.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

# 让 harness 能 import agent 包(与仓库测试同源路径):repo 根(agent_py_agent.* 全限定)
# + agent_py_agent/(agent.* 短名)两条都要,深层 import 链两种写法都用到。
_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "agent_py_agent"))

from agent.ingestion import watch_state as ws  # noqa: E402
from agent.ingestion import watch_tool as wt  # noqa: E402
from agent.ingestion.watch_tool import WatchStreamTool  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import content_source_simulator as sim  # noqa: E402


def _source_handle(source: sim.SourceState):
    def handle(request):
        url = request.url
        q = parse_qs(urlsplit(url).query)
        since = int((q.get("since") or ["0"])[0] or 0)
        limit = max(1, min(500, int((q.get("limit") or ["50"])[0] or 50)))
        return True, source.pull(since, limit), ""
    return handle


def _fresh_tool(owner_home: Path, source, run_id: str) -> WatchStreamTool:
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-selftest"),
        _current_run_params=SimpleNamespace(run_id=run_id),
    )
    tool = WatchStreamTool(agent)
    tool.allow_private_resolution = True
    tool._fetch_json = _source_handle(source)
    return tool


def _drain_all(tool: WatchStreamTool, watch_id: str, max_pulls: int) -> list[dict]:
    """inline 反复 pull 直到追平流尾(或到 max_pulls),收集所有候选行。"""
    rows: list[dict] = []
    for _ in range(max_pulls):
        res = tool.execute({"action": "pull", "watch_id": watch_id, "max_wait_seconds": 0})
        payload = json.loads(res.output)
        rows.extend(payload.get("candidates") or [])
        if payload.get("coverage", {}).get("reached_stream_end"):
            break
    return rows


def _event_id_of(row: dict) -> str:
    ev = row.get("event") or {}
    if isinstance(ev, dict):
        return str(ev.get("event_id") or "")
    return ""


def _open_inline(tool: WatchStreamTool, url: str) -> dict:
    """Open with host-owned tuning, without reviving a model-facing switch."""

    original_new_state = wt.new_state

    def _new_state(
        owner_home: Path,
        source_url: str,
        params: dict[str, object],
        *,
        watch_id: str = "",
    ):
        state = original_new_state(
            owner_home,
            source_url,
            params,
            watch_id=watch_id,
        )
        tuning = replace(state.tuning, background_harvest=0)
        state.tuning = tuning
        state.engine.tuning = tuning
        return state

    wt.new_state = _new_state
    try:
        result = tool.execute({"action": "open", "url": url})
    finally:
        wt.new_state = original_new_state
    return json.loads(result.output)


def _run_config(name: str, spec: dict | None, sources, answer) -> dict:
    """跑一套 spec 配置:每源一个隔离 tool(一源一 run),open→(configure)→drain,收候选。"""
    ws.registry = ws.WatchRegistry()  # 隔离注册表
    wt.registry = ws.registry
    tmp = Path(tempfile.mkdtemp(prefix=f"selftest-{name}-"))
    surfaced: set[str] = set()
    surfaced_hits: set[str] = set()
    candidate_total = 0
    for i, source in enumerate(sources):
        # 每个 config 用各自隔离 tool 从 since=0 重读同一份静态 ring(源不再追加)。
        tool = _fresh_tool(tmp, source, run_id=f"run-{name}-{i}")
        port = 8911 + i
        opened = _open_inline(
            tool,
            f"http://127.0.0.1:{port}/pull?since=<next>&limit=<limit>",
        )
        wid = opened["watch_id"]
        if spec is not None:
            cfg = tool.execute({"action": "configure", "watch_id": wid, "spec": spec})
            assert json.loads(cfg.output).get("ok"), cfg.output
        rows = _drain_all(tool, wid, max_pulls=200)
        candidate_total += len(rows)
        for row in rows:
            eid = _event_id_of(row)
            if not eid:
                continue
            surfaced.add(eid)
            if eid in answer["hit_ids"]:
                surfaced_hits.add(eid)
    total_hits = len(answer["hit_ids"])
    recall = len(surfaced_hits) / total_hits if total_hits else 0.0
    return {
        "config": name,
        "surface_recall": round(recall, 3),
        "hits_surfaced": len(surfaced_hits),
        "hits_total": total_hits,
        "candidates_surfaced": candidate_total,
    }


def _build_sources(n: int, backlog: int, decoys_per_hit: int, live_ticks: int, seed: int):
    """构造 n 源:各灌 backlog 存量,再补一段活流(含按比例 hit)。答案键收集所有 hit id。"""
    specs = sim._build_specs()[:n]
    sources = [sim.SourceState(specs[i], seed=seed + i * 101) for i in range(n)]
    answer_path = Path(tempfile.mkdtemp(prefix="selftest-ans-")) / "answer_key.jsonl"
    answer_path.write_text("", encoding="utf-8")
    ak = str(answer_path)
    args = SimpleNamespace(answer_key=ak, decoys_per_hit=decoys_per_hit, suspect_frac=0.5)
    for source in sources:
        sim._seed_backlog(source, backlog, decoys_per_hit, ak)
    # 活流补一段(每源每 tick 若干条),让 hit 也出现在"配好判据之后"的活流里。
    for _ in range(live_ticks):
        for source in sources:
            for _ in range(20):
                source.append(sim._roll_kind(source.rng, args), ak)
    hit_ids = set()
    for line in answer_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            hit_ids.add(json.loads(line)["event_id"])
    return sources, {"hit_ids": hit_ids, "path": ak}


def main() -> None:
    N = 5
    # 每源 backlog 存量 + 活流;decoys_per_hit 用 60(比 420 小,单机跑得快但仍是高压比,
    # 足以让"名额cap吃掉hit"和"共享词汇压制hit"暴露)。真机跑用 420。
    sources, answer = _build_sources(N, backlog=800, decoys_per_hit=60, live_ticks=8, seed=20260707)
    print(f"[data] {N} 源, 真 hit 总数={len(answer['hit_ids'])} (存量+活流)", flush=True)

    # A 旧-response-normal:配在响应端,normal 用与 hit 共享的词汇(change/request/action/policy)
    #    → hit 命中 normal 被压;target 用真 hit 里不出现的词(captured/confirmed)→ 命中 0。
    cfg_a = {
        "result_field": "response.message",
        "normal_value_contains": ["change", "request", "action", "policy", "processed", "operation"],
        "target_value_contains": ["captured", "confirmed", "settled"],
        "ignore_fields": ["seq", "ts", "event_id"],
    }
    # B 旧-status-normal:配在 status,全 200 都当常态 → 全灭。
    cfg_b = {"result_field": "response.status", "normal_values": ["200"], "ignore_fields": ["seq", "ts", "event_id"]}
    # C 请求端窄化(默认 spec 名额 8):routine 是常态、suspect 特权动作走 outside_normal。
    #    高压比下 suspect 每拍成百上千,8 个名额 → hit 大概率落 overflow。
    cfg_c = {"result_field": "request.action",
             "normal_values": ["healthcheck", "list", "query", "scrape"],
             "ignore_fields": ["seq", "ts", "event_id", "response"]}
    # D 纯 passthrough(本棒修):无名额cap、每条都递(含 benign)→ 满召回但量最大。
    cfg_d = {"passthrough": True, "ignore_fields": ["seq", "ts", "event_id"]}
    # D2 passthrough + 请求端窄化(本棒修·实战最优):benign(routine)normal 减负、suspect 全递,
    #    passthrough 让 _select_full_read 不吃 spec 名额cap → suspect 满召回、量比纯 passthrough 小。
    cfg_d2 = {"passthrough": True, "result_field": "request.action",
              "normal_values": ["healthcheck", "list", "query", "scrape"],
              "ignore_fields": ["seq", "ts", "event_id", "response"]}
    # E 冷启动无 spec(本棒修):不 configure,靠 cold_start 无条件全读存量。
    cfg_e = None

    results = [
        _run_config("A_response_normal", cfg_a, sources, answer),
        _run_config("B_status_normal", cfg_b, sources, answer),
        _run_config("C_request_narrow_cap8", cfg_c, sources, answer),
        _run_config("D_passthrough_FIX", cfg_d, sources, answer),
        _run_config("D2_passthrough_narrow_FIX", cfg_d2, sources, answer),
        _run_config("E_coldstart_nospec_FIX", cfg_e, sources, answer),
    ]
    print("\n=== 引擎 surface 召回对照(真 hit 被抬到模型眼前的比例)===", flush=True)
    print(f"{'config':<30}{'surface_recall':>16}{'hits':>12}{'candidates':>14}")
    for r in results:
        print(f"{r['config']:<30}{r['surface_recall']:>16}{str(r['hits_surfaced'])+'/'+str(r['hits_total']):>12}{r['candidates_surfaced']:>14}")
    # 断言修法有效:D/D2/E 满召回;旧配置(A 响应端共享词汇压制 ~复现32% / B status全200当常态 /
    # C 请求窄化但名额cap吃掉hit)都明显漏。
    by = {r["config"]: r for r in results}
    fixes_full = all(by[c]["surface_recall"] >= 0.99 for c in
                     ("D_passthrough_FIX", "D2_passthrough_narrow_FIX", "E_coldstart_nospec_FIX"))
    old_miss = all(by[c]["surface_recall"] < 0.5 for c in
                   ("A_response_normal", "B_status_normal", "C_request_narrow_cap8"))
    # D2 应比纯 D 量小(窄化把 benign 减负掉)——证明"narrow+passthrough"是量/召回双赢的实战配置。
    d2_leaner = by["D2_passthrough_narrow_FIX"]["candidates_surfaced"] < by["D_passthrough_FIX"]["candidates_surfaced"]
    ok = fixes_full and old_miss and d2_leaner
    print(f"\n[量对比] 纯passthrough={by['D_passthrough_FIX']['candidates_surfaced']} 候选 vs "
          f"passthrough+窄化={by['D2_passthrough_narrow_FIX']['candidates_surfaced']} 候选(窄化把 benign 减负)", flush=True)
    print("VERDICT:", "PASS 修法 surface 满召回 & 旧配置复现漏报 & 窄化减量" if ok else "FAIL 见上", flush=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
