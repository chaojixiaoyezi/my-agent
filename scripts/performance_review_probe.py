# LLM: 探针只测显式进程内只读操作；不运行任意shell、不调用模型、不写实际owner状态。
# 模块用途: 为CLI构建和配置加载提供可复跑的当前版本时间与内存观察。
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import platform
import statistics
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# LLM: 失败操作直接抛出；不以零耗时或空样本冒充一次成功观察，tracemalloc原状态不被覆盖。
# 函数用途: 测量有界次数的同一操作，返回每次样本及汇总。
def measure_call(operation, *, iterations=3, warmup=1):
    if type(iterations) is not int or type(warmup) is not int or not 1 <= iterations <= 500 or not 0 <= warmup <= 25:
        raise ValueError("iterations须为1到500，warmup须为0到25")
    if tracemalloc.is_tracing():
        raise RuntimeError("已有内存跟踪，拒绝重置其他观察者")
    samples = []
    for _ in range(warmup):
        operation()
    for _ in range(iterations):
        tracemalloc.start()
        try:
            wall, cpu = time.perf_counter(), time.process_time()
            operation()
            elapsed, used = time.perf_counter() - wall, time.process_time() - cpu
            _, peak = tracemalloc.get_traced_memory()
            samples.append({"wall_seconds": elapsed, "cpu_seconds": used, "python_peak_bytes": peak})
        finally:
            tracemalloc.stop()
    return {"samples": samples, "iterations": iterations, "warmup": warmup,
            "median_wall_seconds": statistics.median(s["wall_seconds"] for s in samples),
            "max_python_peak_bytes": max(s["python_peak_bytes"] for s in samples)}


# LLM: 来源摘要取当前源码字节，不能用HEAD号掩盖未提交版本；不包含配置值或秘密。
# 函数用途: 标记观察点、解释器、源文件摘要与输入摘要。
def provenance(paths, *, observation_point):
    return {"observation_point": observation_point, "python": sys.version.split()[0],
            "platform": platform.system(), "source_sha256": {
                str(Path(p).relative_to(ROOT)) if Path(p).is_relative_to(ROOT) else Path(p).name:
                hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths},
            "limitation": "仅当前进程观察；源码摘要仅覆盖列出文件，不是完整依赖快照；不证明服务启动、业务端到端、外部网络或其他机器性能"}


# LLM: 只构建当前检出的parser并检查真实根数，不执行任何解析出的handler。
# 函数用途: 执行CLI解析树构建探针。
def cli_probe(*, iterations=3, warmup=1):
    sys.path.insert(0, str(ROOT))
    from agent_py_agent.cli.argv_compat_v1 import CANONICAL_ROOTS
    from agent_py_agent.cli.parser import build_parser

    # LLM: 每个样本均校验实际树，避免只计时未装配成功的替代对象。
    # 函数用途: 构建并核对一次规范CLI。
    def operation():
        parser = build_parser()
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        if set(sub.choices) != CANONICAL_ROOTS:
            raise RuntimeError("规范CLI根合同不符")
    report = measure_call(operation, iterations=iterations, warmup=warmup)
    return {"ok": True, "case": "cli-build", **report,
            "provenance": provenance([ROOT / "agent_py_agent/cli/parser.py", ROOT / "agent_py_agent/cli/argv_compat_v1.py"],
                                     observation_point="in-process-canonical-cli-build")}


# LLM: stdout仅单个JSON；内部诊断不回显秘密，异常保留失败而不是报告零样本通过。
# 函数用途: 运行一次有界探针并输出结构化结果。
def main(argv=None):
    parser = argparse.ArgumentParser(description="当前检出版本的只读CLI性能探针")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            result = cli_probe(iterations=args.iterations, warmup=args.warmup)
    except Exception as exc:
        result = {"ok": False, "error_type": type(exc).__name__, "samples": [], "observation_point": "in-process-cli-build"}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
