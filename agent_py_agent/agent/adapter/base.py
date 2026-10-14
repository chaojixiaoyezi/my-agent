

# LLM: 渠道接口保持原发送合同；入站 callback 返回值必须原样传递，显式 False 不得伪装受理。
# 模块用途: 提供渠道生命周期、入站回调和发送入口，不推断供应商去重或只读查询能力。
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from .protocol import IncomingMessage, OutgoingMessage


# LLM: 默认无 provider 幂等/查询保证；仅显式支持的子类允许不确定外发的安全恢复。
# 类用途: 声明通道的实际原生能力，TUI/本地或其它未声明渠道均保持不支持。
class BaseChannelAdapter(ABC):

    adapter_name: str = "base"
    provider_idempotent_delivery: bool = False
    provider_delivery_queryable: bool = False
    provider_idempotency_window_seconds: float = 0.0

    # LLM: 初始化仅保存配置和 callback；bool/void 消费者仍共用一个派发入口，不另维护接收状态。
    # 函数用途: 创建未启动的适配器对象，不发请求或写消息队列。
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._message_callback: Callable[[IncomingMessage], object] | None = None
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

    # LLM: callback 是宿主绑定消费者，不来自消息正文；原 void 回调正常返回仍表示消费完成。
    # 函数用途: 绑定唯一入站消费者，其返回值供具体通道确认受理。
    def on_message(self, callback: Callable[[IncomingMessage], object]) -> None:
        """注册消息回调，适配器收到外部消息时调用。"""
        self._message_callback = callback

    # LLM: 原样传递消费者回执和异常，缺消费者返回 False；本入口不捕获异常或提交供应商去重。
    # 函数用途: 调用已经注册的消费者，让飞书能够识别入站落盘失败。
    def _dispatch(self, msg: IncomingMessage) -> object:
        """把收到的消息分发给注册的回调。"""
        if self._message_callback:
            return self._message_callback(msg)
        return False

    # LLM: 公开派发和内部派发使用相同回执，无第二入口状态或隐式成功。
    # 函数用途: 把公开调用交给同一消费者并保留实际结果。
    def dispatch(self, msg: IncomingMessage) -> object:
        """公开的消息分发方法。"""
        return self._dispatch(msg)

    def send_progress_placeholder(self, user_id: str, message_id: str = "") -> str:
        """收到消息、agent 开始处理时立即给"处理中"反馈,返回可撤销的句柄。
        默认返回空串=该通道不支持(route_message 据此跳过,行为不变);飞书重写为给该消息贴
        emoji reaction(原生"正在输入"提示)、返回句柄供完成后撤销。"""
        return ""

    # LLM: Interrupt cleanup removes only the transient indicator; it must not synthesize user-visible text.
    # 函数用途: 中断任务时撤掉处理中提示，不另发一条固定文案。
    def clear_progress_placeholder(self, user_id: str, handle: str) -> None:
        """Remove a pending progress indicator without sending a replacement message."""
        return None

    def finalize_response(self, user_id: str, handle: str, message: OutgoingMessage) -> bool:
        """交付最终结果:有句柄(handle)就先撤掉"处理中"反馈再发结果,否则直接发。
        默认走 send_message(不支持的通道天然降级);飞书重写为先撤 typing reaction 再发回复。"""
        return self.send_message(user_id, message)

    @property
    def running(self) -> bool:
        """适配器是否正在运行。"""
        return self._running
