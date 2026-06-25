

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from .protocol import IncomingMessage, OutgoingMessage


class BaseChannelAdapter(ABC):

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

    def send_progress_placeholder(self, user_id: str) -> str:
        """收到消息、agent 开始处理时立即发一个"处理中"占位反馈,返回可原地更新的句柄。
        默认返回空串=该通道不支持占位(route_message 据此降级为"处理完直接发结果",行为不变);
        飞书重写为发一张"正在思考"卡片、返回 message_id 供完成后原地更新。"""
        return ""

    def finalize_response(self, user_id: str, handle: str, message: OutgoingMessage) -> bool:
        """交付最终结果:有占位句柄(handle)就原地更新那条占位,否则直接发新消息。
        默认走 send_message(不支持占位的通道天然降级);飞书重写为 PATCH 更新占位卡片。"""
        return self.send_message(user_id, message)

    @property
    def running(self) -> bool:
        """适配器是否正在运行。"""
        return self._running
