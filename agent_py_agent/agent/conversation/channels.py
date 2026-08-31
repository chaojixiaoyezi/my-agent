# LLM: 本模块定义通道投递、用户可见投影和本地 transcript 路由常量；调用方不得从正文猜路由或授权。
# 模块用途: 为 Gateway、CLI 和消息出口提供统一的通道身份、投递信封与安全正文投影。

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from .user_visible_text import sanitize_user_visible_text

# 内置默认支持"主动外呼"的通道。纯路由 helper 和 FakeDeliveryService 用它保持历史默认；生产
# DeliveryService 以 registry capabilities 为权威，因此新增 IM 通过注册 proactive 能力扩展，不改此常量。
# internal/chat/gateway-cli 没有主动能力，不会外发。
PROACTIVE_PUSH_CHANNELS = frozenset({"feishu"})
LOCAL_CHAT_CHANNEL = "chat"
LOCAL_CHAT_SOURCE = "cli_chat"
LOCAL_AGENT_USER_ID = "local-agent"

# LLM: 本地会话路由的交付提交是权威 transcript append，不能按“未注册 IM”
# 处理。真实富 TUI 使用独立的 ``tui`` channel 身份，它和 CLI/chat 一样由
# ConversationStore 交付；未知外部通道仍不在此集合中并保持 fail-closed。
# 常量用途: 声明由 Gateway/CLI/TUI 的权威会话库直接承诺的路由能力。
TRANSCRIPT_DELIVERY_CHANNELS = frozenset(
    {"", "internal", "local", "cli", "terminal", "chat", "tui", "gateway-cli", "http"}
)


# LLM: 本地 transcript 能力只读程序声明的通道事实，不从模型正文、目标或
# adapter 建立失败推断。
# 函数用途: 判断一个路由是否可以用本地权威会话追加作为真实交付回执。
def supports_transcript_delivery(channel: str) -> bool:
    return str(channel or "").strip().lower() in TRANSCRIPT_DELIVERY_CHANNELS

# 内部交付/运行信号前缀:这些是出口门/调度用的结构化标记,不是给用户看的正文。
# 真实投递服务(delivery.service)据此拦截"以记号开头"的整条回复;
# 用户投影据此压制整条内部运行信号。
INTERNAL_SIGNAL_PREFIXES = ("[RUN_", "[SUBAGENT_")
_HOST_ABSOLUTE_PATH_RE = re.compile(
    r"(?P<path>"
    r"(?<![\w.~+\-/\\])/(?!/)[^/\s'\"`<>()（）\[\]{}，。；;、]+"
    r"(?:/[^/\s'\"`<>()（）\[\]{}，。；;、]+)+"
    r"|(?<![\w/\\])~[\\/][^\s'\"`<>()（）\[\]{}，。；;、]+"
    r"|(?<![\w/\\])[A-Za-z]:[\\/][^\s'\"`<>()（）\[\]{}，。；;、]+"
    r")"
)

def leads_with_internal_signal(content: str) -> bool:
    return str(content or "").lstrip().startswith(INTERNAL_SIGNAL_PREFIXES)


# LLM: 用户回复投影只负责净化正文并压制内部协议；产物引用由结构化工具记录单独携带。
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
# 函数用途: 普通回复经协议清洗后保留，任何内部状态信号都不进入用户正文。
def project_user_reply(content: str) -> UserReplyProjection:
    text = str(content or "").strip()
    if not leads_with_internal_signal(text):
        return _plain_user_reply_projection(text)
    return UserReplyProjection(
        content="",
        internal_signal=True,
        projection_status="internal_status",
    )


def _plain_user_reply_projection(text: str) -> UserReplyProjection:
    """Remove executed text-tool envelopes before content reaches any IM."""
    if not text:
        return UserReplyProjection(content="")
    sanitization = sanitize_user_visible_text(text)
    if sanitization.removed_protocol:
        # 剥离协议后仍有正文 → 可交付给用户(与 natural_user_reply 的
        # 接受语义一致,否则 M2.7 表达轮回执带 [TOOL_CALL] 块会整条
        # USER_REPLY_UNAVAILABLE,用户收不到任何回复);剥离后为空才是纯内部信号。
        return UserReplyProjection(
            content=sanitization.content,
            internal_signal=not sanitization.content,
            projection_status=(
                "internal_protocol_removed" if sanitization.content else "tool_envelope_removed"
            ),
        )
    return UserReplyProjection(content=sanitization.content)


def redact_host_absolute_paths(text: str) -> str:
    """Remove server topology at external channel boundaries, preserving relative refs."""
    return _HOST_ABSOLUTE_PATH_RE.sub(_host_path_basename, str(text or ""))


