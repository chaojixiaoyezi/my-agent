
from __future__ import annotations

from typing import Any

from .store_claims import ConversationClaimStore


class ConversationStore(ConversationClaimStore):
    def context_bundle(self, thread_id: str, *, recent_limit: int = 20) -> dict[str, Any]:
        bundle, _load_errors = self.context_bundle_report(thread_id, recent_limit=recent_limit)
        return bundle

    def context_bundle_report(
        self,
        thread_id: str,
        *,
        recent_limit: int = 20,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        thread = self._require_thread(thread_id)
        messages, message_errors = self.recent_messages_report(thread_id, limit=recent_limit)
        tasks, task_errors = self.task_links_report(thread_id)
        observations, observation_errors = self.recent_observations_report(thread_id, limit=recent_limit)
        guidance, guidance_errors = self.pending_guidance_report("thread", thread_id, limit=recent_limit)
        return {
            "thread": thread.to_dict(),
            "messages": [item.to_dict() for item in messages],
            "tasks": [item.to_dict() for item in tasks],
            "channel_bindings": [item.to_dict() for item in thread.channel_bindings],
            "observations": [item.to_dict() for item in observations],
            "guidance": [item.to_dict() for item in guidance],
        }, [*message_errors, *task_errors, *observation_errors, *guidance_errors]


__all__ = ["ConversationStore"]
