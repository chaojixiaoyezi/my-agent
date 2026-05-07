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

from ..agent.gateway import (
    gateway_paths,
    gateway_request_counts,
    gateway_running,
    read_json_file,
    recover_gateway_processing_requests,
)
from .common import format_local_time, make_agent, resume_context_override
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
    paths = gateway_paths(agent)
    local_stats = agent.local_store.stats()
    board = agent.subagents.build_board(recent_limit=args.limit)
    timeline = agent.local_store.timeline(limit=args.limit)
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

    # LLM: _on_run_chunk 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def _on_run_chunk(chunk: str) -> None:
        spinner.stop()
        sys.stdout.write(chunk)
        sys.stdout.flush()

    try:
        result = agent.run(
            args.prompt,
            inject=args.inject or [],
            prompt_files=args.prompt_file or [],
            save=args.save,
            source="cli_run",
            resume_context=resume_context_override(args),
            recovery_next_actions=["如需恢复本次单轮 run，先查看 memory-resume 和 LocalStore 记录。"],
            on_chunk=_on_run_chunk,
        )
    finally:
        spinner.stop()
    if args.show_prompt:
        print("===== FINAL PROMPT =====")
        print(result.prompt)
        print("===== RESPONSE =====")
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
    return 0


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
    records = agent.memory.all()[-args.limit :]
    for rec in records:
        print(json.dumps(rec.__dict__, ensure_ascii=False))
    return 0


# LLM: cmd_memory_search 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_memory_search(args) -> int:

    agent = make_agent(args)
    for rec in agent.recall(args.query, args.limit):
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
