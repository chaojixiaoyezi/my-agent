# LLM: 本模块是所有薄客户端提交 Gateway 请求的唯一写入合同；导入时不得加载 worker、Agent、模型、工具或后台调度。
# 模块用途: 规范聊天请求、生成结构化队列载荷并原子入队，供 TUI、CLI、飞书和未来 Web 复用。

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..conversation.channels import (
    LOCAL_AGENT_USER_ID,
    LOCAL_CHAT_CHANNEL,
    LOCAL_CHAT_SOURCE,
)
from .io import gateway_response_path, new_gateway_request_id, write_gateway_request
from .paths import GatewayPaths

if TYPE_CHECKING:
    from ..core import SimpleAgent


# LLM: GatewayAskExecutionOptions is the immutable, transport-neutral execution option set for one
# ordinary ask. Active-turn fallback must persist this snapshot before POST so a later queued turn
# has exactly the same inject/files/save/resume/client-capability semantics as direct submission.
# 类用途: 保存一次普通请求会改变模型执行方式的选项，并在 TUI、HTTP 和队列请求之间无损转换。
@dataclass(frozen=True)
class GatewayAskExecutionOptions:
    inject: tuple[str, ...] = ()
    prompt_files: tuple[str, ...] = ()
    save: bool = True
    include_prompt: bool = False
    resume_context: bool | None = None
    tool_approval: bool = False
    rich_transcript: bool = False

    # LLM: Serialization emits explicit defaults so idempotency digests cover behavior rather than
    # whichever client happened to omit a false/default field.
    # 函数用途: 转成 Gateway `/ask` 可直接传输和持久化的结构化字段。
    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "inject": list(self.inject),
            "prompt_files": list(self.prompt_files),
            "save": self.save,
            "include_prompt": self.include_prompt,
            "client_capabilities": {
                "tool_approval": self.tool_approval,
                "rich_transcript": self.rich_transcript,
            },
        }
        if self.resume_context is not None:
            payload["resume_context"] = self.resume_context
        return payload

    # LLM: Only the public allowlist is accepted. Invalid open-world body values fall back to the
    # same execution defaults used by Gateway workers; arbitrary metadata is never copied through.
    # 函数用途: 从 HTTP/outbox 对象规范化允许改变执行行为的字段。
    @classmethod
    def from_payload(cls, payload: object) -> GatewayAskExecutionOptions:
        raw = payload if isinstance(payload, dict) else {}
        capabilities = raw.get("client_capabilities")
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        raw_resume = raw.get("resume_context")
        resume_context = raw_resume if isinstance(raw_resume, bool) else None
        return cls(
            inject=_string_tuple(raw.get("inject")),
            prompt_files=_string_tuple(raw.get("prompt_files")),
            save=raw.get("save") if isinstance(raw.get("save"), bool) else True,
            include_prompt=(
                raw.get("include_prompt")
                if isinstance(raw.get("include_prompt"), bool)
                else False
            ),
            resume_context=resume_context,
            tool_approval=capabilities.get("tool_approval") is True,
            rich_transcript=capabilities.get("rich_transcript") is True,
        )


# LLM: List normalization is shared by persisted outbox and HTTP bodies; non-list values cannot
# smuggle a string as character-by-character inject entries.
# 函数用途: 将注入或文件列表规范化成不可变字符串元组。
def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


# LLM: GatewayAskParams 只声明客户端真实支持的交互能力；审批与富 transcript 都必须显式 opt-in，服务端不能按 source 或终端形态猜测。
# 类用途: 描述一次 Gateway ask 请求及其会话、持久化和客户端能力。
@dataclass
class GatewayAskParams:
    prompt: str
    inject: list[str] | None = None
    prompt_files: list[str] | None = None
    save: bool = True
    include_prompt: bool = False
    resume_context: bool | None = None
    chat_session_id: str = ""
    channel: str = LOCAL_CHAT_CHANNEL
    channel_user_id: str = LOCAL_AGENT_USER_ID
    canonical_user_id: str = LOCAL_AGENT_USER_ID
    channel_chat_type: str = ""
    channel_chat_id: str = ""
    agent: SimpleAgent | None = field(default=None, repr=False)
    system_task: dict[str, object] | None = None
    interactive_approvals: bool = False
    rich_transcript: bool = False
    workspace_root: str = ""
    workspace_roots: list[str] | None = None


_DEFAULT_GATEWAY_CLI_SESSION_ID = "default"


