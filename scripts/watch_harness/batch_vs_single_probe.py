#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_env_loader import ensure_model_key

"""批判读 vs 逐条判读 精度对照探针(判读全真调 MiniMax-M2.7)。

回答一个命门:延迟/过载自测台里修复臂那 ~37% 的绝对精度、几十条误报,到底是
【产品判得差】还是【自测台一次塞 ≤48 条给模型批判读、注意力被摊薄】?

产品真机里候选是交给盯守子代理、按 PULL_GUIDANCE【逐条重判】的(记忆实锤:单条直喂
模型往往判得准)。本探针拿【同一批中性料】,两种判法各真调 M2.7,直接比 precision/recall:
  · batch  —— 一次调用把整批候选喂给模型(=延迟/过载台的判法);
  · single —— 每条一次调用、单独判(=产品逐条判的判法)。

若 single 精度显著高于 batch → 误报虚高是【批判读 artifact】,产品逐条判无此问题,
   且自测台该改成小批/逐条判才测得真;若 single 也低 → 是料/判据的模型层真短板。

用法(需 AGENT_API_KEY):
  AGENT_API_KEY=... python3 scripts/watch_harness/batch_vs_single_probe.py --events 30
"""


import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "agent_py_agent"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import content_source_simulator as sim  # noqa: E402

_API_BASE = "https://api.minimaxi.com/anthropic/v1/messages"
_MODEL = "MiniMax-M2.7"

_SYSTEM = (
    "你在盯守一个数据源,逐条判读它推来的候选事件。这个源 response.status 恒为 200"
    "(成败绝不看状态码),真得手 vs 没得手【只能读 response.message 的自然语言语义】——"
    "正文说【已生效/正在服务/持久化落地/已提交/真的送出去了】才算真得手;说【被降级/被后置"
    "一层拦下/暂存未生效/回滚/no-op/影子模式/超额丢弃/未实际入账/没跨过边界】即使 200 也是"
    "没得手,不算命中。请求内容真假两类几乎一样,别看请求端、只按结果端正文语义判。"
    "逐条独立判,拿不准的按存疑【不报】。"
)
_ASK = (
    "\n\n上面是候选批(每条带 event.event_id 和 event.response.message)。逐条判读哪些是"
    "【真得手】。只输出一个 JSON:{\"hits\":[\"EVT-...\"]},hits 放你判定为真得手的 event_id"
    "(判为没得手/存疑的不放)。不要输出别的。"
)

_CALLS = {"n": 0}


def _call(candidates: list[dict], note: str) -> list[str]:
    _CALLS["n"] += 1
    body = {
        "model": _MODEL,
        "max_tokens": 8000,
        "system": _SYSTEM,
        "messages": [{"role": "user", "content": f"数据源判据: 真假只在 response.message 正文语义。\n"
                      f"candidates: {json.dumps(candidates, ensure_ascii=False)}" + _ASK}],
    }
    req = urllib.request.Request(
        _API_BASE, data=json.dumps(body).encode(),
        headers={"x-api-key": ensure_model_key(), "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode())
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            m = re.search(r"\{[^{}]*\"hits\"\s*:\s*\[[^\]]*\][^{}]*\}", text, re.S)
            if m:
                try:
                    return [str(x) for x in (json.loads(m.group(0)).get("hits") or [])]
                except json.JSONDecodeError:
                    pass
            return re.findall(r"EVT-[A-Z]-\d{7}", text)
        except Exception as exc:  # noqa: BLE001
            if attempt == 2:
                print(f"    [call failed {note}] {exc}", flush=True)
                return []
            time.sleep(3.0 * (attempt + 1))
    return []


def _build(n_events: int, decoys_per_hit: int, seed: int):
    """造一批中性料事件 + 真 hit 答案键(取 ring 里的事件当候选)。"""
    source = sim.SourceState(sim._build_specs()[0], seed=seed)
    ak = str(Path(tempfile.mkdtemp(prefix="bvs-")) / "ak.jsonl")
    Path(ak).write_text("", encoding="utf-8")
    sim._seed_backlog(source, n_events, decoys_per_hit, ak)
    hit_set = {json.loads(l)["event_id"] for l in Path(ak).read_text().splitlines() if l.strip()}
    events = [dict(e) for e in source.ring]
    candidates = [{"event": {"event_id": e["event_id"], "src_ip": e.get("src_ip"),
                             "request": e.get("request"), "response": e.get("response")}}
                  for e in events]
    return candidates, hit_set


def _score(reported: set[str], hit_set: set[str]) -> dict:
    tp = reported & hit_set
    fp = reported - hit_set
    return {
        "reported": len(reported), "true_positives": len(tp), "false_positives": len(fp),
        "precision_pct": round(100 * len(tp) / max(1, len(reported))),
        "recall_pct": round(100 * len(tp) / max(1, len(hit_set))),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--events", type=int, default=30)
    p.add_argument("--decoys-per-hit", type=int, default=6)
    p.add_argument("--seed", type=int, default=20260707)
    args = p.parse_args()
    if not ensure_model_key():
        print("需要 AGENT_API_KEY(禁模拟判读)", flush=True)
        sys.exit(2)

    candidates, hit_set = _build(args.events, args.decoys_per_hit, args.seed)
    print(f"=== 批判读 vs 逐条判读 对照({_MODEL})===", flush=True)
    print(f"料: {len(candidates)} 条候选, 其中真 hit {len(hit_set)} 条(答案键旁路)", flush=True)

    # batch:一次调用整批(= 延迟/过载台判法)
    batch_reported = set(_call(candidates, "batch"))
    batch = _score(batch_reported, hit_set)
    print(f"[batch 一次判整批] {json.dumps(batch, ensure_ascii=False)}", flush=True)

    # single:每条一次调用(= 产品逐条判)
    single_reported: set[str] = set()
    for c in candidates:
        eid = c["event"]["event_id"]
        hits = _call([c], f"single:{eid}")
        if eid in hits:
            single_reported.add(eid)
    single = _score(single_reported, hit_set)
    print(f"[single 逐条判] {json.dumps(single, ensure_ascii=False)}", flush=True)

    print(f"\n[结论] 精度 batch={batch['precision_pct']}% → single={single['precision_pct']}%;"
          f" 误报 batch={batch['false_positives']} → single={single['false_positives']};"
          f" 召回 batch={batch['recall_pct']}% → single={single['recall_pct']}%。"
          f" 模型调用 {_CALLS['n']} 次。", flush=True)
    verdict = ("逐条判显著更准 → 误报虚高是【批判读 artifact】,产品逐条判无此问题,自测台应改小批/逐条"
               if single["precision_pct"] >= batch["precision_pct"] + 15
               else "逐条判未显著更准 → 是料/判据的模型层短板,需喂样品学判据(非批判读 artifact)")
    print(f"[判读] {verdict}", flush=True)


if __name__ == "__main__":
    main()
