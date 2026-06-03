
from __future__ import annotations

import argparse

from ..guidance_commands import cmd_guidance_send


def add_guidance_subcommand(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("guidance-send", help="给运行中的主代理/子代理追加一条软提示")
    parser.add_argument("message", help="要追加的自然语言提示")
    parser.add_argument("--run-id", default="", help="目标代理 run_id")
    parser.add_argument("--thread-id", default="", help="目标会话 thread_id")
    parser.add_argument("--task-id", default="", help="目标任务 task_id")
    parser.add_argument("--case-id", default="", help="目标协作 case_id")
    parser.add_argument("--target-type", default="", help="开放目标类型；常见 agent_run/thread/task/case")
    parser.add_argument("--target-id", default="", help="开放目标 id")
    parser.add_argument("--sender", default="cli_user", help="发送者标记")
    parser.add_argument("--priority", default="normal", help="软优先级")
    parser.add_argument("--delivery", default="next_turn", help="投递提示，默认下一轮读取")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.set_defaults(func=cmd_guidance_send)


__all__ = ["add_guidance_subcommand"]
