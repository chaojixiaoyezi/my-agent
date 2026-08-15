#!/usr/bin/env python3
"""中性「花数据」测试台:故意做得又花又杂(像真实日志),用于测【per-源学判据】治本。

与 multi_source_simulator 的区别(三重加难,接手复测校准:仅高基数噪声不足以击穿
通用车道,引擎级召回仍 92%——按本台自述意图补齐真实日志的另两种花法):
  1. 高基数噪声字段(随机串/随机 ID/随机数):淹结构签名。
  2. 长尾良性枚举字段(常见值为主、偶发稀有尾值,尾值≈每窗 1~2 次):真实日志的
     status/code 类长尾——持续触发「少数派取值/稀有形状」兜底车道,诱饵淹精度。
  3. 结果端花两种形态:两源是干净布尔;三源是【文本消息】(结论记号+高基数尾巴),
     高基数折叠后通用车道对它全瞎——只有先学出判据(result_field+结论记号)才盯得住。
  4. 【藏判据提示】:/pull、/status 均不带 schema_note 之类判据说明,逼运行时从样本真学。

- N 源异构 schema;各源 GET /pull?since=<游标>&limit=<n> 游标续读(>= since,
  next_cursor=末 seq+1),返回 {items, returned, next_cursor}。
- 每条事件带唯一 event_id(EVT-<源字母>-<seq>)+ seq;目标极稀疏,真假只能从结果端判。
- 目标同步写旁路 answer-key(流外),供 fleet_score 对账。
纯 stdlib。用法见 --help / 交接文档 §3-A。
"""
from __future__ import annotations

import argparse
import json
import random
import string
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

RING_CAPACITY = 60_000

_HEX = string.hexdigits.lower()[:16]
_ALNUM = string.ascii_lowercase + string.digits


def _noise(rng: random.Random) -> dict:
    """一坨高基数噪声字段:每条几乎唯一,专门淹信号。"""
    return {
        "trace_id": "".join(rng.choices(_HEX, k=16)),
        "req_tag": "".join(rng.choices(_ALNUM, k=10)),
        "node": f"n-{rng.randint(1, 9999)}",
        "lat_ms": rng.randint(1, 5000),
        "nonce": rng.randint(10**8, 10**9),
    }


def _longtail(rng: random.Random, common: list[str], tail: list[str], tail_p: float = 0.0006) -> str:
    """长尾良性枚举:绝大多数取常见值,偶发取尾值(tail_p=尾值总概率;120/s、300s 窗下
    每个尾值≈1.8 次/窗,恰落在"少数派/稀有"判定内)——良性诱饵,专门让通用兜底车道
    持续误抬,淹掉不学判据者的精度。"""
    return rng.choice(tail) if rng.random() < tail_p else rng.choice(common)


def _msg(rng: random.Random, token: str) -> str:
    """结果端文本消息:结论记号+高基数尾巴(真实日志的结果栏常是这种)。"""
    return f"{token} ref={''.join(rng.choices(_HEX, k=10))} t={rng.randint(1, 99999)}"


_OP_TAIL = ["regrind", "anneal", "rebore", "hone", "lap", "peen", "etch", "burr", "shim", "trim", "flux", "seat"]
_VAN_TAIL = [f"V9{i}" for i in range(10)] + ["VX1", "VX2"]
_BENCH_TAIL = [f"B9{i}" for i in range(10)] + ["BX1", "BX2"]
_POOL_TAIL = [f"p9{i}" for i in range(10)] + ["px1", "px2"]
_SHIFT_TAIL = [f"K9{i}" for i in range(10)] + ["KX1", "KX2"]


def _quality(seq: int, rng: random.Random, real: bool) -> dict:
    """A 质检风:结果端 qc.note 是文本消息(pass/rework 常态,defect=目标)。"""
    event = {"item": seq, "line": f"L{rng.randint(1, 20)}", "spec": round(rng.uniform(9, 11), 2)}
    event["op"] = _longtail(rng, ["scan", "fit", "pack", "weld"], _OP_TAIL)
    event["cell"] = _longtail(rng, ["C1", "C2", "C3", "C4"], [f"C9{i}" for i in range(10)] + ["CX1", "CX2"])
    token = "defect" if real else rng.choice(["pass", "pass", "pass", "rework"])
    event["qc"] = {"note": _msg(rng, token)}
    event.update(_noise(rng))
    return event


