# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。

from __future__ import annotations

"""implements status, timeline, run, memory, local-search, local-doctor, and local-rebuild CLI commands.

给人看的解释：
这个文件只放和'本地状态/记忆/LocalStore'相关的命令。
它会调用 local_doctor 里的诊断规则，也会调用 gateway/subagent 的公开 API 取状态。
"""

import json
import sys
import time

from ..agent.backends import ProviderTimeoutError, provider_timeout_report
from ..agent.gateway import (
    gateway_paths,
    gateway_request_counts,
    gateway_running,
    read_json_file,
    recover_gateway_processing_requests,
)
from ..agent.subagents.models import SubAgentBoardOptions
from .common import format_local_time, make_agent, resume_context_override
from .delivery_contracts import delivery_contract_from_file
from .local_doctor import build_status_suggestions
from .local_repair_commands import (
    build_local_doctor_report,
    cmd_local_doctor,
    cmd_local_rebuild,
    rebuild_local_store,
)
from .local_status_payload import (
    GatewayStatusRequest,
    StatusPayloadContext,
    build_status_payload,
    resolve_gateway_status,
)
from .local_status_view import StatusPrintContext, print_status_human
from .models import LocalSearchOptions, TimelineOptions
from .thinking_spinner import ThinkingSpinner


# LLM: cmd_status 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_status(args) -> int:

    agent = make_agent(args)
    limit = _config_int(agent, args, "limit", "cli_status_limit")
    paths = gateway_paths(agent)
    local_stats = agent.local_store.stats()
    board = agent.subagents.build_board(
        options=SubAgentBoardOptions(
            recent_limit=limit,
            include_child_status_counts=False,
        )
    )
    timeline = agent.local_store.timeline(limit=limit)
    pid, alive = gateway_running(paths)
    gateway_state = read_json_file(paths.state)
    heartbeat = read_json_file(paths.heartbeat)
    gateway_status, heartbeat_age = resolve_gateway_status(
        GatewayStatusRequest(
            alive=alive,
            gateway_state=gateway_state,
            heartbeat=heartbeat,
            stale_seconds=agent.config.gateway_stale_seconds,
            now=time.time(),
        )
    )

    # 检测进行中任务
    active_work_summary = None
    if agent.config.auto_detect_work_on_startup:
        from ..agent.startup_recovery import detect_active_work
        active_work_summary = detect_active_work(agent)

    request_counts = gateway_request_counts(paths)
    payload_ctx = StatusPayloadContext(agent, paths, local_stats, board, timeline, pid, alive, gateway_status, heartbeat_age, active_work_summary, request_counts)
    payload = build_status_payload(payload_ctx)
    payload["suggestions"] = build_status_suggestions(agent, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print_ctx = StatusPrintContext(
        agent, paths, local_stats, board, timeline, gateway_status, pid, alive, heartbeat_age, active_work_summary,
        request_counts, payload["suggestions"],
    )
    print_status_human(print_ctx)
    return 0


# LLM: cmd_timeline 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_timeline(args) -> int:

    agent = make_agent(args)
    _apply_default_arg_limit(args, agent, "cli_timeline_limit")
    options = _timeline_options(args)
    items = agent.local_store.timeline(
        limit=options.limit,
        source_type=options.source_type,
        event_type=options.event_type,
    )
    if options.json:
        print(json.dumps([item.__dict__ for item in items], ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print("MY-AGENT TIMELINE")
    if options.source_type:
        print(f"source_type={options.source_type}")
    if options.event_type:
        print(f"event_type={options.event_type}")
    if not items:
        print("暂无事件。")
        return 0
    for item in items:
        source = f"{item.source_type}/{item.source_id}".strip("/")
        title = item.title or "-"
        print(f"- {format_local_time(item.created_at)} {item.event_type} {source} :: {title}")
        if options.details:
            print("  payload=" + json.dumps(item.payload, ensure_ascii=False, sort_keys=True))
    return 0


# LLM: cmd_run 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_run(args) -> int:

    agent = make_agent(args)
    spinner = ThinkingSpinner()
    spinner.start()
    stream_state = {"seen": False, "text": ""}
    on_chunk = _make_run_chunk_writer(spinner, stream_state)

    try:
        result = agent.run(
            args.prompt,
            inject=args.inject or [],
            prompt_files=args.prompt_file or [],
            save=args.save,
            source="cli_run",
            delivery_contract=delivery_contract_from_file(getattr(args, "delivery_contract_file", "")),
            resume_context=resume_context_override(args),
            recovery_next_actions=["如需恢复本次单轮 run，先查看 memory-resume 和 LocalStore 记录。"],
            on_chunk=on_chunk,
        )
    except ProviderTimeoutError as exc:
        print(_provider_timeout_cli_report(agent, exc))
        return 2
    finally:
        spinner.stop()
    _print_run_result(result, show_prompt=args.show_prompt, streamed_text=str(stream_state["text"]))
    return 0


# LLM: _make_run_chunk_writer keeps streaming stdout state out of cmd_run.
# 函数用途: 生成 run 的流式输出回调，并记录是否已经向终端写过 response 正文。
def _make_run_chunk_writer(spinner: ThinkingSpinner, stream_state: dict[str, object]):
    # LLM: _on_run_chunk is the tiny stdout sink used by streaming CLI runs.
    # 函数用途: 收到模型流式片段时停止 spinner、写入终端，并记录正文已流式输出。
    def _on_run_chunk(chunk: str) -> None:
        stream_state["seen"] = True
        stream_state["text"] = str(stream_state.get("text", "")) + chunk
        spinner.stop()
        sys.stdout.write(chunk)
        sys.stdout.flush()

    return _on_run_chunk


# LLM: _print_run_result prints final CLI metadata without hiding post-tool final answers.
# 函数用途: 输出 run 的最终文本、调试 prompt、统计信息和 compact 建议；已完整流式打印的正文不重复打印。
def _print_run_result(result, *, show_prompt: bool, streamed_text: str = "") -> None:
    if show_prompt:
        print("===== FINAL PROMPT =====")
        print(result.prompt)
        print("===== RESPONSE =====")
    if _should_print_final_response(str(result.response), streamed_text):
        if streamed_text and not streamed_text.endswith("\n"):
            print()
        print(result.response)
    snapshot_state = "error" if result.recovery_snapshot_error else "1" if result.recovery_snapshot_path else "0"
    print(
        f"\n[backend={result.backend}; used_memories={result.used_memories}; "
        f"tool_rounds={result.tool_rounds}; routed_rules={result.memory_route_matches}; "
        f"prompt_tokens≈{result.prompt_token_estimate}; inject_tokens≈{result.runtime_injection_token_estimate}; "
        f"archive_events={result.archive_events}; "
        f"recovery_snapshot={snapshot_state}; "
        f"resume_context={1 if result.memory_resume_context_injected else 0}; "
        f"resume_tokens≈{result.memory_resume_context_token_estimate}]"
    )
    _print_compact_suggestion(result)


# LLM: _should_print_final_response separates streamed-visible text from hidden post-tool final responses.
# 函数用途: 判断最终 response 是否已经完整出现在流式输出中，避免重复打印或吞掉工具后的最终回答。
def _should_print_final_response(response: str, streamed_text: str) -> bool:
    if not response:
        return False
    return response not in streamed_text


# LLM: _provider_timeout_cli_report converts backend timeout exceptions into a readable command result.
# 函数用途: 顶层 run 超时时输出恢复提示并退出，不让用户面对长堆栈或沉默等待。
def _provider_timeout_cli_report(agent, exc: ProviderTimeoutError) -> str:
    return provider_timeout_report(
        exc,
        timeout_seconds=getattr(getattr(agent, "config", None), "request_timeout", ""),
    )


# LLM: _print_compact_suggestion keeps run CLI compact output out of cmd_run size-sensitive orchestration.
# 函数用途: 打印 compact 建议、auto cycle 停车状态和推荐命令；只读 result，不触发 apply 或 resume。
def _print_compact_suggestion(result) -> None:
    if not result.memory_compact_suggested:
        return
    print(f"[compact_suggestion={result.memory_compact_status}; {result.memory_compact_message}]")
    print(
        "[compact_auto="
        f"{result.memory_compact_auto_status}; next={result.memory_compact_auto_next_action}; "
        f"tools={result.memory_compact_auto_tool_execution}; "
        f"continue_ready={result.memory_compact_auto_continue_ready}; "
        f"apply_id={result.memory_compact_auto_apply_id or '-'}]"
    )
    for command in result.memory_compact_commands or []:
        print(f"- {command}")


# LLM: cmd_remember 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_remember(args) -> int:

    agent = make_agent(args)
    rec = agent.remember(args.content, kind=args.kind)
    print(json.dumps(rec.__dict__, ensure_ascii=False, indent=2))
    return 0


# LLM: cmd_memory_list 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_memory_list(args) -> int:

    agent = make_agent(args)
    limit = _config_int(agent, args, "limit", "cli_memory_list_limit")
    records = agent.memory.all()[-limit:]
    for rec in records:
        print(json.dumps(rec.__dict__, ensure_ascii=False))
    return 0


# LLM: cmd_memory_search 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_memory_search(args) -> int:

    agent = make_agent(args)
    limit = _config_int(agent, args, "limit", "cli_memory_search_limit")
    for rec in agent.recall(args.query, limit):
        print(json.dumps(rec.__dict__, ensure_ascii=False))
    return 0


# LLM: cmd_local_store_status 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_local_store_status(args) -> int:

    agent = make_agent(args)
    print(json.dumps(agent.local_store.stats(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


# LLM: cmd_local_search 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_local_search(args) -> int:

    agent = make_agent(args)
    _apply_default_arg_limit(args, agent, "cli_local_search_limit")
    if getattr(args, "preview_chars", None) is None:
        args.preview_chars = int(getattr(agent.config, "cli_local_search_preview_chars", 500) or 0)
    options = _local_search_options(args)
    hits = agent.local_store.search(
        options.query,
        limit=options.limit,
        source_type=options.source_type,
        visibility=options.visibility,
    )
    for hit in hits:
        payload = hit.__dict__.copy()
        if options.preview_chars >= 0:
            payload["content"] = payload["content"][: options.preview_chars]
        print(json.dumps(payload, ensure_ascii=False))
    return 0


# LLM: cmd_local_index_memory 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_local_index_memory(args) -> int:

    agent = make_agent(args)
    count = agent.memory.index_all()
    print(
        json.dumps(
            {
                "indexed": count,
                "stats": agent.local_store.stats(),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


# LLM: _timeline_options 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _timeline_options(args) -> TimelineOptions:
    return TimelineOptions(
        limit=int(args.limit or 0),
        source_type=args.source_type,
        event_type=args.event_type,
        json=bool(args.json),
        details=bool(args.details),
    )


# LLM: _config_int resolves CLI optional defaults from AgentConfig after make_agent is available.
# 函数用途: argparse 无法提前读取配置时，在命令执行层把 None 转为配置文件里的默认值。
def _config_int(agent, args, arg_name: str, config_name: str) -> int:
    value = getattr(args, arg_name, None)
    if value is not None:
        return int(value)
    return int(getattr(agent.config, config_name, 0) or 0)


# LLM: _apply_default_arg_limit keeps existing option bundle helpers unchanged while moving defaults to config.
# 函数用途: 在调用旧 helper 前把 args.limit 补成后端配置值。
def _apply_default_arg_limit(args, agent, config_name: str) -> None:
    if getattr(args, "limit", None) is None:
        args.limit = int(getattr(agent.config, config_name, 0) or 0)


# LLM: _local_search_options 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _local_search_options(args) -> LocalSearchOptions:
    return LocalSearchOptions(
        query=args.query,
        limit=int(args.limit or 0),
        source_type=args.source_type,
        visibility=args.visibility,
        preview_chars=int(args.preview_chars),
    )
