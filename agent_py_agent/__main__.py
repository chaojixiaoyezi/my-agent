from __future__ import annotations

"""简单 Python 智能体的 CLI 入口。"""

import argparse
import json
import sys
import time
from pathlib import Path

from .agent.config import load_config
from .agent.core import SimpleAgent

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config" / "agent_config.yaml"


def configure_stdio() -> None:
    """把标准输出尽量固定到 UTF-8。

    这样做主要是为了避免 Windows 终端在打印模型返回内容时再次乱码。
    说白了，就是先把“字能不能正常显示”这个基础问题兜住。
    """

    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def make_agent(args) -> SimpleAgent:
    """根据配置创建一个可直接运行的智能体实例。"""

    config = load_config(args.config)
    return SimpleAgent(config, ROOT)


def cmd_run(args) -> int:
    """执行一次单轮请求。"""

    agent = make_agent(args)
    result = agent.run(
        args.prompt,
        inject=args.inject or [],
        prompt_files=args.prompt_file or [],
        save=args.save,
    )
    if args.show_prompt:
        print("===== FINAL PROMPT =====")
        print(result.prompt)
        print("===== RESPONSE =====")
    print(result.response)
    print(
        f"\n[backend={result.backend}; used_memories={result.used_memories}; "
        f"tool_rounds={result.tool_rounds}]"
    )
    return 0


def cmd_remember(args) -> int:
    """手动写一条记忆。"""

    agent = make_agent(args)
    rec = agent.remember(args.content, kind=args.kind)
    print(json.dumps(rec.__dict__, ensure_ascii=False, indent=2))
    return 0


def cmd_memory_list(args) -> int:
    """列出最近几条记忆。"""

    agent = make_agent(args)
    records = agent.memory.all()[-args.limit :]
    for rec in records:
        print(json.dumps(rec.__dict__, ensure_ascii=False))
    return 0


def cmd_memory_search(args) -> int:
    """按智能体自己的检索规则搜索记忆。"""

    agent = make_agent(args)
    for rec in agent.recall(args.query, args.limit):
        print(json.dumps(rec.__dict__, ensure_ascii=False))
    return 0


def cmd_spawn(args) -> int:
    """生成子任务记录。"""

    agent = make_agent(args)
    tasks = agent.spawn_subagents(args.goal, args.count)
    for task in tasks:
        print(json.dumps(task.__dict__, ensure_ascii=False))
    return 0


