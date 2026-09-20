# LLM: 持久任务关联、活动索引与工作目录投影；终态通过注入能力关闭进度，不拥有执行器或另一份任务状态；保持 canonical 文件、锁和错误报告合同。
# 模块用途: 持久任务关联、活动索引与工作目录投影；终态通过注入能力关闭进度，不拥有执行器或另一份任务状态。
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common.json_io import jsonl_lines, write_text_file_atomic
from ..gateway_parts.io import (
    locked_file_transition,
    update_json_file_atomic,
)
from ..runtime_errors import DataCorruptionError, runtime_error_report

_STORE_LOGGER = logging.getLogger("agent.conversation.store")
from .models import (
    THREAD_TASK_LINK_INACTIVE_STATUSES,
    THREAD_TASK_LINK_NON_RESURRECTABLE_STATUSES,
    ConversationThread,
    MessageLogEntry,
    ThreadTaskLink,
)
from .store_io import (
    now,
    safe_file_stem,
)
from .store_layout import ConversationStorage
from .workspace_paths import validated_durable_work_path


# LLM: store_tasks 的持久化合同：保留既有命名比较规则，Audit 精确区分大小写，其它工作忽略大小写；修改须同步本领域调用方与存储回归。
# 函数用途: 保留既有命名比较规则，Audit 精确区分大小写，其它工作忽略大小写。
def named_work_name_matches(link: ThreadTaskLink, work_kind: str, work_name: str) -> bool:
    current = str(link.work_name or "")
    expected = str(work_name or "")
    return (
        current == expected if work_kind == "audit" else current.casefold() == expected.casefold()
    )


# LLM: store_tasks 的持久化合同：把任务身份加入线程历史和活动索引，保留原顺序及其它字段；修改须同步本领域调用方与存储回归。
# 函数用途: 把任务身份加入线程历史和活动索引，保留原顺序及其它字段。
def _thread_with_task(
    thread: ConversationThread, task_id: str, updated_at: float
) -> ConversationThread:
    task_ids = tuple(dict.fromkeys((*thread.task_ids, task_id)))
    active_task_ids = tuple(dict.fromkeys((*thread.active_task_ids, task_id)))
    return replace(
        thread,
        task_ids=task_ids,
        active_task_ids=active_task_ids,
        updated_at=updated_at,
    )


# LLM: store_tasks 的持久化合同：从活动索引移除任务并保留历史身份，避免终态继续进入热扫描；修改须同步本领域调用方与存储回归。
# 函数用途: 从活动索引移除任务并保留历史身份，避免终态继续进入热扫描。
def _thread_without_task(
    thread: ConversationThread, task_id: str, updated_at: float
) -> ConversationThread:
    task_ids = tuple(dict.fromkeys((*thread.task_ids, task_id)))
    active_task_ids = tuple(item for item in thread.active_task_ids if item != task_id)
    return replace(
        thread,
        task_ids=task_ids,
        active_task_ids=active_task_ids,
        updated_at=updated_at,
    )


# LLM: store_tasks 的持久化合同：读取精确任务关联并核验身份，保留坏账错误而不推断其它任务；修改须同步本领域调用方与存储回归。
# 函数用途: 读取精确任务关联并核验身份，保留坏账错误而不推断其它任务。
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


