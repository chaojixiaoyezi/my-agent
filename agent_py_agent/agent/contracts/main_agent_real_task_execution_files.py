# LLM: Real-task execution files are compatibility wrappers over unified task helpers.
# 模块用途: 保留 real_task 执行目录和恢复 schema；命令、配置、事件和合同写入只维护一套。

from __future__ import annotations

from pathlib import Path

from .main_agent_real_task_recovery_reconcile import reconcile_recovery_open_write_sessions
from .main_agent_real_task_recovery_resume import recovery_delivery_contract_payload
from .main_agent_task_execution_files import (
    append_event,
    case_paths_for_root,
    command_for_case_with_options,
    default_config_text,
    delivery_contract_for_case_with_options,
    execution_root_for_name,
    isolated_runtime_config,
    package_root,
    prompt_for_case,
    recovery_attempt_inspection_budget,
    rel,
    without_config_keys,
    write_case_config,
    write_json,
)
from .main_agent_task_execution_models import MainAgentTaskExecutionRequest
from .main_agent_task_suite import MainAgentTaskCasePlan

_ROOT_NAME = "main_agent_real_task_execution"


def command_for_case(
    case: MainAgentTaskCasePlan,
    request: MainAgentTaskExecutionRequest,
    *,
    config_path: Path,
    workspace: Path,
    delivery_contract_path: Path | None = None,
) -> list[str]:
    return command_for_case_with_options(
        case,
        request,
        config_path=config_path,
        workspace=workspace,
        delivery_contract_path=delivery_contract_path,
        root_name=_ROOT_NAME,
        recovery_reconciler=reconcile_recovery_open_write_sessions,
        recovery_payload_reader=recovery_delivery_contract_payload,
    )


def delivery_contract_for_case(
    case: MainAgentTaskCasePlan,
    *,
    workspace: Path,
    recovery_packet_path: Path | None = None,
) -> dict[str, object]:
    return delivery_contract_for_case_with_options(
        case,
        workspace=workspace,
        recovery_packet_path=recovery_packet_path,
        track=(_ROOT_NAME, recovery_delivery_contract_payload),
    )


def case_paths(workspace: Path, case_id: str) -> dict[str, Path]:
    return case_paths_for_root(workspace, case_id, root_name=_ROOT_NAME)


def execution_root(workspace: Path) -> Path:
    return execution_root_for_name(workspace, root_name=_ROOT_NAME)
