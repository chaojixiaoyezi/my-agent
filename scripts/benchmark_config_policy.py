# LLM: 只用当前检出的正式加载器读取显式配置，输出摘要不输出字段值；不写配置或owner目录。
# 模块用途: 为配置策略整合提供同输入、同版本的有界基准入口。
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.performance_review_probe import measure_call, provenance  # noqa: E402


# LLM: 缺失或加载失败必须失败，不以空配置/零指标冒充成功；统计只代表正式配置加载观察点。
# 函数用途: 重复读取同一输入，记录时间、内存和输入源码摘要。
def benchmark(config_path, *, iterations=3, warmup=1):
    from agent_py_agent.agent.settings import load_config
    path = Path(config_path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("配置必须是普通文件")
    before = path.read_bytes()
    report = measure_call(lambda: load_config(path), iterations=iterations, warmup=warmup)
    if path.read_bytes() != before:
        raise RuntimeError("观察期间输入配置发生变化")
    return {"ok": True, "case": "config-load", **report, "input_sha256": hashlib.sha256(before).hexdigest(),
            "provenance": provenance([ROOT / "agent_py_agent/agent/settings/config.py"],
                                     observation_point="in-process-explicit-config-load")}


# LLM: 输入路径必须由调用者明确给定，不默认访问真实用户配置；stdout只返回脱敏的单个JSON对象。
# 函数用途: 执行配置基准并以0/1区分真实成功和失败，语法错误保持2。
def main(argv=None):
    parser = argparse.ArgumentParser(description="显式配置的只读加载基准")
    parser.add_argument("--config", required=True)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            result = benchmark(args.config, iterations=args.iterations, warmup=args.warmup)
    except Exception as exc:
        result = {"ok": False, "error_type": type(exc).__name__, "samples": [], "observation_point": "in-process-explicit-config-load"}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
