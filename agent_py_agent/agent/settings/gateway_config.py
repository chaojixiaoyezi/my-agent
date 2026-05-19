"""gateway connection and retry settings."""

# LLM: 默认值控制守护进程稳定性，调参前核对 gateway 场景测试。
# 模块用途: gateway 连接、租约、清理和重试参数模型。

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["GatewayConfig"]


# LLM: GatewayConfig 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: GatewayConfig 配置模型，保存 配置系统 的默认值和可调参数。
@dataclass
class GatewayConfig:
    """Gateway connection and retry configuration."""

    gateway_workspace: str = "data/gateway"
    gateway_heartbeat_interval: int = 5
    gateway_stale_seconds: int = 120
    gateway_stop_timeout: int = 20
    gateway_request_timeout: int = 300
    gateway_request_poll_interval: int = 1
    gateway_request_workers: int = 2
    gateway_foreground_reserved_workers: int = 1
    gateway_background_model_request_timeout: int = 900
    gateway_processing_timeout_seconds: int = 900
    gateway_request_max_attempts: int = 2
    gateway_port: int = 8420
    lease_heartbeat_interval_seconds: int = 60
    lease_stale_without_heartbeat_seconds: int = 300
