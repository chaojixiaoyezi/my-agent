from __future__ import annotations

"""LLM: CLI entrypoints for memory archive list/search/resume reports.

给人看的解释：
这个文件只管命令入口和打印。
真正的归档读取、过滤和恢复线索整理在 `agent.memory_archive.query`，避免 CLI 文件重新变成大杂烩。
"""

import json
from typing import Any

from .common import make_agent
from ..agent.memory_archive.query import (
    archive_filters_from_args,
    build_resume_guidance,
    collect_archive_records,
    collect_resume_task_ids,
    collect_task_payloads,
    filter_archive_records,
    local_hit_payload,
    resume_local_query,
    strip_sort_keys,
)
from ..agent.memory_archive.resume_brief import build_resume_brief


def cmd_memory_archive_list(args) -> int:
    """LLM: list recent raw archive and hook snapshot records.

    大白话：这条命令用来回答“最近到底落盘了哪些记忆归档”。
    它会把 raw 事件和 hook 快照摊成统一字段，方便肉眼扫，也方便脚本继续处理。
    """

    agent = make_agent(args)
    records = collect_archive_records(agent.root, layer=args.layer, date_key=args.date, limit=args.limit)
    payload = {
        "ok": True,
        "workspace_root": str(agent.root),
        "layer": args.layer,
        "date": args.date or "",
        "limit": args.limit,
        "records": records,
    }
    _print_archive_list(payload, json_output=args.json)
    return 0


def cmd_memory_archive_search(args) -> int:
    """LLM: search raw archive and hook snapshots with structured filters.

    大白话：这条命令不是只搜一个关键词。
    它可以同时按 session、request、run、工具名、状态、说话对象等字段过滤，适合排查“刚刚那轮到底发生了什么”。
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


def cmd_memory_resume(args) -> int:
    """LLM: build a recovery brief from archive clues, LocalStore hits, and task fact sources.

    大白话：用户说“继续”时，最怕模型只靠印象猜。
    这条命令先把可检索线索找出来，再把任务目录这些权威事实源列出来，帮助下一步真正恢复现场。
    """

    agent = make_agent(args)
    archive_records = collect_archive_records(agent.root, layer=args.layer, date_key=args.date, limit=0)
    filters = archive_filters_from_args(args)
    archive_matches = filter_archive_records(
        archive_records,
        query=args.query or "",
        filters=filters,
        since=args.since,
        until=args.until,
    )[: args.limit]
    local_query = resume_local_query(args, archive_matches)
    local_hits = (
        agent.local_store.search(local_query, limit=args.limit)
        if local_query
        else agent.local_store.list_recent(limit=args.limit)
    )
    local_payloads = [local_hit_payload(hit) for hit in local_hits]
    task_ids = collect_resume_task_ids(args, archive_matches, local_payloads)
    task_payloads = collect_task_payloads(agent, task_ids, limit=args.limit)
    resume = build_resume_guidance(archive_matches, local_payloads, task_payloads)
    brief = build_resume_brief(
        archive_matches,
        local_payloads,
        task_payloads,
        recommended_read_paths=resume["recommended_read_paths"],
        next_actions=resume["next_actions"],
    )
    payload = {
        "ok": True,
        "workspace_root": str(agent.root),
        "query": args.query or "",
        "filters": filters,
        "archive_matches": archive_matches,
        "local_matches": local_payloads,
        "task_fact_sources": task_payloads,
        "resume": resume,
        "brief": brief,
    }
    if getattr(args, "context_only", False):
        print(brief["context_block"])
        return 0
    _print_memory_resume(payload, json_output=args.json)
    return 0


def _print_archive_list(payload: dict[str, Any], *, json_output: bool) -> None:
    """LLM: render memory-archive-list payload as JSON or compact text.

    大白话：JSON 给测试和脚本，文本给人扫。
    文本只打印最关键的字段，完整 payload 仍可用 `--json` 看。
    """

    if json_output:
        print(json.dumps(strip_sort_keys(payload), ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("MY-AGENT MEMORY ARCHIVE LIST")
    print(f"workspace={payload['workspace_root']}")
    print(f"layer={payload['layer']} date={payload['date'] or '-'} records={len(payload['records'])}")
    _print_archive_record_lines(payload["records"])


def _print_archive_search(payload: dict[str, Any], *, json_output: bool) -> None:
    """LLM: render memory-archive-search payload as JSON or compact text.

    大白话：搜索结果会告诉你命中在哪个文件第几行，方便继续打开原始证据。
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


def _print_memory_resume(payload: dict[str, Any], *, json_output: bool) -> None:
    """LLM: render memory-resume payload as JSON or compact text.

    大白话：文本输出按“线索 -> 事实源 -> 下一步”排，提醒人先读权威文件再继续干活。
    """

    if json_output:
        print(json.dumps(strip_sort_keys(payload), ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("MY-AGENT MEMORY RESUME")
    print(f"workspace={payload['workspace_root']}")
    print(f"query={payload['query'] or '-'}")
    print(
        "summary="
        + json.dumps(
            {
                "archive": payload["resume"]["archive_match_count"],
                "local": payload["resume"]["local_match_count"],
                "tasks": payload["resume"]["task_fact_source_count"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    brief = payload["brief"]
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
    print("Archive Clues")
    _print_archive_record_lines(payload["archive_matches"][:5])
    print("Task Fact Sources")
    if not payload["task_fact_sources"]:
        print("- none")
    for task in payload["task_fact_sources"]:
        if not task.get("exists"):
            print(f"- {task['run_id']} missing :: {task.get('error', '')}")
            continue
        print(f"- {task['run_id']} {task['status']}/{task['verification_status']} :: {task['goal']}")
        print(f"  task_dir={task['task_dir']}")
    print("Recommended Reads")
    for path in payload["resume"]["recommended_read_paths"] or ["none"]:
        print(f"- {path}")
    print("Next Actions")
    for action in payload["resume"]["next_actions"]:
        print(f"- {action}")


def _print_archive_record_lines(records: list[dict[str, Any]]) -> None:
    """LLM: print normalized archive records as one-line recovery clues.

    大白话：一行只放 ID、动作、状态、run_id 和原始文件位置。
    这样人在终端里可以快速决定下一步打开哪个文件。
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
