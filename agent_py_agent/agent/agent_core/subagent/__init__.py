from __future__ import annotations

from .params import (
    SpawnSubagentsParams,
    SubagentFinalizeParams,
    SubagentProbeParams,
    SubagentRunFailureParams,
    SubagentRunParams,
    spawn_subagents_params,
    subagent_run_params,
)

__all__ = [
    "SpawnSubagentsParams",
    "SubagentFinalizeParams",
    "SubagentLifecycleService",
    "SubagentProbeParams",
    "SubagentRunFailureParams",
    "SubagentRunParams",
    "config_workflow_dispatch_mode",
    "configured_subagent_allowed_tools",
    "spawn_subagents_params",
    "subagent_run_params",
]


def __getattr__(name: str):
    if name in {"SubagentLifecycleService", "config_workflow_dispatch_mode"}:
        from .lifecycle_service import SubagentLifecycleService, config_workflow_dispatch_mode

        values = {
            "SubagentLifecycleService": SubagentLifecycleService,
            "config_workflow_dispatch_mode": config_workflow_dispatch_mode,
        }
        return values[name]
    if name == "configured_subagent_allowed_tools":
        from .spawn_flow import configured_subagent_allowed_tools

        return configured_subagent_allowed_tools
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
