# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

"""本模块负责把已审核的工单计划落成 SubAgentTask，并保证 apply/dry-run 边界和最终收口。

新手说明:
这里包含 SubAgentTaskCreator（创建任务的协议接口）、SubagentWorkOrderCreationResult（创建结果）
以及 create_subagent_tasks_from_work_order_plan（唯一会调用 subagents.create_run 的入口）。
私有辅助函数 _work_order_quality_contract、_work_order_context_pack、_work_order_plan_steps
分别负责质量契约继承、上下文打包和步骤生成。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from agent_py_agent.agent.subagents.services.base import CreateRunParams

from .planning import (
    PLAN_NOT_READY_ISSUE,
    LogAnalysisWorkOrderPlan,
    SubagentWorkOrder,
    _merge_unique,
)


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 SubAgentTaskCreator 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 声明 SubAgentTaskCreator 的接口契约，让调用方依赖方法签名而非具体实现。
class SubAgentTaskCreator(Protocol):
    """这个 Protocol 说明本模块只依赖 create_run，不关心具体任务存储或调度实现。

    新手说明:
    Protocol 像一份"接口约定"：只要传进来的对象有 create_run 这个方法，就可以被这里使用。
    这样测试时可以传假的对象，真实运行时可以传真正的子代理管理器，函数本身不用知道细节。

    参数说明:
    这个类本身没有构造参数。它只描述"传进来的对象应该长什么样"。
    如果一个对象实现了 create_run 并接收命名字段，它就满足这个接口。"""

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 create_run 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 组装 create run 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
    def create_run(self, *, params: CreateRunParams) -> Any:
        """create_subagent_tasks_from_work_order_plan 通过它把已审核的工单落成 SubAgentTask。

        新手说明:
        输入是 CreateRunParams bundle，例如 goal、role、allowed_tools 和 context_packs。
        输出通常是任务对象，至少要能读到 id；本模块不会启动任务，只保存创建结果。

        参数说明:
        params 是任务创建参数包，具体字段由真实的 SubAgentManager.create_run 接收。
        常见字段包括 goal、thought、plan、agent_name、role、allowed_tools、acceptance_checks、
        quality_contract、context_manifest、context_packs 等。
        这里把 bundle adapter 限定在协议边界，避免内部继续传散参。

        返回说明:
        返回值通常是 SubAgentTask。调用方至少会读取 task.id、task.status 和 task.verification_status。"""
        ...


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 SubagentWorkOrderCreationResult 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 SubagentWorkOrderCreationResult 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class SubagentWorkOrderCreationResult:
    """它记录 apply/dry-run 模式、创建出的 task ids，以及拒绝创建时的 issue/risk。

    新手说明:
    创建任务是有开关的：apply=False 只返回"如果创建会怎样"，apply=True 才真的调用 create_run。
    这个结果对象把是否 ready、创建了哪些任务、为什么没创建等信息集中返回，方便上层展示或测试。

    字段说明:
    case_id: 本次创建结果对应哪个 case。
    ready: 原始计划是否 ready；如果 False，通常不会创建任何任务。
    apply: 调用方这次是否请求真实创建任务。
    dry_run: apply 的反面；True 表示没有真实创建任务。
    mode: 结果模式，通常是 dry_run 或 apply，方便 UI 或日志直接展示。
    created: 已创建任务的摘要列表，包含 task_id、case_id、role、status、verification_status。
    task_ids: 已创建任务 id 列表，方便后续 load 或展示。
    issues: 创建过程中遇到的问题，例如计划未 ready。
    risks: 从计划继承或创建阶段发现的风险。"""

    case_id: str
    ready: bool
    apply: bool
    dry_run: bool
    mode: str
    created: list[dict[str, Any]] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 to_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to dict 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_dict(self) -> dict[str, Any]:
        """为调用方提供可序列化的 task_ids、created、issues 和 risks。

        新手说明:
        这一步不再创建任何东西，只是把结果包装成更容易打印、返回 JSON 或写测试断言的格式。

        参数说明:
        这个方法没有输入参数，只读取当前结果对象自己的字段。

        返回说明:
        返回 dict，适合给 CLI、API、测试或审计日志使用。"""
        return asdict(self)


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 WorkOrderCreationOptions 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 WorkOrderCreationOptions 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class WorkOrderCreationOptions:
    apply: bool = False
    parent_id: str = ""
    root_id: str = ""
    final_owner: str = "parent"


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _work_order_quality_contract 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 work order quality contract 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
def _work_order_quality_contract(order: SubagentWorkOrder) -> dict[str, Any]:
    """它继承上游 quality_contract，并补上 evidence_required 和 must_check。

    新手说明:
    子代理需要知道必须看哪些证据、按哪些标准检查。
    这里把工单自身的证据和验收项并入契约，最终是否继续由普通任务收口流程处理。

    参数说明:
    order: 单张 SubagentWorkOrder。函数会读取 order.context["quality_contract"]、order.evidence_refs、
    order.acceptance_checks 和 order.goal。

    返回说明:
    返回 quality_contract 字典，后续会写进 SubAgentTask.quality_contract 和 context_pack。"""
    source = order.context.get("quality_contract")
    inherited = dict(source) if isinstance(source, Mapping) else {}
    evidence_required = _merge_unique(inherited.get("evidence_required"), order.evidence_refs)
    must_check = list(order.acceptance_checks)
    for item in inherited.get("must_check", []):
        text = str(item or "").strip()
        if text and text not in must_check:
            must_check.append(text)
    return {
        **inherited,
        "user_visible_goal": inherited.get("user_visible_goal") or order.goal,
        "quality_bar": inherited.get("quality_bar") or "Evidence-backed log analysis for parent final review.",
        "evidence_required": evidence_required,
        "must_check": must_check,
    }


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _work_order_context_pack 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 work order context pack 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
def _work_order_context_pack(order: SubagentWorkOrder) -> dict[str, Any]:
    """它把工单压缩成 role/case/evidence/route/quality 等安全字段，避免泄露不该交给子代理的原始上下文。

    新手说明:
    子代理不需要拿到整个 case 的所有数据，只需要完成任务所需的摘要、证据引用和质量要求。
    这样上下文更小，也更容易控制边界：它知道可以依据哪些 evidence_refs，但不能凭空扩展范围。

    参数说明:
    order: 要转换的 SubagentWorkOrder。它的 role、case_id、evidence_refs、context 和验收门会被打包。

    返回说明:
    返回 context pack 字典。这个字典会放进 SubAgentTask.context_packs，
    给子代理运行时读取；它只包含摘要和引用，不包含原始日志全文。"""
    return {
        "name": "log-analysis-work-order",
        "case_id": order.case_id,
        "role": order.role,
        "evidence_refs": list(order.evidence_refs),
        "route_summary": dict(order.context.get("route_summary") or {}),
        "quality_contract": _work_order_quality_contract(order),
        "case_summary": order.context.get("case_summary", ""),
        "handoff_contract": order.context.get("handoff_contract", ""),
        "expected_input": order.context.get("expected_input", ""),
        "expected_output": order.context.get("expected_output", ""),
    }


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _work_order_plan_steps 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 work order plan steps 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
def _work_order_plan_steps(order: SubagentWorkOrder) -> list[str]:
    """它把工单职责转成 SubAgentTask.plan，并为 reviewer 额外加入证据边界复核步骤。

    新手说明:
    任务系统通常需要一个步骤列表，告诉子代理先看上下文、再按工具和证据边界产出结果。
    reviewer 的风险点不同，它要检查 analyst 报告有没有越过证据，所以这里给 reviewer 多加一步。

    参数说明:
    order: 要生成步骤的工单。函数会使用 order.case_id 显示目标 case，并用 order.role 判断是否是 reviewer。

    返回说明:
    返回字符串列表，每一项是一条给 SubAgentTask.plan 的执行步骤。"""
    steps = [
        f"Read the focused context pack for case {order.case_id}.",
        "Use only allowed tools and retained evidence_refs.",
        "Produce the role-specific contract output without claiming final closeout.",
        "Leave final approval to the parent session gate.",
    ]
    if order.role == "reviewer":
        steps.insert(1, "Check the analyst report against the evidence boundary before any approval recommendation.")
    return steps


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 create_subagent_tasks_from_work_order_plan 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 create subagent tasks from work order plan 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def create_subagent_tasks_from_work_order_plan(
    subagents: SubAgentTaskCreator,
    plan: LogAnalysisWorkOrderPlan,
    *,
    options: WorkOrderCreationOptions | None = None,
    apply: bool = False,
    parent_id: str = "",
    root_id: str = "",
    final_owner: str = "parent",
) -> SubagentWorkOrderCreationResult:
    """Create subagent tasks from a ready log-analysis work-order plan."""

    creation_options = options or WorkOrderCreationOptions(
        apply=bool(apply),
        parent_id=str(parent_id),
        root_id=str(root_id),
        final_owner=str(final_owner),
    )
    mode = "apply" if creation_options.apply else "dry_run"
    issues = list(plan.issues)
    risks = list(plan.risks)
    if not plan.ready and PLAN_NOT_READY_ISSUE not in issues:
        issues.append(PLAN_NOT_READY_ISSUE)
    result = SubagentWorkOrderCreationResult(
        case_id=plan.case_id,
        ready=plan.ready,
        apply=creation_options.apply,
        dry_run=not creation_options.apply,
        mode=mode,
        issues=issues,
        risks=risks,
    )
    if not creation_options.apply or not plan.ready:
        return result

    for order in plan.work_orders:
        _create_ready_order(
            subagents,
            result,
            order,
            options=creation_options,
        )
    return result


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _create_ready_order 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 create ready order 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def _create_ready_order(
    subagents: SubAgentTaskCreator,
    result: SubagentWorkOrderCreationResult,
    order: SubagentWorkOrder,
    *,
    options: WorkOrderCreationOptions,
) -> None:
    if not order.ready:
        _note_skipped_order(result, order)
        return
    task = subagents.create_run(params=CreateRunParams(**_create_run_payload(order, options=options)))
    result.task_ids.append(str(task.id))
    result.created.append(_created_task_summary(task, order))


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _note_skipped_order 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 note skipped order 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
def _note_skipped_order(result: SubagentWorkOrderCreationResult, order: SubagentWorkOrder) -> None:
    issue = f"{order.role} work order is not ready; skipped"
    if issue not in result.issues:
        result.issues.append(issue)


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _create_run_payload 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 create run payload 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def _create_run_payload(order: SubagentWorkOrder, *, options: WorkOrderCreationOptions) -> dict[str, Any]:
    parent_id = options.parent_id
    return {
        "goal": order.goal,
        "thought": f"Manual LOG {order.role} work order for {order.case_id}; stay evidence-bound and leave final closeout to the caller.",
        "plan": _work_order_plan_steps(order),
        "agent_name": f"log-{order.role}",
        "role": order.role,
        "parent_id": parent_id,
        "root_id": options.root_id,
        "allowed_tools": list(order.allowed_tools),
        "owner": order.role,
        "supervisor": parent_id or "parent",
        "final_owner": options.final_owner,
        "acceptance_checks": list(order.acceptance_checks),
        "quality_contract": _work_order_quality_contract(order),
        "context_manifest": _context_manifest(order),
        "context_packs": [_work_order_context_pack(order)],
        # LLM: LOG keeps analyst/reviewer as domain-visible roles while still inheriting closeout contracts.
        "normalize_role": False,
    }


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _context_manifest 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 context manifest 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
def _context_manifest(order: SubagentWorkOrder) -> dict[str, Any]:
    return {
        "task_pack_refs": [
            f"log-analysis-case:{order.case_id}",
            *[f"evidence:{ref}" for ref in order.evidence_refs],
        ],
        "role_pack": f"log-analysis-{order.role}",
        "quality_contract_ref": "context_packs[0].quality_contract",
        "omitted_context": ["raw_events", "transcript", "runner_result"],
    }


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _created_task_summary 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 created task summary 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
def _created_task_summary(task: Any, order: SubagentWorkOrder) -> dict[str, str]:
    return {
        "task_id": str(task.id),
        "case_id": order.case_id,
        "role": order.role,
        "status": str(getattr(task, "status", "")),
        "verification_status": str(getattr(task, "verification_status", "")),
    }
