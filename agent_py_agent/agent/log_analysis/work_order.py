"""LogWorkOrder 到 SubAgentTask 的转换器。

负责把日志补查工单桥接到子代理执行系统，转换目标、时间窗口、查询限制和允许的工具。
"""
from dataclasses import dataclass, field
from typing import Any

from agent_py_agent.agent.subagents.models import SubAgentTask

from .models import LogWorkOrder, utc_now_iso


@dataclass
class WorkOrderToTaskConfig:
    """work_order 到 subagent 转换的配置。"""

    default_agent_name: str = "log_analyst"
    default_role: str = "log_analyst"
    quality_contract: dict[str, Any] = field(default_factory=dict)
    context_manifest: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.quality_contract:
            self.quality_contract = {
                "user_visible_goal": "完成日志补查并提交证据",
                "quality_bar": "必须有证据支持结论",
                "failure_conditions": ["无证据就提交结论", "超出查询限制"],
                "forbidden_delivery": ["猜测性的结论", "没有查询记录的分析"],
                "must_check": ["查询结果数", "时间窗覆盖", "证据完整性"],
                "evidence_required": ["查询记录", "结果样本", "时间戳"],
                "risk_report_required": "缺少的关键证据或数据源",
                "final_judge": "parent_final_gate",
            }
        if not self.context_manifest:
            self.context_manifest = {
                "core_pack_version": "log-analysis-first-loop-v1",
                "role_pack": "log_analyst",
                "evidence_budget": 1000,
                "query_limit": 100,
            }


def work_order_to_subagent_task(
    work_order: LogWorkOrder,
    config: WorkOrderToTaskConfig | None = None,
) -> SubAgentTask:
    """Convert a LogWorkOrder into a planned SubAgentTask."""
    if config is None:
        config = WorkOrderToTaskConfig()

    run_id = f"log-subagent-{work_order.work_order_id}"

    goal = _build_goal(work_order)

    thought = _build_thought(work_order)

    plan = _build_plan(work_order)

    allowed_tools = _build_allowed_tools(work_order)

    allowed_skills = []

    execution_context_json = _build_execution_context(
        work_order, config, allowed_tools, allowed_skills
    )

    task = SubAgentTask(
        id=run_id,
        goal=goal,
        thought=thought,
        plan=plan,
        agent_name=config.default_agent_name,
        role=config.default_role,
        allowed_skills=allowed_skills,
        allowed_tools=allowed_tools,
        created_at=utc_now_iso(),
        updated_at=utc_now_iso(),
        status="PLANNING",
        verification_status="UNVERIFIED",
        runner_attempts=0,
        execution_context_json=execution_context_json,
        # quality_contract 和 context_manifest 会从 execution_context 解析
        quality_contract=_quality_contract_from_dict(config.quality_contract),
        context_manifest=_context_manifest_from_dict(config.context_manifest),
    )

    return task


def _build_goal(work_order: LogWorkOrder) -> str:
    """构建任务目标。"""
    parts = [f"补查安全 case {work_order.case_id}"]

    if work_order.investigation_goal:
        parts.append(f"：{work_order.investigation_goal}")

    parts.append(
        f"（时间窗：{work_order.start_time} ~ {work_order.end_time}，"
        f"最多查询 {work_order.max_results} 条结果）"
    )

    return "".join(parts)


def _build_thought(work_order: LogWorkOrder) -> str:
    """构建任务思路。"""
    parts = [
        f"根据 case {work_order.case_id} 的补查目标，",
        "在指定时间窗内使用受控查询工具收集证据，",
        "确保查询结果数和证据数量不超过预算。",
    ]
    return "".join(parts)


