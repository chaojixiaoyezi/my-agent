# LLM: Main-agent task runtime adapter factories keep public wrappers thin.
# 模块用途: 集中构造 task/real_task adapter；旧入口只选择轨道，不复制字段清单。

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .main_agent_real_task_execution_files import (
    append_event as real_append_event,
)
from .main_agent_real_task_execution_files import (
    case_paths as real_case_paths,
)
from .main_agent_real_task_execution_files import (
    command_for_case as real_command_for_case,
)
from .main_agent_real_task_execution_files import (
    execution_root as real_execution_root,
)
from .main_agent_real_task_execution_files import (
    package_root as real_package_root,
)
from .main_agent_real_task_execution_files import (
    rel as real_rel,
)
from .main_agent_real_task_execution_files import (
    write_case_config as real_write_case_config,
)
from .main_agent_real_task_execution_files import (
    write_json as real_write_json,
)
from .main_agent_real_task_execution_models import (
    SCHEMA_VERSION as REAL_SCHEMA_VERSION,
)
from .main_agent_real_task_execution_models import (
    MainAgentRealTaskExecutionCaseResult,
    MainAgentRealTaskExecutionReport,
    MainAgentRealTaskExecutionRequest,
)
from .main_agent_real_task_execution_results import (
    activity_timeout_seconds as real_activity_timeout_seconds,
)
from .main_agent_real_task_execution_results import (
    append_acceptance_event as real_append_acceptance_event,
)
from .main_agent_real_task_execution_results import (
    case_issues as real_case_issues,
)
from .main_agent_real_task_execution_results import (
    case_result as real_case_result,
)
from .main_agent_real_task_execution_results import (
    timeout_issues as real_timeout_issues,
)
from .main_agent_real_task_execution_results import (
    validate_case_artifacts as real_validate_case_artifacts,
)
from .main_agent_real_task_execution_results import (
    write_recovery_packet_ref as real_write_recovery_packet_ref,
)
from .main_agent_real_task_execution_state import (
    CaseResultBundle as RealCaseResultBundle,
)
from .main_agent_real_task_execution_state import (
    CaseRuntime as RealCaseRuntime,
)
from .main_agent_real_task_recovery_resume import (
    case_ids_for_recovery_request as real_case_ids_for_recovery_request,
)
from .main_agent_real_task_recovery_resume import (
    resume_attempt_paths as real_resume_attempt_paths,
)
from .main_agent_real_task_subprocess import (
    RealTaskSubprocessRequest,
    RealTaskSubprocessResult,
    run_real_task_subprocess,
)
from .main_agent_real_task_suite import (
    MainAgentRealTaskSuiteRequest,
    plan_main_agent_real_task_suite,
)
from .main_agent_task_execution_files import (
    append_event as task_append_event,
)
from .main_agent_task_execution_files import (
    case_paths as task_case_paths,
)
from .main_agent_task_execution_files import (
    command_for_case as task_command_for_case,
)
from .main_agent_task_execution_files import (
    execution_root as task_execution_root,
)
from .main_agent_task_execution_files import (
    package_root as task_package_root,
)
from .main_agent_task_execution_files import (
    rel as task_rel,
)
from .main_agent_task_execution_files import (
    write_case_config as task_write_case_config,
)
from .main_agent_task_execution_files import (
    write_json as task_write_json,
)
from .main_agent_task_execution_models import (
    SCHEMA_VERSION as TASK_SCHEMA_VERSION,
)
from .main_agent_task_execution_models import (
    MainAgentTaskExecutionCaseResult,
    MainAgentTaskExecutionReport,
    MainAgentTaskExecutionRequest,
)
from .main_agent_task_execution_results import (
    activity_timeout_seconds as task_activity_timeout_seconds,
)
from .main_agent_task_execution_results import (
    append_acceptance_event as task_append_acceptance_event,
)
from .main_agent_task_execution_results import (
    case_issues as task_case_issues,
)
from .main_agent_task_execution_results import (
    case_result as task_case_result,
)
from .main_agent_task_execution_results import (
    timeout_issues as task_timeout_issues,
)
from .main_agent_task_execution_results import (
    validate_case_artifacts as task_validate_case_artifacts,
)
from .main_agent_task_execution_results import (
    write_recovery_packet_ref as task_write_recovery_packet_ref,
)
from .main_agent_task_execution_state import (
    CaseResultBundle as TaskCaseResultBundle,
)
from .main_agent_task_execution_state import (
    CaseRuntime as TaskCaseRuntime,
)
from .main_agent_task_recovery_resume import (
    case_ids_for_recovery_request as task_case_ids_for_recovery_request,
)
from .main_agent_task_recovery_resume import (
    resume_attempt_paths as task_resume_attempt_paths,
)
from .main_agent_task_runtime_adapter import MainAgentTaskRuntimeAdapter
from .main_agent_task_subprocess import (
    TaskRunSubprocessRequest,
    TaskRunSubprocessResult,
    run_task_subprocess,
)
from .main_agent_task_suite import MainAgentTaskSuiteRequest, plan_main_agent_task_suite


