# LLM: Background subagent dispatch keeps create_subagents non-blocking.
# 模块用途: 创建子代理后后台启动 runner，并把状态写回任务树供父代理查询。

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from ..capabilities import CapabilityRouter
from .agent_tree_status import agent_tree_status_payload
from .dispatch_params import DispatchParams
from .orchestration_create_idempotency import dispatchable_tasks
from .orchestration_dispatch_tool_helpers import _dispatch_capability_config
from .parameters import _bool_param
from .runner_context import current_subagent_run_id


# LLM: _BackgroundDispatchRequest bundles async dispatch inputs so helpers stay small.
# 类用途: 保存后台启动线程所需的 agent、run_ids、router、配置和 dispatch 参数。
@dataclass(frozen=True)
class _BackgroundDispatchRequest:
    agent: object
    run_ids: list[str]
    launch_id: str
    router: object
    cfg: object
    params: DispatchParams


# LLM: auto_start_tasks starts new subagents unless the caller explicitly defers.
# 函数用途: create_subagents 创建任务后立即后台启动可运行 run，并返回状态。
def auto_start_tasks(agent, tasks: list, request_params: dict[str, object]) -> dict[str, object]:
    skipped_run_ids = [_safe_task_id(task) for task in tasks if _safe_task_id(task)]
    dispatchable = dispatchable_tasks(tasks)
    run_ids = [_safe_task_id(task) for task in dispatchable if _safe_task_id(task)]
    if not run_ids:
        return {"status": "not_needed", "run_ids": [], "skipped_run_ids": skipped_run_ids}
    if _bool_param(request_params.get("defer_start"), default=False):
        return {"status": "deferred", "run_ids": run_ids, "reason": "defer_start=true"}
    if not callable(getattr(agent, "dispatch_subagents", None)):
        return {"status": "unavailable", "run_ids": run_ids, "reason": "agent has no dispatch_subagents"}
    try:
        return _start_background_dispatch(agent, run_ids)
    except Exception as exc:
        return {"status": "failed", "run_ids": run_ids, "error": f"{type(exc).__name__}: {exc}"}


# LLM: _start_background_dispatch launches runner work without blocking the parent turn.
# 函数用途: 写入 launching 状态、启动后台线程，并返回 run_id/tree/status。
def _start_background_dispatch(agent, run_ids: list[str]) -> dict[str, object]:
    launch_id = f"subagent-start-{time.time_ns()}"
    router, cfg, dispatch_params = _auto_start_dispatch_args(agent, run_ids)
    request = _BackgroundDispatchRequest(agent, run_ids, launch_id, router, cfg, dispatch_params)
    _mark_background_start(request, status="launching")
    thread = threading.Thread(
        target=_background_dispatch_worker,
        name=f"my-agent-{launch_id}",
        args=(request,),
        daemon=True,
    )
    _remember_background_dispatch(agent, launch_id, run_ids, thread.name)
    thread.start()
    return {
        "status": "started",
        "dispatch_mode": "background",
        "run_ids": run_ids,
        "launch_id": launch_id,
        "thread_name": thread.name,
        "summary": "subagent dispatch launched in background; parent should inspect agent tree for progress",
        "agent_tree": _safe_agent_tree(agent),
    }


# LLM: _background_dispatch_worker is the only async runner side effect.
# 函数用途: 在后台调用 dispatch_subagents，并把成功/失败写回启动状态。
def _background_dispatch_worker(request: _BackgroundDispatchRequest) -> None:
    try:
        _mark_background_start(request, status="running")
        report = request.agent.dispatch_subagents(request.router, request.cfg, params=request.params)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        _mark_background_start(request, status="failed", error=error)
        _remember_background_result(request.agent, request.launch_id, {"ok": False, "error": error})
        return
    _mark_background_start(request, status="finished")
    _remember_background_result(request.agent, request.launch_id, _auto_start_report(request.run_ids, report))


