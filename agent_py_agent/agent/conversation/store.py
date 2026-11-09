# LLM: 在唯一 canonical 根目录显式组装会话领域并透传owner compact根；调用方直接使用领域对象，锁、CAS 和原子提交仍归原组件。
# 模块用途: 连接会话持久能力并提供跨领域上下文和旧账维护，不承载插话、任务或消息的底层实现。
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..gateway_parts.io import cleanup_orphan_lock_files
from .goal_clock import shared_goal_clocks
from .models import MODEL_HIDDEN_THREAD_FIELDS
from .session_pair_rate import SessionPairRateLimiter
from .session_tasks import SessionTaskStore
from .store_audits import AuditStore
from .store_claims import ClaimStore
from .store_goals import GoalStore
from .store_guidance import GuidanceStore
from .store_layout import ConversationStorage
from .store_messages import MessageStore
from .store_observations import ObservationStore
from .store_progress import ProgressStore
from .store_tasks import TaskStore
from .store_threads import ThreadStore
from .store_usage import ModelUsageStore
from .store_wake_quarantine_archive import archive_stale_wake_quarantine
from .store_wakes import WakeStore

logger = logging.getLogger(__name__)

# 策略与已结束租约沿同一维护周期保留七天后归档，文件移动到原账本归档目录，便于诊断和恢复。
_LEDGER_GC_RETENTION_SECONDS = 7 * 24 * 3600


