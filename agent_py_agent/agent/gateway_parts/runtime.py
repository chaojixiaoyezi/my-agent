from __future__ import annotations

"""Gateway runtime entry point — thin re-export facade.

给人看的解释：
这个文件是 gateway 的主运行时。它负责投递 ask 请求、等待响应、显示状态、处理 pending 队列。
大部分实现已经拆分到 request_worker、queue_service、response_renderer、audit_service 等模块。
"""

from .io import write_json_file_atomic
from .lease import (
    _active_heartbeat_request_ids,
    _gateway_processing_lease_interval,
    _start_gateway_processing_lease_heartbeat,
    _touch_gateway_processing_lease,
    is_heartbeat_alive_for_request,
)
from .lease_service import (
    get_active_heartbeat_request_ids,
)
from .queue_service import (
    ensure_gateway_folders,
    gateway_running,
    rebuild_gateway_index,
    render_gateway_status,
    wait_for_gateway_running,
)
from .request_worker import (
    _handle_gateway_request,
    _process_gateway_requests,
    submit_gateway_ask,
    wait_for_gateway_response,
)
from .response_renderer import print_gateway_response

__all__ = [
    "_active_heartbeat_request_ids",
    "_gateway_processing_lease_interval",
    "_handle_gateway_request",
    "_process_gateway_requests",
    "_start_gateway_processing_lease_heartbeat",
    "_touch_gateway_processing_lease",
    "ensure_gateway_folders",
    "gateway_running",
    "get_active_heartbeat_request_ids",
    "is_heartbeat_alive_for_request",
    "print_gateway_response",
    "rebuild_gateway_index",
    "render_gateway_status",
    "submit_gateway_ask",
    "wait_for_gateway_response",
    "wait_for_gateway_running",
    "write_json_file_atomic",
]