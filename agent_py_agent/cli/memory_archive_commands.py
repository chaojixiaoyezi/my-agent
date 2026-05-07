# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。

from __future__ import annotations

"""CLI entrypoints for memory archive list/search/resume reports.

新手说明:
这个文件只管命令入口和打印。
真正的归档读取、过滤和恢复线索整理在 `agent.memory_archive.query`，避免 CLI 文件重新变成大杂烩。
"""

import json
from typing import Any

from ..agent.memory_archive.compact_resume import (
    MemoryCompactResumeOptions,
    build_memory_compact_resume,
)
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


# LLM: cmd_memory_archive_list 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_memory_archive_list(args) -> int:

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


# LLM: cmd_memory_archive_search 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_memory_archive_search(args) -> int:

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


# LLM: _collect_resume_data 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 汇总多个检查来源，并按统一结构返回调用方。
def _collect_resume_data(agent, args):
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


# LLM: cmd_memory_resume 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_memory_resume(args) -> int:
    agent = make_agent(args)
    if _from_compact_arg(args):
        return _cmd_memory_resume_from_compact(agent, args)
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


# LLM: _from_compact_arg ignores MagicMock/default argparse sentinels and accepts only real user input.
# 函数用途: 判断 CLI 是否真正传入 --from-compact，避免旧测试或兼容调用误入 compact resume 分支。
def _from_compact_arg(args) -> str:
    value = getattr(args, "from_compact", "")
    return value.strip() if isinstance(value, str) else ""


# LLM: _cmd_memory_resume_from_compact keeps compact resume read-only and separate from archive search resume.
# 函数用途: 处理 memory-resume --from-compact，读取 apply 产物并输出恢复上下文或 JSON。
def _cmd_memory_resume_from_compact(agent, args) -> int:
    payload = build_memory_compact_resume(
        agent.root,
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
    _print_memory_resume_from_compact(payload, json_output=args.json)
    return 0 if _compact_resume_exit_ok(payload) else 2


# LLM: _compact_resume_exit_ok treats auto guard blocking as a non-zero CLI result.
# 函数用途: 判断 compact resume 命令退出码；manual 成功可返回 0，auto guard 阻断必须返回 2。
def _compact_resume_exit_ok(payload: dict[str, Any]) -> bool:
    if not payload.get("ok"):
        return False
    guard = payload.get("action_guard", {}) if isinstance(payload.get("action_guard"), dict) else {}
    return guard.get("mode") != "auto" or bool(guard.get("allowed_to_continue"))


# LLM: _print_archive_list 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _print_archive_list(payload: dict[str, Any], *, json_output: bool) -> None:

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


# LLM: _print_archive_search 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _print_archive_search(payload: dict[str, Any], *, json_output: bool) -> None:

    if json_output:
        print(json.dumps(strip_sort_keys(payload), ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("MY-AGENT MEMORY ARCHIVE SEARCH")
    print(f"workspace={payload['workspace_root']}")
    print(f"query={payload['query'] or '-'} matches={len(payload['matches'])}")
    if payload["filters"]:
        print("filters=" + json.dumps(payload["filters"], ensure_ascii=False, sort_keys=True))
    _print_archive_record_lines(payload["matches"])


# LLM: _print_resume_brief 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _print_resume_brief(brief: dict[str, Any]) -> None:
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


# LLM: _print_task_fact_sources 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _print_task_fact_sources(task_payloads: list[dict[str, Any]]) -> None:
    print("Task Fact Sources")
    if not task_payloads:
        print("- none")
    for task in task_payloads:
        if not task.get("exists"):
            print(f"- {task['run_id']} missing :: {task.get('error', '')}")
            continue
        print(f"- {task['run_id']} {task['status']}/{task['verification_status']} :: {task['goal']}")
        print(f"  task_dir={task['task_dir']}")


# LLM: _print_gateway_fact_sources 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _print_gateway_fact_sources(gateway_payloads: list[dict[str, Any]]) -> None:
    print("Gateway Fact Sources")
    if not gateway_payloads:
        print("- none")
    for gw in gateway_payloads:
        print(f"- {gw['request_id']} status={gw['status'] or '-'} ok={gw['ok']}")
        for path in gw["recommended_read_paths"]:
            print(f"  - {path}")


# LLM: _print_memory_resume_text 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _print_memory_resume_text(payload: dict[str, Any]) -> None:
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


# LLM: _print_memory_resume 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _print_memory_resume(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(strip_sort_keys(payload), ensure_ascii=False, indent=2, sort_keys=True))
        return
    _print_memory_resume_text(payload)


# LLM: _print_memory_resume_from_compact renders compact-specific resume output without hiding consistency status.
# 函数用途: 输出 compact resume 的 apply id、状态、推荐读取路径和下一步动作。
def _print_memory_resume_from_compact(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(strip_sort_keys(payload), ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("MY-AGENT MEMORY RESUME FROM COMPACT")
    print(f"workspace={payload['workspace_root']}")
    print(f"apply_id={payload['apply_id']}")
    print(f"plan_id={payload['plan_id'] or '-'}")
    print(f"consistency_status={payload['consistency_report']['status']}")
    print(f"action_guard={payload['action_guard']['status']}")
    _print_compact_handoff(payload.get("handoff", {}))
    print("Recommended Reads")
    for path in payload["recommended_read_paths"] or ["none"]:
        print(f"- {path}")
    print("Next Actions")
    for action in payload["next_actions"]:
        print(f"- {action}")


# LLM: _print_compact_handoff shows the resume package fields that matter before continuing work.
# 函数用途: 输出 compact resume 的目标、阶段、验收、约束、测试和 action guard 摘要。
def _print_compact_handoff(handoff: dict[str, Any]) -> None:
    if not handoff:
        return
    print("Handoff")
    print(f"- goal: {handoff.get('goal') or 'unknown'}")
    print(f"- current_phase: {handoff.get('current_phase') or 'unknown'}")
    print(f"- next_step: {handoff.get('next_step') or 'unknown'}")
    print(f"- missing_fields: {json.dumps(handoff.get('missing_fields', []), ensure_ascii=False)}")
    _print_named_items("Acceptance", handoff.get("acceptance", {}).get("items", []))
    _print_named_items("Constraints", handoff.get("constraints", {}).get("items", []))
    _print_named_items("Latest Tests", handoff.get("latest_tests", {}).get("items", []))


# LLM: _print_named_items keeps compact handoff subsections compact and stable for CLI users.
# 函数用途: 输出一个命名列表，空列表明确显示 none。
def _print_named_items(title: str, items: list[str]) -> None:
    print(title)
    for item in items or ["none"]:
        print(f"- {item}")


# LLM: _print_archive_record_lines 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _print_archive_record_lines(records: list[dict[str, Any]]) -> None:

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
