# LLM: Broad role tool manifests are shared data, separate from template loading.
# 模块用途: 集中维护内置角色默认工具集合，避免模板注册和工具清单长期挤在同一个模块。

from __future__ import annotations

# LLM: Web roles use the four network tools; fetch_url remains a hidden compatibility alias only.
WEB_TOOLS = ["web_search", "web_fetch", "web_extract", "http_request"]
READ_ONLY_TOOLS = ["list_files", "read_file", "search_text", "read_artifact", *WEB_TOOLS]
WORKER_READ_TOOLS = ["list_files", "read_file", "search_text", "read_artifact", *WEB_TOOLS]
ARTIFACT_BUILDER_TOOLS: list[str] = []
# LLM: Built-in roles now share the generic write surface instead of append/replace/builder tools.
WORKER_WRITE_TOOLS = ["write_file", "apply_patch"]
REPORT_WRITE_TOOLS = ["write_file", "apply_patch"]
CAPABILITY_REQUEST_TOOL = "capability_request"
MAIN_EVENT_TOOLS = ["raise_observation", "raise_main_event"]
COLLABORATION_TOOLS = [
    "raise_collaboration_event",
    "open_case",
    "request_collaboration",
    "list_collaboration_requests",
    "submit_evidence",
    "update_collaboration_request",
    "reroute_collaboration_request",
    "update_case_status",
    "case_status",
]
ROLE_BASE_TOOLS = [
    *READ_ONLY_TOOLS,
    *REPORT_WRITE_TOOLS,
    *MAIN_EVENT_TOOLS,
    *COLLABORATION_TOOLS,
    CAPABILITY_REQUEST_TOOL,
]
COORDINATOR_TOOLS = [
    "schedule_child_subagents",
    "dispatch_subagents",
    # LLM: Coordinators can inspect status without entering dispatch, keeping "look only" separate from "advance work".
    "inspect_agent_tree",
    "subagent_board",
    "subagent_message",
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
    "WEB_TOOLS",
    "WORKER_READ_TOOLS",
    "WORKER_WRITE_TOOLS",
]
