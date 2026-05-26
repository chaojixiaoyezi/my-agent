# LLM: Thread and channel-binding operations for conversation persistence.
# 模块用途: 管理长期 thread、跨渠道绑定、用户最新 thread 索引和 thread 摘要。

from __future__ import annotations

from dataclasses import replace

from ..gateway_parts.io import read_json_file, update_json_file_atomic, write_json_file_atomic
from .models import ChannelBinding, ConversationThread, new_id
from .store_base import ConversationBaseStore
from .store_common import now as current_time


class ConversationThreadStore(ConversationBaseStore):
    def get_or_create_thread(self, request: dict) -> ConversationThread:
        existing = self.resolve_thread(channel=request.get("channel", ""), channel_conversation_id=request.get("channel_conversation_id", ""), channel_user_id=request.get("channel_user_id", ""))
        if existing is not None:
            return self._bind_existing(existing.thread_id, request)
        latest = self.latest_thread_for_user(request.get("canonical_user_id", "")) if request.get("reuse_latest_for_user") else None
        if latest is not None:
            return self._bind_existing(latest.thread_id, request)
        return self._create_thread(request)

    def resolve_thread(self, *, channel: str, channel_conversation_id: str, channel_user_id: str) -> ConversationThread | None:
        thread_id = str(self._read_bindings().get(_binding_key(channel, channel_conversation_id, channel_user_id)) or "")
        return self.load_thread(thread_id) if thread_id else None

    def latest_thread_for_user(self, canonical_user_id: str) -> ConversationThread | None:
        thread_id = str(read_json_file(self.user_latest_path).get(canonical_user_id) or "")
        return self.load_thread(thread_id) if thread_id else None

    def bind_channel(self, request: dict) -> ConversationThread:
        thread_id = str(request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        current = current_time(request.get("now"))
        binding = _channel_binding(thread.thread_id, current, request)
        updated = replace(thread, canonical_user_id=request.get("canonical_user_id") or thread.canonical_user_id, channel_bindings=_replace_binding(thread, binding), updated_at=current)
        self._write_thread(updated)
        self._write_binding_indexes(binding)
        return updated

    def list_threads(self, *, limit: int = 100) -> list[ConversationThread]:
        threads = [ConversationThread.from_dict(data) for data in self._thread_dicts()]
        threads.sort(key=lambda item: item.updated_at)
        return threads if limit <= 0 else threads[-limit:]

    def update_summary(self, thread_id: str, summary: str, *, now: float | None = None) -> ConversationThread:
        thread = self._require_thread(thread_id)
        updated = replace(thread, summary=summary, updated_at=now if now is not None else __import__("time").time())
        self._write_thread(updated)
        return updated

    def load_thread(self, thread_id: str) -> ConversationThread | None:
        if not thread_id:
            return None
        data = read_json_file(self._thread_path(thread_id))
        return ConversationThread.from_dict(data) if data else None

    def _require_thread(self, thread_id: str) -> ConversationThread:
        thread = self.load_thread(thread_id)
        if thread is None:
            raise KeyError(f"unknown conversation thread: {thread_id}")
        return thread

    def _write_thread(self, thread: ConversationThread) -> None:
        write_json_file_atomic(self._thread_path(thread.thread_id), thread.to_dict())

    def _read_bindings(self) -> dict[str, str]:
        return {str(key): str(value) for key, value in read_json_file(self.bindings_path).items()}

    def _bind_existing(self, thread_id: str, kwargs: dict) -> ConversationThread:
        return self.bind_channel({"thread_id": thread_id, "canonical_user_id": kwargs.get("canonical_user_id", ""), "channel": kwargs.get("channel", ""), "channel_conversation_id": kwargs.get("channel_conversation_id", ""), "channel_user_id": kwargs.get("channel_user_id", ""), "now": kwargs.get("now")})

    def _create_thread(self, kwargs: dict) -> ConversationThread:
        current = current_time(kwargs.get("now"))
        thread = ConversationThread(thread_id=new_id("thread"), canonical_user_id=kwargs.get("canonical_user_id", ""), title=kwargs.get("title", ""), created_at=current, updated_at=current)
        self._write_thread(thread)
        return self._bind_existing(thread.thread_id, {**kwargs, "now": current})

    def _write_binding_indexes(self, binding: ChannelBinding) -> None:
        key = _binding_key(binding.channel, binding.channel_conversation_id, binding.channel_user_id)
        update_json_file_atomic(self.bindings_path, lambda data: {**data, key: binding.thread_id})
        update_json_file_atomic(self.user_latest_path, lambda data: {**data, binding.canonical_user_id: binding.thread_id})

    def _thread_dicts(self) -> list[dict]:
        return [data for path in sorted(self.threads_dir.glob("*.json")) if (data := read_json_file(path))]


def _binding_key(channel: str, conversation_id: str, user_id: str) -> str:
    return "\x1f".join([str(channel), str(conversation_id), str(user_id)])


def _channel_binding(thread_id: str, current: float, kwargs: dict) -> ChannelBinding:
    return ChannelBinding(channel=kwargs.get("channel", ""), channel_conversation_id=kwargs.get("channel_conversation_id", ""), channel_user_id=kwargs.get("channel_user_id", ""), canonical_user_id=kwargs.get("canonical_user_id", ""), thread_id=thread_id, last_active_at=current)


def _replace_binding(thread: ConversationThread, binding: ChannelBinding) -> tuple[ChannelBinding, ...]:
    key = _binding_key(binding.channel, binding.channel_conversation_id, binding.channel_user_id)
    old = (item for item in thread.channel_bindings if _binding_key(item.channel, item.channel_conversation_id, item.channel_user_id) != key)
    return (*old, binding)
