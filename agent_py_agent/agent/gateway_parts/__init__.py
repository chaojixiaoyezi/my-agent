from __future__ import annotations

import importlib
from typing import Any

# LLM: This package facade preserves the public Gateway API through lazy attribute loading. Do
# not restore eager imports: importing one leaf module must not initialize adapters, workers,
# conversation runtime, and model backends before the caller actually needs them.
# 模块用途: 汇总 Gateway 的公开入口，同时按需加载具体模块；聊天启动只用路径和存活探测时，
# 不再连带加载整个 Gateway 工作进程。


_EXPORTS: dict[str, tuple[str, str]] = {
    "_adapter_message_id": ("adapter", "_adapter_message_id"),
    "_adapter_message_prompt": ("adapter", "_adapter_message_prompt"),
    "_adapter_output_path": ("adapter", "_adapter_output_path"),
    "_archive_adapter_message": ("adapter", "_archive_adapter_message"),
    "check_late_responses": ("adapter", "check_late_responses"),
    "process_file_adapter_once": ("adapter", "process_file_adapter_once"),
    "get_running_pid": ("daemon_control", "get_running_pid"),
    "read_pid_record": ("daemon_control", "read_pid_record"),
    "append_gateway_history": ("io", "append_gateway_history"),
    "gateway_queue_ages": ("io", "gateway_queue_ages"),
    "gateway_request_counts": ("io", "gateway_request_counts"),
    "gateway_response_path": ("io", "gateway_response_path"),
    "new_gateway_request_id": ("io", "new_gateway_request_id"),
    "read_json_file": ("io", "read_json_file"),
    "read_pid": ("io", "read_pid"),
    "tail_lines": ("io", "tail_lines"),
    "write_gateway_request": ("io", "write_gateway_request"),
    "write_json_file": ("io", "write_json_file"),
    "_index_gateway_payload": ("logging", "_index_gateway_payload"),
    "_report_gateway_side_effect_error": ("logging", "_report_gateway_side_effect_error"),
    "log_gateway_event": ("logging", "log_gateway_event"),
    "log_gateway_payload": ("logging", "log_gateway_payload"),
    "AdapterPaths": ("paths", "AdapterPaths"),
    "GatewayPaths": ("paths", "GatewayPaths"),
    "adapter_paths": ("paths", "adapter_paths"),
    "gateway_chunk_path": ("paths", "gateway_chunk_path"),
    "gateway_chunk_path_candidates": ("paths", "gateway_chunk_path_candidates"),
    "gateway_paths": ("paths", "gateway_paths"),
    "is_pid_alive": ("process_control", "is_pid_alive"),
    "terminate_pid": ("process_control", "terminate_pid"),
    "wait_for_pid_exit": ("process_control", "wait_for_pid_exit"),
    "ensure_gateway_folders": ("queue_service", "ensure_gateway_folders"),
    "gateway_running": ("queue_service", "gateway_running"),
    "is_heartbeat_alive_for_request": ("queue_service", "is_heartbeat_alive_for_request"),
    "rebuild_gateway_index": ("queue_service", "rebuild_gateway_index"),
    "rebuild_gateway_index_report": ("queue_service", "rebuild_gateway_index_report"),
    "render_gateway_status": ("queue_service", "render_gateway_status"),
    "wait_for_gateway_running": ("queue_service", "wait_for_gateway_running"),
    "_archive_gateway_request": ("recovery", "_archive_gateway_request"),
    "_gateway_processing_started_at": ("recovery", "_gateway_processing_started_at"),
    "_gateway_request_attempts": ("recovery", "_gateway_request_attempts"),
    "_write_gateway_failure_response": ("recovery", "_write_gateway_failure_response"),
    "gateway_stale_processing": ("recovery", "gateway_stale_processing"),
    "recover_gateway_processing_requests": ("recovery", "recover_gateway_processing_requests"),
    "requeue_gateway_processing_requests": ("recovery", "requeue_gateway_processing_requests"),
    "GatewayAskParams": ("request_client", "GatewayAskParams"),
    "GatewayInboxScanGate": ("request_worker", "GatewayInboxScanGate"),
    "_handle_gateway_request": ("request_worker", "_handle_gateway_request"),
    "_process_gateway_requests": ("request_worker", "_process_gateway_requests"),
    "submit_gateway_ask": ("request_client", "submit_gateway_ask"),
    "wait_for_gateway_response": ("request_worker", "wait_for_gateway_response"),
    "print_gateway_response": ("response_renderer", "print_gateway_response"),
}

__all__ = [
    "AdapterPaths",
    "GatewayAskParams",
    "GatewayPaths",
    "adapter_paths",
    "append_gateway_history",
    "check_late_responses",
    "gateway_paths",
    "gateway_chunk_path",
    "gateway_chunk_path_candidates",
    "gateway_queue_ages",
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
    "rebuild_gateway_index_report",
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
    "GatewayInboxScanGate",
    "_process_gateway_requests",
]


# LLM: Resolve declared exports or real sibling modules once and cache them on the package. A
# missing sibling must remain AttributeError so import and introspection semantics stay standard.
# 函数用途: 第一次访问公开符号或子模块时再导入，并缓存结果供后续调用直接使用。
def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is not None:
        module_name, attribute_name = target
        module = importlib.import_module(f".{module_name}", __name__)
        value = getattr(module, attribute_name)
        globals()[name] = value
        return value
    try:
        module = importlib.import_module(f".{name}", __name__)
    except ModuleNotFoundError as exc:
        if exc.name == f"{__name__}.{name}":
            raise AttributeError(name) from None
        raise
    globals()[name] = module
    return module


# LLM: Include lazy exports in introspection without forcing imports.
# 函数用途: 让补全、调试器和 dir() 能看到尚未加载的 Gateway 公开入口。
def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
