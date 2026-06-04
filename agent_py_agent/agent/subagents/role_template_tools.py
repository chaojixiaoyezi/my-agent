
from __future__ import annotations

WEB_TOOLS = ["web_search", "web_fetch"]
READ_ONLY_TOOLS = ["list_files", "read_file", "search_text", "read_artifact", *WEB_TOOLS]
WORKER_READ_TOOLS = ["list_files", "read_file", "search_text", "read_artifact", *WEB_TOOLS]
ARTIFACT_BUILDER_TOOLS: list[str] = []
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
    "wait",
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
