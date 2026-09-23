# LLM: MCP 单连接协议状态只属于创建它的 transport；禁止跨重连复用响应、通知或关闭事件，联测 MCP 客户端及注册层。
# 模块用途: 保存一条连接的在途响应和有界诊断，统一协议错误、取消及排队期限。
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from ..common.cancellation import (
    ToolCancelled,
    cancellation_requested,
    register_cancellation_callback,
)
from ..common.log_redaction import redact_sensitive_text


# LLM: 错误码是调用方映射依据；异常正文必须先脱敏，不能暴露远端回包中的凭证。
# 类用途: 将连接、超时和协议错误作为结构化失败传回工具执行链。
class MCPError(Exception):
    # LLM: effect_outcome 仅由已观察发送边界的宿主设置，不能从错误码/正文推断；未知与未发送必须分开。
    # 函数用途: 创建脱敏 MCP 错误，携带可供原操作账使用的发送事实。
    def __init__(self, message: str, *, code: str = "MCP_ERROR", effect_outcome: str = ""):
        super().__init__(redact_sensitive_text(message, redacted_marker="[REDACTED]", redact_assignment_labels=True))
        self.code = code
        self.effect_outcome = effect_outcome


# LLM: 仅在发送 writer 创建前调用；原回调只核对同次权限，异常结论只描述尚未发送，原账本仍决定是否能收口。
# 函数用途: 复查本次执行权限，保留取消或权威缺失的原因，并向原操作链传递未发送事实。
def require_mcp_execution_authority(check: Callable[[], None] | None) -> None:
    if check is None:
        return
    try:
        check()
    except MCPError:
        raise
    except Exception as exc:
        code = "MCP_CANCELLED" if isinstance(exc, ToolCancelled) else "MCP_EXECUTION_AUTHORITY_UNAVAILABLE"
        raise MCPError("本次 MCP 执行权限不可用，未发送请求", code=code, effect_outcome="not_started") from exc


# LLM: 锁前和锁后均核对期限、取消与原连接关闭；等待不能跨到下一条连接。
# 函数用途: 有期限地排队，连接撤销时及时结束等待，并保证已取得的锁释放。
@contextmanager
def bounded_mcp_lock(lock, deadline: float, *, closed: threading.Event, allow_cancelled: bool = False):
    while True:
        require_mcp_wait(deadline, closed, allow_cancelled=allow_cancelled)
        if lock.acquire(timeout=max(0, min(deadline - time.monotonic(), 0.05))):
            break
    try:
        require_mcp_wait(deadline, closed, allow_cancelled=allow_cancelled)
        yield
    finally:
        lock.release()


# LLM: 检查只读取当前调用取消与固定连接事件，不能重连或推断远端副作用已撤销。
# 函数用途: 统一排队和实际发送前的关闭、取消、期限检查。
def require_mcp_wait(deadline: float, closed: threading.Event, *, allow_cancelled: bool = False) -> None:
    if closed.is_set():
        raise MCPError("MCP 连接已关闭", code="MCP_CONNECTION_CLOSED")
    if cancellation_requested() and not allow_cancelled:
        raise MCPError("MCP 等待已取消", code="MCP_CANCELLED")
    if time.monotonic() >= deadline:
        raise MCPError("MCP 等待超时", code="MCP_TIMEOUT")


