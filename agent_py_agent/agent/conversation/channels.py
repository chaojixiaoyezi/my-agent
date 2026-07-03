
from __future__ import annotations

from dataclasses import dataclass, field

# 支持"主动外呼"(服务端主动发起、用户没先问)的外部通道:后台主代理被子代理事件叫回后产出的
# 汇总要投到这些通道(飞书 send_message 走 REST,无需长连接)。internal/chat/gateway-cli 是本地
# 轮询/内部通道,不主动外发。路由升级(conversation.runtime)与真实投递(gateway_parts.channel_delivery)
# 共用这一份定义,保持"能升级到哪个通道"和"能投到哪个通道"一致。
PROACTIVE_PUSH_CHANNELS = frozenset({"feishu"})

# 内部交付/运行信号前缀:这些是出口门/调度用的结构化标记,不是给用户看的正文。
# 真实投递枢纽(gateway_parts.channel_delivery)据此拦截"以记号开头"的整条回复;
# 逐条结论追加层(conversation.runtime)据此判断该把结论块单独出站还是拼在原文后。
INTERNAL_SIGNAL_PREFIXES = ("[MAIN_AGENT_", "[RUN_", "[SUBAGENT_")


def leads_with_internal_signal(content: str) -> bool:
    return str(content or "").lstrip().startswith(INTERNAL_SIGNAL_PREFIXES)


@dataclass(frozen=True)
class SentChannelMessage:
    channel: str
    target: str
    content: str
    thread_id: str = ""
    task_id: str = ""


@dataclass(frozen=True)
class ChannelSendRequest:
    channel: str
    target: str
    content: str
    thread_id: str = ""
    task_id: str = ""


@dataclass
class FakeChannelAdapter:
    channel: str
    sent_messages: list[SentChannelMessage] = field(default_factory=list)

    def send_message(
        self,
        *,
        target: str,
        content: str,
        thread_id: str = "",
        task_id: str = "",
    ) -> SentChannelMessage:
        message = SentChannelMessage(
            channel=self.channel,
            target=target,
            content=content,
            thread_id=thread_id,
            task_id=task_id,
        )
        self.sent_messages.append(message)
        return message


class FakeChannelHub:
    def __init__(self) -> None:
        self._adapters: dict[str, FakeChannelAdapter] = {}

    def adapter(self, channel: str) -> FakeChannelAdapter:
        key = str(channel or "internal")
        adapter = self._adapters.get(key)
        if adapter is None:
            adapter = FakeChannelAdapter(key)
            self._adapters[key] = adapter
        return adapter

    def send(self, request: ChannelSendRequest) -> SentChannelMessage:
        return self.adapter(request.channel).send_message(
            target=request.target,
            content=request.content,
            thread_id=request.thread_id,
            task_id=request.task_id,
        )


__all__ = [
    "INTERNAL_SIGNAL_PREFIXES",
    "PROACTIVE_PUSH_CHANNELS",
    "ChannelSendRequest",
    "FakeChannelAdapter",
    "FakeChannelHub",
    "SentChannelMessage",
    "leads_with_internal_signal",
]
