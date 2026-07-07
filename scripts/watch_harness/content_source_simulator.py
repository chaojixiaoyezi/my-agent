#!/usr/bin/env python3
"""根因1 复测台:成败只藏在【响应正文自然语言语义】里的多源盯守模拟器(可复用)。

与 multi_source_simulator.py 的关键区别:那台的真假在【结构化字段】(session.established=
true、settlement.state=captured 之类布尔/枚举),子串 spec 能干净分开——是"旧台"。
本台按 DEV_HANDOFF_monitoring_retest_recall.md 洞1 造【更难】的料:

  · 每源 GET /pull?since=<游标>&limit=<n> → {items, next_cursor, schema_note}。
  · 每条事件含 event_id / src_ip / request / response;event_id 存在性不泄真假。
  · 三类人群:
      benign  —— 普通读流量(良性 IP、routine 请求),response 明显良性;非目标,占多数。
      decoy   —— 问题 IP 池 + 特权写请求,status=200,但 response.message 语义说【没得手】
                 (被降级/被后置策略拦下/暂存未生效/回滚…多种措辞轮换)。
      hit     —— 问题 IP 池 + 特权写请求,status=200,response.message 语义说【真得手】
                 (已生效/正在服务/持久化落地…多种措辞轮换)。=要紧事。
  · decoy:hit ≈ 420:1(问题 IP 上绝大多数是"没得手的尝试";蒙 IP 全报→精度崩)。
  · hit 与 decoy 的 request 几乎一样、status 都 200,【区别只在 response.message 语义】;
    message 都是"结论短语 + 唯一高基数尾巴(ref/耗时)",共享词汇(applied/policy/change),
    子串规则要么两个都中要么都不中——结构分不开,只能读正文语义判。
  · 真 hit 产生即写旁路 answer-key(事件流之外,盯守方够不着)。
  · --backlog N:开跑先灌 N 条存量(since=0 一大批,含按比例的 hit),复现"冷启动读存量"。

用法(默认 5 源、每源 ~40/s、20 分钟、每源 backlog 600、总 hits 目标由比例决定):
  python3 scripts/watch_harness/content_source_simulator.py \
      --base-port 8911 --sources 5 --rate 40 --duration 1200 \
      --backlog 600 --answer-key /tmp/content_answer_key.jsonl
冒烟: curl 'http://127.0.0.1:8911/pull?since=0&limit=3'
"""

from __future__ import annotations

import argparse
import json
import random
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

RING_CAPACITY = 200_000

# 问题 IP 池(固定):要紧事只从这里来,但这里 420:1 是没得手的尝试。
_PROBLEM_IPS = [f"203.0.113.{n}" for n in range(2, 42)]  # 40 个问题 IP
# 良性 IP:广撒,routine 流量。
def _benign_ip(rng: random.Random) -> str:
    return f"10.{rng.randint(0, 40)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"


# 5 源各有不同的特权写请求形状(异构),但同源内 hit 与 decoy 的 request 几乎一样。
_PRIVILEGED = [
    {"path": "/admin/config/apply", "action": "apply_privileged_change", "scope": "cluster"},
    {"path": "/iam/role/grant", "action": "escalate_role", "scope": "org"},
    {"path": "/data/export/run", "action": "bulk_export", "scope": "pii_table"},
    {"path": "/fw/rule/push", "action": "push_firewall_rule", "scope": "edge"},
    {"path": "/secrets/rotate", "action": "rotate_and_reveal", "scope": "prod_kv"},
]
_ROUTINE = [
    {"path": "/api/status", "action": "healthcheck"},
    {"path": "/api/orders", "action": "list"},
    {"path": "/api/search", "action": "query"},
    {"path": "/metrics", "action": "scrape"},
]

