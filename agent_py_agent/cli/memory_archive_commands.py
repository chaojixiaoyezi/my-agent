
from __future__ import annotations

"""CLI entrypoints for memory archive list/search/resume reports.

新手说明:
这个文件只管命令入口和打印。
真正的归档读取、过滤和恢复线索整理在 `agent.memory_archive.query`，避免 CLI 文件重新变成大杂烩。
owner archive 根目录解析在 `memory_archive_roots.py`，输出渲染在 `memory_archive_rendering.py`。
"""

import json
from typing import Any

from ..agent.agent_core.runtime.owner_roots import runtime_owner_root
from ..agent.memory_archive.compact_resume import (
    MemoryCompactResumeOptions,
    build_memory_compact_resume,
)
from ..agent.memory_archive.query import (
    ResumeGuidanceRequest,
    archive_filters_from_args,
    build_resume_guidance,
    collect_gateway_payloads,
    collect_resume_task_ids,
    collect_task_payloads,
    filter_archive_records,
    local_hit_payload,
    resume_local_query,
)
from ..agent.memory_archive.resume_brief import build_resume_brief
from .common import make_agent
from .memory_archive_rendering import print_archive_list, print_archive_search, print_memory_resume
from .memory_archive_roots import (
    ArchiveCollectRequest,
    archive_roots,
    collect_agent_archive_records,
)
from .memory_resume_compact_rendering import print_memory_resume_from_compact


def cmd_memory_archive_list(args) -> int:

    agent = make_agent(args)
    _apply_archive_default_limit(agent, args)
    roots = archive_roots(agent)
    records = collect_agent_archive_records(
        agent,
        ArchiveCollectRequest(
            layer=args.layer,
            date_key=args.date,
            limit=args.limit,
            level=getattr(args, "level", None),
            file_limit=_archive_search_file_limit(agent),
        ),
    )
    payload = {
        "ok": True,
        "workspace_root": str(agent.root),
        "archive_roots": [str(path) for path in roots],
        "layer": args.layer,
        "date": args.date or "",
        "level": getattr(args, "level", None),
        "limit": args.limit,
        "records": records,
    }
    print_archive_list(payload, json_output=args.json)
    return 0


def cmd_memory_archive_search(args) -> int:

    agent = make_agent(args)
    _apply_archive_default_limit(agent, args)
    roots = archive_roots(agent)
    records = collect_agent_archive_records(
        agent,
        ArchiveCollectRequest(
            layer=args.layer,
            date_key=args.date,
            limit=0,
            file_limit=_archive_search_file_limit(agent),
        ),
    )
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
        "archive_roots": [str(path) for path in roots],
        "query": args.query or "",
        "filters": filters,
        "date": args.date or "",
        "since": args.since or "",
        "until": args.until or "",
        "limit": args.limit,
        "matches": matches,
    }
    print_archive_search(payload, json_output=args.json)
    return 0


def _collect_resume_data(agent, args):
    roots = archive_roots(agent)
    archive_records = collect_agent_archive_records(
        agent,
        ArchiveCollectRequest(
            layer=args.layer,
            date_key=args.date,
            limit=0,
            file_limit=_archive_search_file_limit(agent),
        ),
    )
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
    local_payloads = [
        local_hit_payload(hit, preview_chars=int(getattr(agent.config, "memory_query_content_preview_chars", 500) or 0))
        for hit in local_hits
    ]
    task_ids = collect_resume_task_ids(args, archive_matches, local_payloads)
    task_payloads = collect_task_payloads(agent, task_ids, limit=args.limit)
    gateway_payloads = collect_gateway_payloads(local_payloads, limit=args.limit)
    return filters, roots, archive_matches, local_payloads, task_payloads, gateway_payloads


def cmd_memory_resume(args) -> int:
    agent = make_agent(args)
    _apply_archive_default_limit(agent, args)
    if _from_compact_arg(args):
        return _cmd_memory_resume_from_compact(agent, args)
    filters, archive_roots, archive_matches, local_payloads, task_payloads, gateway_payloads = _collect_resume_data(
        agent, args
    )
    resume = build_resume_guidance(
        ResumeGuidanceRequest(
            archive_matches=archive_matches,
            local_hits=local_payloads,
            task_payloads=task_payloads,
            gateway_payloads=gateway_payloads,
            recommended_read_paths_limit=int(
                getattr(agent.config, "memory_resume_recommended_read_paths_limit", 20) or 0
            ),
        )
    )
    brief = build_resume_brief(
        archive_matches, local_payloads, task_payloads,
        recommended_read_paths=resume["recommended_read_paths"],
        next_actions=resume["next_actions"],
    )
    payload = {
        "ok": True, "workspace_root": str(agent.root), "query": args.query or "",
        "archive_roots": [str(path) for path in archive_roots],
        "filters": filters, "archive_matches": archive_matches,
        "local_matches": local_payloads, "task_fact_sources": task_payloads,
        "gateway_fact_sources": gateway_payloads, "resume": resume, "brief": brief,
    }
    if getattr(args, "context_only", False):
        print(brief["context_block"])
        return 0
    print_memory_resume(payload, json_output=args.json)
    return 0


def _from_compact_arg(args) -> str:
    value = getattr(args, "from_compact", "")
    return value.strip() if isinstance(value, str) else ""


def _cmd_memory_resume_from_compact(agent, args) -> int:
    payload = build_memory_compact_resume(
        runtime_owner_root(agent),
        MemoryCompactResumeOptions(
            apply_ref=_from_compact_arg(args),
            owner_type=getattr(args, "compact_owner_type", "main_agent") or "main_agent",
            owner_id=getattr(args, "compact_owner_id", "") or "",
            resume_mode=getattr(args, "compact_resume_mode", "manual") or "manual",
        ),
    )
    if getattr(args, "context_only", False):
        print(payload["context_block"])
        return 0 if _compact_resume_exit_ok(payload) else 2
    print_memory_resume_from_compact(payload, json_output=args.json)
    return 0 if _compact_resume_exit_ok(payload) else 2


def _compact_resume_exit_ok(payload: dict[str, Any]) -> bool:
    if not payload.get("ok"):
        return False
    guard = payload.get("action_guard", {}) if isinstance(payload.get("action_guard"), dict) else {}
    return guard.get("mode") != "auto" or bool(guard.get("allowed_to_continue"))


def _archive_search_file_limit(agent) -> int:
    return int(getattr(agent.config, "memory_archive_search_file_limit", 30) or 0)


def _apply_archive_default_limit(agent, args) -> None:
    if getattr(args, "limit", None) is None:
        args.limit = int(getattr(agent.config, "cli_memory_archive_limit", 20) or 0)
