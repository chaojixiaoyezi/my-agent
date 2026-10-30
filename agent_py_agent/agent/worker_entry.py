# LLM: Worker入口保留原sandbox/数据库/owner硬门；信号只停止新活，实际排空由同一总deadline核验，unfinished必须非零退出。
# 模块用途: 装配企业队列消费者并记录可信的停止结果，不把超时当完成。
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
import json
import os
import time

from agent_py_agent.agent.asgi_entry import backend_from_env, env_int
from agent_py_agent.agent.graceful import DrainState, install_sigterm_drain
from agent_py_agent.agent.ingress_queue import IngressQueue
from agent_py_agent.agent.pg_rls import require_restricted_app_role
from agent_py_agent.agent.queue_worker import DrainReport, Handler, StaleReaper, WorkerPool
from agent_py_agent.agent.scale_runtime import ScaleRole, ScaleRuntimeConfig
from agent_py_agent.agent.storage_backend import StorageBackend


# LLM: 仅原未配置处理器的显式诊断占位，不代表业务成功；真实部署仍由WORKER_HANDLER装配。
# 函数用途: 输出未配置处理器的公开字段名，不打印正文。
def _stub_handler(payload: dict) -> None:  # pragma: no cover - 占位,真实接入用 WORKER_HANDLER
    print(f"[worker:stub] 处理消息(未配 WORKER_HANDLER):{list(payload)[:5]}")


# LLM: 只沿原显式环境装配处理器，不解析任务自然语言或绕过scale配置校验。
# 函数用途: 加载部署指定的消息处理函数。
def load_handler() -> Handler:
    """从 env WORKER_HANDLER='module:func' 加载真实 handler;未配则用 stub。"""
    ScaleRuntimeConfig.from_env(ScaleRole.WORKER, os.environ)
    spec = os.environ.get("WORKER_HANDLER", "")
    if not spec:
        return _stub_handler
    module_name, _, func_name = spec.partition(":")
    return getattr(importlib.import_module(module_name), func_name)


# LLM: 保持数据库角色/schema校验与原队列权威，不在装配时领取消息。
# 函数用途: 从部署配置构造消费者池和原入站队列。
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


# LLM: 池与维护线程共用唯一deadline；超时后的原claim仍由原持久租约/recovery处理，不能写假complete。
# 函数用途: 停领新活、保留在途心跳，并有界等待全部后台组件。
def drain_worker_components(pool: WorkerPool, reaper: StaleReaper, *, timeout: float, deadline: float | None = None) -> DrainReport:
    until = time.monotonic() + max(0.0, timeout)
    if deadline is not None:
        until = min(until, deadline)
    # 先关闭全部消费者准入，心跳仍随原handler存活；维护线程不再领取新的回收工作。
    for worker in pool._pool:
        worker.stop(deadline=until)
    reaper.stop()
    result = pool.stop(deadline=until)
    if reaper.is_alive():
        reaper.join(timeout=max(0.0, until - time.monotonic()))
    unfinished = result.unfinished + int(reaper.is_alive())
    return DrainReport(unfinished == 0, unfinished, until, result.in_flight)


# LLM: 启动先通过原bwrap/数据库门；信号只翻drain，主线程finally执行共享deadline并按真实unfinished选择退出码。
# 函数用途: 运行企业worker，排空超时保留恢复事实并非零退出。
def serve() -> int:  # pragma: no cover - 真进程入口(容器内跑)
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

    install_sigterm_drain(drain)
    try:
        pool.start()
        reaper.start()
        drain.wait()
    finally:
        result = drain_worker_components(pool, reaper, timeout=env_int('WORKER_DRAIN_SECONDS', 110))
        print(json.dumps({'event': 'worker_drain', 'budget_source': 'WORKER_DRAIN_SECONDS/default110', **result.to_dict()}), flush=True)
    return 0 if result.drained else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(serve())
