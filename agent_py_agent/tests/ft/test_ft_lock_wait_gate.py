# LLM: Conveyor M00 / R3-G1（C2.1）：自由线程锁等待门禁。3.14t 在 GIL 关闭时，类型缓存未命中会去抢全局类型锁，
#   128 线程下吞吐塌到 16 线程的约 0.15（R3 §2.4）。本门禁在独立子进程里导入真实模块，按 CASES 逐个用例各跑
#   16 线程与 128 线程两档、每档共 3 秒计时：计时切成 3 个 16→128→128→16 块（每片 0.5 秒），取各块吞吐比的中位数，
#   要求它 ≥ 0.8。共享主机的负载漂移与短时尖峰在相邻片上近似相同，被块内对称排列与块间中位数抵消。
#   GIL 开启（含 3.11、3.12、3.14 默认构建）时跳过。
#   CASES 只追加不删除：每个 Cure-A 模块把它转换的类型以“零参可调用工厂”登记进来。
# 模块用途: 提供 G1 门禁测试与其子进程驱动（python -X gil=0 -m agent_py_agent.tests.ft.test_ft_lock_wait_gate <工厂>）。
from __future__ import annotations

import argparse
import importlib
import json
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# G1 通过线：128 线程吞吐至少为 16 线程吞吐的 0.8 倍（R3 §8；类型锁护航下实测约 0.15，余量充足）。
G1_MIN_THROUGHPUT_RATIO = 0.8
# 每个线程数档位的计时总秒数（R3 §8：3 秒），均分到 G1_BLOCK_COUNT 个块里该档的各片。
G1_WINDOW_SECONDS = 3.0
# 基准档线程数。
G1_BASE_THREADS = 16
# 压力档线程数（线上 gateway_http_max_request_workers=128 的验收配置）。
G1_STRESS_THREADS = 128
# 一个计时块内各片的线程数顺序（ABBA）：块内两档的平均时刻相同，负载线性漂移对两档影响相同。
G1_SLICE_ORDER = (G1_BASE_THREADS, G1_STRESS_THREADS, G1_STRESS_THREADS, G1_BASE_THREADS)
# 计时块数：判定取各块吞吐比的中位数。共享主机（负载 40–100、18 核）上顺序测两个 3 秒窗时，同一用例比值在
#   0.38–1.74 间摆动；3 块中位数下同一用例稳定在 1.04–1.81，元组键字典的真实争用稳定在 0.21–0.23。
G1_BLOCK_COUNT = 3
# 每个线程进入计时前的热身调用次数，让特化字节码与对象分配进入稳态。
G1_WARMUP_CALLS = 200
# 子进程驱动的超时秒数：两档各 3 秒计时（12 片）加建类、建线程开销，仍在 CI 每测 60 秒预算内。
G1_SUBPROCESS_TIMEOUT_SECONDS = 50
# 驱动进程需要的最小打开文件数：128 对 socketpair 加解释器自身所需。
G1_MIN_OPEN_FILES = 1024

# (工厂点分路径, 标签)：工厂每次调用返回一个零参可调用（一次调用 = 一个操作），可选带 close() 供驱动收尾。
CASES: list[tuple[str, str]] = [
    ("agent_py_agent.tests.test_conveyor_m00_hot_json.g1_case_hot_json", "hot_json"),
    ("agent_py_agent.tests.test_conveyor_m00_fast_time.g1_case_fast_time", "fast_time"),
    ("agent_py_agent.tests.test_conveyor_m00_fast_dataclass.g1_case_fast_frozen_dto", "fast_frozen_dto"),
    ("agent_py_agent.tests.test_conveyor_m00_direct_socket_io.g1_case_direct_socket_io", "direct_socket_io"),
    (
        "agent_py_agent.tests.test_conveyor_m10_cure_a_tool_loop.g1_case_tool_call_record_params",
        "m10_tool_call_record_params",
    ),
    (
        "agent_py_agent.tests.test_conveyor_m10_cure_a_tool_loop.g1_case_tool_call_execute_params",
        "m10_tool_call_execute_params",
    ),
    (
        "agent_py_agent.tests.test_conveyor_m10_cure_a_tool_loop.g1_case_tool_round_execution_request",
        "m10_tool_round_execution_request",
    ),
    (
        "agent_py_agent.tests.test_conveyor_m10_cure_a_tool_loop.g1_case_tool_progress_event",
        "m10_tool_progress_event",
    ),
    (
        "agent_py_agent.tests.test_conveyor_m10_cure_a_tool_loop.g1_case_parallel_thread_context",
        "m10_parallel_thread_context",
    ),
    (
        "agent_py_agent.tests.test_conveyor_m10_cure_a_tool_loop.g1_case_tool_loop_repair_counters",
        "m10_tool_loop_repair_counters",
    ),
    (
        "agent_py_agent.tests.test_conveyor_m10_cure_a_tool_loop.g1_case_tool_loop_response_decision",
        "m10_tool_loop_response_decision",
    ),
    (
        "agent_py_agent.tests.test_conveyor_m10_cure_a_tool_loop.g1_case_unresolved_runtime_issue_decision",
        "m10_unresolved_runtime_issue_decision",
    ),
    (
        "agent_py_agent.tests.test_conveyor_m10_cure_a_tool_loop.g1_case_unresolved_runtime_issue_decision_request",
        "m10_unresolved_runtime_issue_decision_request",
    ),
    (
        "agent_py_agent.tests.test_conveyor_m10_cure_a_tool_loop.g1_case_no_tool_calls_request",
        "m10_no_tool_calls_request",
    ),
    (
        "agent_py_agent.tests.test_conveyor_m10_cure_a_tool_loop.g1_case_model_turn_request",
        "m10_model_turn_request",
    ),
    (
        "agent_py_agent.tests.test_conveyor_m00_fast_dataclass.g1_case_fast_replace_open_class",
        "fast_replace_open_class",
    ),
    (
        "agent_py_agent.tests.test_conveyor_m00_fast_dataclass.g1_case_fast_replace_guarded_class",
        "fast_replace_guarded_class",
    ),
]