# LLM: 所有领域在唯一 canonical 根目录组装；跨域依赖显式注入，插话通过 guidance 分层组装，不能增加旧 API 转发。
# 类用途: 组装会话持久能力并提供跨领域上下文读取，保持原目录和线程原子更新边界。
class ConversationStore:
    # LLM: initialize=False保持只读打开；compact_root透传同一存储上下文，组件初始化不读写账本，线程回调仍用原跨进程文件锁。
    # 函数用途: 在同一会话与冷检查点路径上下文组装各领域及共享时钟，显式提供跨域能力。
    def __init__(
        self,
        root: str | Path,
        *,
        initialize: bool = True,
        model_default: Callable[[], str] | None = None,
        compact_root: str | Path | None = None,
    ) -> None:
        self.storage = ConversationStorage(root, initialize=initialize, compact_root=compact_root)
        self.threads = ThreadStore(self.storage, model_default=model_default)
        self.messages = MessageStore(
            self.storage, require_thread=self.threads.require,
            update_thread_atomic=self.threads.update_atomic,
        )
        self.guidance = GuidanceStore(self.storage, append_message_once=self.messages.append_once)
        self.progress = ProgressStore(self.storage, require_thread=self.threads.require)
        self.tasks = TaskStore(
            self.storage, require_thread=self.threads.require,
            load_thread_report=self.threads.load_report,
            recent_messages_report=self.messages.recent_report,
            disable_task_progress_policies=self.progress.disable_task,
        )
        self.audits = AuditStore(self.storage, tasks=self.tasks)
        # 会话间派活（第一期）：只存派活记录与状态；正文引用 guidance，目标执行链接结构化。
        self.session_tasks = SessionTaskStore(self.storage)
        # 每对会话每小时限额的计数（结构化节流）：发消息/派活前判断，回报消息只计入不被拒。
        self.session_pair_rate = SessionPairRateLimiter(self.storage)
        self.goal_clock = shared_goal_clocks(self.storage.root)
        self.observations = ObservationStore(
            self.storage, require_thread=self.threads.require,
            update_thread_atomic=self.threads.update_atomic,
        )
        self.goals = GoalStore(
            self.storage, clock=self.goal_clock,
            require_thread=self.threads.require, load_task=self.tasks.load,
        )
        self.wakes = WakeStore(
            self.storage, require_thread=self.threads.require,
            update_thread_atomic=self.threads.update_atomic,
            mark_observations_handled=self.observations.mark_handled,
        )
        # 读不出信封的唤醒结案时按 wake_signal_id 结观察：观察层的方法在这里绑定，WakeStore 不认识观察层。
        self.wakes.attempts.bind_wake_observation_settler(self.observations.mark_handled_for_wake)
        self.claims = ClaimStore(
            self.storage, require_thread=self.threads.require, load_thread=self.threads.load,
        )
        self.model_usage = ModelUsageStore(
            self.storage.model_usage_dir,
            require_thread=self.threads.require,
            update_thread_atomic=self.threads.update_atomic,
        )

    # LLM: 跨域维护只在原调度周期调用，策略与 claim 使用同一时间与保留期，保持策略后 claim 的顺序；
    #   唤醒结案留档最后归档，用它自己的 14 天保留期（store_wake_quarantine_archive，只移不删），并同步清理
    #   已移走或已消费记录遗留的超龄未持锁 .lock sidecar（cleanup_orphan_lock_files），
    #   并在维护周期内隔离解析不了的活动任务链接、剔除幽灵历史任务 id（读路径不修）。
    # 函数用途: 协调策略、租约旧账、唤醒结案留档归档与孤儿锁清理，返回各类计数，不新建后台任务。
    def gc_stale_ledger_records(
        self, *, now: float | None = None,
        retention_seconds: float = _LEDGER_GC_RETENTION_SECONDS,
    ) -> dict[str, int]:
        current = now if now is not None else time.time()
        archived_policies = self.progress.archive_stale(
            current=current, retention_seconds=retention_seconds,
        )
        archived_claims = self.claims.archive_stale(
            current=current, retention_seconds=retention_seconds,
        )
        archived_wake_quarantine = archive_stale_wake_quarantine(self.storage, current=current)
        cleaned_orphan_locks = sum(
            cleanup_orphan_lock_files(directory, now=current)
            for directory in self.storage.lock_sidecar_dirs()
        )
        try:
            threads, _thread_errors = self.threads.list_report(limit=0)
            quarantined_task_links, pruned_task_ids = self.tasks.heal_broken_links(threads)
        except Exception as exc:  # noqa: BLE001 - 自愈步骤失败不影响前面已完成的归档统计
            logger.warning("TASK_LINK_HEAL_SKIPPED error_type=%s", type(exc).__name__)
            quarantined_task_links = pruned_task_ids = 0
        return {
            "archived_policies": archived_policies,
            "archived_claims": archived_claims,
            "archived_wake_quarantine": archived_wake_quarantine,
            "cleaned_orphan_locks": cleaned_orphan_locks,
            "quarantined_task_links": quarantined_task_links,
            "pruned_task_ids": pruned_task_ids,
        }

    # LLM: 只组装既有领域读取结果；严格处理坏账的调用方使用 context_bundle_report，不建立第二份状态。
    # 函数用途: 为旧有正文上下文调用返回跨领域快照，不写盘或标记已消费。
    def context_bundle(self, thread_id: str, *, recent_limit: int = 20,
                       required_task_ids: tuple[str, ...] = ()) -> dict[str, Any]:
        bundle, _load_errors = self.context_bundle_report(thread_id, recent_limit=recent_limit,
                                                         required_task_ids=required_task_ids)
        return bundle

    # LLM: 模型上下文只投影运行事实；include_messages=False只延后正文读取，调用方须同次按任务范围补齐，不能当空历史。
    #   线程字段按 models.MODEL_HIDDEN_THREAD_FIELDS 剔除；历史坏链和缺失活跃链接只告警，不从读路径修索引。
    # 函数用途: 读取会话上下文及加载错误，排除展示遥测与宿主提示，原持久 thread 完整保留。
    def context_bundle_report(
        self,
        thread_id: str,
        *,
        recent_limit: int = 20,
        include_messages: bool = True,
        required_task_ids: tuple[str, ...] = (),
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        thread = self.threads.require(thread_id)
        messages, message_errors = (
            self.messages.recent_report(thread_id, limit=recent_limit) if include_messages else ([], [])
        )
        tasks, task_errors, task_warnings, task_coverage = self.tasks.context_report(
            thread_id, recent_limit=recent_limit, required_task_ids=required_task_ids)
        active_task_ids = set(thread.active_task_ids)
        degraded_ids = {str(row.get("task_id") or row.get("id")) for row in task_warnings
                        if row.get("task_id") or row.get("id")}
        blocking_errors = []
        for error in task_errors:
            task_id = error.get("task_id")
            if (task_id and error.get('scope_resolution') != 'thread_mismatch'
                    and (task_id not in active_task_ids or error.get("error_type") == "FileNotFoundError")):
                degraded_ids.add(str(task_id))
            else:
                blocking_errors.append(error)
        task_errors = blocking_errors
        if degraded_ids or task_warnings:
            logger.warning("TASK_LINK_DEGRADED context=conversation.context_bundle thread=%s tasks=%s",
                           thread_id, ",".join(sorted(degraded_ids)))
        observations, observation_errors = self.observations.recent_report(
            thread_id, limit=recent_limit
        )
        guidance, guidance_errors = self.guidance.pending_report(
            "thread", thread_id, limit=recent_limit
        )
        goals, goal_error = self.goals.list_report(thread_id)
        thread_payload = {key: value for key, value in thread.to_dict().items() if key not in MODEL_HIDDEN_THREAD_FIELDS}
        return {
            "thread": thread_payload,
            "messages": [item.to_dict() for item in messages],
            "tasks": [item.to_dict() for item in tasks],
            "task_coverage": task_coverage,
            "channel_bindings": [item.to_dict() for item in thread.channel_bindings],
            "observations": [item.to_dict() for item in observations],
            "guidance": [item.to_dict() for item in guidance],
            "goals": [goal.to_dict() for goal in goals],
        }, [
            *message_errors,
            *task_errors,
            *observation_errors,
            *guidance_errors,
            *([goal_error] if goal_error is not None else []),
        ]
