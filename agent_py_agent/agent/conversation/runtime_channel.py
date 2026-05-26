# LLM: Channel runtime attaches incoming messages to durable threads.
# 模块用途: 接收外部通道消息，写入 thread 后按需唤醒后台主代理。

from __future__ import annotations

from .models import ConversationThread
from .runtime_utils import now as current_time
from .runtime_worker import BackgroundMainAgentRuntime
from .store import ConversationStore


class ChannelMessageRuntime:
    def __init__(self, *, runtime: BackgroundMainAgentRuntime, store: ConversationStore):
        self.runtime = runtime
        self.store = store

    def receive(self, request: dict) -> ConversationThread:
        current = current_time(request.get("now"))
        thread = self.store.get_or_create_thread(
            {
                "canonical_user_id": request.get("canonical_user_id", ""),
                "channel": request.get("channel", ""),
                "channel_conversation_id": request.get("channel_conversation_id", ""),
                "channel_user_id": request.get("channel_user_id", ""),
                "reuse_latest_for_user": True,
                "now": current,
            }
        )
        self.store.append_message({"thread_id": thread.thread_id, "role": "user", "content": request.get("content", ""), "channel": request.get("channel", ""), "now": current})
        if request.get("run_background", True):
            self.runtime.run_once({"thread_id": thread.thread_id, "reason": "incoming_channel_message", "route_channel": request.get("channel", ""), "route_target": request.get("channel_conversation_id", ""), "now": current})
        latest = self.store.load_thread(thread.thread_id)
        if latest is None:
            raise KeyError(f"unknown conversation thread: {thread.thread_id}")
        return latest
