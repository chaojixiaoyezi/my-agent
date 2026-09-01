
from __future__ import annotations

"""exposes model-callable orchestration tools backed by SimpleAgent subagent workflows.

这些不是普通文件工具，而是'主代理让模型触发子代理流程'的工具。
创建子代理、发送消息和直属子代理控制都在这里，真实启动由系统后台调度器负责。
"""

import json
import re
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..capability.skill_snapshot import SkillSnapshotError
from ..common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from ..settings.defaults import default_config_int
from ..subagents.capability_scope import bind_creation_tool_authority
from ..subagents.services.base import CreateRunParams
from ..subagents.services.hierarchy.scheduled_role import (
    agent_name_has_trailing_identifier,
    is_placeholder_agent_name,
)
from ..tooling.cancellation import ToolCancelled, raise_if_cancelled
from ..tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolInputPolicy,
    ToolInvocationContext,
    ToolRuntimePolicy,
)
from .hierarchy_tools import execute_child_creation
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
    prepare_audit_child_creation_scope,
)
from .orchestration.dispatch_progress_seed import (
    DISPATCH_SEED_NOTE,
    dispatch_coverage_binding,
    seed_dispatch_task_progress,
)
from .orchestration.finding_relation import (
    fence_inactive_audit_investigations,
    prepare_audit_finding_relation,
)
from .orchestration.lifecycle import (
    CreatedSubagentLifecycleRequest,
    publish_created_subagents,
)
from .orchestration.planned_delegation import (
    PLANNED_DELEGATION_ERROR_CODE,
    planned_delegation_failure,
)
from .orchestration.replacements import (
    cancel_unstarted_replacement_tasks,
    record_create_replacements,
    replacement_records_allow_start,
    validate_create_replacements,
)
from .orchestration.shared_context import append_parent_shared_context
from .orchestration.tool_grants import (
    CODING_SUBAGENT_TOOLS,
    READ_ONLY_SUBAGENT_TOOLS,
    subagent_allowed_tools,
)
from .orchestration.tool_specs import build_create_subagents_model_spec
from .orchestration.tools.cancel import (
    CancelSubagentsTool as CancelSubagentsTool,
)
from .orchestration.tools.cancel import (
    execute_cancel_subagents as execute_cancel_subagents,
)
from .orchestration.tools.capability import (
    ResolveCapabilityRequestsTool as ResolveCapabilityRequestsTool,
)
from .orchestration.tools.list_agents import ListAgentsTool as ListAgentsTool
from .orchestration.write_guard import (
    ExternalWriteTargetRequest,
    external_write_target_error,
)
from .parameters import subagent_intent_identity
from .runner.context import current_subagent_run_id
from .runtime.guidance_tool import SendGuidanceTool as SendGuidanceTool
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


# LLM: The plan/declared-write-scope preflight must finish before
# create_run/save/publish. Its failure is an atomic, explicitly not-started tool
# result and never changes progress, lifecycle, constraints, or completion state.
# 函数用途: 把计划绑定或显式范围校验失败包装成模型可直接修正参数后重试的结构化回执。
def _planned_delegation_result(
    agent: SimpleAgent,
    items: list[CreateSubagentItem],
    allowed_tool_values: list[list[str] | None],
) -> ToolHandlerOutcome | None:
    failure = planned_delegation_failure(agent, items, allowed_tool_values)
    if failure is None:
        return None
    return ToolHandlerOutcome(
        "create_subagents",
        False,
        json.dumps(failure, ensure_ascii=False, indent=2),
        error_code=PLANNED_DELEGATION_ERROR_CODE,
        effect_outcome="not_started",
    )


# LLM: Every system-named item-shaped request, including a one-item batch,
# consumes the same parent-scoped display ordinal. User-authored single-item
# names remain unchanged, matching the existing explicit-name contract.
# 函数用途: 给一条批量派工项补连续编号；单项返工只改系统默认名，不改用户亲自取的名字。
def _indexed_item_params(
    run_params: CreateRunParams,
    *,
    index: int,
    total: int,
) -> CreateRunParams:
    task_name = _indexed_agent_name(
        run_params.agent_name,
        role=run_params.role,
        index=index,
        require_index=total > 1,
    )
    return CreateRunParams(**{**run_params.__dict__, "agent_name": task_name})


