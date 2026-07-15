from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# 内置默认支持"主动外呼"的通道。纯路由 helper 和 FakeDeliveryService 用它保持历史默认；生产
# DeliveryService 以 registry capabilities 为权威，因此新增 IM 通过注册 proactive 能力扩展，不改此常量。
# internal/chat/gateway-cli 没有主动能力，不会外发。
PROACTIVE_PUSH_CHANNELS = frozenset({"feishu"})

# 内部交付/运行信号前缀:这些是出口门/调度用的结构化标记,不是给用户看的正文。
# 真实投递服务(delivery.service)据此拦截"以记号开头"的整条回复;
# 逐条结论追加层(conversation.runtime)据此判断该把结论块单独出站还是拼在原文后。
INTERNAL_SIGNAL_PREFIXES = ("[MAIN_AGENT_", "[RUN_", "[SUBAGENT_")
_DELIVERY_COMPLETE_START = "[MAIN_AGENT_DELIVERY_COMPLETE]"
_DELIVERY_COMPLETE_END = "[/MAIN_AGENT_DELIVERY_COMPLETE]"
_MAX_PUBLIC_COMPLETION_SUMMARY_CHARS = 4000
_INTERNAL_SUMMARY_TOKENS = (
    "[main_agent_",
    "[run_",
    "[subagent_",
    "[tool_call",
    "[/tool_call",
    "<tool_call",
    "<tool_result",
)
_TEXT_TOOL_CALL_BLOCK_RE = re.compile(
    r"\[TOOL_CALL\].*?\[/TOOL_CALL\]|<tool_call\b[^>]*>.*?</tool_call\s*>",
    re.IGNORECASE | re.DOTALL,
)
_HOST_ABSOLUTE_PATH_RE = re.compile(
    r"(?P<path>"
    # POSIX 绝对路径的起始 / 不能紧跟在单词、点、波浪线或另一个路径分隔符后。
    # 否则 tasks/foo/output 会从第二段的 /foo/output 开始误命中，最终被折成
    # tasksoutput。URL、./relative、../relative 与普通相对路径也因此保持原样。
    r"(?<![\w.~+\-/\\])/(?!/)[^/\s'\"`<>()（）\[\]{}，。；;、]+"
    r"(?:/[^/\s'\"`<>()（）\[\]{}，。；;、]+)+"
    r"|(?<![\w/\\])~[\\/][^\s'\"`<>()（）\[\]{}，。；;、]+"
    r"|(?<![\w/\\])[A-Za-z]:[\\/][^\s'\"`<>()（）\[\]{}，。；;、]+"
    r")"
)


def leads_with_internal_signal(content: str) -> bool:
    return str(content or "").lstrip().startswith(INTERNAL_SIGNAL_PREFIXES)


# LLM: 用户回复投影只负责把内部完成协议转换成展示文本和结构化产物引用；不得把路径写进 content。
# 类用途: 保存一条可以发给用户的正文，以及仅供后续机器复用的产物引用。
@dataclass(frozen=True)
class UserReplyProjection:
    content: str
    artifacts: tuple[dict[str, object], ...] = ()
    internal_signal: bool = False
    projection_status: str = "plain_text"

    # LLM: 序列化结果会进入网关响应和会话 metadata，artifacts 仍属本机可信结构化事实。
    # 函数用途: 把投影转成能写入 JSON 的字典。
    def to_dict(self) -> dict[str, object]:
        return {
            "content": self.content,
            "artifacts": [dict(item) for item in self.artifacts],
            "internal_signal": self.internal_signal,
            "projection_status": self.projection_status,
        }


# LLM: 通道附件必须来自调用方已完成的 owner/registry 校验；这里只携带发送所需的不可变引用。
# 类用途: 描述一个准备通过聊天通道发送的已登记文件。
@dataclass(frozen=True)
class ChannelAttachment:
    artifact_id: str
    path: str
    name: str = ""
    kind: str = "file"
    sha256: str = ""
    size_bytes: int = 0


# LLM: 这是所有用户通道共用的最终出口净化点；内部标记可作为机器协议，但绝不能成为用户正文。
# 函数用途: 普通回复原样保留；任务完成协议改成简短人话，并另外保留产物引用供下一轮复用。
def project_user_reply(content: str) -> UserReplyProjection:
    text = str(content or "").strip()
    if not leads_with_internal_signal(text):
        return _plain_user_reply_projection(text)
    if text.startswith(_DELIVERY_COMPLETE_START):
        payload = delivery_complete_payload(text)
        if payload is None:
            return UserReplyProjection(
                content="",
                internal_signal=True,
                projection_status="malformed_delivery_complete",
            )
        artifacts = _delivery_artifact_refs(payload.get("artifacts"))
        return UserReplyProjection(
            content=_completed_user_text(
                artifacts,
                _public_completion_summary(payload.get("user_summary")),
            ),
            artifacts=artifacts,
            internal_signal=True,
            projection_status="delivery_complete",
        )
    return UserReplyProjection(
        content="",
        internal_signal=True,
        projection_status="internal_status",
    )


