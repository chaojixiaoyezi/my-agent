# LLM: 薄客户端只消费有界消息并写原审批桥，不初始化 Agent/执行器；路径来自当前 TUI 连接，HTTP 正文不能提供路径。
# 模块用途: 让长插件命令持续显示审批，同时在断连后保留原编号供查询。

from __future__ import annotations

import json
import threading
import urllib.request

from ...agent.contracts.tool_approval import ToolApprovalDecision, ToolApprovalRequest
from ...agent.gateway_parts.command_stream_protocol import (
    COMMAND_STREAM_MAX_BYTES,
    COMMAND_STREAM_MEDIA_TYPE,
    CommandStreamFrame,
    command_approval_path,
    command_stream_owner,
)
from ...agent.gateway_parts.permission_bridge import write_gateway_permission_decision
from ...agent.tooling.cancellation import CancellationToken
from ..chat_client_context import _gateway_headers, _with_client_identity
from .command_interaction import CommandInteraction


# LLM: 每个命令至多一个当前审批线程，网络读流不因用户思考停止；结束只取消该线程，批准不能越过运输寿命。
# 类用途: 在持续读取心跳时独立等待 TUI 原面板，并将决定原子写回原地址。
class _PermissionConsumer:
    # LLM: 连接路径在提交前冻结，规范 owner 只接受本连接首帧；无跨命令缓存，也不持有业务执行状态。
    # 函数用途: 保存本次审批等待的界面引用、原服务路径和结束信号。
    def __init__(self, interaction: CommandInteraction) -> None:
        self.interaction = interaction
        self.chunk_path = None
        self.finished = threading.Event()
        self.token = CancellationToken(_external_check=lambda: self.finished.is_set()
                                       or interaction.cancellation_token.cancelled)
        self.worker: threading.Thread | None = None
        self.error: Exception | None = None

    # LLM: 同一宿主命令不能用第二条并发审批替换原请求；请求必须保留原 command 编号，具体授权仍由服务器完整核验。
    # 函数用途: 启动一次独立的面板等待，让网络线程继续接收心跳和结果。
    def open(self, value: dict) -> None:
        request = ToolApprovalRequest.from_mapping(value)
        if self.chunk_path is None or request.request_id != self.interaction.request_id or self.worker is not None:
            raise ValueError("命令审批身份或次序无效")
        self.worker = threading.Thread(target=self._wait, args=(request,), name="host-command-permission", daemon=True)
        self.worker.start()

    # LLM: 回调在工作线程执行；原取消位在回调后再次检查，失联时不补写批准，不将异常当作批准。
    # 函数用途: 等待用户选择并通过原文件桥回送完整审批绑定。
    def _wait(self, request: ToolApprovalRequest) -> None:
        try:
            raw = self.interaction.request_permission(request.to_dict(), cancellation_token=self.token)
            decision = raw if isinstance(raw, ToolApprovalDecision) else ToolApprovalDecision.from_mapping(raw)
            if not self.token.cancelled:
                write_gateway_permission_decision(self.chunk_path, request, decision)
        except Exception as exc:  # noqa: BLE001 主读流观察错误后关闭连接，不重试或伪造决定
            self.error = exc

    # LLM: 只关闭本次 UI 等待；消费者须观察原令牌，join 有界，不在输入线程等待。
    # 函数用途: 在结果返回、错误或取消时释放本命令的审批线程。
    def close(self) -> None:
        self.finished.set()
        if self.worker is not None:
            self.worker.join(1.0)


# LLM: 地址固定原 loopback Gateway，身份沿原 header/body builder；传输失败由外层保留原编号，不重放命令。
# 函数用途: 持续读取单次命令消息，返回原最终回执，期间把审批交给当前 TUI。
def post_plugin_command_stream(port: int, owner, payload: dict, interaction: CommandInteraction) -> dict:
    if interaction.gateway_paths is None:
        raise ValueError("交互命令缺少本地 Gateway 连接路径")
    consumer = _PermissionConsumer(interaction)
    body = _with_client_identity(owner, {**payload, "interactive": True})
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/client/plugins", data=json.dumps(body).encode(),
        headers=_gateway_headers(owner, json_body=True, conversation_id=payload["conversation_id"]), method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10.0) as response:
            if response.headers.get_content_type() != COMMAND_STREAM_MEDIA_TYPE:
                raise ValueError("宿主未提供命令消息流")
            return _read_command_frames(response, interaction.request_id, consumer)
    finally:
        consumer.close()


# LLM: 首帧规范 owner 与本地 Gateway 根共同固定地址；有界读取拒绝异号、重复连接及坏帧，只有 result 提供结果。
# 函数用途: 核对连接后持续读取原审批和回执，服务端 owner 映射不依赖客户端猜测。
def _read_command_frames(response, request_id: str, consumer: _PermissionConsumer) -> dict:
    first = CommandStreamFrame.decode(response.readline(COMMAND_STREAM_MAX_BYTES + 1), request_id=request_id)
    if first.kind != "connected":
        raise ValueError("命令流缺少连接身份")
    consumer.chunk_path = command_approval_path(consumer.interaction.gateway_paths, command_stream_owner(first.payload), request_id)
    while True:
        if consumer.token.cancelled or consumer.error is not None:
            raise OSError("命令交互已关闭")
        raw = response.readline(COMMAND_STREAM_MAX_BYTES + 1)
        frame = CommandStreamFrame.decode(raw, request_id=request_id)
        if frame.kind == "connected":
            raise ValueError("命令连接身份不能更换")
        if frame.kind == "permission_requested":
            consumer.open(frame.payload["permission"])
        elif frame.kind == "result":
            return frame.payload
