# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。

from __future__ import annotations

"""CLI entrypoint for memory compact planning.

新手说明:
这个命令第一版只做 dry-run。真正的扫描逻辑在 memory_archive.compact，
CLI 只负责把参数传进去并打印报告。
"""

import json

from ..agent.memory_archive.compact import MemoryCompactPlanOptions, build_memory_compact_plan
from .common import make_agent


# LLM: cmd_memory_compact 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_memory_compact(args) -> int:
    if getattr(args, "apply", False):
        print("memory-compact --apply 尚未实现；请先使用 --dry-run 查看计划。")
        return 2
    agent = make_agent(args)
    plan = build_memory_compact_plan(agent.root, _options_from_args(args))
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    _print_memory_compact_plan(plan)
    return 0


# LLM: _options_from_args 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _options_from_args(args) -> MemoryCompactPlanOptions:
    return MemoryCompactPlanOptions(
        layer=args.layer,
        date=args.date or "",
        since=args.since or "",
        until=args.until or "",
        session_id=args.session_id or "",
        request_id=args.request_id or "",
        run_id=args.run_id or "",
        task_id=args.task_id or "",
        level=args.level,
        limit=args.limit,
    )


# LLM: _print_memory_compact_plan 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _print_memory_compact_plan(plan: dict) -> None:
    print("MY-AGENT MEMORY COMPACT DRY-RUN")
    print(f"workspace={plan['workspace_root']}")
    print("scope=" + json.dumps(plan["scope"], ensure_ascii=False, sort_keys=True))
    _print_json_line("archive", _archive_report_payload(plan["archive"]))
    _print_json_line("snapshots", _snapshot_report_payload(plan["snapshots"]))
    _print_json_line("tokens", _token_report_payload(plan["tokens"]))
    print(f"estimated_compactable_bytes={plan['estimated_compactable_bytes']}")
    print("Risks")
    for item in plan["risks"] or ["none"]:
        print(f"- {item}")
    print("Recommended Actions")
    for item in plan["recommended_actions"]:
        print(f"- {item}")


# LLM: _print_json_line 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _print_json_line(label: str, payload: dict) -> None:
    """返回说明: 用一行稳定 JSON 输出，方便复制到日志或测试断言。"""

    print(label + "=" + json.dumps(payload, ensure_ascii=False, sort_keys=True))


# LLM: _archive_report_payload 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _archive_report_payload(archive: dict) -> dict:
    return {
        "records": archive["record_count"],
        "files": archive["file_count"],
        "bytes": archive["total_bytes"],
        "by_layer": archive["by_layer"],
        "by_archive_level": archive["by_archive_level"],
        "errors": archive["error_count"],
    }


# LLM: _snapshot_report_payload 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _snapshot_report_payload(snapshots: dict) -> dict:
    return {
        "files": snapshots["file_count"],
        "invalid": snapshots["invalid_count"],
        "bytes": snapshots["total_bytes"],
    }


# LLM: _token_report_payload 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _token_report_payload(tokens: dict) -> dict:
    return {
        "ledgers": tokens["ledger_count"],
        "turns": tokens["turn_count"],
        "cumulative_tokens": tokens["cumulative_tokens"],
        "invalid": tokens["invalid_count"],
        "bytes": tokens["total_bytes"],
    }
