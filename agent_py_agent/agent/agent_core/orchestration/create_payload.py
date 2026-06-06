from __future__ import annotations

from dataclasses import dataclass

from ...action_protocol import subagent_schedule_envelope_from_payload
from ...contracts.idempotency import idempotency_key, operation_id
from ...model_visible_refs import current_model_ref, current_model_ref_list, current_model_text
from ...subagents.role_templates import role_template_snapshot_for_task
from .child_result_index import child_result_index
from .create_idempotency import created_tasks, dispatchable_tasks, reused_tasks
from .dispatch.state_contract import dispatch_state_contract_payload


@dataclass(frozen=True)
class CreateSubagentsPayloadInput:
    agent: object
    resolutions: list
    allowed_tools: object
    request_params: dict[str, object]
    auto_start: dict[str, object] | None = None
    replacement_records: list[dict[str, object]] | None = None
    conversation_bind_errors: list[dict[str, object]] | None = None


def create_subagents_payload(request: CreateSubagentsPayloadInput) -> dict[str, object]:
    agent = request.agent
    resolutions = request.resolutions
    auto_start = request.auto_start
    request_params = request.request_params
    tasks = [item.task for item in resolutions]
    created = created_tasks(resolutions)
    reused = reused_tasks(resolutions)
    dispatchable = dispatchable_tasks(tasks)
    pending_dispatch = _pending_dispatch_tasks(dispatchable, request_params, auto_start)
    result_index = child_result_index(agent, tasks)
    payload: dict[str, object] = {
        "created": len(created),
        "ids": [task.id for task in tasks],
        "created_run_ids": [task.id for task in created],
        "reused_run_ids": [task.id for task in reused],
        "dispatch_run_ids": [task.id for task in pending_dispatch],
        "auto_start": _auto_start_payload(auto_start),
        "next_action": _dispatch_next_action(dispatchable, request_params, auto_start),
        "status_tool_call": {"tool": "inspect_agent_tree", "params": {}},
        "wait_tool_call": _wait_tool_call(agent),
        "allowed_tools": request.allowed_tools or "automatic",
        "operation_contract": _operation_contract(request_params, created, reused, pending_dispatch),
        "replacement_records": request.replacement_records or [],
        "conversation_bind_errors": request.conversation_bind_errors or [],
        "scheduling_advice": _scheduling_advice(tasks, request_params, auto_start),
        "child_result_index": result_index,
        "child_output_read_order": _child_output_read_order(result_index),
        "tasks": [_task_payload(task) for task in tasks],
    }
    payload.update(dispatch_state_contract_payload(agent))
    payload["typed_envelope"] = subagent_schedule_envelope_from_payload(payload, tool="create_subagents").to_dict()
    return payload


def _auto_start_payload(auto_start: dict[str, object] | None) -> dict[str, object]:
    if not isinstance(auto_start, dict):
        return {"status": "not_attempted"}
    allowed = {
        "status",
        "dispatch_mode",
        "run_ids",
        "deferred_run_ids",
        "started_run_ids",
        "failed_run_ids",
        "warnings",
    }
    payload = {key: auto_start[key] for key in allowed if key in auto_start}
    return payload or {"status": str(auto_start.get("status") or "unknown")}


def _pending_dispatch_tasks(tasks: list, request_params: dict[str, object], auto_start: dict[str, object] | None) -> list:
    if bool(request_params.get("defer_start")):
        return tasks
    deferred = set(_string_items((auto_start or {}).get("deferred_run_ids")))
    if deferred:
        return [task for task in tasks if _task_text(task, "id") in deferred]
    if (auto_start or {}).get("status") in {"started", "not_needed"}:
        return []
    return tasks


def _operation_contract(request_params: dict[str, object], created: list, reused: list, dispatch: list) -> dict[str, object]:
    payload = {"params": request_params}
    return {
        "contract": "idempotency.v1",
        "operation": "create_subagents",
        "idempotency_key": idempotency_key("create_subagents", payload),
        "operation_id": operation_id("create_subagents", payload),
        "created_run_ids": [task.id for task in created],
        "reused_run_ids": [task.id for task in reused],
        "dispatch_run_ids": [task.id for task in dispatch],
    }


