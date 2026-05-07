# LLM: Gateway service module; keep file-queue, daemon, HTTP, and audit contracts stable.
# 模块用途: 拆分 gateway 请求队列、守护进程、HTTP 处理和响应渲染逻辑。

from __future__ import annotations

"""Persisted runtime status for gateway daemon diagnostics."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .daemon_metadata import (
    _get_process_start_time,
    _read_json_file,
    _utc_now_iso,
    _write_json_file,
)


# LLM: WriteRuntimeStatusParams 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 集中保存write运行时状态参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class WriteRuntimeStatusParams:

    status_path: Path
    gateway_state: Any = None
    exit_reason: Any = None
    restart_requested: bool = False
    active_agents: int = 0
    platform: str | None = None
    platform_state: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    extra: dict[str, Any] | None = None


# LLM: _base_runtime_payload 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理基础运行时载荷相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def _base_runtime_payload(status_path: Path) -> dict:
    return _read_json_file(status_path) or {
        "kind": "my-agent-gateway",
        "pid": os.getpid(),
        "start_time": _get_process_start_time(os.getpid()),
        "gateway_state": "unknown",
        "exit_reason": None,
        "restart_requested": False,
        "active_agents": 0,
        "platforms": {},
        "updated_at": _utc_now_iso(),
    }


# LLM: _merge_runtime_fields 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 更新运行时字段对应的任务或运行状态，并保留既有字段语义；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def _merge_runtime_fields(payload: dict, params: WriteRuntimeStatusParams) -> None:
    payload.setdefault("platforms", {})
    payload["pid"] = os.getpid()
    payload["start_time"] = _get_process_start_time(os.getpid())
    payload["updated_at"] = _utc_now_iso()
    if params.gateway_state is not None:
        payload["gateway_state"] = params.gateway_state
    if params.exit_reason is not None:
        payload["exit_reason"] = params.exit_reason
    payload["restart_requested"] = params.restart_requested
    payload["active_agents"] = max(0, int(params.active_agents))
    if params.extra:
        payload.update({key: value for key, value in params.extra.items() if value is not None})


# LLM: _merge_platform_status 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 更新platform状态对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新请求队列、租约文件、进程状态和响应渲染，需避免破坏既有状态机约定。
def _merge_platform_status(payload: dict, params: WriteRuntimeStatusParams) -> None:
    if params.platform is None:
        return
    platform_payload = payload["platforms"].get(params.platform, {})
    if params.platform_state is not None:
        platform_payload["state"] = params.platform_state
    if params.error_code is not None:
        platform_payload["error_code"] = params.error_code
    if params.error_message is not None:
        platform_payload["error_message"] = params.error_message
    platform_payload["updated_at"] = _utc_now_iso()
    payload["platforms"][params.platform] = platform_payload


# LLM: write_runtime_status 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 写入运行时状态的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
def write_runtime_status(params: WriteRuntimeStatusParams) -> None:
    payload = _base_runtime_payload(params.status_path)
    _merge_runtime_fields(payload, params)
    _merge_platform_status(payload, params)
    _write_json_file(params.status_path, payload)


# LLM: read_runtime_status 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 读取或查询运行时状态需要的状态，返回调用方可继续处理的快照；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def read_runtime_status(status_path: Path) -> dict | None:
    return _read_json_file(status_path)
