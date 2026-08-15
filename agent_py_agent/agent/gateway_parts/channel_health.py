from __future__ import annotations

"""Project the adapter daemon's typed lifecycle file into channel health facts."""

from datetime import datetime, timezone
from typing import Any

from .daemon_control import get_running_pid_report
from .io import read_json_file_report
from .paths import gateway_paths
from .process_control import is_pid_alive

_HEALTH_STATES = frozenset(
    {"not_probed", "registered", "starting", "healthy", "unhealthy", "stopped"}
)
_HEARTBEAT_STALE_SECONDS = 30.0


# LLM: 该 provider 只读取 adapter PID 和结构化状态文件；模型正文、日志文字和配置猜测都不参与健康判断。
# 函数用途: 为当前 Agent 的 channel registry 提供跨进程健康覆盖，状态缺失或损坏时安全降级。
def adapter_runtime_health(agent: object) -> dict[str, dict[str, str]]:
    paths = gateway_paths(agent)
    checked_at = _utc_now_iso()
    pid_report = get_running_pid_report(paths.adapter_pid)
    if pid_report.load_error is not None:
        return _all_channels_health(
            "unhealthy",
            checked_at,
            "CHANNEL_ADAPTER_PID_UNREADABLE",
        )
    pid = pid_report.pid
    alive = bool(pid and is_pid_alive(pid))
    state_report = read_json_file_report(
        paths.root / "adapter_state.json",
        context="gateway.channel_health.adapter_state.read",
    )
    if state_report.load_error is not None:
        return _all_channels_health(
            "unhealthy" if alive else "stopped",
            checked_at,
            "CHANNEL_ADAPTER_STATE_UNREADABLE",
        )
    payload = state_report.payload if isinstance(state_report.payload, dict) else {}
    if not alive:
        state = "stopped" if payload.get("state") == "stopped" else "unhealthy"
        error = "" if state == "stopped" else "CHANNEL_ADAPTER_NOT_RUNNING"
        return _all_channels_health(state, checked_at, error)
    if _state_is_stale(payload):
        return _all_channels_health(
            "unhealthy",
            checked_at,
            "CHANNEL_ADAPTER_HEARTBEAT_STALE",
        )
    rows = _channel_rows(payload.get("channels"), checked_at)
    if rows:
        return rows
    requested = str(payload.get("requested_channel") or payload.get("channel") or "*").strip()
    state = "healthy" if payload.get("state") == "running" else "starting"
    key = "*" if requested in {"", "all"} else requested
    return {key: _health_payload(state, checked_at)}


# LLM: 通道行只接受 registry 发布的脱敏 health 协议；未知状态不能自动改成 healthy。
# 函数用途: 从 adapter 状态文件读取每个通道的健康值。
def _channel_rows(value: object, checked_at: str) -> dict[str, dict[str, str]]:
    if not isinstance(value, list):
        return {}
    result: dict[str, dict[str, str]] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().lower()
        health = item.get("health")
        if not name or not isinstance(health, dict):
            continue
        raw_state = str(health.get("state") or "not_probed")
        state = raw_state if raw_state in _HEALTH_STATES else "not_probed"
        error_code = str(health.get("error_code") or "")
        if raw_state not in _HEALTH_STATES:
            error_code = "CHANNEL_HEALTH_PROTOCOL_INVALID"
        result[name] = _health_payload(state, checked_at, error_code)
    return result


# LLM: heartbeat 只比较 adapter 自己写入的 UTC timestamp；无法解析按 stale 处理，不能 fail-open。
# 函数用途: 判断 adapter 生命周期文件是否停止更新。
def _state_is_stale(payload: dict[str, Any]) -> bool:
    updated_at = str(payload.get("updated_at") or "")
    if not updated_at:
        return True
    try:
        stamp = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds() > _HEARTBEAT_STALE_SECONDS


# LLM: wildcard 只表示当前 adapter 进程整体状态，registry 仍只投影自己已注册的 installed channels。
# 函数用途: 生成适用于所有已注册通道的健康覆盖。
def _all_channels_health(state: str, checked_at: str, error_code: str) -> dict[str, dict[str, str]]:
    return {"*": _health_payload(state, checked_at, error_code)}


# LLM: health payload 是 registry provider 的稳定最小 schema，不带 PID、路径或异常正文。
# 函数用途: 创建一个脱敏健康记录。
def _health_payload(state: str, checked_at: str, error_code: str = "") -> dict[str, str]:
    return {
        "state": state,
        "checked_at": checked_at,
        "error_code": error_code,
    }


# LLM: 跨进程时间统一 UTC ISO，便于测试与运行时 freshness 判断。
# 函数用途: 返回当前 UTC 时间。
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["adapter_runtime_health"]
