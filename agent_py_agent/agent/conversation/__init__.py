# LLM: 本包门面按符号延迟加载；输入补全或常量读取不得初始化 compact、后台调度、Memory 或 Agent orchestration。
# 模块用途: 保留会话模块的公开导入路径，同时让终端、飞书和未来 Web 的薄入口只加载所需合同。

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    "CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR": (
        "authority",
        "CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR",
    ),
    "conversation_transcript_is_authoritative": (
        "authority",
        "conversation_transcript_is_authoritative",
    ),
    "ChannelAttachment": ("channels", "ChannelAttachment"),
    "ChannelTargetDecision": ("channels", "ChannelTargetDecision"),
    "DeliveryContext": ("channels", "DeliveryContext"),
    "DeliveryReceipt": ("channels", "DeliveryReceipt"),
    "DeliveryServiceProtocol": ("channels", "DeliveryServiceProtocol"),
    "FakeDeliveryAdapter": ("channels", "FakeDeliveryAdapter"),
    "FakeDeliveryService": ("channels", "FakeDeliveryService"),
    "ReplyEnvelope": ("channels", "ReplyEnvelope"),
    "UserReplyProjection": ("channels", "UserReplyProjection"),
    "leads_with_internal_signal": ("channels", "leads_with_internal_signal"),
    "project_user_reply": ("channels", "project_user_reply"),
    "ConversationCompactResult": ("compact", "ConversationCompactResult"),
    "ConversationScope": ("compact", "ConversationScope"),
    "conversation_scope": ("compact", "conversation_scope"),
    "prepare_conversation_context": ("compact", "prepare_conversation_context"),
    "BackgroundDeliveryCommit": ("models", "BackgroundDeliveryCommit"),
    "BackgroundMainAgentReport": ("models", "BackgroundMainAgentReport"),
    "ChannelBinding": ("models", "ChannelBinding"),
    "ConversationThread": ("models", "ConversationThread"),
    "MessageLogEntry": ("models", "MessageLogEntry"),
    "ObservationEvent": ("models", "ObservationEvent"),
    "ProgressPolicy": ("models", "ProgressPolicy"),
    "ThreadGoal": ("models", "ThreadGoal"),
    "ThreadTaskLink": ("models", "ThreadTaskLink"),
    "WakeSignal": ("models", "WakeSignal"),
    "BackgroundMainAgentRuntime": ("runtime", "BackgroundMainAgentRuntime"),
    "BackgroundMainAgentScheduler": ("runtime", "BackgroundMainAgentScheduler"),
    "BackgroundRunRequest": ("runtime", "BackgroundRunRequest"),
    "ChannelMessageRuntime": ("runtime", "ChannelMessageRuntime"),
    "ConversationStore": ("store", "ConversationStore"),
    "GuidanceEntry": ("store", "GuidanceEntry"),
    "complete_current_conversation_task": (
        "task_promotion",
        "complete_current_conversation_task",
    ),
    "promote_current_conversation_task": (
        "task_promotion",
        "promote_current_conversation_task",
    ),
}

__all__ = list(_EXPORTS)


# LLM: 公开符号和兄弟模块都只在首次访问时解析并缓存；不得因 convenience import 恢复整包 eager graph。
# 函数用途: 按调用方真正需要的会话能力加载对应文件。
def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is not None:
        module_name, attribute_name = target
        module = importlib.import_module(f".{module_name}", __name__)
        value = getattr(module, attribute_name)
        globals()[name] = value
        return value
    try:
        module = importlib.import_module(f".{name}", __name__)
    except ModuleNotFoundError as exc:
        if exc.name == f"{__name__}.{name}":
            raise AttributeError(name) from None
        raise
    globals()[name] = module
    return module


# LLM: introspection 只展示可用入口，不导入 runtime。
# 函数用途: 为补全和调试器列出会话公开符号。
def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
