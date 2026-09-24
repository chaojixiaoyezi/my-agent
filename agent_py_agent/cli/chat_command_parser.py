from __future__ import annotations

# LLM: This module owns the canonical chat/resume argparse contract and must stay dependency-
# light. Handlers import chat.py only after parsing, preserving the fast interactive startup
# path while the full parser reuses exactly the same options.
# 模块用途: 集中注册 chat 和 resume 的全部命令行参数；启动时只解析轻量参数，真正执行后
# 才加载聊天运行时，避免为了进入 TUI 预先加载所有其他命令。
import argparse
import sys

from .bootstrap import add_resume_context_switches


# LLM: Both the full CLI parser and the fast interactive parser call this function. Keep one
# authoritative option definition and route execution through lazy wrappers below.
# 函数用途: 注册新建聊天与恢复聊天两个交互命令，并保持两条入口的公共选项一致。
def add_chat_subcommands(
    sub: argparse._SubParsersAction,
    *,
    lazy_handlers: bool = False,
) -> None:
    if lazy_handlers:
        chat_handler, resume_handler = _cmd_chat, _cmd_resume
    else:
        from .chat import cmd_chat, cmd_resume

        chat_handler, resume_handler = cmd_chat, cmd_resume
    chat = sub.add_parser("chat", help="启动交互循环，反复与智能体交流")
    _add_shared_chat_arguments(chat)
    chat.add_argument("--session-id", help="恢复指定会话，不传则创建新会话")
    chat.set_defaults(func=chat_handler)

    resume = sub.add_parser(
        "resume",
        help="显式连接指定会话继续交流: resume <session_id> (不存在则报错退出)",
    )
    resume.add_argument("session_id", help="要恢复的会话 ID(如 sess_1712_abcd1234)")
    _add_shared_chat_arguments(resume)
    resume.set_defaults(func=resume_handler)


# LLM: Shared options are defined once so chat and resume cannot silently diverge. The
# explicit session positional remains owned by add_chat_subcommands because its semantics differ.
# 函数用途: 添加共用参数；Gateway 超时只限制观察窗口，TUI 页面存活时继续接收原请求结果。
def _add_shared_chat_arguments(parser: argparse.ArgumentParser) -> None:
    # LLM: 工作目录只接受用户显式声明——不把进程 cwd 自动当权限来源（那会重开"从 /root 启动
    # 就把 /root 当任务目录"的洞）。多个目录用逗号分隔，仍逐个过硬门校验。
    # 函数用途: 让用户显式指定本次会话的工作目录（可多个），据此派生派工与第二层写作用域。
    parser.add_argument(
        "--workspace",
        dest="workspace_root",
        default=None,
        help="显式指定本次会话的工作目录；多个目录用逗号分隔。不传则沿用 owner home",
    )
    parser.add_argument("--inject", action="append", help="启动时注入 prompt，可多次传入")
    parser.add_argument("--prompt-file", action="append", help="启动时加载额外 prompt 文件，可多次传入")
    parser.add_argument("--memory-limit", type=int, default=None, help="交互中 /memory 默认显示条数；默认读配置")
    parser.add_argument(
        "--no-save",
        action="store_true",
        help=(
            "关闭本次运行归档与持久化 Compact；Gateway 模式仍记录 ConversationStore/审计，"
            "且不会直接写正式长期记忆"
        ),
    )
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument(
        "--gateway",
        action="store_true",
        dest="gateway",
        help="使用正式后台 gateway（默认）",
    )
    transport.add_argument(
        "--direct",
        action="store_false",
        dest="gateway",
        help="开发调试：在当前前台进程直接调用模型",
    )
    parser.set_defaults(gateway=True)
    parser.add_argument(
        "--gateway-timeout",
        type=float,
        help="Gateway 连续无活动的等待窗口（秒）；TUI 超时后继续观察原请求，plain 停止等待",
    )
    add_resume_context_switches(parser)


# LLM: Lazy import is the startup contract: never move chat.py back to module scope. Tests may
# patch this wrapper, while production dispatch remains identical to cmd_chat(args).
# 函数用途: 参数解析完成后才加载聊天运行时并启动新会话或隐式恢复会话。
def _cmd_chat(args) -> int:
    _show_boot_frame(args)
    from .chat import cmd_chat

    return cmd_chat(args)


# LLM: Resume shares the same lazy boundary and preserves cmd_resume's fail-closed session checks.
# 函数用途: 参数解析完成后才加载恢复逻辑，指定会话不存在或不属于当前用户时拒绝进入。
def _cmd_resume(args) -> int:
    _show_boot_frame(args)
    from .chat import cmd_resume

    return cmd_resume(args)


# LLM: The boot frame is immediate visual feedback only; readiness and session state remain owned
# by cmd_chat/TUI preflight. Never imply the Gateway is ready or accept input in this transient view.
# 函数用途: 在加载较重的聊天运行时前立刻显示启动画面，避免用户面对无内容的终端误以为卡死。
def _show_boot_frame(args) -> None:
    if bool(getattr(args, "plain", False)):
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return
    sys.stdout.write(
        "\x1b[2J\x1b[H"
        "\n  ◆ my-agent\n"
        "\n  正在启动交互界面…\n"
        "  随后会显示 Gateway 连接状态。\n"
    )
    sys.stdout.flush()
