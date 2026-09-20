# LLM: 目标集合、内容版本 CAS 与用量的唯一持久合同，复用原线程锁与共享时钟；修改须联测控制、恢复和预算。
# 模块用途: 持久化线程 Goal 生命周期，不拥有调度器、模型或另一份任务状态。
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..gateway_parts.io import (
    locked_file_transition,
    update_json_file_atomic,
)
from ..runtime_errors import DataCorruptionError, runtime_error_report
from .goal_clock import GoalClockGroup
from .models import (
    THREAD_GOAL_OBJECTIVE_MAX_CHARS,
    THREAD_GOAL_STATUSES,
    ConversationThread,
    ThreadGoal,
    ThreadTaskLink,
    new_id,
)
from .store_io import (
    now,
    read_json_object_report,
    safe_file_stem,
)
from .store_layout import ConversationStorage

_UNFINISHED_GOAL_STATUSES = THREAD_GOAL_STATUSES - {"complete"}

_LEGACY_CLEARED_GOAL_STATUS = "cleared"

_GOAL_COLLECTION_SCHEMA_VERSION = 2


# LLM: One per-thread collection is the single goal authority; legacy single-goal
# records are read only for deployed-state migration and are rewritten on mutation.
# 函数用途: 读取并校验一个会话的全部持续目标，兼容已部署的单目标旧记录。
def _read_thread_goals_report(path: Path) -> tuple[list[ThreadGoal], dict[str, Any] | None]:
    payload, error = read_json_object_report(path, context="conversation.goal.read")
    if error is not None or not payload:
        return [], error
    return _goals_from_update_data(payload, path=path)


# LLM: 集合 schema 和排序沿原持久格式，不丢历史已完成目标。
# 函数用途: 将目标集合序列化为原原子写入负载，不写文件。
def _goal_collection_payload(goals: list[ThreadGoal]) -> dict[str, Any]:
    return {
        "schema_version": _GOAL_COLLECTION_SCHEMA_VERSION,
        "goals": [goal.to_dict() for goal in goals],
    }


# LLM: 只处理现有集合及已部署单目标迁移；身份、状态和重复 ID 必须显式报错。
# 函数用途: 解析目标文件，在变更前保留全部损坏事实，禁止静默挑选坏数据。
def _goals_from_update_data(
    payload: dict[str, Any],
    *,
    path: Path,
) -> tuple[list[ThreadGoal], dict[str, Any] | None]:
    if not payload:
        return [], None
    try:
        if "goals" in payload:
            rows = payload.get("goals")
            if payload.get("schema_version") != _GOAL_COLLECTION_SCHEMA_VERSION or not isinstance(
                rows, list
            ):
                raise DataCorruptionError(f"thread goal collection is invalid: {path}")
        else:
            rows = [payload]
        goals: list[ThreadGoal] = []
        seen_ids: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise DataCorruptionError(f"thread goal row is invalid: {path}")
            goal = ThreadGoal.from_dict(row)
            if goal.status == _LEGACY_CLEARED_GOAL_STATUS:
                continue
            if not goal.goal_id or not goal.thread_id or not goal.task_id:
                raise DataCorruptionError(f"thread goal identity is invalid: {path}")
            if goal.status not in THREAD_GOAL_STATUSES:
                raise DataCorruptionError(f"thread goal status is invalid: {goal.status}")
            if goal.goal_id in seen_ids:
                raise DataCorruptionError(f"duplicate thread goal id: {goal.goal_id}")
            seen_ids.add(goal.goal_id)
            goals.append(goal)
        return goals, None
    except Exception as exc:
        report = runtime_error_report(exc, context="conversation.goal.read")
        report["path"] = str(path)
        return [], report


# LLM: 仅投影已提交原子更新的返回值，不能从模型文本判断创建成功。
# 函数用途: 将成功提交后的集合负载恢复为目标对象。
def _goals_from_mutation_payload(payload: dict[str, Any]) -> list[ThreadGoal]:
    rows = payload.get("goals") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    return [ThreadGoal.from_dict(row) for row in rows if isinstance(row, dict)]