# C0 规则 9 的 GIL 探针：3.11/3.12 没有 sys._is_gil_enabled，按“GIL 开启”处理，收集阶段不会 AttributeError。
_GIL_DISABLED = getattr(sys, "_is_gil_enabled", lambda: True)() is False


# 函数用途: 把 “模块.属性” 点分路径解析成工厂函数。
def _resolve(dotted: str):
    module_name, _, attribute = dotted.rpartition(".")
    return getattr(importlib.import_module(module_name), attribute)


# 函数用途: 驱动进程按需抬高打开文件软上限（macOS 终端默认 256，不够 128 对 socketpair）。
def _ensure_open_file_limit() -> None:
    try:
        import resource
    except ImportError:
        return
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft == resource.RLIM_INFINITY or soft >= G1_MIN_OPEN_FILES:
        return
    target = G1_MIN_OPEN_FILES if hard == resource.RLIM_INFINITY else min(hard, G1_MIN_OPEN_FILES)
    resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))


# 函数用途: 用 threads 个线程各跑一个工厂产物，栅栏对齐后计时 seconds 秒，返回总操作数/秒。
def _measure(factory, threads: int, seconds: float) -> float:
    operations = [factory() for _ in range(threads)]
    counts = [0] * threads
    stop = [False]
    failures: list[str] = []
    barrier = threading.Barrier(threads + 1)

    def run(index: int) -> None:
        operation = operations[index]
        try:
            for _ in range(G1_WARMUP_CALLS):
                operation()
        except BaseException as exc:  # noqa: BLE001 - 失败经由 failures 交回主线程
            failures.append(repr(exc))
            barrier.abort()
            return
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            return
        done = 0
        try:
            while not stop[0]:
                operation()
                done += 1
        except BaseException as exc:  # noqa: BLE001 - 失败经由 failures 交回主线程
            failures.append(repr(exc))
        counts[index] = done

    workers = [threading.Thread(target=run, args=(index,), name=f"g1-{threads}-{index}", daemon=True)
               for index in range(threads)]
    for worker in workers:
        worker.start()
    try:
        barrier.wait()
    except threading.BrokenBarrierError:
        pass
    started = time.perf_counter()
    time.sleep(seconds)
    stop[0] = True
    stopped = time.perf_counter()
    for worker in workers:
        worker.join()
    for operation in operations:
        close = getattr(operation, "close", None)
        if callable(close):
            close()
    if failures:
        raise RuntimeError(f"G1 case failed: {failures[:3]}")
    return sum(counts) / (stopped - started)


# 函数用途: 子进程入口：确认 GIL 关闭后按块交替测 16 与 128 线程两档，输出一行 JSON（含各块比值与中位数）。
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="G1 free-threading lock-wait driver")
    parser.add_argument("factory", help="dotted path of a zero-argument callable factory")
    args = parser.parse_args(argv)
    if getattr(sys, "_is_gil_enabled", lambda: True)() is not False:
        print(json.dumps({"error": "gil_enabled"}))
        return 2
    _ensure_open_file_limit()
    factory = _resolve(args.factory)
    all_rates: dict[int, list[float]] = {G1_BASE_THREADS: [], G1_STRESS_THREADS: []}
    block_ratios = []
    for _block in range(G1_BLOCK_COUNT):
        rates: dict[int, list[float]] = {G1_BASE_THREADS: [], G1_STRESS_THREADS: []}
        for threads in G1_SLICE_ORDER:
            seconds = G1_WINDOW_SECONDS / (G1_BLOCK_COUNT * G1_SLICE_ORDER.count(threads))
            rates[threads].append(_measure(factory, threads, seconds))
        block_ratios.append(sum(rates[G1_STRESS_THREADS]) / sum(rates[G1_BASE_THREADS]))
        for threads, values in rates.items():
            all_rates[threads] += values
    print(json.dumps({
        "base_ops_per_second": statistics.fmean(all_rates[G1_BASE_THREADS]),
        "stress_ops_per_second": statistics.fmean(all_rates[G1_STRESS_THREADS]),
        "block_ratios": block_ratios,
        "ratio": statistics.median(block_ratios),
        "gil_disabled": getattr(sys, "_is_gil_enabled", lambda: True)() is False,
    }))
    return 0


def test_cases_are_unique_and_resolve_to_factories():
    labels = [label for _dotted, label in CASES]
    assert len(set(labels)) == len(labels)
    assert len({dotted for dotted, _label in CASES}) == len(CASES)
    for dotted, _label in CASES:
        assert callable(_resolve(dotted)), dotted


@pytest.mark.skipif(not _GIL_DISABLED, reason="G1 只在 GIL 关闭的自由线程解释器上判定")
@pytest.mark.parametrize(("factory", "label"), CASES, ids=[label for _dotted, label in CASES])
def test_throughput_at_128_threads_holds_against_16_threads(factory, label):
    completed = subprocess.run(
        [sys.executable, "-X", "gil=0", "-m", "agent_py_agent.tests.ft.test_ft_lock_wait_gate", factory],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=G1_SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-4000:]
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["gil_disabled"] is True, result
    assert result["ratio"] >= G1_MIN_THROUGHPUT_RATIO, (label, round(result["ratio"], 3), result)


if __name__ == "__main__":
    raise SystemExit(main())