def _plain_user_reply_projection(text: str) -> UserReplyProjection:
    """Remove executed text-tool envelopes before content reaches any IM."""
    if not text:
        return UserReplyProjection(content="")
    cleaned = _TEXT_TOOL_CALL_BLOCK_RE.sub("", text).strip()
    folded = cleaned.casefold()
    if any(token in folded for token in _INTERNAL_SUMMARY_TOKENS):
        prefix = _content_before_internal_protocol(cleaned)
        return UserReplyProjection(
            content=prefix,
            internal_signal=True,
            projection_status="internal_protocol_removed",
        )
    if cleaned:
        return UserReplyProjection(content=cleaned)
    return UserReplyProjection(
        content="",
        internal_signal=True,
        projection_status="tool_envelope_removed",
    )


def _content_before_internal_protocol(text: str) -> str:
    folded = text.casefold()
    positions = [folded.find(token) for token in _INTERNAL_SUMMARY_TOKENS]
    positions = [position for position in positions if position >= 0]
    return text[: min(positions)].strip() if positions else text.strip()


# LLM: 只解析完整、成对的完成标记；不从任意正文猜 JSON，避免普通模型文字获得机器权威。
# 函数用途: 取出 MAIN_AGENT 完成块里的 JSON 对象，格式不完整就返回 None。
# LLM: 完成协议的解析与渲染必须共用同一实现；工具循环可以据此追加一次模型自然回复，
# 但只有这段成对机器协议能携带附件与完成事实，普通模型文字仍不能获得机器权威。
# 函数用途: 解析完整 MAIN_AGENT 完成块，供通道投影和模型完成回复出口共同复用。
def delivery_complete_payload(content: str) -> dict[str, object] | None:
    start = content.find(_DELIVERY_COMPLETE_START)
    if start < 0:
        return None
    body_start = start + len(_DELIVERY_COMPLETE_START)
    end = content.find(_DELIVERY_COMPLETE_END, body_start)
    if end < 0:
        return None
    try:
        payload = json.loads(content[body_start:end].strip())
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


# LLM: 这里只序列化机器信封，不生成任何用户可见句子；user_summary 必须来自模型自然回复。
# 函数用途: 把已验收的结构化完成载荷渲染成统一内部信号，附件和正文随后由通道投影拆开。
def render_delivery_complete_signal(payload: dict[str, object]) -> str:
    return (
        _DELIVERY_COMPLETE_START
        + "\n"
        + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
        + _DELIVERY_COMPLETE_END
    )


# LLM: 产物引用来自结构化完成块，保留 path 仅供内部后续工具复用；用户正文只能使用 name。
# 函数用途: 清洗、去重完成块里的成功文件引用。
def _delivery_artifact_refs(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        return ()
    refs: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict) or item.get("ok") is not True:
            continue
        artifact_id = str(item.get("artifact_id") or "").strip()
        path = str(item.get("path") or "").strip()
        if not artifact_id and not path:
            continue
        key = (artifact_id, path)
        if key in seen:
            continue
        seen.add(key)
        refs.append(
            {
                "artifact_id": artifact_id,
                "path": path,
                "name": Path(path).name if path else artifact_id,
                "kind": str(item.get("kind") or "file"),
                "ok": True,
            }
        )
    return tuple(refs)


# LLM: 完成正文仍属于模型；产物名留在 typed metadata，通道层不得凭文件列表编造一句完成话术。
# 函数用途: 返回清洗后的模型完成说明；模型没有合格说明时保持空正文。
def _completed_user_text(
    artifacts: tuple[dict[str, object], ...],
    user_summary: str = "",
) -> str:
    del artifacts
    return user_summary


def _public_completion_summary(value: object) -> str:
    text = "".join(
        character
        for character in str(value or "")
        if character in {"\n", "\t"} or ord(character) >= 32
    ).strip()
    if not text:
        return ""
    folded = text.casefold()
    if any(token in folded for token in _INTERNAL_SUMMARY_TOKENS):
        return ""
    text = _HOST_ABSOLUTE_PATH_RE.sub(_host_path_basename, text)
    if len(text) > _MAX_PUBLIC_COMPLETION_SUMMARY_CHARS:
        text = text[:_MAX_PUBLIC_COMPLETION_SUMMARY_CHARS].rstrip() + "…"
    return text


def _host_path_basename(match: re.Match[str]) -> str:
    raw = match.group("path").replace("\\", "/").rstrip("/")
    return raw.rsplit("/", 1)[-1] or "文件"


# LLM: 投递上下文只由入站适配器、owner 配置或会话绑定构造；模型输出不得覆盖 channel/target/reply_to。
# 类用途: 保存一次回复要送往哪里的可信路由，以及回复原消息所需的通道上下文。
@dataclass(frozen=True)
class DeliveryContext:
    channel: str
    target: str
    mode: str = "proactive"
    conversation_id: str = ""
    reply_to: str = ""
    progress_handle: str = ""
    request_id: str = ""
    thread_id: str = ""
    task_id: str = ""


