# LLM: CLI scenario case definition; keep fixture flow and expected gateway/subagent behavior stable.
# 模块用途: 定义一类命令行情景测试，用来复现和验证端到端流程。

from __future__ import annotations

"""re-exports all public run_scenario_* functions from the scenario_cases package.

给人看的解释：
这个包把 scenario_cases.py 拆成了几个子模块，但对外保持一样的导入接口。
所有 run_scenario_* 函数都能从这里直接导入。
"""

from .gateway_cases import (
    run_scenario_gateway_delayed_response_case,
    run_scenario_gateway_restart_case,
    run_scenario_gateway_stale_lease_case,
)
from .gateway_cross_day_case import run_scenario_gateway_cross_day_resume_case
from .gateway_multi_worker_case import run_scenario_gateway_multi_worker_case
from .gateway_processing_case import run_scenario_gateway_processing_stop_case
from .real_model_multi_round_case import run_scenario_real_model_recovery_multi_round_case
from .real_model_recovery_case import run_scenario_real_model_recovery_case
from .repair_retry_cases import (
    ScenarioRetryBackend,
    ScenarioStructuredRepairBackend,
    run_scenario_runner_retry_case,
    run_scenario_structured_repair_case,
)
from .subagent_cases import run_scenario_parent_subagent_cross_day_resume_case
from .verification_case import run_scenario_verification_case

__all__ = [
    "ScenarioRetryBackend",
    "ScenarioStructuredRepairBackend",
    "run_scenario_gateway_cross_day_resume_case",
    "run_scenario_gateway_delayed_response_case",
    "run_scenario_gateway_multi_worker_case",
    "run_scenario_gateway_processing_stop_case",
    "run_scenario_gateway_restart_case",
    "run_scenario_gateway_stale_lease_case",
    "run_scenario_parent_subagent_cross_day_resume_case",
    "run_scenario_real_model_recovery_case",
    "run_scenario_real_model_recovery_multi_round_case",
    "run_scenario_runner_retry_case",
    "run_scenario_structured_repair_case",
    "run_scenario_verification_case",
]