def _host_path_basename(match: re.Match[str]) -> str:
    raw = match.group("path").replace("\\", "/").rstrip("/")
    return raw.rsplit("/", 1)[-1] or "文件"


# LLM: 标识遮蔽只消费调用方从可信 request/thread/delivery 结构中传入的精确值；不得扫描正文猜
#   哪一段“看起来像 ID”，也不得把模型文字作为新的遮蔽规则。
# 函数用途: 在用户出口和权威用户 transcript 精确替换当前用户、会话、请求和任务的内部标识。
def redact_structured_identifiers(
    text: str,
    identifiers: Iterable[tuple[object, str]],
) -> str:
    projected = str(text or "")
    replacements: dict[str, str] = {}
    for raw_value, public_label in identifiers:
        value = str(raw_value or "").strip()
        # 短词很可能是普通正文（例如用户名、项目名或数字）；内部路由标识均应使用足够长的稳定值。
        if len(value) < 8:
            continue
        replacements.setdefault(value, str(public_label or "当前对象"))
    for value in sorted(replacements, key=len, reverse=True):
        projected = projected.replace(value, replacements[value])
    return projected


# LLM: DeliveryContext 是主动/回复投递的唯一可信路由，出口遮蔽只能从这里取值，不能从正文反推收件人。
# 函数用途: 去掉一条用户回复中意外复述的通道目标、会话、消息、请求、线程和任务标识。
def redact_delivery_context_identifiers(text: str, context: DeliveryContext) -> str:
    return redact_structured_identifiers(
        text,
        (
            (context.target, "当前会话"),
            (context.conversation_id, "当前会话"),
            (context.reply_to, "当前消息"),
            (context.progress_handle, "当前进度"),
            (context.request_id, "当前请求"),
            (context.thread_id, "当前会话"),
            (context.task_id, "当前任务"),
        ),
    )


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
    # 外部 provider 原生去重只接收程序生成的稳定键；ReplyEnvelope/模型参数不能覆盖。
    idempotency_key: str = ""


# LLM: 回复信封只描述用户可见正文与已校验附件，不携带收件人；路由权威必须留在 DeliveryContext。
# 类用途: 用一个通道无关的结构承载普通最终回复、主动消息和附件。
@dataclass(frozen=True)
class ReplyEnvelope:
    content: str = ""
    attachments: tuple[ChannelAttachment, ...] = ()
    format: str = "markdown"
    # 由工具结果、观察事件或任务账本提供的逻辑引用；DeliveryService 只搬运，不从正文提取。
    evidence_refs: tuple[str, ...] = ()


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
    evidence_refs: tuple[str, ...] = ()
    receipt_id: str = ""


# LLM: 后台会话运行时只依赖这一最小投递协议，不能反向依赖网关或某个 provider 实现。
# 类用途: 约束真实与 fake 投递服务都提供相同的 typed deliver 方法。
class DeliveryServiceProtocol(Protocol):
    # LLM: 实现必须把 context 当可信路由、envelope 当无目标内容，不能从正文反推收件人。
    # 函数用途: 投递一份回复信封并返回结构化回执。
    def deliver(self, context: DeliveryContext, envelope: ReplyEnvelope) -> DeliveryReceipt: ...

    # LLM: 后台路由只读取 registry 的结构化 proactive 能力，不从通道名或模型文字猜测。
    # 函数用途: 判断某通道是否允许主动外呼。
    def supports_proactive(self, channel: str) -> bool: ...

    # LLM: transcript 能力和 provider 主动外呼是两种不同的结构化交付方式。
    # 函数用途: 判断某通道是否以权威会话追加作为用户可见交付提交。
    def supports_transcript(self, channel: str) -> bool: ...


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
            evidence_refs=tuple(envelope.evidence_refs),
            receipt_id=(
                str(context.idempotency_key or context.request_id or "").strip()
                or f"fake-receipt-{len(self.sent_messages) + 1}"
            ),
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

    # LLM: fake 与生产服务共用同一本地 transcript 能力声明，防止测试只覆盖
    # 伪造成功的 provider 回执而遗漏 CLI 真实路径。
    # 函数用途: 返回测试通道是否由权威会话库直接交付。
    def supports_transcript(self, channel: str) -> bool:
        return supports_transcript_delivery(channel)


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
    "TRANSCRIPT_DELIVERY_CHANNELS",
    "UserReplyProjection",
    "leads_with_internal_signal",
    "supports_transcript_delivery",
    "project_user_reply",
    "redact_host_absolute_paths",
]
