# LLM: Broad role tool manifests are shared data, separate from template loading.
# 模块用途: 集中维护内置角色默认工具集合，避免模板注册和工具清单长期挤在同一个模块。

from __future__ import annotations

WEB_TOOLS = ["fetch_url", "http_request"]
READ_ONLY_TOOLS = ["list_files", "read_file", "search_text", "read_artifact", *WEB_TOOLS]
WORKER_READ_TOOLS = ["list_files", "read_file", "search_text", "read_artifact", *WEB_TOOLS]
ARTIFACT_BUILDER_TOOLS = ["file_write_session", "write_structured_json", "data_to_workbook", "markdown_to_pdf"]
WORKER_WRITE_TOOLS = ["write_file", "append_file", "replace_in_file", *ARTIFACT_BUILDER_TOOLS]
REPORT_WRITE_TOOLS = ["write_file", "append_file", "replace_in_file", *ARTIFACT_BUILDER_TOOLS]
CAPABILITY_REQUEST_TOOL = "capability_request"
ROLE_BASE_TOOLS = [*READ_ONLY_TOOLS, *REPORT_WRITE_TOOLS, CAPABILITY_REQUEST_TOOL]
COORDINATOR_TOOLS = [
    "schedule_child_subagents",
    "dispatch_subagents",
    "subagent_board",
    "subagent_message",
    *ROLE_BASE_TOOLS,
]

__all__ = [
    "ARTIFACT_BUILDER_TOOLS",
    "CAPABILITY_REQUEST_TOOL",
    "COORDINATOR_TOOLS",
    "READ_ONLY_TOOLS",
    "REPORT_WRITE_TOOLS",
    "ROLE_BASE_TOOLS",
    "WEB_TOOLS",
    "WORKER_READ_TOOLS",
    "WORKER_WRITE_TOOLS",
]