# LLM: _mark_background_start records launch progress on each task node.
# 函数用途: 把 launching/running/finished/failed 写入 task.attributes.background_start。
def _mark_background_start(request: _BackgroundDispatchRequest, *, status: str, error: str = "") -> None:
    now = time.time()
    manager = getattr(request.agent, "subagents", None)
    for run_id in request.run_ids:
        try:
            task = manager.load(run_id)
        except Exception:
            continue
        if getattr(task, "id", "") != run_id:
            continue
        attrs = dict(getattr(task, "attributes", {}) or {})
        attrs["background_start"] = {
            "launch_id": request.launch_id,
            "status": status,
            "updated_at": now,
            "error": error,
        }
        task.attributes = attrs
        try:
            manager.save(task)
        except Exception:
            continue


# LLM: _remember_background_dispatch keeps an in-memory index for the foreground parent.
# 函数用途: 记录本轮启动的后台线程，便于父代理或测试读取。
def _remember_background_dispatch(agent, launch_id: str, run_ids: list[str], thread_name: str) -> None:
    registry = getattr(agent, "_background_subagent_dispatches", None)
    if not isinstance(registry, dict):
        registry = {}
        agent._background_subagent_dispatches = registry
    registry[launch_id] = {
        "run_ids": list(run_ids),
        "thread_name": thread_name,
        "status": "running",
        "started_at": time.time(),
    }


# LLM: _remember_background_result stores the final lightweight dispatch outcome.
# 函数用途: 更新后台启动登记项的 finished/failed 状态和摘要结果。
def _remember_background_result(agent, launch_id: str, result: dict[str, object]) -> None:
    registry = getattr(agent, "_background_subagent_dispatches", None)
    if not isinstance(registry, dict):
        return
    item = dict(registry.get(launch_id) or {})
    status = "finished" if result.get("ok", True) else "failed"
    item.update({"status": status, "result": result, "finished_at": time.time()})
    registry[launch_id] = item


# LLM: _safe_agent_tree gives create_subagents a best-effort status snapshot.
# 函数用途: 读取任务树失败时返回结构化 warning，而不是阻断创建。
def _safe_agent_tree(agent) -> dict[str, object]:
    try:
        return agent_tree_status_payload(agent, {"scope": "root_tree"})
    except Exception as exc:
        return {"schema_version": "agent_tree_status.v1", "warnings": [f"agent_tree_unavailable:{type(exc).__name__}"]}


# LLM: _auto_start_dispatch_args builds the existing dispatch_subagents call.
# 函数用途: 复用原调度器参数，只把调用放到后台执行。
def _auto_start_dispatch_args(agent, run_ids: list[str]) -> tuple[object, object, DispatchParams]:
    cfg = _dispatch_capability_config(agent)
    tool_specs = [spec for spec in agent.tools.specs() if getattr(spec, "category", "") != "orchestration"]
    router = CapabilityRouter(config=cfg, tool_specs=tool_specs)
    params = DispatchParams(
        apply=True,
        execute_runners=True,
        workflow_mode="off",
        max_runners=len(run_ids),
        limit=max(20, len(run_ids)),
        reviewer="create-subagents-auto-start",
        note="auto-start after create_subagents",
        parent_run_id=current_subagent_run_id(agent),
        include_run_ids=run_ids,
    )
    return router, cfg, params


# LLM: _auto_start_report normalizes dispatch reports from real agents and test doubles.
# 函数用途: 把后台 dispatch 结果压成小型 JSON 字段。
def _auto_start_report(run_ids: list[str], report: object) -> dict[str, object]:
    summary = getattr(report, "summary", "")
    records = getattr(report, "records", [])
    if not isinstance(summary, str) or not isinstance(records, list):
        return {"status": "started", "run_ids": run_ids, "summary": "dispatch started"}
    return {
        "status": "started",
        "run_ids": run_ids,
        "summary": summary,
        "record_count": len(records),
        "dry_run": bool(getattr(report, "dry_run", False)),
    }


# LLM: _safe_task_id extracts persisted ids without leaking MagicMock objects.
# 函数用途: 从任务对象读取字符串 id，非法值返回空字符串。
def _safe_task_id(task: object) -> str:
    value = getattr(task, "id", "")
    return value.strip() if isinstance(value, str) else ""


__all__ = ["auto_start_tasks"]
