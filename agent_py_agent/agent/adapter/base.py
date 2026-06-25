

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

    def send_progress_placeholder(self, user_id: str, message_id: str = "") -> str:
        """收到消息、agent 开始处理时立即给"处理中"反馈,返回可撤销的句柄。
        默认返回空串=该通道不支持(route_message 据此跳过,行为不变);飞书重写为给该消息贴
        emoji reaction(原生"正在输入"提示)、返回句柄供完成后撤销。"""
        return ""

    def finalize_response(self, user_id: str, handle: str, message: OutgoingMessage) -> bool:
        """交付最终结果:有句柄(handle)就先撤掉"处理中"反馈再发结果,否则直接发。
        默认走 send_message(不支持的通道天然降级);飞书重写为先撤 typing reaction 再发回复。"""
        return self.send_message(user_id, message)

    @property
    def running(self) -> bool:
        """适配器是否正在运行。"""
        return self._running