# LLM: Single-child and batch creation share one sibling ordinal allocator.
# Explicit custom names remain untouched; only system placeholder names receive
# the stable suffix used by TUI/Web human-readable control surfaces.
# 函数用途: 给单个补派的系统默认名称续上同一父级的历史编号，避免每批都显示同名 worker。
def _indexed_single_run_params(agent: SimpleAgent, run_params: CreateRunParams) -> CreateRunParams:
    index = _next_system_lineage_index(agent, [run_params])
    task_name = _indexed_agent_name(
        run_params.agent_name,
        role=run_params.role,
        index=index,
        require_index=True,
    )
    return CreateRunParams(**{**run_params.__dict__, "agent_name": task_name})


def _indexed_agent_name(agent_name: str, *, role: str, index: int, require_index: bool = False) -> str:
    name = str(agent_name or "").strip()
    if _needs_system_lineage_name(name, role=role):
        return f"agent-d{_DEFAULT_DEPTH}-{_role_suffix(role)}-{index}"
    if require_index and not agent_name_has_trailing_identifier(name):
        return f"{name}-{index}"
    return name


# LLM: create_run_params may already have expanded an omitted name into the
# unnumbered top-level lineage stem; it is still system-owned, not an explicit
# display name. Do not broaden this check to arbitrary agent-d* custom names.
# 函数用途: 识别空白/占位名以及系统刚生成但尚未编号的第一层名称。
def _needs_system_lineage_name(agent_name: str, *, role: str) -> bool:
    name = str(agent_name or "").strip()
    if is_placeholder_agent_name(name):
        return True
    expected = f"agent-d{_DEFAULT_DEPTH}-{_role_suffix(role)}"
    return name.casefold() == expected.casefold()


def _role_suffix(role: str) -> str:
    suffix = str(role or "worker").strip().replace("_", "-").strip("-") or "worker"
    if suffix in {"general", "child", "subagent", "agent"}:
        return "worker"
    return suffix


class CreateSubagentsTool(BaseTool):
    model_spec = build_create_subagents_model_spec()
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("mutating"),
        idempotency_policy=IdempotencyPolicy("operation"),
        # output_files 是交付元数据和界面线索，不是文件所有权。与 会话运行时 一样，
        # 父子代理可以共享 cwd 并通过实际 diff/测试合并；只对精确的控制面
        # 身份保留持久逻辑互斥。
        resource_scopes=ResourceScopePolicy(
            parameter_names=("input_refs", "replacement_for_run_ids"),
            parameter_kinds={
                "input_refs": "logical",
                "replacement_for_run_ids": "logical",
            },
        ),
        input_policy=ToolInputPolicy(
            internal_parameters=("dry_run", "extra_write_roots"),
        ),
        promotes_task=True,
    )

    def __init__(self, agent: SimpleAgent):
        self.agent = agent

    # LLM: Root and recursive creation share one owner-local transaction and one propagated tool
    # cancellation token.  The lock ends before child runtime starts, so sibling execution remains
    # parallel while /stop can reconcile every durable prefix of an in-flight nested batch.
    # 函数用途: 创建当前代理的直属下级；无论主代理还是子代理派工，都能在停止安全点及时收口。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        try:
            if current_subagent_run_id(self.agent):
                nested_params = _nested_create_params(self.agent, params)
                if isinstance(nested_params, ToolHandlerOutcome):
                    return nested_params
                with _create_subagents_transaction(self.agent):
                    return execute_child_creation(
                        self.agent,
                        nested_params,
                        tool_name="create_subagents",
                    )
            return execute_create_subagents_service(self.agent, params)
        except ToolCancelled as exc:
            return _cancelled_create_subagents_result(exc)

    # LLM: The Tool Gateway's immutable snapshot is the only source for a root or nested parent's
    # creation-time tool ceiling. Inject it after model input validation and before persistence.
    # 函数用途: 创建 child 前把父代理本轮真实工具快照写进宿主属性，供跨进程权限申请精确裁决。
    def execute_scoped(
        self,
        params: dict[str, object],
        context: ToolInvocationContext,
    ) -> ToolHandlerOutcome:
        with bind_creation_tool_authority(context.runtime_snapshot):
            return self.execute(params)


# LLM: A propagated user/turn cancellation is a known interrupted create, not a generic handler
# crash.  Mark the operation failed (never succeeded or replayable) and tell the stop controller
# to reconcile any durable prefix; generic unknown remains reserved for genuinely unproven effects.
# 函数用途: 把派工安全点收到的停止信号转换成明确“已中断”工具结果，避免界面误报副作用未知。
def _cancelled_create_subagents_result(exc: ToolCancelled) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        "create_subagents",
        False,
        json.dumps(
            {
                "ok": False,
                "error": str(exc) or "interrupted",
                "status": "cancelled",
                "message": "派工已中断；已落盘的子代理由当前停止链按结构化谱系收口。",
            },
            ensure_ascii=False,
        ),
        error_code="CANCELLED",
        effect_outcome="failed",
    )


