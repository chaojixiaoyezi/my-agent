"""Web 仪表盘子系统(Phase 3):自建 stdlib http.server 只读仪表盘,零外部依赖、fail-closed。"""

from __future__ import annotations

from agent_py_agent.agent.web.dashboard import (
    DashboardConfig,
    DashboardServer,
    DashboardSources,
    build_dashboard_payload,
)

__all__ = [
    "DashboardConfig",
    "DashboardServer",
    "DashboardSources",
    "build_dashboard_payload",
]
