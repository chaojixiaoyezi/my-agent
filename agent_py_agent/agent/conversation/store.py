# LLM: 在唯一 canonical 根目录显式组装会话领域；调用方直接使用领域对象，锁、CAS 和原子提交仍归原组件。
# 模块用途: 连接会话持久能力并提供跨领域上下文和旧账维护，不承载插话、任务或消息的底层实现。
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .goal_clock import shared_goal_clocks
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
from .store_wakes import WakeStore

# 策略与已结束租约沿同一维护周期保留七天后归档，文件移动到原账本归档目录，便于诊断和恢复。
_LEDGER_GC_RETENTION_SECONDS = 7 * 24 * 3600


# LLM: 所有领域在唯一 canonical 根目录组装；跨域依赖显式注入，插话通过 guidance 分层组装，不能增加旧 API 转发。
# 类用途: 组装会话持久能力并提供跨领域上下文读取，保持原目录和线程原子更新边界。
class ConversationStore:
    # LLM: initialize=False 保持只读打开；组件初始化不读写账本，线程回调仍使用原跨进程文件锁。
    # 函数用途: 在同一存储上下文组装各领域及共享时钟，向组件显式提供跨域能力。
    def __init__(
        self,
        root: str | Path,
        *,
        initialize: bool = True,
        model_default: Callable[[], str] | None = None,
    ) -> None:
        self.storage = ConversationStorage(root, initialize=initialize)
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
        self.claims = ClaimStore(
            self.storage, require_thread=self.threads.require, load_thread=self.threads.load,
        )
        self.model_usage = ModelUsageStore(
            self.storage.model_usage_dir,
            require_thread=self.threads.require,
            update_thread_atomic=self.threads.update_atomic,
        )

    # LLM: 跨域维护只在原调度周期调用，两个领域使用同一时间与保留期，保持策略后 claim 的顺序。
    # 函数用途: 协调策略和租约旧账归档，返回原有分类计数，不新建后台任务。
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
        return {"archived_policies": archived_policies, "archived_claims": archived_claims}

    # LLM: 只组装既有领域读取结果；严格处理坏账的调用方使用 context_bundle_report，不建立第二份状态。
    # 函数用途: 为旧有正文上下文调用返回跨领域快照，不写盘或标记已消费。
    def context_bundle(self, thread_id: str, *, recent_limit: int = 20) -> dict[str, Any]:
        bundle, _load_errors = self.context_bundle_report(thread_id, recent_limit=recent_limit)
        return bundle

    # LLM: 模型上下文只投影会话内容和运行事实，model_context_usage 仅供 UI，不得扰动缓存前缀。
    # 函数用途: 读取会话上下文及加载错误，排除展示遥测，原持久 thread 完整保留。
    def context_bundle_report(
        self,
        thread_id: str,
        *,
        recent_limit: int = 20,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        thread = self.threads.require(thread_id)
        messages, message_errors = self.messages.recent_report(thread_id, limit=recent_limit)
        tasks, task_errors = self.tasks.list_report(thread_id)
        observations, observation_errors = self.observations.recent_report(
            thread_id, limit=recent_limit
        )
        guidance, guidance_errors = self.guidance.pending_report(
            "thread", thread_id, limit=recent_limit
        )
        goals, goal_error = self.goals.list_report(thread_id)
        thread_payload = thread.to_dict()
        thread_payload.pop("model_context_usage", None)
        thread_payload.pop("model_metrics", None)
        return {
            "thread": thread_payload,
            "messages": [item.to_dict() for item in messages],
            "tasks": [item.to_dict() for item in tasks],
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
