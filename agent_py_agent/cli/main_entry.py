from __future__ import annotations

"""LLM: CLI process entrypoint: parse args and dispatch the chosen subcommand.

给人看的解释：
这个文件只有 main()——程序启动时第一个被调用的函数。
它调用 build_parser() 解析命令行参数，然后根据子命令分发到对应的处理函数。
参数注册逻辑在 parser.py，具体命令逻辑在 cli 各子模块。
"""

from .common import configure_stdio
from .parser import build_parser


def main() -> int:
    """LLM: parse CLI arguments and dispatch to the matching subcommand handler.

    新手说明:
    这是 my-agent 程序的主入口函数。
    它先配置标准输入输出，再构建参数解析器解析命令行，
    最后调用 args.func(args) 把控制权交给对应子命令的处理函数。
    返回值是进程退出码，0 表示成功，非零表示出错。
    """

    configure_stdio()
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)
