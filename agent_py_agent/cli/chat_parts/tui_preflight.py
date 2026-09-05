# LLM: 本模块把真实 Gateway readiness 映射为 TUI 启动事件；探测仍复用 canonical Gateway state，不另建健康事实源。
# 模块用途: 在 TUI 已可见后后台等待 Gateway 并准备恢复历史，全部成功才启动 worker，失败则安全退出。

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .tui_runtime import TuiRuntime


# LLM: 同一 Application 的 prepare_session 只能在 readiness 后读恢复数据，返回空错误码表示成功；不能执行任务或另起 Gateway。
# 类用途: 描述一次连接、可选会话恢复及 worker 启动所需依赖，不保存端点或密钥。
@dataclass(frozen=True)
class TuiGatewayPreflight:
    application: Any
    runtime: TuiRuntime
    paths: Any
    timeout_seconds: float
    stop_event: threading.Event
    on_ready: Callable[[], None]
    prepare_session: Callable[[], str] | None = None


# LLM: compatibility wrapper preserves the test/adapter patch point while delaying the heavy daemon status stack until the preflight thread runs.
# 函数用途: 在后台探活真正开始时按需加载并调用 Gateway readiness 检查。
def wait_for_gateway_running(paths: Any, *, timeout: float):
    from ...agent.gateway_parts.status_rendering import wait_for_gateway_running as _wait

    return _wait(paths, timeout=timeout)


# LLM: preflight 必须由 Application pre_run 调用，确保失败时可经 event-loop thread-safe exit；返回线程只供测试等待。
# 函数用途: 显示连接动画并启动真实 Gateway readiness 后台检查。
def start_tui_gateway_preflight(config: TuiGatewayPreflight) -> threading.Thread:
    config.runtime.publish_connection_check()
    thread = threading.Thread(
        target=_run_gateway_preflight,
        args=(config,),
        name="my-agent-tui-gateway-preflight",
        daemon=True,
    )
    thread.start()
    return thread


# LLM: readiness 后才允许有界历史读取，准备失败不得启动 worker；结果经 loop 提交，退出后迟到回调不得复活客户端。
# 函数用途: 后台顺序连接服务、准备会话，把完整启动结果排队送回界面线程。
def _run_gateway_preflight(config: TuiGatewayPreflight) -> None:
    error_code = ""
    try:
        _state, alive = wait_for_gateway_running(
            config.paths,
            timeout=max(0.0, float(config.timeout_seconds or 0.0)),
        )
        if config.stop_event.is_set():
            return
        if alive and config.prepare_session is not None:
            error_code = config.prepare_session()
            alive = not bool(error_code)
    except Exception as exc:  # noqa: BLE001 探活异常必须成为 typed 启动失败而不能杀后台线程
        alive = False
        error_code = type(exc).__name__
    final_error_code = error_code or ("" if alive else "GATEWAY_NOT_READY")
    loop = getattr(config.application, "loop", None)
    if loop is not None and not bool(getattr(loop, "is_closed", lambda: False)()):
        loop.call_soon_threadsafe(
            lambda: _finish_gateway_preflight(
                config,
                alive=bool(alive),
                error_code=final_error_code,
            )
        )
        return
    _finish_gateway_preflight(
        config,
        alive=bool(alive),
        error_code=final_error_code,
    )


# LLM: 这是 preflight 唯一状态落点；已退出时不发布事件，否则在 loop 更新 runtime/启动 worker 后 invalidate。
# 函数用途: 落实连接与历史恢复结果，忽略用户退出后才返回的旧结果。
def _finish_gateway_preflight(
    config: TuiGatewayPreflight,
    *,
    alive: bool,
    error_code: str,
) -> None:
    future = getattr(config.application, "future", None)
    if config.stop_event.is_set() or (future is not None and future.done()):
        return
    config.runtime.resolve_connection_check(ok=alive, error_code=error_code)
    if alive:
        config.on_ready()
        config.application.invalidate()
        return
    config.stop_event.set()
    config.application.invalidate()
    _exit_failed_application(config.application)


# LLM: 迟到失败不能覆盖用户已经退出的 Application future；只有当前 run 尚活跃时设置错误返回码。
# 函数用途: 在线程安全回调中以退出码 2 结束启动失败的 TUI。
def _exit_failed_application(application: Any) -> None:
    future = getattr(application, "future", None)
    if future is None or future.done():
        return
    application.exit(result=2)


__all__ = ["TuiGatewayPreflight", "start_tui_gateway_preflight"]
