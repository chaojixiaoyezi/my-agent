# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。

from __future__ import annotations

"""CLI entrypoint for manual memory compact planning and rescue apply.

新手说明:
普通运行里的上下文压缩走 automatic runtime compact，不需要用户手动调用这里。
这个命令是调试/救援入口：先做 dry-run 计划；显式 --apply 时只生成 compact
context、自检和 ledger，不会删除 raw archive、snapshot、token ledger 或 task/run 文件。
"""

import json

from ..agent.memory_archive.compact import MemoryCompactPlanOptions, build_memory_compact_plan
from ..agent.memory_archive.compact_apply import MemoryCompactApplyOptions, apply_memory_compact
from ..agent.user_space.context_bundle import latest_main_context_bundle_path
from .common import make_agent


# LLM: cmd_memory_compact 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_memory_compact(args) -> int:
    if getattr(args, "apply", False):
        return _cmd_memory_compact_apply(args)
    agent = make_agent(args)
    plan = build_memory_compact_plan(agent.root, _options_from_args(args, agent=agent))
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    _print_memory_compact_plan(plan)
    return 0


# LLM: _cmd_memory_compact_apply 属于memory CLI；必须保持非破坏性 apply 和明确退出码。
# 函数用途: 执行 compact apply 命令，写 compact context/self-check/ledger 并按 JSON 或文本输出结果。
def _cmd_memory_compact_apply(args) -> int:
    agent = make_agent(args)
    # 函数用途: CLI 默认把最近一次主代理任务卡传给 apply，用户不用手动找 context bundle 路径。
    result = apply_memory_compact(
        agent.root,
        MemoryCompactApplyOptions(
            plan_options=_options_from_args(args, agent=agent),
            main_context_bundle_ref=_main_context_bundle_ref_for_args(args, agent),
            main_context_bundle_ref_explicit=bool(getattr(args, "main_context_bundle_ref", "") or ""),
        ),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["ok"] else 2
    _print_memory_compact_apply(result)
    return 0 if result["ok"] else 2


# LLM: _options_from_args 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _options_from_args(args, *, agent=None) -> MemoryCompactPlanOptions:
    resolved_agent = agent or make_agent(args)
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
        limit=_compact_limit(resolved_agent, args),
    )


# LLM: _compact_limit keeps compact complete by default; --limit is only an explicit debug override.
# 函数用途: 用户没有传 --limit 时不截断 compact 记录；显式传入时才限制本次处理范围。
def _compact_limit(agent, args) -> int:
    value = getattr(args, "limit", None)
    if value is not None:
        return int(value)
    return 0


# LLM: _main_context_bundle_ref_for_args separates explicit refs from automatic latest lookup.
# 函数用途: 用户显式传 ref 时照用；否则才尝试 latest，后续由 apply 做 scope match。
def _main_context_bundle_ref_for_args(args, agent) -> str:
    explicit = str(getattr(args, "main_context_bundle_ref", "") or "").strip()
    if explicit:
        return explicit
    return latest_main_context_bundle_path(getattr(agent, "home_paths", None))


# LLM: _print_memory_compact_plan 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _print_memory_compact_plan(plan: dict) -> None:
    print("MY-AGENT MEMORY COMPACT DRY-RUN")
    print("mode=manual_rescue_plan")
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


# LLM: _print_memory_compact_apply 属于memory CLI；输出文案是用户确认 apply 语义的第一层说明。
# 函数用途: 展示非破坏性 compact apply 的结果、产物路径和 self-check 状态。
def _print_memory_compact_apply(result: dict) -> None:
    print("MY-AGENT MEMORY COMPACT APPLY")
    print("mode=manual_rescue_non_destructive")
    print(f"workspace={result['workspace_root']}")
    print(f"event_id={result['event_id']}")
    print(f"compact_status={result['compact_status']}")
    print("content_preserved=true")
    _print_json_line("refs", result["refs"])
    _print_json_line("self_check", _self_check_report_payload(result["post_compact_self_check"]))


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


# LLM: _self_check_report_payload 属于memory CLI；保持 compact apply 自检摘要短小稳定。
# 函数用途: 生成 CLI 展示用的 self-check 摘要，避免直接打印整份检查报告。
def _self_check_report_payload(self_check: dict) -> dict:
    return {
        "ok": self_check["ok"],
        "check_count": len(self_check["checks"]),
        "failed": [item["name"] for item in self_check["checks"] if not item["ok"]],
    }
