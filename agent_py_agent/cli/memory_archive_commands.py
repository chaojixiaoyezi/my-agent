from __future__ import annotations

"""LLM: CLI entrypoints for memory archive list/search/resume reports.

新手说明:
这个文件只管命令入口和打印。
真正的归档读取、过滤和恢复线索整理在 `agent.memory_archive.query`，避免 CLI 文件重新变成大杂烩。
"""

import json
from typing import Any

from ..agent.memory_archive.query import (
    archive_filters_from_args,
    build_resume_guidance,
    collect_archive_records,
    collect_gateway_payloads,
    collect_resume_task_ids,
    collect_task_payloads,
    filter_archive_records,
    local_hit_payload,
    resume_local_query,
    strip_sort_keys,
)
from ..agent.memory_archive.resume_brief import build_resume_brief
from .common import make_agent


def cmd_memory_archive_list(args) -> int:
    """LLM: list recent raw archive and hook snapshot records.

    新手说明:
    这条命令用来回答'最近到底落盘了哪些记忆归档'。
    它会把 raw 事件和 hook 快照摊成统一字段，方便肉眼扫，也方便脚本继续处理。

    参数说明:
    `args` 是 argparse 对象，包含 `layer`、`date`、`limit`、`json` 和通用 agent 参数。

    返回说明:
    返回 CLI 退出码，打印成功时为 0。
    """

    agent = make_agent(args)
    records = collect_archive_records(
        agent.root,
        layer=args.layer,
        date_key=args.date,
        limit=args.limit,
        level=getattr(args, "level", None),
    )
    payload = {
        "ok": True,
        "workspace_root": str(agent.root),
        "layer": args.layer,
        "date": args.date or "",
        "level": getattr(args, "level", None),
        "limit": args.limit,
        "records": records,
    }
    _print_archive_list(payload, json_output=args.json)
    return 0


def cmd_memory_archive_search(args) -> int:
    """LLM: search raw archive and hook snapshots with structured filters.

    新手说明:
    这条命令不是只搜一个关键词。
    它可以同时按 session、request、run、工具名、状态、说话对象等字段过滤，适合排查'刚刚那轮到底发生了什么'。

    参数说明:
    `args` 是 argparse 对象，包含关键词、字段过滤、时间窗口、layer/date/limit/json 等。

    返回说明:
    返回 CLI 退出码，打印成功时为 0。
    """

    agent = make_agent(args)
    records = collect_archive_records(agent.root, layer=args.layer, date_key=args.date, limit=0)
    filters = archive_filters_from_args(args)
    matches = filter_archive_records(
        records,
        query=args.query or "",
        filters=filters,
        since=args.since,
        until=args.until,
        level=getattr(args, "level", None),
    )[: args.limit]
    payload = {
        "ok": True,
        "workspace_root": str(agent.root),
        "query": args.query or "",
        "filters": filters,
        "date": args.date or "",
        "since": args.since or "",
        "until": args.until or "",
        "limit": args.limit,
        "matches": matches,
    }
    _print_archive_search(payload, json_output=args.json)
    return 0


def _collect_resume_data(agent, args):
    """收集 resume 所需的归档匹配、本地命中、任务事实源和 gateway 事实源。"""
    archive_records = collect_archive_records(agent.root, layer=args.layer, date_key=args.date, limit=0)
    filters = archive_filters_from_args(args)
    archive_matches = filter_archive_records(
        archive_records, query=args.query or "", filters=filters,
        since=args.since, until=args.until, level=getattr(args, "level", None),
    )[:args.limit]
    local_query = resume_local_query(args, archive_matches)
    local_hits = (
        agent.local_store.search(local_query, limit=args.limit)
        if local_query else agent.local_store.list_recent(limit=args.limit)
    )
    local_payloads = [local_hit_payload(hit) for hit in local_hits]
    task_ids = collect_resume_task_ids(args, archive_matches, local_payloads)
    task_payloads = collect_task_payloads(agent, task_ids, limit=args.limit)
    gateway_payloads = collect_gateway_payloads(local_payloads, limit=args.limit)
    return filters, archive_matches, local_payloads, task_payloads, gateway_payloads