def _build_plan(work_order: LogWorkOrder) -> list[str]:
    """构建执行计划。"""
    plan = [
        f"1. 分析补查目标：{work_order.investigation_goal}",
        f"2. 确定时间窗口：{work_order.start_time} 至 {work_order.end_time}",
        f"3. 使用受控查询工具，最多返回 {work_order.max_results} 条结果",
        f"4. 整理证据，确保不超过预算 {work_order.evidence_budget}",
        "5. 输出 [SUBAGENT_RESULT] 结构化结果，包含 evidence 和 gaps",
    ]
    return plan


def _build_allowed_tools(work_order: LogWorkOrder) -> list[str]:
    """构建允许的工具列表。"""
    # 第一版只支持 file_tail 模板
    tools = ["log_bounded_query"]

    # 如果工单指定了查询模板，额外添加对应的工具名
    for template in work_order.allowed_query_templates:
        tool_name = _template_to_tool_name(template)
        if tool_name and tool_name not in tools:
            tools.append(tool_name)

    return tools


def _template_to_tool_name(template: str) -> str | None:
    """把查询模板名映射到工具名。"""
    template_map = {
        "file_tail": "log_bounded_query",
        "grep_search": "log_bounded_query",
        "parquet_scan": "parquet_bounded_query",
        "sql_query": "sql_bounded_query",
    }
    return template_map.get(template)


def _build_execution_context(
    work_order: LogWorkOrder,
    config: WorkOrderToTaskConfig,
    allowed_tools: list[str],
    allowed_skills: list[str],
) -> str:
    """构建执行上下文 JSON。"""
    import json

    context = {
        "work_order_id": work_order.work_order_id,
        "case_id": work_order.case_id,
        "investigation_goal": work_order.investigation_goal,
        "time_window": {
            "start": work_order.start_time,
            "end": work_order.end_time,
        },
        "query_limits": {
            "max_results": work_order.max_results,
            "evidence_budget": work_order.evidence_budget,
            "allowed_templates": work_order.allowed_query_templates,
        },
        "allowed_tools": allowed_tools,
        "allowed_skills": allowed_skills,
        "quality_contract": config.quality_contract,
        "context_manifest": config.context_manifest,
        "instructions": [
            "必须使用受控查询工具，不允许直接读取大文件",
            "所有查询必须指定时间窗口，超出范围会被拒绝",
            "查询结果数超过 max_results 时会被截断",
            "必须提交证据列表和缺失的证据",
            "不能在没有证据的情况下得出结论",
        ],
    }
    return json.dumps(context, ensure_ascii=False, indent=2)


def _quality_contract_from_dict(d: dict[str, Any]) -> Any:
    """从字典构造质量契约。"""
    from agent_py_agent.agent.subagents.models import QualityContract

    return QualityContract(
        user_visible_goal=d.get("user_visible_goal", ""),
        benchmark_sample=d.get("benchmark_sample", ""),
        quality_bar=d.get("quality_bar", ""),
        failure_conditions=d.get("failure_conditions", []),
        forbidden_delivery=d.get("forbidden_delivery", []),
        must_check=d.get("must_check", []),
        sampling_plan=d.get("sampling_plan", []),
        evidence_required=d.get("evidence_required", []),
        risk_report_required=d.get("risk_report_required", ""),
        allowed_degradation=d.get("allowed_degradation", []),
        final_judge=d.get("final_judge", "parent_final_gate"),
    )


def _context_manifest_from_dict(d: dict[str, Any]) -> Any:
    """从字典构造上下文清单。"""
    from agent_py_agent.agent.subagents.models import ContextManifest

    return ContextManifest(
        core_pack_version=d.get("core_pack_version", ""),
        task_pack_refs=d.get("task_pack_refs", []),
        role_pack=d.get("role_pack", ""),
        required_read_paths=d.get("required_read_paths", []),
        quality_contract_ref=d.get("quality_contract_ref", ""),
        omitted_context=d.get("omitted_context", []),
        token_budget=d.get("token_budget", 0),
    )


__all__ = [
    "WorkOrderToTaskConfig",
    "work_order_to_subagent_task",
]
