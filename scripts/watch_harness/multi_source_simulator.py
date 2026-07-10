#!/usr/bin/env python3
"""能力一(监控研判编队)正式测试台:多源异构事件模拟器(可复用,非一次性脚本)。

对应交接文档「可复用长期测试台」节的 1-4 项:
  1. 一个进程起 N 个 HTTP 源(连续端口),各以可调速率持续吐结构化事件,N 路 schema 各不相同;
  2. 每源提供 GET /pull?since=<游标>&limit=<n> 候选查询,返回 {items, next_cursor};
  3. 候选里绝大多数是"看着像命中、结果端证明不是"的迷惑事件(同触发形状、结果字段否定),
     真命中极稀疏——**真假只能从"结果/响应"端字段判,输入端全是迷惑**;
  4. 每条真命中产生时,把 {时刻, 唯一ID, 源, 一句话} 写进旁路 answer-key(事件流之外),
     供 fleet_score.py 对账命中率/漏报/误报/端到端延迟。

用法(默认 5 源、每源 ~100 条/秒、20 分钟、22 条真命中):
  python3 scripts/watch_harness/multi_source_simulator.py \
      --base-port 8901 --sources 5 --rate 100 --duration 1260 --hits 22 \
      --answer-key /tmp/watch_answer_key.jsonl
跨机:跑在数据源机上,运行机(部署机)走局域网访问 http://<本机IP>:8901..8905。
冒烟: curl http://127.0.0.1:8901/status ; curl 'http://127.0.0.1:8901/pull?since=0&limit=3'
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

RING_CAPACITY = 60_000  # 每源保留最近 N 条(100/s 下 ≈10 分钟窗口),游标落后太多=真实背压丢失


# ---- 5 种异构 schema:同一逻辑(触发端像命中/结果端才定真假),字段形状各不相同 ----
# 每个生成器返回 (event_dict, is_true_hit 由调用方注入结果端字段后决定)。

def _schema_auth(seq: int, now: float, rng: random.Random, kind: str) -> dict:
    """A 认证日志风:触发端=admin 登录尝试;结果端 session.established 才是真的。"""
    user = rng.choice(["admin", "root", "svc-backup"]) if kind != "noise" else rng.choice(["alice", "bob", "carol", "dave"])
    return {
        "seq": seq,
        "ts": now,
        "kind": "login_attempt" if kind != "noise" else rng.choice(["login_attempt", "logout", "token_refresh"]),
        "user": user,
        "src_ip": f"10.{rng.randint(0,9)}.{rng.randint(0,255)}.{rng.randint(1,254)}",
        "session": {"established": kind == "hit", "mfa": rng.choice([True, False])},
    }


def _schema_pay(seq: int, now: float, rng: random.Random, kind: str) -> dict:
    """B 支付风:触发端=大额 charge;结果端 settlement.state=captured 才是真的。"""
    amount = rng.randint(90_000, 999_000) if kind != "noise" else rng.randint(1, 500)
    return {
        "cursor": seq,
        "time_ms": int(now * 1000),
        "event": "charge" if kind != "noise" else rng.choice(["charge", "refund", "query"]),
        "amount_cents": amount,
        "currency": "CNY",
        "settlement": {"state": "captured" if kind == "hit" else rng.choice(["declined", "reversed", "pending_review"])},
    }


def _schema_infra(seq: int, now: float, rng: random.Random, kind: str) -> dict:
    """C 基建告警风:触发端=disk_failure_predicted;结果端 probe.verified 才是真的。"""
    return {
        "offset": seq,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)) + f".{int(now % 1 * 1000):03d}",
        "alert": "disk_failure_predicted" if kind != "noise" else rng.choice(["cpu_high", "mem_high", "disk_failure_predicted_test", "net_flap"]),
        "node": f"node-{rng.randint(1, 40)}",
        "probe": {"verified": kind == "hit", "smart_reallocated": rng.randint(0, 12)},
    }


def _schema_apigw(seq: int, now: float, rng: random.Random, kind: str) -> dict:
    """D 网关访问风:触发端=首响 5xx;结果端 upstream.final_code 仍 5xx 才是真的(重试恢复=迷惑)。"""
    first = 500 + rng.randint(0, 3) if kind != "noise" else rng.choice([200, 200, 200, 301, 404])
    return {
        "n": seq,
        "t": now,
        "route": rng.choice(["/api/orders", "/api/users", "/api/search", "/api/pay"]),
        "first_code": first,
        "upstream": {"final_code": first if kind == "hit" else (200 if first >= 500 else first), "retries": rng.randint(0, 3)},
    }


def _schema_sensor(seq: int, now: float, rng: random.Random, kind: str) -> dict:
    """E 传感器风:触发端=超阈读数;结果端 calibration.valid 才是真的(失准读数=迷惑)。"""
    reading = round(rng.uniform(95.0, 120.0), 2) if kind != "noise" else round(rng.uniform(20.0, 60.0), 2)
    return {
        "idx": seq,
        "epoch": now,
        "sensor": f"S-{rng.randint(100, 140)}",
        "metric": "core_temp_c",
        "reading": reading,
        "calibration": {"valid": kind == "hit", "last_check_days": rng.randint(0, 90)},
    }


# (源名, 生成器, id字段名, 结果端判据说明)。**所有事件(噪声/迷惑/命中)都带唯一事件 ID**——
# id 的存在不携带任何真假信号(否则输入端就泄题了);真假只能读结果端字段。id 字段名各源不同(异构)。
def _build_schemas() -> list[SourceSpec]:
    return [
        SourceSpec("auth_log", _schema_auth, "event_id", "结果端判据: session.established=true 才是真命中(admin 登录尝试大多失败=迷惑)"),
        SourceSpec("payments", _schema_pay, "txn_ref", "结果端判据: settlement.state=captured 才是真命中(大额 charge 大多 declined/reversed=迷惑)"),
        SourceSpec("infra_alerts", _schema_infra, "alert_id", "结果端判据: probe.verified=true 才是真命中(预测告警大多未证实=迷惑)"),
        SourceSpec("api_gateway", _schema_apigw, "trace_id", "结果端判据: upstream.final_code 仍 5xx 才是真命中(首响 5xx 大多重试恢复=迷惑)"),
        SourceSpec("sensors", _schema_sensor, "reading_id", "结果端判据: calibration.valid=true 才是真命中(超阈读数大多失准=迷惑)"),
    ]


class SourceSpec:
    """一路源的静态描述:(源名, 事件生成器, id字段名, 结果端判据说明)。"""

    def __init__(self, name: str, maker, id_field: str, note: str):
        self.name, self.maker, self.id_field, self.note = name, maker, id_field, note


class SourceState:
    def __init__(self, index: int, spec: SourceSpec, seed: int):
        self.index = index
        self.name = spec.name
        self.maker = spec.maker
        self.id_field = spec.id_field
        self.note = spec.note
        self.rng = random.Random(seed)
        self.ring: deque[dict] = deque(maxlen=RING_CAPACITY)
        self.seq = 0
        self.lock = threading.Lock()
        self.hit_count = 0

    def append(self, kind: str, answer_key_path: str) -> None:
        now = time.time()
        event_id = f"EVT-{chr(ord('A') + self.index)}-{self.seq:06d}"
        event = self.maker(self.seq, now, self.rng, kind)
        event[self.id_field] = event_id  # 每条都有 id,存在性不泄真假
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

    def pull(self, since: int, limit: int) -> dict:
        with self.lock:
            items = [dict(event) for event in self.ring if _event_seq(event) >= since][:limit]
            next_cursor = (_event_seq(items[-1]) + 1) if items else max(since, self.seq)
        return {
            "api": self.name,
            "schema_note": self.note,
            "items": items,
            "returned": len(items),
            "next_cursor": next_cursor,
        }


def _event_seq(event: dict) -> int:
    for key in ("seq", "cursor", "offset", "n", "idx"):
        if key in event:
            return int(event[key])
    return -1


_ANSWER_LOCK = threading.Lock()


def _append_answer_key(path: str, row: dict) -> None:
    with _ANSWER_LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _make_handler(source: SourceState):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server 接口
            parts = urlsplit(self.path)
            if parts.path == "/status":
                return self._reply({"ok": True, "source": source.name, "seq": source.seq, "hits_emitted": source.hit_count})
            if parts.path == "/pull":
                query = parse_qs(parts.query)
                since = int((query.get("since") or ["0"])[0] or 0)
                limit = max(1, min(500, int((query.get("limit") or ["50"])[0] or 50)))
                return self._reply(source.pull(since, limit))
            return self._reply({"error": "unknown path", "paths": ["/status", "/pull?since=<cursor>&limit=<n>"]}, code=404)

        def _reply(self, payload: dict, code: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:  # BaseHTTPRequestHandler override 约定;静默访问日志(100/s 下 stdout 会淹掉)
            pass

    return Handler


def _feeder(sources: list[SourceState], args, hit_schedule: list[tuple[float, int]], stop: threading.Event) -> None:
    """单线程喂入:每 tick 给每源补齐平均速率的事件;命中按 schedule 在指定源指定时刻插入。
    迷惑事件(confuser,触发像命中/结果端否定)按 ~1.5% 掺入,其余纯噪声。"""
    started = time.time()
    tick_seconds = 0.1
    per_tick = max(1, int(args.rate * tick_seconds))
    schedule = list(hit_schedule)
    while not stop.is_set() and time.time() - started < args.duration:
        _drain_due_hits(schedule, sources, time.time() - started, args.answer_key)
        _tick_background_events(sources, per_tick, args.answer_key)
        time.sleep(tick_seconds)
    print(f"[feeder] done at +{time.time() - started:.0f}s, hits emitted: {sum(s.hit_count for s in sources)}", flush=True)


def _drain_due_hits(schedule: list[tuple[float, int]], sources: list[SourceState], now_rel: float, answer_key: str) -> None:
    while schedule and schedule[0][0] <= now_rel:
        _at, source_index = schedule.pop(0)
        sources[source_index].append("hit", answer_key)


def _tick_background_events(sources: list[SourceState], per_tick: int, answer_key: str) -> None:
    for source in sources:
        kinds = ["confuser" if source.rng.random() < 0.015 else "noise" for _ in range(per_tick)]
        for kind in kinds:
            source.append(kind, answer_key)


def _build_hit_schedule(args, rng: random.Random) -> list[tuple[float, int]]:
    """真命中在喂入窗内均匀散布(留头尾余量,带抖动),轮转分配到各源;窗按时长自适应,
    短时长冒烟跑也能出命中。"""
    lo = min(60.0, args.duration * 0.15)
    hi = max(lo + 1.0, args.duration - min(90.0, args.duration * 0.2))
    step = (hi - lo) / max(1, args.hits - 1)
    jitter = min(15.0, step / 3)
    schedule = [
        (min(hi, max(lo, lo + i * step + rng.uniform(-jitter, jitter))), i % args.sources)
        for i in range(args.hits)
    ]
    return sorted(schedule)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-port", type=int, default=8901)
    parser.add_argument("--sources", type=int, default=5)
    parser.add_argument("--rate", type=int, default=100, help="每源每秒事件数")
    parser.add_argument("--duration", type=int, default=1260, help="喂入秒数(HTTP 服务喂完仍常驻,直到进程被杀)")
    parser.add_argument("--hits", type=int, default=22)
    parser.add_argument("--answer-key", default="watch_answer_key.jsonl")
    parser.add_argument("--seed", type=int, default=20260702)
    parser.add_argument(
        "--seq-base", type=int, default=0,
        help="起始事件序号(模拟'同一路源持续在涨':sim 重启后用高于旧游标的序号续喂,"
        "盯守方从持久化游标续读不空转;0=从头)",
    )
    args = parser.parse_args()

    open(args.answer_key, "w", encoding="utf-8").close()  # 清空旧 key
    rng = random.Random(args.seed)
    specs = _build_schemas()
    sources = [SourceState(i, specs[i % len(specs)], seed=args.seed + i) for i in range(args.sources)]
    for source in sources:
        source.seq = max(0, int(args.seq_base))

    servers = []
    for source in sources:
        server = ThreadingHTTPServer(("0.0.0.0", args.base_port + source.index), _make_handler(source))
        threading.Thread(target=server.serve_forever, daemon=True, name=f"http-{source.name}").start()
        servers.append(server)
        print(f"[serve] {source.name} on :{args.base_port + source.index}  ({source.note})", flush=True)

    stop = threading.Event()
    schedule = _build_hit_schedule(args, rng)
    print(f"[plan] {args.hits} hits over {args.duration}s -> answer key: {args.answer_key}", flush=True)
    feeder = threading.Thread(target=_feeder, args=(sources, args, schedule, stop), daemon=True)
    feeder.start()
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        stop.set()
        for server in servers:
            server.shutdown()


if __name__ == "__main__":
    main()
