from __future__ import annotations

"""LLM: implements status, timeline, run, memory, local-search, local-doctor, and local-rebuild CLI commands.

给人看的解释：
这个文件只放和“本地状态/记忆/LocalStore”相关的命令。
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
from .local_doctor import build_local_doctor_report, build_status_suggestions, rebuild_local_store
from .thinking_spinner import ThinkingSpinner


def cmd_status(args) -> int:
    """显示 my-agent 当前全局状态。"""

    agent = make_agent(args)
    paths = gateway_paths(agent)
    local_stats = agent.local_store.stats()
    board = agent.subagents.build_board(recent_limit=args.limit)
    timeline = agent.local_store.timeline(limit=args.limit)
    pid, alive = gateway_running(paths)
    gateway_state = read_json_file(paths.state)
    heartbeat = read_json_file(paths.heartbeat)
    heartbeat_at = float(heartbeat.get("updated_at", 0) or 0)
    heartbeat_age = time.time() - heartbeat_at if heartbeat_at else 0
    gateway_status = "running" if alive else gateway_state.get("status", "stopped")
    if alive and heartbeat_at and heartbeat_age > agent.config.gateway_stale_seconds:
        gateway_status = "stale"

    # 检测进行中任务
    active_work_summary = None
    if agent.config.auto_detect_work_on_startup:
        from ..agent.startup_recovery import detect_active_work, format_active_work_summary
        active_work_summary = detect_active_work(agent)

    payload = {
        "agent_name": agent.config.agent_name,
        "workspace_root": str(agent.root),
        "gateway": {
            "status": gateway_status,
            "pid": pid,
            "alive": alive,
            "heartbeat_age_seconds": round(heartbeat_age, 1) if heartbeat_at else 0,
            "request_counts": gateway_request_counts(paths),
            "workspace": str(paths.root),
        },
        "local_store": local_stats,
        "subagents": {
            "summary": board.summary,
            "hot_count": len(board.hot_list),
            "recent_count": len(board.recent),
            "hot": [item.__dict__ for item in board.hot_list[: args.limit]],
            "recent": [item.__dict__ for item in board.recent[: args.limit]],
        },
        "active_work": {
            "gateway_alive": active_work_summary.gateway_alive if active_work_summary else False,
            "active_task_count": active_work_summary.active_task_count if active_work_summary else 0,
            "stale_request_count": active_work_summary.stale_request_count if active_work_summary else 0,
            "recent_tasks": active_work_summary.recent_tasks if active_work_summary else [],
        } if active_work_summary else None,
        "timeline": [item.__dict__ for item in timeline],
    }
    payload["suggested_actions"] = build_status_suggestions(agent, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print("MY-AGENT STATUS")
    print(f"agent={agent.config.agent_name}")
    print(f"workspace={agent.root}")
    print("")
    print("Gateway")
    print(f"- status={gateway_status} pid={pid if pid else '-'} alive={alive}")
    if heartbeat_at:
        print(f"- heartbeat_age_seconds={heartbeat_age:.1f}")
    print("- requests=" + json.dumps(gateway_request_counts(paths), ensure_ascii=False, sort_keys=True))
    print(f"- workspace={paths.root}")
    print("")
    print("Local Store")
    print(f"- records={local_stats['record_count']} events={local_stats['event_count']} fts5={local_stats['fts5_enabled']}")
    print(f"- db={local_stats['db_path']}")
    print("")
    # 显示进行中任务
    if active_work_summary:
        from ..agent.startup_recovery import has_active_work
        print("进行中任务")
        print("-" + format_active_work_summary(active_work_summary).replace("\n", "\n  - "))
        if active_work_summary.active_task_count > 0:
            from ..agent.startup_recovery import has_active_work
            print("  运行 my-agent subagents-dispatch 可继续调度")
    else:
        print("进行中任务")
        print("- 暂无")
    print("")
    print("Subagents")
    print("- summary=" + json.dumps(board.summary, ensure_ascii=False, sort_keys=True))
    if board.hot_list:
        print(f"- hot={len(board.hot_list)}")
        for item in board.hot_list[: args.limit]:
            flags = ",".join(item.risk_flags) if item.risk_flags else "ok"
            print(f"  - {item.id} {item.status}/{item.verification_status} flags={flags} :: {item.goal}")
    else:
        print("- hot=0")
    if args.recent:
        print("- recent:")
        for item in board.recent[: args.limit]:
            print(f"  - {item.id} {item.status}/{item.verification_status} :: {item.goal}")
    print("")
    print("Timeline")
    if not timeline:
        print("- 暂无事件")
    for item in timeline:
        source = f"{item.source_type}/{item.source_id}".strip("/")
        print(f"- {format_local_time(item.created_at)} {item.event_type} {source} :: {item.title}")
    print("")
    print("Suggested Actions")
    if payload["suggested_actions"]:
        for item in payload["suggested_actions"]:
            print(f"- {item}")
    else:
        print("- 暂无，当前没有明显需要立刻处理的事项。")
    return 0


def cmd_timeline(args) -> int:
    """显示 LocalStore 最近事件。"""

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
    """执行一次单轮请求。"""

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


def cmd_local_store_status(args) -> int:
    """显示本地事实源状态。"""

    agent = make_agent(args)
    print(json.dumps(agent.local_store.stats(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_local_search(args) -> int:
    """搜索本地事实源。"""

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
    """把现有 JSONL 记忆补建到本地事实源索引。"""

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


def cmd_local_doctor(args) -> int:
    """诊断 LocalStore、gateway 队列和 subagent 文件事实源的一致性。"""

    agent = make_agent(args)
    if args.repair:
        recovery = recover_gateway_processing_requests(
            gateway_paths(agent),
            startup=False,
            max_attempts=agent.config.gateway_request_max_attempts,
            timeout_seconds=agent.config.gateway_processing_timeout_seconds,
            agent=agent,
        )
    else:
        recovery = {}
    report = build_local_doctor_report(agent, limit=args.limit)
    if recovery:
        report["repair"] = {"gateway_processing_recovery": recovery}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report["ok"] else 1

    print("MY-AGENT LOCAL DOCTOR")
    print(f"ok={report['ok']}")
    print("stats=" + json.dumps(report["stats"], ensure_ascii=False, sort_keys=True))
    print("source_counts=" + json.dumps(report["source_counts"], ensure_ascii=False, sort_keys=True))
    if recovery:
        print("repair=" + json.dumps(report["repair"], ensure_ascii=False, sort_keys=True))
    for check in report["checks"]:
        mark = "OK" if check["ok"] else check["severity"]
        print(f"- [{mark}] {check['name']}: {check['message']}")
    if report["suggestions"]:
        print("")
        print("Suggested Actions")
        for item in report["suggestions"]:
            print(f"- {item}")
    return 0 if report["ok"] else 1


def cmd_local_rebuild(args) -> int:
    """从文件事实源重建 LocalStore 索引。"""

    agent = make_agent(args)
    requested = set(args.source or ["memory", "gateway", "subagent", "fts"])
    if "all" in requested:
        requested = {"memory", "gateway", "subagent", "fts"}
    allowed = {"memory", "gateway", "subagent", "fts"}
    unknown = sorted(requested - allowed)
    if unknown:
        print(f"未知 source: {', '.join(unknown)}", file=sys.stderr)
        return 2
    result = rebuild_local_store(agent, sources=requested, reset=args.reset)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0
