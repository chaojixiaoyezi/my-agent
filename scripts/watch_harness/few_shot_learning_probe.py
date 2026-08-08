#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_env_loader import ensure_model_key
"""判读误报根因探针:裸判 vs 喂样品学习 vs 产品级判读纪律(判读全真调 MiniMax-M2.7)。

前一个探针(batch_vs_single)证了:批判读 11% / 逐条判 13%,精度都低——【不是批判读 artifact】。
但军规:判不准先问"有没有先喂样品让 agent 学"再研判,禁裸判就下"模型判不动"结论。上个探针
是【裸判】(只给一句 schema 说明),而产品链路是 sample→学判据→带 judgment_note + 大段 PULL_GUIDANCE
判读纪律。本探针把同一批中性料,用三种"喂得多寡不同"的判读上下文各真调 M2.7,隔离根因:
  · A 裸判(cold)     —— 只给一句结果端判据说明(= 上个探针、也是最坏情况);
  · B 喂样品(few-shot)—— 额外给 6 条带答案的示范(3 真得手 / 3 没得手,示范正文语义边界);
  · C 产品级(guided) —— few-shot + 一段贴近产品 PULL_GUIDANCE 的判读纪律(读全正文语义、
                          否定/未竟语义即使 200 也一票否决、拿不准存疑不报)。
若 C 精度显著高于 A → 误报根因=【判读没喂够上下文/没学料】,产品 learn 链路能救,裸判低估精度;
若 C 仍低 → 才是 M2.7 在"正文语义否定"这类料上的真判别力短板(下一步在判据学习/更强初筛)。

用法(需 AGENT_API_KEY):
  AGENT_API_KEY=... python3 scripts/watch_harness/few_shot_learning_probe.py --events 30 --decoys-per-hit 3
"""

from __future__ import annotations

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
_CALLS = {"n": 0}

_SCHEMA = ("数据源判据: response.status 恒 200(成败不看状态码),真得手 vs 没得手【只能读 "
           "response.message 的自然语言语义】。请求内容真假两类几乎一样,别看请求端。")

# 贴近产品 PULL_GUIDANCE 的判读纪律(语义读法 + 否定一票否决 + 存疑不报)。
_GUIDANCE = (
    "判「得手/生效」必须读全响应正文语义,不是只看状态码:很多迷惑项状态码像成功,正文里却说"
    "这次操作实际未生效/被改写/被降级/被后置一层拦下/被回滚/影子模式/超额丢弃/未实际入账/没跨过"
    "边界——读到这类否定或未竟语义时,即使状态码成功也一票否决判【未得手】。反过来,正文明确说"
    "目标已达成(已生效/正在服务/持久化落地/已提交/真送出去了)才算命中。逐条独立判,正文自相"
    "矛盾或真读不出成没成的按存疑【不报】——宁可漏报也别把『尝试了但没成』的当命中。"
)

_ASK = ("\n逐条判读哪些是【真得手】。只输出一个 JSON:{\"hits\":[\"EVT-...\"]},hits 放真得手的 "
        "event_id(没得手/存疑的不放)。不要输出别的。")


def _fewshot_block() -> str:
    """从措辞库取 3 真 3 假做带标签示范(与料同分布、但示范用固定代表句)。"""
    hits = sim._HIT_PHRASES[:3]
    decoys = sim._DECOY_PHRASES[:3]
    lines = ["判读示范(学会正文语义边界,再判下面的候选):"]
    for p in hits:
        lines.append(f"  正文『{p}』→ 真得手(命中)")
    for p in decoys:
        lines.append(f"  正文『{p}』→ 没得手(不报:说了成功但实际被中和/回滚/未生效)")
    return "\n".join(lines)


