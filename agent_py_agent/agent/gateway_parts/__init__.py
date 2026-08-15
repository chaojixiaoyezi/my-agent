
from __future__ import annotations

"""public gateway package API composed from focused protocol modules.

gateway 代码按职责拆开：路径、文件 IO、进程控制、恢复、运行时、adapter 转换、索引日志各管一块。
"""

from .adapter import (
    _adapter_message_id,
    _adapter_message_prompt,
    _adapter_output_path,
    _archive_adapter_message,
    check_late_responses,
    process_file_adapter_once,
)
from .daemon_control import get_running_pid, read_pid_record
from .io import (
    append_gateway_history,
    gateway_queue_ages,
    gateway_request_counts,
    gateway_response_path,
    new_gateway_request_id,
    read_json_file,
    read_pid,
    tail_lines,
    write_gateway_request,
    write_json_file,
)
from .logging import (
    _index_gateway_payload,
    _report_gateway_side_effect_error,
    log_gateway_event,
    log_gateway_payload,
)
from .paths import (
    AdapterPaths,
    GatewayPaths,
    adapter_paths,
    gateway_chunk_path,
    gateway_chunk_path_candidates,
    gateway_paths,
)
from .process_control import is_pid_alive, terminate_pid, wait_for_pid_exit
from .queue_service import (
    ensure_gateway_folders,
    gateway_running,
    is_heartbeat_alive_for_request,
    rebuild_gateway_index,
    rebuild_gateway_index_report,
    render_gateway_status,
    wait_for_gateway_running,
)
from .recovery import (
    _archive_gateway_request,
    _gateway_processing_started_at,
    _gateway_request_attempts,
    _write_gateway_failure_response,
    gateway_stale_processing,
    recover_gateway_processing_requests,
    requeue_gateway_processing_requests,
)
from .request_worker import (
    GatewayAskParams,
    GatewayInboxScanGate,
    _handle_gateway_request,
    _process_gateway_requests,
    submit_gateway_ask,
    wait_for_gateway_response,
)
from .response_renderer import print_gateway_response

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
