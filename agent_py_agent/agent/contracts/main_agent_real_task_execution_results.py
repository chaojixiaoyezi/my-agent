# LLM: Real-task execution results reuse the unified task result helpers.
# 模块用途: 保留 real_task recovery schema；验收、issue 和事件汇总只维护一套。

from __future__ import annotations

from .main_agent_real_task_recovery_packet import (
    RealTaskRecoveryPacketRequest,
    real_task_recovery_refs,
    write_real_task_recovery_packet,
)
from .main_agent_task_execution_results import (
    activity_timeout_seconds,
    append_acceptance_event,
    case_issues,
    case_result,
    timeout_issues,
    validate_case_artifacts,
    write_recovery_packet_ref_with_writer,
)
from .main_agent_task_execution_state import CaseResultBundle


def write_recovery_packet_ref(bundle: CaseResultBundle) -> str:
    return write_recovery_packet_ref_with_writer(
        bundle,
        packet_request_type=RealTaskRecoveryPacketRequest,
        refs_func=real_task_recovery_refs,
        writer=write_real_task_recovery_packet,
    )


__all__ = [
    "activity_timeout_seconds",
    "append_acceptance_event",
    "case_issues",
    "case_result",
    "timeout_issues",
    "validate_case_artifacts",
    "write_recovery_packet_ref",
]
