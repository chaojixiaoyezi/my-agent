# LLM: 本模块把真实 Gateway readiness 映射为 TUI 启动事件；探测必须调用
#   agent.gateway_parts.status_rendering 的唯一权威判据（gateway_readiness / wait_for_gateway_readiness），
#   不允许在这里或别处再写"PID 活就 prepare_session"。进程已活但 state 仍 starting 时只显示
#   "启动中"并继续等待到真 ready（有界）；只有真失败（进程消退/服务发布 failed）才立刻退出。
# 模块用途: 在 TUI 已可见后后台等待 Gateway 并准备恢复历史，全部成功才启动 worker；失败按结构化
#   reason 分型后以退出码 2 安全退出。

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .tui_runtime import TuiRuntime

# LLM: "启动中"提示只走 runtime 既有的 footer notice 通道，带独立 notice_kind 便于精确清除；
#   它不写 journal、不创建 turn、不改 Gateway 状态，只反映结构化 readiness 观测。
_GATEWAY_STARTING_NOTICE_KIND = "gateway_startup"
_GATEWAY_STARTING_NOTICE_SECONDS = 2.0
_GATEWAY_STARTING_NOTICE_REFRESH_SECONDS = 1.0
_GATEWAY_STARTING_NOTICE_TEXT = "Gateway 启动中…（等待 HTTP 就绪）"

# LLM: 失败文案由结构化 reason 映射，禁止反向从文案推断分型；未列出的 reason 回落到通用说明。
_GATEWAY_FAILURE_NOTICES = {
    "GATEWAY_START_TIMEOUT": "Gateway 启动超时（仍在启动中），未开始处理任务。",
    "GATEWAY_PROCESS_EXITED": "Gateway 进程已退出，未开始处理任务。",
    "GATEWAY_START_FAILED": "Gateway 服务启动失败，请查看 gateway 日志。",
    "GATEWAY_NOT_READY": "Gateway 未在运行。请先执行: my-agent gateway start",
}


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
# 函数用途: 在后台探活真正开始时按需加载并调用唯一的 Gateway readiness 有界等待。
def wait_for_gateway_readiness(
    paths: Any,
    *,
    timeout: float,
    on_observation: Callable[[Any], None] | None = None,
):
    from ...agent.gateway_parts.status_rendering import wait_for_gateway_readiness as _wait

    return _wait(paths, timeout=timeout, on_observation=on_observation)


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
# 函数用途: 后台顺序等待真就绪、准备会话，把完整启动结果按结构化 reason 排队送回界面线程。
def _run_gateway_preflight(config: TuiGatewayPreflight) -> None:
    alive = False
    error_code = ""
    try:
        outcome = wait_for_gateway_readiness(
            config.paths,
            timeout=max(0.0, float(config.timeout_seconds or 0.0)),
            on_observation=_gateway_starting_observer(config),
        )
        if config.stop_event.is_set():
            return
        if outcome.ready:
            # LLM: 只有 ready 才允许进入历史准备；starting/timeout/failed 一律不碰会话数据。
            alive = True
            if config.prepare_session is not None:
                error_code = config.prepare_session()
                alive = not bool(error_code)
        else:
            # 结构化分型直接成为错误码：超时、进程消退、服务失败、从未启动四者可区分。
            error_code = outcome.reason or "GATEWAY_NOT_READY"
    except Exception as exc:  # noqa: BLE001 探活异常必须成为 typed 启动失败而不能杀后台线程
        alive = False
        error_code = type(exc).__name__
    final_error_code = error_code or ("" if alive else "GATEWAY_NOT_READY")
    _dispatch_to_application_loop(
        config,
        lambda: _finish_gateway_preflight(
            config,
            alive=bool(alive),
            error_code=final_error_code,
        ),
    )


# LLM: 观测回调只把 starting 这一结构化事实投影成可见提示，并在一秒内去重；它不改状态、不做判断，
#   ready/failed 由等待结局负责收口。
# 函数用途: 生成等待期间显示"启动中"的观测回调。
def _gateway_starting_observer(config: TuiGatewayPreflight) -> Callable[[Any], None]:
    started_at = time.monotonic()
    last_notice_at = 0.0

    def observe(readiness: Any) -> None:
        nonlocal last_notice_at
        if str(getattr(readiness, "state", "")) != "starting":
            return
        now = time.monotonic()
        if now - last_notice_at < _GATEWAY_STARTING_NOTICE_REFRESH_SECONDS:
            return
        last_notice_at = now
        elapsed = max(0.0, now - started_at)
        text = f"{_GATEWAY_STARTING_NOTICE_TEXT} 已等待 {elapsed:.0f}s"
        _dispatch_to_application_loop(
            config,
            lambda: config.runtime.set_notice(
                text,
                duration_seconds=_GATEWAY_STARTING_NOTICE_SECONDS,
                notice_kind=_GATEWAY_STARTING_NOTICE_KIND,
            ),
        )

    return observe


# LLM: runtime 事件必须回到 Application loop 线程提交，loop 已关闭时直接降级执行；两条路径都不写
#   第二份健康事实，只负责把已经确定的结果排队。
# 函数用途: 把回调投递到 Application 事件循环线程。
def _dispatch_to_application_loop(config: TuiGatewayPreflight, callback: Callable[[], None]) -> None:
    loop = getattr(config.application, "loop", None)
    if loop is not None and not bool(getattr(loop, "is_closed", lambda: False)()):
        loop.call_soon_threadsafe(callback)
        return
    callback()


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
    config.runtime.clear_notice(expected_kind=_GATEWAY_STARTING_NOTICE_KIND)
    if alive:
        config.runtime.resolve_connection_check(ok=True)
        config.on_ready()
        config.application.invalidate()
        return
    config.runtime.resolve_connection_check(ok=False, error_code=error_code)
    _publish_gateway_failure(config, error_code)
    config.stop_event.set()
    config.application.invalidate()
    _exit_failed_application(config.application)


# LLM: 失败说明来自结构化 error_code 映射表，不是解析异常正文；只有能解释启动失败的码才写一行，
#   历史恢复类失败保留原有的连接块文案，避免多写一条无信息量的系统消息。
# 函数用途: 在启动失败时把 typed 原因显示为一行系统消息。
def _publish_gateway_failure(config: TuiGatewayPreflight, error_code: str) -> None:
    notice = _GATEWAY_FAILURE_NOTICES.get(str(error_code or ""))
    if notice:
        config.runtime.write_console(notice)


# LLM: 迟到失败不能覆盖用户已经退出的 Application future；只有当前 run 尚活跃时设置错误返回码。
# 函数用途: 在线程安全回调中以退出码 2 结束启动失败的 TUI。
def _exit_failed_application(application: Any) -> None:
    future = getattr(application, "future", None)
    if future is None or future.done():
        return
    application.exit(result=2)


__all__ = ["TuiGatewayPreflight", "start_tui_gateway_preflight"]
