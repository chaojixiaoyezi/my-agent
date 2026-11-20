"""最近空闲优先唤醒的可取消工作队列（自 agent/gateway_parts/http_lanes.py 原样迁出）。

行为与 http_lanes 内的 _LifoWakeCondition/_CancellableWorkQueue 完全一致，只换成公开名（C1.6）。
discard 只做槽位卫生：移除排队项、返回 None，从不作为“工作项没有运行”的证明（该证明归 work_item_pool 的
句柄状态机）。M22 把 http_lanes 的本地定义改为导入本模块，此后只剩这一份实现。
"""

from __future__ import annotations

import threading
from queue import Queue


# LLM: 3.14t 的 mimalloc 按线程保留峰值；FIFO 唤醒会把短请求轮转到池内每个空闲线程，最终每个线程都长到峰值。
#   这里只改“哪个等待线程接手”（最近空闲者优先），条目仍按 Queue 原 FIFO 出队，容量、超时与取消语义不变。
# 类用途: 让 put 唤醒最近进入等待的消费者，使短请求集中在少数热线程上。
class LifoWakeCondition(threading.Condition):
    # 字段用途: 已唤醒但尚未取项的等待者数；只在同一 mutex 内增减，供扩容判定计入即将接手的 worker。
    pending_wakeups = 0

    # 函数用途: 与标准 notify 相同的唤醒/计数规则，只从等待表尾部（最近等待者）开始。
    def notify(self, n: int = 1) -> None:
        if not self._is_owned():
            raise RuntimeError("cannot notify on un-acquired lock")
        waiters = self._waiters
        while waiters and n > 0:
            waiter = waiters.pop()
            try:
                waiter.release()
            except RuntimeError:
                continue  # 已被前一次 notify 释放但尚未移除的等待者，不计入本次唤醒。
            n -= 1
            self.pending_wakeups += 1


# LLM: 原daemon执行器沿用CPython WorkItem；Future取消本身不会删除排队项，必须在归还槽位前移除。
# 类用途: 为运输池与工作项池提供可按原对象身份丢弃的工作队列，避免取消占位随历史增长。
class CancellableWorkQueue(Queue):
    # 函数用途: 建立原 Queue 后只把 not_empty 换成最近空闲优先的唤醒条件，共用同一把 mutex。
    def __init__(self, maxsize: int = 0) -> None:
        super().__init__(maxsize)
        self.not_empty = LifoWakeCondition(self.mutex)

    # LLM: 任何取项（被唤醒者或抢先的 get_nowait）都消耗一次待接手唤醒；被抢的唤醒者会重新进入等待表，计数仍一致。
    # 函数用途: 在原 mutex 内出队并扣减一个待接手唤醒。
    def _get(self):
        self.not_empty.pending_wakeups = max(0, self.not_empty.pending_wakeups - 1)
        return super()._get()

    # LLM: 只在原 mutex 内比较排队项与（挂起 + 已唤醒未取项）worker 数，不唤醒、不取项；多算只会多起一个线程。
    # 函数用途: 告诉执行器当前排队工作是否超过真实空闲容量，即是否需要新 worker。
    def needs_worker(self) -> bool:
        with self.mutex:
            return len(self.queue) > len(self.not_empty._waiters) + self.not_empty.pending_wakeups

    # LLM: 仅删除尚未被worker取走的原WorkItem，不调用Future或用户代码；运行中的项由原CAS处理。
    # 函数用途: 在取消或提交中断后移除对应的物理排队项。
    def discard(self, owner: object) -> None:
        with self.mutex:
            for item in self.queue:
                args = getattr(item, 'args', ())
                # CPython新worker context将原(fn, args, kwargs)保存在task中。
                task = getattr(item, 'task', None)
                if not args and isinstance(task, tuple) and len(task) == 3:
                    args = task[1]
                if getattr(item, 'future', None) is owner or (args and args[0] is owner):
                    self.queue.remove(item)
                    self.not_empty.pending_wakeups = max(0, self.not_empty.pending_wakeups - 1)
                    self.unfinished_tasks -= 1
                    self.not_full.notify()
                    return


__all__ = ["CancellableWorkQueue", "LifoWakeCondition"]