# LLM: conversation task link 是每次执行的生命周期权威；work/state.json 只是可复用
# task_path 当前执行的 owner-local 投影。旧 link 与当前投影身份不同是正常历史关系，
# 不能覆盖当前执行，也不应按数据损坏重复报警。
# 函数用途: 在当前任务链接变更后同步工作区状态，避免停止或完成后目录仍永久显示 RUNNING。
def sync_task_workspace_status(
    link: ThreadTaskLink,
    current: float,
    *,
    owner_home: str,
) -> None:
    task_path = str(link.task_path or "").strip()
    owner_path = str(owner_home or "").strip()
    if not task_path or not owner_path:
        return
    try:
        task_root = validated_durable_work_path(
            owner_path,
            task_path,
            str(link.work_kind or ""),
        )
        unresolved_state_path = task_root / "work" / "state.json"
        if unresolved_state_path.is_symlink() or unresolved_state_path.parent.is_symlink():
            raise ValueError("task workspace state path cannot use symbolic links")
        state_path = unresolved_state_path.resolve(strict=False)
        state_path.relative_to(task_root)
    except ValueError as exc:
        _STORE_LOGGER.warning(
            "task workspace state path rejected(task=%s): %s",
            link.task_id,
            exc,
        )
        return
    except (OSError, RuntimeError) as exc:
        _STORE_LOGGER.warning(
            "task workspace state path unavailable(task=%s): %s", link.task_id, exc
        )
        return
    if not state_path.is_file():
        return
    projected_status = _TASK_WORKSPACE_STATUS_BY_LINK.get(
        str(link.status or "").strip().lower(),
        str(link.status or "UNKNOWN").strip().upper(),
    )

    # LLM: store_tasks 的持久化合同：只把已核验任务身份和结构化状态写入工作区状态投影；修改须同步本领域调用方与存储回归。
    # 函数用途: 只把已核验任务身份和结构化状态写入工作区状态投影。
    def updater(data: dict[str, Any]) -> dict[str, Any]:
        if not data:
            raise DataCorruptionError(f"task workspace state is unreadable: {link.task_id}")
        state_task_id = str(data.get("task_id") or "").strip()
        if state_task_id and state_task_id != link.task_id:
            return data
        updated = dict(data)
        updated["task_id"] = link.task_id
        updated["status"] = projected_status
        updated["current_step"] = projected_status
        updated["updated_at"] = datetime.fromtimestamp(current, timezone.utc).isoformat()
        return updated

    try:
        updated_state = update_json_file_atomic(state_path, updater, require_existing=True)
        _sync_task_workspace_summary_status(
            task_root,
            state_path.parent / "summaries" / "current_summary.md",
            updated_state,
        )
    except (DataCorruptionError, OSError, TypeError, ValueError) as exc:
        _STORE_LOGGER.warning(
            "task workspace state sync failed(task=%s,status=%s): %s",
            link.task_id,
            projected_status,
            exc,
        )


# LLM: current_summary.md is a derived human/model view of canonical state.json. This writer may
# mirror typed lifecycle fields only; it must never parse the old prose to decide status or task
# completion, and it must reject symlink escapes from the validated task root.
# 函数用途: 任务状态切换后原子刷新摘要里的 status/current_step，避免 Compact 读到旧占位状态。
def _sync_task_workspace_summary_status(
    task_root: Path,
    summary_path: Path,
    state: dict[str, Any],
) -> None:
    if not summary_path.is_file():
        return
    if summary_path.is_symlink() or summary_path.parent.is_symlink():
        raise ValueError("task workspace summary path cannot use symbolic links")
    summary_path.resolve(strict=False).relative_to(task_root)
    status = str(state.get("status") or "UNKNOWN").strip().upper() or "UNKNOWN"
    # 这个函数会按行重写文件，所以边界必须与写回时用的 "\n" 一致：用 splitlines() 会把
    # 摘要正文里的 NEL/U+2028 等字符当成换行切开，重写时又被落成 LF——静默改写任务产物。
    lines = list(jsonl_lines(summary_path.read_text(encoding="utf-8")))
    next_lines: list[str] = []
    status_found = False
    step_found = False
    for line in lines:
        if line.startswith("- status:"):
            next_lines.append(f"- status: {status}")
            status_found = True
        elif line.startswith("- current_step:"):
            next_lines.append(f"- current_step: {status}")
            step_found = True
        else:
            next_lines.append(line)
    if not status_found or not step_found:
        return
    next_text = "\n".join(next_lines) + ("\n" if lines else "")
    current_text = summary_path.read_text(encoding="utf-8")
    if next_text != current_text:
        write_text_file_atomic(summary_path, next_text)


