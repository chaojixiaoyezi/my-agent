# LLM: 本模块只实现 HTTP 信封的显式期限和读取上限；不得生成请求、重试、启动 worker 或改变普通 SSE 合同。
# 模块用途: 供唯一 Gateway 传输入口校验严格请求、有限读取正文，并取得同一个标准库 socket。
from __future__ import annotations

import http.client
import json
import math
import socket
import time
import urllib.error
from typing import TYPE_CHECKING, Any

from ..common.strict_json import load_strict_json
from .errors import ProviderResponseError, ProviderTimeoutError

if TYPE_CHECKING:
    from .gateway_helpers import GatewayRequest

_STRICT_JSON_MAX_DEPTH = 64


# LLM: 仅校验新增显式限制，不重解释既有普通模型配置；发网前统一拒绝布尔、非有限数、溢出数和非法计数。
# 带 send_permit 的请求必须零重试、禁止重定向并有绝对期限，许可须可调用 admit；无许可请求不受影响。
# 函数用途: 检查严格 HTTP 信封，避免无效 deadline、重试值或放宽的许可请求导致意外发送。
def validate_request_limits(request: GatewayRequest) -> None:
    deadline = request.deadline
    if deadline is not None:
        try:
            valid = not isinstance(deadline, bool) and isinstance(deadline, (int, float)) and math.isfinite(deadline)
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("deadline 必须是有限单调时钟数值")
    for name, minimum in (("max_retries", 0), ("max_response_bytes", 1)):
        value = getattr(request, name)
        if value is not None and (type(value) is not int or value < minimum):
            raise ValueError(f"{name} 必须是大于等于 {minimum} 的整数")
    permit = getattr(request, "send_permit", None)
    if permit is not None and (not callable(getattr(permit, "admit", None)) or request.max_retries != 0
                               or request.allow_redirects is not False or deadline is None):
        raise ValueError("带发送许可的请求必须零重试、禁止重定向并有绝对期限")


# LLM: 用户取消优先于局部到期；返回剩余绝对期限，绝不重新授予等待时长，也不声称能强停 DNS/CPU。
# 函数用途: 在网络和解析安全点检查取消与期限，超时沿既有 wall_clock 错误合同返回。
def remaining_deadline_seconds(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    from ..concurrency.interrupt import is_interrupted

    if is_interrupted():
        raise InterruptedError("模型接口请求已被用户停止")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProviderTimeoutError("模型接口请求总期限已耗尽", stage="wall_clock")
    return remaining


# LLM: 仅访问 CPython urllib 的已知响应及 HTTPError 外壳，不探测任意对象；guard 和有限读取共用这个 socket 事实源。
# 函数用途: 取得成功或错误响应实际持有的 socket，供设置读取期限和中断关闭。
def stdlib_response_socket(response: Any) -> socket.socket | None:
    if isinstance(response, urllib.error.HTTPError):
        response = response.fp
    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", None)
    transport = getattr(raw, "_sock", None)
    return transport if isinstance(transport, socket.socket) else None


# LLM: 普通请求保持 read() 原语义；严格请求分块收取至多上限加一字节，并按剩余期限更新真实 socket，不启动线程。
# 函数用途: 读取完整 HTTP 正文，在超量、取消或到期时拒绝返回半份响应；调用方负责 guard 和 watchdog 的生命周期。
def read_response_body(response: Any, request: GatewayRequest) -> bytes:
    if request.deadline is None and request.max_response_bytes is None:
        return response.read()
    body = bytearray()
    read = getattr(response, "read1", None)
    if not callable(read):
        read = response.read
    while True:
        remaining = remaining_deadline_seconds(request.deadline)
        transport = stdlib_response_socket(response)
        if remaining is not None and transport is not None:
            transport.settimeout(min(remaining, float(request.timeout)))
        size = 65536
        if request.max_response_bytes is not None:
            size = min(size, request.max_response_bytes + 1 - len(body))
        chunk = read(size)
        remaining_deadline_seconds(request.deadline)
        if not chunk:
            unread = getattr(response, "length", None)
            if isinstance(unread, int) and unread > 0:
                raise http.client.IncompleteRead(bytes(body), unread)
            return bytes(body)
        body.extend(chunk)
        if request.max_response_bytes is not None and len(body) > request.max_response_bytes:
            raise ProviderResponseError(
                "模型接口响应超过本次字节上限", error_code="PROVIDER_RESPONSE_TOO_LARGE",
            )


# LLM: 显式限制请求先检查嵌套资源，再复用原 strict_json；成功或失败都复核同一 deadline，领域 schema 留给后端。
# 函数用途: 在总期限内解码完整响应，普通模型保留既有宽松 JSON 行为，不重复实现解析器。
def decode_response_json(raw: bytes, request: GatewayRequest) -> Any:
    remaining_deadline_seconds(request.deadline)
    try:
        if any(value is not None for value in (request.deadline, request.max_retries, request.max_response_bytes)):
            _check_json_nesting(raw, request.deadline)
            return load_strict_json(raw)
        return json.loads(raw.decode("utf-8", "replace"))
    finally:
        remaining_deadline_seconds(request.deadline)


# LLM: 扫描只限制严格传输的容器嵌套，不代替唯一 JSON 解析器；忽略字符串和转义，防止提高 Python 递归限后耗尽 C 栈。
# 函数用途: 用字符串/转义两个状态位拒绝超过 64 层的响应，扫描期间仍检查当前请求的取消和期限。
def _check_json_nesting(raw: bytes, deadline: float | None) -> None:
    depth = 0
    in_string = False
    escaped = False
    for offset, value in enumerate(raw):
        if offset % 65536 == 0:
            remaining_deadline_seconds(deadline)
        if in_string:
            in_string = escaped or value != 34
            escaped = not escaped and value == 92
            continue
        if value == 34:
            in_string = True
        elif value in (91, 123):
            depth += 1
        elif value in (93, 125):
            depth = max(0, depth - 1)
        if depth > _STRICT_JSON_MAX_DEPTH:
            raise ValueError("JSON 嵌套超过严格响应的 64 层上限")
