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

from agent_py_agent.agent.asgi_entry import backend_from_env, env_int
from agent_py_agent.agent.graceful import DrainState, install_sigterm_drain
from agent_py_agent.agent.ingress_queue import IngressQueue
from agent_py_agent.agent.pg_rls import require_restricted_app_role
from agent_py_agent.agent.queue_worker import Handler, StaleReaper, WorkerPool
from agent_py_agent.agent.scale_runtime import ScaleRole, ScaleRuntimeConfig
from agent_py_agent.agent.storage_backend import StorageBackend


def _stub_handler(payload: dict) -> None:  # pragma: no cover - 占位,真实接入用 WORKER_HANDLER
    print(f"[worker:stub] 处理消息(未配 WORKER_HANDLER):{list(payload)[:5]}")


def load_handler() -> Handler:
    """从 env WORKER_HANDLER='module:func' 加载真实 handler;未配则用 stub。"""
    ScaleRuntimeConfig.from_env(ScaleRole.WORKER, os.environ)
    spec = os.environ.get("WORKER_HANDLER", "")
    if not spec:
        return _stub_handler
    module_name, _, func_name = spec.partition(":")
    return getattr(importlib.import_module(module_name), func_name)


def build_pool(handler: Handler, backend: StorageBackend | None = None) -> tuple[WorkerPool, IngressQueue]:
    """装配 worker 池 + 队列。backend 可注入做测试(默认从 env)。返回 (pool, queue)。"""
    runtime = ScaleRuntimeConfig.from_env(ScaleRole.WORKER, os.environ)
    backend = backend or (
        StorageBackend(runtime.database_url) if runtime.is_scale else backend_from_env()
    )
    queue = IngressQueue(backend, release_channel=runtime.release_channel)
    if runtime.is_scale:
        from agent_py_agent.agent.runtime_schema import require_runtime_schema_current

        require_restricted_app_role(backend, runtime.database_app_role)
        require_runtime_schema_current(
            backend,
            include_scale_data=True,
            app_role=runtime.database_app_role,
        )
    else:
        queue.ensure_schema()
    workers = env_int("WORKER_CONCURRENCY", 4)
    return WorkerPool(queue, handler, workers=workers), queue


# LLM: 企业 worker 是用户命令执行节点，启动前必须通过完整 bwrap 自检；失败应让进程
#   非零退出并由编排层摘除，不能先领取消息再在工具调用处发现隔离缺失。
# 函数用途: 启动带 sandbox 硬门、优雅退出和过期租约回收的企业队列 worker。
def serve() -> None:  # pragma: no cover - 真进程入口(容器内跑)
    from agent_py_agent.agent.common.thread_hooks import install_thread_excepthook
    from agent_py_agent.agent.observability.otel import configure_otel_from_env
    from agent_py_agent.agent.tooling.sandbox import require_sandbox_ready

    require_sandbox_ready()
    runtime = ScaleRuntimeConfig.from_env(ScaleRole.WORKER, os.environ)
    backend = StorageBackend(runtime.database_url) if runtime.is_scale else backend_from_env()
    from agent_py_agent.agent.owner_object_store import require_owner_store_ready

    require_owner_store_ready(runtime, backend)
    configure_otel_from_env(
        service_name="my-agent-worker",
        env=os.environ,
        required=runtime.is_scale,
    )
    install_thread_excepthook()  # 后台线程(worker/reaper)未捕获异常落日志可告警,不静默死(审计 #19)
    drain = DrainState()
    pool, queue = build_pool(load_handler(), backend)
    reaper = StaleReaper(queue)  # 周期回收崩溃 worker 的租约,防会话永久卡死

    def _stop_all() -> None:
        pool.stop()
        reaper.stop()

    install_sigterm_drain(drain, on_drain=_stop_all)  # SIGTERM → 停领新活、停 reaper、在途跑完
    pool.start()
    reaper.start()
    drain.wait()  # 挂起到退出信号


if __name__ == "__main__":  # pragma: no cover
    serve()
