# LLM: 会话间消息与派活的权限判定唯一权威。只读结构化字段（owner_kind/kind/开关/同 owner/同 thread），
#   绝不解析自然语言、任务正文、模型摘要或报告文本来决定放行；显式参数与当前 runner 上下文冲突时只返回
#   scope_warnings，不静默猜、不冒充别的会话。错误码字符串是本模块的公开合同，改名前先全局搜索调用方与测试。
# 模块用途: 给"会话间发消息"和"会话间派任务"提供可单测的纯权限判定，供模型工具、TUI 命令与后续服务端入口共用。
from __future__ import annotations

from dataclasses import dataclass

from ..user_space.owner_resolver import OwnerIdentity

# kind 结构化取值：消息与任务。
SESSION_KIND_MESSAGE = "message"
SESSION_KIND_TASK = "task"
SESSION_KINDS = frozenset({SESSION_KIND_MESSAGE, SESSION_KIND_TASK})

# 结构化错误码（公开合同）。
SESSION_MESSAGING_DISABLED = "SESSION_MESSAGING_DISABLED"
SESSION_TASK_NOT_ALLOWED = "SESSION_TASK_NOT_ALLOWED"
SESSION_TARGET_OUT_OF_SCOPE = "SESSION_TARGET_OUT_OF_SCOPE"
SESSION_TASK_TARGET_SELF = "SESSION_TASK_TARGET_SELF"
# 目标是 IM 渠道的会话：第一期不支持把普通会话消息投进 IM 目标，返回明确错误码。
SESSION_TARGET_CHANNEL_UNSUPPORTED = "SESSION_TARGET_CHANNEL_UNSUPPORTED"

# IM 渠道集合：这些渠道的会话第一期不做会话间消息目标（第一期只服务本机会话）。
IM_CHANNELS = frozenset({"feishu", "qq", "wecom", "dingtalk"})

# guidance metadata 里标记来源的结构化键与取值；注入渲染据此把会话消息呈现为宿主事件而非用户原话。
SESSION_MESSAGE_ORIGIN_KIND = "session_message"
SESSION_MESSAGE_ORIGIN_THREAD_KEY = "origin_thread_id"
# 宿主事件标记：渲染会话消息时写在最前面，明确它不是用户原话。
SESSION_MESSAGE_HOST_EVENT_MARKER = "[SESSION_MESSAGE_HOST_EVENT]"

# owner_kind 结构化取值；main 是管理员（含 IM 绑定管理员私聊），user/group 是普通用户。
OWNER_KIND_MAIN = "main"


# LLM: 冻结值对象；两个开关与目标归属都由调用方从结构化配置/存储读出后传入，本对象不做 IO。
# 类用途: 承载一次权限判定所需的全部结构化输入，便于合同单测逐格构造。
@dataclass(frozen=True)
class SessionMessagingRequest:
    # 当前 runner 的发送方身份；只从这里取 owner_kind，不从模型显式参数取。
    sender_identity: OwnerIdentity
    # 发送方所在会话 thread_id（用于同 thread 判定与每对会话限流分桶）。
    sender_thread_id: str
    # 接收方会话 thread_id。
    target_thread_id: str
    # 接收方会话的 owner 身份；目标不存在或不属于发送方 owner 时二者都按"越界"处理，不区分存在性。
    target_owner_identity: OwnerIdentity | None
    # 发送行为类型：message 或 task。
    kind: str
    # 结构化开关（来自 CapabilityConfig）。
    messaging_admin_enabled: bool
    messaging_user_enabled: bool
    task_admin_enabled: bool
    # 目标会话的渠道（来自 canonical thread 的 channel_bindings）；IM 渠道第一期不作为目标。
    target_channel: str = ""


# LLM: 判定结果只有"允许/拒绝 + 稳定错误码 + 结构化 warnings"，没有自然语言结论；调用方据此决定是否继续。
# 类用途: 保存一次权限判定的结构化结果，供工具与服务端一起读取。
@dataclass(frozen=True)
class SessionMessagingDecision:
    allowed: bool
    error_code: str = ""
    scope_warnings: tuple[str, ...] = ()