# LLM: A descendant uses the same one-of contract as the root: one non-empty
# goal or non-empty items whose entries each own a goal. This adapter only converts
# the public batch shape into the hierarchy service's structured children list.
# 函数用途: 把子代理发来的单目标或批量 items 转成下一层创建参数。
def _nested_create_params(
    agent: SimpleAgent,
    params: dict[str, object],
) -> dict[str, object] | ToolHandlerOutcome:
    goal = str(params.get("goal") or "").strip()
    items = create_items_from_params(params)
    if isinstance(items, str):
        return ToolHandlerOutcome(
            "create_subagents",
            False,
            items,
            error_code="TOOL_INVALID_ARGUMENTS",
        )
    if not goal and not items:
        return ToolHandlerOutcome(
            "create_subagents",
            False,
            "create_subagents 需要一个非空 goal，或一个含独立 goal 的非空 items 批次。",
            error_code="TOOL_INVALID_ARGUMENTS",
        )
    delegated_items = items or [CreateSubagentItem(goal=goal, params=dict(params))]
    allowed_tool_values = [subagent_allowed_tools(item.params) for item in delegated_items]
    if invalid := _planned_delegation_result(agent, delegated_items, allowed_tool_values):
        return invalid
    children = [dict(item.params) for item in delegated_items]
    for child in children:
        child.pop("items", None)
    return {
        "children": children,
        "dry_run": bool(params.get("dry_run", False)),
        "max_depth": params.get("max_depth", 0),
        "max_children": params.get("max_children", 0),
    }


# LLM: Root child creation shares the current tool cancellation token.  ToolCancelled must
# escape the diagnostic fallback so the canonical Tool Gateway records CANCELLED rather than
# misclassifying an explicit /stop as an invalid model argument.
# 函数用途: 在唯一创建事务里创建根子代理；用户停止时立即退出安全点并交给停止侧收口已落盘记录。
def execute_create_subagents_service(
    agent: SimpleAgent,
    params: dict[str, object],
) -> ToolHandlerOutcome:
    """Create audit/runtime children without pretending an internal service call is a model tool call."""

    try:
        with _create_subagents_transaction(agent):
            return _execute_create_subagents(agent, params)
    except ToolCancelled:
        raise
    except Exception as exc:
        # create_subagents 在真实 dispatch 路径上会偶发崩溃(真机 B1/R3:合法 goal+output_files
        # 调用也抛异常,堆栈没落到任何日志,模型只看到无信息、retryable=False 的 UNKNOWN_ERROR 兜底码
        # 就放弃、退回主代理独自写)。这里兜住异常:① 记完整 traceback 便于定位;② 把异常类型+摘要
        # 写进报错消息(工具账本里就能看到崩在哪);③ 用 retryable 的 TOOL_INVALID_ARGUMENTS 让模型
        # 换简单写法重试,而不是吞成 UNKNOWN_ERROR 直接弃疗。
        import logging
        import traceback as _tb

        logging.getLogger(__name__).error(
            "create_subagents crashed: %s\n%s",
            exc,
            _tb.format_exc(),
        )
        return ToolHandlerOutcome(
            "create_subagents",
            False,
            f"create_subagents 执行时内部出错({type(exc).__name__}: {exc})。多半是某个参数触发的内部问题——"
            "换最简单的写法重试:只传一个 goal、先别带 output_files 等附加字段。别因此就改回自己写。",
            error_code="TOOL_INVALID_ARGUMENTS",
        )


# LLM: One owner-local file guard serializes only create preflight/materialization;
# stop-side reconciliation also waits on this exact manager-owned boundary before
# re-reading lineage. Child runtime remains fully parallel and never holds this lock.
# 函数用途: 复用 manager 的唯一创建事务锁，防止并发重复创建和停止时漏掉迟到子代理。
def _create_subagents_transaction(agent: object):
    guard = getattr(getattr(agent, "subagents", None), "creation_guard", None)
    if not callable(guard):
        return nullcontext()
    return guard()

