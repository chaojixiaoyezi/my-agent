"""LLM: 通道适配器基类 — 所有外部通道适配器（飞书、QQ 等）必须继承的抽象基类。

给人看的解释：
定义统一的适配器接口：start、stop、send_message、on_message。
通道管理器通过这些接口透明地操作所有适配器。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from .protocol import IncomingMessage, OutgoingMessage


class BaseChannelAdapter(ABC):
    """外部通讯通道适配器的抽象基类。

    所有具体通道适配器（FeishuAdapter、QQAdapter）必须实现：
    - adapter_name: 通道名称
    - start() / stop(): 启停适配器
    - send_message(): 向用户发消息
    - on_message(): 注册消息回调
    """

    adapter_name: str = "base"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._message_callback: Callable[[IncomingMessage], None] | None = None
        self._running = False

    # -------------------------------------------------------------------------
    # 抽象方法 — 子类必须实现
    # -------------------------------------------------------------------------

    @abstractmethod
    def start(self) -> None:
        """启动适配器，连接外部服务或开始监听回调。"""
        ...

    @abstractmethod
    def stop(self) -> None:
        """停止适配器，释放资源。"""
        ...

    @abstractmethod
    def send_message(self, user_id: str, message: OutgoingMessage) -> bool:
        """向指定用户发送消息。返回是否成功。"""
        ...

    # -------------------------------------------------------------------------
    # 公共方法 — 子类可复用
    # -------------------------------------------------------------------------

    def on_message(self, callback: Callable[[IncomingMessage], None]) -> None:
        """注册消息回调，适配器收到外部消息时调用。"""
        self._message_callback = callback

    def _dispatch(self, msg: IncomingMessage) -> None:
        """把收到的消息分发给注册的回调。"""
        if self._message_callback:
            self._message_callback(msg)

    def dispatch(self, msg: IncomingMessage) -> None:
        """公开的消息分发方法。"""
        self._dispatch(msg)

    @property
    def running(self) -> bool:
        """适配器是否正在运行。"""
        return self._running
