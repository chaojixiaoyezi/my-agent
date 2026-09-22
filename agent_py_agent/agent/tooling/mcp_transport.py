# LLM: transport 固定原进程/托管资源，发送复查原代与本次权限；普通树和托管 Store 清理各保留原回执，联测排队与撤销。
# 模块用途: 管理一条 MCP 连接，旧线程不能触及新实例，单次未发送的拒绝不关闭共享连接。
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..plugin_installation import PluginInstallationError
from .mcp_protocol import (
    MCPError,
    MCPInbox,
    bounded_mcp_lock,
    require_mcp_execution_authority,
    require_mcp_wait,
)
from .process_registry import (
    ProcessTerminationReceipt,
    capture_process_birth_token,
    terminate_process_tree,
)
from .process_session_cleanup import ProcessSessionCleanup, ProcessSessionCleanupError

if TYPE_CHECKING:
    from .mcp_managed_process import ManagedMCPProcess

MCPCleanupReceipt = ProcessTerminationReceipt | ProcessSessionCleanup


# LLM: 托管引用保留原资源账和激活，不据进程名猜归属；清理不能重新查询 client.current 或只杀托管 host。
# 类用途: 固定这条连接的进程出生身份及可选的原托管资源。
@dataclass(frozen=True)
class MCPProcessBinding:
    process: subprocess.Popen
    birth_token: str
    managed: ManagedMCPProcess | None = None


