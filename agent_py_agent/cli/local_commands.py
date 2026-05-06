from __future__ import annotations

"""LLM: implements status, timeline, run, memory, local-search, local-doctor, and local-rebuild CLI commands.

给人看的解释：
这个文件只放和'本地状态/记忆/LocalStore'相关的命令。
它会调用 local_doctor 里的诊断规则，也会调用 gateway/subagent 的公开 API 取状态。
"""

import json
import sys
import time
from dataclasses import dataclass
from typing import Any

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
from .local_status_payload import StatusPayloadContext, build_status_payload, resolve_gateway_status
from .thinking_spinner import ThinkingSpinner


def _format_gateway_section(gateway_status: str, pid: int | None, alive: bool, heartbeat_age: float, paths) -> None:
    print("Gateway")
    print(f"- status={gateway_status} pid={pid if pid else '-'} alive={alive}")
    if heartbeat_age:
        print(f"- heartbeat_age_seconds={heartbeat_age:.1f}")
    print("- requests=" + json.dumps(gateway_request_counts(paths), ensure_ascii=False, sort_keys=True))
    print(f"- workspace={paths.root}")


def _format_active_work(active_work_summary) -> None:
    from ..agent.startup_recovery import format_active_work_summary
    print("进行中任务")
    print("-" + format_active_work_summary(active_work_summary).replace("\n", "\n  - "))
    if active_work_summary.active_task_count > 0:
        print("  运行 my-agent subagents-dispatch 可继续调度")


def _format_subagents_section(board, limit: int) -> None:
    print("Subagents")
    print("- summary=" + json.dumps(board.summary, ensure_ascii=False, sort_keys=True))
    if board.hot_list:
        print(f"- hot={len(board.hot_list)}")
        for item in board.hot_list[: limit]:
            flags = ",".join(item.risk_flags) if item.risk_flags else "ok"
            print(f"  - {item.id} {item.status}/{item.verification_status} flags={flags} :: {item.goal}")
    else:
        print("- hot=0")
    if board.recent:
        print("- recent:")
        for item in board.recent[: limit]:
            print(f"  - {item.id} {item.status}/{item.verification_status} :: {item.goal}")


def _format_timeline(timeline) -> None:
    print("Timeline")
    if not timeline:
        print("- 暂无事件")
    for item in timeline:
        source = f"{item.source_type}/{item.source_id}".strip("/")
        print(f"- {format_local_time(item.created_at)} {item.event_type} {source} :: {item.title}")


@dataclass
class _StatusPrintContext:
    agent: Any
    paths: Any
    local_stats: dict
    board: Any
    timeline: list
    gateway_status: str
    pid: int | None
    alive: bool
    heartbeat_age: float
    active_work_summary: Any
    suggested_actions: list


def _print_status_human(ctx: _StatusPrintContext):
    agent = ctx.agent
    print("MY-AGENT STATUS")
    print(f"agent={agent.config.agent_name}")
    print(f"workspace={agent.root}")
    print("")
    _format_gateway_section(ctx.gateway_status, ctx.pid, ctx.alive, ctx.heartbeat_age, ctx.paths)
    print("")
    print("Local Store")
    print(f"- records={ctx.local_stats['record_count']} events={ctx.local_stats['event_count']} fts5={ctx.local_stats['fts5_enabled']}")
    print(f"- db={ctx.local_stats['db_path']}")
    print("")
    if ctx.active_work_summary:
        _format_active_work(ctx.active_work_summary)
    else:
        print("进行中任务")
        print("- 暂无")
    print("")
    _format_subagents_section(ctx.board, agent.config.subagent_board_limit)
    print("")
    _format_timeline(ctx.timeline)
    print("")
    print("Suggested Actions")
    if ctx.suggested_actions:
        for item in ctx.suggested_actions:
            print(f"- {item}")
    else:
        print("- 暂无，当前没有明显需要立刻处理的事项。")


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
        alive=alive,
        gateway_state=gateway_state,
        heartbeat=heartbeat,
        stale_seconds=agent.config.gateway_stale_seconds,
        now=time.time(),
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

    print_ctx = _StatusPrintContext(agent, paths, local_stats, board, timeline, gateway_status, pid, alive, heartbeat_age, active_work_summary, payload["suggestions"])
    _print_status_human(print_ctx)
    return 0


def cmd_timeline(args) -> int:

    agent = make_agent(args)
    items = agent.local_store.timeline(
        limit=args.limit,
        source_type=args.source_type,
        event_type=args.event_type,
    )
    if args.json:
        print(json.dumps([item.__dict__ for item in items], ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print("MY-AGENT TIMELINE")
    if args.source_type:
        print(f"source_type={args.source_type}")
    if args.event_type:
        print(f"event_type={args.event_type}")
    if not items:
        print("暂无事件。")
        return 0
    for item in items:
        source = f"{item.source_type}/{item.source_id}".strip("/")
        title = item.title or "-"
        print(f"- {format_local_time(item.created_at)} {item.event_type} {source} :: {title}")
        if args.details:
            print("  payload=" + json.dumps(item.payload, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_run(args) -> int:

    agent = make_agent(args)
    spinner = ThinkingSpinner()
    spinner.start()

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


def cmd_remember(args) -> int:

    agent = make_agent(args)
    rec = agent.remember(args.content, kind=args.kind)
    print(json.dumps(rec.__dict__, ensure_ascii=False, indent=2))
    return 0


def cmd_memory_list(args) -> int:

    agent = make_agent(args)
    records = agent.memory.all()[-args.limit :]
    for rec in records:
        print(json.dumps(rec.__dict__, ensure_ascii=False))
    return 0


def cmd_memory_search(args) -> int:

    agent = make_agent(args)
    for rec in agent.recall(args.query, args.limit):
        print(json.dumps(rec.__dict__, ensure_ascii=False))
    return 0


def cmd_local_store_status(args) -> int:

    agent = make_agent(args)
    print(json.dumps(agent.local_store.stats(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_local_search(args) -> int:

    agent = make_agent(args)
    hits = agent.local_store.search(
        args.query,
        limit=args.limit,
        source_type=args.source_type,
        visibility=args.visibility,
    )
    for hit in hits:
        payload = hit.__dict__.copy()
        if args.preview_chars >= 0:
            payload["content"] = payload["content"][: args.preview_chars]
        print(json.dumps(payload, ensure_ascii=False))
    return 0


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
