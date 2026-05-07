# LLM: CLI command registration module; keep parser wiring and command handler imports stable.
# 模块用途: 组织某组命令行子命令，让用户能从 CLI 触发对应功能。

from __future__ import annotations

"""registers the low-risk bench-model CLI command.

给人看的解释：
这是 CLI parser 拆分的第一步：注册逻辑迁到 commands 包，
执行逻辑仍复用 bench_model.py，避免改变用户可见行为。
"""

import argparse

from ..bench_model import cmd_bench_model


# LLM: add_bench_model_command 属于benchmark CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 注册 argparse 参数和子命令，决定用户可见的命令形状。
def add_bench_model_command(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:

    bench = subparsers.add_parser("bench-model", help="运行模型速度基准测试或查看已有速度模型")
    bench.add_argument("--show", action="store_true", help="查看已有速度模型，不运行测试")
    bench.add_argument("--profile", help="速度模型文件路径；默认使用 data/model_speed_profile.json")
    bench.set_defaults(func=cmd_bench_model)
