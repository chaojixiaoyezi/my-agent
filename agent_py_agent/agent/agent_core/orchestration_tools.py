
from __future__ import annotations

"""exposes model-callable orchestration tools backed by SimpleAgent subagent workflows.

这些不是普通文件工具，而是'主代理让模型触发子代理流程'的工具。
创建子代理、发送消息和直属子代理控制都在这里，真实启动由系统后台调度器负责。
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..capability.skill_snapshot import SkillSnapshotError
from ..common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from ..settings.defaults import default_config_int
from ..subagents.services.base import CreateRunParams
from ..subagents.services.hierarchy.scheduled_role import (
    agent_name_has_trailing_identifier,
    is_placeholder_agent_name,
)
from ..tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolInputPolicy,
    ToolRuntimePolicy,
)
from .hierarchy_tools import execute_child_creation
from .orchestration.create_constraints import (
    CreateTaskResolution,
    creation_active_lineage_conflicts,
    creation_output_scope_conflicts,
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
    autobind_covers_from_goal_ids,
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
from .orchestration.replacements import record_create_replacements
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
from .orchestration.work_scope import add_work_scope_key
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
    model_spec = build_create_subagents_model_spec()
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("mutating"),
        idempotency_policy=IdempotencyPolicy("operation"),
        # seq 253 锁层级自冲突：input_refs（artifact 引用）与
        # replacement_for_run_ids（run id 列表）不是物理写根——缺省参数走
        # None 兜底会锁 workspace:{cwd}（父锁），与 output_files 声明的工作
        # 区内子路径锁父子重叠 → 同一事务自冲突 → TOOL_OPERATION_STORE_UNAVAILABLE
        # → 工具从未运行（真机 natural-language e2e 实锤：子代理 0 创建）。
        # 标 logical（seq 248 #6 既有机制）只让 output_files 锁 workspace。
        resource_scopes=ResourceScopePolicy(
            parameter_names=("output_files", "input_refs", "replacement_for_run_ids"),
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

    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        if current_subagent_run_id(self.agent):
            nested_params = _nested_create_params(params)
            if isinstance(nested_params, ToolHandlerOutcome):
                return nested_params
            return execute_child_creation(
                self.agent,
                nested_params,
                tool_name="create_subagents",
            )
        return execute_create_subagents_service(self.agent, params)


# LLM: A descendant uses the same create_subagents schema as the root; this adapter only
# converts the public batch shape into the hierarchy service's structured children list.
# 函数用途: 把子代理发来的统一 create_subagents 参数转换为下一层创建参数。
def _nested_create_params(
    params: dict[str, object],
) -> dict[str, object] | ToolHandlerOutcome:
    goal = str(params.get("goal") or "").strip()
    if not goal:
        return ToolHandlerOutcome(
            "create_subagents",
            False,
            "create_subagents 缺少始终必填的 goal。",
            error_code="TOOL_INVALID_ARGUMENTS",
        )
    items = create_items_from_params(params)
    if isinstance(items, str):
        return ToolHandlerOutcome(
            "create_subagents",
            False,
            items,
            error_code="TOOL_INVALID_ARGUMENTS",
        )
    children = [dict(item.params) for item in items] if items else [dict(params)]
    for child in children:
        child.pop("items", None)
    return {
        "children": children,
        "dry_run": bool(params.get("dry_run", False)),
        "max_depth": params.get("max_depth", 0),
        "max_children": params.get("max_children", 0),
    }


def execute_create_subagents_service(
    agent: SimpleAgent,
    params: dict[str, object],
) -> ToolHandlerOutcome:
    """Create audit/runtime children without pretending an internal service call is a model tool call."""

    try:
        return _execute_create_subagents(agent, params)
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

def _execute_create_subagents(agent: SimpleAgent, params: dict[str, object]) -> ToolHandlerOutcome:
    if not agent.config.enable_subagents:
        return ToolHandlerOutcome("create_subagents", False, "配置已禁用 subagent。", error_code="TOOL_UNAVAILABLE")
    if not str(params.get("goal") or "").strip():
        # goal 是整批派工的结构化意图，items 模式也不能省。这样 schema 入口与直接
        # execute 入口使用同一硬约束，不靠模型正文猜本批任务是什么。
        return ToolHandlerOutcome(
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
        return ToolHandlerOutcome(
            "create_subagents",
            False,
            "create_subagents 不接受 count 克隆同一任务。只派一个时直接传 goal；"
            "需要并行时使用 items 明确列出互不重复的具体子任务和各自交付边界。",
            error_code="TOOL_INVALID_ARGUMENTS",
        )
    items_result = _items_result(agent, params)
    if items_result is not None:
        return items_result
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
    prepared = _prepare_single_mode(agent, related_params)
    if isinstance(prepared, ToolHandlerOutcome):
        return prepared
    allowed_tools, run_params = prepared
    task_params = [run_params]
    if conflict := _output_scope_conflict_result(agent, task_params):
        return conflict
    if conflict := _active_lineage_creation_result(agent, task_params):
        return conflict
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


def _created_tasks_result(
    agent: SimpleAgent,
    resolutions: list[CreateTaskResolution],
    allowed_tools: list[str] | str | None,
    request_params: dict[str, object],
) -> ToolHandlerOutcome:
    tasks = [item.task for item in resolutions]
    replacement_records = record_create_replacements(agent, tasks)
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
    # P-bigbuild 参数落难兜底:goal 里字面写了清单项 id 却没带 covers 的 item,创建前自动补绑
    # (纯 id token 对账;显式 covers 一字不动),covers 经属性白名单随任务落 canonical。
    autobind_covers_from_goal_ids(agent, capped)
    allowed_tool_values = [subagent_allowed_tools(item.params) for item in capped]
    validation = _validate_items(agent, capped, allowed_tool_values)
    if validation:
        return ToolHandlerOutcome("create_subagents", False, validation, error_code="TOOL_INVALID_ARGUMENTS")
    task_params = _indexed_item_run_params(agent, capped)
    if conflict := _output_scope_conflict_result(agent, task_params):
        return conflict
    if conflict := _active_lineage_creation_result(agent, task_params):
        return conflict
    resolutions = _resolve_task_params(agent, task_params)
    return _created_items_result(CreatedItemsResultRequest(
        agent=agent,
        resolutions=resolutions,
        allowed_tool_values=allowed_tool_values,
        request_params=request_params,
        capped_items=capped,
    ))


def _created_items_result(request: CreatedItemsResultRequest) -> ToolHandlerOutcome:
    tasks = [item.task for item in request.resolutions]
    for task in tasks:
        request.agent.subagents.save(task)
    replacement_records = record_create_replacements(request.agent, tasks)
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
    return ToolHandlerOutcome(
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


# LLM: conflict 来源只读结构化字段，不能从 goal 或错误文案猜测。
# 函数用途: 把输出锁冲突分成本批内重叠、已有 run 占用和对应 run id。
def _output_scope_conflict_groups(
    conflicts: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[str]]:
    run_ids = list(dict.fromkeys(
        str(item.get("existing_run_id") or "").strip()
        for item in conflicts
        if str(item.get("existing_run_id") or "").strip()
    ))
    proposed_conflicts = [
        item for item in conflicts
        if item.get("conflicting_proposed_index") is not None
    ]
    existing_conflicts = [
        item for item in conflicts
        if str(item.get("existing_run_id") or "").strip()
    ]
    return proposed_conflicts, existing_conflicts, run_ids


# LLM: repair 动作与 conflict 类型一一对应；只有 existing run 才能产生等生命周期的动作。
# 函数用途: 生成可供模型逐项修复的结构化动作列表。
def _output_scope_required_repairs(
    proposed_conflicts: list[dict[str, object]],
    existing_conflicts: list[dict[str, object]],
    run_ids: list[str],
) -> list[dict[str, object]]:
    required_repairs: list[dict[str, object]] = []
    if proposed_conflicts:
        required_repairs.append({
            "action": "revise_proposed_output_scopes",
            "reason": (
                "同一次请求里的多个 item 声明了重叠的 output_files/output_refs；"
                "请给每个 item 分配互不重叠的交付路径，或把共享文件合并为一个 item。"
            ),
        })
    if existing_conflicts:
        required_repairs.append({
            "action": "await_existing_run_lifecycle_event",
            "run_ids": run_ids,
            "reason": (
                "既有 run 仍持有该交付目标；宿主会在其生命周期变化时唤醒直接父级，"
                "确需接管时再用 replacement_for_run_ids 显式创建替补。"
            ),
        })
    return required_repairs


# LLM: 批内冲突不得返回 await；所有拒绝都显式保留用户原始约束。
# 函数用途: 根据冲突组合返回人类可读错误和唯一下一动作。
def _output_scope_next_action(
    proposed_conflicts: list[dict[str, object]],
    existing_conflicts: list[dict[str, object]],
    run_ids: list[str],
    required_repairs: list[dict[str, object]],
) -> tuple[str, dict[str, object]]:
    if proposed_conflicts and existing_conflicts:
        return (
            "本次请求同时存在批内交付路径重叠和既有子代理输出占用；本批没有创建任何子代理。",
            {
                "action": "resolve_output_scope_conflicts_and_retry",
                "required_repairs": required_repairs,
                "retry_tool": "create_subagents",
                "preserve_user_constraints": True,
            },
        )
    if proposed_conflicts:
        return (
            "本次请求里的多个子代理声明了相同的 output_files/output_refs；本批没有创建任何子代理。",
            {
                "action": "revise_proposed_output_scopes_and_retry",
                "required_repairs": required_repairs,
                "retry_tool": "create_subagents",
                "preserve_user_constraints": True,
                "reason": (
                    "修正本批 items 的交付边界后重试；"
                    "工具失败不会变更用户的原始要求，也不会授权父代理改用被用户禁止的做法。"
                ),
            },
        )
    return (
        "未结束的同级子代理已占用相同 output_files/output_refs；本批没有创建任何子代理。",
        {
            "action": "await_existing_run_lifecycle_event",
            "run_ids": run_ids,
            "required_repairs": required_repairs,
            "preserve_user_constraints": True,
            "reason": (
                "现有 run 仍持有该交付目标；等它的生命周期事件，"
                "确需接管时再用 replacement_for_run_ids 显式创建替补。"
            ),
        },
    )


# LLM: 输出冲突必须区分本批内重叠与既有 run 占用；前者要修正参数重试，后者才等直属生命周期事件。
# 函数用途: 把共享输出锁冲突包装成无副作用且可直接修正的创建失败回执。
def _output_scope_conflict_result(
    agent: SimpleAgent,
    task_params: list[CreateRunParams],
) -> ToolHandlerOutcome | None:
    conflicts = creation_output_scope_conflicts(agent.subagents, task_params)
    if not conflicts:
        return None
    proposed_conflicts, existing_conflicts, run_ids = _output_scope_conflict_groups(conflicts)
    required_repairs = _output_scope_required_repairs(proposed_conflicts, existing_conflicts, run_ids)
    error, next_action = _output_scope_next_action(
        proposed_conflicts,
        existing_conflicts,
        run_ids,
        required_repairs,
    )
    payload = {
        "ok": False,
        "error_code": "SUBAGENT_OUTPUT_SCOPE_CONFLICT",
        "error": error,
        "conflicts": conflicts,
        "proposed_conflicts": proposed_conflicts,
        "existing_conflicts": existing_conflicts,
        "existing_run_ids": run_ids,
        "next_action": next_action,
    }
    return ToolHandlerOutcome(
        "create_subagents",
        False,
        json.dumps(payload, ensure_ascii=False, indent=2),
        error_code="SUBAGENT_OUTPUT_SCOPE_CONFLICT",
        effect_outcome="not_started",
    )


# LLM: 后台轮遇到活跃同 lineage 时复用原树并等待事件，不能创建第二批影子 run。
# 函数用途: 返回“已有活跃子代理”的结构化拒绝与后续等待说明。
def _active_lineage_creation_result(
    agent: SimpleAgent,
    task_params: list[CreateRunParams],
) -> ToolHandlerOutcome | None:
    current = getattr(agent, "_current_run_params", None)
    if str(getattr(current, "source", "") or "").strip() != "background_main_agent":
        return None
    conflicts = creation_active_lineage_conflicts(agent.subagents, task_params)
    if not conflicts:
        return None
    run_ids = list(dict.fromkeys(
        str(item.get("existing_run_id") or "").strip()
        for item in conflicts
        if str(item.get("existing_run_id") or "").strip()
    ))
    payload = {
        "ok": False,
        "error_code": "SUBAGENT_ACTIVE_LINEAGE_EXISTS",
        "error": (
            "当前后台监督轮所属任务已有未结束子代理；本批没有创建任何子代理。"
            "请查看或引导现有 run，而不是另起一批。"
        ),
        "conflicts": conflicts,
        "existing_run_ids": run_ids,
        "next_action": {
            "action": "await_existing_run_lifecycle_event",
            "run_ids": run_ids,
            "reason": (
                "宿主会把现有 run 的进展送回直接父级；必要时使用 send_guidance 补充消息；"
                "只有明确接管旧 run 时才使用 replacement_for_run_ids。"
            ),
        },
    }
    return ToolHandlerOutcome(
        "create_subagents",
        False,
        json.dumps(payload, ensure_ascii=False, indent=2),
        error_code="SUBAGENT_ACTIVE_LINEAGE_EXISTS",
        effect_outcome="not_started",
    )


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
    if isinstance(raw, str | Path) and str(raw).strip():
        return str(raw).strip()
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None)
    workspace = attrs.get("run_workspace") if isinstance(attrs, dict) else None
    if not isinstance(workspace, dict):
        return ""
    return str(workspace.get("task_root") or "").strip()


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
