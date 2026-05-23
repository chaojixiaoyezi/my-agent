# LLM: Real-task recovery packets share the unified task packet structure.
# 模块用途: 保留 real_task schema 和函数名，恢复包生成逻辑只维护一份。

from __future__ import annotations

from pathlib import Path

from .main_agent_task_recovery_packet import (
    TaskRunRecoveryPacketRequest as RealTaskRecoveryPacketRequest,
)
from .main_agent_task_recovery_packet import (
    recovery_refs,
    write_recovery_packet,
)
from .main_agent_task_suite import MainAgentTaskCasePlan

SCHEMA_VERSION = "main-agent-real-task-recovery.v1"


# LLM: write_real_task_recovery_packet writes the shared packet with real_task schema.
# 函数用途: 复用统一恢复包结构，同时保持旧工具识别的 real_task schema_version。
def write_real_task_recovery_packet(request: RealTaskRecoveryPacketRequest) -> str:
    return write_recovery_packet(request, schema_version=SCHEMA_VERSION)


# LLM: real_task_recovery_refs delegates to structured refs from the unified packet module.
# 函数用途: 收集续跑锚点，不从日志或 prompt 文本推断任务事实。
def real_task_recovery_refs(
    paths: dict[str, Path],
    case: MainAgentTaskCasePlan,
) -> dict[str, Path | str]:
    return recovery_refs(paths, case)


__all__ = [
    "RealTaskRecoveryPacketRequest",
    "SCHEMA_VERSION",
    "real_task_recovery_refs",
    "write_real_task_recovery_packet",
]
