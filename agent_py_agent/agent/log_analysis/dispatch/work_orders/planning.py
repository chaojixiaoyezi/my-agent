"""LLM: 本模块定义日志分析工单的数据结构和规划逻辑，生成证据受限、不可自验收的两阶段工单。

新手说明:
这里包含 SubagentWorkOrder（单张工单卡片）、LogAnalysisWorkOrderPlan（一个 case 的完整计划）
以及 plan_case_subagent_work_orders（规划入口函数）。
私有辅助函数 _get、_case_id、_merge_unique、_acceptance_checks 负责字段读取、ID 兼容、
证据合并和验收检查清单组装。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from ...agents.contracts import (
    DEFAULT_ACCEPTANCE_CHECKS,
    DEFAULT_ANALYST_TOOLS,
    normalize_evidence_refs,
)
from ...agents.summaries import render_case_summary, summarize_case

DEFAULT_REVIEWER_TOOLS = ["evidence_read"]

PARENT_FINAL_GATE = "parent_session_final_approval_required"
NO_EVIDENCE_ISSUE = "case has no evidence_refs; analyst/reviewer work orders are not ready"
PLAN_NOT_READY_ISSUE = "work-order plan is not ready; refusing to create subagent tasks"


@dataclass
class SubagentWorkOrder:
    """LLM: 它承载 analyst 或 reviewer 的目标、证据 refs、工具白名单、验收条件和父级最终验收门。

    新手说明:
    这像一张任务卡片，告诉某个角色"要做什么、能看哪些证据、能用哪些工具、完成后按什么标准检查"。
    dry_run 表示当前只是计划，ready 表示证据和前置条件是否足够；cannot_self_accept 和 parent_final_gate 防止子代理自己宣布最终通过。

    字段说明:
    role: 子代理角色名，例如 analyst 或 reviewer；不同角色拿到的工具和任务重点不同。
    case_id: 日志分析 case 的唯一标识，方便以后把任务、证据、报告和审计记录关联起来。
    goal: 给子代理看的任务目标，应该描述"要产出什么"，而不是只写"处理一下"。
    mode: 派工模式，目前通常是 manual，表示需要父会话控制和确认。
    dry_run: True 表示只是计划或预览；False 表示这张工单来自真实创建流程。
    ready: True 表示证据和前置条件足够，可以进入创建任务阶段；False 表示不能派发。
    allowed_tools: 子代理允许使用的工具白名单，比如 evidence_read；没列出的工具不应该使用。
    evidence_refs: 子代理可以依据的证据引用列表；这是安全分析的边界，不是原始日志全文。
    context: 给子代理的上下文包来源，通常包含 case_summary、route_summary、质量契约等。
    acceptance_checks: 父级或 reviewer 要检查的清单，子代理不能跳过。
    cannot_self_accept: True 表示子代理不能自己宣布最终通过。
    parent_final_gate: 父级最终验收门的名字，用来提醒所有角色最终裁决在父会话。
    issues: 已知阻塞问题，例如没有证据引用。
    risks: 还没阻塞但需要注意的风险，例如证据不足可能导致 unsupported analysis。
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

        参数说明:
        这个方法没有输入参数，只读取当前对象自己的字段。

        返回说明:
        返回 dict，里面包含 role、case_id、goal、allowed_tools、evidence_refs 等全部字段。
        """
        return asdict(self)


@dataclass
class LogAnalysisWorkOrderPlan:
    """LLM: 它聚合 case 的两张工单和整体 ready/issue/risk 状态，默认只是描述性 dry-run。

    新手说明:
    一个 case 通常需要先分析、再复核，所以计划里会有 analyst 和 reviewer 两张工单。
    计划本身不会创建任务、不会调用模型、不会改队列；它让父流程先看到"准备好了没有"和"风险在哪里"。

    字段说明:
    case_id: 这份计划对应哪个日志分析 case。
    ready: 整体计划是否可以进入真实创建任务阶段；只要缺关键证据就应该是 False。
    dry_run: True 表示计划只是预览，不能理解成任务已经派出。
    mode: 当前派工模式，和每张 SubagentWorkOrder 的 mode 保持一致。
    work_orders: 计划包含的具体工单列表，目前通常是 analyst 和 reviewer 两张。
    issues: 阻止计划进入下一步的问题，会同步到结果里给父会话看。
    risks: 不一定阻止创建、但需要父会话知道的风险。
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

        参数说明:
        这个方法没有输入参数，只读取当前计划对象自己的字段。

        返回说明:
        返回 dict，其中 work_orders 会是由每张工单 to_dict() 生成的列表。
        """
        payload = asdict(self)
        payload["work_orders"] = [order.to_dict() for order in self.work_orders]
        return payload


def _get(source: Any, key: str, default: Any = None) -> Any:
    """LLM: 让规划函数能读取 dict case、dataclass case 或普通对象 case 的同名字段。

    新手说明:
    有些调用方传字典，有些传对象；字典用 source["key"] 或 get，对象用 source.key。
    这个小函数把两种写法统一起来，找不到时返回 default，避免主流程里到处写判断。

    参数说明:
    source: 要读取的对象，可以是 dict、dataclass 实例或普通 Python 对象。
    key: 想读取的字段名，例如 "case_id"、"evidence_refs"。
    default: 找不到字段时返回的备用值，默认是 None。

    返回说明:
    如果 source 是 Mapping，就返回 source.get(key, default)；否则返回 getattr(source, key, default)。
    """
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _case_id(case: Any, summary: Mapping[str, Any]) -> str:
    """LLM: 它按优先级补齐工单必须携带的 case 标识，缺失时降级为 unknown-case。

    新手说明:
    工单必须知道自己属于哪个 case，但不同输入可能把编号放在 case_id、id 或 summary["case"] 里。
    这里集中处理这些兼容路径，让后面的工单创建不用重复猜字段名。

    参数说明:
    case: 原始 case 对象，可能是 dict，也可能是带属性的对象。
    summary: summarize_case(case).to_dict() 的结果，是已经整理过的摘要字典。

    返回说明:
    返回字符串形式的 case id。优先使用 case.case_id / case["case_id"]，
    其次使用 case.id / case["id"]，再看 summary["case"]["case_id"]。
    都没有时返回 "unknown-case"，保证后续工单仍有可显示的标识。
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

    参数说明:
    *values: 任意数量的证据引用来源。每个来源可以是字符串、列表、None，或 normalize_evidence_refs 支持的格式。
    传多个来源是为了把"用户显式给的 evidence_refs""case 自带的 evidence_refs""summary 里的 evidence"合并起来。

    返回说明:
    返回去重后的证据引用列表，顺序按第一次出现的位置保留。
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

    参数说明:
    quality_contract: 上游传来的质量契约字典，可以包含 acceptance_checks。
    如果是 None，就只使用 DEFAULT_ACCEPTANCE_CHECKS。

    返回说明:
    返回最终检查清单。它总是先包含默认检查项，再追加 quality_contract.acceptance_checks 里的非空新项目。
    """
    checks = list(DEFAULT_ACCEPTANCE_CHECKS)
    if quality_contract:
        for item in quality_contract.get("acceptance_checks", []):
            text = str(item or "").strip()
            if text and text not in checks:
                checks.append(text)
    return checks


def _build_work_order_context(
    summary: dict[str, Any],
    refs: list[str],
    route: dict[str, Any],
    quality_contract: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the common context for analyst and reviewer work orders."""
    return {
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


@dataclass(frozen=True)
class CreateWorkOrdersParams:
    """Params bundle for _create_work_orders."""
    case_id: str
    common_context: dict[str, Any]
    refs: list[str]
    checks: list[str]
    issues: list[str]
    risks: list[str]
    mode: str
    dry_run: bool
    ready: bool


def _create_work_orders(*, params: CreateWorkOrdersParams) -> tuple[SubagentWorkOrder, SubagentWorkOrder]:
    """Create analyst and reviewer work orders from common data."""
    case_id = params.case_id
    common_context = params.common_context
    refs = params.refs
    checks = params.checks
    issues = params.issues
    risks = params.risks
    mode = params.mode
    dry_run = params.dry_run
    ready = params.ready
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
    return analyst, reviewer


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

    参数说明:
    case: 日志分析 case。可以是 dict 或对象；函数会从里面提取 case_id、evidence_refs、route 等摘要信息。
    evidence_refs: 调用方显式指定的证据引用。它会和 case 里的证据合并；显式传入适合父会话收窄证据范围。
    route_summary: 路由摘要，例如攻击入口、时间线、下一步查询建议。没有传时会尝试使用 case summary 里的 route。
    quality_contract: 本次派工的质量要求，例如额外 acceptance_checks、must_check、evidence_required。
    mode: 派工模式字符串，目前通常是 "manual"，表示由父会话手动控制。
    dry_run: 是否只是预览。True 表示只生成计划；False 也不会自动创建任务，只会写入工单状态供上层区分。

    返回说明:
    返回 LogAnalysisWorkOrderPlan。ready=True 时才适合交给 create_subagent_tasks_from_work_order_plan(apply=True)。
    ready=False 时必须先处理 issues/risks，尤其是缺少 evidence_refs 的情况。
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

    common_context = _build_work_order_context(summary, refs, route, quality_contract)
    analyst, reviewer = _create_work_orders(
        params=CreateWorkOrdersParams(
            case_id=case_id,
            common_context=common_context,
            refs=refs,
            checks=checks,
            issues=issues,
            risks=risks,
            mode=mode,
            dry_run=dry_run,
            ready=ready,
        )
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
