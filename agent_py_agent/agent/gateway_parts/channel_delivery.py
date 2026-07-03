
from __future__ import annotations

"""网关侧"真渠道"投递枢纽:把后台主代理产出主动外呼到飞书等外部通道。

给人看的解释:
后台主代理被子代理事件叫回、跑出一份汇总后,得把这份汇总"主动"发回用户所在的外部通道(用户没先问、
agent 主动发)。以前后台循环塞的是 FakeChannelHub(只在内存记一笔、不真发),于是"叫回来产出了也发不出去"。
这个枢纽实现同样的 .send(request) 接口,但对"可主动外呼"的通道(飞书)真调 adapter.send_message 投递;
对 internal/chat 这类本地/内部通道保持原样(不外发),所以单机/CLI 行为不变。

飞书发送走 adapter 的 REST(无需长连接),因此网关进程即使没起飞书长连接也能主动外发。adapter/凭据
懒建并缓存(按通道),任何构建或发送异常都只记日志、绝不抛回 scheduler(一个通道炸了不拖垮值守循环)。
"""

import logging
import threading
from typing import Any

from ..conversation.channels import (
    PROACTIVE_PUSH_CHANNELS,
    ChannelSendRequest,
    SentChannelMessage,
    leads_with_internal_signal,
)

_LOGGER = logging.getLogger(__name__)


def _is_internal_signal(content: str) -> bool:
    # 内部交付/运行信号是出口门/调度用的结构化标记,不是给用户看的正文,主动外呼时要滤掉。
    # 前缀定义在 conversation.channels(与逐条结论追加层共用一份,防两处漂移)。
    return leads_with_internal_signal(content)


class GatewayChannelHub:
    """真渠道投递枢纽(接口兼容 FakeChannelHub 的 .send);按通道懒建+缓存 adapter,线程安全,永不抛。"""

    def __init__(self, config: Any) -> None:
        self._config = config
        self._adapters: dict[str, Any] = {}  # channel -> adapter 或 None(None=已试过但不可用,不再重试)
        self._lock = threading.Lock()

    def send(self, request: ChannelSendRequest) -> SentChannelMessage:
        channel = str(getattr(request, "channel", "") or "")
        target = str(getattr(request, "target", "") or "")
        content = str(getattr(request, "content", "") or "")
        receipt = SentChannelMessage(
            channel=channel,
            target=target,
            content=content,
            thread_id=str(getattr(request, "thread_id", "") or ""),
            task_id=str(getattr(request, "task_id", "") or ""),
        )
        # 非可主动外呼通道(internal/chat/gateway-cli)/无目标/空内容 → 只回执不外发(等价原 Fake 行为)。
        if channel not in PROACTIVE_PUSH_CHANNELS or not target or not content.strip():
            return receipt
        # 内部交付/运行信号([MAIN_AGENT_DELIVERY_...]、[RUN_NONBLOCKING_YIELD]、[RUN_UNFINISHED_EXIT] 等)
        # 是给出口门/调度用的,不是给用户看的——唤醒多次时别把这些当消息主动推给用户(只回执)。
        if _is_internal_signal(content):
            return receipt
        adapter = self._adapter_for(channel)
        if adapter is None:
            _LOGGER.info("gateway channel hub: channel=%s 无可用 adapter(缺凭据?),本次主动外呼跳过", channel)
            return receipt
        self._safe_send(adapter, channel, target, content)
        return receipt

    def _safe_send(self, adapter: Any, channel: str, target: str, content: str) -> None:
        try:
            outgoing = self._build_outgoing(channel, target, content)
            ok = bool(adapter.send_message(target, outgoing))
            if not ok:
                _LOGGER.warning("gateway channel hub: channel=%s send_message 返回失败 target=%s", channel, target)
        except Exception as exc:  # 主动外呼失败绝不回抛,只记账
            _LOGGER.error("gateway channel hub: channel=%s 主动外呼异常 %s: %s", channel, type(exc).__name__, exc)

    def _adapter_for(self, channel: str) -> Any:
        with self._lock:
            if channel in self._adapters:
                return self._adapters[channel]
            adapter = self._build_adapter(channel)
            self._adapters[channel] = adapter  # 缓存(含 None:缺凭据时不每次重试)
            return adapter

    def _build_adapter(self, channel: str) -> Any:
        if channel == "feishu":
            return self._build_feishu_adapter()
        return None

    def _build_feishu_adapter(self) -> Any:
        # 懒导入:飞书 adapter/凭据模块只在真要发飞书时才加载(网关不发飞书就不碰),隔离潜在依赖问题。
        try:
            from ..adapter.feishu import FeishuAdapter
            from ..settings.secret_ref import resolve_secret_ref

            app_id = resolve_secret_ref(str(getattr(self._config, "feishu_app_id", "") or ""))
            app_secret = resolve_secret_ref(str(getattr(self._config, "feishu_app_secret", "") or ""))
            if not app_id or not app_secret:
                return None
            # 只构造(读凭据/发 REST),不 start()(那是入站长连接/绑端口);发送靠 tenant token + REST。
            return FeishuAdapter({"feishu_app_id": app_id, "feishu_app_secret": app_secret})
        except Exception as exc:
            _LOGGER.error("gateway channel hub: 构建飞书 adapter 失败 %s: %s", type(exc).__name__, exc)
            return None

    def _build_outgoing(self, channel: str, target: str, content: str) -> Any:
        from ..adapter.protocol import OutgoingMessage

        return OutgoingMessage(channel=channel, user_id=target, content=content, format="markdown")


__all__ = ["GatewayChannelHub"]