# LLM: 请求锁、写锁、响应箱均是连接私有；撤销先关闭准入，再在独立清理锁下回收固定进程。
# 类用途: 承载一次启动的 MCP 连接，保留真实清理回执并隔离旧请求与重连。
class MCPTransport:
    # LLM: 托管连接只能绑定该原句柄和已登记出生身份；非托管 MCP 保持原路径，不能据可选字段猜测资源归属。
    # 函数用途: 为一次启动创建收发与清理句柄，插件清理保留完整原资源回执。
    def __init__(self, process: subprocess.Popen, name: str, *, max_line_chars: int, connect_timeout: float,
                 managed: ManagedMCPProcess | None = None):
        if managed is not None and managed.hosted.process is not process:
            raise ValueError("MCP 托管进程绑定冲突")
        birth = managed.hosted.record["pid_birth_token"] if managed is not None else capture_process_birth_token(process.pid)
        self.binding = MCPProcessBinding(process, birth, managed)
        self.inbox = MCPInbox(name)
        self.max_line_chars = max_line_chars
        self.connect_timeout = connect_timeout
        self.ready = False
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.request_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.admission_lock = threading.Lock()
        self.cleanup_lock = threading.Lock()
        self._next_id = 1
        self._receipt: MCPCleanupReceipt | None = None
        self._cleanup_error: ProcessSessionCleanupError | None = None
        self.reader = threading.Thread(target=read_messages, args=(self,), name=f"mcp-reader-{name}", daemon=True)
        self.stderr_reader = threading.Thread(target=self.inbox.stderr.drain, args=(process.stderr,),
                                              name=f"mcp-stderr-{name}", daemon=True)

    # LLM: 不 poll/wait 提前回收组长；读线程通过 EOF 判别失联，原出生身份保留给进程树清理。
    # 函数用途: 判断握手完成的当前连接是否还能收发消息。
    def is_running(self) -> bool:
        return self.ready and not self.inbox.closed.is_set()

    # LLM: 线程都绑定 self 的原进程和响应箱；不得从客户端读取后来替换的 transport。
    # 函数用途: 启动 stdout 与 stderr 的独立排空线程。
    def start_readers(self) -> None:
        self.reader.start()
        self.stderr_reader.start()

    # LLM: 与发送线程启动共享短锁；先撤销保证后取得准入的排队调用不能写入，已准入的调用仍可能有副作用。
    # 函数用途: 立即拒绝后续收发并唤醒原连接等待者，不等待业务响应。
    def revoke(self) -> None:
        with self.admission_lock:
            self.ready = False
            self.inbox.close()

    # LLM: 托管清理保留原 Store 回执/异常且不回退裸 Popen；普通 MCP 沿原树回执，未确认不能重连。
    # 函数用途: 清理固定连接，重复返回原结果或原提交异常，读线程只关闭自己拥有的流。
    def terminate(self, *, grace_seconds: float = 3.0) -> MCPCleanupReceipt:
        self.revoke()
        with self.cleanup_lock:
            if self._cleanup_error is not None:
                raise self._cleanup_error
            if self._receipt is None:
                try:
                    if self.binding.managed is not None:
                        self._receipt = self.binding.managed.terminate()
                    else:
                        self._receipt = terminate_process_tree(
                            self.binding.process.pid, self.binding.process, grace_seconds=grace_seconds,
                            expected_birth_token=self.binding.birth_token,
                        )
                except ProcessSessionCleanupError as exc:
                    self._cleanup_error = exc
                    close_finished_streams(self)
                    raise
                except Exception as exc:
                    if self.binding.managed is not None:
                        self._cleanup_error = ProcessSessionCleanupError(exc, self.binding.managed.hosted.record, (), False)
                        close_finished_streams(self)
                        raise self._cleanup_error from exc
                    logging.getLogger(__name__).warning("MCP 进程清理异常：%s", type(exc).__name__)
                    self._receipt = ProcessTerminationReceipt("cleanup_error", False, None, 0,
                                                              (self.binding.process.pid,))
            receipt = self._receipt
        close_finished_streams(self)
        return receipt

    # LLM: 本次权限回调不存入共享连接，排队后随请求交给发送；迟到结果不跨连接，总期限覆盖整个调用。
    # 函数用途: 发送带 ID 的请求，保留调用自身的执行检查，退出时移除在途状态。
    def request(self, method: str, params: dict[str, Any], *, timeout: float,
                authority_check: Callable[[], None] | None = None) -> Any:
        deadline = time.monotonic() + max(0.01, timeout)
        with bounded_mcp_lock(self.request_lock, deadline, closed=self.inbox.closed):
            req_id = self._next_id
            self._next_id += 1
            with self.inbox.condition:
                self.inbox.pending.add(req_id)
            sent = False
            try:
                self.send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params},
                          deadline=deadline, authority_check=authority_check)
                sent = True
                return self.inbox.await_response(req_id, method, deadline)
            except MCPError as exc:
                if sent and method != "initialize" and exc.code in {"MCP_CANCELLED", "MCP_TIMEOUT"}:
                    self.cancel_request(req_id, exc.code)
                raise
            finally:
                with self.inbox.condition:
                    self.inbox.pending.discard(req_id)
                    self.inbox.responses.pop(req_id, None)

    # LLM: 取消通知仅是协议请求，不证明远端执行已回滚；永远发到原请求连接。
    # 函数用途: 在短期限内尽力通知服务端取消已发送请求。
    def cancel_request(self, req_id: int, reason: str) -> None:
        try:
            self.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                       "params": {"requestId": req_id, "reason": reason}},
                      deadline=time.monotonic() + 0.2, allow_cancelled=True)
        except MCPError:
            pass

    # LLM: 通知沿同一连接队列，不清空在途表，不允许初始化通知跨重連。
    # 函数用途: 在连接期限内发出无需等待响应的协议通知。
    def notify(self, method: str, params: dict[str, Any]) -> None:
        deadline = time.monotonic() + self.connect_timeout
        with bounded_mcp_lock(self.request_lock, deadline, closed=self.inbox.closed):
            self.send({"jsonrpc": "2.0", "method": method, "params": params}, deadline=deadline)

    # LLM: 排队后在原资源/本连接短锁内检查同一代；尚未发送的拒绝只结束调用，已创建 writer 的失败须清理原连接。
    # 函数用途: 有界发送完整帧，插件停用阻止旧队列，单次排队取消不关闭共享连接。
    def send(self, message: dict[str, Any], *, deadline: float | None = None, allow_cancelled: bool = False,
             authority_check: Callable[[], None] | None = None) -> None:
        until = deadline if deadline is not None else time.monotonic() + self.connect_timeout
        payload = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        if len(payload) > self.max_line_chars:
            raise MCPError("MCP 请求超过单帧大小上限", code="MCP_PROTOCOL_ERROR")
        with bounded_mcp_lock(self.write_lock, until, closed=self.inbox.closed, allow_cancelled=allow_cancelled):
            writer = None
            try:
                with self._send_admission(message, until, allow_cancelled=allow_cancelled):
                    require_mcp_execution_authority(authority_check)
                    require_mcp_wait(until, self.inbox.closed, allow_cancelled=allow_cancelled)
                    stream = self.binding.process.stdin
                    if stream is None:
                        raise OSError("MCP stdin 不可用")
                    writer = MCPFrameWriter(os.dup(stream.fileno()), payload, self.inbox.name)
                    writer.start()
                writer.wait(until, self.inbox.closed, allow_cancelled=allow_cancelled)
            except MCPError as exc:
                exc.effect_outcome = "not_started" if writer is None else "unknown"
                if writer is not None:
                    self.terminate(grace_seconds=0)
                raise
            except (OSError, ValueError) as exc:
                if writer is not None:
                    self.terminate(grace_seconds=0)
                raise MCPError(f"MCP 写入失败：{exc}", code="MCP_CONNECTION_CLOSED",
                               effect_outcome="not_started" if writer is None else "unknown") from exc

    # LLM: 原资源锁先于本地准入锁，排队不阻止 revoke；安装表只读，不能持锁等待服务端。只有协议初始化/发现可用 preparing。
    # 函数用途: 为发送线程启动建立可取消的临界区，检查原进程、激活和本次等待条件。
    @contextmanager
    def _send_admission(self, message: dict, deadline: float, *, allow_cancelled: bool):
        managed = self.binding.managed
        # LLM: 只检查本次期限、取消和原连接关闭；供原目录锁在等待阶段调用，不触发资源清理。
        # 函数用途: 让跨进程锁排队与 MCP 请求共享同一个取消和超时边界。
        def check() -> None:
            require_mcp_wait(deadline, self.inbox.closed, allow_cancelled=allow_cancelled)
        admitted = False
        try:
            with (managed.transaction(check) if managed is not None else nullcontext()) as transaction:
                with self.admission_lock:
                    check()
                    if managed is not None:
                        method = message.get("method")
                        preparation = method in {"initialize", "notifications/initialized", "tools/list"} or method is None
                        managed.require(transaction, allow_preparing=preparation)
                    check()
                    admitted = True
                    yield
        except PluginInstallationError as exc:
            if admitted:
                raise
            raise MCPError("插件原激活不可用，拒绝发送", code="PLUGIN_ACTIVATION_UNAVAILABLE") from exc
        except (OSError, ValueError, RuntimeError) as exc:
            if admitted:
                raise
            raise MCPError("MCP 原资源准入未确认", code="MCP_ADMISSION_UNAVAILABLE") from exc


