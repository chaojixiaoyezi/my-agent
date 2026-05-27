# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""exposes model-callable orchestration tools backed by SimpleAgent subagent workflows.

这些不是普通文件工具，而是'主代理让模型触发子代理流程'的工具。
创建子代理、查看看板、执行 dispatch 都在这里，真实业务再转给 SimpleAgent 和 SubAgentManager。
"""

import json
import threading
import time
from typing import TYPE_CHECKING

from ..capabilities import CapabilityRouter
from ..subagents.services.base import CreateRunParams
from ..tools import BaseTool, ToolExecutionResult
from .agent_tree_status import agent_tree_status_payload
from .dispatch_params import DispatchParams
from .hierarchy_tools import ScheduleChildSubagentsTool as ScheduleChildSubagentsTool
from .orchestration_create_constraints import (
    delegation_constraint_conflict_error,
    explicit_root_missing_write_root_error,
)
from .orchestration_create_idempotency import (
    CreateTaskResolution,
    dispatchable_tasks,
    resolve_create_run,
)
from .orchestration_create_items import (
    CreateSubagentItem,
    create_items_from_params,
)
from .orchestration_create_payload import CreateSubagentsPayloadInput, create_subagents_payload
from .orchestration_create_policy import (
    create_run_params,
)
from .orchestration_dispatch_tool import DispatchSubagentsTool
from .orchestration_dispatch_tool_helpers import _dispatch_capability_config
from .orchestration_event_tools import RaiseMainEventTool as RaiseMainEventTool
from .orchestration_event_tools import RaiseObservationTool as RaiseObservationTool
from .orchestration_lineage_names import indexed_count_params, indexed_item_params
from .orchestration_run_scope import (
    remember_orchestration_run_ids,
)
from .orchestration_shared_context import append_parent_shared_context
from .orchestration_sibling_roster import attach_sibling_roster
from .orchestration_status_tools import InspectAgentTreeTool as InspectAgentTreeTool
from .orchestration_status_tools import SubagentBoardTool as SubagentBoardTool
from .orchestration_tool_grants import (
    CODING_SUBAGENT_TOOLS,
    READ_ONLY_SUBAGENT_TOOLS,
    subagent_allowed_tools,
)
from .orchestration_tool_specs import build_create_subagents_spec
from .orchestration_workflow_mode import tool_workflow_mode as _tool_workflow_mode
from .orchestration_write_guard import ExternalWriteTargetRequest, external_write_target_error
from .parameters import _bool_param, _positive_int

if TYPE_CHECKING:
    from ..core import SimpleAgent


# LLM: CreateSubagentsTool 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 提供create子代理工具模型工具入口，把结构化参数转为子代理操作；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
class CreateSubagentsTool(BaseTool):

    # LLM: __init__ 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_create_subagents_spec()

    # LLM: execute 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 推进execute的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        if not self.agent.config.enable_subagents:
            return ToolExecutionResult("create_subagents", False, "配置已禁用 subagent。")

        items_result = self._items_result(params)
        if items_result is not None:
            return items_result

        prepared = self._prepare_count_mode(params)
        if isinstance(prepared, ToolExecutionResult):
            return prepared
        goal, count, allowed_tools, run_params = prepared
        task_params = self._count_run_params(count, run_params)
        resolutions = self._resolve_task_params(task_params)
        tasks = [item.task for item in resolutions]
        attach_sibling_roster(self.agent.subagents, tasks)
        _bind_created_tasks_to_conversation(self.agent, tasks)
        remember_orchestration_run_ids(self.agent, [task.id for task in tasks])
        auto_start = _auto_start_tasks(self.agent, tasks, params)
        payload = create_subagents_payload(
            CreateSubagentsPayloadInput(
                agent=self.agent,
                resolutions=resolutions,
                allowed_tools=allowed_tools,
                request_params=params,
                auto_start=auto_start,
            )
        )
        return ToolExecutionResult(
            "create_subagents",
            True,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

    # LLM: _items_result routes structured batch mode before count-mode validation.
    # 函数用途: 解析 items/tasks 入口；返回 None 表示继续单 goal/count 模式。
    def _items_result(self, params: dict[str, object]) -> ToolExecutionResult | None:
        items = create_items_from_params(params)
        if isinstance(items, str):
            return ToolExecutionResult("create_subagents", False, items)
        if items:
            return self._execute_items(items, params)
        return None

    # LLM: _prepare_count_mode validates single-goal delegation and builds run params.
    # 函数用途: 聚合 count 模式的 goal、count、工具和 run 参数，保持 execute 入口薄。
    def _prepare_count_mode(
        self, params: dict[str, object]
    ) -> tuple[str, int, list[str] | None, CreateRunParams] | ToolExecutionResult:
        params = append_parent_shared_context(self.agent, params)
        goal = str(params.get("goal") or "").strip()
        if not goal:
            return ToolExecutionResult("create_subagents", False, "缺少必填参数 goal。")
        count = self._requested_count(params)
        if isinstance(count, ToolExecutionResult):
            return count
        allowed_tools = subagent_allowed_tools(params)
        validation = self._validate_single_goal(params, goal, allowed_tools)
        if validation:
            return ToolExecutionResult("create_subagents", False, validation)
        run_params = create_run_params(self.agent, params, goal, allowed_tools)
        return goal, count, allowed_tools, run_params

    # LLM: _execute_items is the structured batch path, equivalent to 长期助手 delegate_task tasks[].
    # 函数用途: 按 items[] 中每个独立 goal 创建子代理，避免 count 复制同一个任务目标。
    def _execute_items(
        self,
        items: list[CreateSubagentItem],
        request_params: dict[str, object],
    ) -> ToolExecutionResult:
        capped = self._items_with_parent_context(self._cap_items(items))
        allowed_tool_values = [subagent_allowed_tools(item.params) for item in capped]
        validation = self._validate_items(capped, allowed_tool_values)
        if validation:
            return ToolExecutionResult("create_subagents", False, validation)
        run_params_by_item = self._indexed_item_run_params(capped)
        resolutions = self._resolve_task_params(run_params_by_item)
        tasks = [item.task for item in resolutions]
        attach_sibling_roster(self.agent.subagents, tasks, save=False)
        for task in tasks:
            self.agent.subagents.save(task)
        _bind_created_tasks_to_conversation(self.agent, tasks)
        remember_orchestration_run_ids(self.agent, [task.id for task in tasks])
        payload_request = self._items_payload_request(request_params, capped)
        auto_start = _auto_start_tasks(self.agent, tasks, payload_request)
        payload = create_subagents_payload(
            CreateSubagentsPayloadInput(
                agent=self.agent,
                resolutions=resolutions,
                allowed_tools=_payload_allowed_tools(allowed_tool_values),
                request_params=payload_request,
                auto_start=auto_start,
            )
        )
        payload["batch_mode"] = "items"
        return ToolExecutionResult(
            "create_subagents",
            True,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

    # LLM: _items_with_parent_context applies inherited context to each batch item.
    # 函数用途: 将父级共享上下文注入每个 item，保持 _execute_items 主流程短。
    def _items_with_parent_context(self, items: list[CreateSubagentItem]) -> list[CreateSubagentItem]:
        return [
            CreateSubagentItem(
                goal=item.goal,
                params=append_parent_shared_context(self.agent, item.params),
            )
            for item in items
        ]

    # LLM: _validate_items keeps batch validation separated from creation.
    # 函数用途: 校验 items 模式下每个子任务的写入边界和委派约束，返回首个错误。
    def _validate_items(self, items: list[CreateSubagentItem], allowed_tool_values: list[list[str] | None]) -> str:
        for item, allowed_tools in zip(items, allowed_tool_values, strict=True):
            validation = self._validate_single_goal(item.params, item.goal, allowed_tools)
            if validation:
                return validation
        return ""

    # LLM: _indexed_item_run_params builds persisted run params for each item.
    # 函数用途: 将 item goal/params 转成带批次序号的 CreateRunParams。
    def _indexed_item_run_params(self, items: list[CreateSubagentItem]) -> list[CreateRunParams]:
        run_params_by_item: list[CreateRunParams] = []
        for index, item in enumerate(items, start=1):
            run_params = create_run_params(
                self.agent,
                item.params,
                item.goal,
                subagent_allowed_tools(item.params),
            )
            run_params_by_item.append(indexed_item_params(run_params, index=index, total=len(items)))
        return run_params_by_item

    # LLM: _items_payload_request records the exact item params used for payload/audit.
    # 函数用途: 给 create_subagents payload 保存 items 参数快照，不把构造逻辑塞进执行主流程。
    def _items_payload_request(self, request_params: dict[str, object], items: list[CreateSubagentItem]) -> dict[str, object]:
        payload_request = dict(request_params)
        payload_request["items"] = [item.params for item in items]
        return payload_request

    # LLM: _cap_items applies the same user-configured fan-out ceiling as count mode.
    # 函数用途: 避免 items[] 绕过 max_subagents；配置为 0 或更小时表示不限制。
    def _cap_items(self, items: list[CreateSubagentItem]) -> list[CreateSubagentItem]:
        max_subagents = int(getattr(self.agent.config, "max_subagents", 0) or 0)
        if max_subagents > 0:
            return items[:max_subagents]
        return items

    # LLM: _validate_single_goal centralizes per-child create checks for count and items modes.
    # 函数用途: 对单个子代理目标做写入边界和约束检查，返回空字符串表示可创建。
    def _validate_single_goal(
        self,
        params: dict[str, object],
        goal: str,
        allowed_tools: list[str] | None,
    ) -> str:
        missing_write_root = explicit_root_missing_write_root_error(self.agent, params, goal)
        if missing_write_root:
            return missing_write_root
        target_error = external_write_target_error(
            ExternalWriteTargetRequest(
                agent=self.agent,
                allowed_tools=allowed_tools or CODING_SUBAGENT_TOOLS,
                params=params,
            )
        )
        if target_error:
            return target_error
        return delegation_constraint_conflict_error(params)

    # LLM: _requested_count 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 发送requested数量请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
    def _requested_count(self, params: dict[str, object]) -> int | ToolExecutionResult:
        count = _positive_int(params.get("count"), default=1)
        if count <= 0:
            return ToolExecutionResult("create_subagents", False, "count 必须大于 0。")
        if self.agent.config.max_subagents > 0:
            count = min(count, self.agent.config.max_subagents)
        return count

    # LLM: _create_tasks 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 构建tasks所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def _count_run_params(self, count: int, run_params: CreateRunParams) -> list[CreateRunParams]:
        return [
            indexed_count_params(run_params, index=index, count=count)
            for index in range(1, count + 1)
        ]

    # LLM: _resolve_task_params is the only place count/items params become persisted runs.
    # 函数用途: 将 create_subagents 参数创建或复用成真实 run；不在创建阶段插入额外等待门。
    def _resolve_task_params(self, task_params: list[CreateRunParams]) -> list[CreateTaskResolution]:
        resolutions: list[CreateTaskResolution] = []
        for item in task_params:
            resolutions.append(resolve_create_run(self.agent.subagents, item))
        return resolutions


# LLM: _payload_allowed_tools summarizes items-mode tool policy without hiding per-task params.
# 函数用途: 多个 item 工具策略一致时保留原输出；不一致时告诉模型每个任务独立决定。
def _payload_allowed_tools(values: list[list[str] | None]) -> list[str] | str | None:
    if not values:
        return None
    first = values[0]
    if all(value == first for value in values):
        return first
    return "per_item"


# LLM: _auto_start_tasks makes create_subagents mean "create and start" unless defer_start is explicit.
# 函数用途: 创建子代理后立即启动可运行 run；defer_start=true 时才只建记录，避免父代理忘记再调度。
def _auto_start_tasks(agent, tasks: list, request_params: dict[str, object]) -> dict[str, object]:
    skipped_run_ids = [_safe_task_id(task) for task in tasks if _safe_task_id(task)]
    dispatchable = dispatchable_tasks(tasks)
    run_ids = [_safe_task_id(task) for task in dispatchable if _safe_task_id(task)]
    if not run_ids:
        return {"status": "not_needed", "run_ids": [], "skipped_run_ids": skipped_run_ids}
    if _bool_param(request_params.get("defer_start"), default=False):
        return {"status": "deferred", "run_ids": run_ids, "reason": "defer_start=true"}
    dispatcher = getattr(agent, "dispatch_subagents", None)
    if not callable(dispatcher):
        return {"status": "unavailable", "run_ids": run_ids, "reason": "agent has no dispatch_subagents"}
    try:
        return _start_background_dispatch(agent, run_ids)
    except Exception as exc:
        return {"status": "failed", "run_ids": run_ids, "error": f"{type(exc).__name__}: {exc}"}


# LLM: _start_background_dispatch launches children without making the parent wait for completion.
# 函数用途: create_subagents 默认创建并后台启动 run，立即返回 run_id/tree/status；真实执行结果后续从 inspect_agent_tree 读取。
def _start_background_dispatch(agent, run_ids: list[str]) -> dict[str, object]:
    launch_id = f"subagent-start-{time.time_ns()}"
    _mark_background_start(agent, run_ids, launch_id, status="launching")
    router, cfg, dispatch_params = _auto_start_dispatch_args(agent, run_ids)
    thread = threading.Thread(
        target=_background_dispatch_worker,
        name=f"my-agent-{launch_id}",
        args=(agent, run_ids, launch_id, router, cfg, dispatch_params),
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


# LLM: _background_dispatch_worker contains the only async side effect for create-time autostart.
# 函数用途: 后台调用原 dispatch_subagents；失败时只记录状态，不能把父代理工具调用卡死。
def _background_dispatch_worker(agent, run_ids: list[str], launch_id: str, router, cfg, dispatch_params: DispatchParams) -> None:
    try:
        _mark_background_start(agent, run_ids, launch_id, status="running")
        report = agent.dispatch_subagents(router, cfg, params=dispatch_params)
    except Exception as exc:
        _mark_background_start(agent, run_ids, launch_id, status="failed", error=f"{type(exc).__name__}: {exc}")
        _remember_background_result(agent, launch_id, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return
    _mark_background_start(agent, run_ids, launch_id, status="finished")
    _remember_background_result(agent, launch_id, _auto_start_report(run_ids, report))


def _mark_background_start(agent, run_ids: list[str], launch_id: str, *, status: str, error: str = "") -> None:
    now = time.time()
    manager = getattr(agent, "subagents", None)
    for run_id in run_ids:
        try:
            task = manager.load(run_id)
        except Exception:
            continue
        if getattr(task, "id", "") != run_id:
            continue
        attrs = dict(getattr(task, "attributes", {}) or {})
        attrs["background_start"] = {
            "launch_id": launch_id,
            "status": status,
            "updated_at": now,
            "error": error,
        }
        task.attributes = attrs
        try:
            manager.save(task)
        except Exception:
            continue


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


def _remember_background_result(agent, launch_id: str, result: dict[str, object]) -> None:
    registry = getattr(agent, "_background_subagent_dispatches", None)
    if not isinstance(registry, dict):
        return
    item = dict(registry.get(launch_id) or {})
    item.update({"status": "finished" if result.get("ok", True) else "failed", "result": result, "finished_at": time.time()})
    registry[launch_id] = item


def _safe_agent_tree(agent) -> dict[str, object]:
    try:
        return agent_tree_status_payload(agent, {"scope": "root_tree"})
    except Exception as exc:
        return {"schema_version": "agent_tree_status.v1", "warnings": [f"agent_tree_unavailable:{type(exc).__name__}"]}


# LLM: _auto_start_dispatch_args builds the dispatch call for create-time auto-start.
# 函数用途: 生成 router、capability config 和 DispatchParams，避免 _auto_start_tasks 膨胀。
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
        include_run_ids=run_ids,
    )
    return router, cfg, params


# LLM: _auto_start_report normalizes dispatch reports from real agents and MagicMock tests.
# 函数用途: 将自动启动的 dispatch 结果压成小型 JSON 字段，供 create_subagents payload 返回。
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


# LLM: _safe_task_id extracts persisted run ids without leaking MagicMock or adapter objects.
# 函数用途: 从任务对象读取字符串 id，非法或空 id 返回空字符串供调用方过滤。
def _safe_task_id(task: object) -> str:
    value = getattr(task, "id", "")
    return value.strip() if isinstance(value, str) else ""


# LLM: _bind_created_tasks_to_conversation makes local subagents addressable by task_id in event tools.
# 函数用途: 如果 create_run_params 已继承 conversation_thread_id，则把每个 run_id 也绑定到同一 thread；
# 这样子代理上报事件只需传自己的 run_id，不必知道外部会话 ID。
def _bind_created_tasks_to_conversation(agent, tasks: list) -> None:
    for task in tasks:
        attrs = getattr(task, "attributes", {}) or {}
        if not isinstance(attrs, dict):
            continue
        thread_id = str(attrs.get("conversation_thread_id") or "").strip()
        if not thread_id:
            continue
        try:
            agent.conversation_store.bind_task({'thread_id': thread_id, 'task_id': str(getattr(task, "id", "") or ""), 'goal': str(getattr(task, "goal", "") or "")})
        except Exception:
            continue
