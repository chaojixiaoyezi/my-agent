# LLM: Audit 准备、发布与重启账本；依赖同源任务关联和命名锁，不复制任务状态或解析模型文字决定迁移；修改须同步任务状态和 Audit 回归。
# 模块用途: Audit 准备、发布与重启账本；依赖同源任务关联和命名锁，不复制任务状态或解析模型文字决定迁移。
from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..gateway_parts.io import (
    update_json_file_atomic,
)
from ..ingestion.source_binding import merge_audit_source_bindings
from ..runtime_errors import DataCorruptionError
from .audit_requirements import (
    append_audit_pending_requirement,
    append_audit_user_requirement,
    published_audit_requirement,
)
from .models import (
    THREAD_TASK_LINK_INACTIVE_STATUSES,
    THREAD_TASK_LINK_NON_RESURRECTABLE_STATUSES,
    ThreadTaskLink,
)
from .store_io import (
    now,
)
from .store_layout import ConversationStorage
from .store_tasks import TaskStore, named_work_name_matches, sync_task_workspace_status


# LLM: store_audits 的持久化合同：构造发布修订并清空已消费的准备字段，保留原来源绑定合并语义；修改须同步本领域调用方与存储回归。
# 函数用途: 构造发布修订并清空已消费的准备字段，保留原来源绑定合并语义。
def _published_audit_link(
    link: ThreadTaskLink,
    *,
    prompt: str,
    prepare_request_id: str,
    current_time: float,
    refs: tuple[str, ...],
    source_bindings: tuple[dict[str, Any], ...],
    source_update_mode: str,
    user_history: str,
) -> ThreadTaskLink:
    return replace(
        link,
        goal=published_audit_requirement(
            validated_notes=prompt,
            user_prepare_history=user_history,
        ),
        effective_user_prompt=user_history,
        pending_prompt="",
        pending_updated_at=0.0,
        pending_prepare_request_id="",
        effective_revision=link.effective_revision + 1,
        effective_updated_at=current_time,
        effective_prepare_request_id=prepare_request_id,
        effective_evidence_refs=refs,
        effective_source_bindings=(
            merge_audit_source_bindings(
                () if source_update_mode == "replace" else link.effective_source_bindings,
                source_bindings,
            )
            if source_bindings or source_update_mode == "replace"
            else link.effective_source_bindings
        ),
    )


# LLM: store_audits 的持久化合同：通过准备请求身份和结构化字段区分首次发布与同轮修订；修改须同步本领域调用方与存储回归。
# 函数用途: 通过准备请求身份和结构化字段区分首次发布与同轮修订。
def _audit_publish_modes(link: ThreadTaskLink, prepare_request_id: str) -> tuple[bool, bool]:
    pending_prompt = str(link.pending_prompt or "").strip()
    return (
        bool(pending_prompt and link.pending_prepare_request_id == prepare_request_id),
        bool(
            not pending_prompt
            and link.effective_prepare_request_id == prepare_request_id
            and str(link.effective_user_prompt or "").strip()
        ),
    )


# LLM: store_audits 的持久化合同：核验精确 Audit 身份、非终态及合法修订模式，不解析需求正文；修改须同步本领域调用方与存储回归。
# 函数用途: 核验精确 Audit 身份、非终态及合法修订模式，不解析需求正文。
def _audit_revision_publishable(
    link: ThreadTaskLink,
    task_id: str,
    first_publish: bool,
    same_turn_amendment: bool,
) -> bool:
    return bool(
        link.task_id == task_id
        and str(link.work_kind or "").strip().lower() == "audit"
        and str(link.status or "").strip().lower() not in THREAD_TASK_LINK_INACTIVE_STATUSES
        and (first_publish or same_turn_amendment)
    )


# LLM: store_audits 的持久化合同：核对记录属于具名 Audit，不把普通任务转换为命名工作；修改须同步本领域调用方与存储回归。
# 函数用途: 核对记录属于具名 Audit，不把普通任务转换为命名工作。
def _reopenable_named_audit(link: object | None) -> bool:
    return bool(
        link is not None
        and str(getattr(link, "work_kind", "") or "").strip().lower() == "audit"
        and str(getattr(link, "work_name", "") or "").strip()
    )


