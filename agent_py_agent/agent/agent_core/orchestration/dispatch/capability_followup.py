
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
    _raise_grant_wake_signals(agent, route_records)
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


def _raise_grant_wake_signals(agent: Any, records: list) -> None:
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return
    for run_id in _granted_run_ids(records):
        _raise_grant_wake_signal_for_run(agent, store, run_id)


def _raise_grant_wake_signal_for_run(agent: Any, store: Any, run_id: str) -> None:
    try:
        task = agent.subagents.load(run_id)
        thread = store.thread_for_task(run_id)
        if thread is None:
            return
        observation = store.append_observation(_grant_wake_observation(thread, task, run_id))
        signal = store.raise_wake_signal(_grant_wake_signal_request(thread, task, run_id, observation))
        _record_grant_wake(agent, task, signal.wake_signal_id)
    except Exception as exc:
        _record_grant_wake_error(agent, run_id, exc)


def _grant_wake_observation(thread: Any, task: Any, run_id: str) -> dict[str, object]:
    return {
        "thread_id": thread.thread_id,
        "event_type": "subagent_capability_granted",
        "summary": f"子代理 {run_id} 的 capability_request 已授权，继续同一任务后续执行。",
        "urgency": "normal",
        "source_agent_id": run_id,
        "parent_agent_id": str(getattr(task, "parent_id", "") or ""),
        "root_task_id": str(getattr(task, "root_id", "") or run_id),
        "requires_main_agent": True,
        "metadata": {"run_id": run_id, "grant_wake": True},
    }


def _grant_wake_signal_request(thread: Any, task: Any, run_id: str, observation: Any) -> dict[str, object]:
    return {
        "thread_id": thread.thread_id,
        "observation": observation,
        "urgency": "normal",
        "reason": "subagent_capability_granted",
        "source_agent_id": run_id,
        "parent_agent_id": str(getattr(task, "parent_id", "") or ""),
        "root_task_id": str(getattr(task, "root_id", "") or run_id),
        "dedupe_key": f"capability-granted:{run_id}",
        "metadata": {"run_id": run_id, "grant_wake": True},
    }


def _record_grant_wake(agent: Any, task: Any, wake_signal_id: str) -> None:
    attrs = dict(getattr(task, "attributes", {}) or {})
    attrs["capability_grant_wake"] = {
        "schema_version": "capability_grant_wake.v1",
        "wake_signal_id": wake_signal_id,
        "status": "raised",
    }
    task.attributes = attrs
    agent.subagents.save(task)


def _record_grant_wake_error(agent: Any, run_id: str, exc: BaseException) -> None:
    try:
        task = agent.subagents.load(run_id)
    except Exception:
        return
    attrs = dict(getattr(task, "attributes", {}) or {})
    attrs["capability_grant_wake_error"] = runtime_error_report(exc, context="dispatch_capability_followup.grant_wake")
    task.attributes = attrs
    try:
        agent.subagents.save(task)
    except Exception:
        return


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