# LLM: store_tasks 的持久化合同：合并任务重复绑定，保持终态不复活、身份及已发布路径不被迟到请求覆盖；修改须同步本领域调用方与存储回归。
# 函数用途: 合并任务重复绑定，保持终态不复活、身份及已发布路径不被迟到请求覆盖。
def _merged_task_link(
    data: dict[str, Any],
    *,
    request: dict,
    thread_id: str,
    task_id: str,
    current: float,
    path_exists: bool,
) -> ThreadTaskLink:
    requested_duration = (
        max(1, int(request["duration_seconds"]))
        if request.get("duration_seconds") is not None
        else None
    )
    requested_expires_at = (
        float(request["expires_at"])
        if request.get("expires_at") is not None
        else current + requested_duration
        if requested_duration is not None
        else None
    )
    if not data:
        if path_exists:
            raise DataCorruptionError(f"conversation task link is unreadable: {task_id}")
        return _new_task_link(
            request,
            thread_id=thread_id,
            task_id=task_id,
            current=current,
            requested_duration=requested_duration,
            requested_expires_at=requested_expires_at,
        )
    existing = ThreadTaskLink.from_dict(data)
    if not existing.thread_id or existing.task_id != task_id:
        raise DataCorruptionError(f"conversation task link identity is invalid: {task_id}")
    if existing.thread_id != thread_id:
        raise ValueError(f"task {task_id} is already bound to another conversation thread")
    requested_status = str(request.get("status") or "").strip()
    # bind_task is an idempotent identity/path upsert, not a lifecycle reopen API.
    # A gateway retry, process restart, late runner callback, or repeated workspace
    # materialization may bind the same task again with its old "active" snapshot.
    # Once /stop or another terminal transition has landed, that stale bind must
    # never resurrect the task.  Deliberate reopen goes through update_task_status
    # (bind_current_conversation_workspace), where the caller names the exact task.
    existing_status = str(existing.status or "").strip()
    merged_status = (
        existing_status
        if existing_status.lower() in THREAD_TASK_LINK_NON_RESURRECTABLE_STATUSES
        else requested_status or existing_status
    )
    return _updated_task_link(
        existing,
        request,
        merged_status=merged_status,
        current=current,
        requested_duration=requested_duration,
        requested_expires_at=requested_expires_at,
    )


# LLM: store_tasks 的持久化合同：从首次绑定请求构造原任务记录，不触发文件写入或启动执行器；修改须同步本领域调用方与存储回归。
# 函数用途: 从首次绑定请求构造原任务记录，不触发文件写入或启动执行器。
def _new_task_link(
    request: dict,
    *,
    thread_id: str,
    task_id: str,
    current: float,
    requested_duration: int | None,
    requested_expires_at: float | None,
) -> ThreadTaskLink:
    return ThreadTaskLink(
        thread_id=thread_id,
        task_id=task_id,
        goal=str(request.get("goal") or ""),
        status=str(request.get("status") or "active"),
        created_at=current,
        task_path=str(request.get("task_path") or ""),
        work_kind=str(request.get("work_kind") or ""),
        work_name=str(request.get("work_name") or ""),
        duration_seconds=requested_duration,
        expires_at=requested_expires_at,
        cancellation_scope=str(request.get("cancellation_scope") or "foreground"),
        context_anchor_message_id=str(request.get("context_anchor_message_id") or ""),
        pending_prompt=str(request.get("pending_prompt") or ""),
        pending_updated_at=float(request.get("pending_updated_at") or 0.0),
        pending_prepare_request_id=str(request.get("pending_prepare_request_id") or ""),
        effective_user_prompt=str(request.get("effective_user_prompt") or ""),
        effective_revision=max(0, int(request.get("effective_revision") or 0)),
        effective_updated_at=float(request.get("effective_updated_at") or 0.0),
        effective_prepare_request_id=str(request.get("effective_prepare_request_id") or ""),
        effective_evidence_refs=tuple(
            str(item) for item in (request.get("effective_evidence_refs") or []) if str(item)
        ),
        effective_source_bindings=tuple(
            dict(item)
            for item in (request.get("effective_source_bindings") or [])
            if isinstance(item, dict)
        ),
        run_epoch=max(0, int(request.get("run_epoch") or 0)),
        run_prompt=str(request.get("run_prompt") or ""),
    )


