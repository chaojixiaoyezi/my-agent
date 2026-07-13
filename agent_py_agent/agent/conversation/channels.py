
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# 支持"主动外呼"(服务端主动发起、用户没先问)的外部通道:后台主代理被子代理事件叫回后产出的
# 汇总要投到这些通道(飞书 send_message 走 REST,无需长连接)。internal/chat/gateway-cli 是本地
# 轮询/内部通道,不主动外发。路由升级(conversation.runtime)与真实投递(gateway_parts.channel_delivery)
# 共用这一份定义,保持"能升级到哪个通道"和"能投到哪个通道"一致。
PROACTIVE_PUSH_CHANNELS = frozenset({"feishu"})

# 内部交付/运行信号前缀:这些是出口门/调度用的结构化标记,不是给用户看的正文。
# 真实投递枢纽(gateway_parts.channel_delivery)据此拦截"以记号开头"的整条回复;
# 逐条结论追加层(conversation.runtime)据此判断该把结论块单独出站还是拼在原文后。
INTERNAL_SIGNAL_PREFIXES = ("[MAIN_AGENT_", "[RUN_", "[SUBAGENT_")
_DELIVERY_COMPLETE_START = "[MAIN_AGENT_DELIVERY_COMPLETE]"
_DELIVERY_COMPLETE_END = "[/MAIN_AGENT_DELIVERY_COMPLETE]"


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
        return UserReplyProjection(content=text)
    if text.startswith(_DELIVERY_COMPLETE_START):
        payload = _delivery_complete_payload(text)
        if payload is None:
            return UserReplyProjection(
                content="任务已经处理完成，但结果整理时出现异常，请稍后再试。",
                internal_signal=True,
                projection_status="malformed_delivery_complete",
            )
        artifacts = _delivery_artifact_refs(payload.get("artifacts"))
        return UserReplyProjection(
            content=_completed_user_text(artifacts),
            artifacts=artifacts,
            internal_signal=True,
            projection_status="delivery_complete",
        )
    if text.startswith("[RUN_UNFINISHED_EXIT]"):
        message = "这项工作还没有完成，系统会继续保留当前进度。"
    elif text.startswith("[RUN_NONBLOCKING_YIELD]"):
        message = "这项工作仍在处理中。"
    else:
        message = "任务正在处理，目前还没有可交付的最终结果。"
    return UserReplyProjection(
        content=message,
        internal_signal=True,
        projection_status="internal_status",
    )


# LLM: 只解析完整、成对的完成标记；不从任意正文猜 JSON，避免普通模型文字获得机器权威。
# 函数用途: 取出 MAIN_AGENT 完成块里的 JSON 对象，格式不完整就返回 None。
def _delivery_complete_payload(content: str) -> dict[str, object] | None:
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


# LLM: 用户文案不能声称“已发送”，因为此处只知道任务收口成功，不知道外部通道副作用是否成功。
# 函数用途: 根据产物文件名生成简短完成提示，不暴露服务器路径和验收字段。
def _completed_user_text(artifacts: tuple[dict[str, object], ...]) -> str:
    names = [str(item.get("name") or item.get("artifact_id") or "文件") for item in artifacts]
    if not names:
        return "任务已经处理完成。"
    if len(names) == 1:
        return f"文件已经生成：{names[0]}"
    rendered = "\n".join(f"- {name}" for name in names)
    return f"任务已经处理完成，生成了这些文件：\n{rendered}"


# LLM: 通道回执同时记录正文与结构化附件 ID，不能把附件路径拼回 content。
# 类用途: 描述一次通道发送的可审计结果。
@dataclass(frozen=True)
class SentChannelMessage:
    channel: str
    target: str
    content: str
    thread_id: str = ""
    task_id: str = ""
    delivery_status: str = "recorded"
    error_code: str = ""
    attachment_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChannelTargetDecision:
    allowed: bool
    channel: str
    target_kind: str
    error_code: str = ""


def validate_channel_target(channel: object, target: object) -> ChannelTargetDecision:
    """Validate the provider address kind before any external send is attempted."""

    channel_name = str(channel or "").strip().lower()
    target_value = str(target or "").strip()
    if not target_value:
        return ChannelTargetDecision(False, channel_name, "unknown", "CHANNEL_TARGET_MISSING")
    if any(char.isspace() for char in target_value):
        return ChannelTargetDecision(False, channel_name, "unknown", "CHANNEL_TARGET_INVALID")
    if channel_name == "feishu":
        allowed = target_value.startswith("ou_") and len(target_value) > 3
        return ChannelTargetDecision(
            allowed,
            channel_name,
            "open_id",
            "" if allowed else "CHANNEL_TARGET_INVALID",
        )
    return ChannelTargetDecision(True, channel_name, "opaque")


# LLM: 发送请求把附件作为独立 typed 字段传递，避免靠正文中的 MEDIA/path 约定猜测。
# 类用途: 描述一次发给确定通道目标的正文及附件请求。
@dataclass(frozen=True)
class ChannelSendRequest:
    channel: str
    target: str
    content: str
    thread_id: str = ""
    task_id: str = ""
    attachments: tuple[ChannelAttachment, ...] = ()


@dataclass
class FakeChannelAdapter:
    channel: str
    sent_messages: list[SentChannelMessage] = field(default_factory=list)

    def send_message(
        self,
        *,
        target: str,
        content: str,
        thread_id: str = "",
        task_id: str = "",
    ) -> SentChannelMessage:
        message = SentChannelMessage(
            channel=self.channel,
            target=target,
            content=content,
            thread_id=thread_id,
            task_id=task_id,
            delivery_status="sent",
        )
        self.sent_messages.append(message)
        return message


class FakeChannelHub:
    def __init__(self) -> None:
        self._adapters: dict[str, FakeChannelAdapter] = {}

    def adapter(self, channel: str) -> FakeChannelAdapter:
        key = str(channel or "internal")
        adapter = self._adapters.get(key)
        if adapter is None:
            adapter = FakeChannelAdapter(key)
            self._adapters[key] = adapter
        return adapter

    def send(self, request: ChannelSendRequest) -> SentChannelMessage:
        return self.adapter(request.channel).send_message(
            target=request.target,
            content=request.content,
            thread_id=request.thread_id,
            task_id=request.task_id,
        )


__all__ = [
    "INTERNAL_SIGNAL_PREFIXES",
    "PROACTIVE_PUSH_CHANNELS",
    "ChannelAttachment",
    "ChannelSendRequest",
    "ChannelTargetDecision",
    "FakeChannelAdapter",
    "FakeChannelHub",
    "SentChannelMessage",
    "UserReplyProjection",
    "leads_with_internal_signal",
    "project_user_reply",
    "validate_channel_target",
]