# LLM: 每个实例只服务一个固定进程；迟到响应不积压，目录变更只修改本连接的代次。
# 类用途: 为读线程与等待请求提供独立响应箱，避免旧线程污染重连后的新连接。
class MCPInbox:
    # LLM: 创建纯内存状态；name 仅用于展示，不能当进程或插件身份。
    # 函数用途: 初始化当前连接的响应队列、关闭事件和目录变更计数。
    def __init__(self, name: str):
        self.name = name
        self.condition = threading.Condition()
        self.closed = threading.Event()
        self.responses: dict[int, dict[str, Any]] = {}
        self.pending: set[int] = set()
        self.tools_generation = 0
        self.tools_changed = False
        self.stderr = MCPStderrTail(name)

    # LLM: 关闭只唤醒本实例，不清空已收到的真实结果，也不证明远端进程退出。
    # 函数用途: 让当前连接的等待者立即得知不能再等待新响应。
    def close(self) -> None:
        with self.condition:
            self.closed.set()
            self.condition.notify_all()

    # LLM: 方法消息不能冒充响应；仅接受精确在途整数 ID，返回的 server 请求回包仍发送到原连接。
    # 函数用途: 消费通知或响应；对于服务端请求，返回应回复的 JSON-RPC 消息。
    def dispatch(self, message: object) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return None
        msg_id, method = message.get("id"), message.get("method")
        with self.condition:
            if self.closed.is_set():
                return None
            if isinstance(method, str):
                if msg_id is not None:
                    response = ({"result": {}} if method == "ping" else
                                {"error": {"code": -32601, "message": "Client method not supported"}})
                    return {"jsonrpc": "2.0", "id": msg_id, **response}
                if method == "notifications/tools/list_changed":
                    self.tools_generation += 1
                    self.tools_changed = True
            elif type(msg_id) is int and ("result" in message) != ("error" in message) and msg_id in self.pending:
                self.responses.setdefault(msg_id, message)
                self.condition.notify_all()
        return None

    # LLM: 已有响应是已知事实；关闭只终止尚未取得结果的等待，超时不证明远端未执行。
    # 函数用途: 在总期限内等原请求的响应，取消或断连时返回明确错误。
    def await_response(self, req_id: int, method: str, deadline: float) -> Any:
        with register_cancellation_callback(self.wake):
            with self.condition:
                while True:
                    if cancellation_requested():
                        raise MCPError("MCP 调用已取消", code="MCP_CANCELLED")
                    if req_id in self.responses:
                        return unwrap_jsonrpc(self.name, self.responses.pop(req_id), method)
                    require_mcp_wait(deadline, self.closed)
                    self.condition.wait(timeout=min(max(0, deadline - time.monotonic()), 0.5))

    # LLM: 取消回调只唤醒当前响应箱，不发请求、不结束其他连接。
    # 函数用途: 使绑定取消 token 的请求及时重新检查取消状态。
    def wake(self) -> None:
        with self.condition:
            self.condition.notify_all()


# LLM: result/error 判别已在消息入口验证；这里保留远端协议错误而不是返回空成功。
# 函数用途: 取出响应结果，或把远端 JSON-RPC 错误转换为已脱敏异常。
def unwrap_jsonrpc(server_name: str, payload: dict[str, Any], method: str) -> Any:
    err = payload.get("error")
    if err is None:
        return payload.get("result")
    detail = f"{err.get('message') or '未知 JSON-RPC 错误'}（code={err.get('code')}）" if isinstance(err, dict) else str(err)
    raise MCPError(f"MCP server '{server_name}' {method} 返回错误：{detail}", code="MCP_PROTOCOL_ERROR")


# LLM: 诊断归属于单条连接，段长与段数均有上限；不得把旧连接 stderr 携带到新连接。
# 类用途: 持续排空错误管道，保留少量脱敏末尾输出。
class MCPStderrTail:
    # LLM: 创建只用于当前进程的有界诊断缓冲，不读取任何全局日志。
    # 函数用途: 设置错误输出的保留段数。
    def __init__(self, server_name: str, keep: int = 20):
        self._server_name = server_name
        self._keep = keep
        self._lines: list[str] = []

    # LLM: 没有换行的失控输出也要分块；只能保留脱敏文本，不能阻塞 server 写 stderr。
    # 函数用途: 在线程内读完当前错误管道并裁剪末尾诊断。
    def drain(self, stream: Any) -> None:
        if stream is None:
            return
        try:
            for raw in iter(lambda: stream.readline(4096), ""):
                line = raw.rstrip("\n")
                if line:
                    self._lines.append(redact_sensitive_text(line, redacted_marker="[REDACTED]", redact_assignment_labels=True))
                    del self._lines[:-self._keep]
        except (OSError, ValueError):
            logging.getLogger(__name__).debug("MCP server '%s' 错误管道已关闭", self._server_name)
        finally:
            stream.close()
