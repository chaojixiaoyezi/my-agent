# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

"""本包把日志分析工单拆成 planning（数据结构和规划）与 creation（任务创建）两个子模块。

新手说明:
从外部 import 时路径不变，仍然可以用 from ...dispatch.work_orders import plan_case_subagent_work_orders。
__init__.py 把两个子模块的公开名称全部 re-export，保证拆包后上游代码不需要修改。
"""

from .creation import (
    SubAgentTaskCreator,
    SubagentWorkOrderCreationResult,
    create_subagent_tasks_from_work_order_plan,
)
from .planning import (
    DEFAULT_REVIEWER_TOOLS,
    NO_EVIDENCE_ISSUE,
    PARENT_FINAL_GATE,
    PLAN_NOT_READY_ISSUE,
    LogAnalysisWorkOrderPlan,
    SubagentWorkOrder,
    build_log_analysis_work_orders,
    plan_case_subagent_work_orders,
)

__all__ = [
    "DEFAULT_REVIEWER_TOOLS",
    "LogAnalysisWorkOrderPlan",
    "NO_EVIDENCE_ISSUE",
    "PARENT_FINAL_GATE",
    "PLAN_NOT_READY_ISSUE",
    "SubAgentTaskCreator",
    "SubagentWorkOrder",
    "SubagentWorkOrderCreationResult",
    "build_log_analysis_work_orders",
    "create_subagent_tasks_from_work_order_plan",
    "plan_case_subagent_work_orders",
]