def _dispatch_next_action(
    tasks,
    request_params: dict[str, object],
    auto_start: dict[str, object] | None = None,
) -> dict[str, object]:
    run_ids = [task.id for task in tasks]
    if not run_ids:
        return {
            "tool": "inspect_agent_tree",
            "reason": "create_subagents 没有可调度的新 run；请读取代理树状态后决定是否汇报或进入验收。",
            "params": {},
        }
    if bool(request_params.get("defer_start")):
        return {
            "tool": "dispatch_subagents",
            "reason": "defer_start=true，本次只建任务记录；需要开跑时再显式推进这些 run_id。",
            "params": {"dry_run": False, "run_ids": run_ids, "max_runners": len(run_ids)},
        }
    deferred = _string_items((auto_start or {}).get("deferred_run_ids"))
    if deferred:
        return {
            "tool": "inspect_agent_tree",
            "reason": "部分子代理已自动启动；defer_start=true 的子代理会留在 dispatch_run_ids，等前置产物出现后再显式启动。",
            "params": {},
        }
    if (auto_start or {}).get("status") == "started":
        return {
            "tool": "inspect_agent_tree",
            "reason": "create_subagents 已把本批 run 交给后台调度；主代理可以继续准备汇总材料，稍后查看代理树读取已完成结果。",
            "params": {},
        }
    return {
        "tool": "dispatch_subagents",
        "reason": "create_subagents 自动启动未完成；如需继续推进、恢复或重跑，请调度这些 run_id。",
        "params": {"dry_run": False, "run_ids": run_ids, "max_runners": len(run_ids)},
    }


def _task_payload(task: object) -> dict[str, object]:
    return {
        "id": _task_text(task, "id"),
        "goal": current_model_text(_task_text(task, "goal")),
        "status": _task_text(task, "status"),
        "verification_status": _task_text(task, "verification_status"),
        "task_root": current_model_ref(_task_text(task, "task_workspace_dir")),
        "attributes": _task_attributes(task),
    }


def _child_output_read_order(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for row in rows:
        result.append(
            {
                "run_id": str(row.get("run_id") or ""),
                "agent_name": str(row.get("agent_name") or ""),
                "role": str(row.get("role") or ""),
                "status": str(row.get("status") or ""),
                "expected_outputs": list(row.get("expected_outputs") or []),
                "read_order": list(row.get("read_order") or []),
            }
        )
    return result


def _wait_tool_call(agent: object) -> dict[str, object]:
    config = getattr(agent, "config", None)
    try:
        seconds = int(getattr(config, "subagent_watch_interval_seconds", 120))
    except (TypeError, ValueError):
        seconds = 120
    if seconds < 60:
        seconds = 60
    if seconds > 7200:
        seconds = 7200
    return {
        "tool": "wait",
        "params": {
            "seconds": seconds,
            "reason": "wait before checking subagent progress again",
        },
    }


def _task_text(task: object, field: str) -> str:
    value = getattr(task, field, "")
    return value if isinstance(value, str) else ""


def _task_attributes(task: object) -> dict[str, object]:
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return {}
    return {
        str(key): projected
        for key, value in attrs.items()
        if (projected := _task_attribute_value(str(key), value)) not in ("", [], None)
    }


def _task_attribute_value(key: str, value: object) -> object:
    ref_keys = {"input_refs", "output_refs", "output_files", "artifact_refs", "required_read_paths"}
    if key in ref_keys:
        return current_model_ref_list(value)
    if isinstance(value, str):
        return current_model_text(value)
    return value


def _scheduling_advice(tasks: list, request_params: dict[str, object], auto_start: dict[str, object] | None) -> list[dict[str, object]]:
    del request_params
    advice: list[dict[str, object]] = []
    quality_tasks = [task for task in tasks if _is_dependent_quality_role(task) and _task_text(task, "id")]
    deferred = set(_string_items((auto_start or {}).get("deferred_run_ids")))
    early = [task for task in quality_tasks if _task_text(task, "id") not in deferred]
    if early:
        advice.append(
            {
                "code": "dependent_quality_task_started_early",
                "run_ids": [_task_text(task, "id") for task in early],
                "message": "测试、找错、验收、汇总这类任务通常依赖前置产物；如果产物还没出来，建议下次创建时给这些 item 设置 defer_start=true，等产物 refs 出现后再启动。",
            }
        )
    return advice


def _is_dependent_quality_role(task: object) -> bool:
    snapshot = role_template_snapshot_for_task(task)
    return bool(snapshot.get("depends_on_outputs"))


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
