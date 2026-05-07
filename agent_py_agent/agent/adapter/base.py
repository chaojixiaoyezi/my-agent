# LLM: External adapter module; keep platform payload and runtime boundary contracts stable.
# 模块用途: 对接 QQ、飞书等外部渠道，把平台事件转换成内部请求。


from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from .protocol import IncomingMessage, OutgoingMessage


# LLM: BaseChannelAdapter 属于外部通道适配的类边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 类用途: 适配基础通道adapter协议，把平台消息转换为内部统一消息契约；关键副作用: 方法可能触发通道配置、消息回调和平台输入输出相关副作用，需保持公开契约稳定。
class BaseChannelAdapter(ABC):

    adapter_name: str = "base"

    # LLM: __init__ 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._message_callback: Callable[[IncomingMessage], None] | None = None
        self._running = False

    # -------------------------------------------------------------------------
    # 抽象方法 — 子类必须实现
    # -------------------------------------------------------------------------

    # LLM: start 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 推进start的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响通道配置、消息回调和平台输入输出，需保持重试、超时和状态迁移语义。
    @abstractmethod
    def start(self) -> None:
        """启动适配器，连接外部服务或开始监听回调。"""
        ...

    # LLM: stop 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 推进stop的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响通道配置、消息回调和平台输入输出，需保持重试、超时和状态迁移语义。
    @abstractmethod
    def stop(self) -> None:
        """停止适配器，释放资源。"""
        ...

    # LLM: send_message 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 发送消息请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
    @abstractmethod
    def send_message(self, user_id: str, message: OutgoingMessage) -> bool:
        """向指定用户发送消息。返回是否成功。"""
        ...

    # -------------------------------------------------------------------------
    # 公共方法 — 子类可复用
    # -------------------------------------------------------------------------

    # LLM: on_message 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 处理on消息相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
    def on_message(self, callback: Callable[[IncomingMessage], None]) -> None:
        """注册消息回调，适配器收到外部消息时调用。"""
        self._message_callback = callback

    # LLM: _dispatch 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 推进调度的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响通道配置、消息回调和平台输入输出，需保持重试、超时和状态迁移语义。
    def _dispatch(self, msg: IncomingMessage) -> None:
        """把收到的消息分发给注册的回调。"""
        if self._message_callback:
            self._message_callback(msg)

    # LLM: dispatch 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 推进调度的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响通道配置、消息回调和平台输入输出，需保持重试、超时和状态迁移语义。
    def dispatch(self, msg: IncomingMessage) -> None:
        """公开的消息分发方法。"""
        self._dispatch(msg)

    # LLM: running 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 推进running的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响通道配置、消息回调和平台输入输出，需保持重试、超时和状态迁移语义。
    @property
    def running(self) -> bool:
        """适配器是否正在运行。"""
        return self._running