# LLM: All root child creation modes converge here. A single child requires goal;
# a batch is authoritative through each item goal and may omit redundant top-level
# goal. Both paths share identity, scope, progress, and lifecycle publication.
# 函数用途: 按单目标或批量 items 校验并创建直属子代理，再交给统一后台调度链启动。
def _execute_create_subagents(agent: SimpleAgent, params: dict[str, object]) -> ToolHandlerOutcome:
    if not agent.config.enable_subagents:
        return ToolHandlerOutcome("create_subagents", False, "配置已禁用 subagent。", error_code="TOOL_UNAVAILABLE")
    items_result = _items_result(agent, params)
    if items_result is not None:
        return items_result
    if not str(params.get("goal") or "").strip():
        return ToolHandlerOutcome(
            "create_subagents",
            False,
            "create_subagents 需要一个非空 goal，或一个含独立 goal 的非空 items 批次。"
            "这里与用户是否使用 /goal 无关；普通聊天任务也可以派子代理。两种正确写法:"
            '① 派一个: {"goal":"这个子代理要完成的具体任务"};'
            '② 派多个不同任务: {"items":[{"goal":"任务A"},{"goal":"任务B"}]};'
            "请补全其中一种后重试；别因为这个就改回自己写。",
            error_code="TOOL_INVALID_ARGUMENTS",
        )
    if "count" in params:
        return ToolHandlerOutcome(
            "create_subagents",
            False,
            "create_subagents 不接受 count 克隆同一任务。只派一个时直接传 goal；"
            "需要并行时使用 items 明确列出互不重复的具体子任务和各自交付边界。",
            error_code="TOOL_INVALID_ARGUMENTS",
        )
    related_params, relation_error = prepare_audit_finding_relation(
        agent,
        params,
        goal=str(params.get("goal") or ""),
    )
    if relation_error:
        return ToolHandlerOutcome(
            "create_subagents",
            False,
            relation_error,
            error_code="TOOL_INVALID_ARGUMENTS",
        )
    single_items = [
        CreateSubagentItem(
            goal=str(related_params.get("goal") or "").strip(),
            params=related_params,
        )
    ]
    prepared = _prepare_single_mode(agent, related_params)
    if isinstance(prepared, ToolHandlerOutcome):
        return prepared
    allowed_tools, run_params = prepared
    if invalid := _planned_delegation_result(agent, single_items, [allowed_tools]):
        return invalid
    run_params = _indexed_single_run_params(agent, run_params)
    task_params = [run_params]
    if invalid := _replacement_preflight_result(agent, task_params):
        return invalid
    return _created_tasks_result(
        agent,
        _resolve_task_params(agent, task_params),
        allowed_tools,
        related_params,
    )


def _items_result(agent: SimpleAgent, params: dict[str, object]) -> ToolHandlerOutcome | None:
    items = create_items_from_params(params)
    if isinstance(items, str):
        return ToolHandlerOutcome("create_subagents", False, items, error_code="TOOL_INVALID_ARGUMENTS")
    if items:
        return _execute_items(agent, items, params)
    return None


def _prepare_single_mode(
    agent: SimpleAgent,
    params: dict[str, object],
) -> tuple[list[str] | None, CreateRunParams] | ToolHandlerOutcome:
    params = append_parent_shared_context(agent, params)
    params, skill_error = _params_with_skill_snapshot_refs(agent, params)
    if skill_error:
        return ToolHandlerOutcome(
            "create_subagents",
            False,
            skill_error,
            error_code="TOOL_INVALID_ARGUMENTS",
        )
    params, audit_scope_error = prepare_audit_child_creation_scope(agent, params)
    if audit_scope_error:
        return ToolHandlerOutcome(
            "create_subagents",
            False,
            audit_scope_error,
            error_code="TOOL_INVALID_ARGUMENTS",
        )
    goal = str(params.get("goal") or "").strip()
    capacity = _checked_creation_capacity(agent)
    if isinstance(capacity, ToolHandlerOutcome):
        return capacity
    slots, limits = capacity
    if slots < 1:
        return _subagent_quota_result(1, slots, limits)
    allowed_tools = subagent_allowed_tools(params)
    validation = _validate_single_goal(ValidateSingleGoalRequest(agent, params, goal, allowed_tools))
    if validation:
        return ToolHandlerOutcome("create_subagents", False, validation, error_code="TOOL_INVALID_ARGUMENTS")
    return allowed_tools, create_run_params(agent, params, goal, allowed_tools)


