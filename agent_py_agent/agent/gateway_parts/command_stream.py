# LLM: HTTP 原请求线程执行命令；辅助线程只发有界心跳，断连置本命令取消位，不创建新执行器或改写持久结果。
# 模块用途: 在同机插件命令执行期间运输原审批和结果，并把客户端消失传给原取消令牌。

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import asdict

from ..tooling.cancellation import CancellationToken
from ..user_space.owner_resolver import OwnerIdentity
from .command_stream_protocol import (
    COMMAND_STREAM_MEDIA_TYPE,
    CommandStreamFrame,
    command_approval_path,
)
from .paths import GatewayPaths
from .stream_approval import StreamApproval

_HEARTBEAT_SECONDS = 1.0
_WRITE_TIMEOUT_SECONDS = 3.0


# LLM: 实例仅拥有一个 HTTP 输出流和易失取消位，写失败必须先标失联；原业务副作用与 UNKNOWN 裁决归执行器。
# 类用途: 串行写出命令消息，让心跳可以打断无人响应的审批等待。
class _CommandStream:
    # LLM: handler 由已鉴权入口提供，编号已校验；构造不执行命令、不启动线程。
    # 函数用途: 保存本连接的输出锁与寿命事件。
    def __init__(self, handler, request_id: str) -> None:
        self.handler, self.request_id = handler, request_id
        self.finished = threading.Event()
        self.disconnected = threading.Event()
        self.lock = threading.Lock()

    # LLM: 同一锁序列化心跳和原事件；I/O 失败只关闭当前命令，不吞成成功回执。
    # 函数用途: 写出一条消息并及时冲刷到客户端。
    def publish(self, kind: str, payload: dict) -> None:
        raw = CommandStreamFrame(self.request_id, kind, payload).encode()
        with self.lock:
            if self.disconnected.is_set():
                raise OSError("命令连接已关闭")
            try:
                self.handler.wfile.write(raw)
                self.handler.wfile.flush()
            except OSError:
                self.disconnected.set()
                raise

    # LLM: 此线程只观察运输寿命，不登记任务、重试命令或清理其它连接；写入受 socket timeout 限制。
    # 函数用途: 在执行或审批没有新输出时持续探测当前客户端连接。
    def heartbeat(self) -> None:
        while not self.finished.wait(_HEARTBEAT_SECONDS):
            try:
                self.publish("heartbeat", {})
            except OSError:
                return


# LLM: execute 在当前 HTTP 线程调用一次；原 StreamApproval 使用完整绑定和空缓存，退出只关闭本连接的辅助线程。
# 函数用途: 建立命令消息流，等待真实审批并运输原执行结果。
def serve_command_stream(handler, paths: GatewayPaths, owner: OwnerIdentity, request_id: str,
                         execute: Callable) -> None:
    chunk_path = command_approval_path(paths, owner, request_id)
    stream = _CommandStream(handler, request_id)
    token = CancellationToken(_external_check=stream.disconnected.is_set)
    handler.connection.settimeout(_WRITE_TIMEOUT_SECONDS)
    handler.close_connection = True
    handler.send_response(200)
    handler.send_header("Content-Type", COMMAND_STREAM_MEDIA_TYPE)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Connection", "close")
    handler.end_headers()

    # LLM: 事件字段直接来自原 StreamApproval；只拆 kind，不改审批编号或 binding。
    # 函数用途: 将原审批事件写入当前命令流。
    def publish_approval(event: dict) -> None:
        stream.publish(event["kind"], {key: value for key, value in event.items() if key != "kind"})

    approval = StreamApproval(chunk_path, publish_approval, lambda: None)

    # LLM: 原执行器传来的取消令牌继续传给文件桥；不启用会话批准缓存或模式旁路。
    # 函数用途: 在原执行区间等待本次用户决定。
    def request_permission(value: dict, *, cancellation_token=None) -> dict:
        return approval.request(value, interactive=True, cancellation_token=cancellation_token)

    heartbeat = threading.Thread(target=stream.heartbeat, name="host-command-heartbeat", daemon=True)
    try:
        stream.publish("connected", {"owner": asdict(owner)})
        heartbeat.start()
        result = execute(request_permission, token)
        stream.publish("result", result)
    except OSError:
        stream.disconnected.set()
    finally:
        stream.finished.set()
        stream.disconnected.set()
        if heartbeat.ident is not None:
            heartbeat.join(_WRITE_TIMEOUT_SECONDS + 0.5)
