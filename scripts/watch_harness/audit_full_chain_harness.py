#!/usr/bin/env python3
"""H —— /audit 保证档【真产品全链路】自测台(真 MiniMax-M2.7,禁模拟)。

治 A–G 盲区:A–G 的 audit_guarantee_harness 直接 set guarantee + 聚焦单调判读、灌固定量,【绕过】了
真 harvester 抬取和真判读管道,所以 A–G 全绿但真产品召回崩。本台走真路径量端到端:

  发出(源) → 入队(真 harvester 抬取,含排他型游标源) → 判读(真产品 pull 路内置的聚焦判读) → 上报

覆盖两个真产品 gap(受控台照不出、真产品才现形):
  H1 全量入队(Gap 2):保证档下发出的记录全部进 durable 队列、丢弃 0,且【不因源把 since 当排他而
     每隔一条丢一条】——包含型 + 排他型两种游标源都要一条不落。
  H2 真判读召回(Gap 1):真判读管道端到端召回贴齐【直喂模型天花板】。三口径真调 M2.7:
     · 天花板 direct_feed:每条真事单独直喂(测试方所指"直喂判得出"的基准);
     · 修复路 focused:每批候选另起无历史聚焦调用(产品 pull 路现已内置的同款);
     · 控制 accumulated:复刻旧路的累积会话判读(诚实注:干净合成【欠估】真机严重度,见 honesty_note)。

自包含:源是进程内游标缓冲(可切包含/排他),harvester 经注入 fetch_json 走【真 drain/re-anchor/spool】
逻辑(与真 HTTP 源同一条抬取路);判读真调 MiniMax-M2.7(api_base/key/model 同产品配)。缺 AGENT_API_KEY
直接退(sys.exit 2),绝不模拟。数字如实落 run_manifest.json。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "agent_py_agent"))
sys.path.insert(0, str(_REPO))

from agent.backends.base import AnthropicCompatibleBackend, BackendOptions  # noqa: E402
from agent.ingestion import harvester as hv  # noqa: E402
from agent.ingestion import watch_state as ws  # noqa: E402
from agent.ingestion.audit_judge import _FOCUS_SYSTEM, _parse_focus_verdicts  # noqa: E402
from agent.ingestion.watch_tool import WatchStreamTool  # noqa: E402

_API_BASE = "https://api.minimaxi.com/anthropic"
_MODEL = "MiniMax-M2.7"
_ART = _REPO / "data" / "audit_full_chain_harness"
_BATCH = 6
_DRAIN_DEADLINE = 40.0
_SOURCE_NOTE = "安全日志:真假只能读 request(触发端)+ response(结果端)判——得手 vs 被拦/失败/噪音。"

# 合成安全事件模板(通用形状,明显假 ID):真得手 response 明写生效/泄露;噪声/未遂 response 明写被拦/失败。
_REAL_TEMPLATES = [
    ("POST /users/1001 role=admin", "200 OK {\"id\":1001,\"role\":\"admin\"} (privilege escalated, took effect)"),
    ("GET /export?table=users", "200 OK 4211 rows incl password_hash column dumped (bulk exfil succeeded)"),
    ("POST /profile {\"__proto__\":{\"isAdmin\":true}}", "200 OK {\"user\":\"joe\",\"isAdmin\":true} (prototype polluted, admin granted)"),
    ("POST /reset email=victim@x.io Host:attacker.test", "200 OK reset link sent to attacker.test host (token leaked to attacker)"),
    ("GET /redir?u=%0d%0aSet-Cookie:sess=att", "302 Found Set-Cookie:sess=att (CRLF injected, session fixed)"),
    ("POST /graphql {users{passwordHash}}", "200 OK {\"users\":[{\"passwordHash\":\"deadbeef00\"}]} (sensitive field exposed)"),
]
_NOISE_TEMPLATES = [
    ("GET /search?q=laptop", "200 OK (normal search results)"),
    ("GET /.git/config", "403 Forbidden (probe blocked, nothing exposed)"),
    ("POST /login user=bob", "401 Unauthorized (bad credentials, no access)"),
    ("GET /admin", "302 Found -> /login (unauthenticated, redirected)"),
    ("POST /users/2 role=admin", "403 Forbidden (authz denied, change rejected)"),
    ("GET /export?table=users", "429 Too Many Requests (rate limited, no data returned)"),
    ("POST /reset email=a@x.io", "200 OK reset link sent to registered address (normal)"),
    ("GET /health", "200 OK ok"),
]


# ── 进程内游标源(可切包含/排他)——harvester 经注入 fetch_json 走真 drain 逻辑 ──────────

class MemSource:
    def __init__(self, exclusive: bool) -> None:
        self.exclusive = exclusive
        self.events: list[dict] = []

    def emit(self, request: str, response: str, real: bool) -> str:
        seq = len(self.events) + 1
        eid = f"EVT-{seq:05d}"
        self.events.append({"seq": seq, "event_id": eid, "request": request, "response": response, "_real": real})
        return eid

    def pull(self, since: int, limit: int) -> dict:
        keep = [e for e in self.events if (e["seq"] > since if self.exclusive else e["seq"] >= since)]
        items = [{k: v for k, v in e.items() if k != "_real"} for e in keep[:limit]]
        nxt = (items[-1]["seq"] + 1) if items else max(since, len(self.events))
        return {"items": items, "next_cursor": nxt}


def _fetch_from(src: MemSource):
    def fetch(url: str) -> tuple[bool, object, str]:
        from urllib.parse import parse_qs, urlsplit

        q = parse_qs(urlsplit(url).query)
        since = int(q.get("since", ["0"])[0])
        limit = int(q.get("limit", ["400"])[0])
        return True, src.pull(since, limit), ""

    return fetch


def _emit_stream(src: MemSource, n: int, real_positions: set[int]) -> set[str]:
    """按位次注入 n 条(真事在 real_positions,其余噪声/未遂);返回真事 event_id 集合(答案键)。"""
    real_ids: set[str] = set()
    for i in range(1, n + 1):
        if i in real_positions:
            req, resp = _REAL_TEMPLATES[i % len(_REAL_TEMPLATES)]
            real_ids.add(src.emit(req, resp, True))
        else:
            req, resp = _NOISE_TEMPLATES[i % len(_NOISE_TEMPLATES)]
            src.emit(req, resp, False)
    return real_ids


# ── 真模型后端 / 判读调用 ──────────────────────────────────────────────────────────────

def _backend():
    key = os.environ.get("AGENT_API_KEY")
    if not key:
        print("[abort] 缺 AGENT_API_KEY——本台判读必须真调 MiniMax-M2.7,禁模拟。", flush=True)
        sys.exit(2)
    opts = BackendOptions(
        api_base=_API_BASE, api_key=key, model_name=_MODEL, request_timeout=180,
        max_tokens=8000, context_window_tokens=200000, temperature=0.2, stream_enabled=False,
    )
    return AnthropicCompatibleBackend(opts, anthropic_version="2023-06-01")


def _agent(owner_home: Path, backend, run_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-hchain"),
        backend=backend, _current_subagent_run_id=run_id,
    )


def _call(backend, system: str, messages: list) -> str:
    try:
        return str(getattr(backend.generate(system, messages=messages), "text", "") or "")
    except Exception as exc:  # noqa: BLE001
        print(f"    [model call failed] {exc}", flush=True)
        return ""


def _verdict_word(text: str) -> str:
    m = re.search(r'"verdict"\s*:\s*"(hit|clear|unsure)"', text.lower())
    return m.group(1) if m else "clear"


# ── 判读三口径(都真调 M2.7)────────────────────────────────────────────────────────────

_ARM_A_SYS = (
    "你是安全值守判读子代理,长期盯守一路安全日志流。每轮 pull 给你候选(request+response),逐条判:"
    "真得手/命中=hit;攻击尝试但被拦/失败/降级/未跨边界=clear;真读不出=unsure。读 response 正文语义定,"
    "不是只看状态码。每轮只输出一行 JSON:{\"verdict\":\"hit|clear|unsure\",\"why\":\"..\"}。"
)


def _judge_batch(backend, system: str, rows: list[dict]) -> dict[str, str]:
    payload = [{"ack_id": r["ack_id"], "event": r["event"]} for r in rows]
    text = _call(backend, system, [{"role": "user", "content": "candidates: " + json.dumps(payload, ensure_ascii=False)}])
    parsed = _parse_focus_verdicts(text, {r["ack_id"] for r in rows})
    return {r["ack_id"]: parsed.get(r["ack_id"], {}).get("verdict", "clear") for r in rows}


def _caught_reals(rows: list[dict], real_ids: set[str], by_ack: dict[str, str]) -> set[str]:
    hit_acks = {a for a, v in by_ack.items() if v == "hit"}
    return {r["event"]["event_id"] for r in rows if r["ack_id"] in hit_acks and r["event"]["event_id"] in real_ids}


def _arm_focused(backend, rows: list[dict], real_ids: set[str]) -> set[str]:
    """修复路:每批 _BATCH 条另起无历史聚焦调用(产品 pull 路现已内置的同款)。"""
    system = _FOCUS_SYSTEM.format(source_note=_SOURCE_NOTE)
    by_ack: dict[str, str] = {}
    for i in range(0, len(rows), _BATCH):
        by_ack.update(_judge_batch(backend, system, rows[i:i + _BATCH]))
    return _caught_reals(rows, real_ids, by_ack)


def _arm_direct_feed(backend, rows: list[dict], real_ids: set[str]) -> set[str]:
    """天花板:每条真事【单独无上下文】直喂(测试方"直喂判得出"的基准)。"""
    system = _FOCUS_SYSTEM.format(source_note=_SOURCE_NOTE)
    reals = [r for r in rows if r["event"]["event_id"] in real_ids]
    by_ack: dict[str, str] = {}
    for r in reals:
        by_ack.update(_judge_batch(backend, system, [r]))
    return _caught_reals(reals, real_ids, by_ack)


def _clear_seed(rows: list[dict], real_ids: set[str]) -> list[dict]:
    """深 clear 累积上下文:非真候选(本就 clear)+ 空拉轮,复刻旧路"真事稀疏混在一片 clear 里"。"""
    seed: list[dict] = []
    for r in rows:
        if r["event"]["event_id"] in real_ids:
            continue
        seed.append({"role": "user", "content": "本轮 pull:无新候选,继续盯守。"})
        seed.append({"role": "assistant", "content": "收到,暂无新事件,继续 watch。"})
        seed.append({"role": "user", "content": "本轮 pull 候选: " + json.dumps(r["event"], ensure_ascii=False)})
        seed.append({"role": "assistant", "content": json.dumps({"verdict": "clear", "why": "被拦/失败,无事"}, ensure_ascii=False)})
    return seed


def _arm_accumulated(backend, rows: list[dict], real_ids: set[str]) -> set[str]:
    """控制:复刻旧路——每条真事放进一份深 clear 累积上下文判(诚实注:干净合成欠估真机严重度)。"""
    seed = _clear_seed(rows, real_ids)
    caught: set[str] = set()
    for r in rows:
        eid = r["event"]["event_id"]
        if eid not in real_ids:
            continue
        msgs = list(seed) + [{"role": "user", "content": "本轮 pull 候选: " + json.dumps(r["event"], ensure_ascii=False)}]
        if _verdict_word(_call(backend, _ARM_A_SYS, msgs)) == "hit":
            caught.add(eid)
    return caught


# ── 全链路装配 ────────────────────────────────────────────────────────────────────────

@dataclass
class LaneSpec:
    letter: str
    exclusive: bool
    n_events: int
    real_positions: set[int]
    judge: bool = True


def _open_audit_lane(tool: WatchStreamTool, run_id: str) -> str:
    # URL 只过网络安全闸(127.0.0.1 放行);真拉取走注入的 _fetch_json(进程内游标缓冲,不碰网络)。
    # 每路 URL 唯一(watch_id=hash(owner_home+url)),两路 lane 不撞同一份 watch 状态。
    opened = json.loads(tool.execute({
        "action": "open", "url": f"http://127.0.0.1:9/{run_id}", "audit": 1, "watch_window_seconds": 3600,
    }).output)
    assert opened.get("audit_guarantee") is True, "open 未进入保证档"
    wid = opened["watch_id"]
    tool.execute({"action": "configure", "watch_id": wid, "spec": {"passthrough": True, "result_field": "response"}})
    return wid


def _drain_rows(owner_home: Path, wid: str, expect: int) -> list[dict]:
    """真 harvester 抬取直到入队追平发出数;返回 spool 里全部候选行(ack_id+event)。"""
    state = ws.registry.get_or_load(owner_home, wid)
    deadline = time.time() + _DRAIN_DEADLINE
    while time.time() < deadline and int(state.totals.get("spool_candidates", 0) or 0) < expect:
        time.sleep(0.2)
    rows: list[dict] = []
    sp = hv.spool_path(state)
    if not sp.exists():
        return rows
    for line in sp.open():
        rows.extend({"ack_id": c.get("ack_id"), "event": c.get("event")} for c in (json.loads(line).get("candidates") or []))
    return rows


def _h1_facts(n_events: int, rows: list[dict], real_ids: set[str]) -> dict:
    enq_ids = {r["event"].get("event_id") for r in rows if r.get("event")}
    enq_real = real_ids & enq_ids
    return {
        "emitted": n_events, "enqueued": len(enq_ids), "emitted_real": len(real_ids),
        "enqueued_real": len(enq_real), "full_enqueue": len(enq_ids) == n_events,
        "_enq_real_ids": enq_real,
    }


def _h2_facts(backend, rows: list[dict], enq_real: set[str]) -> dict:
    ceiling = _arm_direct_feed(backend, rows, enq_real)
    focused = _arm_focused(backend, rows, enq_real)
    control = _arm_accumulated(backend, rows, enq_real)
    denom = max(1, len(enq_real))
    return {
        "enqueued_real": len(enq_real),
        "direct_feed_ceiling_recall": round(len(ceiling) / denom, 3),
        "arm_focused_recall": round(len(focused) / denom, 3),
        "arm_accumulated_control_recall": round(len(control) / denom, 3),
        "focused_caught": len(focused), "ceiling_caught": len(ceiling), "control_caught": len(control),
    }


def run_lane(backend, owner_home: Path, spec: LaneSpec) -> dict:
    src = MemSource(spec.exclusive)
    run_id = f"h-{spec.letter}-{'excl' if spec.exclusive else 'incl'}"
    tool = WatchStreamTool(_agent(owner_home, backend, run_id))
    tool.allow_private_resolution = True
    tool._fetch_json = _fetch_from(src)
    real_ids = _emit_stream(src, spec.n_events, spec.real_positions)
    wid = _open_audit_lane(tool, run_id)
    rows = _drain_rows(owner_home, wid, spec.n_events)
    hv.stop_harvester(wid)
    h1 = _h1_facts(spec.n_events, rows, real_ids)
    enq_real = h1.pop("_enq_real_ids")
    h2 = _h2_facts(backend, rows, enq_real) if spec.judge else None
    return {"lane": run_id, "exclusive": spec.exclusive, "H1": h1, "H2": h2}


def _print_lane(res: dict) -> None:
    h1 = res["H1"]
    print(f"  H1 入队: 发出{h1['emitted']} 入队{h1['enqueued']} 全量入队={h1['full_enqueue']} "
          f"真事入队{h1['enqueued_real']}/{h1['emitted_real']}", flush=True)
    if res["H2"]:
        h2 = res["H2"]
        print(f"  H2 判读召回(仅已入队真事,共{h2['enqueued_real']}): 天花板直喂={h2['direct_feed_ceiling_recall']}  "
              f"修复路聚焦={h2['arm_focused_recall']}  控制·旧路累积={h2['arm_accumulated_control_recall']}", flush=True)


def _verdict(lanes: list[dict]) -> dict:
    h1_pass = all(l["H1"]["full_enqueue"] for l in lanes)
    judged = [l for l in lanes if l["H2"]]
    focused = sum(l["H2"]["focused_caught"] for l in judged)
    ceiling = sum(l["H2"]["ceiling_caught"] for l in judged)
    control = sum(l["H2"]["control_caught"] for l in judged)
    total = sum(l["H2"]["enqueued_real"] for l in judged)
    h2_pass = total > 0 and focused >= ceiling and focused / total >= 0.8
    return {
        "H1_full_enqueue_both_cursor_semantics": h1_pass,
        "H2_direct_feed_ceiling_recall": round(ceiling / max(1, total), 3),
        "H2_focused_product_path_recall": round(focused / max(1, total), 3),
        "H2_accumulated_control_recall": round(control / max(1, total), 3),
        "H2_focused_matches_ceiling": bool(h2_pass),
        "PASS": bool(h1_pass and h2_pass),
    }


_HONESTY = (
    "H2 达标口径=修复路(聚焦)召回贴齐直喂天花板(端到端≈直喂模型)。控制臂(旧路累积)用干净合成种子"
    "【欠估】真机严重度:rut-rate 探针实测干净种子沟触发率低且不随深度单增,真机 60% 漏(入队真事 6/10 判"
    " clear)来自满载荷+compaction 的脏累积上下文。旧路权威红基线=真网关取证+隔离 rut 实验;修复路(每批"
    "干净上下文判读)对上述所有累积退化形态【构造上免疫】,故稳定贴顶。"
)


def main() -> None:
    backend = _backend()
    _ART.mkdir(parents=True, exist_ok=True)
    owner_home = _ART / "owner"
    n, reals = 30, {23, 25, 27, 29}
    print(f"=== H 全链路台(真 {_MODEL};判读全真调)n={n}/源 real={len(reals)} ===", flush=True)
    specs = [LaneSpec("A", False, n, reals, judge=True), LaneSpec("A", True, n, reals, judge=False)]
    lanes = []
    for spec in specs:
        tag = "排他型" if spec.exclusive else "包含型"
        print(f"\n[lane {tag}源] 真 harvester 抬取{'+三口径真判读' if spec.judge else '(仅测入队)'}…", flush=True)
        res = run_lane(backend, owner_home, spec)
        lanes.append(res)
        _print_lane(res)
    verdict = _verdict(lanes)
    manifest = {"model": _MODEL, "endpoint": _API_BASE, "n_per_source": n,
                "lanes": lanes, "verdict": verdict, "honesty_note": _HONESTY}
    out = _ART / "run_manifest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== 判定 ===\nH1 两种游标语义全量入队丢弃0: {verdict['H1_full_enqueue_both_cursor_semantics']}", flush=True)
    print(f"H2 端到端召回: 直喂天花板={verdict['H2_direct_feed_ceiling_recall']}  修复路聚焦="
          f"{verdict['H2_focused_product_path_recall']}  (控制·旧路累积={verdict['H2_accumulated_control_recall']})", flush=True)
    print(f"总判定 PASS={verdict['PASS']}\n[manifest] {out}", flush=True)
    sys.exit(0 if verdict["PASS"] else 1)


if __name__ == "__main__":
    main()