def cmd_memory_resume(args) -> int:
    """LLM: build a recovery brief from archive clues, LocalStore hits, and task fact sources.

    新手说明:
    用户说'继续'时，最怕模型只靠印象猜。
    这条命令先把可检索线索找出来，再把任务目录这些权威事实源列出来，帮助下一步真正恢复现场。
    如果线索来自 gateway 请求，它会额外列出 gateway request/response JSON，避免用户只看 LocalStore 摘要。

    参数说明:
    `args` 是 argparse 对象，包含 query、过滤字段、layer/date/limit/json/context_only 等。

    返回说明:
    返回 CLI 退出码；`--context-only` 时只打印恢复块。
    """
    agent = make_agent(args)
    filters, archive_matches, local_payloads, task_payloads, gateway_payloads = _collect_resume_data(agent, args)
    resume = build_resume_guidance(archive_matches, local_payloads, task_payloads, gateway_payloads)
    brief = build_resume_brief(
        archive_matches, local_payloads, task_payloads,
        recommended_read_paths=resume["recommended_read_paths"],
        next_actions=resume["next_actions"],
    )
    payload = {
        "ok": True, "workspace_root": str(agent.root), "query": args.query or "",
        "filters": filters, "archive_matches": archive_matches,
        "local_matches": local_payloads, "task_fact_sources": task_payloads,
        "gateway_fact_sources": gateway_payloads, "resume": resume, "brief": brief,
    }
    if getattr(args, "context_only", False):
        print(brief["context_block"])
        return 0
    _print_memory_resume(payload, json_output=args.json)
    return 0


