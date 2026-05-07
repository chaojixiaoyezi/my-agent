# LLM: CLI command registration module; keep parser wiring and command handler imports stable.
# 模块用途: 组织某组命令行子命令，让用户能从 CLI 触发对应功能。

from __future__ import annotations

"""registers learning-draft management commands.

给人看的解释：
learning 命令只负责管理自动学习草稿候选，不参与模型运行主链路。
"""

import argparse

from ..learning import cmd_learn_accept, cmd_learn_list, cmd_learn_reject, cmd_learn_stats


# LLM: add_learning_subcommand 属于learning CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 注册 argparse 参数和子命令，决定用户可见的命令形状。
def add_learning_subcommand(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:

    learn = subparsers.add_parser("learn", help="管理自动生成的 learning draft 候选")
    learn_sub = learn.add_subparsers(dest="learn_command", required=True)

    learn_list = learn_sub.add_parser("list", help="列出当前 learning draft 候选")
    learn_list.set_defaults(func=cmd_learn_list)

    learn_accept = learn_sub.add_parser("accept", help="确认一个 learning draft")
    learn_accept.add_argument("candidate_id", help="learning draft ID")
    learn_accept.set_defaults(func=cmd_learn_accept)

    learn_reject = learn_sub.add_parser("reject", help="拒绝一个 learning draft")
    learn_reject.add_argument("candidate_id", help="learning draft ID")
    learn_reject.set_defaults(func=cmd_learn_reject)

    learn_stats = learn_sub.add_parser("stats", help="查看 learning draft 汇总统计")
    learn_stats.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    learn_stats.set_defaults(func=cmd_learn_stats)

