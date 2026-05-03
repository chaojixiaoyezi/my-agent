"""LLM: gateway connection and retry settings."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["GatewayConfig"]


@dataclass
class GatewayConfig:
    """Gateway connection and retry configuration."""

    gateway_workspace: str = "data/gateway"
    gateway_heartbeat_interval: int = 5
    gateway_stale_seconds: int = 120
    gateway_stop_timeout: int = 20
    gateway_request_timeout: int = 300
    gateway_request_poll_interval: int = 1
    gateway_request_workers: int = 1
    gateway_processing_timeout_seconds: int = 900
    gateway_request_max_attempts: int = 2
    gateway_port: int = 8420
    lease_heartbeat_interval_seconds: int = 60
    lease_stale_without_heartbeat_seconds: int = 300