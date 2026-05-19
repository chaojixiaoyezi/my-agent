from __future__ import annotations

# LLM: MessageRuntimeTool adapts internal messages to the model-callable tool contract.
# 模块用途: 提供 message_runtime 工具，支持发送、读取、确认、进度和用户输入请求。
import json
from pathlib import Path
from typing import Any

from ..tools import BaseTool, ToolExecutionResult, ToolSpec
from .models import MessageTarget
from .store import MessageStore
from .tool import MessageTool


 # LLM: MessageRuntimeTool keeps message actions behind a stable tool schema.
 # 类用途: 把模型传入的 action 参数转换为内部 MessageTool 调用。
class MessageRuntimeTool(BaseTool):
    # LLM: MessageRuntimeTool.__init__ wires a local MessageStore to tool metadata.
    # 函数用途: 初始化 message_runtime 工具和工具规格。
    def __init__(self, messages_root: str | Path):
        self.messages_root = Path(messages_root)
        self.message_tool = MessageTool(MessageStore(self.messages_root))
        self.spec = build_message_runtime_spec()

    # LLM: MessageRuntimeTool.execute dispatches safe message actions by name.
    # 函数用途: 根据 action 执行发送、读取、确认、进度或用户输入请求。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            action = str(params.get("action", "")).strip()
            if action == "send":
                return self._send(params)
            if action == "read_inbox":
                return self._read_inbox(params)
            if action == "ack":
                return self._ack(params)
            if action == "progress":
                return self._progress(params)
            if action == "need_user_input":
                return self._need_user_input(params)
            return _error(f"unknown message action: {action}", code="MESSAGE_ACTION_UNKNOWN")
        except ValueError as exc:
            return _error(str(exc), code="MESSAGE_TARGET_INVALID")

    # LLM: MessageRuntimeTool._send handles generic internal message delivery.
    # 函数用途: 执行 send 动作并返回结构化消息结果。
    def _send(self, params: dict[str, Any]) -> ToolExecutionResult:
        message = self.message_tool.send_message(
            sender=_parse_target(params.get("sender")),
            target=_parse_target(params.get("target")),
            content=str(params.get("content") or ""),
            task_id=_optional_string(params.get("task_id")),
            message_type=str(params.get("message_type") or "text"),
            idempotency_key=_optional_string(params.get("idempotency_key")),
            metadata=_dict_param(params.get("metadata")),
        )
        return _ok({"message": message.to_dict()})

    # LLM: MessageRuntimeTool._read_inbox returns inbox rows for one target.
    # 函数用途: 执行 read_inbox 动作并返回消息列表。
    def _read_inbox(self, params: dict[str, Any]) -> ToolExecutionResult:
        target = _parse_target(params.get("target"))
        unread_only = bool(params.get("unread_only", True))
        messages = [message.to_dict() for message in self.message_tool.read_inbox(target, unread_only=unread_only)]
        return _ok({"messages": messages})

    # LLM: MessageRuntimeTool._ack marks a target delivery as read.
    # 函数用途: 执行 ack 动作并返回确认结果。
    def _ack(self, params: dict[str, Any]) -> ToolExecutionResult:
        target = _parse_target(params.get("target"))
        message_id = str(params.get("message_id") or "")
        if not message_id:
            return _error("message_id is required", code="MESSAGE_ID_REQUIRED")
        return _ok({"acked": self.message_tool.ack_message(target, message_id)})

    # LLM: MessageRuntimeTool._progress sends task progress to a structured target.
    # 函数用途: 执行 progress 动作，生成任务进度消息。
    def _progress(self, params: dict[str, Any]) -> ToolExecutionResult:
        message = self.message_tool.broadcast_progress(
            task_id=str(params.get("task_id") or ""),
            route_target=_parse_target(params.get("target")),
            content=str(params.get("content") or ""),
        )
        return _ok({"message": message.to_dict()})

    # LLM: MessageRuntimeTool._need_user_input sends a blocker question to the frontstage session.
    # 函数用途: 执行 need_user_input 动作，要求前台主代理向用户确认。
    def _need_user_input(self, params: dict[str, Any]) -> ToolExecutionResult:
        message = self.message_tool.send_need_user_input(
            task_id=str(params.get("task_id") or ""),
            session_id=str(params.get("session_id") or ""),
            prompt=str(params.get("content") or params.get("prompt") or ""),
        )
        return _ok({"message": message.to_dict()})


 # LLM: build_message_runtime_spec documents the model-callable message runtime tool.
 # 函数用途: 构建 message_runtime 的工具规格、参数说明和示例。
def build_message_runtime_spec() -> ToolSpec:
    return ToolSpec(
        name="message_runtime",
        category="runtime",
        description="发送、读取、确认内部 session/user/task 消息，并承载后台任务进度或用户确认请求。",
        use_cases=[
            "后台 TaskAgent 需要向前台 session 汇报进度或完成结果",
            "前台 MainAgent 需要读取当前 session/user 的未读内部消息",
        ],
        avoid_when=["只需要子代理层级内纠偏时继续使用 subagent_message"],
        keywords=["message", "消息", "inbox", "session", "task", "progress", "need_user_input"],
        parameters={
            "action": "send/read_inbox/ack/progress/need_user_input",
            "sender": "发送方目标，例如 session:sess-a、task:task-1、system:runtime",
            "target": "接收方目标，例如 session:sess-b、user:user-1、task:task-1",
            "content": "消息正文；超长正文会外部化保存",
            "task_id": "可选，关联任务 id",
            "message_id": "ack 动作需要",
        },
        examples=[
            '{"tool":"message_runtime","action":"send","sender":"task:task-1","target":"session:sess-1","content":"阶段 1 完成"}',
            '{"tool":"message_runtime","action":"read_inbox","target":"session:sess-1"}',
        ],
    )


 # LLM: _parse_target validates structured target strings before routing.
 # 函数用途: 解析 kind:identifier 格式的消息目标，格式错误时抛出 ValueError。
def _parse_target(value: object) -> MessageTarget:
    text = str(value or "")
    if ":" not in text:
        raise ValueError(f"invalid message target: {text}")
    return MessageTarget.parse(text)


 # LLM: _optional_string normalizes optional payload strings.
 # 函数用途: 把可选参数转换成非空字符串或 None。
def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


 # LLM: _dict_param accepts metadata dictionaries without trusting arbitrary objects.
 # 函数用途: 安全提取 dict 类型 metadata。
def _dict_param(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


 # LLM: _ok renders successful tool payloads as JSON.
 # 函数用途: 构造 message_runtime 成功工具结果。
def _ok(payload: dict[str, Any]) -> ToolExecutionResult:
    return ToolExecutionResult(tool="message_runtime", ok=True, output=json.dumps(payload, ensure_ascii=False, sort_keys=True))


 # LLM: _error renders stable tool failures with explicit error codes.
 # 函数用途: 构造 message_runtime 失败工具结果。
def _error(message: str, *, code: str) -> ToolExecutionResult:
    return ToolExecutionResult(tool="message_runtime", ok=False, output=message, error_code=code)
