"""adapter channel settings."""

# LLM: 字段默认值会影响外部平台连接行为，改动前核对 CLI 和适配器调用方。
# 模块用途: 聊天适配器、轮询和消息窗口的配置模型。

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["AdapterConfig"]


# LLM: AdapterConfig 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: AdapterConfig 配置模型，保存 配置系统 的默认值和可调参数。
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