# LLM: store_audits 的持久化合同：核验精确任务身份和可重开的终态，作为持锁迁移条件；修改须同步本领域调用方与存储回归。
# 函数用途: 核验精确任务身份和可重开的终态，作为持锁迁移条件。
def _terminal_named_audit(link: ThreadTaskLink, task_id: str) -> bool:
    return bool(
        link.task_id == task_id
        and str(link.work_kind or "").strip().lower() == "audit"
        and str(link.status or "").strip().lower() in THREAD_TASK_LINK_NON_RESURRECTABLE_STATUSES
    )


# LLM: 本领域只拥有 Audit 修订操作，任务路径、命名锁和活动索引仍由同一 TaskStore 提供。
# 类用途: 提交 Audit 准备、发布和重启状态，不创建平行任务记录或新的运行控制规则。
class AuditStore:
    # LLM: storage 和 tasks 来自同一组装入口；构造不产生文件，所有更新沿原任务 CAS 和锁执行。
    # 函数用途: 接入任务账本及其命名事务能力，供 Audit 生命周期提交使用。
    def __init__(self, storage: ConversationStorage, *, tasks: TaskStore) -> None:
        self.storage = storage
        self.tasks = tasks

    # LLM: store_audits 的持久化合同：向非终态 Audit 原子追加一轮准备需求，保留请求身份和时间；修改须同步本领域调用方与存储回归。
    # 函数用途: 向非终态 Audit 原子追加一轮准备需求，保留请求身份和时间。
    def record_pending_prompt(self, request: dict) -> ThreadTaskLink | None:
        task_id = str(request.get("task_id") or "").strip()
        prompt = str(request.get("prompt") or "").strip()
        prepare_request_id = str(request.get("prepare_request_id") or "").strip()
        path = self.storage.task_path(task_id)
        if not task_id or not prompt or not prepare_request_id or not path.exists():
            return None
        current_time = now(request.get("now"))
        updated = False

        # LLM: store_audits 的持久化合同：仅在精确 Audit 非终态时追加待准备正文与请求身份；修改须同步本领域调用方与存储回归。
        # 函数用途: 仅在精确 Audit 非终态时追加待准备正文与请求身份。
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal updated
            link = ThreadTaskLink.from_dict(data)
            if (
                link.task_id != task_id
                or str(link.work_kind or "").strip().lower() != "audit"
                or str(link.status or "").strip().lower() in THREAD_TASK_LINK_INACTIVE_STATUSES
            ):
                return data
            updated = True
            return replace(
                link,
                pending_prompt=append_audit_pending_requirement(link, prompt),
                pending_updated_at=current_time,
                pending_prepare_request_id=prepare_request_id,
            ).to_dict()

        payload = update_json_file_atomic(path, updater, require_existing=True)
        return ThreadTaskLink.from_dict(payload) if updated else None

    # LLM: store_audits 的持久化合同：从 preparing 原子开始运行，保留发布配置并更新线程索引及状态投影；修改须同步本领域调用方与存储回归。
    # 函数用途: 从 preparing 原子开始运行，保留发布配置并更新线程索引及状态投影。
    def activate(self, request: dict) -> ThreadTaskLink | None:
        task_id = str(request.get("task_id") or "").strip()
        fallback_goal = str(request.get("goal") or "").strip()
        path = self.storage.task_path(task_id)
        if not task_id or not fallback_goal or not path.exists():
            return None
        current_time = now(request.get("now"))
        duration = max(1, int(request.get("duration_seconds") or 0))
        updated = False

        # LLM: store_audits 的持久化合同：只将精确 preparing Audit 变为 active 并推进 epoch，保留已发布配置；修改须同步本领域调用方与存储回归。
        # 函数用途: 只将精确 preparing Audit 变为 active 并推进 epoch，保留已发布配置。
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal updated
            link = ThreadTaskLink.from_dict(data)
            if (
                link.task_id != task_id
                or str(link.work_kind or "").strip().lower() != "audit"
                or str(link.status or "").strip().lower() != "preparing"
            ):
                return data
            updated = True
            return replace(
                link,
                goal=link.goal or fallback_goal,
                run_prompt=fallback_goal,
                status="active",
                duration_seconds=duration,
                expires_at=current_time + duration,
                cancellation_scope="detached",
                effective_revision=max(1, link.effective_revision),
                effective_updated_at=link.effective_updated_at or current_time,
                run_epoch=max(1, link.run_epoch + 1),
            ).to_dict()

        payload = update_json_file_atomic(path, updater, require_existing=True)
        return self._indexed_link(task_id, current_time, payload, updated)

    # LLM: store_audits 的持久化合同：在命名锁和任务锁内重开终态准备，保留原配置与工作目录；修改须同步本领域调用方与存储回归。
    # 函数用途: 在命名锁和任务锁内重开终态准备，保留原配置与工作目录。
    def reopen_prepare(self, request: dict) -> ThreadTaskLink | None:
        task_id = str(request.get("task_id") or "").strip()
        prompt = str(request.get("prompt") or "").strip()
        prepare_request_id = str(request.get("prepare_request_id") or "").strip()
        path = self.storage.task_path(task_id)
        if not task_id or not prompt or not prepare_request_id or not path.exists():
            return None
        current_time = now(request.get("now"))
        initial = self.tasks.load(task_id)
        if not _reopenable_named_audit(initial):
            return None
        updated = False

        # LLM: store_audits 的持久化合同：再次核验原终态后进入 preparing，不受锁前旧快照驱动复活；修改须同步本领域调用方与存储回归。
        # 函数用途: 再次核验原终态后进入 preparing，不受锁前旧快照驱动复活。
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal updated
            link = ThreadTaskLink.from_dict(data)
            if not _terminal_named_audit(link, task_id):
                return data
            updated = True
            return replace(
                link,
                status="preparing",
                run_prompt="",
                duration_seconds=None,
                expires_at=None,
                cancellation_scope="foreground",
                pending_prompt=append_audit_pending_requirement(link, prompt),
                pending_updated_at=current_time,
                pending_prepare_request_id=prepare_request_id,
            ).to_dict()

        with self.tasks.named_work_transition_guard(initial.thread_id, "audit", initial.work_name):
            if self._active_named_work_conflict(initial, task_id):
                return None
            with self.tasks.transition_guard(task_id):
                payload = update_json_file_atomic(path, updater, require_existing=True)
        return self._indexed_link(task_id, current_time, payload, updated)

    # LLM: store_audits 的持久化合同：在命名锁和任务锁内开始新运行 epoch，保持原发布修订与任务身份；修改须同步本领域调用方与存储回归。
    # 函数用途: 在命名锁和任务锁内开始新运行 epoch，保持原发布修订与任务身份。
    def reactivate(self, request: dict) -> ThreadTaskLink | None:
        task_id = str(request.get("task_id") or "").strip()
        fallback_goal = str(request.get("goal") or "").strip()
        path = self.storage.task_path(task_id)
        if not task_id or not fallback_goal or not path.exists():
            return None
        current_time = now(request.get("now"))
        initial = self.tasks.load(task_id)
        if not _reopenable_named_audit(initial):
            return None
        duration = max(1, int(request.get("duration_seconds") or 0))
        updated = False

        # LLM: store_audits 的持久化合同：再次核验原终态后原子推进 epoch 并清空准备字段，保留持久配置；修改须同步本领域调用方与存储回归。
        # 函数用途: 再次核验原终态后原子推进 epoch 并清空准备字段，保留持久配置。
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal updated
            link = ThreadTaskLink.from_dict(data)
            if not _terminal_named_audit(link, task_id):
                return data
            updated = True
            return replace(
                link,
                goal=link.goal or fallback_goal,
                run_prompt=fallback_goal,
                status="active",
                duration_seconds=duration,
                expires_at=current_time + duration,
                cancellation_scope="detached",
                pending_prompt="",
                pending_updated_at=0.0,
                pending_prepare_request_id="",
                effective_revision=max(1, link.effective_revision),
                effective_updated_at=link.effective_updated_at or current_time,
                run_epoch=max(1, link.run_epoch + 1),
            ).to_dict()

        with self.tasks.named_work_transition_guard(initial.thread_id, "audit", initial.work_name):
            if self._active_named_work_conflict(initial, task_id):
                return None
            with self.tasks.transition_guard(task_id):
                payload = update_json_file_atomic(path, updater, require_existing=True)
        return self._indexed_link(task_id, current_time, payload, updated)

    # LLM: store_audits 的持久化合同：按权威任务关联检查同名非终态冲突，损坏列表不能当成无冲突；修改须同步本领域调用方与存储回归。
    # 函数用途: 按权威任务关联检查同名非终态冲突，损坏列表不能当成无冲突。
    def _active_named_work_conflict(
        self,
        selected: ThreadTaskLink,
        task_id: str,
    ) -> bool:
        links, errors = self.tasks.list_report(selected.thread_id)
        if errors:
            raise DataCorruptionError("conversation task links are unavailable")
        return any(
            link.task_id != task_id
            and str(link.work_kind or "").strip().lower() == "audit"
            and named_work_name_matches(link, "audit", selected.work_name)
            and str(link.status or "").strip().lower() not in THREAD_TASK_LINK_INACTIVE_STATUSES
            for link in links
        )

    # LLM: store_audits 的持久化合同：校验准备请求与来源更新模式后原子发布修订，不凭模型声明推进状态；修改须同步本领域调用方与存储回归。
    # 函数用途: 校验准备请求与来源更新模式后原子发布修订，不凭模型声明推进状态。
    def publish_effective_prompt(
        self,
        request: dict,
    ) -> ThreadTaskLink | None:
        task_id = str(request.get("task_id") or "").strip()
        prompt = str(request.get("prompt") or "").strip()
        prepare_request_id = str(request.get("prepare_request_id") or "").strip()
        refs = tuple(str(item) for item in (request.get("evidence_refs") or []) if str(item))
        source_bindings = tuple(
            dict(item) for item in (request.get("source_bindings") or []) if isinstance(item, dict)
        )
        source_update_mode = str(request.get("source_update_mode") or "upsert").strip().lower()
        path = self.storage.task_path(task_id)
        if (
            not task_id
            or not prompt
            or not prepare_request_id
            or source_update_mode not in {"upsert", "replace"}
            or not path.exists()
        ):
            return None
        current_time = now(request.get("now"))
        updated = False

        # LLM: store_audits 的持久化合同：在持锁最新任务上核验修订资格，再合并发布记录及原始用户要求；修改须同步本领域调用方与存储回归。
        # 函数用途: 在持锁最新任务上核验修订资格，再合并发布记录及原始用户要求。
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal updated
            link = ThreadTaskLink.from_dict(data)
            first, amendment = _audit_publish_modes(link, prepare_request_id)
            if not _audit_revision_publishable(link, task_id, first, amendment):
                return data
            updated = True
            user_history = (
                append_audit_user_requirement(link, str(link.pending_prompt or "").strip())
                if first
                else link.effective_user_prompt
            )
            return _published_audit_link(
                link,
                prompt=prompt,
                prepare_request_id=prepare_request_id,
                current_time=current_time,
                refs=refs,
                source_bindings=source_bindings,
                source_update_mode=source_update_mode,
                user_history=user_history,
            ).to_dict()

        payload = update_json_file_atomic(path, updater, require_existing=True)
        return ThreadTaskLink.from_dict(payload) if updated else None

    # LLM: store_audits 的持久化合同：任务提交成功后更新原线程索引和工作区状态，未提交时不产生投影写入；修改须同步本领域调用方与存储回归。
    # 函数用途: 任务提交成功后更新原线程索引和工作区状态，未提交时不产生投影写入。
    def _indexed_link(
        self,
        task_id: str,
        current_time: float,
        payload: dict[str, Any],
        updated: bool,
    ) -> ThreadTaskLink | None:
        if not updated:
            return None
        link = ThreadTaskLink.from_dict(payload)
        thread = self.tasks.update_thread_index(
            link.thread_id,
            task_id,
            link.status,
            current_time,
        )
        sync_task_workspace_status(link, current_time, owner_home=thread.owner_home)
        return link
