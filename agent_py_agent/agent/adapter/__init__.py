"""LLM: 通道适配器模块 — 支持飞书、QQ 等外部通讯平台接入 agent。

给人看的解释：
这个包提供统一的外部通道接入框架。
- base.py: 适配器抽象基类
- protocol.py: 统一消息格式（IncomingMessage / OutgoingMessage）
- manager.py: 通道管理器
- feishu.py: 飞书适配器
- qq.py: QQ 适配器
"""

from .base import BaseChannelAdapter
from .feishu import FeishuAdapter
from .manager import ChannelManager
from .protocol import IncomingMessage, OutgoingMessage, feishu_to_incoming, outgoing_to_feishu, qq_to_incoming, outgoing_to_qq
from .qq import QQAdapter

__all__ = [
    "BaseChannelAdapter",
    "ChannelManager",
    "IncomingMessage",
    "OutgoingMessage",
    "FeishuAdapter",
    "QQAdapter",
    "feishu_to_incoming",
    "outgoing_to_feishu",
    "qq_to_incoming",
    "outgoing_to_qq",
]
