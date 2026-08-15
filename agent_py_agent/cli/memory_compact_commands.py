
from __future__ import annotations

"""CLI entrypoint for manual memory compact planning and rescue apply.

新手说明:
普通运行里的上下文压缩走 automatic runtime compact，不需要用户手动调用这里。
这个命令是调试/救援入口：先做 dry-run 计划；显式 --apply 时只生成 compact
context、自检和 ledger，不会删除 raw archive、snapshot、token ledger 或 task/run 文件。
"""

import json

from ..agent.agent_core.runtime.owner_roots import runtime_owner_root
from ..agent.memory_archive.compact import MemoryCompactPlanOptions, build_memory_compact_plan
from ..agent.memory_archive.compact_apply import MemoryCompactApplyOptions, apply_memory_compact
from ..agent.user_space.context_bundle import latest_main_context_bundle_path
from .common import make_agent


def cmd_memory_compact(args) -> int:
    if getattr(args, "apply", False):
        return _cmd_memory_compact_apply(args)
    agent = make_agent(args)
    plan = build_memory_compact_plan(_compact_root(agent), _options_from_args(args, agent=agent))
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    _print_memory_compact_plan(plan)
    return 0


def _cmd_memory_compact_apply(args) -> int:
    agent = make_agent(args)
    result = apply_memory_compact(
        _compact_root(agent),
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


def _compact_limit(agent, args) -> int:
    value = getattr(args, "limit", None)
    if value is not None:
        return int(value)
    return 0


def _main_context_bundle_ref_for_args(args, agent) -> str:
    explicit = str(getattr(args, "main_context_bundle_ref", "") or "").strip()
    if explicit:
        return explicit
    return latest_main_context_bundle_path(getattr(agent, "home_paths", None))


def _compact_root(agent) -> object:
    """Manual compact follows the same owner-home fact source as runtime compact."""

    return runtime_owner_root(agent)


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


def _print_memory_compact_apply(result: dict) -> None:
    print("MY-AGENT MEMORY COMPACT APPLY")
    print("mode=manual_rescue_non_destructive")
    print(f"workspace={result['workspace_root']}")
    print(f"event_id={result['event_id']}")
    print(f"compact_status={result['compact_status']}")
    print("content_preserved=true")
    _print_json_line("refs", result["refs"])
    _print_json_line("self_check", _self_check_report_payload(result["post_compact_self_check"]))


def _print_json_line(label: str, payload: dict) -> None:
    """返回说明: 用一行稳定 JSON 输出，方便复制到日志或测试断言。"""

    print(label + "=" + json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _archive_report_payload(archive: dict) -> dict:
    return {
        "records": archive["record_count"],
        "files": archive["file_count"],
        "bytes": archive["total_bytes"],
        "by_layer": archive["by_layer"],
        "by_archive_level": archive["by_archive_level"],
        "errors": archive["error_count"],
    }


def _snapshot_report_payload(snapshots: dict) -> dict:
    return {
        "files": snapshots["file_count"],
        "invalid": snapshots["invalid_count"],
        "bytes": snapshots["total_bytes"],
    }


def _token_report_payload(tokens: dict) -> dict:
    return {
        "ledgers": tokens["ledger_count"],
        "turns": tokens["turn_count"],
        "cumulative_tokens": tokens["cumulative_tokens"],
        "invalid": tokens["invalid_count"],
        "bytes": tokens["total_bytes"],
    }


def _self_check_report_payload(self_check: dict) -> dict:
    return {
        "ok": self_check["ok"],
        "check_count": len(self_check["checks"]),
        "failed": [item["name"] for item in self_check["checks"] if not item["ok"]],
    }
