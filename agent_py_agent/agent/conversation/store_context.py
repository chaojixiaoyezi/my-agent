# LLM: Context bundle rendering reads existing ledgers without side effects.
# 模块用途: 为后台主代理提供小型 thread/message/task/observation 上下文包。

from __future__ import annotations

from typing import Any

from .store_claims import ConversationClaimStore


class ConversationContextStore(ConversationClaimStore):
    def context_bundle(self, thread_id: str, *, recent_limit: int = 20) -> dict[str, Any]:
        thread = self._require_thread(thread_id)
        return {
            "thread": thread.to_dict(),
            "messages": [item.to_dict() for item in self.recent_messages(thread_id, limit=recent_limit)],
            "tasks": [item.to_dict() for item in self.task_links(thread_id)],
            "channel_bindings": [item.to_dict() for item in thread.channel_bindings],
            "observations": [item.to_dict() for item in self.recent_observations(thread_id, limit=recent_limit)],
            "guidance": [item.to_dict() for item in self.pending_guidance("thread", thread_id, limit=recent_limit)],
        }
