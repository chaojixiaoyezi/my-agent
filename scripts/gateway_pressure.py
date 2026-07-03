#!/usr/bin/env python3
"""网关并发压测驱动(T4 并发公平层量化,纯标准库,复用 GET /metrics 探针)。

用法(先准备一份指向独立 my_agent_home 的 config;别用真实用户家目录):
  # 吞吐画像(echo 后端,量队列排队/认领/worker 饱和):
  python3 scripts/gateway_pressure.py --config /tmp/pressure-config.yaml \
      --requests 1000 --users 100 --concurrency 200 --out /tmp/pressure_echo.json

  # 持续在飞画像(慢速 stub LLM,压"同时活跃"并发;config 的 api_base 指到 stub):
  python3 scripts/gateway_pressure.py --config /tmp/pressure-slow.yaml \
      --requests 1000 --users 100 --concurrency 300 \
      --llm-stub-port 8899 --llm-delay 20 --out /tmp/pressure_hold.json

采样口径(全部来自网关 /metrics,不另造探针):
  agent_gateway_requests_enqueued_total / claimed_total   进队/认领累计(差值=积压)
  agent_gateway_queue_wait_seconds                        进队→认领等待直方图
  agent_gateway_workers_busy                              request worker 在忙数
  agent_background_owner_ticks_inflight                   后台整合 tick 在飞
  agent_subagent_runners_inflight                         子代理 runner 在飞
  agent_llm_inflight                                      真实压在模型上的并发
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_WATCHED_SERIES = (
    "agent_gateway_requests_enqueued_total",
    "agent_gateway_requests_claimed_total",
    "agent_gateway_workers_busy",
    "agent_background_owner_ticks_inflight",
    "agent_subagent_runners_inflight",
    "agent_llm_inflight",
)


class _StubLLMHandler(BaseHTTPRequestHandler):
    delay_seconds = 0.0

    def do_POST(self):  # noqa: N802 - http.server 接口名
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        time.sleep(self.delay_seconds)
        payload = {
            "id": "stub-1",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "收到,已完成。"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - BaseHTTPRequestHandler override 约定;静音访问日志
        return


def _start_stub_llm(port: int, delay: float) -> ThreadingHTTPServer:
    _StubLLMHandler.delay_seconds = delay
    server = ThreadingHTTPServer(("127.0.0.1", port), _StubLLMHandler)
    threading.Thread(target=server.serve_forever, name="stub-llm", daemon=True).start()
    return server


def _http_get(url: str, timeout: float = 5.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _post_ask(base: str, user: str, goal: str) -> tuple[int, str]:
    body = json.dumps({"kind": "ask", "goal": goal}).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/ask",
        data=body,
        headers={"Content-Type": "application/json", "X-User-Id": user, "X-Channel": "feishu"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")[:200]
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也计数,不炸压测
        return exc.code, str(exc)[:200]
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)[:200]


def _parse_metrics(text: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split()
        if len(parts) != 2:
            continue
        name = parts[0]
        try:
            value = float(parts[1])
        except ValueError:
            continue
        bare = name.split("{", 1)[0]
        if bare in _WATCHED_SERIES or bare.startswith("agent_gateway_queue_wait_seconds"):
            values[name] = values.get(name, 0.0) + value
    return values


def _queue_wait_quantile(samples: dict[str, float], q: float) -> float:
    buckets: list[tuple[float, float]] = []
    total = 0.0
    for name, value in samples.items():
        if not name.startswith("agent_gateway_queue_wait_seconds_bucket"):
            continue
        le_text = name.split('le="', 1)[-1].split('"', 1)[0]
        edge = float("inf") if le_text == "+Inf" else float(le_text)
        buckets.append((edge, value))
    count = samples.get("agent_gateway_queue_wait_seconds_count", 0.0)
    if not buckets or count <= 0:
        return 0.0
    buckets.sort()
    target = count * q
    for edge, cumulative in buckets:
        total = cumulative
        if cumulative >= target:
            return edge
    return buckets[-1][0] if total else 0.0


def _final_summary(samples: list[dict], statuses: dict[int, int], wall: float) -> dict:
    last = samples[-1]["values"] if samples else {}
    peak = {
        key: max((s["values"].get(key, 0.0) for s in samples), default=0.0)
        for key in (
            "agent_gateway_workers_busy",
            "agent_background_owner_ticks_inflight",
            "agent_subagent_runners_inflight",
            "agent_llm_inflight",
        )
    }
    enqueued = last.get("agent_gateway_requests_enqueued_total", 0.0)
    claimed = last.get("agent_gateway_requests_claimed_total", 0.0)
    return {
        "wall_seconds": round(wall, 1),
        "post_statuses": {str(k): v for k, v in sorted(statuses.items())},
        "enqueued_total": enqueued,
        "claimed_total": claimed,
        "backlog_at_end": enqueued - claimed,
        "queue_wait_p50_le_seconds": _queue_wait_quantile(last, 0.5),
        "queue_wait_p95_le_seconds": _queue_wait_quantile(last, 0.95),
        "queue_wait_count": last.get("agent_gateway_queue_wait_seconds_count", 0.0),
        "queue_wait_sum_seconds": round(last.get("agent_gateway_queue_wait_seconds_sum", 0.0), 1),
        "peak_gauges": peak,
        "claim_throughput_per_second": round(claimed / wall, 2) if wall > 0 else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True)
    parser.add_argument("--gateway-port", type=int, default=8420)
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=200)
    parser.add_argument("--goal", default="回复两个字:收到。不要调用任何工具。")
    parser.add_argument("--llm-stub-port", type=int, default=0)
    parser.add_argument("--llm-delay", type=float, default=0.0)
    parser.add_argument("--sample-interval", type=float, default=2.0)
    parser.add_argument("--settle-timeout", type=int, default=900)
    parser.add_argument("--out", default="")
    parser.add_argument("--keep-gateway", action="store_true", help="结束后不杀网关(排障用)")
    args = parser.parse_args()

    base = f"http://127.0.0.1:{args.gateway_port}"
    if not _prepare_environment(args, base):
        return 2
    gateway = subprocess.Popen(
        [sys.executable, "-m", "agent_py_agent", "--config", args.config, "gateway", "start"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    try:
        if not _wait_gateway_ready(base):
            print("[pressure] 网关 60s 未就绪,退出", file=sys.stderr)
            return 2
        print("[pressure] gateway ready, blasting requests...")
        samples: list[dict] = []
        stop_sampling = threading.Event()
        sampler = _start_sampler(base, args.sample_interval, samples, stop_sampling)
        started = time.time()
        statuses = _blast(base, args)
        print(f"[pressure] all posted in {time.time() - started:.1f}s statuses={statuses}; waiting for drain...")
        _wait_drain(args, samples, statuses)
        stop_sampling.set()
        sampler.join(timeout=5)
        _emit_report(args, samples, statuses, time.time() - started)
        return 0
    finally:
        if not args.keep_gateway:
            _stop_gateway(args.config, gateway)


def _prepare_environment(args, base: str) -> bool:
    """起 stub LLM(可选)并确认目标端口空闲。gateway start 会守护化(真身是独立的
    gateway run 进程):若端口已有活网关,压测会打在旧代码/累计计数上(实测坑)。"""
    if args.llm_stub_port:
        _start_stub_llm(args.llm_stub_port, args.llm_delay)
        print(f"[pressure] stub LLM on 127.0.0.1:{args.llm_stub_port} delay={args.llm_delay}s")
    if _port_serving(base):
        print(f"[pressure] 端口 {args.gateway_port} 已有网关在跑;先 gateway stop 或换端口再压", file=sys.stderr)
        return False
    return True


def _port_serving(base: str) -> bool:
    try:
        _http_get(f"{base}/metrics", timeout=2)
        return True
    except Exception:
        return False


def _wait_gateway_ready(base: str, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() <= deadline:
        if _port_serving(base):
            return True
        time.sleep(1)
    return False


def _start_sampler(base: str, interval: float, samples: list, stop: threading.Event) -> threading.Thread:
    def _sampler():
        while not stop.is_set():
            try:
                samples.append({"at": time.time(), "values": _parse_metrics(_http_get(f"{base}/metrics"))})
            except Exception:  # noqa: BLE001 - 采样失败跳过本轮
                pass
            stop.wait(interval)

    thread = threading.Thread(target=_sampler, name="metrics-sampler", daemon=True)
    thread.start()
    return thread


def _blast(base: str, args) -> dict[int, int]:
    statuses: dict[int, int] = {}
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [
            pool.submit(_post_ask, base, f"u-press-{i % args.users:03d}", args.goal)
            for i in range(args.requests)
        ]
        for future in futures:
            code, _ = future.result()
            statuses[code] = statuses.get(code, 0) + 1
    return statuses


def _wait_drain(args, samples: list, statuses: dict[int, int]) -> None:
    accepted = sum(v for k, v in statuses.items() if k == 202)
    settle_deadline = time.time() + args.settle_timeout
    idle_rounds = 0
    while time.time() < settle_deadline and idle_rounds < 3:
        time.sleep(args.sample_interval)
        values = samples[-1]["values"] if samples else {}
        claimed = values.get("agent_gateway_requests_claimed_total", 0.0)
        busy = values.get("agent_gateway_workers_busy", 0.0)
        idle_rounds = idle_rounds + 1 if (claimed >= accepted and busy <= 0) else 0


def _emit_report(args, samples: list, statuses: dict[int, int], wall: float) -> None:
    summary = _final_summary(samples, statuses, wall)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.out:
        report = {"args": vars(args), "summary": summary, "samples": samples}
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[pressure] full report -> {args.out}")


def _stop_gateway(config: str, gateway: subprocess.Popen) -> None:
    # 守护化网关必须走 gateway stop(terminate 只能杀 start 包装,真身 gateway run 会活下来占端口)。
    subprocess.run(
        [sys.executable, "-m", "agent_py_agent", "--config", config, "gateway", "stop"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        timeout=120,
        check=False,
    )
    gateway.terminate()
    try:
        gateway.wait(timeout=15)
    except subprocess.TimeoutExpired:
        gateway.kill()


if __name__ == "__main__":
    raise SystemExit(main())