# LLM: 精确选择器遇到多条未结束记录必须暴露冲突；历史完成记录不遮挡后来创建的唯一活跃目标。
# 函数用途: 在持久目标集合里选取当前记录，不能把歧义当作目标不存在或暗中覆盖旧目标。
def _select_thread_goal(
    goals: list[ThreadGoal],
    *,
    goal_id: str = "",
    task_id: str = "",
    name: str = "",
) -> ThreadGoal | None:
    selected = goals
    if goal_id:
        selected = [goal for goal in selected if goal.goal_id == goal_id]
    if task_id:
        selected = [goal for goal in selected if goal.task_id == task_id]
    if name:
        folded = name.casefold()
        selected = [goal for goal in selected if goal.name.casefold() == folded]
    if goal_id:
        if len(selected) > 1:
            raise ValueError("duplicate goal id; goal state requires repair")
        return selected[0] if selected else None
    unfinished = [goal for goal in selected if goal.status in _UNFINISHED_GOAL_STATUSES]
    if len(unfinished) > 1:
        raise ValueError("multiple unfinished goals exist; select one by name or id")
    if unfinished:
        return unfinished[0]
    return max(selected, key=lambda goal: goal.updated_at, default=None)


# LLM: 计费允许状态来自调用方声明的模式，不能从回复正文或完成措辞推断。
# 函数用途: 校验目标计费模式并返回原协议允许结算的状态集合。
def _accounting_statuses(mode: str) -> set[str]:
    allowed_statuses = {
        "active_status_only": {"active"},
        "active_only": {"active", "budget_limited"},
        "active_or_complete": {"active", "budget_limited", "complete"},
        "active_or_stopped": {
            "active",
            "paused",
            "blocked",
            "usage_limited",
            "budget_limited",
        },
    }.get(mode)
    if allowed_statuses is None:
        raise ValueError(f"unsupported goal accounting mode: {mode}")
    return allowed_statuses


