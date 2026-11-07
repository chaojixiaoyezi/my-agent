"""Explicit S2-W5B short probe; not a soak or a real-provider capacity claim.

Run with the target interpreter: python -m agent_py_agent.tests.ft.short_load_probe
Uses the current checkout's real foreground Gateway and SLO loopback provider.
"""
from __future__ import annotations

import gc
import http.client
import json
import os
import sys
import sysconfig
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_py_agent.tests.slo.fake_provider import FakeProvider
from agent_py_agent.tests.slo.load import GatewayProcess, json_request


# LLM: Both runs execute the same pure-Python integer loop for the same wall-time budget.
# 函数用途: 用总计约5秒的单线程与四线程对照记录吞吐，不使用会释放GIL的C计算替代。
def cpu_parallel():
    observations = []
    for workers in (1, 4):
        start = threading.Event()
        deadline = [0.0]

        # LLM: Local arithmetic state is never shared and all workers use one release barrier.
        # 函数用途: 统计实际完成的Python运算批次和线程CPU时间。
        def run():
            start.wait()
            before = time.thread_time()
            batches, value = 0, 1
            while time.perf_counter() < deadline[0]:
                for _ in range(2000):
                    value = (value * 1664525 + 1013904223) & 0xFFFFFFFF
                batches += 1
            return batches, time.thread_time() - before, value

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(run) for _ in range(workers)]
            began = time.perf_counter()
            deadline[0] = began + 2.5
            start.set()
            results = [future.result(timeout=5) for future in futures]
        observations.append({"workers": workers, "batches": sum(row[0] for row in results),
                             "thread_cpu_seconds": sum(row[1] for row in results),
                             "wall_seconds": time.perf_counter() - began})
    return {"samples": observations,
            "speedup_4t": observations[1]["batches"] / observations[0]["batches"]}


# LLM: Start/close only the isolated fixture Gateway; never touch a running user service.
# 函数用途: 对真实Gateway的/status发起300个客户端、5秒并发探针，保留过载与错误。
def status_load(root):
    parameters = {"model_context_window_tokens": 32768, "probe_seconds": 1.0,
                  "startup_seconds": 15.0, "poll_seconds": 0.05, "teardown_seconds": 5.0}
    with FakeProvider(root / "provider") as provider:
        gateway = GatewayProcess(root / "gateway", provider, parameters)
        try:
            gateway.start()
            headers = gateway.headers()
            start, ready = threading.Event(), threading.Barrier(301)
            deadline = [0.0]

            # LLM: Each client keeps private counters; aggregation follows Future completion.
            # 函数用途: 真正发送HTTP请求，逐个计入成功、503或传输错误，不隐藏失败。
            def request_loop():
                counts, latencies, sample = Counter(), [], None
                ready.wait(timeout=10)
                start.wait()
                while time.perf_counter() < deadline[0]:
                    began = time.perf_counter()
                    try:
                        status, payload = json_request(gateway.base_url, "/status", headers=headers, timeout=2)
                        counts[str(status)] += 1
                        if status == 200 and sample is None:
                            sample = payload
                    except (OSError, http.client.HTTPException) as error:
                        counts[type(error).__name__] += 1
                    latencies.append(time.perf_counter() - began)
                return counts, latencies, sample

            with ThreadPoolExecutor(max_workers=300) as executor:
                futures = [executor.submit(request_loop) for _ in range(300)]
                ready.wait(timeout=10)
                began = time.perf_counter()
                deadline[0] = began + 5
                start.set()
                results = [future.result(timeout=10) for future in futures]
            counts, latencies = Counter(), []
            for count, durations, _ in results:
                counts.update(count)
                latencies.extend(durations)
            sample = next((row[2] for row in results if row[2] is not None), None)
            latencies.sort()
            observations = {"clients": 300, "load_window_seconds": 5,
                            "elapsed_seconds": time.perf_counter() - began,
                            "responses": dict(counts), "request_count": len(latencies),
                            "p99_seconds": latencies[min(len(latencies) - 1, int(len(latencies) * .99))],
                            "gateway_alive_after_load": gateway.proc.poll() is None,
                            "status_sample": sample}
            assert counts["200"] > 0, observations
            assert observations["gateway_alive_after_load"], observations
            return observations
        finally:
            gateway.close()


# LLM: Persist raw observations before deciding the exit status; a short probe is not a soak.
# 函数用途: 记录真实解释器、CPU吞吐、HTTP结果与GC停顿，失败也保留原始数据。
def main():
    home = Path(os.environ["MY_AGENT_HOME"])
    root = Path(tempfile.mkdtemp(prefix="short-probe-", dir=home))
    result = {"version": sys.version, "executable": sys.executable,
              "gil_enabled": getattr(sys, "_is_gil_enabled", lambda: True)(),
              "free_threaded_build": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
              "probe_root": str(root), "soak": False}
    try:
        result["cpu_parallel"] = cpu_parallel()
        result["status_load"] = status_load(root)
        pauses = []
        for _ in range(10):
            before = time.perf_counter()
            gc.collect()
            pauses.append(time.perf_counter() - before)
        result["probe_process_gc_pause_max_seconds"] = max(pauses)
        result["gil_enabled_after_load"] = getattr(sys, "_is_gil_enabled", lambda: True)()
        result["cpu_target_met"] = result["cpu_parallel"]["speedup_4t"] >= 3
        assert result["cpu_target_met"], "cpu_parallel_4t_below_3x; default image switch is not accepted"
    except BaseException as error:
        result["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        output = home / "short-probe.json"
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