# LLM: 回复信封只描述用户可见正文与已校验附件，不携带收件人；路由权威必须留在 DeliveryContext。
# 类用途: 用一个通道无关的结构承载普通最终回复、主动消息和附件。
@dataclass(frozen=True)
class ReplyEnvelope:
    content: str = ""
    attachments: tuple[ChannelAttachment, ...] = ()
    format: str = "markdown"


# LLM: 投递回执同时记录正文与结构化附件 ID，不能把附件路径拼回 content。
# 类用途: 描述一次通道投递的可审计结果。
@dataclass(frozen=True)
class DeliveryReceipt:
    channel: str
    target: str
    content: str
    thread_id: str = ""
    task_id: str = ""
    delivery_status: str = "recorded"
    error_code: str = ""
    attachment_ids: tuple[str, ...] = ()


# LLM: 后台会话运行时只依赖这一最小投递协议，不能反向依赖网关或某个 provider 实现。
# 类用途: 约束真实与 fake 投递服务都提供相同的 typed deliver 方法。
class DeliveryServiceProtocol(Protocol):
    # LLM: 实现必须把 context 当可信路由、envelope 当无目标内容，不能从正文反推收件人。
    # 函数用途: 投递一份回复信封并返回结构化回执。
    def deliver(self, context: DeliveryContext, envelope: ReplyEnvelope) -> DeliveryReceipt: ...

    # LLM: 后台路由只读取 registry 的结构化 proactive 能力，不从通道名或模型文字猜测。
    # 函数用途: 判断某通道是否允许主动外呼。
    def supports_proactive(self, channel: str) -> bool: ...


# LLM: target validator 只能返回结构化判断；provider HTTP 错误或自然语言说明不能替代此前置事实。
# 类用途: 描述一个通道目标是否符合其声明的地址类型。
@dataclass(frozen=True)
class ChannelTargetDecision:
    allowed: bool
    channel: str
    target_kind: str
    error_code: str = ""


# LLM: 测试 adapter 只记录统一投递回执，不模拟 provider 私有协议。
# 类用途: 为 conversation 运行时测试保存某个通道收到的投递。
@dataclass
class FakeDeliveryAdapter:
    channel: str
    sent_messages: list[DeliveryReceipt] = field(default_factory=list)

    # LLM: fake 只接受拆分后的可信上下文和回复信封，保持与真实 DeliveryService 的边界一致。
    # 函数用途: 记录一条测试投递并返回成功回执。
    def deliver(self, context: DeliveryContext, envelope: ReplyEnvelope) -> DeliveryReceipt:
        message = DeliveryReceipt(
            channel=context.channel,
            target=context.target,
            content=envelope.content,
            thread_id=context.thread_id,
            task_id=context.task_id,
            delivery_status="sent",
            attachment_ids=tuple(item.artifact_id for item in envelope.attachments),
        )
        self.sent_messages.append(message)
        return message


# LLM: conversation 测试替身必须实现真实服务同名的 deliver(context, envelope)，避免测试维护旧出口。
# 类用途: 按通道保存 fake adapter，并为后台运行时提供无外部副作用的统一投递服务。
class FakeDeliveryService:
    # LLM: 每个 fake 服务独立保存记录，测试之间不能共享可变发送状态。
    # 函数用途: 创建空的测试投递服务。
    def __init__(self) -> None:
        self._adapters: dict[str, FakeDeliveryAdapter] = {}

    # LLM: fake adapter 以规范化通道名为键，空通道统一记作 internal。
    # 函数用途: 取得或创建某个测试通道的记录器。
    def adapter(self, channel: str) -> FakeDeliveryAdapter:
        key = str(channel or "internal")
        adapter = self._adapters.get(key)
        if adapter is None:
            adapter = FakeDeliveryAdapter(key)
            self._adapters[key] = adapter
        return adapter

    # LLM: fake 与真实服务必须共享同一个 typed 方法，不能重新引入混合路由和正文的 request。
    # 函数用途: 把测试回复交给对应 fake adapter 记录。
    def deliver(self, context: DeliveryContext, envelope: ReplyEnvelope) -> DeliveryReceipt:
        return self.adapter(context.channel).deliver(context, envelope)

    # LLM: fake 的主动通道名单与内置生产默认保持一致，测试可验证路由但不会真的外发。
    # 函数用途: 判断测试通道是否支持主动投递。
    def supports_proactive(self, channel: str) -> bool:
        return str(channel or "").strip().lower() in PROACTIVE_PUSH_CHANNELS


__all__ = [
    "INTERNAL_SIGNAL_PREFIXES",
    "PROACTIVE_PUSH_CHANNELS",
    "ChannelAttachment",
    "ChannelTargetDecision",
    "DeliveryContext",
    "DeliveryReceipt",
    "DeliveryServiceProtocol",
    "FakeDeliveryAdapter",
    "FakeDeliveryService",
    "ReplyEnvelope",
    "UserReplyProjection",
    "leads_with_internal_signal",
    "project_user_reply",
]
