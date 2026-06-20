"""Worker 进程入口(Tier 5 部署):消费入站队列、跑 handler,SIGTERM 优雅停。

装配链:StorageBackend(DATABASE_URL)→ IngressQueue(ensure_schema)→ WorkerPool(handler)。
worker 无状态(状态全在 PG/队列),可横向加副本——这正是 my-agent 解掉了 通道运行时 单副本约束、
能放心多副本滚动的根因(研究核验)。``python -m ...worker_entry`` 走 serve():装 SIGTERM 漏排
(on_drain=pool.stop 优雅停消费,在途消息靠心跳续租跑完)后挂起等退出信号。

handler 经 env ``WORKER_HANDLER='module:func'`` 注入(真实接入 agent 主循环的挂载点);
未配则用 stub(只记录,便于先把部署链路跑通)。env:WORKER_CONCURRENCY(默认 4)。
"""

from __future__ import annotations

import importlib
import os

from agent_py_agent.agent.asgi_entry import backend_from_env
from agent_py_agent.agent.graceful import DrainState, install_sigterm_drain
from agent_py_agent.agent.ingress_queue import IngressQueue
from agent_py_agent.agent.queue_worker import Handler, WorkerPool
from agent_py_agent.agent.storage_backend import StorageBackend


def _stub_handler(payload: dict) -> None:  # pragma: no cover - 占位,真实接入用 WORKER_HANDLER
    print(f"[worker:stub] 处理消息(未配 WORKER_HANDLER):{list(payload)[:5]}")


def load_handler() -> Handler:
    """从 env WORKER_HANDLER='module:func' 加载真实 handler;未配则用 stub。"""
    spec = os.environ.get("WORKER_HANDLER", "")
    if not spec:
        return _stub_handler
    module_name, _, func_name = spec.partition(":")
    return getattr(importlib.import_module(module_name), func_name)


def build_pool(handler: Handler, backend: StorageBackend | None = None) -> tuple[WorkerPool, IngressQueue]:
    """装配 worker 池 + 队列。backend 可注入做测试(默认从 env)。返回 (pool, queue)。"""
    backend = backend or backend_from_env()
    queue = IngressQueue(backend)
    queue.ensure_schema()
    workers = int(os.environ.get("WORKER_CONCURRENCY", "4"))
    return WorkerPool(queue, handler, workers=workers), queue


def serve() -> None:  # pragma: no cover - 真进程入口(容器内跑)
    drain = DrainState()
    pool, _queue = build_pool(load_handler())
    install_sigterm_drain(drain, on_drain=pool.stop)  # SIGTERM → 停领新活、在途跑完
    pool.start()
    drain.wait()  # 挂起到退出信号


if __name__ == "__main__":  # pragma: no cover
    serve()