# ── 语义措辞库:hit 与 decoy 都是"结论短语 + 唯一尾巴",共享词汇、区别只在语义。──
# 多套措辞轮换 = 硬编码某几个子串会"过这套料换套就崩",逼判读读懂语义而非背字符串。
_HIT_PHRASES = [
    "change applied and now active on all nodes, serving live traffic",
    "privileged action completed; new state persisted and in effect",
    "request authorized and executed, mutation committed to primary",
    "grant took effect immediately; caller now holds the elevated scope",
    "export finished and delivered; rows left the boundary successfully",
    "rule is live at the edge and enforcing on the next connection",
    "secret rotated and the new value was revealed to the caller as requested",
]
_DECOY_PHRASES = [
    "change accepted but reverted by downstream policy guard, not applied",
    "request acknowledged; mutation staged then rolled back on commit check",
    "action authorized at gateway but blocked one layer deeper, no effect",
    "grant recorded in intent log only; enforcement layer denied activation",
    "export queued then quarantined by DLP; no rows crossed the boundary",
    "rule compiled and validated but shadow-mode only, never enforced",
    "rotation initiated then aborted; old secret still current, nothing revealed",
    "operation returned success to client while the effect was silently dropped",
    "processed and logged; the privileged effect was down-graded to a no-op",
]
_BENIGN_PHRASES = [
    "ok, served from cache",
    "healthy; all checks green",
    "200 rows returned to client",
    "query completed within slo",
    "metrics scraped, no anomalies",
]


class SourceSpec:
    def __init__(self, index: int, name: str, note: str):
        self.index, self.name, self.note = index, name, note


def _build_specs() -> list[SourceSpec]:
    names = ["control_plane", "iam_audit", "data_egress", "edge_fw", "secrets_kv"]
    return [
        SourceSpec(
            i, names[i % len(names)],
            "结果端判据: response.status 恒为 200(成败不看状态码);真得手 vs 没得手【只能读 "
            "response.message 的自然语言语义】——正文说'已生效/正在服务/持久化落地'才是真得手,"
            "说'被降级/被后置拦下/暂存未生效/回滚/no-op'即使 200 也是没得手。请求内容真假两类几乎一样。",
        )
        for i in range(5)
    ]


class SourceState:
    def __init__(self, spec: SourceSpec, seed: int):
        self.index = spec.index
        self.name = spec.name
        self.note = spec.note
        self.rng = random.Random(seed)
        self.ring: deque[dict] = deque(maxlen=RING_CAPACITY)
        self.seq = 0
        self.lock = threading.Lock()
        self.hit_count = 0
        self.decoy_count = 0

    def _make_event(self, kind: str) -> dict:
        priv = _PRIVILEGED[self.index % len(_PRIVILEGED)]
        if kind == "hit":
            ip = self.rng.choice(_PROBLEM_IPS)
            req = dict(priv)
            phrase = self.rng.choice(_HIT_PHRASES)
        elif kind == "decoy":
            ip = self.rng.choice(_PROBLEM_IPS)
            req = dict(priv)  # 与 hit 几乎一样的请求
            phrase = self.rng.choice(_DECOY_PHRASES)
        else:  # benign
            ip = _benign_ip(self.rng)
            req = dict(self.rng.choice(_ROUTINE))
            phrase = self.rng.choice(_BENIGN_PHRASES)
        # message = 结论短语 + 唯一高基数尾巴(ref + 耗时):整值高基数(字面失明),
        # 尾巴让每条都不同,语义在短语里、结构记号摸不到。
        ref = f"{self.rng.randint(0, 0xFFFFFFFF):08x}"
        latency = self.rng.randint(3, 900)
        message = f"{phrase} (change_ref={ref} took_ms={latency})"
        event = {
            "src_ip": ip,
            "request": req,
            "response": {"status": 200, "message": message},
        }
        return event

    def append(self, kind: str, answer_key_path: str) -> None:
        now = time.time()
        event_id = f"EVT-{chr(ord('A') + self.index)}-{self.seq:07d}"
        event = self._make_event(kind)
        event["event_id"] = event_id
        event["seq"] = self.seq
        event["ts"] = round(now, 3)
        with self.lock:
            self.ring.append(event)
            self.seq += 1
        if kind == "hit":
            self.hit_count += 1
            _append_answer_key(answer_key_path, {
                "event_id": event_id, "source": self.name, "port_index": self.index,
                "emitted_at": now, "seq": self.seq - 1,
                "note": f"true hit #{self.hit_count} on {self.name}",
            })
        elif kind == "decoy":
            self.decoy_count += 1

    def pull(self, since: int, limit: int) -> dict:
        with self.lock:
            items = [dict(e) for e in self.ring if int(e.get("seq", -1)) >= since][:limit]
            next_cursor = (int(items[-1]["seq"]) + 1) if items else max(since, self.seq)
        return {
            "api": self.name,
            "schema_note": self.note,
            "items": items,
            "returned": len(items),
            "next_cursor": next_cursor,
        }


_ANSWER_LOCK = threading.Lock()


