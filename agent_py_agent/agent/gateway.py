# LLM: Agent package module; keep public imports and cross-module compatibility stable.
# 模块用途: 提供 agent 核心功能的一部分，对外暴露稳定入口或兼容转发。

from __future__ import annotations

"""compatibility facade for the split `agent.gateway_parts` modules.

给人看的解释：
真正的 gateway 实现已经拆到 `agent_py_agent.agent.gateway_parts` 目录。
这个文件保留老导入路径，避免 `from agent_py_agent.agent.gateway import ...` 的调用方失效。
新代码可以优先按职责导入 gateway_parts 里的具体模块。
"""

from .gateway_parts import (
    AdapterPaths,
    GatewayAskParams,
    GatewayPaths,
    _handle_gateway_request,
    _process_gateway_requests,
    adapter_paths,
    append_gateway_history,
    check_late_responses,
    gateway_chunk_path,
    gateway_paths,
    gateway_request_counts,
    gateway_response_path,
    gateway_running,
    gateway_stale_processing,
    get_running_pid,
    is_heartbeat_alive_for_request,
    is_pid_alive,
    log_gateway_event,
    log_gateway_payload,
    new_gateway_request_id,
    print_gateway_response,
    process_file_adapter_once,
    read_json_file,
    read_pid,
    read_pid_record,
    rebuild_gateway_index,
    recover_gateway_processing_requests,
    render_gateway_status,
    requeue_gateway_processing_requests,
    submit_gateway_ask,
    tail_lines,
    terminate_pid,
    wait_for_gateway_response,
    wait_for_gateway_running,
    wait_for_pid_exit,
    write_gateway_request,
    write_json_file,
)

__all__ = [
    "AdapterPaths",
    "GatewayAskParams",
    "GatewayPaths",
    "adapter_paths",
    "append_gateway_history",
    "check_late_responses",
    "gateway_paths",
    "gateway_chunk_path",
    "gateway_request_counts",
    "gateway_response_path",
    "gateway_running",
    "get_running_pid",
    "gateway_stale_processing",
    "is_pid_alive",
    "is_heartbeat_alive_for_request",
    "log_gateway_event",
    "log_gateway_payload",
    "new_gateway_request_id",
    "print_gateway_response",
    "process_file_adapter_once",
    "read_json_file",
    "read_pid",
    "read_pid_record",
    "rebuild_gateway_index",
    "recover_gateway_processing_requests",
    "render_gateway_status",
    "requeue_gateway_processing_requests",
    "submit_gateway_ask",
    "tail_lines",
    "terminate_pid",
    "wait_for_gateway_response",
    "wait_for_gateway_running",
    "wait_for_pid_exit",
    "write_gateway_request",
    "write_json_file",
    "_handle_gateway_request",
    "_process_gateway_requests",
]
