# LLM: 单 Gateway 进程中，同一 canonical store 共用目标时钟；进程重启没有旧单调时间基线。
# 模块用途: 防止前台、后台和控制入口各建一个时钟，把同一段等待时间重复记入 Goal。
from __future__ import annotations

import os
import threading
import weakref
from dataclasses import dataclass, field
from pathlib import Path


# LLM: 锁与计时字典同生命周期，由持有该 owner store 的调用者强引用；不存模型文本或凭据。
# 类用途: 保存一个会话存储的共享计时状态，最后一个运行对象释放后可回收。
@dataclass
class GoalClockGroup:
    lock: object = field(default_factory=threading.Lock)
    clocks: dict[str, tuple[str, float]] = field(default_factory=dict)


_CLOCK_GROUPS: weakref.WeakValueDictionary[tuple[int, str], GoalClockGroup] = weakref.WeakValueDictionary()
_CLOCK_GROUPS_LOCK = threading.Lock()


# LLM: resolved root 和 PID 是共享边界；同名 goal ID 不能跨 owner 共用，fork 后的新 store 不继承父进程基线。
# 函数用途: 为不同 Store 实例取得同一把目标时钟锁，不写磁盘、不启动后台任务。
def shared_goal_clocks(root: Path) -> GoalClockGroup:
    key = (os.getpid(), str(root.resolve(strict=False)))
    with _CLOCK_GROUPS_LOCK:
        group = _CLOCK_GROUPS.get(key)
        if group is None:
            group = GoalClockGroup()
            _CLOCK_GROUPS[key] = group
        return group
