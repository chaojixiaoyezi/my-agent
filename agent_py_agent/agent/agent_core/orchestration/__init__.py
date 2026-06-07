"""Orchestration helpers shared by main-agent control-plane tools."""

from .tool_grants import CODING_SUBAGENT_TOOLS, READ_ONLY_SUBAGENT_TOOLS, subagent_allowed_tools
from .tool_specs import (
    build_create_subagents_spec,
    build_dispatch_subagents_spec,
    build_inspect_agent_tree_spec,
    build_raise_event_spec,
    build_schedule_child_subagents_spec,
    build_task_progress_spec,
)

__all__ = [
    "CODING_SUBAGENT_TOOLS",
    "READ_ONLY_SUBAGENT_TOOLS",
    "build_create_subagents_spec",
    "build_dispatch_subagents_spec",
    "build_inspect_agent_tree_spec",
    "build_raise_event_spec",
    "build_schedule_child_subagents_spec",
    "build_task_progress_spec",
    "subagent_allowed_tools",
]
