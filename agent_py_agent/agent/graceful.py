"""优雅退出(Tier 5 零停机基础):就绪门翻转 + SIGTERM 漏排。

K8s 滚动升级零停机的应用侧基础:收到 SIGTERM 先把 draining 翻起(就绪探针 /readyz 转 503 → Service
摘除该 pod 的 endpoint、LB 停止往这送新流量),再配合清单里的 preStop sleep + terminationGracePeriodSeconds
让在途 HTTP / in-flight job 跑完才真退。

研究核验:参考实现 把 drain 埋在 cli 的 finally(uvicorn/gunicorn 收 SIGTERM 时不一定可靠触发),
且 readyz 就绪门 / preStop / terminationGracePeriodSeconds 这套 K8s 层零停机配置三家都没有——本模块补应用侧。
signal 只能在主线程注册;worker 池把 on_drain=pool.stop 传进来即可在退出信号时优雅停消费。
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable


class DrainState:
    """退出漏排状态:start_draining() 后 is_draining()=True,就绪探针据此转 503 摘流量。线程安全。"""

    def __init__(self) -> None:
        self._draining = threading.Event()

    def start_draining(self) -> None:
        self._draining.set()

    def is_draining(self) -> bool:
        return self._draining.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """阻塞直到开始漏排(worker 主循环用:挂起等退出信号)。返回是否已漏排。"""
        return self._draining.wait(timeout)


def install_sigterm_drain(drain: DrainState, on_drain: Callable[[], None] | None = None) -> None:
    """注册 SIGTERM/SIGINT 处理:翻 draining + 可选 on_drain 回调(如 pool.stop 优雅停 worker)。

    仅主线程可注册(Python signal 限制)。幂等可重复调用(覆盖)。
    """

    def _handler(_signum: int, _frame: object) -> None:
        drain.start_draining()
        if on_drain is not None:
            on_drain()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
