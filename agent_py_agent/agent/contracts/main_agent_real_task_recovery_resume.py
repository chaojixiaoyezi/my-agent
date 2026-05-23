# LLM: Real-task recovery resume is a schema facade over the unified resume reader.
# 模块用途: 保留 real_task 续跑入口；恢复包读取、case 选择和 attempt 路径只维护一套。

from __future__ import annotations

from pathlib import Path

from .main_agent_real_task_recovery_packet import SCHEMA_VERSION
from .main_agent_task_recovery_resume import (
    case_ids_for_recovery_request_for_schema,
    recovery_case_id_for_schema,
    recovery_delivery_contract_payload_for_schema,
    recovery_packet_payload_for_schema,
    resume_attempt_paths,
)


def recovery_packet_payload(packet_path: Path | None) -> dict[str, object]:
    return recovery_packet_payload_for_schema(packet_path, expected_schema_version=SCHEMA_VERSION)


def recovery_case_id(packet_path: Path | None) -> str:
    return recovery_case_id_for_schema(packet_path, expected_schema_version=SCHEMA_VERSION)


def case_ids_for_recovery_request(
    requested_case_ids: tuple[str, ...],
    packet_path: Path | None,
) -> tuple[str, ...]:
    return case_ids_for_recovery_request_for_schema(
        requested_case_ids,
        packet_path,
        expected_schema_version=SCHEMA_VERSION,
    )


def recovery_delivery_contract_payload(
    packet_path: Path | None,
    *,
    workspace: Path,
) -> dict[str, object]:
    return recovery_delivery_contract_payload_for_schema(
        packet_path,
        workspace=workspace,
        expected_schema_version=SCHEMA_VERSION,
    )


__all__ = [
    "case_ids_for_recovery_request",
    "recovery_case_id",
    "recovery_delivery_contract_payload",
    "recovery_packet_payload",
    "resume_attempt_paths",
]
