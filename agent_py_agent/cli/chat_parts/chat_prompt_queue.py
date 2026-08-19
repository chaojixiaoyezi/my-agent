# LLM: 本模块只对 chat 已有的 canonical queue.Queue 做原子筛选取回；不得复制任务、另建影子队列或从 UI 文案反推可编辑性。
# 模块用途: 让 TUI 在 worker 并发消费期间安全取回尚未执行的排队消息，并正确维护 Queue.join 计数。

from __future__ import annotations

from collections.abc import Callable
from queue import Queue
from typing import Any


# LLM: predicate 在 Queue.mutex 内只读取 job 的稳定字段；调用方不得在 predicate 中阻塞、入队或再次操作同一 Queue。
# 函数用途: 从真实任务队列原子移除所有匹配项，保留未匹配顺序，并同步 unfinished_tasks/not_full 条件。
def pop_all_matching(
    jobs: Queue[Any],
    predicate: Callable[[Any], bool],
) -> tuple[Any, ...]:
    with jobs.mutex:
        retained: list[Any] = []
        removed: list[Any] = []
        for job in jobs.queue:
            (removed if predicate(job) else retained).append(job)
        if not removed:
            return ()
        if int(jobs.unfinished_tasks) < len(removed):
            raise RuntimeError("chat Queue unfinished_tasks is smaller than removed pending jobs")
        jobs.queue.clear()
        jobs.queue.extend(retained)
        jobs.unfinished_tasks = int(jobs.unfinished_tasks) - len(removed)
        if jobs.unfinished_tasks == 0:
            jobs.all_tasks_done.notify_all()
        jobs.not_full.notify_all()
        return tuple(removed)


__all__ = ["pop_all_matching"]