def _print_archive_list(payload: dict[str, Any], *, json_output: bool) -> None:
    """LLM: render memory-archive-list payload as JSON or compact text.

    新手说明:
    JSON 给测试和脚本，文本给人扫。
    文本只打印最关键的字段，完整 payload 仍可用 `--json` 看。

    参数说明:
    `payload` 是 list 命令报告；`json_output` 控制输出格式。

    返回说明:
    不返回值；直接打印。
    """

    if json_output:
        print(json.dumps(strip_sort_keys(payload), ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("MY-AGENT MEMORY ARCHIVE LIST")
    print(f"workspace={payload['workspace_root']}")
    level_text = payload.get("level")
    print(
        f"layer={payload['layer']} date={payload['date'] or '-'} "
        f"level={level_text if level_text is not None else '-'} records={len(payload['records'])}"
    )
    _print_archive_record_lines(payload["records"])


def _print_archive_search(payload: dict[str, Any], *, json_output: bool) -> None:
    """LLM: render memory-archive-search payload as JSON or compact text.

    新手说明:
    搜索结果会告诉你命中在哪个文件第几行，方便继续打开原始证据。

    参数说明:
    `payload` 是 search 命令报告；`json_output` 控制输出格式。

    返回说明:
    不返回值；直接打印。
    """

    if json_output:
        print(json.dumps(strip_sort_keys(payload), ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("MY-AGENT MEMORY ARCHIVE SEARCH")
    print(f"workspace={payload['workspace_root']}")
    print(f"query={payload['query'] or '-'} matches={len(payload['matches'])}")
    if payload["filters"]:
        print("filters=" + json.dumps(payload["filters"], ensure_ascii=False, sort_keys=True))
    _print_archive_record_lines(payload["matches"])


def _print_resume_brief(brief: dict[str, Any]) -> None:
    """打印 Recovery Brief 区段。"""
    print("Recovery Brief")
    print(f"- latest_user_intent: {brief['latest_user_intent'] or 'unknown'}")
    print(f"- latest_assistant_action: {brief['latest_assistant_action'] or 'unknown'}")
    print("- related_ids=" + json.dumps(brief["related_ids"], ensure_ascii=False, sort_keys=True))
    if brief["likely_task_statuses"]:
        for item in brief["likely_task_statuses"][:5]:
            print(f"- task_status: {item['run_id']} {item['status']}/{item['verification_status']} :: {item['goal']}")
    else:
        print("- task_status: none")
    print(f"- authority: {brief['authority_note']}")


def _print_task_fact_sources(task_payloads: list[dict[str, Any]]) -> None:
    """打印 Task Fact Sources 区段。"""
    print("Task Fact Sources")
    if not task_payloads:
        print("- none")
    for task in task_payloads:
        if not task.get("exists"):
            print(f"- {task['run_id']} missing :: {task.get('error', '')}")
            continue
        print(f"- {task['run_id']} {task['status']}/{task['verification_status']} :: {task['goal']}")
        print(f"  task_dir={task['task_dir']}")


def _print_gateway_fact_sources(gateway_payloads: list[dict[str, Any]]) -> None:
    """打印 Gateway Fact Sources 区段。"""
    print("Gateway Fact Sources")
    if not gateway_payloads:
        print("- none")
    for gw in gateway_payloads:
        print(f"- {gw['request_id']} status={gw['status'] or '-'} ok={gw['ok']}")
        for path in gw["recommended_read_paths"]:
            print(f"  - {path}")


def _print_memory_resume_text(payload: dict[str, Any]) -> None:
    """以文本格式打印 resume 报告。"""
    print("MY-AGENT MEMORY RESUME")
    print(f"workspace={payload['workspace_root']}")
    print(f"query={payload['query'] or '-'}")
    resume = payload["resume"]
    print("summary=" + json.dumps({
        "archive": resume["archive_match_count"],
        "local": resume["local_match_count"],
        "tasks": resume["task_fact_source_count"],
    }, ensure_ascii=False, sort_keys=True))
    _print_resume_brief(payload["brief"])
    print("Archive Clues")
    _print_archive_record_lines(payload["archive_matches"][:5])
    _print_task_fact_sources(payload["task_fact_sources"])
    _print_gateway_fact_sources(payload["gateway_fact_sources"])
    print("Recommended Reads")
    for path in resume["recommended_read_paths"] or ["none"]:
        print(f"- {path}")
    print("Next Actions")
    for action in resume["next_actions"]:
        print(f"- {action}")


def _print_memory_resume(payload: dict[str, Any], *, json_output: bool) -> None:
    """LLM: render memory-resume payload as JSON or compact text.

    新手说明:
    文本输出按'线索 -> 事实源 -> 下一步'排，提醒人先读权威文件再继续干活。

    参数说明:
    `payload` 是 resume 命令报告；`json_output` 控制输出格式。

    返回说明:
    不返回值；直接打印。
    """
    if json_output:
        print(json.dumps(strip_sort_keys(payload), ensure_ascii=False, indent=2, sort_keys=True))
        return
    _print_memory_resume_text(payload)


def _print_archive_record_lines(records: list[dict[str, Any]]) -> None:
    """LLM: print normalized archive records as one-line recovery clues.

    新手说明:
    一行只放 ID、动作、状态、run_id 和原始文件位置。
    这样人在终端里可以快速决定下一步打开哪个文件。

    参数说明:
    `records` 是标准化归档记录列表。

    返回说明:
    不返回值；直接打印。
    """

    if not records:
        print("- none")
        return
    for record in records:
        ident = record.get("id", "-")
        action = record.get("action", "-") or "-"
        status = record.get("status", "-") or "-"
        preview = str(record.get("content_preview", "") or "").replace("\n", " ")[:120]
        print(
            f"- {record['layer']} {ident} action={action} status={status} "
            f"run={record.get('run_id') or '-'} file={record['file_path']}:{record['line_no']} :: {preview}"
        )