# LLM: A cancellation after materialization but before publication must leave the record visible
# to stop-side lineage reconciliation without binding or starting a new child execution.
# 函数用途: 发布单个已创建子代理前再次检查停止信号，避免停止后仍启动后台执行。
def _created_tasks_result(
    agent: SimpleAgent,
    resolutions: list[CreateTaskResolution],
    allowed_tools: list[str] | str | None,
    request_params: dict[str, object],
) -> ToolHandlerOutcome:
    raise_if_cancelled()
    tasks = [item.task for item in resolutions]
    replacement_records = record_create_replacements(agent, tasks)
    if replacement_records and not replacement_records_allow_start(replacement_records):
        return _replacement_record_failure_result(agent, resolutions, replacement_records)
    lifecycle = publish_created_subagents(CreatedSubagentLifecycleRequest(agent, tasks, request_params))
    relation_fence = fence_inactive_audit_investigations(agent, tasks)
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
    if relation_fence:
        payload["finding_investigation_fence"] = relation_fence
    # 派工即种账本(学 终端应用 TodoWrite):模型的派工计划自动落成 task_progress 待办,
    # 收口闸/出口续航才有账可守;note 同时在工具结果里当面提醒(终端应用 式 tool-result nudge)。
    if seed := seed_dispatch_task_progress(agent, tasks):
        payload["task_progress_seed"] = {**seed, "note": DISPATCH_SEED_NOTE}
    # P1 covers 绑定回执:回显绑定/警示绑错 id/没绑时提醒清单还有 open 项可绑。
    if binding := dispatch_coverage_binding(agent, tasks):
        payload["coverage_binding"] = binding
    return _create_subagents_success(payload)


def _execute_items(
    agent: SimpleAgent,
    items: list[CreateSubagentItem],
    request_params: dict[str, object],
) -> ToolHandlerOutcome:
    capacity = _checked_creation_capacity(agent)
    if isinstance(capacity, ToolHandlerOutcome):
        return capacity
    slots, limits = capacity
    if len(items) > slots:
        return _subagent_quota_result(len(items), slots, limits)
    related_items, relation_error = _items_with_finding_relations(agent, items)
    if relation_error:
        return ToolHandlerOutcome(
            "create_subagents",
            False,
            relation_error,
            error_code="TOOL_INVALID_ARGUMENTS",
        )
    capped = _items_with_parent_context(agent, related_items)
    capped, skill_error = _items_with_skill_snapshot_refs(agent, capped)
    if skill_error:
        return ToolHandlerOutcome(
            "create_subagents",
            False,
            skill_error,
            error_code="TOOL_INVALID_ARGUMENTS",
        )
    capped, audit_scope_error = _items_with_audit_child_scopes(agent, capped)
    if audit_scope_error:
        return ToolHandlerOutcome(
            "create_subagents",
            False,
            audit_scope_error,
            error_code="TOOL_INVALID_ARGUMENTS",
        )
    allowed_tool_values = [subagent_allowed_tools(item.params) for item in capped]
    if invalid := _planned_delegation_result(agent, capped, allowed_tool_values):
        return invalid
    validation = _validate_items(agent, capped, allowed_tool_values)
    if validation:
        return ToolHandlerOutcome("create_subagents", False, validation, error_code="TOOL_INVALID_ARGUMENTS")
    task_params = _indexed_item_run_params(agent, capped)
    if invalid := _replacement_preflight_result(agent, task_params):
        return invalid
    resolutions = _resolve_task_params(agent, task_params)
    return _created_items_result(CreatedItemsResultRequest(
        agent=agent,
        resolutions=resolutions,
        allowed_tool_values=allowed_tool_values,
        request_params=request_params,
        capped_items=capped,
    ))


# LLM: Batch persistence is cancellation-aware between records.  Partial canonical records are
# intentional and remain discoverable by exact request lineage; no late lifecycle publication is
# allowed after the token flips.
# 函数用途: 批量保存和发布子代理时逐项检查停止信号，让大批派工可以在安全点及时收口。
def _created_items_result(request: CreatedItemsResultRequest) -> ToolHandlerOutcome:
    tasks = [item.task for item in request.resolutions]
    for task in tasks:
        raise_if_cancelled()
        request.agent.subagents.save(task)
    raise_if_cancelled()
    replacement_records = record_create_replacements(request.agent, tasks)
    if replacement_records and not replacement_records_allow_start(replacement_records):
        return _replacement_record_failure_result(
            request.agent,
            request.resolutions,
            replacement_records,
        )
    payload_request = _items_payload_request(request.request_params, request.capped_items)
    lifecycle = publish_created_subagents(CreatedSubagentLifecycleRequest(request.agent, tasks, payload_request))
    relation_fence = fence_inactive_audit_investigations(request.agent, tasks)
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
    if relation_fence:
        payload["finding_investigation_fence"] = relation_fence
    payload["batch_mode"] = "items"
    # 单任务与 items 批量共用同一套进度账本和监督出口。
    if seed := seed_dispatch_task_progress(request.agent, tasks):
        payload["task_progress_seed"] = {**seed, "note": DISPATCH_SEED_NOTE}
    # P1 covers 绑定回执。
    if binding := dispatch_coverage_binding(request.agent, tasks):
        payload["coverage_binding"] = binding
    return _create_subagents_success(payload)


