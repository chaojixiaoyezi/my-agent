# LLM: Memory archive CLI rendering lives outside command handlers to keep CLI wiring thin.
# 模块用途: 渲染 memory archive list/search/resume 的 JSON 或人工文本输出。

from __future__ import annotations

import json
from typing import Any

from ..agent.memory_archive.query import strip_sort_keys


# LLM: print_archive_list keeps archive list output stable for CLI snapshots.
# 函数用途: 输出 memory-archive-list 的 JSON 或人工文本。
def print_archive_list(payload: dict[str, Any], *, json_output: bool) -> None:
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
    print_archive_record_lines(payload["records"])


# LLM: print_archive_search keeps archive search output stable for CLI snapshots.
# 函数用途: 输出 memory-archive-search 的 JSON 或人工文本。
def print_archive_search(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(strip_sort_keys(payload), ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("MY-AGENT MEMORY ARCHIVE SEARCH")
    print(f"workspace={payload['workspace_root']}")
    print(f"query={payload['query'] or '-'} matches={len(payload['matches'])}")
    if payload["filters"]:
        print("filters=" + json.dumps(payload["filters"], ensure_ascii=False, sort_keys=True))
    print_archive_record_lines(payload["matches"])


# LLM: print_memory_resume renders the resume payload without changing recovery data.
# 函数用途: 输出 memory-resume 的 JSON 或人工文本。
def print_memory_resume(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(strip_sort_keys(payload), ensure_ascii=False, indent=2, sort_keys=True))
        return
    _print_memory_resume_text(payload)


# LLM: _print_memory_resume_text prints layered resume clues without mutating memory.
# 函数用途: 输出 memory-resume 的人工可读正文。
def _print_memory_resume_text(payload: dict[str, Any]) -> None:
    print("MY-AGENT MEMORY RESUME")
    print(f"workspace={payload['workspace_root']}")
    print(f"query={payload['query'] or '-'}")
    resume = payload["resume"]
    print(
        "summary="
        + json.dumps(
            {
                "archive": resume["archive_match_count"],
                "local": resume["local_match_count"],
                "tasks": resume["task_fact_source_count"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    _print_resume_brief(payload["brief"])
    print("Archive Clues")
    print_archive_record_lines(payload["archive_matches"][:5])
    _print_task_fact_sources(payload["task_fact_sources"])
    _print_gateway_fact_sources(payload["gateway_fact_sources"])
    print("Recommended Reads")
    for path in resume["recommended_read_paths"] or ["none"]:
        print(f"- {path}")
    print("Next Actions")
    for action in resume["next_actions"]:
        print(f"- {action}")


# LLM: _print_resume_brief highlights the smallest useful recovery context.
# 函数用途: 输出恢复简报、相关 ID 和任务状态线索。
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


# LLM: _print_task_fact_sources renders task facts as refs instead of expanding artifacts.
# 函数用途: 输出任务事实源路径和状态。
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


# LLM: _print_gateway_fact_sources shows gateway handoff paths for cross-day recovery.
# 函数用途: 输出 gateway 请求/响应事实源。
def _print_gateway_fact_sources(gateway_payloads: list[dict[str, Any]]) -> None:
    print("Gateway Fact Sources")
    if not gateway_payloads:
        print("- none")
    for gw in gateway_payloads:
        print(f"- {gw['request_id']} status={gw['status'] or '-'} ok={gw['ok']}")
        for path in gw["recommended_read_paths"]:
            print(f"  - {path}")


# LLM: print_archive_record_lines keeps archive record text output compact and stable.
# 函数用途: 输出 archive 记录列表；空列表显示 none。
def print_archive_record_lines(records: list[dict[str, Any]]) -> None:
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


__all__ = ["print_archive_list", "print_archive_record_lines", "print_archive_search", "print_memory_resume"]