def _logistics(seq: int, rng: random.Random, real: bool) -> dict:
    """B 物流风:结果端 delivery.log 是文本消息(delivered/in-transit/returned 常态,lost=目标)。"""
    event = {"parcel": seq, "route": f"R{rng.randint(1, 80)}", "hub": f"H{rng.randint(1, 9)}"}
    event["van"] = _longtail(rng, ["V1", "V2", "V3", "V4", "V5"], _VAN_TAIL)
    event["gate"] = _longtail(rng, ["G1", "G2", "G3"], [f"G9{i}" for i in range(10)] + ["GX1", "GX2"])
    token = "lost" if real else rng.choice(["delivered", "delivered", "in-transit", "returned"])
    event["delivery"] = {"log": _msg(rng, token)}
    event.update(_noise(rng))
    return event


def _calib(seq: int, rng: random.Random, real: bool) -> dict:
    """C 校准风:结果端 calibration.valid 是干净布尔(true=目标)——验证学判据对易源同样适用。"""
    event = {"reading": seq, "device": f"D{rng.randint(1, 200)}", "val": round(rng.uniform(50, 120), 1)}
    event["bench"] = _longtail(rng, ["B1", "B2", "B3", "B4"], _BENCH_TAIL)
    event["mode"] = _longtail(rng, ["auto", "manual", "batch"], ["m9a", "m9b", "m9c", "m9d", "m9e", "m9f", "m9g", "m9h", "m9i", "m9j", "m9k", "m9l"])
    event["calibration"] = {"valid": bool(real)}
    event.update(_noise(rng))
    return event


def _batch(seq: int, rng: random.Random, real: bool) -> dict:
    """D 批处理风:结果端 run.log 是文本消息(success/retried 常态,failed=目标)。"""
    event = {"job": seq, "queue": rng.choice(["etl", "report", "sync", "index"]), "rows": rng.randint(100, 99999)}
    event["pool"] = _longtail(rng, ["p1", "p2", "p3"], _POOL_TAIL)
    event["trigger"] = _longtail(rng, ["cron", "manual", "chain"], ["t9a", "t9b", "t9c", "t9d", "t9e", "t9f", "t9g", "t9h", "t9i", "t9j", "t9k", "t9l"])
    token = "failed" if real else rng.choice(["success", "success", "success", "retried"])
    event["run"] = {"log": _msg(rng, token)}
    event.update(_noise(rng))
    return event


def _inventory(seq: int, rng: random.Random, real: bool) -> dict:
    """E 库存风:结果端 audit.reconciled 是干净布尔(false=对不上账=目标)。"""
    event = {"rec": seq, "sku": f"SKU{rng.randint(1000, 99999)}", "store": f"S{rng.randint(1, 50)}"}
    event["counter"] = _longtail(rng, ["K1", "K2", "K3", "K4"], _SHIFT_TAIL)
    event["audit"] = {"reconciled": (not real)}
    event.update(_noise(rng))
    return event


SPECS = [("quality", "A", _quality), ("logistics", "B", _logistics), ("calib", "C", _calib),
         ("batch", "D", _batch), ("inventory", "E", _inventory)]


class Source:
    """一路源:滚动缓冲 + 游标查询;/pull 与 /status 都【不带判据提示】(藏掉,逼真学)。"""

    def __init__(self, index: int, spec: tuple, seed: int):
        self.index = index
        self.name, self.letter, self.maker = spec
        self.rng = random.Random(seed)
        self.ring: deque[dict] = deque(maxlen=RING_CAPACITY)
        self.seq = 0
        self.lock = threading.Lock()

    def emit(self, real: bool) -> dict:
        self.seq += 1
        event = self.maker(self.seq, self.rng, real)
        event["seq"] = self.seq
        event["event_id"] = f"EVT-{self.letter}-{self.seq:06d}"
        event["ts"] = round(time.time(), 3)
        with self.lock:
            self.ring.append(event)
        return event

    def pull(self, since: int, limit: int) -> dict:
        with self.lock:
            items = [dict(event) for event in self.ring if event["seq"] >= since][:limit]
        next_cursor = (items[-1]["seq"] + 1) if items else max(since, self.seq)
        return {"source": self.name, "items": items, "returned": len(items), "next_cursor": next_cursor}