# LLM: 写线程拥有单独 dup fd，直到自身 finally 关闭；不得在外线程关闭此 fd 引入复用竞态。
# 类用途: 在线程中排空一帧字节，让管道背压仍受到调用方的期限限制。
class MCPFrameWriter:
    # LLM: fd 所有权交给本实例；启动失败也须关闭它，不能泄露到下一次连接。
    # 函数用途: 保存一次发送的完整字节与完成事件。
    def __init__(self, fd: int, payload: bytes, name: str):
        self.fd, self.payload = fd, payload
        self.complete = threading.Event()
        self.error: OSError | None = None
        self.thread = threading.Thread(target=self.write, name=f"mcp-writer-{name}", daemon=True)

    # LLM: 线程成功启动后独占 fd；启动异常仍由此实例回收，不留旁路 writer。
    # 函数用途: 启动当前帧的有界等待写入任务。
    def start(self) -> None:
        try:
            self.thread.start()
        except BaseException:
            os.close(self.fd)
            raise

    # LLM: 仅操作构造时冻结的 fd；所有字节包含在同一帧，错误留给等待方处理。
    # 函数用途: 写出完整字节并关闭本线程专属 fd。
    def write(self) -> None:
        try:
            remaining = memoryview(self.payload)
            while remaining:
                count = os.write(self.fd, remaining)
                if count <= 0:
                    raise OSError("MCP 管道写入无进展")
                remaining = remaining[count:]
        except OSError as exc:
            self.error = exc
        finally:
            os.close(self.fd)
            self.complete.set()

    # LLM: 这里只等待固定 writer；取消/期限不证明远端未收到部分帧，调用方必须废弃此 transport。
    # 函数用途: 检查发送完成、失败、关闭和取消，不无限等管道恢复。
    def wait(self, deadline: float, closed: threading.Event, *, allow_cancelled: bool) -> None:
        while not self.complete.wait(timeout=0.01):
            require_mcp_wait(deadline, closed, allow_cancelled=allow_cancelled)
        if self.error is not None:
            raise self.error


# LLM: reader 仅持本 transport；老进程 EOF 和服务端请求都不能写新连接的响应箱或 stdin。
# 函数用途: 有界按行读取 JSON-RPC，将消息交给当前响应箱。
def read_messages(transport: MCPTransport) -> None:
    stream = transport.binding.process.stdout
    try:
        if stream is None:
            return
        while not transport.inbox.closed.is_set():
            raw = stream.readline(transport.max_line_chars + 1)
            if not raw:
                break
            if discard_oversized_line(raw, stream, transport.max_line_chars):
                continue
            try:
                response = transport.inbox.dispatch(json.loads(raw))
            except (ValueError, TypeError):
                continue
            if response is not None:
                transport.send(response)
    except (MCPError, OSError, ValueError):
        pass
    finally:
        transport.inbox.close()
        if stream is not None:
            stream.close()


# LLM: 长行必须持续排空且有界分段，不能一次读入剩余全部输出。
# 函数用途: 丢弃超过单帧上限的整行，后续合法帧仍能解析。
def discard_oversized_line(raw: str, stream: Any, cap: int) -> bool:
    if raw.endswith("\n") or len(raw) <= cap:
        return False
    while True:
        extra = stream.readline(cap + 1)
        if not extra or extra.endswith("\n"):
            break
    logging.getLogger(__name__).warning("MCP 超长行已丢弃，上限 %d 字符", cap)
    return True


# LLM: stdout 正在 readline 时 close 可阻塞，未确认退出不能强关其他线程持有的缓冲流。
# 函数用途: 关闭当前输入流；读线程已结束时才回收输出流。
def close_finished_streams(transport: MCPTransport) -> None:
    process = transport.binding.process
    streams = [process.stdin]
    for thread, stream in ((transport.reader, process.stdout), (transport.stderr_reader, process.stderr)):
        if thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=0.1)
        if not thread.is_alive():
            streams.append(stream)
    for stream in streams:
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
