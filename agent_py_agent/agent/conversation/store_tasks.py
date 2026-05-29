# LLM: Task bindings link long-running work to a conversation thread.
# 模块用途: 维护 thread 与 task 的轻量绑定关系。

from __future__ import annotations

from dataclasses import replace

from ..gateway_parts.io import read_json_file, write_json_file_atomic
from .models import ConversationThread, ThreadTaskLink
from .store_common import now as current_time
from .store_messages import ConversationMessageStore


class ConversationTaskStore(ConversationMessageStore):
    def bind_task(self, request: dict) -> ThreadTaskLink:
        thread_id = str(request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        task_id = str(request.get("task_id") or "")
        link = ThreadTaskLink(thread_id=thread.thread_id, task_id=task_id, goal=str(request.get("goal") or ""), status=str(request.get("status") or "active"), created_at=current_time(request.get("now")))
        write_json_file_atomic(self._task_path(task_id), link.to_dict())
        self._write_thread(_thread_with_task(thread, task_id, link.created_at))
        return link

    def task_links(self, thread_id: str) -> list[ThreadTaskLink]:
        thread = self._require_thread(thread_id)
        return [ThreadTaskLink.from_dict(data) for task_id in thread.active_task_ids if (data := read_json_file(self._task_path(task_id)))]

    def thread_for_task(self, task_id: str) -> ConversationThread | None:
        data = read_json_file(self._task_path(task_id))
        return self.load_thread(ThreadTaskLink.from_dict(data).thread_id) if data else None

    def update_task_status(self, request: dict) -> ThreadTaskLink | None:
        task_id = str(request.get("task_id") or "")
        data = read_json_file(self._task_path(task_id))
        if not data:
            return None
        current = current_time(request.get("now"))
        link = replace(ThreadTaskLink.from_dict(data), status=str(request.get("status") or "active"))
        write_json_file_atomic(self._task_path(task_id), link.to_dict())
        if thread := self.load_thread(link.thread_id):
            self._write_thread(replace(thread, updated_at=current))
        return link


def _thread_with_task(thread: ConversationThread, task_id: str, updated_at: float) -> ConversationThread:
    task_ids = tuple(dict.fromkeys((*thread.active_task_ids, task_id)))
    return replace(thread, active_task_ids=task_ids, updated_at=updated_at)
