# LLM: 单进程名额只由同一 Condition 下的计数裁决；保留容量与占用原子核对，默认准入合同不变，同步 admission 测试。
# 模块用途: 限制实际在途调用并允许可选工作给普通调用留名额；不提供跨进程或跨机器配额保证。
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager


# LLM: 此类型只表示未取得资源；调用者不得把它误报为供应商已经发出请求或消耗用量。
# 类用途: 通知上层并发名额不足，可按自身流程等待、拒绝或跳过增强。
class ConcurrencyTimeout(Exception):
    """等不到并发槽(在途调用已达上限且超时)。上层应排队/降级,不是闷等。"""


# LLM: 普通与保留名额请求共享唯一计数；退出上下文才释放，不能让等待者超时释放仍执行的 worker 名额。
# 类用途: 在单进程内按真实调用生命周期领取和释放并发名额。
class ConcurrencyLimiter:

    # LLM: 实例只持有容量与当前占用，不再并行维护 semaphore 和另一套统计。
    # 函数用途: 初始化固定容量的准入条件变量。
    def __init__(self, max_in_flight: int) -> None:
        if max_in_flight < 1:
            raise ValueError("max_in_flight 必须 ≥ 1")
        self._max = int(max_in_flight)
        self._in_flight = 0
        self._condition = threading.Condition()

    # LLM: reserve 仅影响本次领取，不降低普通调用容量；检查/占用同锁，0 秒立即返回，业务异常不泄漏名额。
    # 函数用途: 为实际执行领取一个名额，可选工作指定保留容量；作用域退出时唤醒等待的普通请求。
    @contextmanager
    def slot(self, timeout: float | None = None, *, reserve: int = 0) -> Iterator[None]:
        if type(reserve) is not int or reserve < 0:
            raise ValueError("reserve 必须为非负整数")
        with self._condition:
            available = self._max - reserve
            if available <= 0 or not self._condition.wait_for(lambda: self._in_flight < available, timeout):
                raise ConcurrencyTimeout(f"等不到并发槽(上限 {self._max},等待 {timeout}s 超时)")
            self._in_flight += 1
        try:
            yield
        finally:
            with self._condition:
                self._in_flight -= 1
                self._condition.notify_all()

    # LLM: 统计读同一准入计数，不推导线程数或形成第二容量权威。
    # 函数用途: 返回当前确实持有名额的调用数。
    def in_flight(self) -> int:
        with self._condition:
            return self._in_flight