# LLM: Goal 领域对象拥有原目标集合的持久操作；跨域读取只能经显式注入的线程、任务和时钟能力。
# 类用途: 持久化当前会话的持续目标，并提供原子生命周期与用量计量。
class GoalStore:
    # LLM: 复用唯一目录、共享时钟与两个只读跨域能力；构造不读写目标，也不领取执行权。
    # 函数用途: 将目标存储显式连接到同一线程和任务事实源。
    def __init__(
        self,
        storage: ConversationStorage,
        *,
        clock: GoalClockGroup,
        require_thread: Callable[[str], ConversationThread],
        load_task: Callable[[str], ThreadTaskLink | None],
    ) -> None:
        self.storage = storage
        self.clock = clock
        self._require_thread = require_thread
        self._load_task = load_task

    # LLM: All load-plus-mutate goal operations for one thread share this filesystem transition lock.
    # 函数用途: 为单个 thread 的目标查看和迁移提供跨线程/跨进程临界区。
    def transition_guard(self, thread_id: str):
        normalized = safe_file_stem(str(thread_id or "").strip())
        if not normalized:
            raise ValueError("thread_id is required")
        return locked_file_transition(self.storage.goals_dir / f".{normalized}.transition")

    # LLM: Strict callers receive corruption as an exception rather than an apparent empty goal.
    # 函数用途: 严格读取当前 thread 目标，损坏时 fail-closed。
    def load(
        self,
        thread_id: str,
        *,
        goal_id: str = "",
        task_id: str = "",
        name: str = "",
    ) -> ThreadGoal | None:
        goals, error = self.list_report(thread_id)
        if error is not None:
            raise DataCorruptionError(str(error.get("message") or "conversation goal read failed"))
        goal = _select_thread_goal(
            goals,
            goal_id=str(goal_id or "").strip(),
            task_id=str(task_id or "").strip(),
            name=str(name or "").strip(),
        )
        if goal is not None and goal.status == "active":
            self.clock.begin(goal)
        elif goal is not None and goal.status not in {"active", "budget_limited"}:
            self.clock.clear(goal.thread_id, goal_id=goal.goal_id)
        return goal

    # LLM: 缺失和损坏保持区分，只为活跃目标启动原共享计时基线。
    # 函数用途: 列出本线程全部目标，同时恢复活动目标的进程内计时。
    def list(self, thread_id: str) -> list[ThreadGoal]:
        goals, error = self.list_report(thread_id)
        if error is not None:
            raise DataCorruptionError(str(error.get("message") or "conversation goal read failed"))
        for goal in goals:
            if goal.status == "active":
                self.clock.begin(goal)
        return goals

    # LLM: Request assembly uses the report form so it can expose a typed load failure without mutating state.
    # 函数用途: 读取全部目标与结构化错误，供 Gateway 状态和上下文装配使用。
    def list_report(self, thread_id: str) -> tuple[list[ThreadGoal], dict[str, Any] | None]:
        normalized = str(thread_id or "").strip()
        self._require_thread(normalized)
        return _read_thread_goals_report(self.storage.goal_path(normalized))

    # LLM: 歧义目标按原结构化错误返回，不能将多目标冲突当成无目标。
    # 函数用途: 读取当前目标及选择错误，供控制和展示使用。
    def load_report(self, thread_id: str) -> tuple[ThreadGoal | None, dict[str, Any] | None]:
        goals, error = self.list_report(thread_id)
        if error is not None:
            return None, error
        try:
            return _select_thread_goal(goals), None
        except ValueError as exc:
            report = runtime_error_report(exc, context="conversation.goal.select")
            report["thread_id"] = thread_id
            return None, report

    # LLM: Each canonical agent thread permits one unfinished Goal, regardless of name/task. Completed history is retained.
    # 函数用途: 原子创建当前代理目标；名字不同不再产生隐式并行执行器，暂停目标也不能被覆盖。
    def create(self, request: dict[str, Any]) -> ThreadGoal:
        thread_id = str(request.get("thread_id") or "").strip()
        objective = str(request.get("objective") or "").strip()
        name = str(request.get("name") or "").strip()
        self._require_thread(thread_id)
        if not objective:
            raise ValueError("goal objective is required")
        if len(name) > 64 or "\n" in name or "\r" in name:
            raise ValueError("goal name must be at most 64 characters on one line")
        if len(objective) > THREAD_GOAL_OBJECTIVE_MAX_CHARS:
            raise ValueError(f"goal objective exceeds {THREAD_GOAL_OBJECTIVE_MAX_CHARS} characters")
        token_budget_value = request.get("token_budget")
        token_budget = int(token_budget_value) if token_budget_value is not None else None
        if token_budget is not None and token_budget <= 0:
            raise ValueError("goal token_budget must be positive")
        duration_value = request.get("duration_seconds")
        duration_seconds = int(duration_value) if duration_value is not None else None
        if duration_seconds is not None and duration_seconds <= 0:
            raise ValueError("goal duration_seconds must be positive")
        current_time = now(request.get("now"))
        created: ThreadGoal | None = None

        # LLM: 在同一目标文件锁内校验旧集合与未完成目标，单次返回完整新集合。
        # 函数用途: 阻止并发创建覆盖活跃或暂停目标，生成新目标身份并保留历史。
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal created
            existing, error = _goals_from_update_data(
                data,
                path=self.storage.goal_path(thread_id),
            )
            if error is not None:
                raise DataCorruptionError(
                    str(error.get("message") or "conversation goal read failed")
                )
            unfinished = [goal for goal in existing if goal.status in _UNFINISHED_GOAL_STATUSES]
            if unfinished:
                raise ValueError(
                    "an unfinished goal already exists for this agent thread; "
                    "close or update it explicitly instead of creating another"
                )
            goal_id = new_id("goal")
            created = ThreadGoal(
                goal_id=goal_id,
                thread_id=thread_id,
                objective=objective,
                task_id=str(request.get("task_id") or f"goal-task-{goal_id.removeprefix('goal-')}"),
                name=name,
                token_budget=token_budget,
                duration_seconds=duration_seconds,
                created_at=current_time,
                updated_at=current_time,
                metadata=request.get("metadata")
                if isinstance(request.get("metadata"), dict)
                else {},
            )
            return _goal_collection_payload([*existing, created])

        payload = update_json_file_atomic(self.storage.goal_path(thread_id), updater)
        goals = _goals_from_mutation_payload(payload)
        goal = created or _select_thread_goal(goals, task_id=str(request.get("task_id") or ""))
        if goal is None:
            raise DataCorruptionError(f"created goal is missing: {thread_id}")
        self.clock.begin(goal, reset=True)
        return goal

    # LLM: Compare exact Goal/status/content revision before mutation; usage accounting never increments this revision.
    # 函数用途: 原子保存目标并拒绝过期草稿；保留用量和历史，不把模型计费刷新误判为编辑冲突。
    def update(self, request: dict[str, Any]) -> ThreadGoal | None:
        thread_id = str(request.get("thread_id") or "").strip()
        requested_status = str(request.get("status") or "").strip().lower()
        expected_status = str(request.get("expected_status") or "").strip().lower()
        expected_revision = request.get("expected_revision")
        expected_goal_id = str(
            request.get("goal_id") or request.get("expected_goal_id") or ""
        ).strip()
        objective = str(request.get("objective") or "").strip()
        if requested_status and requested_status not in THREAD_GOAL_STATUSES:
            raise ValueError(f"unsupported goal status: {requested_status}")
        if objective and len(objective) > THREAD_GOAL_OBJECTIVE_MAX_CHARS:
            raise ValueError(f"goal objective exceeds {THREAD_GOAL_OBJECTIVE_MAX_CHARS} characters")
        if not requested_status and not objective and "token_budget" not in request:
            raise ValueError("goal update is empty")
        token_budget = request.get("token_budget")
        if "token_budget" in request and token_budget is not None:
            token_budget = int(token_budget)
            if token_budget <= 0:
                raise ValueError("goal token_budget must be positive")
        self._require_thread(thread_id)
        current_time = now(request.get("now"))
        changed = False
        previous_status = ""
        updated_goal: ThreadGoal | None = None

        # LLM: 精确 ID、状态和内容 revision 在原锁内比较；活跃耗时沿共享时钟结算。
        # 函数用途: 更新匹配目标的内容或状态，保留其它目标和已累计用量。
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal changed, previous_status, updated_goal
            if not data:
                return data
            goals, error = _goals_from_update_data(data, path=self.storage.goal_path(thread_id))
            if error is not None:
                raise DataCorruptionError(
                    str(error.get("message") or "conversation goal read failed")
                )
            current = _select_thread_goal(goals, goal_id=expected_goal_id)
            if current is None:
                return data
            if expected_status and current.status != expected_status:
                return data
            if expected_revision is not None and current.revision != int(expected_revision):
                return data
            changed = True
            previous_status = current.status
            status = requested_status or current.status
            next_budget = token_budget if "token_budget" in request else current.token_budget
            if (
                status == "active"
                and next_budget is not None
                and current.tokens_used >= next_budget
            ):
                status = "budget_limited"
            elapsed = (
                self.clock.take_elapsed_seconds(current)
                if current.status in {"active", "budget_limited"}
                else 0
            )
            next_time_used = current.time_used_seconds + elapsed
            if (
                status == "active"
                and current.duration_seconds is not None
                and next_time_used >= current.duration_seconds
            ):
                status = "budget_limited"
            updated_goal = replace(
                current,
                objective=objective or current.objective,
                status=status,
                token_budget=next_budget,
                time_used_seconds=next_time_used,
                updated_at=current_time,
                revision=current.revision + 1,
            )
            return _goal_collection_payload(
                [updated_goal if goal.goal_id == current.goal_id else goal for goal in goals]
            )

        update_json_file_atomic(self.storage.goal_path(thread_id), updater, require_existing=True)
        if not changed:
            return None
        updated = updated_goal
        if updated is None:
            return None
        if updated.status == "active":
            self.clock.begin(updated, reset=previous_status != "active")
        else:
            self.clock.clear(updated.thread_id, goal_id=updated.goal_id)
        return updated

    # LLM: 调用方持有目标迁移锁；内部使用 goal/task 双重 CAS，不重复领取同一文件锁；先准备禁用任务链接。
    # 函数用途: 修复旧数据里多个目标共用任务的冲突，保留目标身份、历史用量与原始记录，不自行唤醒。
    def rebind_task(
        self, thread_id: str, *, goal_id: str, expected_task_id: str, task_id: str
    ) -> ThreadGoal | None:
        self._require_thread(thread_id)
        link = self._load_task(task_id)
        if link is None or link.thread_id != thread_id or link.status != "interrupted":
            raise ValueError("goal migration requires a prepared inactive task in this thread")
        migrated = None

        # LLM: 整个源目标快照与新绑定同一次原子落盘，失败不发布半条迁移记录。
        # 函数用途: 比较旧绑定后只替换所选目标，兄弟目标原样保留。
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal migrated
            goals, error = _goals_from_update_data(data, path=self.storage.goal_path(thread_id))
            if error is not None:
                raise DataCorruptionError(str(error.get("message") or "goal migration read failed"))
            current = _select_thread_goal(goals, goal_id=goal_id)
            if current is None or current.task_id != expected_task_id:
                return data
            if any(item.goal_id != goal_id and item.task_id == task_id for item in goals):
                raise ValueError("goal migration destination already belongs to another goal")
            migrated = replace(
                current,
                task_id=task_id,
                metadata={
                    **current.metadata,
                    "task_binding_migration": {
                        "schema_version": "goal-task-binding.v1",
                        "source_goal": current.to_dict(),
                        "reason": "explicit_resume_shared_task",
                        "task_id": task_id,
                        "migrated_at": now(None),
                    },
                },
            )
            return _goal_collection_payload(
                [migrated if item.goal_id == goal_id else item for item in goals]
            )

        update_json_file_atomic(self.storage.goal_path(thread_id), updater, require_existing=True)
        return migrated

    # LLM: Clear removes one exact goal from the per-thread collection; sibling
    # goals, task evidence, and transcript remain intact.
    # 函数用途: 以目标编号 CAS 删除一个持续目标，不影响同会话其他目标。
    def delete(self, thread_id: str, *, expected_goal_id: str = "") -> ThreadGoal | None:
        normalized = str(thread_id or "").strip()
        self._require_thread(normalized)
        path = self.storage.goal_path(normalized)
        deleted: ThreadGoal | None = None

        # LLM: 只删除精确匹配的目标，历史、任务和相邻目标不受影响。
        # 函数用途: 在原目标集合更新中移除所选记录，未匹配时保持原数据。
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal deleted
            goals, error = _goals_from_update_data(data, path=path)
            if error is not None:
                raise DataCorruptionError(
                    str(error.get("message") or "conversation goal read failed")
                )
            selected = _select_thread_goal(goals, goal_id=expected_goal_id)
            if selected is None:
                return data
            deleted = selected
            return _goal_collection_payload(
                [goal for goal in goals if goal.goal_id != selected.goal_id]
            )

        update_json_file_atomic(path, updater, require_existing=True)
        if deleted is not None:
            self.clock.clear(normalized, goal_id=deleted.goal_id)
        return deleted

    # LLM: Charge only the exact active goal; reaching the budget is a system-owned status transition.
    # 函数用途: 原子累计目标 token 和活跃耗时，并在达到预算时标记 budget_limited。
    def account_usage(self, request: dict[str, Any]) -> ThreadGoal | None:
        thread_id = str(request.get("thread_id") or "").strip()
        expected_goal_id = str(request.get("goal_id") or "").strip()
        token_delta = max(0, int(request.get("token_delta") or 0))
        time_delta = max(0, int(request.get("time_delta_seconds") or 0))
        mode = str(request.get("mode") or "active_status_only").strip()
        allowed_statuses = _accounting_statuses(mode)
        self._require_thread(thread_id)
        if token_delta == 0 and time_delta == 0:
            return self.load(thread_id, goal_id=expected_goal_id)
        current_time = now(request.get("now"))
        changed = False
        updated_goal: ThreadGoal | None = None

        # LLM: 按调用方声明的允许状态和目标 ID 记账，用量变化不能增加内容 revision。
        # 函数用途: 累计 token 与时间，在达到原预算时保存 budget_limited。
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal changed, updated_goal
            if not data:
                return data
            goals, error = _goals_from_update_data(data, path=self.storage.goal_path(thread_id))
            if error is not None:
                raise DataCorruptionError(
                    str(error.get("message") or "conversation goal read failed")
                )
            current = _select_thread_goal(goals, goal_id=expected_goal_id)
            if current is None or current.status not in allowed_statuses:
                return data
            changed = True
            tokens_used = current.tokens_used + token_delta
            status = current.status
            if (
                current.status == "active"
                and current.token_budget is not None
                and tokens_used >= current.token_budget
            ):
                status = "budget_limited"
            time_used = current.time_used_seconds + time_delta
            if (
                current.status == "active"
                and current.duration_seconds is not None
                and time_used >= current.duration_seconds
            ):
                status = "budget_limited"
            updated_goal = replace(
                current,
                tokens_used=tokens_used,
                time_used_seconds=time_used,
                status=status,
                updated_at=current_time,
            )
            return _goal_collection_payload(
                [updated_goal if goal.goal_id == current.goal_id else goal for goal in goals]
            )

        update_json_file_atomic(self.storage.goal_path(thread_id), updater, require_existing=True)
        return updated_goal if changed else None