# LLM: 请求文件是 Gateway admission 的权威输入；客户端能力必须显式写入结构化 payload，不能按 source/终端文案猜测。
# 函数用途: 校验并原子提交一条 Gateway ask 请求。
def submit_gateway_ask(
    paths: GatewayPaths,
    *,
    params: GatewayAskParams,
) -> tuple[str, Path, Path]:
    prompt, system_task = _normalized_gateway_prompt(params)
    request_id = new_gateway_request_id()
    payload = {
        "id": request_id,
        "kind": "ask",
        "prompt": prompt,
        "inject": params.inject or [],
        "prompt_files": params.prompt_files or [],
        "save": params.save,
        "include_prompt": params.include_prompt,
        "created_at": time.time(),
        "client_pid": 0,
        "status": "pending",
        "priority": "interactive",
        "source": LOCAL_CHAT_SOURCE if params.chat_session_id else "cli_gateway",
        "attempts": 0,
        "client_capabilities": {
            "tool_approval": bool(params.interactive_approvals),
            "rich_transcript": bool(params.rich_transcript),
        },
    }
    if params.resume_context is not None:
        payload["resume_context"] = bool(params.resume_context)
    if system_task:
        payload["system_task"] = system_task
    workspace = gateway_request_workspace_payload(
        params.workspace_root,
        params.workspace_roots,
    )
    if workspace:
        payload["workspace"] = workspace
    conversation = _gateway_conversation_payload(params)
    payload["conversation"] = conversation
    # File-queue clients do not have HTTP headers, so the same structured ingress identity must
    # travel in the canonical request body. Owner routing reads these fields, never prompt text.
    payload["user_id"] = str(params.canonical_user_id or params.channel_user_id)
    payload["metadata"] = _gateway_identity_metadata(params, conversation)
    request_path = write_gateway_request(paths, payload)
    response_path = gateway_response_path(paths, request_id)
    if params.agent is not None:
        from .audit_service import audit_request_queued

        audit_request_queued(
            params.agent,
            {**payload, "status": "queued", "ok": False},
            request_path,
            response_path,
        )
    return request_id, request_path, response_path


# LLM: 普通自然语言不得加载控制运行时；只有显式 slash 语法才按共享 typed parser 校验或转换成 system_task。
# 函数用途: 规范请求正文和可选持续任务载荷，并拒绝绕过控制端点的系统命令。
def _normalized_gateway_prompt(params: GatewayAskParams) -> tuple[str, dict[str, object]]:
    prompt = str(params.prompt or "").strip()
    system_task = dict(params.system_task or {})
    if not prompt.startswith("/"):
        return prompt, system_task
    from ..conversation.control_commands import (
        parse_conversation_task_command,
        system_slash_command_name,
    )

    if not system_task:
        task_command = parse_conversation_task_command(prompt)
        if task_command is not None:
            if not task_command.valid:
                raise ValueError(task_command.usage)
            prompt = task_command.prompt
            system_task = task_command.to_request_payload()
    if system_slash_command_name(prompt):
        raise ValueError("系统命令不能作为普通 Gateway 请求提交，请使用对应控制入口")
    return prompt, system_task


# LLM: 会话路由只读取 typed 参数和共享通道常量；session id 不得从 prompt 或 UI 文案推断。
# 函数用途: 构造 Gateway 请求中的本地聊天会话身份。
def _gateway_conversation_payload(params: GatewayAskParams) -> dict[str, str]:
    session_id = str(params.chat_session_id or _DEFAULT_GATEWAY_CLI_SESSION_ID)
    return {
        "channel": (
            str(params.channel or LOCAL_CHAT_CHANNEL)
            if params.chat_session_id
            else "gateway-cli"
        ),
        "channel_conversation_id": session_id,
        "channel_user_id": str(params.channel_user_id or LOCAL_AGENT_USER_ID),
        "canonical_user_id": str(params.canonical_user_id or LOCAL_AGENT_USER_ID),
    }


# LLM: Owner routing for queued asks must use the same typed metadata as HTTP admission. Group
# discriminators remain explicit provider facts and are never inferred from a conversation id.
# 函数用途: 构造文件队列请求的可信通道身份，供单 Gateway 选择正确用户或群工作区。
def _gateway_identity_metadata(
    params: GatewayAskParams,
    conversation: dict[str, str],
) -> dict[str, str]:
    metadata = {"channel": str(conversation.get("channel") or "gateway-cli")}
    chat_type = str(params.channel_chat_type or "").strip().lower()
    chat_id = str(params.channel_chat_id or "").strip()
    if chat_type and chat_id:
        metadata.update(
            {
                "channel_chat_type": chat_type,
                "channel_chat_id": chat_id,
            }
        )
    return metadata


# LLM: Client cwd is a typed per-thread execution setting, separate from the owner-level Gateway
# queue. Only absolute normalized paths are serialized; the Gateway validates existence, locality,
# and owner scope again before persisting them on the conversation thread.
# 函数用途: 把当前 TUI/CLI 的工作目录整理成可随请求发送的结构化 workspace 字段。
def gateway_request_workspace_payload(
    workspace_root: object,
    workspace_roots: object = None,
) -> dict[str, object]:
    cwd = _absolute_workspace_path(workspace_root)
    if not cwd:
        return {}
    raw_roots = workspace_roots if isinstance(workspace_roots, (list, tuple)) else []
    roots: list[str] = []
    for value in [cwd, *raw_roots]:
        path = _absolute_workspace_path(value)
        if path and path not in roots:
            roots.append(path)
    return {"cwd": cwd, "roots": roots or [cwd]}


# LLM: This helper normalizes syntax only; server-side request execution remains the authority for
# directory existence and permission scope so HTTP and file-queue callers share one hard gate.
# 函数用途: 将一个可能的路径值规范化成绝对路径文本，非法或相对路径返回空。
def _absolute_workspace_path(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text).expanduser()
    if not path.is_absolute():
        return ""
    try:
        return str(path.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return ""


__all__ = [
    "GatewayAskExecutionOptions",
    "GatewayAskParams",
    "gateway_request_workspace_payload",
    "submit_gateway_ask",
]
