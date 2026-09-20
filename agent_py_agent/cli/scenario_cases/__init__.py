# LLM: 仅导出仍注册的 CLI 场景；后端替身由对应场景直接导入，不形成公开兼容入口。
# 模块用途: 汇集隔离诊断场景的命令入口，供 scenario 注册表使用。
from __future__ import annotations

"""Scenario case public entrypoint.

给人看的解释：
各 scenario 按领域放在子模块里；本包公开当前可运行的 case 函数。
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
from .runner_retry_case import run_scenario_runner_retry_case
from .subagent_cases import run_scenario_parent_subagent_cross_day_resume_case

__all__ = [
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
]
