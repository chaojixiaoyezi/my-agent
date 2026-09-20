# LLM: 单 Gateway 进程中，同一 canonical store 共用目标时钟；进程重启没有旧单调时间基线。
# 模块用途: 防止前台、后台和控制入口各建一个时钟，把同一段等待时间重复记入 Goal。
from __future__ import annotations

import os
import threading
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path

from .models import ThreadGoal


# LLM: 锁与计时字典同生命周期，由持有该 owner store 的调用者强引用；不存模型文本或凭据。
# 类用途: 保存并操作一个会话存储的共享时钟，统一结算和只读展示，最后一个运行对象释放后可回收。
@dataclass
class GoalClockGroup:
    lock: object = field(default_factory=threading.Lock)
    clocks: dict[str, tuple[str, float]] = field(default_factory=dict)

    # LLM: The live wall clock is process-local like 会话运行时 GoalWallClockAccounting;
    # persisted updated_at is presentation metadata and must never be used as a timer.
    # 函数用途: 启动或恢复同一目标的运行时计时基线，服务停机时不累计耗时。
    def begin(
        self,
        goal: ThreadGoal,
        *,
        reset: bool = False,
        monotonic_now: float | None = None,
    ) -> None:
        current = time.monotonic() if monotonic_now is None else float(monotonic_now)
        with self.lock:
            prior = self.clocks.get(goal.goal_id)
            if reset or prior is None or prior[0] != goal.thread_id:
                self.clocks[goal.goal_id] = (goal.thread_id, current)

    # LLM: Whole-second accounting preserves the fractional remainder, matching
    # 会话运行时's Instant::elapsed().as_secs() plus mark_accounted advance behavior.
    # 函数用途: 取出本目标自上次结算后的完整秒数，并推进内存计时基线。
    def take_elapsed_seconds(
        self,
        goal: ThreadGoal,
        *,
        monotonic_now: float | None = None,
    ) -> int:
        current = time.monotonic() if monotonic_now is None else float(monotonic_now)
        with self.lock:
            prior = self.clocks.get(goal.goal_id)
            if prior is None or prior[0] != goal.thread_id:
                self.clocks[goal.goal_id] = (goal.thread_id, current)
                return 0
            elapsed = max(0, int(current - prior[1]))
            if elapsed > 0:
                self.clocks[goal.goal_id] = (goal.thread_id, prior[1] + elapsed)
            return elapsed

    # LLM: 状态展示只读本目标已落盘秒数与共享时钟，不推进结算基线，不跨线程或 owner 查询。
    # 函数用途: 显示已结算及当前运行的总时间，暂停目标只返回已结算值。
    def current_time_seconds(
        self,
        goal: ThreadGoal,
        *,
        monotonic_now: float | None = None,
    ) -> int:
        """Return persisted plus current live seconds without advancing accounting."""
        if goal.status != "active":
            return max(0, int(goal.time_used_seconds))
        current = time.monotonic() if monotonic_now is None else float(monotonic_now)
        with self.lock:
            prior = self.clocks.get(goal.goal_id)
            live = (
                max(0, int(current - prior[1]))
                if prior is not None and prior[0] == goal.thread_id
                else 0
            )
        return max(0, int(goal.time_used_seconds)) + live

    # LLM: Paused, blocked, limited, completed, replaced, or cleared goals stop
    # the active runtime clock without changing their durable usage snapshot.
    # 函数用途: 清除指定目标的运行时计时状态，避免后续目标继承旧基线。
    def clear(self, thread_id: str, *, goal_id: str = "") -> None:
        with self.lock:
            if goal_id:
                prior = self.clocks.get(goal_id)
                if prior is not None and prior[0] == thread_id:
                    self.clocks.pop(goal_id, None)
                return
            stale = [
                current_goal_id
                for current_goal_id, (current_thread_id, _started_at) in self.clocks.items()
                if current_thread_id == thread_id
            ]
            for current_goal_id in stale:
                self.clocks.pop(current_goal_id, None)


_CLOCK_GROUPS: weakref.WeakValueDictionary[tuple[int, str], GoalClockGroup] = weakref.WeakValueDictionary()
_CLOCK_GROUPS_LOCK = threading.Lock()


# LLM: resolved root 和 PID 是共享边界；同名 goal ID 不能跨 owner 共用，fork 后的新 store 不继承父进程基线。
# 函数用途: 为不同 Store 实例取得持有同一把锁和计时操作的共享对象，不写磁盘、不启动后台任务。
def shared_goal_clocks(root: Path) -> GoalClockGroup:
    key = (os.getpid(), str(root.resolve(strict=False)))
    with _CLOCK_GROUPS_LOCK:
        group = _CLOCK_GROUPS.get(key)
        if group is None:
            group = GoalClockGroup()
            _CLOCK_GROUPS[key] = group
        return group
