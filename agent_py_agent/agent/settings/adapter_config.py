"""LLM: adapter channel settings."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["AdapterConfig"]


@dataclass
class AdapterConfig:
    """Adapter channel configuration for external platforms (Feishu, QQ, etc.)."""

    adapter_workspace: str = "data/adapters/file"
    # Feishu adapter
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""
    feishu_callback_port: int = 8421
    # QQ adapter
    qq_app_id: str = ""
    qq_app_secret: str = ""