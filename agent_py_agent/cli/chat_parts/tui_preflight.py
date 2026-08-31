# LLM: 本模块把真实 Gateway readiness 映射为 TUI 启动事件；探测仍复用 canonical Gateway state，不另建健康事实源。
# 模块用途: 在 TUI 已可见后后台等待 Gateway，成功才启动聊天 worker，失败则安全退出并返回错误码。

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .tui_runtime import TuiRuntime


# LLM: 配置束只携带同一 Application/TuiRuntime、Gateway paths 和一次 worker starter；不保存 endpoint 或 secret。
# 类用途: 描述一次 TUI Gateway 启动探活所需依赖。
@dataclass(frozen=True)
class TuiGatewayPreflight:
    application: Any
    runtime: TuiRuntime
    paths: Any
    timeout_seconds: float
    stop_event: threading.Event
    on_ready: Callable[[], None]


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


# LLM: wait 结果是 readiness 唯一事实；后台线程只产生结果，状态切换必须投递回 Application loop 以免首帧丢失刷新。
# 函数用途: 等待 Gateway 就绪，并把终态排队送回 TUI 事件循环。
def _run_gateway_preflight(config: TuiGatewayPreflight) -> None:
    error_code = ""
    try:
        _state, alive = wait_for_gateway_running(
            config.paths,
            timeout=max(0.0, float(config.timeout_seconds or 0.0)),
        )
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


# LLM: 这是 Gateway preflight 的唯一状态落点；必须在 Application loop 内先更新 runtime/启动 worker，再 invalidate 触发可见首帧。
# 函数用途: 在 TUI 事件循环里落实连接成功或失败结果。
def _finish_gateway_preflight(
    config: TuiGatewayPreflight,
    *,
    alive: bool,
    error_code: str,
) -> None:
    config.runtime.resolve_connection_check(ok=alive, error_code=error_code)
    if alive:
        future = getattr(config.application, "future", None)
        if not config.stop_event.is_set() and (future is None or not future.done()):
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
