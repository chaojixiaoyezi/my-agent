# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。

from __future__ import annotations

"""CLI process entrypoint: parse args and dispatch the chosen subcommand.

给人看的解释：
这个文件只有 main()——程序启动时第一个被调用的函数。
它调用 build_parser() 解析命令行参数，然后根据子命令分发到对应的处理函数。
参数注册逻辑在 parser.py，具体命令逻辑在 cli 各子模块。
"""

from .common import configure_stdio
from .parser import build_parser


# LLM: main 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 脚本入口，解析参数、运行主流程，并用退出码表达成功或失败。
def main() -> int:

    configure_stdio()
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)
