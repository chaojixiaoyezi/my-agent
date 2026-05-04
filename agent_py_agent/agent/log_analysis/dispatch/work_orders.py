"""LLM: 本模块把一个日志分析 case 转成 analyst/reviewer 工单，并用 dry-run/apply 边界防止子代理绕过父级最终验收。

新手说明:
这里不直接分析日志，也不直接跑子代理；它只负责准备“工作说明书”。
先生成可以检查的计划，确认有 evidence_refs 等必要证据后，才允许调用外部 subagent creator 创建任务。
这样拆开可以让系统在执行前看清目标、证据、工具权限、验收要求和风险。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

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

    参数说明:
    这个类本身没有构造参数。它只描述“传进来的对象应该长什么样”。
    如果一个对象实现了 create_run(**kwargs)，它就满足这个接口。
    """

    def create_run(self, **kwargs: Any) -> Any:
        """LLM: create_subagent_tasks_from_work_order_plan 通过它把已审核的工单落成 SubAgentTask。

        新手说明:
        输入是一组关键字参数，例如 goal、role、allowed_tools 和 context_packs。
        输出通常是任务对象，至少要能读到 id；本模块不会启动任务，只保存创建结果。

        参数说明:
        **kwargs 是一包命名参数，具体字段由真实的 SubAgentManager.create_run 接收。
        常见字段包括 goal、thought、plan、agent_name、role、allowed_tools、acceptance_checks、
        quality_contract、context_manifest、context_packs 等。
        这里使用 **kwargs 是为了让本模块只依赖“能创建任务”这件事，而不绑定某个具体 manager 类。

        返回说明:
        返回值通常是 SubAgentTask。调用方至少会读取 task.id、task.status 和 task.verification_status。
        """
        ...


@dataclass
class SubagentWorkOrder:
    """LLM: 它承载 analyst 或 reviewer 的目标、证据 refs、工具白名单、验收条件和父级最终验收门。

    新手说明:
    这像一张任务卡片，告诉某个角色“要做什么、能看哪些证据、能用哪些工具、完成后按什么标准检查”。
    dry_run 表示当前只是计划，ready 表示证据和前置条件是否足够；cannot_self_accept 和 parent_final_gate 防止子代理自己宣布最终通过。

    字段说明:
    role: 子代理角色名，例如 analyst 或 reviewer；不同角色拿到的工具和任务重点不同。
    case_id: 日志分析 case 的唯一标识，方便以后把任务、证据、报告和审计记录关联起来。
    goal: 给子代理看的任务目标，应该描述“要产出什么”，而不是只写“处理一下”。
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
    计划本身不会创建任务、不会调用模型、不会改队列；它让父流程先看到“准备好了没有”和“风险在哪里”。

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


@dataclass
class SubagentWorkOrderCreationResult:
    """LLM: 它记录 apply/dry-run 模式、创建出的 task ids，以及拒绝创建时的 issue/risk。

    新手说明:
    创建任务是有开关的：apply=False 只返回“如果创建会怎样”，apply=True 才真的调用 create_run。
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
    risks: 从计划继承或创建阶段发现的风险。
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

        参数说明:
        这个方法没有输入参数，只读取当前结果对象自己的字段。

        返回说明:
        返回 dict，适合给 CLI、API、测试或审计日志使用。
        """
        return asdict(self)


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
    传多个来源是为了把“用户显式给的 evidence_refs”“case 自带的 evidence_refs”“summary 里的 evidence”合并起来。

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


def _create_work_orders(
    case_id: str,
    common_context: dict[str, Any],
    refs: list[str],
    checks: list[str],
    issues: list[str],
    risks: list[str],
    mode: str,
    dry_run: bool,
    ready: bool,
) -> tuple[SubagentWorkOrder, SubagentWorkOrder]:
    """Create analyst and reviewer work orders from common data."""
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
        case_id,
        common_context,
        refs,
        checks,
        issues,
        risks,
        mode,
        dry_run,
        ready,
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

    参数说明:
    order: 单张 SubagentWorkOrder。函数会读取 order.context["quality_contract"]、order.evidence_refs、
    order.acceptance_checks 和 order.goal。

    返回说明:
    返回 quality_contract 字典，后续会写进 SubAgentTask.quality_contract 和 context_pack。
    返回值会强制包含 cannot_self_accept=True、parent_final_gate=True、final_judge="parent_final_gate"。
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

    参数说明:
    order: 要转换的 SubagentWorkOrder。它的 role、case_id、evidence_refs、context 和验收门会被打包。

    返回说明:
    返回 context pack 字典。这个字典会放进 SubAgentTask.context_packs，
    给子代理运行时读取；它只包含摘要和引用，不包含原始日志全文。
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

    参数说明:
    order: 要生成步骤的工单。函数会使用 order.case_id 显示目标 case，并用 order.role 判断是否是 reviewer。

    返回说明:
    返回字符串列表，每一项是一条给 SubAgentTask.plan 的执行步骤。
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

    参数说明:
    subagents: 负责创建 SubAgentTask 的对象，通常是真实 SubAgentManager，也可以是测试替身。
    plan: plan_case_subagent_work_orders 生成的计划，里面包含 analyst/reviewer 工单和 ready 状态。
    apply: 是否真的创建任务。默认 False，表示只返回 dry-run 结果；只有 True 才会调用 subagents.create_run。
    parent_id: 父任务或父会话 id，会写入新任务的 parent_id 和 supervisor，方便追踪谁派的工。
    root_id: 整条任务树的根 id；如果为空，SubAgentManager 会为每个任务使用自己的 id 或默认规则。
    final_owner: 最终验收负责人，默认 "parent"，表示最后由父会话裁决。

    返回说明:
    返回 SubagentWorkOrderCreationResult。created 和 task_ids 只在 apply=True 且 plan.ready=True 时有内容。
    如果 apply=False、plan.ready=False 或某张 order.ready=False，结果会保留 issues/risks 来解释为什么没创建。
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