# LLM: task_runtime_adapter describes the generic task track as runtime facts.
# 函数用途: 构造通用任务 adapter；测试可传入 monkeypatch 后的 subprocess 函数。
def task_runtime_adapter(
    run_subprocess_func: Callable[[Any], Any] | None = None,
) -> MainAgentTaskRuntimeAdapter:
    return MainAgentTaskRuntimeAdapter(
        "task",
        TASK_SCHEMA_VERSION,
        "main_agent_task_suite",
        ("main_agent_real_task_execution",),
        MainAgentTaskExecutionRequest,
        MainAgentTaskExecutionReport,
        MainAgentTaskExecutionCaseResult,
        TaskCaseRuntime,
        TaskCaseResultBundle,
        MainAgentTaskSuiteRequest,
        plan_main_agent_task_suite,
        task_case_paths,
        task_execution_root,
        task_package_root,
        task_rel,
        task_write_json,
        task_write_case_config,
        task_command_for_case,
        task_append_event,
        TaskRunSubprocessRequest,
        TaskRunSubprocessResult,
        run_subprocess_func or run_task_subprocess,
        task_activity_timeout_seconds,
        task_validate_case_artifacts,
        task_append_acceptance_event,
        task_case_issues,
        task_case_result,
        task_timeout_issues,
        task_write_recovery_packet_ref,
        task_case_ids_for_recovery_request,
        task_resume_attempt_paths,
    )


# LLM: real_task_runtime_adapter describes the real_task track as runtime facts.
# 函数用途: 构造真实任务 adapter；测试可传入 monkeypatch 后的 subprocess 函数。
def real_task_runtime_adapter(
    run_subprocess_func: Callable[[Any], Any] | None = None,
) -> MainAgentTaskRuntimeAdapter:
    return MainAgentTaskRuntimeAdapter(
        "real_task",
        REAL_SCHEMA_VERSION,
        "main_agent_real_task_suite",
        ("main_agent_task_execution",),
        MainAgentRealTaskExecutionRequest,
        MainAgentRealTaskExecutionReport,
        MainAgentRealTaskExecutionCaseResult,
        RealCaseRuntime,
        RealCaseResultBundle,
        MainAgentRealTaskSuiteRequest,
        plan_main_agent_real_task_suite,
        real_case_paths,
        real_execution_root,
        real_package_root,
        real_rel,
        real_write_json,
        real_write_case_config,
        real_command_for_case,
        real_append_event,
        RealTaskSubprocessRequest,
        RealTaskSubprocessResult,
        run_subprocess_func or run_real_task_subprocess,
        real_activity_timeout_seconds,
        real_validate_case_artifacts,
        real_append_acceptance_event,
        real_case_issues,
        real_case_result,
        real_timeout_issues,
        real_write_recovery_packet_ref,
        real_case_ids_for_recovery_request,
        real_resume_attempt_paths,
    )


__all__ = ["real_task_runtime_adapter", "task_runtime_adapter"]
