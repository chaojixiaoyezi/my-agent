# LLM: 这里只保存进程内请求资源事实；不保存用户正文、凭据或任务状态，不代替调度/权限账。
# 模块用途: 让后台调用看到同端点前台占用，并把单次后台调用预算传到 HTTP 层。
from __future__ import annotations

import contextvars
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

_LOCK = threading.Lock()
_FOREGROUND: dict[tuple[str, str, int | None], int] = {}
_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "provider_request_deadline", default=None,
)


# LLM: 资源身份取真实后端的 HTTP origin；不按模型名、用户或 localhost 字样猜测服务器。
# 函数用途: 为同 Gateway 内共用模型端点的请求生成无凭据资源键；代理别名不自动合并。
def _endpoint_key(backend: object) -> tuple[str, str, int | None] | None:
    try:
        parts = urlsplit(str(getattr(backend, "api_base", "") or ""))
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return None
        return parts.scheme, parts.hostname.lower(), parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return None


# LLM: 占用覆盖完整 agent.run（包括工具间隙/Compact），finally 清理；不串行化任何前台任务。
# 函数用途: 登记正在使用某模型端点的主/子代理工作片，供低优先级维护延后启动。
@contextmanager
def foreground_model_scope(backend: object) -> Iterator[None]:
    key = _endpoint_key(backend)
    if key is not None:
        with _LOCK:
            _FOREGROUND[key] = _FOREGROUND.get(key, 0) + 1
    try:
        yield
    finally:
        if key is not None:
            with _LOCK:
                remaining = _FOREGROUND[key] - 1
                if remaining:
                    _FOREGROUND[key] = remaining
                else:
                    _FOREGROUND.pop(key, None)


# LLM: 只读取进程内活动事实，不扫描 owner 历史；已开始的后台请求不在此处强杀。
# 函数用途: 判断此后端的服务器是否正被前台工作片使用。
def foreground_model_active(backend: object) -> bool:
    key = _endpoint_key(backend)
    with _LOCK:
        return key is not None and _FOREGROUND.get(key, 0) > 0


# LLM: 预算是 request-local 的绝对 deadline；嵌套预算只能缩短，不修改共享 backend 配置。
# 函数用途: 为一次有外层预算的后台调用设置 HTTP 可用时长，退出后恢复原预算。
@contextmanager
def provider_request_budget(seconds: float) -> Iterator[None]:
    deadline = time.monotonic() + max(0.0, float(seconds))
    previous = _DEADLINE.get()
    token = _DEADLINE.set(min(previous, deadline) if previous is not None else deadline)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


# LLM: 无预算时原样保留默认值；有预算时使用剩余墙钟时长，避免固定 240s 抢先截断 720s 调用。
# 函数用途: 把调用方授权的剩余等待时长传入单次 HTTP 信封。
def provider_request_timeout(default: float) -> float:
    deadline = _DEADLINE.get()
    if deadline is None:
        return default
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("模型调用预算已耗尽")
    return remaining
