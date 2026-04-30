"""LLM: 本模块把一个日志分析 case 转成 analyst/reviewer 工单，并用 dry-run/apply 边界防止子代理绕过父级最终验收。

新手说明:
这里不直接分析日志，也不直接跑子代理；它只负责准备“工作说明书”。
先生成可以检查的计划，确认有 evidence_refs 等必要证据后，才允许调用外部 subagent creator 创建任务。
这样拆开可以让系统在执行前看清目标、证据、工具权限、验收要求和风险。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Protocol

from ..agents.contracts import (
    DEFAULT_ACCEPTANCE_CHECKS,
    DEFAULT_ANALYST_TOOLS,
    normalize_evidence_refs,
)
from ..agents.summaries import render_case_summary, summarize_case


DEFAULT_REVIEWER_TOOLS = ["evidence_read"]

PARENT_FINAL_GATE = "parent_session_final_approval_required"
NO_EVIDENCE_ISSUE = "case has no evidence_refs; analyst/reviewer work orders are not ready"
PLAN_NOT_READY_ISSUE = "work-order plan is not ready; refusing to create subagent tasks"


class SubAgentTaskCreator(Protocol):
    """LLM: 这个 Protocol 说明本模块只依赖 create_run，不关心具体任务存储或调度实现。

    新手说明:
    Protocol 像一份“接口约定”：只要传进来的对象有 create_run 这个方法，就可以被这里使用。
    这样测试时可以传假的对象，真实运行时可以传真正的子代理管理器，函数本身不用知道细节。
    """

    def create_run(self, **kwargs: Any) -> Any:
        """LLM: create_subagent_tasks_from_work_order_plan 通过它把已审核的工单落成 SubAgentTask。

        新手说明:
        输入是一组关键字参数，例如 goal、role、allowed_tools 和 context_packs。
        输出通常是任务对象，至少要能读到 id；本模块不会启动任务，只保存创建结果。
        """
        ...


@dataclass
class SubagentWorkOrder:
    """LLM: 它承载 analyst 或 reviewer 的目标、证据 refs、工具白名单、验收条件和父级最终验收门。

    新手说明:
    这像一张任务卡片，告诉某个角色“要做什么、能看哪些证据、能用哪些工具、完成后按什么标准检查”。
    dry_run 表示当前只是计划，ready 表示证据和前置条件是否足够；cannot_self_accept 和 parent_final_gate 防止子代理自己宣布最终通过。
    """

    role: str
    case_id: str
    goal: str
    mode: str = "manual"
    dry_run: bool = True
    ready: bool = False
    allowed_tools: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    acceptance_checks: list[str] = field(default_factory=list)
    cannot_self_accept: bool = True
    parent_final_gate: str = PARENT_FINAL_GATE
    issues: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """LLM: 为 API 返回、测试断言和审计日志提供稳定的可序列化结构。

        新手说明:
        dataclass 对象读起来方便，但传给前端、日志或测试时字典更通用。
        这个方法不改变工单内容，只把字段展开成 dict。
        """
        return asdict(self)


@dataclass
class LogAnalysisWorkOrderPlan:
    """LLM: 它聚合 case 的两张工单和整体 ready/issue/risk 状态，默认只是描述性 dry-run。

    新手说明:
    一个 case 通常需要先分析、再复核，所以计划里会有 analyst 和 reviewer 两张工单。
    计划本身不会创建任务、不会调用模型、不会改队列；它让父流程先看到“准备好了没有”和“风险在哪里”。
    """

    case_id: str
    ready: bool
    dry_run: bool = True
    mode: str = "manual"
    work_orders: list[SubagentWorkOrder] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """LLM: 输出完整的计划快照，便于调用方检查 dry-run 结果或写入审计记录。

        新手说明:
        plan 里面套着 work_orders，直接 asdict 虽然也能展开，但这里显式调用每张工单的 to_dict。
        这样以后工单序列化逻辑变复杂时，计划输出仍然走同一条路径。
        """
        payload = asdict(self)
        payload["work_orders"] = [order.to_dict() for order in self.work_orders]
        return payload


@dataclass
class SubagentWorkOrderCreationResult:
    """LLM: 它记录 apply/dry-run 模式、创建出的 task ids，以及拒绝创建时的 issue/risk。

    新手说明:
    创建任务是有开关的：apply=False 只返回“如果创建会怎样”，apply=True 才真的调用 create_run。
    这个结果对象把是否 ready、创建了哪些任务、为什么没创建等信息集中返回，方便上层展示或测试。
    """

    case_id: str
    ready: bool
    apply: bool
    dry_run: bool
    mode: str
    created: list[dict[str, Any]] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """LLM: 为调用方提供可序列化的 task_ids、created、issues 和 risks。

        新手说明:
        这一步不再创建任何东西，只是把结果包装成更容易打印、返回 JSON 或写测试断言的格式。
        """
        return asdict(self)


def _get(source: Any, key: str, default: Any = None) -> Any:
    """LLM: 让规划函数能读取 dict case、dataclass case 或普通对象 case 的同名字段。

    新手说明:
    有些调用方传字典，有些传对象；字典用 source["key"] 或 get，对象用 source.key。
    这个小函数把两种写法统一起来，找不到时返回 default，避免主流程里到处写判断。
    """
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _case_id(case: Any, summary: Mapping[str, Any]) -> str:
    """LLM: 它按优先级补齐工单必须携带的 case 标识，缺失时降级为 unknown-case。

    新手说明:
    工单必须知道自己属于哪个 case，但不同输入可能把编号放在 case_id、id 或 summary["case"] 里。
    这里集中处理这些兼容路径，让后面的工单创建不用重复猜字段名。
    """
    summary_case = summary.get("case", {})
    if not isinstance(summary_case, Mapping):
        summary_case = {}
    return str(_get(case, "case_id") or _get(case, "id") or summary_case.get("case_id") or "unknown-case")


def _merge_unique(*values: Any) -> list[str]:
    """LLM: 它把显式参数、case 字段和 summary 里的证据引用归一成同一份边界列表。

    新手说明:
    证据引用可能来自好几个地方，而且格式可能不完全一样。
    这个函数会先调用 normalize_evidence_refs 做清洗，再按第一次出现的顺序去重，避免同一证据重复塞进工单。
    """
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in normalize_evidence_refs(value):
            if item not in seen:
                output.append(item)
                seen.add(item)
    return output


def _acceptance_checks(quality_contract: Mapping[str, Any] | None) -> list[str]:
    """LLM: 它确保所有工单至少包含默认质量门，并保留调用方补充的检查要求。

    新手说明:
    默认检查项像最低标准，quality_contract 里可以再加本次 case 特有的要求。
    函数会过滤空字符串并去重，避免验收列表里出现无意义或重复的项目。
    """
    checks = list(DEFAULT_ACCEPTANCE_CHECKS)
    if quality_contract:
        for item in quality_contract.get("acceptance_checks", []):
            text = str(item or "").strip()
            if text and text not in checks:
                checks.append(text)
    return checks


def plan_case_subagent_work_orders(
    case: Any,
    *,
    evidence_refs: Any = None,
    route_summary: Mapping[str, Any] | None = None,
    quality_contract: Mapping[str, Any] | None = None,
    mode: str = "manual",
    dry_run: bool = True,
) -> LogAnalysisWorkOrderPlan:
    """LLM: 这是 LOG dispatch 的主要规划入口，生成证据受限、不可自验收、受父级最终门控制的两阶段工单。

    新手说明:
    输入是一个 case，以及可选的证据 refs、路由摘要和质量契约；输出是 LogAnalysisWorkOrderPlan。
    如果没有 evidence_refs，计划会标记为 not ready 并写入 issue/risk，因为没有证据就派发分析会产生没有依据的结论。
    dry_run 参数会写进工单，提醒调用方当前只是规划；真正创建任务要走 create_subagent_tasks_from_work_order_plan。
    """

    summary_obj = summarize_case(case)
    summary = summary_obj.to_dict()
    case_id = _case_id(case, summary)
    route = dict(route_summary or summary.get("route") or {})
    refs = _merge_unique(evidence_refs, _get(case, "evidence_refs") or _get(case, "evidence"), summary.get("evidence"))
    checks = _acceptance_checks(quality_contract)

    issues: list[str] = []
    risks: list[str] = []
    ready = bool(refs)
    if not ready:
        issues.append(NO_EVIDENCE_ISSUE)
        risks.append("dispatching without evidence_refs would invite unsupported analysis")

    # context_pack 后续会交给子代理；这里只放摘要和引用，避免把原始日志或 runner 结果整包塞进去。
    common_context = {
        "case_summary": render_case_summary(
            {
                "case": summary.get("case", {}),
                "evidence": refs,
                "route": route,
            }
        ),
        "route_summary": route,
        "quality_contract": dict(quality_contract or {}),
    }

    analyst = SubagentWorkOrder(
        role="analyst",
        case_id=case_id,
        goal="Prepare an evidence-backed log analysis report for reviewer gate.",
        mode=mode,
        dry_run=dry_run,
        ready=ready,
        allowed_tools=list(DEFAULT_ANALYST_TOOLS),
        evidence_refs=list(refs),
        context={
            **common_context,
            "handoff_contract": "AnalystInput",
            "expected_output": "AnalystReport",
        },
        acceptance_checks=list(checks),
        issues=list(issues),
        risks=list(risks),
    )
    reviewer = SubagentWorkOrder(
        role="reviewer",
        case_id=case_id,
        goal="Review the analyst report against evidence boundaries before parent final approval.",
        mode=mode,
        dry_run=dry_run,
        ready=ready,
        allowed_tools=list(DEFAULT_REVIEWER_TOOLS),
        evidence_refs=list(refs),
        context={
            **common_context,
            "handoff_contract": "ReviewerInput",
            "expected_input": "AnalystReport from analyst work order",
            "expected_output": "ReviewerDecision",
        },
        acceptance_checks=list(checks),
        issues=list(issues),
        risks=list(risks),
    )

    return LogAnalysisWorkOrderPlan(
        case_id=case_id,
        ready=ready,
        dry_run=dry_run,
        mode=mode,
        work_orders=[analyst, reviewer],
        issues=issues,
        risks=risks,
    )


build_log_analysis_work_orders = plan_case_subagent_work_orders


def _work_order_quality_contract(order: SubagentWorkOrder) -> dict[str, Any]:
    """LLM: 它继承上游 quality_contract，并强制补上 evidence_required、cannot_self_accept 和 parent_final_gate。

    新手说明:
    子代理需要知道“必须看哪些证据、按哪些标准检查、谁有最终决定权”。
    这里把工单自身的证据和验收项并入契约，同时固定最终验收属于父级，避免 analyst/reviewer 自己给自己盖章。
    """
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
        "final_judge": "parent_final_gate",
        "cannot_self_accept": True,
        "parent_final_gate": True,
    }


def _work_order_context_pack(order: SubagentWorkOrder) -> dict[str, Any]:
    """LLM: 它把工单压缩成 role/case/evidence/route/quality 等安全字段，避免泄露不该交给子代理的原始上下文。

    新手说明:
    子代理不需要拿到整个 case 的所有数据，只需要完成任务所需的摘要、证据引用和质量要求。
    这样上下文更小，也更容易控制边界：它知道可以依据哪些 evidence_refs，但不能凭空扩展范围。
    """
    return {
        "name": "log-analysis-work-order",
        "case_id": order.case_id,
        "role": order.role,
        "evidence_refs": list(order.evidence_refs),
        "route_summary": dict(order.context.get("route_summary") or {}),
        "quality_contract": _work_order_quality_contract(order),
        "cannot_self_accept": order.cannot_self_accept,
        "parent_final_gate": order.parent_final_gate,
        "case_summary": order.context.get("case_summary", ""),
        "handoff_contract": order.context.get("handoff_contract", ""),
        "expected_input": order.context.get("expected_input", ""),
        "expected_output": order.context.get("expected_output", ""),
    }


def _work_order_plan_steps(order: SubagentWorkOrder) -> list[str]:
    """LLM: 它把工单职责转成 SubAgentTask.plan，并为 reviewer 额外加入证据边界复核步骤。

    新手说明:
    任务系统通常需要一个步骤列表，告诉子代理先看上下文、再按工具和证据边界产出结果。
    reviewer 的风险点不同，它要检查 analyst 报告有没有越过证据，所以这里给 reviewer 多加一步。
    """
    steps = [
        f"Read the focused context pack for case {order.case_id}.",
        "Use only allowed tools and retained evidence_refs.",
        "Produce the role-specific contract output without claiming final acceptance.",
        "Leave final approval to the parent session gate.",
    ]
    if order.role == "reviewer":
        steps.insert(1, "Check the analyst report against the evidence boundary before any approval recommendation.")
    return steps


def create_subagent_tasks_from_work_order_plan(
    subagents: SubAgentTaskCreator,
    plan: LogAnalysisWorkOrderPlan,
    *,
    apply: bool = False,
    parent_id: str = "",
    root_id: str = "",
    final_owner: str = "parent",
) -> SubagentWorkOrderCreationResult:
    """LLM: 这是唯一会调用 subagents.create_run 的入口，但只有 apply=True 且 plan.ready 时才会创建任务。

    新手说明:
    输入是前一步生成的 plan 和一个会 create_run 的 subagents 对象；输出是创建结果。
    apply=False 时只返回 dry-run 结果，不会创建任务；plan 缺证据或 not ready 时也会拒绝创建，并记录 issue。
    即使创建成功，它也只是保存 SubAgentTask 记录，不启动 runner、模型或分析循环；最终验收仍由父级负责。
    """

    mode = "apply" if apply else "dry_run"
    issues = list(plan.issues)
    risks = list(plan.risks)
    if not plan.ready and PLAN_NOT_READY_ISSUE not in issues:
        issues.append(PLAN_NOT_READY_ISSUE)
    result = SubagentWorkOrderCreationResult(
        case_id=plan.case_id,
        ready=plan.ready,
        apply=apply,
        dry_run=not apply,
        mode=mode,
        issues=issues,
        risks=risks,
    )
    if not apply or not plan.ready:
        return result

    for order in plan.work_orders:
        if not order.ready:
            issue = f"{order.role} work order is not ready; skipped"
            if issue not in result.issues:
                result.issues.append(issue)
            continue
        quality_contract = _work_order_quality_contract(order)
        context_pack = _work_order_context_pack(order)
        task = subagents.create_run(
            goal=order.goal,
            thought=(
                f"Manual LOG {order.role} work order for {order.case_id}; "
                "stay evidence-bound and leave final acceptance to the parent."
            ),
            plan=_work_order_plan_steps(order),
            agent_name=f"log-{order.role}",
            role=order.role,
            parent_id=parent_id,
            root_id=root_id,
            allowed_tools=list(order.allowed_tools),
            owner=order.role,
            supervisor=parent_id or "parent",
            final_owner=final_owner,
            acceptance_checks=list(order.acceptance_checks),
            quality_contract=quality_contract,
            context_manifest={
                "task_pack_refs": [
                    f"log-analysis-case:{order.case_id}",
                    *[f"evidence:{ref}" for ref in order.evidence_refs],
                ],
                "role_pack": f"log-analysis-{order.role}",
                "quality_contract_ref": "context_packs[0].quality_contract",
                "omitted_context": ["raw_events", "transcript", "runner_result"],
            },
            context_packs=[context_pack],
        )
        result.task_ids.append(str(task.id))
        result.created.append(
            {
                "task_id": str(task.id),
                "case_id": order.case_id,
                "role": order.role,
                "status": getattr(task, "status", ""),
                "verification_status": getattr(task, "verification_status", ""),
            }
        )
    return result


__all__ = [
    "DEFAULT_REVIEWER_TOOLS",
    "LogAnalysisWorkOrderPlan",
    "NO_EVIDENCE_ISSUE",
    "PARENT_FINAL_GATE",
    "PLAN_NOT_READY_ISSUE",
    "SubagentWorkOrder",
    "SubagentWorkOrderCreationResult",
    "build_log_analysis_work_orders",
    "create_subagent_tasks_from_work_order_plan",
    "plan_case_subagent_work_orders",
]