# LLM: store_tasks 的持久化合同：合并已有任务的允许字段，保留原路径、时限和发布修订的优先关系；修改须同步本领域调用方与存储回归。
# 函数用途: 合并已有任务的允许字段，保留原路径、时限和发布修订的优先关系。
def _updated_task_link(
    existing: ThreadTaskLink,
    request: dict,
    *,
    merged_status: str,
    current: float,
    requested_duration: int | None,
    requested_expires_at: float | None,
) -> ThreadTaskLink:
    return replace(
        existing,
        goal=existing.goal or str(request.get("goal") or ""),
        status=merged_status,
        created_at=existing.created_at or current,
        task_path=existing.task_path or str(request.get("task_path") or ""),
        work_kind=existing.work_kind or str(request.get("work_kind") or ""),
        work_name=existing.work_name or str(request.get("work_name") or ""),
        duration_seconds=existing.duration_seconds
        if existing.duration_seconds is not None
        else requested_duration,
        expires_at=existing.expires_at if existing.expires_at is not None else requested_expires_at,
        cancellation_scope=(
            existing.cancellation_scope
            if existing.cancellation_scope != "foreground"
            else str(request.get("cancellation_scope") or existing.cancellation_scope)
        ),
        context_anchor_message_id=existing.context_anchor_message_id
        or str(request.get("context_anchor_message_id") or ""),
        pending_prompt=existing.pending_prompt or str(request.get("pending_prompt") or ""),
        pending_updated_at=existing.pending_updated_at
        or float(request.get("pending_updated_at") or 0.0),
        pending_prepare_request_id=existing.pending_prepare_request_id
        or str(request.get("pending_prepare_request_id") or ""),
        effective_user_prompt=existing.effective_user_prompt
        or str(request.get("effective_user_prompt") or ""),
        effective_revision=max(
            existing.effective_revision, int(request.get("effective_revision") or 0)
        ),
        effective_updated_at=existing.effective_updated_at
        or float(request.get("effective_updated_at") or 0.0),
        effective_prepare_request_id=existing.effective_prepare_request_id
        or str(request.get("effective_prepare_request_id") or ""),
        effective_evidence_refs=existing.effective_evidence_refs
        or tuple(str(item) for item in (request.get("effective_evidence_refs") or []) if str(item)),
        effective_source_bindings=existing.effective_source_bindings
        or tuple(
            dict(item)
            for item in (request.get("effective_source_bindings") or [])
            if isinstance(item, dict)
        ),
        run_epoch=max(existing.run_epoch, int(request.get("run_epoch") or 0)),
        run_prompt=existing.run_prompt or str(request.get("run_prompt") or ""),
    )


_TASK_WORKSPACE_STATUS_BY_LINK = {
    "abandoned": "ABANDONED",
    "active": "RUNNING",
    "blocked": "BLOCKED",
    "cancelled": "CANCELLED",
    "channel_error": "CHANNEL_ERROR",
    "completed": "DONE",
    "done": "DONE",
    "failed": "FAILED",
    "interrupted": "PAUSED",
    "superseded": "ABANDONED",
    "taken_over": "TAKEN_OVER",
    "timeout": "TIMEOUT",
}


