
from __future__ import annotations

from typing import Any

from ....runtime_errors import runtime_error_report
from .mixin_helpers import run_dispatch_runner_stage
from .service import make_capability_route_records


def run_post_runner_capability_followup(agent: Any, state: tuple[Any, Any, list]) -> list:
    ctx, params, existing_records = state
    records = list(existing_records)
    if not _should_follow_up(ctx):
        return records
    route_records = make_capability_route_records(
        agent,
        ctx,
        mutate_state=True,
    )
    records.extend(route_records)
    if not _created_grant(route_records):
        return records
    ctx.runner_instruction = _with_capability_followup_instruction(agent, ctx, records)
    return run_dispatch_runner_stage(agent, ctx=ctx, params=params, records=records)


def _should_follow_up(ctx: Any) -> bool:
    router = getattr(ctx, "router", None)
    plan = getattr(ctx, "execution_plan", None)
    return bool(
        getattr(plan, "mutate_state", False)
        and getattr(plan, "start_runners", False)
        and hasattr(router, "search")
    )


def _created_grant(records: list) -> bool:
    return any(
        str(getattr(item, "step", "")) == "capability_route"
        and str(getattr(item, "action", "")).lower() == "granted"
        for item in records
    )


def _with_capability_followup_instruction(agent: Any, ctx: Any, records: list) -> str:
    addition = _capability_followup_instruction(agent, records)
    existing = str(getattr(ctx, "runner_instruction", "") or "").strip()
    return "\n\n".join(item for item in [existing, addition] if item)


def _capability_followup_instruction(agent: Any, records: list) -> str:
    run_ids = _granted_run_ids(records)
    snippets = _granted_task_snippets(agent, run_ids)
    lines = [
        "父级已处理上一轮 capability_request；本轮是同一个 run 的授权后续跑。",
        "不要从头重做任务；先看当前 task 的 blockers、next_actions、artifact_refs 和已有文件，再继续完成缺失部分。",
        "如果获批了 write_file/apply_patch，优先补齐或修复目标产物；完成后写标准 SUBAGENT_RESULT 或 execution_context.output_json。",
    ]
    if snippets:
        lines.extend(["", "授权后续跑上下文：", *snippets])
    return "\n".join(lines)


def _granted_run_ids(records: list) -> list[str]:
    run_ids: list[str] = []
    for item in records:
        if str(getattr(item, "step", "")) != "capability_route":
            continue
        if str(getattr(item, "action", "")).lower() != "granted":
            continue
        run_id = str(getattr(item, "run_id", "") or "").strip()
        if run_id and run_id not in run_ids:
            run_ids.append(run_id)
    return run_ids


def _granted_task_snippets(agent: Any, run_ids: list[str]) -> list[str]:
    snippets: list[str] = []
    for run_id in run_ids[:5]:
        try:
            task = agent.subagents.load(run_id)
        except Exception as exc:
            snippets.append(_task_load_error_snippet(run_id, exc))
            continue
        tools = sorted({tool for grant in getattr(task, "capability_grants", []) for tool in getattr(grant, "tools", [])})
        blockers = [str(item) for item in (getattr(task, "blockers", []) or []) if str(item).strip()]
        next_actions = _task_next_actions(task)
        paths = [str(item) for item in (getattr(task, "artifact_refs", []) or []) if str(item).strip()]
        snippets.append(
            f"- run_id={run_id}; granted_tools={','.join(tools[:8]) or 'none'}; "
            f"blockers={'; '.join(blockers[:2]) or 'none'}; "
            f"next_actions={'; '.join(next_actions[:2]) or 'none'}; "
            f"artifact_refs={'; '.join(paths[:3]) or 'none'}"
        )
    return snippets


def _task_next_actions(task: Any) -> list[str]:
    import json
    from pathlib import Path

    path = str(getattr(task, "next_actions_json", "") or "").strip()
    if not path:
        return []
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        report = runtime_error_report(exc, context="dispatch_capability_followup.next_actions.load")
        return [
            "next_actions_load_error="
            f"{report.get('category', 'unknown')}; "
            f"context={report.get('context', '')}; "
            f"message={report.get('message', '')}"
        ]
    values = payload.get("next_actions", []) if isinstance(payload, dict) else []
    return [str(item) for item in values if str(item or "").strip()]


def _task_load_error_snippet(run_id: str, exc: BaseException) -> str:
    report = runtime_error_report(exc, context="dispatch_capability_followup.subagents.load")
    return (
        f"- run_id={run_id}; load_error={report.get('category', 'unknown')}; "
        f"context={report.get('context', '')}; message={report.get('message', '')}"
    )
