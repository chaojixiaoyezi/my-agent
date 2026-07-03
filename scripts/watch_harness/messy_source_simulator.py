#!/usr/bin/env python3
"""中性「花数据」测试台:故意做得又花又杂(像真实日志),用于测【per-源学判据】治本。

与 multi_source_simulator 的区别:每条事件【混高基数噪声字段】(随机 tag/trace/session 等),
把「结果端那一栏」的信号淹在噪声里——【通用结构稀有度/少数派车道会失效(候选≈0)】,
逼摄取层必须先【学出这个源的判据】(哪个字段是结果端、什么值是目标、哪些字段是噪声)、
再配过滤器才抬得出目标。内容纯中性(质检/物流/校准/批处理/库存),零具体行业内容。

- N 源异构 schema;各源 GET /pull?since=<游标>&limit=<n> 游标续读(格式对齐 multi_source:>= since、
  next_cursor=末seq+1),返回 {items, returned, next_cursor}。
- 每条事件带唯一 event_id(EVT-<源字母>-<seq>)+ seq;目标极稀疏,只能从【结果端字段的少数派取值】判。
- 目标同步写旁路 answer-key(流外),供 fleet_score 对账。
纯 stdlib。用法见 --help / 交接文档 §3-A。
"""
from __future__ import annotations
import argparse, json, random, string, threading, time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

RING = 60000

def _noise(rng):
    """一坨高基数噪声字段:每条几乎唯一,专门淹信号。"""
    return {
        "trace_id": "".join(rng.choices(string.hexdigits.lower(), k=16)),
        "req_tag": "".join(rng.choices(string.ascii_lowercase + string.digits, k=10)),
        "node": f"n-{rng.randint(1, 9999)}",
        "lat_ms": rng.randint(1, 5000),
        "nonce": rng.randint(10**8, 10**9),
    }

# 5 中性源:各不同 schema;结果端字段的【少数派取值】=目标(真假只能从结果端判)。
def _quality(seq, rng, real):
    e = {"item": seq, "line": f"L{rng.randint(1,20)}", "spec": round(rng.uniform(9, 11), 2)}
    e["qc"] = {"grade": "defect" if real else rng.choice(["pass", "pass", "pass", "rework"])}
    e.update(_noise(rng)); return e
def _logistics(seq, rng, real):
    e = {"parcel": seq, "route": f"R{rng.randint(1,80)}", "hub": f"H{rng.randint(1,9)}"}
    e["delivery"] = {"state": "lost" if real else rng.choice(["delivered", "delivered", "in_transit", "returned"])}
    e.update(_noise(rng)); return e
def _calib(seq, rng, real):
    e = {"reading": seq, "device": f"D{rng.randint(1,200)}", "val": round(rng.uniform(50, 120), 1)}
    e["calibration"] = {"valid": bool(real)}
    e.update(_noise(rng)); return e
def _batch(seq, rng, real):
    e = {"job": seq, "queue": rng.choice(["etl", "report", "sync", "index"]), "rows": rng.randint(100, 99999)}
    e["run"] = {"outcome": "failed" if real else rng.choice(["success", "success", "success", "retried"])}
    e.update(_noise(rng)); return e
def _inventory(seq, rng, real):
    e = {"rec": seq, "sku": f"SKU{rng.randint(1000,99999)}", "store": f"S{rng.randint(1,50)}"}
    e["audit"] = {"reconciled": (not real)}  # real=对不上账(少数派)
    e.update(_noise(rng)); return e

SPECS = [("quality", "A", _quality), ("logistics", "B", _logistics), ("calib", "C", _calib),
         ("batch", "D", _batch), ("inventory", "E", _inventory)]

class Source:
    def __init__(self, index, name, letter, gen):
        self.index, self.name, self.letter, self.gen = index, name, letter, gen
        self.ring = deque(maxlen=RING); self.seq = 0; self.lock = threading.Lock()
    def emit(self, rng, real):
        self.seq += 1
        ev = self.gen(self.seq, rng, real)
        ev["seq"] = self.seq; ev["event_id"] = f"EVT-{self.letter}-{self.seq:06d}"; ev["ts"] = round(time.time(), 3)
        with self.lock: self.ring.append(ev)
        return ev
    def pull(self, since, limit):
        with self.lock:
            items = [dict(e) for e in self.ring if e["seq"] >= since][:limit]
        nxt = (items[-1]["seq"] + 1) if items else max(since, self.seq)
        return {"source": self.name, "items": items, "returned": len(items), "next_cursor": nxt}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-port", type=int, default=8901)
    p.add_argument("--sources", type=int, default=5)
    p.add_argument("--rate", type=int, default=120, help="每源每秒事件数")
    p.add_argument("--duration", type=int, default=1800, help="喂入秒数(服务喂完仍常驻)")
    p.add_argument("--hits", type=int, default=24)
    p.add_argument("--answer-key", default="messy_answer_key.jsonl")
    p.add_argument("--seed", type=int, default=20260703)
    a = p.parse_args()
    open(a.answer_key, "w").close()
    rng = random.Random(a.seed)
    sources = [Source(i, n, l, g) for i, (n, l, g) in enumerate(SPECS[:a.sources] * ((a.sources // len(SPECS)) + 1))][:a.sources]
    aklock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *x): pass
        def do_GET(self):
            u = urlsplit(self.path); q = parse_qs(u.query); s = self.server.src
            if u.path == "/pull":
                since = int(q.get("since", ["0"])[0]); limit = min(int(q.get("limit", ["100"])[0]), 500)
                self._j(s.pull(since, limit))
            elif u.path in ("/", "/status"):
                self._j({"source": s.name, "note": "GET /pull?since=游标&limit=n；真假只能看结果端字段的少数派取值；字段里混了大量噪声"})
            else:
                self._j({"error": "use /pull"}, 404)
        def _j(self, o, code=200):
            b = json.dumps(o, ensure_ascii=False).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    for s in sources:
        srv = ThreadingHTTPServer(("0.0.0.0", a.base_port + s.index), H); srv.src = s
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"[serve] messy/{s.name} on :{a.base_port + s.index}", flush=True)

    # 目标时刻表:hits 个目标均匀撒在 duration 内、随机分到各源
    schedule = sorted(rng.uniform(a.duration * 0.05, a.duration * 0.95) for _ in range(a.hits))
    hits = [(t, rng.randrange(len(sources))) for t in schedule]
    print(f"[plan] {a.hits} hits over {a.duration}s -> {a.answer_key}", flush=True)
    t0 = time.time(); hi = 0
    while True:
        el = time.time() - t0
        for s in sources:
            due = hi < len(hits) and el >= hits[hi][0] and hits[hi][1] == s.index
            for k in range(a.rate):
                real = due and k == 0
                ev = s.emit(rng, real)
                if real:
                    with aklock:
                        open(a.answer_key, "a").write(json.dumps({"event_id": ev["event_id"], "source": s.name, "emitted_at": ev["ts"]}, ensure_ascii=False) + "\n")
            if due: hi += 1
        time.sleep(max(0, 1.0 - (time.time() - t0 - el)))


if __name__ == "__main__":
    main()