# LLM: 持久任务关联、活动索引与工作目录投影；终态通过注入能力关闭进度，不拥有执行器或另一份任务状态；修改须核对直接调用方与原子存储测试。
# 类用途: 持久任务关联、活动索引与工作目录投影；终态通过注入能力关闭进度，不拥有执行器或另一份任务状态。
class TaskStore:
    # LLM: 线程和消息只经显式能力访问；终态进度关闭为必需依赖，不能靠隐藏继承或可选探测悄悄跳过。
    # 函数用途: 组装任务持久化所需的读取及收尾能力，不创建任务、不改变目录或进度状态。
    def __init__(
        self,
        storage: ConversationStorage,
        *,
        require_thread: Callable[[str], ConversationThread],
        load_thread_report: Callable[
            [str], tuple[ConversationThread | None, dict[str, Any] | None]
        ],
        recent_messages_report: Callable[..., tuple[list[MessageLogEntry], list[dict[str, Any]]]],
        disable_task_progress_policies: Callable[..., tuple[str, ...]],
    ) -> None:
        self.storage = storage
        self._require_thread = require_thread
        self._load_thread_report = load_thread_report
        self._recent_messages_report = recent_messages_report
        self._disable_task_progress_policies = disable_task_progress_policies

    # LLM: store_tasks 的持久化合同：返回停止、引导和完成共用的精确任务锁，不在此执行状态变更；修改须同步本领域调用方与存储回归。
    # 函数用途: 返回停止、引导和完成共用的精确任务锁，不在此执行状态变更。
    def transition_guard(self, task_id: str):
        """Return the cross-process lock shared by steer, stop, and completion."""
        normalized = safe_file_stem(str(task_id or ""))
        if not normalized:
            raise ValueError("task_id is required")
        return locked_file_transition(self.storage.tasks_dir / f".{normalized}.transition")

    # LLM: store_tasks 的持久化合同：按线程、种类和名称持有原命名锁，串行同名任务创建及重启；修改须同步本领域调用方与存储回归。
    # 函数用途: 按线程、种类和名称持有原命名锁，串行同名任务创建及重启。
    def named_work_transition_guard(
        self,
        thread_id: str,
        work_kind: str,
        work_name: str,
    ):
        """Serialize one exact named-work identity across task ids.

        A pre-upgrade conversation may contain more than one terminal link with
        the same Audit name.  Reopening one of those identities must share the
        same lock as first creation, otherwise two concurrent starts could each
        resurrect a different historical task.
        """

        selected_thread = safe_file_stem(str(thread_id or ""))
        selected_kind = safe_file_stem(str(work_kind or "").strip().lower())
        selected_name = str(work_name or "").strip()
        if not selected_thread or not selected_kind or not selected_name:
            raise ValueError("thread_id, work_kind and work_name are required")
        return locked_file_transition(
            self.storage.tasks_dir
            / (
                f".named.{selected_thread}.{selected_kind}."
                f"{hashlib.sha256(selected_name.encode('utf-8')).hexdigest()[:20]}"
            )
        )

    # LLM: store_tasks 的持久化合同：在原线程文件锁内合并任务索引，避免并发任务互相覆盖；修改须同步本领域调用方与存储回归。
    # 函数用途: 在原线程文件锁内合并任务索引，避免并发任务互相覆盖。
    def update_thread_index(
        self,
        thread_id: str,
        task_id: str,
        status: str,
        current: float,
    ) -> ConversationThread:
        # LLM: store_tasks 的持久化合同：持锁校验线程身份后按任务状态合并活动索引，保留并发字段；修改须同步本领域调用方与存储回归。
        # 函数用途: 持锁校验线程身份后按任务状态合并活动索引，保留并发字段。
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            if not data:
                raise DataCorruptionError(f"conversation thread is unreadable: {thread_id}")
            latest = ConversationThread.from_dict(data)
            if latest.thread_id != thread_id:
                raise DataCorruptionError(f"conversation thread identity is invalid: {thread_id}")
            updated = (
                _thread_without_task(latest, task_id, current)
                if str(status or "").strip().lower() in THREAD_TASK_LINK_INACTIVE_STATUSES
                else _thread_with_task(latest, task_id, current)
            )
            return updated.to_dict()

        payload = update_json_file_atomic(
            self.storage.thread_path(thread_id),
            updater,
            require_existing=True,
        )
        return ConversationThread.from_dict(payload)

    # LLM: store_tasks 的持久化合同：幂等保存任务关联，再更新线程索引和工作区投影；命名检查与写入共用原锁；修改须同步本领域调用方与存储回归。
    # 函数用途: 幂等保存任务关联，再更新线程索引和工作区投影；命名检查与写入共用原锁。
    def bind(self, request: dict) -> ThreadTaskLink:
        thread_id = str(request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        task_id = str(request.get("task_id") or "")
        if not task_id:
            raise ValueError("task_id is required")
        current = now(request.get("now"))
        work_kind = str(request.get("work_kind") or "").strip().lower()
        work_name = str(request.get("work_name") or "").strip()
        cancellation_scope = str(request.get("cancellation_scope") or "foreground").strip().lower()
        request = dict(request)
        if (
            work_kind in {"audit", "goal"}
            and work_name
            and cancellation_scope == "detached"
            and "context_anchor_message_id" not in request
        ):
            messages, message_errors = self._recent_messages_report(
                thread.thread_id,
                limit=1,
            )
            if message_errors:
                raise DataCorruptionError(
                    "conversation transcript is unavailable for detached task binding"
                )
            request["context_anchor_message_id"] = messages[-1].message_id if messages else ""
        guard = (
            self.named_work_transition_guard(thread.thread_id, work_kind, work_name)
            if work_kind in {"audit", "goal"} and work_name
            else nullcontext()
        )
        with guard:
            if work_kind and work_name:
                links, errors = self.list_report(thread.thread_id)
                if errors:
                    raise DataCorruptionError("conversation task links are unavailable")
                duplicate = next(
                    (
                        link
                        for link in links
                        if link.task_id != task_id
                        and str(link.work_kind or "").strip().lower() == work_kind
                        and named_work_name_matches(link, work_kind, work_name)
                        and str(link.status or "").strip().lower()
                        not in THREAD_TASK_LINK_INACTIVE_STATUSES
                    ),
                    None,
                )
                if duplicate is not None:
                    raise ValueError(f"an active {work_kind} named {work_name!r} already exists")
            path = self.storage.task_path(task_id)
            payload = update_json_file_atomic(
                path,
                lambda data: _merged_task_link(
                    data,
                    request=request,
                    thread_id=thread.thread_id,
                    task_id=task_id,
                    current=current,
                    path_exists=path.exists(),
                ).to_dict(),
            )
            link = ThreadTaskLink.from_dict(payload)
            indexed_thread = self.update_thread_index(
                thread.thread_id,
                task_id,
                link.status,
                current,
            )
            sync_task_workspace_status(link, current, owner_home=indexed_thread.owner_home)
            return link

    # LLM: This is the only durable writer for the thread's latest task projection. It validates
    # exact identity but grants no execution/cwd authority to a later ordinary turn.
    # 函数用途: 记住会话最近一次任务，供状态和导航显示；不会让后续普通消息自动进入旧目录。
    def select_workspace_task(self, request: dict) -> ConversationThread:
        thread_id = str(request.get("thread_id") or "").strip()
        task_id = str(request.get("task_id") or "").strip()
        if not thread_id or not task_id:
            raise ValueError("thread_id and task_id are required")
        thread = self._require_thread(thread_id)
        link, error = _read_task_link(
            self.storage.task_path(task_id),
            task_id,
            context="conversation.workspace_task.read",
        )
        if error is not None or link is None:
            raise DataCorruptionError(
                str(
                    (error or {}).get("message")
                    or f"conversation task link is unavailable: {task_id}"
                )
            )
        if link.thread_id != thread.thread_id:
            raise ValueError(f"task {task_id} is not bound to conversation thread {thread_id}")
        current = now(request.get("now"))

        # LLM: store_tasks 的持久化合同：只更新已被线程索引的任务显示指针，不授予新的执行目录权限；修改须同步本领域调用方与存储回归。
        # 函数用途: 只更新已被线程索引的任务显示指针，不授予新的执行目录权限。
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            if not data:
                raise DataCorruptionError(f"conversation thread is unreadable: {thread_id}")
            latest = ConversationThread.from_dict(data)
            if latest.thread_id != thread_id:
                raise DataCorruptionError(f"conversation thread identity is invalid: {thread_id}")
            if task_id not in latest.task_ids:
                raise ValueError(
                    f"task {task_id} is not indexed by conversation thread {thread_id}"
                )
            return replace(
                latest,
                workspace_task_id=task_id,
                updated_at=max(latest.updated_at, current),
            ).to_dict()

        payload = update_json_file_atomic(
            self.storage.thread_path(thread_id),
            updater,
            require_existing=True,
        )
        return ConversationThread.from_dict(payload)

    # LLM: store_tasks 的持久化合同：读取线程全部历史任务关联，需要坏账信息时使用 list_report；修改须同步本领域调用方与存储回归。
    # 函数用途: 读取线程全部历史任务关联，需要坏账信息时使用 list_report。
    def list(self, thread_id: str) -> list[ThreadTaskLink]:
        links, _load_errors = self.list_report(thread_id)
        return links

    # LLM: store_tasks 的持久化合同：读取精确任务关联，沿原合同把损坏记录抛为 DataCorruptionError；修改须同步本领域调用方与存储回归。
    # 函数用途: 读取精确任务关联，沿原合同把损坏记录抛为 DataCorruptionError。
    def load(self, task_id: str) -> ThreadTaskLink | None:
        link, error = self.load_report(task_id)
        if error is not None:
            raise DataCorruptionError(
                str(error.get("message") or "conversation task link read failed")
            )
        return link

    # LLM: store_tasks 的持久化合同：按非空任务身份读取原文件，不存在时返回空结果，不创建记录；修改须同步本领域调用方与存储回归。
    # 函数用途: 按非空任务身份读取原文件，不存在时返回空结果，不创建记录。
    def load_report(
        self,
        task_id: str,
    ) -> tuple[ThreadTaskLink | None, dict[str, Any] | None]:
        selected = str(task_id or "").strip()
        if not selected:
            return None, None
        path = self.storage.task_path(selected)
        if not path.exists():
            return None, None
        return _read_task_link(path, selected)

    # LLM: store_tasks 的持久化合同：从线程历史任务身份读取关联及错误，不扫描其它线程；修改须同步本领域调用方与存储回归。
    # 函数用途: 从线程历史任务身份读取关联及错误，不扫描其它线程。
    def list_report(self, thread_id: str) -> tuple[list[ThreadTaskLink], list[dict[str, Any]]]:
        thread = self._require_thread(thread_id)
        return self._links_for_ids(thread.task_ids)

    # LLM: store_tasks 的持久化合同：从线程活动索引读取候选任务，完成记录不因目录扫描重新进入列表；修改须同步本领域调用方与存储回归。
    # 函数用途: 从线程活动索引读取候选任务，完成记录不因目录扫描重新进入列表。
    def active_report(self, thread_id: str) -> tuple[list[ThreadTaskLink], list[dict[str, Any]]]:
        thread = self._require_thread(thread_id)
        return self._links_for_ids(thread.active_task_ids)

    # LLM: store_tasks 的持久化合同：按索引顺序加载任务文件并汇总错误，不改变原记录或索引；修改须同步本领域调用方与存储回归。
    # 函数用途: 按索引顺序加载任务文件并汇总错误，不改变原记录或索引。
    def _links_for_ids(
        self, task_ids: tuple[str, ...]
    ) -> tuple[list[ThreadTaskLink], list[dict[str, Any]]]:
        links: list[ThreadTaskLink] = []
        load_errors: list[dict[str, Any]] = []
        for task_id in task_ids:
            link, error = _read_task_link(self.storage.task_path(task_id), str(task_id))
            if link is not None:
                links.append(link)
            if error is not None:
                load_errors.append(error)
        return links, load_errors

    # LLM: store_tasks 的持久化合同：按精确任务反查线程，坏账沿原合同抛错；修改须同步本领域调用方与存储回归。
    # 函数用途: 按精确任务反查线程，坏账沿原合同抛错。
    def thread_for(self, task_id: str) -> ConversationThread | None:
        thread, load_error = self.thread_for_report(task_id)
        if load_error is not None:
            raise DataCorruptionError(
                str(load_error.get("message") or "conversation task link read failed")
            )
        return thread

    # LLM: store_tasks 的持久化合同：从任务关联的权威线程身份反查记录，保留两段读取的错误报告；修改须同步本领域调用方与存储回归。
    # 函数用途: 从任务关联的权威线程身份反查记录，保留两段读取的错误报告。
    def thread_for_report(
        self, task_id: str
    ) -> tuple[ConversationThread | None, dict[str, Any] | None]:
        path = self.storage.task_path(task_id)
        if not path.exists():
            return None, None
        link, error = _read_task_link(path, task_id, context="conversation.thread_for_task")
        if error is not None:
            return None, error
        if link is None:
            return None, None
        return self._load_thread_report(link.thread_id)

    # LLM: store_tasks 的持久化合同：通过任务状态 CAS 提交变更，再刷新索引和工作区，终态最后关闭进度策略；修改须同步本领域调用方与存储回归。
    # 函数用途: 通过任务状态 CAS 提交变更，再刷新索引和工作区，终态最后关闭进度策略。
    def update_status(self, request: dict) -> ThreadTaskLink | None:
        task_id = str(request.get("task_id") or "")
        path = self.storage.task_path(task_id)
        if not path.exists():
            return None
        current = now(request.get("now"))
        requested_status = str(request.get("status") or "active")
        expected_status = str(request.get("expected_status") or "").strip().lower()
        updated = False

        # LLM: store_tasks 的持久化合同：校验任务身份及 expected_status 后更新状态，CAS 不匹配保持原记录；修改须同步本领域调用方与存储回归。
        # 函数用途: 校验任务身份及 expected_status 后更新状态，CAS 不匹配保持原记录。
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal updated
            if not data:
                raise DataCorruptionError(f"conversation task link is unreadable: {task_id}")
            link_current = ThreadTaskLink.from_dict(data)
            if not link_current.thread_id or link_current.task_id != task_id:
                raise DataCorruptionError(f"conversation task link identity is invalid: {task_id}")
            if (
                expected_status
                and str(link_current.status or "").strip().lower() != expected_status
            ):
                return data
            updated = True
            return replace(link_current, status=requested_status).to_dict()

        payload = update_json_file_atomic(path, updater, require_existing=True)
        if not updated:
            return None
        link = ThreadTaskLink.from_dict(payload)
        # active_task_ids 是候选任务索引，不是历史归档。终态链接保留在
        # tasks/<id>.json 供精确反查，但必须从热索引移除，避免普通聊天
        # 每轮扫描并注入越来越多已完成工作。索引更新也在文件锁内合并，
        # 避免多个子任务同时绑定/结束时彼此覆盖 thread.task_ids。
        indexed_thread = self.update_thread_index(link.thread_id, task_id, link.status, current)
        sync_task_workspace_status(link, current, owner_home=indexed_thread.owner_home)
        if str(link.status or "").strip().lower() in THREAD_TASK_LINK_NON_RESURRECTABLE_STATUSES:
            self._disable_task_progress_policies(task_id, now=current)
        return link

    # LLM: Goal edits update display intent only and preserve task/thread/workspace identity.
    # 函数用途: 精确修改一个持久任务的用户可见目标文本。
    def update_goal(self, request: dict) -> ThreadTaskLink | None:
        """Update only the user-visible goal of one exact durable task link."""
        task_id = str(request.get("task_id") or "").strip()
        goal = str(request.get("goal") or "").strip()
        path = self.storage.task_path(task_id)
        if not task_id or not goal or not path.exists():
            return None
        updated = False

        # LLM: store_tasks 的持久化合同：仅修改精确任务的目标文本，身份错误时拒绝提交；修改须同步本领域调用方与存储回归。
        # 函数用途: 仅修改精确任务的目标文本，身份错误时拒绝提交。
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal updated
            if not data:
                raise DataCorruptionError(f"conversation task link is unreadable: {task_id}")
            current = ThreadTaskLink.from_dict(data)
            if not current.thread_id or current.task_id != task_id:
                raise DataCorruptionError(f"conversation task link identity is invalid: {task_id}")
            updated = True
            return replace(current, goal=goal).to_dict()

        payload = update_json_file_atomic(path, updater, require_existing=True)
        return ThreadTaskLink.from_dict(payload) if updated else None
