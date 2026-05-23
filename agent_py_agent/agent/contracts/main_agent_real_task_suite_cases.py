# LLM: Real-task cases reuse the unified main-agent task case contracts.
# 模块用途: 保留 real_task 默认案例入口；案例正文和机器合同只维护一份。

from __future__ import annotations

from .main_agent_real_task_suite import MainAgentRealTaskCase
from .main_agent_task_suite_cases import default_main_agent_task_cases

_REAL_TASK_CASE_IDS = {
    "furniture_homepage_html",
    "shopping_site_flow",
    "github_weekly_star_growth_xlsx",
    "research_documents_translation_pdf",
}


# LLM: default_main_agent_real_task_cases selects real-track cases by structured case_id.
# 函数用途: 从统一案例集筛选 real_task 默认任务，不解析标题或 prompt 文本。
def default_main_agent_real_task_cases() -> list[MainAgentRealTaskCase]:
    return [
        case
        for case in default_main_agent_task_cases()
        if case.case_id in _REAL_TASK_CASE_IDS
    ]


__all__ = ["default_main_agent_real_task_cases"]
