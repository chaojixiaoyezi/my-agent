from __future__ import annotations

"""LLM: 通道投递公共入口；业务代码只依赖 typed context/envelope、registry 和 service。

模块用途: 汇总多 IM 共用的投递模型、适配器注册表与统一发送服务。
"""

from ..conversation.channels import (
    ChannelAttachment,
    DeliveryContext,
    DeliveryReceipt,
    DeliveryServiceProtocol,
    ReplyEnvelope,
)
from .registry import (
    ChannelAdapterRegistry,
    ChannelCapabilities,
    ChannelHealth,
    ChannelRuntimeSnapshot,
    RuntimeHealthProvider,
    TargetValidator,
    build_default_channel_registry,
)
from .service import DeliveryService

__all__ = [
    "ChannelAdapterRegistry",
    "ChannelAttachment",
    "ChannelCapabilities",
    "ChannelHealth",
    "ChannelRuntimeSnapshot",
    "DeliveryContext",
    "DeliveryReceipt",
    "DeliveryService",
    "DeliveryServiceProtocol",
    "ReplyEnvelope",
    "RuntimeHealthProvider",
    "TargetValidator",
    "build_default_channel_registry",
]
