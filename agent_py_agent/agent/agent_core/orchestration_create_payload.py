# LLM: Create-subagents payload helpers keep the tool facade below code-size risk.
# 模块用途: 构造 create_subagents 的结构化返回包、幂等合同和下一步建议。

from __future__ import annotations

from dataclasses import dataclass

from ..action_protocol import subagent_schedule_envelope_from_payload
from ..contracts.idempotency import idempotency_key, operation_id
from ..model_visible_refs import current_model_ref, current_model_ref_list
from .orchestration_child_result_index import child_result_index
from .orchestration_create_idempotency import created_tasks, dispatchable_tasks, reused_tasks
from .orchestration_dispatch_state_contract import dispatch_state_contract_payload


# LLM: CreateSubagentsPayloadInput groups create payload facts to keep the public helper small.
# 类用途: 保存 create_subagents payload 构造需要的 agent、resolution、工具策略和自动启动结果。
@dataclass(frozen=True)
class CreateSubagentsPayloadInput:
    agent: object
    resolutions: list
    allowed_tools: object
    request_params: dict[str, object]
    auto_start: dict[str, object] | None = None
    replacement_records: list[dict[str, object]] | None = None


# LLM: create_subagents_payload renders create/reuse/dispatch facts for the parent model.
# 函数用途: create_subagents 返回机器可读状态，避免父级下一轮靠自然语言记忆猜哪些 run 可调度。
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
    payload: dict[str, object] = {
        "created": len(created),
        "ids": [task.id for task in tasks],
        "created_run_ids": [task.id for task in created],
        "reused_run_ids": [task.id for task in reused],
        "dispatch_run_ids": [task.id for task in pending_dispatch],
        "auto_start": _auto_start_payload(auto_start),
        "next_action": _dispatch_next_action(dispatchable, request_params, auto_start),
        "allowed_tools": request.allowed_tools or "automatic",
        "operation_contract": _operation_contract(request_params, created, reused, pending_dispatch),
        "replacement_records": request.replacement_records or [],
        "scheduling_advice": _scheduling_advice(tasks, request_params, auto_start),
        "child_result_index": child_result_index(agent, tasks),
        "subagent_workspace": current_model_ref(getattr(agent.subagents, "workspace", "")),
        "tasks": [_task_payload(task) for task in tasks],
    }
    payload.update(dispatch_state_contract_payload(agent))
    payload["typed_envelope"] = subagent_schedule_envelope_from_payload(payload, tool="create_subagents").to_dict()
    return payload


# LLM: _auto_start_payload keeps create_subagents output compact and current-ref only.
# 函数用途: 自动启动结果只保留状态和 run id；树状态请用 inspect_agent_tree 查询，避免嵌套旧路径流入模型。
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


# LLM: _pending_dispatch_tasks separates explicit deferred starts from already auto-started runs.
# 函数用途: 自动开跑成功时不再把同一批 run 放进 dispatch_run_ids，避免父级重复催跑。
def _pending_dispatch_tasks(tasks: list, request_params: dict[str, object], auto_start: dict[str, object] | None) -> list:
    if bool(request_params.get("defer_start")):
        return tasks
    deferred = set(_string_items((auto_start or {}).get("deferred_run_ids")))
    if deferred:
        return [task for task in tasks if _task_text(task, "id") in deferred]
    if (auto_start or {}).get("status") in {"started", "not_needed"}:
        return []
    return tasks


# LLM: _operation_contract gives create_subagents a stable idempotency envelope without blocking repeats.
# 函数用途: 把本次 create 的请求键、操作编号和结果 run ids 写成机器字段，后续调度可复用。
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


# LLM: _dispatch_next_action keeps create/start/defer facts explicit for the parent model.
# 函数用途: 默认创建即启动；只有 defer_start=true 才返回后续调度建议。
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
            "reason": "create_subagents 已自动启动这些 run；下一步查看状态、读取产物或按需继续推进。",
            "params": {},
        }
    return {
        "tool": "dispatch_subagents",
        "reason": "create_subagents 自动启动未完成；如需继续推进、恢复或重跑，请调度这些 run_id。",
            "params": {"dry_run": False, "run_ids": run_ids, "max_runners": len(run_ids)},
    }


# LLM: _task_payload keeps create_subagents payloads JSON-safe across real tasks and mock adapters.
# 函数用途: 只返回小型状态字段和 attributes，避免序列化正文或 mock 对象。
def _task_payload(task: object) -> dict[str, object]:
    return {
        "id": _task_text(task, "id"),
        "goal": _task_text(task, "goal"),
        "status": _task_text(task, "status"),
        "verification_status": _task_text(task, "verification_status"),
        "task_root": current_model_ref(_task_text(task, "task_workspace_dir")),
        "agent_work_dir": current_model_ref(_task_text(task, "agent_run_workspace_dir")),
        "attributes": _task_attributes(task),
    }


# LLM: _task_text reads small string fields defensively.
# 函数用途: 过滤 MagicMock/非字符串对象，避免旧测试替身或 adapter 让 JSON 序列化失败。
def _task_text(task: object, field: str) -> str:
    value = getattr(task, field, "")
    return value if isinstance(value, str) else ""


# LLM: _task_attributes exposes small structured task facts without serializing mocks or bodies.
# 函数用途: create_subagents 返回 output/input/QA 等机器字段，避免父级下一轮再从 goal 文字里猜。
def _task_attributes(task: object) -> dict[str, object]:
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return {}
    return {
        str(key): projected
        for key, value in attrs.items()
        if (projected := _task_attribute_value(str(key), value)) not in ("", [], None)
    }


# LLM: _task_attribute_value keeps structured task refs current while leaving non-ref metadata intact.
# 函数用途: 对 input/output/artifact 等 ref 字段做当前路径投影，其他属性原样传递。
def _task_attribute_value(key: str, value: object) -> object:
    ref_keys = {"input_refs", "output_refs", "output_files", "artifact_refs", "required_read_paths"}
    if key in ref_keys:
        return current_model_ref_list(value, basename_for_legacy=True)
    if isinstance(value, str):
        return current_model_ref(value)
    return value


# LLM: _scheduling_advice is a soft create_subagents hint, not a dispatch blocker.
# 函数用途: 发现测试/验收/汇总类子任务过早启动时返回中文建议。
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


# LLM: _is_dependent_quality_role uses role labels only to phrase scheduling advice.
# 函数用途: 判断子任务是否像测试、找错、验收或汇总类依赖后置任务。
def _is_dependent_quality_role(task: object) -> bool:
    role = f"{_task_text(task, 'role')} {_task_text(task, 'agent_name')}".casefold().replace("-", "_")
    return any(token in role for token in ("tester", "bug_finder", "reviewer", "verifier", "qa", "summary", "汇总", "测试", "找错", "验收"))


# LLM: _string_items normalizes optional list-like fields from model payloads.
# 函数用途: 将列表、元组或集合里的非空项转成字符串列表。
def _string_items(value: object) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
