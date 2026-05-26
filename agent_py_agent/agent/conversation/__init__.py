# LLM: Conversation package is the public facade for durable main-agent threads.
# 模块用途: 暴露长期会话、后台主代理运行器和离线通道测试工具。

from __future__ import annotations

from .channels import ChannelSendRequest, FakeChannelAdapter, FakeChannelHub, SentChannelMessage
from .models import (
    BackgroundMainAgentReport,
    ChannelBinding,
    ConversationThread,
    MessageLogEntry,
    ObservationEvent,
    ProgressPolicy,
    ThreadTaskLink,
    WakeSignal,
)
from .runtime import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    BackgroundRunRequest,
    ChannelMessageRuntime,
)
from .store import ConversationStore

__all__ = [
    "BackgroundMainAgentReport",
    "BackgroundMainAgentRuntime",
    "BackgroundMainAgentScheduler",
    "BackgroundRunRequest",
    "ChannelBinding",
    "ChannelSendRequest",
    "ChannelMessageRuntime",
    "ConversationStore",
    "ConversationThread",
    "FakeChannelAdapter",
    "FakeChannelHub",
    "MessageLogEntry",
    "ObservationEvent",
    "ProgressPolicy",
    "SentChannelMessage",
    "ThreadTaskLink",
    "WakeSignal",
]
