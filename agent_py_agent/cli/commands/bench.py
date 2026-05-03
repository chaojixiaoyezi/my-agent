from __future__ import annotations

"""LLM: registers the low-risk bench-model CLI command.

给人看的解释：
这是 CLI parser 拆分的第一步：注册逻辑迁到 commands 包，
执行逻辑仍复用 bench_model.py，避免改变用户可见行为。
"""

import argparse

from ..bench_model import cmd_bench_model


def add_bench_model_command(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the `bench-model` subcommand."""

    bench = subparsers.add_parser("bench-model", help="运行模型速度基准测试或查看已有速度模型")
    bench.add_argument("--show", action="store_true", help="查看已有速度模型，不运行测试")
    bench.add_argument("--profile", help="速度模型文件路径；默认使用 data/model_speed_profile.json")
    bench.set_defaults(func=cmd_bench_model)