def _make_handler(source: Source):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server 接口
            parts = urlsplit(self.path)
            if parts.path == "/pull":
                query = parse_qs(parts.query)
                since = int((query.get("since") or ["0"])[0] or 0)
                limit = max(1, min(500, int((query.get("limit") or ["100"])[0] or 100)))
                return self._reply(source.pull(since, limit))
            if parts.path in ("/", "/status"):
                return self._reply({"ok": True, "source": source.name, "seq": source.seq})
            return self._reply({"error": "use /pull?since=<cursor>&limit=<n>"}, code=404)

        def _reply(self, payload: dict, code: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:  # BaseHTTPRequestHandler override 约定;静默访问日志(120/s 下 stdout 会淹掉)
            pass

    return Handler


_ANSWER_LOCK = threading.Lock()


def _append_answer_key(path: str, row: dict) -> None:
    with _ANSWER_LOCK:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _build_hit_schedule(args, rng: random.Random) -> list[tuple[float, int]]:
    """目标在喂入窗内均匀散布(留头尾余量,带抖动),轮转分配到各源。"""
    lo = min(60.0, args.duration * 0.15)
    hi = max(lo + 1.0, args.duration - min(90.0, args.duration * 0.2))
    step = (hi - lo) / max(1, args.hits - 1)
    jitter = min(15.0, step / 3)
    schedule = [
        (min(hi, max(lo, lo + i * step + rng.uniform(-jitter, jitter))), i % args.sources)
        for i in range(args.hits)
    ]
    return sorted(schedule)


def _feeder(sources: list[Source], args, schedule: list[tuple[float, int]], stop: threading.Event) -> None:
    """单线程喂入:每 tick 给每源补齐平均速率;目标按 schedule 在指定源指定时刻插入。"""
    started = time.time()
    tick_seconds = 0.1
    per_tick = max(1, int(args.rate * tick_seconds))
    pending = list(schedule)
    while not stop.is_set() and time.time() - started < args.duration:
        _emit_due_hits(pending, sources, time.time() - started, args.answer_key)
        _tick_background_events(sources, per_tick)
        time.sleep(tick_seconds)
    print(f"[feeder] done at +{time.time() - started:.0f}s", flush=True)


def _tick_background_events(sources: list[Source], per_tick: int) -> None:
    for source in sources:
        for _ in range(per_tick):
            source.emit(real=False)


def _emit_due_hits(pending: list[tuple[float, int]], sources: list[Source], now_rel: float, answer_key: str) -> None:
    while pending and pending[0][0] <= now_rel:
        _at, source_index = pending.pop(0)
        source = sources[source_index]
        event = source.emit(real=True)
        _append_answer_key(answer_key, {"event_id": event["event_id"], "source": source.name, "emitted_at": event["ts"]})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-port", type=int, default=8901)
    parser.add_argument("--sources", type=int, default=5)
    parser.add_argument("--rate", type=int, default=120, help="每源每秒事件数")
    parser.add_argument("--duration", type=int, default=1800, help="喂入秒数(服务喂完仍常驻)")
    parser.add_argument("--hits", type=int, default=24)
    parser.add_argument("--answer-key", default="messy_answer_key.jsonl")
    parser.add_argument("--seed", type=int, default=20260703)
    args = parser.parse_args()

    open(args.answer_key, "w", encoding="utf-8").close()
    sources = [Source(index, SPECS[index % len(SPECS)], seed=args.seed + index) for index in range(args.sources)]
    for source in sources:
        server = ThreadingHTTPServer(("0.0.0.0", args.base_port + source.index), _make_handler(source))
        threading.Thread(target=server.serve_forever, daemon=True, name=f"http-{source.name}").start()
        print(f"[serve] messy/{source.name} on :{args.base_port + source.index}", flush=True)

    schedule = _build_hit_schedule(args, random.Random(args.seed))
    print(f"[plan] {args.hits} hits over {args.duration}s -> {args.answer_key}", flush=True)
    stop = threading.Event()
    threading.Thread(target=_feeder, args=(sources, args, schedule, stop), daemon=True).start()
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
