
from __future__ import annotations

"""exposes model-callable orchestration tools backed by SimpleAgent subagent workflows.

这些不是普通文件工具，而是'主代理让模型触发子代理流程'的工具。
创建子代理、查看看板、执行 dispatch 都在这里，真实业务再转给 SimpleAgent 和 SubAgentManager。
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..settings.defaults import default_config_int
from ..subagents.services.base import CreateRunParams
from ..subagents.services.hierarchy.scheduled_role import (
    agent_name_has_trailing_identifier,
    is_placeholder_agent_name,
)
from ..tooling.models import BaseTool, ToolExecutionResult
from .hierarchy_tools import ScheduleChildSubagentsTool as ScheduleChildSubagentsTool
from .orchestration.create_constraints import (
    CreateTaskResolution,
    explicit_root_missing_write_root_error,
    resolve_create_run,
)
from .orchestration.create_payload import (
    CreateSubagentItem,
    CreateSubagentsPayloadInput,
    create_items_from_params,
    create_subagents_payload,
)
from .orchestration.create_policy import (
    create_run_params,
)
from .orchestration.dispatch.tool import DispatchSubagentsTool
from .orchestration.dispatch_progress_seed import (
    DISPATCH_SEED_NOTE,
    autobind_covers_from_goal_ids,
    dispatch_coverage_binding,
    seed_dispatch_task_progress,
)
from .orchestration.lifecycle import (
    CreatedSubagentLifecycleRequest,
    publish_created_subagents,
)
from .orchestration.replacements import record_create_replacements
from .orchestration.shared_context import append_parent_shared_context
from .orchestration.sibling_roster import attach_sibling_roster
from .orchestration.tool_grants import (
    CODING_SUBAGENT_TOOLS,
    READ_ONLY_SUBAGENT_TOOLS,
    subagent_allowed_tools,
)
from .orchestration.tool_specs import build_create_subagents_spec
from .orchestration.tools.cancel import CancelSubagentsTool as CancelSubagentsTool
from .orchestration.tools.capability import (
    ResolveCapabilityRequestsTool as ResolveCapabilityRequestsTool,
)
from .orchestration.tools.event import RaiseEventTool as RaiseEventTool
from .orchestration.tools.status import InspectAgentTreeTool as InspectAgentTreeTool
from .orchestration.work_scope import add_work_scope_key
from .orchestration.write_guard import (
    ExternalWriteTargetRequest,
    external_write_target_error,
)
from .parameters import subagent_intent_identity
from .runtime.guidance_tool import SendGuidanceTool as SendGuidanceTool
from .runtime.wait_tool import register_dispatch_supervision_policy
from .task_progress_tool import TaskProgressTool as TaskProgressTool

if TYPE_CHECKING:
    from ..core import SimpleAgent

_DEFAULT_MAX_SUBAGENTS = default_config_int("max_subagents")
_DEFAULT_DEPTH = 1


@dataclass(frozen=True)
class CreatedItemsResultRequest:
    agent: SimpleAgent
    resolutions: list[CreateTaskResolution]
    allowed_tool_values: list[list[str] | None]
    request_params: dict[str, object]
    capped_items: list[CreateSubagentItem]


@dataclass(frozen=True)
class ValidateSingleGoalRequest:
    agent: SimpleAgent
    params: dict[str, object]
    goal: str
    allowed_tools: list[str] | None


def _indexed_item_params(run_params: CreateRunParams, *, index: int, total: int) -> CreateRunParams:
    task_name = _indexed_agent_name(
        run_params.agent_name,
        role=run_params.role,
        index=index,
        require_index=total > 1,
    )
    return CreateRunParams(**{**run_params.__dict__, "agent_name": task_name})


def _indexed_agent_name(agent_name: str, *, role: str, index: int, require_index: bool = False) -> str:
    name = str(agent_name or "").strip()
    if _needs_system_lineage_name(name):
        return f"agent-d{_DEFAULT_DEPTH}-{_role_suffix(role)}-{index}"
    if require_index and not agent_name_has_trailing_identifier(name):
        return f"{name}-{index}"
    return name


def _needs_system_lineage_name(agent_name: str) -> bool:
    return is_placeholder_agent_name(agent_name)


def _role_suffix(role: str) -> str:
    suffix = str(role or "worker").strip().replace("_", "-").strip("-") or "worker"
    if suffix in {"general", "child", "subagent", "agent"}:
        return "worker"
    return suffix


class CreateSubagentsTool(BaseTool):

    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_create_subagents_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        try:
            return _execute_create_subagents(self.agent, params)
        except Exception as exc:
            # create_subagents 在真实 dispatch 路径上会偶发崩溃(真机 B1/R3:合法 goal+output_files
            # 调用也抛异常,堆栈没落到任何日志,模型只看到无信息、retryable=False 的 UNKNOWN_ERROR 兜底码
            # 就放弃、退回主代理独自写)。这里兜住异常:① 记完整 traceback 便于定位;② 把异常类型+摘要
            # 写进报错消息(工具账本里就能看到崩在哪);③ 用 retryable 的 TOOL_INVALID_ARGUMENTS 让模型
            # 换简单写法重试,而不是吞成 UNKNOWN_ERROR 直接弃疗。
            import logging
            import traceback as _tb
            logging.getLogger(__name__).error("create_subagents crashed: %s\n%s", exc, _tb.format_exc())
            return ToolExecutionResult(
                "create_subagents",
                False,
                f"create_subagents 执行时内部出错({type(exc).__name__}: {exc})。多半是某个参数触发的内部问题——"
                "换最简单的写法重试:只传一个 goal、先别带 output_files/acceptance_checks 等附加字段。别因此就改回自己写。",
                error_code="TOOL_INVALID_ARGUMENTS",
            )

def _execute_create_subagents(agent: SimpleAgent, params: dict[str, object]) -> ToolExecutionResult:
    if not agent.config.enable_subagents:
        return ToolExecutionResult("create_subagents", False, "配置已禁用 subagent。", error_code="TOOL_UNAVAILABLE")
    if not str(params.get("goal") or "").strip():
        # goal 是整批派工的结构化意图，items 模式也不能省。这样 schema 入口与直接
        # execute 入口使用同一硬约束，不靠模型正文猜本批任务是什么。
        return ToolExecutionResult(
            "create_subagents",
            False,
            "create_subagents 缺少始终必填的 goal。这里的 goal 是内部整批派工说明，"
            "与用户是否使用 /goal 无关；普通聊天任务也可以派子代理。要派工必须说清整批要完成什么。两种正确写法:"
            '① 派一个: {"goal":"这个子代理要完成的具体任务"};'
            '② 派多个不同任务: {"goal":"整批派工目的","items":[{"goal":"任务A"},{"goal":"任务B"}]};'
            "请补上顶层 goal 后重试，使用 items 时每项也要有独立 goal；别因为这个就改回自己写。",
            error_code="TOOL_INVALID_ARGUMENTS",
        )
    if "count" in params:
        return ToolExecutionResult(
            "create_subagents",
            False,
            "create_subagents 不接受 count 克隆同一任务。只派一个时直接传 goal；"
            "需要并行时使用 items 明确列出互不重复的具体子任务和各自交付边界。",
            error_code="TOOL_INVALID_ARGUMENTS",
        )
    items_result = _items_result(agent, params)
    if items_result is not None:
        return items_result
    prepared = _prepare_single_mode(agent, params)
    if isinstance(prepared, ToolExecutionResult):
        return prepared
    allowed_tools, run_params = prepared
    return _created_tasks_result(agent, _resolve_task_params(agent, [run_params]), allowed_tools, params)


def _items_result(agent: SimpleAgent, params: dict[str, object]) -> ToolExecutionResult | None:
    items = create_items_from_params(params)
    if isinstance(items, str):
        return ToolExecutionResult("create_subagents", False, items, error_code="TOOL_INVALID_ARGUMENTS")
    if items:
        return _execute_items(agent, items, params)
    return None


def _prepare_single_mode(
    agent: SimpleAgent,
    params: dict[str, object],
) -> tuple[list[str] | None, CreateRunParams] | ToolExecutionResult:
    params = append_parent_shared_context(agent, params)
    goal = str(params.get("goal") or "").strip()
    capacity = _checked_creation_capacity(agent)
    if isinstance(capacity, ToolExecutionResult):
        return capacity
    slots, limits = capacity
    if slots < 1:
        return _subagent_quota_result(1, slots, limits)
    allowed_tools = subagent_allowed_tools(params)
    validation = _validate_single_goal(ValidateSingleGoalRequest(agent, params, goal, allowed_tools))
    if validation:
        return ToolExecutionResult("create_subagents", False, validation, error_code="TOOL_INVALID_ARGUMENTS")
    return allowed_tools, create_run_params(agent, params, goal, allowed_tools)


def _created_tasks_result(
    agent: SimpleAgent,
    resolutions: list[CreateTaskResolution],
    allowed_tools: list[str] | str | None,
    request_params: dict[str, object],
) -> ToolExecutionResult:
    tasks = [item.task for item in resolutions]
    attach_sibling_roster(agent.subagents, tasks)
    replacement_records = record_create_replacements(agent, tasks)
    lifecycle = publish_created_subagents(CreatedSubagentLifecycleRequest(agent, tasks, request_params))
    payload = create_subagents_payload(
        CreateSubagentsPayloadInput(
            agent=agent,
            resolutions=resolutions,
            allowed_tools=allowed_tools,
            request_params=request_params,
            auto_start=lifecycle.auto_start,
            replacement_records=replacement_records,
            conversation_bind_errors=lifecycle.conversation_bind_errors,
        )
    )
    # 派工即种账本(学 终端应用 TodoWrite):模型的派工计划自动落成 task_progress 待办,
    # 收口闸/出口续航才有账可守;note 同时在工具结果里当面提醒(终端应用 式 tool-result nudge)。
    if seed := seed_dispatch_task_progress(agent, tasks):
        payload["task_progress_seed"] = {**seed, "note": DISPATCH_SEED_NOTE}
    # P1 covers 绑定回执:回显绑定/警示绑错 id/没绑时提醒清单还有 open 项可绑。
    if binding := dispatch_coverage_binding(agent, tasks):
        payload["coverage_binding"] = binding
    # 派工即挂监督提醒(机制层,不依赖模型自觉调 wait):窗口期有人定时巡场/上报中途进展。
    if supervision := register_dispatch_supervision_policy(agent, run_ids=[task.id for task in tasks]):
        payload["dispatch_supervision"] = supervision
    return _create_subagents_success(payload)


def _execute_items(
    agent: SimpleAgent,
    items: list[CreateSubagentItem],
    request_params: dict[str, object],
) -> ToolExecutionResult:
    capacity = _checked_creation_capacity(agent)
    if isinstance(capacity, ToolExecutionResult):
        return capacity
    slots, limits = capacity
    if len(items) > slots:
        return _subagent_quota_result(len(items), slots, limits)
    capped = _items_with_parent_context(agent, items)
    # P-bigbuild 参数落难兜底:goal 里字面写了清单项 id 却没带 covers 的 item,创建前自动补绑
    # (纯 id token 对账;显式 covers 一字不动),covers 经属性白名单随任务落 canonical。
    autobind_covers_from_goal_ids(agent, capped)
    allowed_tool_values = [subagent_allowed_tools(item.params) for item in capped]
    validation = _validate_items(agent, capped, allowed_tool_values)
    if validation:
        return ToolExecutionResult("create_subagents", False, validation, error_code="TOOL_INVALID_ARGUMENTS")
    resolutions = _resolve_task_params(agent, _indexed_item_run_params(agent, capped))
    return _created_items_result(CreatedItemsResultRequest(
        agent=agent,
        resolutions=resolutions,
        allowed_tool_values=allowed_tool_values,
        request_params=request_params,
        capped_items=capped,
    ))


def _created_items_result(request: CreatedItemsResultRequest) -> ToolExecutionResult:
    tasks = [item.task for item in request.resolutions]
    attach_sibling_roster(request.agent.subagents, tasks, save=False)
    for task in tasks:
        request.agent.subagents.save(task)
    replacement_records = record_create_replacements(request.agent, tasks)
    payload_request = _items_payload_request(request.request_params, request.capped_items)
    lifecycle = publish_created_subagents(CreatedSubagentLifecycleRequest(request.agent, tasks, payload_request))
    payload = create_subagents_payload(
        CreateSubagentsPayloadInput(
            agent=request.agent,
            resolutions=request.resolutions,
            allowed_tools=_payload_allowed_tools(request.allowed_tool_values),
            request_params=payload_request,
            auto_start=lifecycle.auto_start,
            replacement_records=replacement_records,
            conversation_bind_errors=lifecycle.conversation_bind_errors,
        )
    )
    payload["batch_mode"] = "items"
    # 单任务与 items 批量共用同一套进度账本和监督出口。
    if seed := seed_dispatch_task_progress(request.agent, tasks):
        payload["task_progress_seed"] = {**seed, "note": DISPATCH_SEED_NOTE}
    # P1 covers 绑定回执。
    if binding := dispatch_coverage_binding(request.agent, tasks):
        payload["coverage_binding"] = binding
    # 派工即挂机制层监督提醒。
    if supervision := register_dispatch_supervision_policy(
        request.agent,
        run_ids=[task.id for task in tasks],
    ):
        payload["dispatch_supervision"] = supervision
    return _create_subagents_success(payload)


def _create_subagents_success(payload: dict[str, object]) -> ToolExecutionResult:
    """Preserve a compact scheduling receipt after full output archival."""
    lifecycle = payload.get("schedule_lifecycle")
    envelope = {"schedule_lifecycle": dict(lifecycle)} if isinstance(lifecycle, dict) else {}
    return ToolExecutionResult(
        "create_subagents",
        True,
        json.dumps(payload, ensure_ascii=False, indent=2),
        result_envelope=envelope,
    )


def _items_with_parent_context(agent: SimpleAgent, items: list[CreateSubagentItem]) -> list[CreateSubagentItem]:
    return [
        CreateSubagentItem(goal=item.goal, params=append_parent_shared_context(agent, item.params))
        for item in items
    ]


def _validate_items(
    agent: SimpleAgent,
    items: list[CreateSubagentItem],
    allowed_tool_values: list[list[str] | None],
) -> str:
    seen: set[str] = set()
    for item, allowed_tools in zip(items, allowed_tool_values, strict=True):
        identity = subagent_intent_identity({}, item.params)
        if identity in seen:
            return (
                "create_subagents items 含重复的结构化子任务；每项必须是不同的具体工作。"
                "若确实需要多个代理，请把职责、交付边界或 replacement_for_run_ids 明确拆开。"
            )
        seen.add(identity)
        validation = _validate_single_goal(ValidateSingleGoalRequest(agent, item.params, item.goal, allowed_tools))
        if validation:
            return validation
    return ""


def _indexed_item_run_params(agent: SimpleAgent, items: list[CreateSubagentItem]) -> list[CreateRunParams]:
    run_params_by_item: list[CreateRunParams] = []
    for index, item in enumerate(items, start=1):
        run_params = create_run_params(agent, item.params, item.goal, subagent_allowed_tools(item.params))
        indexed = _indexed_item_params(run_params, index=index, total=len(items))
        run_params_by_item.append(_with_default_child_output_ref(agent, indexed, index=index))
    return run_params_by_item


def _items_payload_request(request_params: dict[str, object], items: list[CreateSubagentItem]) -> dict[str, object]:
    payload_request = dict(request_params)
    payload_request["items"] = [item.params for item in items]
    return payload_request


def _validate_single_goal(request: ValidateSingleGoalRequest) -> str:
    missing_write_root = explicit_root_missing_write_root_error(request.agent, request.params, request.goal)
    if missing_write_root:
        return missing_write_root
    target_error = external_write_target_error(
        ExternalWriteTargetRequest(
            agent=request.agent,
            allowed_tools=request.allowed_tools or CODING_SUBAGENT_TOOLS,
            params=request.params,
        )
    )
    return target_error or ""


def _resolve_task_params(agent: SimpleAgent, task_params: list[CreateRunParams]) -> list[CreateTaskResolution]:
    return [resolve_create_run(agent.subagents, item) for item in task_params]


def _payload_allowed_tools(values: list[list[str] | None]) -> list[str] | str | None:
    if not values:
        return None
    first = values[0]
    if all(value == first for value in values):
        return first
    return "per_item"


def _configured_max_subagents(agent) -> int:
    raw_value = getattr(getattr(agent, "config", None), "max_subagents", _DEFAULT_MAX_SUBAGENTS)
    try:
        return max(0, int(raw_value))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_SUBAGENTS


# LLM: Capacity accounting failures are distinct from user-requested over-capacity and must fail closed.
# 类用途: 标记权威子代理占用量无法读取，禁止按零占用继续创建。
class _SubagentCapacityStateError(RuntimeError):
    """Canonical live subagent usage could not be read safely."""


# LLM: Convert capacity storage failures into one structured fail-closed tool result for single and items modes.
# 函数用途: 统一读取子代理容量，账本异常时整批拒绝而不假定占用量为零。
def _checked_creation_capacity(
    agent: object,
) -> tuple[int, dict[str, int]] | ToolExecutionResult:
    try:
        return _available_creation_slots(agent)
    except _SubagentCapacityStateError:
        return ToolExecutionResult(
            "create_subagents",
            False,
            "当前无法读取权威子代理容量状态；本批没有创建任何子代理。",
            error_code="SUBAGENT_CAPACITY_UNAVAILABLE",
        )


# LLM: Capacity is the strict intersection of configured, owner-policy, per-call, task, and live usage limits.
# 函数用途: 在创建任何子代理前计算本批真正可用的严格容量。
def _available_creation_slots(agent: object) -> tuple[int, dict[str, int]]:
    """Return the strictest remaining owner/task/per-call capacity for this call."""
    owner_cap = _configured_max_subagents(agent)
    policy_cap = _positive_limit(getattr(getattr(agent, "owner_policy", None), "max_subagents", 0))
    if policy_cap:
        owner_cap = min(owner_cap, policy_cap) if owner_cap else policy_cap
    per_call_cap = _positive_limit(
        getattr(
            getattr(agent, "config", None),
            "subagent_hierarchy_max_children_per_tool_call",
            0,
        )
    )
    task_cap = _positive_limit(getattr(getattr(agent, "config", None), "task_max_subagents", 0))
    owner_active, task_active = _active_subagent_counts(agent)
    candidates = [owner_cap - owner_active] if owner_cap else []
    if per_call_cap:
        candidates.append(per_call_cap)
    if task_cap:
        candidates.append(task_cap - task_active)
    slots = max(0, min(candidates)) if candidates else _DEFAULT_MAX_SUBAGENTS
    return slots, {
        "owner_cap": owner_cap,
        "owner_active": owner_active,
        "task_cap": task_cap,
        "task_active": task_active,
        "per_call_cap": per_call_cap,
    }


# LLM: Count canonical non-terminal runs globally for the owner and exactly for the current root task.
# 函数用途: 统计当前 owner 与当前根任务已占用的子代理数。
def _active_subagent_counts(agent: object) -> tuple[int, int]:
    try:
        from ..subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in

        runs = list(agent.subagents.list_runs())
        active = [
            run
            for run in runs
            if str(getattr(run, "id", "") or "").strip()
            and not task_status_in(getattr(run, "status", ""), SUBAGENT_ENDED_STATUSES)
        ]
    except Exception as exc:
        raise _SubagentCapacityStateError("subagent registry is unavailable") from exc
    task_id = _current_root_task_id(agent)
    if not task_id:
        return len(active), 0
    try:
        related_ids = set(agent.subagent_run_ids_for_request(task_id))
    except Exception as exc:
        raise _SubagentCapacityStateError("task subagent lineage is unavailable") from exc
    return len(active), sum(
        str(getattr(run, "id", "") or "").strip() in related_ids for run in active
    )


# LLM: Prefer the conversation task attribute because request/run IDs are only bounded compatibility fallbacks.
# 函数用途: 从当前结构化运行参数解析根任务身份。
def _current_root_task_id(agent: object) -> str:
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) if current is not None else None
    if isinstance(attrs, dict):
        task_id = str(attrs.get("conversation_task_id") or "").strip()
        if task_id:
            return task_id
    return str(
        getattr(current, "task_id", "")
        or getattr(current, "request_id", "")
        or getattr(current, "run_id", "")
        or ""
    ).strip()


# LLM: Zero means this layer adds no limit; invalid values never become accidental negative capacity.
# 函数用途: 把配置容量收紧为非负整数。
def _positive_limit(value: object) -> int:
    if not isinstance(value, (int, float, str)):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


# LLM: Reject the entire batch with structured capacity facts; never silently truncate or partially create.
# 函数用途: 生成子代理数量超限时的结构化整批拒绝结果。
def _subagent_quota_result(
    requested: int,
    available: int,
    limits: dict[str, int],
) -> ToolExecutionResult:
    payload = {
        "ok": False,
        "error_code": "SUBAGENT_CAPACITY_EXCEEDED",
        "error": "本次请求的子代理数量超过当前可用容量；没有创建任何部分批次。",
        "requested": requested,
        "available": available,
        "limits": limits,
        "how_to_fix": "减少 items 数量后重试；已有子代理结束后容量会自动释放。",
    }
    return ToolExecutionResult(
        "create_subagents",
        False,
        json.dumps(payload, ensure_ascii=False, indent=2),
        error_code="SUBAGENT_CAPACITY_EXCEEDED",
    )


def _with_default_child_output_ref(agent: object, run_params: CreateRunParams, *, index: int) -> CreateRunParams:
    attrs = dict(run_params.attributes or {})
    if _has_structured_output_ref(attrs):
        return run_params
    task_root = _current_task_root(agent)
    if not task_root:
        return run_params
    default_ref = str(Path(task_root) / "work" / "child_outputs" / f"{index:02d}-{_output_slug(run_params)}.md")
    attrs["output_files"] = [default_ref]
    attrs["system_default_output_ref"] = True
    add_work_scope_key(attrs)
    return CreateRunParams(**{**run_params.__dict__, "attributes": attrs})


def _has_structured_output_ref(attrs: dict[str, object]) -> bool:
    for key in ("output_files", "output_refs", "artifact_refs"):
        value = attrs.get(key)
        if isinstance(value, list) and any(str(item or "").strip() for item in value):
            return True
    return False


def _current_task_root(agent: object) -> str:
    raw = getattr(agent, "_current_run_task_workspace", "")
    if not isinstance(raw, str | Path):
        return ""
    return str(raw).strip()


def _output_slug(run_params: CreateRunParams) -> str:
    base = str(run_params.agent_name or run_params.role or "child").strip()
    chars: list[str] = []
    last_dash = False
    for char in base:
        replacement, last_dash = _slug_char(char, last_dash)
        if replacement:
            chars.append(replacement)
    slug = "".join(chars).strip("-_").lower()
    return (slug or "child")[:80]


def _slug_char(char: str, last_dash: bool) -> tuple[str, bool]:
    if char.isalnum() or char in {"_", "-"}:
        return char, False
    return ("", True) if last_dash else ("-", True)