# LLM: Replacement source validation runs after full parameter normalization but
# before any run id, workspace, authority row, or lifecycle event is materialized.
# 函数用途: 把无效接管关系作为整批未启动错误返回，避免先造 child 再发现旧 run 不可接管。
def _replacement_preflight_result(
    agent: SimpleAgent,
    task_params: list[CreateRunParams],
) -> ToolHandlerOutcome | None:
    issues = validate_create_replacements(agent, task_params)
    if not issues:
        return None
    payload = {
        "ok": False,
        "error_code": "SUBAGENT_REPLACEMENT_INVALID",
        "error": "接管关系无效；本批没有创建任何子代理。",
        "issues": issues,
        "next_action": {
            "action": "repair_replacement_run_ids_and_retry",
            "retry_tool": "create_subagents",
        },
    }
    return ToolHandlerOutcome(
        "create_subagents",
        False,
        json.dumps(payload, ensure_ascii=False, indent=2),
        error_code="SUBAGENT_REPLACEMENT_INVALID",
        effect_outcome="not_started",
    )


# LLM: Lifecycle publication is forbidden after a replacement edge write fails;
# newly created records are fenced terminal and their failure remains structured.
# 函数用途: 接管落账异常时取消尚未启动的新 child，并向模型返回可恢复的明确错误。
def _replacement_record_failure_result(
    agent: SimpleAgent,
    resolutions: list[CreateTaskResolution],
    records: list[dict[str, object]],
) -> ToolHandlerOutcome:
    cancelled = cancel_unstarted_replacement_tasks(agent.subagents, resolutions, records)
    payload = {
        "ok": False,
        "error_code": "SUBAGENT_REPLACEMENT_RECORD_FAILED",
        "error": "接管关系未全部落账；本批新 child 均未启动。",
        "replacement_records": records,
        "creation_fence": cancelled,
        "next_action": {
            "action": "retry_after_replacement_state_recovers",
            "retry_tool": "create_subagents",
        },
    }
    return ToolHandlerOutcome(
        "create_subagents",
        False,
        json.dumps(payload, ensure_ascii=False, indent=2),
        error_code="SUBAGENT_REPLACEMENT_RECORD_FAILED",
        effect_outcome="not_started",
    )


# LLM: The compact result envelope preserves typed scheduling and bounded Todo
# facts across large-output externalization; it must not copy goals or child output.
# 函数用途: 生成成功派工结果，并保留 TUI 仍需读取的结构化小字段。
def _create_subagents_success(payload: dict[str, object]) -> ToolHandlerOutcome:
    lifecycle = payload.get("schedule_lifecycle")
    envelope = {"schedule_lifecycle": dict(lifecycle)} if isinstance(lifecycle, dict) else {}
    progress_seed = payload.get("task_progress_seed")
    if isinstance(progress_seed, dict):
        envelope["task_progress_seed"] = {
            "run_id": str(progress_seed.get("run_id") or ""),
            "seeded": _positive_limit(progress_seed.get("seeded")),
            "generation_id": str(progress_seed.get("generation_id") or ""),
            "plan_revision": _positive_limit(progress_seed.get("plan_revision")),
            "items": [
                {
                    "id": str(item.get("id") or ""),
                    "title": str(item.get("title") or ""),
                    "status": str(item.get("status") or "pending"),
                }
                for item in list(progress_seed.get("items") or [])[:64]
                if isinstance(item, dict)
            ],
        }
    coverage_binding = payload.get("coverage_binding")
    if isinstance(coverage_binding, dict):
        envelope["coverage_binding"] = _bounded_coverage_binding(coverage_binding)
    return ToolHandlerOutcome(
        "create_subagents",
        True,
        json.dumps(payload, ensure_ascii=False, indent=2),
        result_envelope=envelope,
    )


# LLM: Only bounded ids and explicit exact bindings survive output reduction;
# human notes and arbitrary provider payload fields must not enter the envelope.
# 函数用途: 压缩派工绑定回执，供当前模型轮和 TUI 读取稳定的小字段。
def _bounded_coverage_binding(binding: dict[str, object]) -> dict[str, object]:
    raw_bound = binding.get("bound")
    bound = dict(raw_bound) if isinstance(raw_bound, dict) else {}
    return {
        "schema_version": str(binding.get("schema_version") or ""),
        "binding_mode": str(binding.get("binding_mode") or ""),
        "ledger_run_id": str(binding.get("ledger_run_id") or ""),
        "open_target_ids": _bounded_text_list(binding.get("open_target_ids")),
        "open_count": _positive_limit(binding.get("open_count")),
        "bound": {
            str(run_id): _bounded_text_list(covers)
            for run_id, covers in bound.items()
            if str(run_id or "").strip()
        },
        "unbound_child_run_ids": _bounded_text_list(
            binding.get("unbound_child_run_ids")
        ),
    }


