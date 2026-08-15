#!/usr/bin/env python3
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_env_loader import ensure_model_key
"""洞2 报准·真 LLM 判读探针:引擎已把候选 surface 到模型眼前后,模型读正文语义判真假的准头。

确定性引擎 harness 证明了"引擎 surface 满召回";这一层用【真模型】(MiniMax-M2.7,与产品同源)
量报准——重点是洞2 的硬迷惑项:status 都 200、正文说没生效(被降级/被后置拦下/回滚/no-op)。
对照两套判读引导:
  OLD:极简"判断哪些成功了" → 诱导只看状态码 → 复现"200 就报成功"的误报。
  NEW:本棒改的 PULL_GUIDANCE(读全正文语义、否定语义一票否决、event_id 取自本条)。
量:precision(误报率)/recall/event_id 串号。跑真模型、非确定,给出实测数字。

用法(需 AGENT_API_KEY):python3 scripts/watch_harness/content_judge_probe.py [--rounds 2]
"""


import argparse
import json
import os
import random
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import content_source_simulator as sim  # noqa: E402

_API_BASE = "https://api.minimaxi.com/anthropic/v1/messages"
_MODEL = "MiniMax-M2.7"

# 从产品真源码取 PULL_GUIDANCE 的判读段(证明测的就是上线的引导,不是另写一套)。
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent_py_agent"))
from agent.ingestion.watch_payloads import PULL_GUIDANCE  # noqa: E402

_OLD_GUIDANCE = (
    "下面是盯守初筛抬上来的候选事件。逐条看看,把命中/成功的挑出来上报 event_id。"
)

# 洞2 硬迷惑:status/结构字段【全都撒谎说成功】(status=200 + result=success + ok=true),
# 真假只在 message——decoy 的 message【先扬后抑】(开头肯定语气"successfully/completed",
# 半句后才否定"but reverted/blocked/no-op/not in effect"),诱导只看状态码/开头就报成功。
_HARD_HIT_MSG = [
    "operation successful — the privileged change is applied and now active on every node, serving live traffic",
    "completed successfully; new state committed to primary and confirmed in effect across the fleet",
    "success: elevated grant took effect immediately and the caller is now operating with the new scope",
    "done — export finished and the rows were delivered outside the boundary as requested",
    "succeeded: the firewall rule is live at the edge and already enforcing on new connections",
]
_HARD_DECOY_MSG = [
    "operation successful — however the change was reverted by the downstream policy guard and is not in effect",
    "completed successfully at the gateway, but blocked one layer deeper on commit check; effect was dropped",
    "success: request authorized and logged — yet enforcement denied activation, so nothing actually changed",
    "done — export was queued then quarantined by DLP; ultimately no rows crossed the boundary",
    "succeeded in staging only; the rule stayed in shadow mode and never enforced on any connection",
    "operation successful; the privileged effect was silently down-graded to a no-op after the ack",
    "completed and acknowledged to the client, but rolled back on the post-commit guard; old state stands",
]


def _hard_event(src: sim.SourceState, kind: str) -> dict:
    """造一条洞2 硬迷惑事件:结构字段全说成功,真假只在 message 语义。"""
    priv = sim._PRIVILEGED[0]
    ip = src.rng.choice(sim._PROBLEM_IPS)
    ref = f"{src.rng.randint(0, 0xFFFFFFFF):08x}"
    msg = src.rng.choice(_HARD_HIT_MSG if kind == "hit" else _HARD_DECOY_MSG)
    event_id = f"EVT-A-{src.seq:07d}"
    src.seq += 1
    # 结构端一律"看着像成功":status=200 + result=success + ok=true(hit/decoy 完全一样)。
    return {
        "event_id": event_id,
        "src_ip": ip,
        "request": dict(priv),
        "response": {"status": 200, "result": "success", "ok": True,
                     "message": f"{msg} (change_ref={ref})"},
    }


def _build_batch(rng: random.Random, n_hit: int, n_decoy: int, n_benign: int) -> tuple[list[dict], set[str]]:
    """造一批已 surface 的候选(引擎的活已干完):n_hit 真得手 + n_decoy 硬迷惑(结构端全说成功、
    只 message 语义否定)+ n_benign 良性。返回 (候选行, 真得手 event_id 集合)。"""
    src = sim.SourceState(sim._build_specs()[0], seed=rng.randint(1, 10**9))
    rows: list[dict] = []
    truth: set[str] = set()
    plan = ["hit"] * n_hit + ["decoy"] * n_decoy + ["benign"] * n_benign
    rng.shuffle(plan)
    for kind in plan:
        if kind == "benign":
            src.append("benign", os.devnull)
            event = {k: src.ring[-1][k] for k in ("event_id", "src_ip", "request", "response")}
        else:
            event = _hard_event(src, kind)
        eid = event["event_id"]
        if kind == "hit":
            truth.add(eid)
        rows.append({
            "stream_pos": int(re.search(r"(\d+)$", eid).group(1)),
            "event": event,
            "triage": {"reason": "full_stream_read", "full_read": True},
        })
    rng.shuffle(rows)
    return rows, truth


