# LLM: Model backend module; keep streaming, gateway, and backend protocol shapes stable.
# 模块用途: 封装模型后端协议、流式解析和 gateway 辅助调用。

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass

from .errors import ProviderTimeoutError


# LLM: GatewayRequest 属于模型后端请求的类边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 类用途: 集中保存网关请求字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发模型请求参数、流式解析和错误传播相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class GatewayRequest:
    """bundle for HTTP gateway model requests."""

    api_base: str
    api_key: str
    path: str
    payload: dict[str, Any]
    headers: dict[str, str]
    timeout: int

    # LLM: url 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
    # 函数用途: 处理url相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模型请求参数、流式解析和错误传播上的返回值和副作用边界稳定。
    @property
    def url(self) -> str:
        return self.api_base + self.path


# LLM: post_json 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 函数用途: 发送JSON请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def post_json(
    request: GatewayRequest,
) -> dict[str, Any]:
    _require_api_key(request.api_key)
    req = _urllib_request(request)
    try:
        with urllib.request.urlopen(req, timeout=request.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise _runtime_http_error(exc) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _runtime_network_error(exc, request) from exc


# LLM: post_stream 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 函数用途: 发送流式请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def post_stream(
    request: GatewayRequest,
) -> list[str]:
    return list(_post_stream_lines(request))


# LLM: post_stream_iter 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 函数用途: 发送流式迭代请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def post_stream_iter(
    request: GatewayRequest,
):
    yield from _post_stream_lines(request)


# LLM: _post_stream_lines 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 函数用途: 发送流式lines请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _post_stream_lines(request: GatewayRequest) -> Iterator[str]:
    request.payload["stream"] = True
    _require_api_key(request.api_key)
    req = _urllib_request(request)
    deadline = _stream_deadline(request.timeout)
    try:
        with urllib.request.urlopen(req, timeout=request.timeout) as resp:
            yield from _iter_sse_data_lines(resp, deadline=deadline, timeout=request.timeout, url=request.url)
    except urllib.error.HTTPError as exc:
        raise _runtime_http_error(exc) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _runtime_network_error(exc, request) from exc


# LLM: _urllib_request 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 函数用途: 处理urllib请求相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _urllib_request(request: GatewayRequest) -> urllib.request.Request:
    return urllib.request.Request(
        request.url,
        data=json.dumps(request.payload).encode("utf-8"),
        method="POST",
        headers=request.headers,
    )


# LLM: _require_api_key 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 函数用途: 校验apikey需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _require_api_key(api_key: str) -> None:
    if not api_key:
        raise ValueError("api_key 为空：请在配置文件中填写 API Key。")


# LLM: _runtime_http_error 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 函数用途: 推进运行时HTTPerror的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _runtime_http_error(exc: urllib.error.HTTPError) -> RuntimeError:
    detail = exc.read().decode("utf-8", "replace")
    return RuntimeError(f"HTTP {exc.code}: {detail}")


# LLM: _runtime_network_error 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 函数用途: 推进运行时networkerror的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响模型请求参数、流式解析和错误传播，需保持重试、超时和状态迁移语义。
def _runtime_network_error(exc: BaseException, request: GatewayRequest) -> RuntimeError:
    parsed = urllib.parse.urlparse(request.url)
    host = parsed.netloc or parsed.path.split("/", 1)[0] or request.api_base
    reason = getattr(exc, "reason", None) or str(exc) or exc.__class__.__name__
    if _is_timeout_exception(exc):
        return ProviderTimeoutError(
            "模型接口请求超时: "
            f"host={host} request_timeout={request.timeout}s url={request.url} "
            f"底层错误: {reason}"
        )
    return RuntimeError(
        "网络请求失败: "
        f"无法连接模型接口 {host}（{request.url}）。"
        "请检查 DNS、网络/代理和 api_base 配置；"
        f"底层错误: {reason}"
    )


# LLM: _is_timeout_exception recognizes urllib/socket timeout shapes without broad message matching.
# 函数用途: 把直接 TimeoutError 和 URLError.reason 里的 timeout 都归一成 provider timeout。
def _is_timeout_exception(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, (TimeoutError, socket.timeout))


# LLM: _iter_sse_data_lines 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 函数用途: 处理迭代SSEdatalines相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模型请求参数、流式解析和错误传播上的返回值和副作用边界稳定。
def _stream_deadline(timeout: int) -> float:
    return time.monotonic() + max(1, int(timeout or 0))


# LLM: _iter_sse_data_lines enforces total stream wall-clock timeout, not only socket idle timeout.
# 函数用途: 迭代 SSE 数据行；如果服务端持续发空心跳但没有结束，也会按 request_timeout 总时长退出。
def _iter_sse_data_lines(response, *, deadline: float, timeout: int, url: str) -> Iterator[str]:
    for raw_line in response:
        if time.monotonic() > deadline:
            raise ProviderTimeoutError(
                "模型接口流式响应超时: "
                f"request_timeout={timeout}s url={url}"
            )
        line = raw_line.decode("utf-8").strip()
        if _is_sse_data_line(line):
            yield line[5:].strip()


# LLM: _is_sse_data_line 属于模型后端请求的函数边界；调整时先确认模型请求参数、流式解析和错误传播仍按原契约工作。
# 函数用途: 判断SSEdataline条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _is_sse_data_line(line: str) -> bool:
    return bool(line and line.startswith("data:"))
