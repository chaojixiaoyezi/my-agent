# LLM: Fake channels exercise routing semantics without touching real Feishu or WeChat.
# 模块用途: 提供 internal/fake-feishu/fake-wechat 的离线投递器，供后台主代理测试使用。

from __future__ import annotations

from dataclasses import dataclass, field


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


__all__ = ["ChannelSendRequest", "FakeChannelAdapter", "FakeChannelHub", "SentChannelMessage"]
