
from __future__ import annotations

import argparse

from ..background_main_agent import (
    cmd_background_main_agent_bind_task,
    cmd_background_main_agent_message,
    cmd_background_main_agent_observe,
    cmd_background_main_agent_service,
    cmd_background_main_agent_status,
    cmd_background_main_agent_tick,
)


def add_background_main_agent_subcommands(subparsers) -> None:
    group = subparsers.add_parser(
        "background-main-agent",
        help="本地长期主代理线程、定时汇报和后台唤醒命令",
    )
    sub = group.add_subparsers(dest="background_command")

    _add_message_command(sub)
    _add_bind_task_command(sub)
    _add_observe_command(sub)
    _add_status_command(sub)
    _add_tick_command(sub)
    _add_service_command(sub)


def _add_message_command(sub) -> None:
    message = sub.add_parser("message", help="写入一条本地模拟通道消息")
    message.add_argument("--channel", default="internal", help="通道名，如 internal/feishu/wechat")
    message.add_argument("--conversation-id", required=True, help="通道会话或群/私聊 ID")
    message.add_argument("--user-id", required=True, help="通道内用户 ID")
    message.add_argument("--canonical-user-id", default="", help="跨渠道统一用户 ID；空值使用 --user-id")
    message.add_argument("--content", required=True, help="用户消息正文")
    message.add_argument("--now", type=float, default=None, help="测试用时间戳；不传使用当前时间")
    message.add_argument("--json", action="store_true", help="输出 JSON")
    background = message.add_mutually_exclusive_group()
    background.add_argument("--run-background", action="store_true", dest="run_background", help="写入后立即唤醒主代理")
    background.add_argument("--no-run-background", action="store_false", dest="run_background", help="只写消息，不唤醒")
    message.set_defaults(func=cmd_background_main_agent_message, run_background=False)


def _add_bind_task_command(sub) -> None:
    bind = sub.add_parser("bind-task", help="把 thread 绑定任务并可选创建定时汇报策略")
    bind.add_argument("--thread-id", required=True, help="ConversationThread ID")
    bind.add_argument("--task-id", required=True, help="任务 ID")
    bind.add_argument("--goal", required=True, help="任务目标摘要")
    bind.add_argument("--progress-interval-seconds", type=int, default=0, help="定时汇报间隔；0 表示不创建策略")
    bind.add_argument("--route-channel", default="internal", help="汇报投递通道")
    bind.add_argument("--route-target", default="", help="汇报投递目标；空值使用 thread 的绑定目标")
    bind.add_argument("--now", type=float, default=None, help="测试用时间戳；不传使用当前时间")
    bind.add_argument("--json", action="store_true", help="输出 JSON")
    bind.set_defaults(func=cmd_background_main_agent_bind_task)


def _add_tick_command(sub) -> None:
    tick = sub.add_parser("tick", help="执行一次 due progress policy 检查")
    tick.add_argument("--now", type=float, default=None, help="测试用时间戳；不传使用当前时间")
    tick.add_argument("--json", action="store_true", help="输出 JSON")
    tick.set_defaults(func=cmd_background_main_agent_tick)


def _add_observe_command(sub) -> None:
    observe = sub.add_parser("observe", help="写入子/孙代理 observation，并可选叫醒主代理")
    observe.add_argument("--thread-id", default="", help="ConversationThread ID；可用 --task-id 反查")
    observe.add_argument("--task-id", default="", help="任务 ID；thread-id 为空时用于反查会话")
    observe.add_argument("--event-type", default="observation", help="事件类型，不限定业务枚举")
    observe.add_argument("--summary", required=True, help="事件摘要")
    observe.add_argument("--urgency", default="normal", help="urgent 会创建 wake signal")
    observe.add_argument("--severity", default="", help="严重程度，原样记录")
    observe.add_argument("--source-agent-id", default="", help="上报事件的子/孙代理 ID")
    observe.add_argument("--parent-agent-id", default="", help="上报者的父代理 ID")
    observe.add_argument("--root-task-id", default="", help="整棵任务树根 ID；空值用 task-id")
    observe.add_argument("--evidence-ref", action="append", default=[], help="证据引用，可重复传")
    observe.add_argument("--requires-main-agent", action="store_true", help="下一次 tick 需要主代理判断")
    observe.add_argument("--requires-llm-report", action="store_true", help="需要主代理用 LLM 汇报")
    observe.add_argument("--wake", action="store_true", help="即使 urgency 不是 urgent，也创建 wake signal")
    observe.add_argument("--dedupe-key", default="", help="wake 幂等键")
    observe.add_argument("--now", type=float, default=None, help="测试用时间戳；不传使用当前时间")
    observe.add_argument("--json", action="store_true", help="输出 JSON")
    observe.set_defaults(func=cmd_background_main_agent_observe)


def _add_status_command(sub) -> None:
    status = sub.add_parser("status", help="只读查看长期会话、唤醒队列、协作 case 和代理树")
    status.add_argument("--json", action="store_true", help="输出 JSON")
    status.set_defaults(func=cmd_background_main_agent_status)


def _add_service_command(sub) -> None:
    service = sub.add_parser("service", help="按间隔循环 tick；本地前台运行")
    service.add_argument("--interval", type=float, default=5.0, help="tick 间隔秒数")
    service.add_argument("--max-cycles", type=int, default=0, help="最多循环次数；0 表示持续运行")
    service.add_argument("--stop-file", default="", help="存在该文件时停止循环")
    service.add_argument("--json", action="store_true", help="输出 JSON")
    service.set_defaults(func=cmd_background_main_agent_service)


__all__ = ["add_background_main_agent_subcommands"]