def _call(system: str, user: str, note: str) -> list[str]:
    _CALLS["n"] += 1
    body = {"model": _MODEL, "max_tokens": 8000, "system": system,
            "messages": [{"role": "user", "content": user}]}
    req = urllib.request.Request(
        _API_BASE, data=json.dumps(body).encode(),
        headers={"x-api-key": ensure_model_key(), "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
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
    source = sim.SourceState(sim._build_specs()[0], seed=seed)
    ak = str(Path(tempfile.mkdtemp(prefix="fsl-")) / "ak.jsonl")
    Path(ak).write_text("", encoding="utf-8")
    sim._seed_backlog(source, n_events, decoys_per_hit, ak)
    hit_set = {json.loads(l)["event_id"] for l in Path(ak).read_text().splitlines() if l.strip()}
    candidates = [{"event": {"event_id": e["event_id"], "src_ip": e.get("src_ip"),
                             "request": e.get("request"), "response": e.get("response")}}
                  for e in source.ring]
    return candidates, hit_set


def _score(reported: set[str], hit_set: set[str]) -> dict:
    tp = reported & hit_set
    return {"reported": len(reported), "tp": len(tp), "fp": len(reported - hit_set),
            "precision_pct": round(100 * len(tp) / max(1, len(reported))),
            "recall_pct": round(100 * len(tp) / max(1, len(hit_set)))}


def _arm(name: str, system: str, prefix: str, candidates: list[dict], hit_set: set[str]) -> dict:
    user = f"{prefix}\ncandidates: {json.dumps(candidates, ensure_ascii=False)}{_ASK}"
    rep = set(_call(system, user, name))
    sc = _score(rep, hit_set)
    print(f"[{name}] {json.dumps(sc, ensure_ascii=False)}", flush=True)
    return sc


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--events", type=int, default=30)
    p.add_argument("--decoys-per-hit", type=int, default=3)
    p.add_argument("--seed", type=int, default=20260707)
    p.add_argument("--repeat", type=int, default=2, help="每臂重复几次取平均(压模型随机)")
    args = p.parse_args()
    if not ensure_model_key():
        print("需要 AGENT_API_KEY(禁模拟判读)", flush=True)
        sys.exit(2)

    candidates, hit_set = _build(args.events, args.decoys_per_hit, args.seed)
    print(f"=== 判读误报根因:裸判 vs 喂样品 vs 产品级纪律({_MODEL})===", flush=True)
    print(f"料: {len(candidates)} 条候选, 真 hit {len(hit_set)} 条; 每臂跑 {args.repeat} 次取平均", flush=True)

    arms = {
        "A裸判": (_SCHEMA, _SCHEMA),
        "B喂样品": (_SCHEMA, _SCHEMA + "\n" + _fewshot_block()),
        "C产品级纪律": (_SCHEMA + "\n" + _GUIDANCE, _SCHEMA + "\n" + _GUIDANCE + "\n" + _fewshot_block()),
    }
    avg: dict[str, dict] = {}
    for name, (system, prefix) in arms.items():
        runs = [_arm(f"{name}#{i+1}", system, prefix, candidates, hit_set) for i in range(args.repeat)]
        avg[name] = {
            "precision_pct": round(sum(r["precision_pct"] for r in runs) / len(runs)),
            "recall_pct": round(sum(r["recall_pct"] for r in runs) / len(runs)),
            "fp": round(sum(r["fp"] for r in runs) / len(runs), 1),
        }

    print("\n[平均] " + " | ".join(
        f"{n}: 精度{avg[n]['precision_pct']}%/召回{avg[n]['recall_pct']}%/误报{avg[n]['fp']}" for n in arms), flush=True)
    a, c = avg["A裸判"]["precision_pct"], avg["C产品级纪律"]["precision_pct"]
    verdict = ("产品级纪律+样品显著提精度 → 误报根因=判读上下文没喂够(裸判低估精度),产品 learn 链路能救"
               if c >= a + 15 else
               "喂够上下文精度仍低 → M2.7 在此类正文否定语义料上确有判别力短板,下一步=判据学习/更强初筛/更强判读模型")
    print(f"[判读] A裸判 {a}% → C产品级 {c}%;{verdict}。模型调用 {_CALLS['n']} 次。", flush=True)


if __name__ == "__main__":
    main()