# LLM: 纯函数，无 IO、无副作用；顺序固定：先 kind 合法性，再目标归属，再自派任务，最后按 owner_kind 与开关。
#   目标不存在（None）与目标属于别的 owner 都返回同一码 SESSION_TARGET_OUT_OF_SCOPE，不泄露存在性；
#   发送方 target 就是自己时任务拒绝、消息允许（等价于给自己插话）。
# 函数用途: 按结构化字段判定一次会话间消息或派任务是否允许。
def decide_session_messaging(request: SessionMessagingRequest) -> SessionMessagingDecision:
    kind = str(request.kind or "").strip().lower()
    if kind not in SESSION_KINDS:
        return SessionMessagingDecision(False, "SESSION_KIND_INVALID", (f"unknown_kind:{kind}",))

    warnings: list[str] = []
    sender_kind = str(request.sender_identity.owner_kind or "").strip()
    sender_is_admin = sender_kind == OWNER_KIND_MAIN

    # 目标归属：None（不存在）与跨 owner 都按越界处理，返回同一码，不区分存在性。
    target_identity = request.target_owner_identity
    if target_identity is None or _owner_key(target_identity) != _owner_key(request.sender_identity):
        return SessionMessagingDecision(
            False,
            SESSION_TARGET_OUT_OF_SCOPE,
            ("target_owner_unresolved_or_foreign",),
        )

    same_thread = (
        str(request.target_thread_id or "").strip() != ""
        and str(request.target_thread_id or "").strip()
        == str(request.sender_thread_id or "").strip()
    )

    # 目标是 IM 渠道的会话：第一期不支持作为会话消息目标，返回明确码。
    if str(request.target_channel or "").strip().lower() in IM_CHANNELS:
        return SessionMessagingDecision(False, SESSION_TARGET_CHANNEL_UNSUPPORTED, ())

    if kind == SESSION_KIND_TASK:
        if not sender_is_admin:
            # 普通用户派任务第一期直接拒绝，不提供开关。
            return SessionMessagingDecision(False, SESSION_TASK_NOT_ALLOWED, ())
        if not request.task_admin_enabled:
            return SessionMessagingDecision(False, SESSION_TASK_NOT_ALLOWED, ("task_admin_disabled",))
        if same_thread:
            return SessionMessagingDecision(False, SESSION_TASK_TARGET_SELF, ())
        return SessionMessagingDecision(True)

    # message
    if sender_is_admin:
        if not request.messaging_admin_enabled:
            return SessionMessagingDecision(False, SESSION_MESSAGING_DISABLED, ("messaging_admin_disabled",))
        return SessionMessagingDecision(True)
    if not request.messaging_user_enabled:
        return SessionMessagingDecision(False, SESSION_MESSAGING_DISABLED, ("messaging_user_disabled",))
    if warnings:
        return SessionMessagingDecision(True, "", tuple(warnings))
    return SessionMessagingDecision(True)


# LLM: owner 归一键只由结构化 provider/owner_kind/owner_id 组成；不做字符串模糊匹配或 trim 猜测。
# 函数用途: 生成跨 owner 比较用的稳定键。
def _owner_key(identity: OwnerIdentity) -> tuple[str, str, str]:
    return (
        str(identity.provider or "").strip().lower(),
        str(identity.owner_kind or "").strip().lower(),
        str(identity.owner_id or "").strip(),
    )


# LLM: 只读 home_paths 的 owner_kind 与开关，不读文本、不做 IO；注册处据此决定工具是否进 registry。
#   返回 False 时工具根本不进模型工具列表（结构化可用性），而不是"调用时才拒绝"。
# 函数用途: 判断当前 owner 是否应该看到"发会话消息"工具。
def session_messaging_tool_visible(home_paths: object, config: object) -> bool:
    owner_kind = str(getattr(home_paths, "owner_kind", "") or "").strip()
    if owner_kind == OWNER_KIND_MAIN:
        return bool(getattr(config, "session_messaging_admin_enabled", True))
    return bool(getattr(config, "session_messaging_user_enabled", False))


# LLM: 派任务第一期只开给管理员，且没有普通用户开关；返回 False 时工具不进 registry。
# 函数用途: 判断当前 owner 是否应该看到"派会话任务"工具。
def session_task_tool_visible(home_paths: object, config: object) -> bool:
    owner_kind = str(getattr(home_paths, "owner_kind", "") or "").strip()
    return owner_kind == OWNER_KIND_MAIN and bool(
        getattr(config, "session_task_admin_enabled", True)
    )


__all__ = [
    "OWNER_KIND_MAIN",
    "SESSION_KINDS",
    "SESSION_KIND_MESSAGE",
    "SESSION_KIND_TASK",
    "SESSION_MESSAGING_DISABLED",
    "SESSION_TARGET_OUT_OF_SCOPE",
    "SESSION_TARGET_CHANNEL_UNSUPPORTED",
    "IM_CHANNELS",
    "SESSION_TASK_NOT_ALLOWED",
    "SESSION_TASK_TARGET_SELF",
    "SessionMessagingDecision",
    "SessionMessagingRequest",
    "decide_session_messaging",
    "session_messaging_tool_visible",
    "session_task_tool_visible",
]
