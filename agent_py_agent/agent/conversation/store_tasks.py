# LLM: 任务关联、索引、选择CAS共用原文件；选择必须先领取再I/O，执行权由本地callback核验，不拥有执行器或另一份状态。
# 模块用途: 保存任务、工作区投影和一次选择标记；终态沿原能力关闭进度，坏选择标记不影响普通任务读取。
from __future__ import annotations

import hashlib
import json
import logging
import uuid
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
from .capability_selection_state import TaskCapabilitySelection

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


# LLM: 请求 task_id 必须与原文件 payload.task_id 精确相同，即便 thread 相同也不能串账；失败沿原 load_report 坏账错误返回。
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
        link = ThreadTaskLink.from_dict(payload)
        if link.task_id != task_id:
            raise ValueError("conversation task link identity does not match requested task")
        return link, None
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


# LLM: 重复绑定保留终态/身份/已发布路径及选择marker；typed pending只传给真正新建分支，不为旧任务补资格。
# 函数用途: 合并原任务绑定，防止迟到请求复活状态、覆盖上下文或重开一次选择。
def _merged_task_link(
    data: dict[str, Any],
    *,
    request: dict,
    thread_id: str,
    task_id: str,
    current: float,
    path_exists: bool,
    capability_selection: TaskCapabilitySelection | None = None,
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
            capability_selection=capability_selection,
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


# LLM: 仅真正新建分支接收宿主typed pending；request中的同名键从不消费，不能给旧任务补资格；本函数无I/O。
# 函数用途: 从首次绑定请求构造任务，可写入已由宿主确认资格的一次选择初值。
def _new_task_link(
    request: dict,
    *,
    thread_id: str,
    task_id: str,
    current: float,
    requested_duration: int | None,
    requested_expires_at: float | None,
    capability_selection: TaskCapabilitySelection | None = None,
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
        capability_selection=capability_selection,
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


# LLM: CAS未命中以内部信号退出原JSON更新器，避免旧/坏标记被无意义重写；只有本类型可被选择事务吞掉。
# 类用途: 表达一次选择未发生变更，不表示任务错误，也不触发任何重试。
class _CapabilitySelectionSkipped(Exception):
    pass


# LLM: 原transition锁先于JSON锁；callback仅允许本地执行权读取，不能重入任务锁、写状态或做网络I/O。
# 函数用途: 对同一任务执行一次状态CAS；缺标记/非active/执行权失效跳过，其它未知异常不被吞掉。
def _transition_capability_selection(
    store: TaskStore, *, task_id: str, thread_id: str, expected: TaskCapabilitySelection,
    updated: TaskCapabilitySelection, authority_claim: TaskCapabilitySelection,
    execution_is_current: Callable[[ThreadTaskLink, TaskCapabilitySelection], bool],
) -> TaskCapabilitySelection | None:
    if not task_id or not thread_id or not callable(execution_is_current):
        raise ValueError("CAPABILITY_SELECTION_BINDING_INVALID")

    # LLM: 原JSON锁中先复核canonical身份，再比较完整marker及执行权；未命中抛私有信号以保证文件字节不变。
    # 函数用途: 形成唯一新任务值，保留同时已更新的pins、目标及所有其它字段。
    def updater(data: dict[str, Any]) -> dict[str, Any]:
        latest = ThreadTaskLink.from_dict(data)
        if latest.task_id != task_id or latest.thread_id != thread_id:
            raise ValueError("CAPABILITY_SELECTION_BINDING_INVALID")
        if (latest.status != "active" or latest.capability_selection_corruption is not None
                or latest.capability_selection != expected):
            raise _CapabilitySelectionSkipped
        if execution_is_current(latest, authority_claim) is not True:
            raise _CapabilitySelectionSkipped
        return replace(latest, capability_selection=updated).to_dict()

    with store.transition_guard(task_id):
        try:
            payload = update_json_file_atomic(store.storage.task_path(task_id), updater, require_existing=True)
        except _CapabilitySelectionSkipped:
            return None
    return ThreadTaskLink.from_dict(payload).capability_selection


# LLM: 沿 bind 原命名锁范围调用；读取原任务集合拒绝同名活跃记录，检查坏账时保持原失败语义。
# 函数用途: 核对新绑定是否与已有工作重名，不写任务、不创建索引或推断执行权。
def _assert_no_named_task_conflict(
    store: TaskStore, thread_id: str, task_id: str, work_kind: str, work_name: str,
) -> None:
    links, errors = store.list_report(thread_id)
    if errors:
        raise DataCorruptionError("conversation task links are unavailable")
    duplicate = next(
        (link for link in links
         if link.task_id != task_id
         and str(link.work_kind or "").strip().lower() == work_kind
         and named_work_name_matches(link, work_kind, work_name)
         and str(link.status or "").strip().lower() not in THREAD_TASK_LINK_INACTIVE_STATUSES),
        None,
    )
    if duplicate is not None:
        raise ValueError(f"an active {work_kind} named {work_name!r} already exists")


# LLM: 仅绑定 detached 命名任务且请求未给 anchor 时读取原消息；保持坏转录失败语义，不解析任务文案或写入会话。
# 函数用途: 为新绑定形成独立请求值，延续原 context anchor 规则，防止调用方字典被修改。
def _task_request_with_context_anchor(
    store: TaskStore, thread: ConversationThread, request: dict, *, work_kind: str, work_name: str,
) -> dict:
    cancellation_scope = str(request.get("cancellation_scope") or "foreground").strip().lower()
    request = dict(request)
    if (
        work_kind in {"audit", "goal"}
        and work_name
        and cancellation_scope == "detached"
        and "context_anchor_message_id" not in request
    ):
        messages, message_errors = store._recent_messages_report(
            thread.thread_id,
            limit=1,
        )
        if message_errors:
            raise DataCorruptionError(
                "conversation transcript is unavailable for detached task binding"
            )
        request["context_anchor_message_id"] = messages[-1].message_id if messages else ""
    return request


# LLM: 任务、pins与一次选择共用原文件；选择CAS按transition→JSON锁，callback只核真实执行权，不发网络或重入锁。
# 类用途: 保存任务及其上下文事实，不执行模型；原状态/目标/pin更新保留选择标记和坏账证据。
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

    # LLM: pending只来自独立typed宿主参数并仅新建时初始化；已有link即便缺键也原样保留，不从request读选择权限。
    # 函数用途: 幂等保存任务关联并同步索引/工作区；可为真实新任务保存一次选择资格，不调用模型。
    def bind(self, request: dict, *, capability_selection: TaskCapabilitySelection | None = None) -> ThreadTaskLink:
        if capability_selection is not None and (
            not isinstance(capability_selection, TaskCapabilitySelection) or capability_selection.status != "pending"
        ):
            raise ValueError("CAPABILITY_SELECTION_PENDING_REQUIRED")
        thread_id = str(request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        task_id = str(request.get("task_id") or "")
        if not task_id:
            raise ValueError("task_id is required")
        current = now(request.get("now"))
        work_kind = str(request.get("work_kind") or "").strip().lower()
        work_name = str(request.get("work_name") or "").strip()
        request = _task_request_with_context_anchor(self, thread, request, work_kind=work_kind, work_name=work_name)
        guard = (
            self.named_work_transition_guard(thread.thread_id, work_kind, work_name)
            if work_kind in {"audit", "goal"} and work_name
            else nullcontext()
        )
        with guard:
            if work_kind and work_name:
                _assert_no_named_task_conflict(self, thread.thread_id, task_id, work_kind, work_name)
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
                    capability_selection=capability_selection,
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

    # LLM: 仅pending可领取；宿主身份、候选和准备时实际模型绑定摘要在I/O前写入原文件，claimed重启不会重新请求。
    # 函数用途: 在原任务事务内领取一次能力选择；未命中返回None，身份或执行权读取异常原样上抛。
    def claim_capability_selection(
        self, *, task_id: str, thread_id: str, request_id: str, run_id: str, attempt_id: str,
        candidate_digest: str, model_binding_digest: str,
        execution_is_current: Callable[[ThreadTaskLink, TaskCapabilitySelection], bool],
    ) -> TaskCapabilitySelection | None:
        claimed = TaskCapabilitySelection(
            status="claimed", claim_id=uuid.uuid4().hex, request_id=request_id, run_id=run_id,
            attempt_id=attempt_id, candidate_digest=candidate_digest, model_binding_digest=model_binding_digest,
        )
        return _transition_capability_selection(
            self, task_id=task_id, thread_id=thread_id, expected=TaskCapabilitySelection.pending(),
            updated=claimed, authority_claim=claimed, execution_is_current=execution_is_current,
        )

    # LLM: 只有完整expected_claim和当前执行权均相符才结束；refs仅形成结果摘要，原pins仍是版本权威。
    # 函数用途: 保存selected/empty/failed一次终态（失败时连同无正文的failure原因）；迟到、已结束或损坏marker均不改盘，不pin或注入正文。
    def finish_capability_selection(
        self, *, task_id: str, thread_id: str, expected_claim: TaskCapabilitySelection,
        outcome: str, selected_refs=(), warning_codes=(), failure=None,
        execution_is_current: Callable[[ThreadTaskLink, TaskCapabilitySelection], bool],
    ) -> TaskCapabilitySelection | None:
        if not isinstance(expected_claim, TaskCapabilitySelection) or expected_claim.status != "claimed":
            raise ValueError("CAPABILITY_SELECTION_CLAIM_REQUIRED")
        finished = expected_claim.finished(outcome=outcome, selected_refs=selected_refs, warning_codes=warning_codes,
                                           failure=failure)
        return _transition_capability_selection(
            self, task_id=task_id, thread_id=thread_id, expected=expected_claim, updated=finished,
            authority_claim=expected_claim, execution_is_current=execution_is_current,
        )

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

    # LLM: 版本固定在原transition→JSON锁；可选执行权复核在active检查后且查重/写入前，失权异常原样抛出。
    # 函数用途: 首次读包时固定准确版本，重读幂等；选择宿主可在同锁拒绝停止或换attempt后的迟到pin。
    def pin_skill_reference(
        self, *, task_id: str, thread_id: str, reference: dict[str, str],
        execution_authority_check: Callable[[], None] | None = None,
    ) -> ThreadTaskLink:
        from ..capability.task_references import normalize_skill_reference

        selected = safe_file_stem(str(task_id or ""))
        fixed = normalize_skill_reference(reference)
        if not selected or not thread_id or fixed.get("kind") != "capability_package":
            raise ValueError("SKILL_TASK_REFERENCE_INVALID")

        # LLM: updater持原文件锁，先核归属/active，再运行本地执行权检查；callback不得重入任务锁或发网络请求。
        # 函数用途: 复核当前执行权后合并包引用，失败零写入，重复调用保留全部任务字段。
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            latest = ThreadTaskLink.from_dict(data)
            if latest.task_id != task_id or latest.thread_id != thread_id:
                raise ValueError("SKILL_TASK_BINDING_INVALID")
            if latest.status in THREAD_TASK_LINK_INACTIVE_STATUSES:
                raise ValueError("SKILL_TASK_INACTIVE")
            if execution_authority_check is not None:
                execution_authority_check()
            for previous in latest.skill_snapshot_refs:
                if previous["stable_id"] == fixed["stable_id"]:
                    if previous != fixed:
                        raise ValueError("SKILL_PACKAGE_REFERENCE_CONFLICT")
                    return data
            return replace(latest, skill_snapshot_refs=(*latest.skill_snapshot_refs, fixed)).to_dict()

        with self.transition_guard(task_id):
            result = update_json_file_atomic(self.storage.task_path(selected), updater, require_existing=True)
        return ThreadTaskLink.from_dict(result)

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