# LLM: Envelope arrays are capped and normalized without accepting scalar prose.
# 函数用途: 把绑定回执中的字符串数组限制为最多 24 个非空值。
def _bounded_text_list(value: object) -> list[str]:
    values = value if isinstance(value, list | tuple) else ()
    return [str(item) for item in list(values)[:24] if str(item or "").strip()]


def _items_with_parent_context(agent: SimpleAgent, items: list[CreateSubagentItem]) -> list[CreateSubagentItem]:
    return [
        CreateSubagentItem(goal=item.goal, params=append_parent_shared_context(agent, item.params))
        for item in items
    ]


def _items_with_finding_relations(
    agent: SimpleAgent,
    items: list[CreateSubagentItem],
) -> tuple[list[CreateSubagentItem], str]:
    related: list[CreateSubagentItem] = []
    for item in items:
        params, error = prepare_audit_finding_relation(
            agent,
            item.params,
            goal=item.goal,
        )
        if error:
            return [], error
        related.append(CreateSubagentItem(goal=item.goal, params=params))
    return related, ""


def _items_with_skill_snapshot_refs(
    agent: SimpleAgent,
    items: list[CreateSubagentItem],
) -> tuple[list[CreateSubagentItem], str]:
    normalized: list[CreateSubagentItem] = []
    for index, item in enumerate(items):
        params, error = _params_with_skill_snapshot_refs(agent, item.params)
        if error:
            return [], f"items[{index}] {error}"
        normalized.append(CreateSubagentItem(goal=item.goal, params=params))
    return normalized, ""


def _items_with_audit_child_scopes(
    agent: SimpleAgent,
    items: list[CreateSubagentItem],
) -> tuple[list[CreateSubagentItem], str]:
    scoped: list[CreateSubagentItem] = []
    for index, item in enumerate(items):
        params, error = prepare_audit_child_creation_scope(agent, item.params)
        if error:
            return [], f"items[{index}] {error}"
        scoped.append(CreateSubagentItem(goal=item.goal, params=params))
    return scoped, ""


def _params_with_skill_snapshot_refs(
    agent: SimpleAgent,
    params: dict[str, object],
) -> tuple[dict[str, object], str]:
    requested = string_list(params.get("allowed_skills"), TOOL_TEXT_LIST_OPTIONS)
    normalized = dict(params)
    attrs = dict(params.get("attributes") or {}) if isinstance(params.get("attributes"), dict) else {}
    attrs.pop("skill_snapshot_refs", None)
    if not requested:
        normalized.pop("allowed_skills", None)
        normalized["attributes"] = attrs
        return normalized, ""
    try:
        snapshot = agent.current_skill_snapshot()
        entries = []
        missing = []
        seen: set[str] = set()
        for reference in requested:
            entry = snapshot.resolve(reference)
            if entry is None:
                missing.append(reference)
                continue
            if entry.stable_id not in seen:
                entries.append(entry)
                seen.add(entry.stable_id)
    except SkillSnapshotError as exc:
        return normalized, f"无法读取当前 Skill 快照：{exc}"
    if missing:
        return normalized, "allowed_skills 含当前 owner 不可用或已禁用的 Skill：" + ", ".join(missing)
    normalized["allowed_skills"] = [entry.stable_id for entry in entries]
    attrs["skill_snapshot_refs"] = [
        {
            "stable_id": entry.stable_id,
            "name": entry.name,
            "source": entry.source,
            "content_sha256": entry.content_sha256,
        }
        for entry in entries
    ]
    normalized["attributes"] = attrs
    return normalized, ""


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
    prepared = [
        create_run_params(
            agent,
            item.params,
            item.goal,
            subagent_allowed_tools(item.params),
        )
        for item in items
    ]
    start_index = _next_system_lineage_index(agent, prepared)
    run_params_by_item: list[CreateRunParams] = []
    for offset, run_params in enumerate(prepared):
        index = start_index + offset
        indexed = _indexed_item_params(run_params, index=index, total=len(items))
        run_params_by_item.append(indexed)
    return run_params_by_item


