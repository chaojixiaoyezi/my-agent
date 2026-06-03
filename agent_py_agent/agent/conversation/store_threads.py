
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..gateway_parts.io import update_json_file_atomic, write_json_file_atomic
from ..runtime_errors import DataCorruptionError, runtime_error_report
from .models import ChannelBinding, ConversationThread, new_id
from .store_base import ConversationBaseStore
from .store_common import now as current_time


class ConversationThreadStore(ConversationBaseStore):
    def get_or_create_thread(self, request: dict) -> ConversationThread:
        existing, binding_error = self.resolve_thread_report(channel=request.get("channel", ""), channel_conversation_id=request.get("channel_conversation_id", ""), channel_user_id=request.get("channel_user_id", ""))
        if binding_error is not None:
            raise DataCorruptionError(str(binding_error))
        if existing is not None:
            return self._bind_existing(existing.thread_id, request)
        latest = None
        if request.get("reuse_latest_for_user"):
            latest, latest_error = self.latest_thread_for_user_report(request.get("canonical_user_id", ""))
            if latest_error is not None:
                raise DataCorruptionError(str(latest_error))
        if latest is not None:
            return self._bind_existing(latest.thread_id, request)
        return self._create_thread(request)

    def resolve_thread(self, *, channel: str, channel_conversation_id: str, channel_user_id: str) -> ConversationThread | None:
        thread, _load_error = self.resolve_thread_report(
            channel=channel,
            channel_conversation_id=channel_conversation_id,
            channel_user_id=channel_user_id,
        )
        return thread

    def resolve_thread_report(
        self,
        *,
        channel: str,
        channel_conversation_id: str,
        channel_user_id: str,
    ) -> tuple[ConversationThread | None, dict[str, Any] | None]:
        bindings, error = self._read_bindings_report()
        if error is not None:
            return None, error
        thread_id = str(bindings.get(_binding_key(channel, channel_conversation_id, channel_user_id)) or "")
        return self.load_thread_report(thread_id) if thread_id else (None, None)

    def latest_thread_for_user(self, canonical_user_id: str) -> ConversationThread | None:
        thread, _load_error = self.latest_thread_for_user_report(canonical_user_id)
        return thread

    def latest_thread_for_user_report(self, canonical_user_id: str) -> tuple[ConversationThread | None, dict[str, Any] | None]:
        payload, error = _read_json_object_report(
            self.user_latest_path,
            context="conversation.user_latest.read",
        )
        if error is not None:
            return None, error
        thread_id = str(payload.get(canonical_user_id) or "")
        return self.load_thread_report(thread_id) if thread_id else (None, None)

    def bind_channel(self, request: dict) -> ConversationThread:
        thread_id = str(request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        current = current_time(request.get("now"))
        binding = _channel_binding(thread.thread_id, current, request)
        updated = replace(
            thread,
            canonical_user_id=request.get("canonical_user_id") or thread.canonical_user_id,
            owner_id=str(request.get("owner_id") or thread.owner_id or ""),
            owner_home=str(request.get("owner_home") or thread.owner_home or ""),
            channel_bindings=_replace_binding(thread, binding),
            updated_at=current,
        )
        self._write_thread(updated)
        self._write_binding_indexes(binding)
        return updated

    def list_threads(self, *, limit: int = 100) -> list[ConversationThread]:
        threads, _load_errors = self.list_threads_report(limit=limit)
        return threads

    def list_threads_report(self, *, limit: int = 100) -> tuple[list[ConversationThread], list[dict[str, Any]]]:
        threads: list[ConversationThread] = []
        load_errors: list[dict[str, Any]] = []
        for path in sorted(self.threads_dir.glob("*.json")):
            thread, error = self._load_thread_path_report(path)
            if thread is not None:
                threads.append(thread)
            if error is not None:
                load_errors.append(error)
        threads.sort(key=lambda item: item.updated_at)
        limited = threads if limit <= 0 else threads[-limit:]
        return limited, load_errors

    def update_summary(self, thread_id: str, summary: str, *, now: float | None = None) -> ConversationThread:
        thread = self._require_thread(thread_id)
        updated = replace(thread, summary=summary, updated_at=now if now is not None else __import__("time").time())
        self._write_thread(updated)
        return updated

    def load_thread(self, thread_id: str) -> ConversationThread | None:
        thread, _load_error = self.load_thread_report(thread_id)
        return thread

    def load_thread_report(self, thread_id: str) -> tuple[ConversationThread | None, dict[str, Any] | None]:
        if not thread_id:
            return None, None
        return self._load_thread_path_report(self._thread_path(thread_id), thread_id=thread_id)

    def _require_thread(self, thread_id: str) -> ConversationThread:
        thread = self.load_thread(thread_id)
        if thread is None:
            raise KeyError(f"unknown conversation thread: {thread_id}")
        return thread

    def _write_thread(self, thread: ConversationThread) -> None:
        write_json_file_atomic(self._thread_path(thread.thread_id), thread.to_dict())

    def _read_bindings(self) -> dict[str, str]:
        bindings, _load_error = self._read_bindings_report()
        return bindings

    def _read_bindings_report(self) -> tuple[dict[str, str], dict[str, Any] | None]:
        payload, error = _read_json_object_report(
            self.bindings_path,
            context="conversation.bindings.read",
        )
        if error is not None:
            return {}, error
        return {str(key): str(value) for key, value in payload.items()}, None

    def _bind_existing(self, thread_id: str, kwargs: dict) -> ConversationThread:
        return self.bind_channel({"thread_id": thread_id, "canonical_user_id": kwargs.get("canonical_user_id", ""), "channel": kwargs.get("channel", ""), "channel_conversation_id": kwargs.get("channel_conversation_id", ""), "channel_user_id": kwargs.get("channel_user_id", ""), "now": kwargs.get("now")})

    def _create_thread(self, kwargs: dict) -> ConversationThread:
        current = current_time(kwargs.get("now"))
        thread = ConversationThread(
            thread_id=new_id("thread"),
            canonical_user_id=kwargs.get("canonical_user_id", ""),
            owner_id=str(kwargs.get("owner_id") or ""),
            owner_home=str(kwargs.get("owner_home") or ""),
            title=kwargs.get("title", ""),
            created_at=current,
            updated_at=current,
        )
        self._write_thread(thread)
        return self._bind_existing(thread.thread_id, {**kwargs, "now": current})

    def _write_binding_indexes(self, binding: ChannelBinding) -> None:
        key = _binding_key(binding.channel, binding.channel_conversation_id, binding.channel_user_id)
        update_json_file_atomic(self.bindings_path, lambda data: {**data, key: binding.thread_id})
        update_json_file_atomic(self.user_latest_path, lambda data: {**data, binding.canonical_user_id: binding.thread_id})

    def _load_thread_path_report(
        self,
        path: Path,
        *,
        thread_id: str = "",
    ) -> tuple[ConversationThread | None, dict[str, Any] | None]:
        payload, error = _read_json_object_report(path, context="conversation.thread.read")
        actual_thread_id = str(thread_id or path.stem)
        if error is not None:
            error["thread_id"] = actual_thread_id
            return None, error
        if not payload:
            return None, None
        try:
            return ConversationThread.from_dict(payload), None
        except Exception as exc:
            report = runtime_error_report(exc, context="conversation.thread.read")
            report["thread_id"] = actual_thread_id
            report["path"] = str(path)
            return None, report


def _binding_key(channel: str, conversation_id: str, user_id: str) -> str:
    return "\x1f".join([str(channel), str(conversation_id), str(user_id)])


def _channel_binding(thread_id: str, current: float, kwargs: dict) -> ChannelBinding:
    return ChannelBinding(channel=kwargs.get("channel", ""), channel_conversation_id=kwargs.get("channel_conversation_id", ""), channel_user_id=kwargs.get("channel_user_id", ""), canonical_user_id=kwargs.get("canonical_user_id", ""), thread_id=thread_id, last_active_at=current)


def _replace_binding(thread: ConversationThread, binding: ChannelBinding) -> tuple[ChannelBinding, ...]:
    key = _binding_key(binding.channel, binding.channel_conversation_id, binding.channel_user_id)
    old = (item for item in thread.channel_bindings if _binding_key(item.channel, item.channel_conversation_id, item.channel_user_id) != key)
    return (*old, binding)


def _read_json_object_report(path: Path, *, context: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not path.exists():
        return {}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise DataCorruptionError(f"{path.name} is {type(payload).__name__}, expected object")
        return payload, None
    except Exception as exc:
        report = runtime_error_report(exc, context=context)
        report["path"] = str(path)
        return {}, report
