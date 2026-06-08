
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from ..gateway_parts.io import read_json_file, write_json_file_atomic
from ..runtime_errors import DataCorruptionError, runtime_error_report
from .models import ConversationThread, ThreadTaskLink
from .store_common import now as current_time
from .store_messages import ConversationMessageStore


class ConversationTaskStore(ConversationMessageStore):
    def bind_task(self, request: dict) -> ThreadTaskLink:
        thread_id = str(request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        task_id = str(request.get("task_id") or "")
        link = ThreadTaskLink(
            thread_id=thread.thread_id,
            task_id=task_id,
            goal=str(request.get("goal") or ""),
            status=str(request.get("status") or "active"),
            created_at=current_time(request.get("now")),
            task_path=str(request.get("task_path") or ""),
        )
        write_json_file_atomic(self._task_path(task_id), link.to_dict())
        self._write_thread(_thread_with_task(thread, task_id, link.created_at))
        return link

    def task_links(self, thread_id: str) -> list[ThreadTaskLink]:
        links, _load_errors = self.task_links_report(thread_id)
        return links

    def task_links_report(self, thread_id: str) -> tuple[list[ThreadTaskLink], list[dict[str, Any]]]:
        thread = self._require_thread(thread_id)
        links: list[ThreadTaskLink] = []
        load_errors: list[dict[str, Any]] = []
        for task_id in thread.active_task_ids:
            link, error = _read_task_link(self._task_path(task_id), str(task_id))
            if link is not None:
                links.append(link)
            if error is not None:
                load_errors.append(error)
        return links, load_errors

    def thread_for_task(self, task_id: str) -> ConversationThread | None:
        thread, load_error = self.thread_for_task_report(task_id)
        if load_error is not None:
            raise DataCorruptionError(str(load_error.get("message") or "conversation task link read failed"))
        return thread

    def thread_for_task_report(self, task_id: str) -> tuple[ConversationThread | None, dict[str, Any] | None]:
        path = self._task_path(task_id)
        if not path.exists():
            return None, None
        link, error = _read_task_link(path, task_id, context="conversation.thread_for_task")
        if error is not None:
            return None, error
        if link is None:
            return None, None
        return self.load_thread_report(link.thread_id)

    def update_task_status(self, request: dict) -> ThreadTaskLink | None:
        task_id = str(request.get("task_id") or "")
        path = self._task_path(task_id)
        if not path.exists():
            return None
        link_current, load_error = _read_task_link(path, task_id, context="conversation.update_task_status")
        if load_error is not None:
            raise DataCorruptionError(str(load_error.get("message") or "conversation task link read failed"))
        if link_current is None:
            return None
        current = current_time(request.get("now"))
        link = replace(link_current, status=str(request.get("status") or "active"))
        write_json_file_atomic(self._task_path(task_id), link.to_dict())
        if thread := self.load_thread(link.thread_id):
            self._write_thread(replace(thread, updated_at=current))
        return link


def _thread_with_task(thread: ConversationThread, task_id: str, updated_at: float) -> ConversationThread:
    task_ids = tuple(dict.fromkeys((*thread.active_task_ids, task_id)))
    return replace(thread, active_task_ids=task_ids, updated_at=updated_at)


def _read_task_link(
    path,
    task_id: str,
    *,
    context: str = "conversation.task_link.read",
) -> tuple[ThreadTaskLink | None, dict[str, Any] | None]:
    try:
        if not path.exists():
            raise FileNotFoundError(str(path))
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"conversation task link is {type(payload).__name__}, expected object")
        return ThreadTaskLink.from_dict(payload), None
    except Exception as exc:
        report = runtime_error_report(exc, context=context)
        report["task_id"] = task_id
        report["path"] = str(path)
        return None, report