def _append_answer_key(path: str, row: dict) -> None:
    with _ANSWER_LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _make_handler(source: SourceState):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parts = urlsplit(self.path)
            if parts.path == "/status":
                return self._reply({"ok": True, "source": source.name, "seq": source.seq,
                                    "hits": source.hit_count, "decoys": source.decoy_count})
            if parts.path == "/pull":
                query = parse_qs(parts.query)
                since = int((query.get("since") or ["0"])[0] or 0)
                limit = max(1, min(500, int((query.get("limit") or ["50"])[0] or 50)))
                return self._reply(source.pull(since, limit))
            return self._reply({"error": "unknown", "paths": ["/status", "/pull?since=&limit="]}, 404)

        def _reply(self, payload: dict, code: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a) -> None:  # 静默
            pass

    return Handler


def _seed_backlog(source: SourceState, n: int, decoys_per_hit: int, answer_key: str) -> None:
    """开跑前灌 n 条存量(since=0 的一大批):按 decoys_per_hit 比例掺 hit,其余 benign/decoy。
    存量里也埋 hit——复现'冷启动读存量时判据没学出来就把存量要紧事放走'。"""
    for _ in range(n):
        r = source.rng.random()
        if r < 1.0 / (decoys_per_hit + 1) * 0.5:   # 存量里 hit 稀疏(约 decoys_per_hit:1 的一半密度)
            kind = "hit"
        elif r < 0.5:
            kind = "decoy"
        else:
            kind = "benign"
        source.append(kind, answer_key)


def _feeder(sources: list[SourceState], args, stop: threading.Event) -> None:
    started = time.time()
    tick = 0.1
    per_tick = max(1, int(args.rate * tick))
    # 活流里 hit 的出现概率:让 decoy:hit ≈ args.decoys_per_hit(问题 IP 上)。
    # 每 tick 每源约 per_tick 条,其中一部分是问题 IP 特权流(suspect),suspect 内 420:1。
    while not stop.is_set() and time.time() - started < args.duration:
        for source in sources:
            for _ in range(per_tick):
                source.append(_roll_kind(source.rng, args), args.answer_key)
        time.sleep(tick)
    total_hits = sum(s.hit_count for s in sources)
    total_decoys = sum(s.decoy_count for s in sources)
    print(f"[feeder] done +{time.time()-started:.0f}s hits={total_hits} decoys={total_decoys}", flush=True)


def _roll_kind(rng: random.Random, args) -> str:
    """活流事件类型:suspect_frac 概率是问题 IP 特权流(suspect),其内 1/(decoys_per_hit+1) 是 hit;
    其余是 benign。"""
    if rng.random() < args.suspect_frac:
        return "hit" if rng.random() < 1.0 / (args.decoys_per_hit + 1) else "decoy"
    return "benign"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-port", type=int, default=8911)
    p.add_argument("--sources", type=int, default=5)
    p.add_argument("--rate", type=int, default=40, help="每源每秒事件数")
    p.add_argument("--duration", type=int, default=1200)
    p.add_argument("--backlog", type=int, default=600, help="开跑前每源灌多少条存量(since=0 一大批)")
    p.add_argument("--decoys-per-hit", type=int, default=420, help="问题 IP 上 decoy:hit 比")
    p.add_argument("--suspect-frac", type=float, default=0.25, help="活流里问题 IP 特权流占比")
    p.add_argument("--answer-key", default="content_answer_key.jsonl")
    p.add_argument("--seed", type=int, default=20260707)
    args = p.parse_args()

    open(args.answer_key, "w", encoding="utf-8").close()
    specs = _build_specs()[: args.sources]
    sources = [SourceState(specs[i], seed=args.seed + i * 101) for i in range(len(specs))]

    servers = []
    for source in sources:
        srv = ThreadingHTTPServer(("0.0.0.0", args.base_port + source.index), _make_handler(source))
        threading.Thread(target=srv.serve_forever, daemon=True, name=f"http-{source.name}").start()
        servers.append(srv)
        print(f"[serve] {source.name} on :{args.base_port + source.index}", flush=True)

    if args.backlog > 0:
        for source in sources:
            _seed_backlog(source, args.backlog, args.decoys_per_hit, args.answer_key)
        print(f"[backlog] seeded {args.backlog}/src; hits so far={sum(s.hit_count for s in sources)}", flush=True)

    stop = threading.Event()
    feeder = threading.Thread(target=_feeder, args=(sources, args, stop), daemon=True)
    feeder.start()
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        stop.set()
        for srv in servers:
            srv.shutdown()


if __name__ == "__main__":
    main()
