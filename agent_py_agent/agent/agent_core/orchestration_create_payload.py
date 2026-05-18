# LLM: Create-subagents payload helpers keep the tool facade below code-size risk.
# 模块用途: 构造 create_subagents 的结构化返回包、幂等合同和下一步建议。

from __future__ import annotations

from ..action_protocol import subagent_schedule_envelope_from_payload
from ..contracts.idempotency import idempotency_key, operation_id
from .orchestration_create_idempotency import created_tasks, dispatchable_tasks, reused_tasks
from .orchestration_dispatch_state_contract import dispatch_state_contract_payload


# LLM: create_subagents_payload renders create/reuse/dispatch facts for the parent model.
# 函数用途: create_subagents 返回机器可读状态，避免父级下一轮靠自然语言记忆猜哪些 run 可调度。
def create_subagents_payload(agent, resolutions: list, allowed_tools, request_params: dict[str, object]) -> dict[str, object]:
    tasks = [item.task for item in resolutions]
    created = created_tasks(resolutions)
    reused = reused_tasks(resolutions)
    dispatch = dispatchable_tasks(tasks)
    payload: dict[str, object] = {
        "created": len(created),
        "ids": [task.id for task in tasks],
        "created_run_ids": [task.id for task in created],
        "reused_run_ids": [task.id for task in reused],
        "dispatch_run_ids": [task.id for task in dispatch],
        "allowed_tools": allowed_tools or "automatic",
        "operation_contract": _operation_contract(request_params, created, reused, dispatch),
        "next_action": _dispatch_next_action(dispatch),
        "subagent_workspace": str(agent.subagents.workspace),
        "tasks": [_task_payload(task) for task in tasks],
    }
    payload.update(dispatch_state_contract_payload(agent))
    payload["typed_envelope"] = subagent_schedule_envelope_from_payload(payload, tool="create_subagents").to_dict()
    return payload


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


# LLM: _dispatch_next_action makes create-vs-run explicit for the parent model.
# 函数用途: 告诉模型 create_subagents 只创建任务记录；下一步默认先推进 1 个，流水线依赖由 dispatch 再判断。
def _dispatch_next_action(tasks) -> dict[str, object]:
    run_ids = [task.id for task in tasks]
    if not run_ids:
        return {"tool": "subagent_board", "reason": "create_subagents 没有可调度的新 run；请读取看板/状态后决定是否汇报或进入验收。", "params": {"limit": 20}}
    return {
        "tool": "dispatch_subagents",
        "reason": "create_subagents 只创建任务记录；要让子代理真正开始工作，请调度这些 run_id。默认 max_runners=1，确认任务彼此独立时再提高并发。",
        "params": {"apply": True, "execute_runners": True, "run_ids": run_ids, "max_runners": 1},
    }


# LLM: _task_payload keeps create_subagents payloads JSON-safe across real tasks and mock adapters.
# 函数用途: 只返回小型状态字段和 attributes，避免序列化正文或 mock 对象。
def _task_payload(task: object) -> dict[str, object]:
    return {
        "id": _task_text(task, "id"),
        "goal": _task_text(task, "goal"),
        "status": _task_text(task, "status"),
        "verification_status": _task_text(task, "verification_status"),
        "task_dir": _task_text(task, "task_dir"),
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
    return dict(attrs) if isinstance(attrs, dict) else {}