def cmd_chat(args) -> int:
    """启动交互循环。"""

    agent = make_agent(args)
    print(
        f"{agent.config.agent_name} 交互循环已启动。"
        "输入 /help 查看命令，输入 exit 或 logout 退出，也可以直接按 Ctrl+C。"
    )
    runtime_inject: list[str] = args.inject or []
    prompt_files: list[str] = args.prompt_file or []
    while True:
        try:
            user = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return 0
        if not user:
            continue
        if user.lower() in {"/exit", "/quit", "exit", "logout", "退出"}:
            print("再见。")
            return 0
        if user == "/help":
            print(
                """可用命令：
/help                         显示帮助
/exit                         退出
exit / logout                 退出
/memory [关键词]              搜索记忆；不带关键词显示最近记忆
/remember <内容>              手动写入记忆
/inject <内容>                增加运行时 prompt 注入
/inject-clear                 清空运行时 prompt 注入
/prompt-file <路径>           增加动态 prompt 文件
/subagents <数量> <目标>      生成 subagent 任务记录
/show-prompt <问题>           显示最终 prompt 并回答
Ctrl+C                        退出
其他输入                       正常对话
"""
            )
            continue
        if user.startswith("/remember "):
            rec = agent.remember(user[len("/remember ") :], kind="note")
            print(f"已记忆: {rec.content}")
            continue
        if user.startswith("/memory"):
            query = user[len("/memory") :].strip()
            records = (
                agent.recall(query, args.memory_limit)
                if query
                else agent.memory.all()[-args.memory_limit :]
            )
            if not records:
                print("没有找到记忆。")
            for rec in records:
                print(f"- [{rec.kind}] {rec.role}: {rec.content}")
            continue
        if user.startswith("/inject "):
            runtime_inject.append(user[len("/inject ") :])
            print(f"已加入注入 prompt，当前 {len(runtime_inject)} 条。")
            continue
        if user == "/inject-clear":
            runtime_inject.clear()
            print("已清空运行时 prompt 注入。")
            continue
        if user.startswith("/prompt-file "):
            prompt_files.append(user[len("/prompt-file ") :].strip())
            print(f"已加入 prompt 文件，当前 {len(prompt_files)} 个。")
            continue
        if user.startswith("/subagents "):
            parts = user.split(maxsplit=2)
            if len(parts) < 3 or not parts[1].isdigit():
                print("用法: /subagents <数量> <目标>")
                continue
            tasks = agent.spawn_subagents(parts[2], int(parts[1]))
            for task in tasks:
                print(f"- {task.id}: {task.goal}")
            continue

        show_prompt = False
        if user.startswith("/show-prompt "):
            show_prompt = True
            user = user[len("/show-prompt ") :]

        try:
            started_at = time.perf_counter()
            print("正在等待模型响应...", flush=True)
            result = agent.run(
                user,
                inject=runtime_inject,
                prompt_files=prompt_files,
                save=not args.no_save,
            )
            elapsed = time.perf_counter() - started_at
            if show_prompt:
                print("===== FINAL PROMPT =====")
                print(result.prompt)
                print("===== RESPONSE =====")
            print(f"[耗时 {elapsed:.2f}s; 工具轮数 {result.tool_rounds}]")
            print(f"{agent.config.agent_name}> {result.response}")
        except Exception as exc:
            print(f"错误: {exc}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器。"""

    parser = argparse.ArgumentParser(
        prog="agent_py_agent",
        description="Simple Python3 CLI Agent with memory, dynamic prompt and subagents.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="配置文件路径，默认使用 config/agent_config.yaml",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="运行一次智能体对话")
    run.add_argument("prompt", help="用户任务 / prompt")
    run.add_argument("--inject", action="append", help="动态注入 prompt，可多次传入")
    run.add_argument("--prompt-file", action="append", help="额外动态 prompt 文件，可多次传入")
    run.add_argument("--save", action="store_true", default=None, help="保存本次对话到记忆")
    run.add_argument("--no-save", action="store_false", dest="save", help="不保存本次对话到记忆")
    run.add_argument("--show-prompt", action="store_true", help="打印最终拼装后的 prompt")
    run.set_defaults(func=cmd_run)

    remember = sub.add_parser("remember", help="手动写入一条记忆")
    remember.add_argument("content", help="记忆内容")
    remember.add_argument("--kind", default="note", help="记忆类型，如 note/preference/fact")
    remember.set_defaults(func=cmd_remember)

    memory_list = sub.add_parser("memory-list", help="列出最近记忆")
    memory_list.add_argument("--limit", type=int, default=20, help="最多显示条数")
    memory_list.set_defaults(func=cmd_memory_list)

    memory_search = sub.add_parser("memory-search", help="搜索记忆")
    memory_search.add_argument("query", help="搜索关键词")
    memory_search.add_argument("--limit", type=int, default=5, help="最多显示条数")
    memory_search.set_defaults(func=cmd_memory_search)

    chat = sub.add_parser("chat", help="启动交互循环，反复与智能体交流")
    chat.add_argument("--inject", action="append", help="启动时注入 prompt，可多次传入")
    chat.add_argument("--prompt-file", action="append", help="启动时加载额外 prompt 文件，可多次传入")
    chat.add_argument("--memory-limit", type=int, default=5, help="交互中 /memory 默认显示条数")
    chat.add_argument("--no-save", action="store_true", help="交互对话不自动保存到记忆")
    chat.set_defaults(func=cmd_chat)

    spawn = sub.add_parser("spawn-subagents", help="拆分并创建 subagent 任务记录")
    spawn.add_argument("goal", help="要拆分的目标")
    spawn.add_argument("--count", type=int, default=3, help="子代理数量")
    spawn.set_defaults(func=cmd_spawn)
    return parser


def main() -> int:
    """程序入口。"""

    configure_stdio()
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