# LLM: Generated display names are stable sibling identities for TUI/Web control.
# The ordinal is scoped to the exact parent and generated root-lineage prefix and
# includes terminal history so a
# later batch cannot reuse an earlier child's name.
# 函数用途: 为同一父代理后续批次找到尚未使用的下一个系统编号。
def _next_system_lineage_index(
    agent: SimpleAgent,
    prepared: list[CreateRunParams],
) -> int:
    if not prepared:
        return 1
    parent_id = str(prepared[0].parent_id or "").strip()
    pattern = re.compile(
        rf"^agent-d{_DEFAULT_DEPTH}-.+-(?P<index>[1-9][0-9]*)$"
    )
    try:
        existing = list(agent.subagents.list_runs())
    except (AttributeError, OSError, TypeError, ValueError):
        return 1
    highest = 0
    for task in existing:
        if str(getattr(task, "parent_id", "") or "").strip() != parent_id:
            continue
        match = pattern.fullmatch(str(getattr(task, "agent_name", "") or "").strip())
        if match:
            highest = max(highest, int(match.group("index")))
    return highest + 1


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


# LLM: Resolve/materialize one child at a time and observe the already-propagated tool token
# between durable records.  A partial prefix remains structurally linked to the request so the
# asynchronous stop reconciler can cancel it after the creation guard is released.
# 函数用途: 逐个创建子代理，并在每个安全点响应用户停止，避免仍把整批剩余任务落盘。
def _resolve_task_params(agent: SimpleAgent, task_params: list[CreateRunParams]) -> list[CreateTaskResolution]:
    resolutions: list[CreateTaskResolution] = []
    for item in task_params:
        raise_if_cancelled()
        resolutions.append(resolve_create_run(agent.subagents, item))
    raise_if_cancelled()
    return resolutions


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
) -> tuple[int, dict[str, int]] | ToolHandlerOutcome:
    try:
        return _available_creation_slots(agent)
    except _SubagentCapacityStateError:
        return ToolHandlerOutcome(
            "create_subagents",
            False,
            "当前无法读取权威子代理容量状态；本批没有创建任何子代理。",
            error_code="SUBAGENT_CAPACITY_UNAVAILABLE",
            effect_outcome="not_started",
        )


# LLM: ``max_subagents`` is root-session scoped like 会话运行时 AgentControl; unrelated
# durable runs may stay resumable but cannot consume this tree's slots. Explicit
# owner-policy caps remain owner-wide and count every non-terminal run.
# 函数用途: 按当前根任务容量、全局管理员配额和单次上限计算真正可用的创建槽位。
def _available_creation_slots(agent: object) -> tuple[int, dict[str, int]]:
    """Return the strictest remaining session/owner/task/per-call capacity."""
    session_cap = _configured_max_subagents(agent)
    owner_policy_cap = _positive_limit(
        getattr(getattr(agent, "owner_policy", None), "max_subagents", 0)
    )
    per_call_cap = _positive_limit(
        getattr(
            getattr(agent, "config", None),
            "subagent_hierarchy_max_children_per_tool_call",
            0,
        )
    )
    task_cap = _positive_limit(getattr(getattr(agent, "config", None), "task_max_subagents", 0))
    owner_active, task_active = _active_subagent_counts(agent)
    has_root_scope = bool(_current_root_task_id(agent))
    session_active = task_active if has_root_scope else owner_active
    candidates = [session_cap - session_active] if session_cap else []
    if owner_policy_cap:
        candidates.append(owner_policy_cap - owner_active)
    active_agent_cap = _positive_limit(
        getattr(getattr(agent, "owner_policy", None), "max_active_agents", 0)
    )
    if active_agent_cap:
        candidates.append(active_agent_cap - 1 - owner_active)
    if per_call_cap:
        candidates.append(per_call_cap)
    if task_cap:
        candidates.append(task_cap - task_active)
    slots = max(0, min(candidates)) if candidates else _DEFAULT_MAX_SUBAGENTS
    return slots, {
        "session_cap": session_cap,
        "session_active": session_active,
        "owner_cap": owner_policy_cap,
        "owner_active": owner_active,
        "active_agent_cap": active_agent_cap,
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
) -> ToolHandlerOutcome:
    payload = {
        "ok": False,
        "error_code": "SUBAGENT_CAPACITY_EXCEEDED",
        "error": "本次请求的子代理数量超过当前可用容量；没有创建任何部分批次。",
        "requested": requested,
        "available": available,
        "limits": limits,
        "how_to_fix": "减少 items 数量后重试；已有子代理结束后容量会自动释放。",
    }
    return ToolHandlerOutcome(
        "create_subagents",
        False,
        json.dumps(payload, ensure_ascii=False, indent=2),
        error_code="SUBAGENT_CAPACITY_EXCEEDED",
        effect_outcome="not_started",
    )