def _call_model(system: str, user: str, max_tokens: int = 8000) -> str:
    body = json.dumps({
        "model": _MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")
    req = urllib.request.Request(_API_BASE, data=body, method="POST", headers={
        "content-type": "application/json",
        "x-api-key": ensure_model_key(),
        "anthropic-version": "2023-06-01",
    })
    with urllib.request.urlopen(req, timeout=180) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    texts = [b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text"]
    return "\n".join(texts)


def _parse_reported(text: str) -> set[str] | None:
    """严格只认最后一个 REPORT:[...] 的 JSON 数组(模型被要求最后一行只输出它)。
    找不到/坏 JSON → 返回 None(标记本轮不可解析,绝不兜底抓正文里所有 EVT id——那会把模型
    在推理里【讨论过的迷惑项】也算成"上报",凭空制造误报,是测量假象)。"""
    matches = re.findall(r"REPORT\s*:\s*(\[[^\]]*\])", text)
    if not matches:
        return None
    try:
        return {str(x) for x in json.loads(matches[-1])}
    except json.JSONDecodeError:
        return None


def _score(reported: set[str], truth: set[str], all_ids: set[str]) -> dict:
    reported = reported & all_ids  # 只认真实存在的 id(防幻觉/串号计进分子)
    tp = len(reported & truth)
    fp = len(reported - truth)
    fn = len(truth - reported)
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": round(precision, 3), "recall": round(recall, 3),
            "reported": len(reported)}


def _run_variant(name: str, guidance: str, rows: list[dict], truth: set[str], all_ids: set[str]) -> dict:
    system = (
        "你是盯守研判子代理。引擎已把结构初筛后的候选事件批递到你眼前(每条含唯一 event_id 和完整"
        "事件)。你的任务:逐条判断哪些是【真正得手/生效】的要紧事,只上报真得手的 event_id。\n\n"
        + guidance
        + "\n\n【输出格式·硬要求】你可以先简短分析,但【最后一行必须且只能是】一行:"
        "REPORT:[\"<event_id>\", ...] —— 一个 JSON 数组,只放你判定为【真得手】的 event_id,"
        "判定为没得手/良性的一律不要放进去(没有真得手就写 REPORT:[])。这一行之后不要再写任何字。"
    )
    user = "候选事件批(逐条判):\n" + json.dumps(rows, ensure_ascii=False, indent=1)
    text = _call_model(system, user)
    reported = _parse_reported(text)
    if reported is None:
        # 保存原始输出供人看,不凭空打分。
        dbg = Path(os.environ.get("PROBE_DEBUG_DIR", ".")) / f"probe_unparsed_{name}.txt"
        dbg.write_text(text, encoding="utf-8")
        return {"config": name, "unparsed": True, "raw_saved": str(dbg)}
    sc = _score(reported, truth, all_ids)
    sc["config"] = name
    return sc


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--rounds", type=int, default=2)
    p.add_argument("--hits", type=int, default=5)
    p.add_argument("--decoys", type=int, default=18)
    p.add_argument("--benign", type=int, default=5)
    args = p.parse_args()
    rng = random.Random(20260707)

    agg: dict[str, list[dict]] = {"OLD_minimal": [], "NEW_pull_guidance": []}
    for r in range(args.rounds):
        rows, truth = _build_batch(rng, args.hits, args.decoys, args.benign)
        all_ids = {row["event"]["event_id"] for row in rows}
        print(f"\n[round {r+1}] batch={len(rows)} (hit={len(truth)} decoy={args.decoys} benign={args.benign})", flush=True)
        for name, guidance in (("OLD_minimal", _OLD_GUIDANCE), ("NEW_pull_guidance", PULL_GUIDANCE)):
            try:
                sc = _run_variant(name, guidance, rows, truth, all_ids)
            except Exception as exc:  # noqa: BLE001 - 探针要把网络/解析错如实打出来
                print(f"  {name}: ERROR {exc}", flush=True)
                continue
            if sc.get("unparsed"):
                print(f"  {name:<20} UNPARSED(无 REPORT 行,原始存 {sc['raw_saved']})", flush=True)
                continue
            agg[name].append(sc)
            print(f"  {name:<20} P={sc['precision']} R={sc['recall']} "
                  f"tp={sc['tp']} fp={sc['fp']} fn={sc['fn']} reported={sc['reported']}", flush=True)

    print("\n=== 汇总(均值,只计可解析轮)===", flush=True)
    for name, runs in agg.items():
        if not runs:
            print(f"{name:<20} (无可解析轮)", flush=True)
            continue
        n = len(runs)
        P = round(sum(x["precision"] for x in runs) / n, 3)
        R = round(sum(x["recall"] for x in runs) / n, 3)
        fp = sum(x["fp"] for x in runs)
        print(f"{name:<20} 均值 P={P} R={R} 累计误报fp={fp} (rounds={n})", flush=True)


if __name__ == "__main__":
    main()
