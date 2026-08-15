#!/usr/bin/env python3
"""M1 蹲守复现喂入器:每秒 1 行追加写日志,固定分钟点埋真命中(CRITICAL+[INCIDENT]),
其余为噪声与迷惑行(演练/阈值内/count=0——看着像命中但按判据不是)。

用法: watch_feeder.py <日志路径> <总时长秒> <命中分钟点逗号串,如 "2.0,4.5,7.0">
"""
from __future__ import annotations

import sys
import time

_NOISE = (
    "INFO heartbeat ok latency_ms={i}ms seq={n}",
    "DEBUG cache warm ratio=0.97 seq={n}",
    "INFO order placed id=ORD{n} amount=129.00 status=success",
    "INFO gc pause 12ms seq={n}",
)
_DECOYS = (
    "WARN error rate 0.1% still below threshold (no action needed) seq={n}",
    "INFO login failed count=0 in last window seq={n}",
    "ERROR drill simulation: fake payment timeout for chaos test, NOT a real incident seq={n}",
    "WARN CRITICAL-looking spike was retried and recovered automatically seq={n}",
)
_HIT = (
    "CRITICAL payment-gateway timeout spike: 5xx errors surging in last 60s, "
    "order_loss_estimate=8400CNY seq={n} [INCIDENT]"
)


def _line(elapsed: int, n: int, hit_seconds: set[int]) -> str:
    ts = time.strftime("%H:%M:%S")
    if elapsed in hit_seconds:
        hit_seconds.discard(elapsed)
        return f"{ts} " + _HIT.format(n=n)
    if n % 13 == 7:
        return f"{ts} " + _DECOYS[n % len(_DECOYS)].format(n=n)
    return f"{ts} " + _NOISE[n % len(_NOISE)].format(n=n, i=n % 40)


def main() -> None:
    log_path = sys.argv[1]
    duration_s = int(sys.argv[2])
    hit_seconds = {int(float(m) * 60) for m in sys.argv[3].split(",")}
    start = time.time()
    n = 0
    with open(log_path, "a", encoding="utf-8") as f:
        while int(time.time() - start) < duration_s:
            f.write(_line(int(time.time() - start), n, hit_seconds) + "\n")
            f.flush()
            n += 1
            time.sleep(1)
    print(f"feeder done: {n} lines")


if __name__ == "__main__":
    main()
