from __future__ import annotations

"""LLM: implements interactive chat mode with optional gateway client execution and background job queue.

给人看的解释：
chat 模式要一边接收用户输入，一边让模型在后台跑。
这个文件只处理交互体验、队列和内置斜杠命令，真正模型调用仍然走 SimpleAgent 或 gateway。
"""

import queue
import sys
import threading
import time
from contextlib import nullcontext

from ..agent.gateway import (
    gateway_paths,
    gateway_running,
    render_gateway_status,
    submit_gateway_ask,
    wait_for_gateway_response,
    wait_for_gateway_running,
)
from .common import CHAT_PROMPT, FALLBACK_CHAT_PROMPT, make_agent
from .models import ChatJob

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout
except ImportError:  # pragma: no cover - 让项目在无额外依赖时仍能跑
    PromptSession = None
    patch_stdout = None


def cmd_chat(args) -> int:
    """启动交互循环。"""

    agent = make_agent(args)
    use_gateway = bool(args.gateway)
    paths = gateway_paths(agent)
    if use_gateway:
        _, alive = wait_for_gateway_running(paths, timeout=10.0)
        if not alive:
            print("gateway 未在运行。请先执行: my-agent gateway start", file=sys.stderr)
            return 2
    print(
        f"{agent.config.agent_name} 交互循环已启动。"
        "输入 /help 查看命令，输入 /exit 或 /logout 退出，也可以直接按 Ctrl+C。"
    )
    if use_gateway:
        print("当前模式: gateway 客户端。普通消息会投递给后台 gateway 处理。")
    runtime_inject: list[str] = args.inject or []
    prompt_files: list[str] = args.prompt_file or []
    jobs: queue.Queue[ChatJob] = queue.Queue()
    state_lock = threading.Lock()
    is_running = False
    pending_jobs = 0
    shutting_down = False
    running_prompt = ""
    running_started_at = 0.0
    prompt_session = (
        PromptSession()
        if PromptSession is not None and sys.stdin.isatty() and sys.stdout.isatty()
        else None
    )
    fallback_interactive = prompt_session is None and sys.stdin.isatty() and sys.stdout.isatty()
    fallback_waiting_for_input = False

    def bottom_toolbar() -> str:
        with state_lock:
            active_count = pending_jobs + (1 if is_running else 0)
            elapsed = time.perf_counter() - running_started_at if is_running else 0
        if not active_count:
            return ""
        if is_running:
            return f"思考中... {elapsed:.0f}s | 队列 {pending_jobs}"
        return f"等待处理 | 队列 {pending_jobs}"

    def redraw_fallback_prompt() -> None:
        """Redraw the plain input prompt after background output.

        prompt_toolkit handles this automatically. The stdlib input() fallback
        does not, so a background reply can leave the terminal without a visible
        `user> ` prompt even though input is still waiting.
        """

        if not fallback_interactive:
            return
        with state_lock:
            should_redraw = fallback_waiting_for_input and not shutting_down
        if should_redraw:
            print(FALLBACK_CHAT_PROMPT, end="", flush=True)

    def worker() -> None:
        nonlocal is_running, pending_jobs, running_prompt, running_started_at
        while True:
            job = jobs.get()
            with state_lock:
                pending_jobs -= 1
                is_running = True
                running_prompt = job.user
                running_started_at = time.perf_counter()
            try:
                started_at = running_started_at
                print(f"\n正在处理: {job.user}", flush=True)
                if use_gateway:
                    _, alive = gateway_running(paths)
                    if not alive:
                        raise RuntimeError("gateway 已停止。请先执行: my-agent gateway start")
                    request_id, _, response_path = submit_gateway_ask(
                        paths,
                        prompt=job.user,
                        inject=job.inject,
                        prompt_files=job.prompt_files,
                        save=not args.no_save,
                        include_prompt=job.show_prompt,
                        agent=agent,
                    )
                    timeout = (
                        args.gateway_timeout
                        if args.gateway_timeout is not None
                        else agent.config.gateway_request_timeout
                    )
                    response = wait_for_gateway_response(paths, request_id, timeout)
                    elapsed = time.perf_counter() - started_at
                    if not response:
                        raise TimeoutError(
                            f"gateway 请求等待超时: request_id={request_id} response={response_path}"
                        )
                    if job.show_prompt and response.get("prompt"):
                        print("===== FINAL PROMPT =====")
                        print(response.get("prompt", ""))
                        print("===== RESPONSE =====")
                    print(
                        f"[耗时 {elapsed:.2f}s; gateway_request={request_id}; "
                        f"工具轮数 {response.get('tool_rounds', 0)}]"
                    )
                    if response.get("ok"):
                        print(f"{agent.config.agent_name}> {response.get('response', '')}")
                    else:
                        print(f"错误: {response.get('error', 'gateway 请求失败')}")
                else:
                    result = agent.run(
                        job.user,
                        inject=job.inject,
                        prompt_files=job.prompt_files,
                        save=not args.no_save,
                    )
                    elapsed = time.perf_counter() - started_at
                    if job.show_prompt:
                        print("===== FINAL PROMPT =====")
                        print(result.prompt)
                        print("===== RESPONSE =====")
                    print(f"[耗时 {elapsed:.2f}s; 工具轮数 {result.tool_rounds}]")
                    print(f"{agent.config.agent_name}> {result.response}")
            except Exception as exc:
                print(f"错误: {exc}")
            finally:
                with state_lock:
                    is_running = False
                    running_prompt = ""
                    running_started_at = 0.0
                jobs.task_done()
                redraw_fallback_prompt()

    threading.Thread(target=worker, daemon=True).start()

    def enqueue_job(user: str, *, show_prompt: bool = False) -> None:
        nonlocal pending_jobs
        job = ChatJob(
            user=user,
            show_prompt=show_prompt,
            inject=list(runtime_inject),
            prompt_files=list(prompt_files),
        )
        with state_lock:
            active_count = pending_jobs + (1 if is_running else 0)
            pending_jobs += 1
        jobs.put(job)
        if active_count:
            print(f"已加入任务队列，前面还有 {active_count} 个任务。")
        else:
            if use_gateway:
                print("已发送到 gateway 后台，模型响应期间可以继续输入。")
            else:
                print("已发送到后台，模型响应期间可以继续输入。")

    output_context = patch_stdout() if prompt_session is not None else nullcontext()
    with output_context:
        while True:
            try:
                if prompt_session is not None:
                    user = prompt_session.prompt(
                        CHAT_PROMPT,
                        bottom_toolbar=bottom_toolbar,
                        refresh_interval=1,
                    ).strip()
                else:
                    if fallback_interactive:
                        print(FALLBACK_CHAT_PROMPT, end="", flush=True)
                        with state_lock:
                            fallback_waiting_for_input = True
                        try:
                            user = input().strip()
                        finally:
                            with state_lock:
                                fallback_waiting_for_input = False
                    else:
                        user = input(FALLBACK_CHAT_PROMPT).strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见。")
                return 0
            if not user:
                continue
            if user.lower() in {"/exit", "/logout", "/quit", "exit", "logout", "退出"}:
                shutting_down = True
                with state_lock:
                    active_count = pending_jobs + (1 if is_running else 0)
                if active_count:
                    print(f"还有 {active_count} 个后台任务，等待完成后退出。按 Ctrl+C 可强制退出。")
                    jobs.join()
                print("再见。")
                return 0
            if user == "/help":
                print(
                    """可用命令：
/help                         显示帮助
/status                       查看后台任务状态；gateway 模式会额外显示 gateway 状态
/exit                         退出
/logout                       退出
exit / logout                 兼容旧习惯
/memory [关键词]              搜索记忆；不带关键词显示最近记忆
/remember <内容>              手动写入记忆
/btw                         显示当前运行时 prompt 注入
/btw <内容>                   增加运行时 prompt 注入
/btw-clear                   清空运行时 prompt 注入
/prompt-file <路径>           增加动态 prompt 文件
/subagents <数量> <目标>      生成 subagent 任务记录
/show-prompt <问题>           显示最终 prompt 并回答
Ctrl+C                        退出
其他输入                       正常对话
"""
                )
                continue
            if user == "/status":
                with state_lock:
                    active_count = pending_jobs + (1 if is_running else 0)
                    prompt = running_prompt
                    elapsed = time.perf_counter() - running_started_at if is_running else 0
                if not active_count:
                    print("当前没有后台任务。")
                elif is_running:
                    print(f"正在响应中，已等待 {elapsed:.0f}s；队列中还有 {pending_jobs} 个任务。")
                    print(f"当前任务: {prompt}")
                else:
                    print(f"当前没有运行中的任务；队列中还有 {pending_jobs} 个任务。")
                if use_gateway:
                    for line in render_gateway_status(agent, paths):
                        print(line)
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
            if user == "/btw":
                if not runtime_inject:
                    print("当前没有运行时 prompt 注入。")
                else:
                    print("当前运行时 prompt 注入：")
                    for index, item in enumerate(runtime_inject, 1):
                        print(f"{index}. {item}")
                continue
            if user.startswith("/btw "):
                runtime_inject.append(user[len("/btw ") :])
                print(f"已加入注入 prompt，当前 {len(runtime_inject)} 条。")
                continue
            if user == "/btw-clear":
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

            enqueue_job(user, show_prompt=show_prompt)
    return 0
