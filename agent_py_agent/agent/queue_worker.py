"""队列 worker(Tier 1.1 企业规模化):消费入站队列、跑 handler,闭合两层架构。

研究确认 worker 池是简单循环、**自建**可控无额外依赖(claw 已自建)。无状态:状态全在队列/DB,
worker 可横向加副本,跨实例靠多副本 + 队列 SKIP-LOCKED 分发。每消息一个**心跳线程**按 lease/2
续租——防多步 LLM turn(可能数十秒)超 lease 被 recover_stale 误回收重复处理(claw 标注"验收 CRITICAL")。
handler 抛异常 → fail(不卡队列);CPU 密集 handler 应放独立进程(Python GIL,研究提醒)。
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from agent_py_agent.agent.ingress_queue import ClaimedMessage, IngressQueue

Handler = Callable[[dict], None]


class _Heartbeat(threading.Thread):
    """每消息一个心跳线程:每 lease/2 续租,防长 turn 超 lease 被误回收。"""

    def __init__(self, queue: IngressQueue, claim_token: str, lease_seconds: int) -> None:
        super().__init__(daemon=True)
        self._queue = queue
        self._token = claim_token
        self._lease = lease_seconds
        self._stop = threading.Event()

    def run(self) -> None:
        interval = max(1.0, self._lease / 2)
        while not self._stop.wait(interval):
            self._queue.heartbeat(self._token, lease_seconds=self._lease)

    def stop(self) -> None:
        self._stop.set()


class QueueWorker:
    """单 worker:领一条→跑 handler(带心跳续租)→complete/fail。"""

    def __init__(self, queue: IngressQueue, handler: Handler, *, lease_seconds: int = 120, poll_interval: float = 0.5) -> None:
        self._queue = queue
        self._handler = handler
        self._lease = lease_seconds
        self._poll = poll_interval
        self._stop = threading.Event()

    def run_once(self) -> bool:
        """领一条处理(成功返回 True;无可领返回 False)。"""
        msg = self._queue.claim(lease_seconds=self._lease)
        if msg is None:
            return False
        self._process(msg)
        return True

    def _process(self, msg: ClaimedMessage) -> None:
        hb = _Heartbeat(self._queue, msg.claim_token, self._lease)
        hb.start()
        try:
            self._handler(msg.payload)
            self._queue.complete(msg.claim_token)
        except Exception:
            self._queue.fail(msg.claim_token)  # handler 失败 → 标 failed,不卡队列/lane
        finally:
            hb.stop()

    def run_forever(self) -> None:
        while not self._stop.is_set():
            if not self.run_once():
                self._stop.wait(self._poll)  # 空闲退避

    def stop(self) -> None:
        self._stop.set()


class WorkerPool:
    """N 个 worker 线程消费同一队列(单实例内并发;跨实例靠多副本 + 队列 SKIP-LOCKED 分发)。"""

    def __init__(self, queue: IngressQueue, handler: Handler, *, workers: int = 2, lease_seconds: int = 120) -> None:
        self._pool = [QueueWorker(queue, handler, lease_seconds=lease_seconds) for _ in range(max(1, workers))]
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        for w in self._pool:
            t = threading.Thread(target=w.run_forever, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self, timeout: float = 5.0) -> None:
        for w in self._pool:
            w.stop()
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads = []
