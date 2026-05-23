# LLM: Real-task recovery reconciliation reuses the unified task reconciliation engine.
# 模块用途: 保留 real_task reconciliation schema；重复 open write-session 修复逻辑只维护一份。

from __future__ import annotations

from pathlib import Path

from .main_agent_real_task_recovery_packet import SCHEMA_VERSION
from .main_agent_task_recovery_reconcile import (
    reconcile_recovery_open_write_sessions_for_schema,
)


def reconcile_recovery_open_write_sessions(
    packet_path: Path | None,
    *,
    workspace: Path,
) -> dict[str, object]:
    return reconcile_recovery_open_write_sessions_for_schema(
        packet_path,
        workspace=workspace,
        packet_schema_version=SCHEMA_VERSION,
        reconcile_schema_version="real-task-recovery-reconcile.v1",
    )


__all__ = ["reconcile_recovery_open_write_sessions"]
