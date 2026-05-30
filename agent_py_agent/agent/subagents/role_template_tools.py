# LLM: Broad role tool manifests are shared data, separate from template loading.
# 模块用途: 集中维护内置角色默认工具集合，避免模板注册和工具清单长期挤在同一个模块。

from __future__ import annotations

# LLM: Web roles expose only discovery and URL/API reading to avoid duplicate network entrypoints.
WEB_TOOLS = ["web_search", "web_fetch"]
READ_ONLY_TOOLS = ["list_files", "read_file", "search_text", "read_artifact", *WEB_TOOLS]
WORKER_READ_TOOLS = ["list_files", "read_file", "search_text", "read_artifact", *WEB_TOOLS]
ARTIFACT_BUILDER_TOOLS: list[str] = []
# LLM: Built-in roles now share the generic write surface instead of append/replace/builder tools.
WORKER_WRITE_TOOLS = ["write_file", "apply_patch"]
REPORT_WRITE_TOOLS = ["write_file", "apply_patch"]
SHELL_TOOL = "run_command"
CAPABILITY_REQUEST_TOOL = "capability_request"
MAIN_EVENT_TOOLS = ["raise_event"]
COLLABORATION_TOOLS = [
    "raise_collaboration",
    "inspect_collaboration",
    "submit_collaboration_result",
    "update_collaboration",
]
ROLE_BASE_TOOLS = [
    *READ_ONLY_TOOLS,
    *REPORT_WRITE_TOOLS,
    SHELL_TOOL,
    "inspect_agent_tree",
    *MAIN_EVENT_TOOLS,
    *COLLABORATION_TOOLS,
    CAPABILITY_REQUEST_TOOL,
]
COORDINATOR_TOOLS = [
    "schedule_child_subagents",
    "dispatch_subagents",
    # LLM: Coordinators can inspect status without entering dispatch, keeping "look only" separate from "advance work".
    "inspect_agent_tree",
    "send_guidance",
    *ROLE_BASE_TOOLS,
]

__all__ = [
    "ARTIFACT_BUILDER_TOOLS",
    "CAPABILITY_REQUEST_TOOL",
    "COLLABORATION_TOOLS",
    "COORDINATOR_TOOLS",
    "MAIN_EVENT_TOOLS",
    "READ_ONLY_TOOLS",
    "REPORT_WRITE_TOOLS",
    "ROLE_BASE_TOOLS",
    "SHELL_TOOL",
    "WEB_TOOLS",
    "WORKER_READ_TOOLS",
    "WORKER_WRITE_TOOLS",
]
