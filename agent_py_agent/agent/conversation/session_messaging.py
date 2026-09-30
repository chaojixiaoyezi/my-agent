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
# 身份缺失：拿不到结构化 owner 身份时必须 fail closed，绝不默认成 main/local。
SESSION_IDENTITY_UNAVAILABLE = "SESSION_IDENTITY_UNAVAILABLE"
# 当前上下文没有会话（独立命令等）。
SESSION_NO_CURRENT_THREAD = "SESSION_NO_CURRENT_THREAD"
# 派活链深度超限：由对端任务触发的任务沿 origin_task_id 链累计，超过上限拒绝。
SESSION_TASK_CHAIN_LIMIT = "SESSION_TASK_CHAIN_LIMIT"
# 每对会话每小时消息上限（回报消息也计入）。
SESSION_TASK_RATE_LIMIT = "SESSION_TASK_RATE_LIMIT"
# 查询/取消时找不到该任务。
SESSION_TASK_NOT_FOUND = "SESSION_TASK_NOT_FOUND"

# guidance metadata 里区分 task 正文的来源标记（与 session_message 并列）。
SESSION_TASK_ORIGIN_KIND = "session_task"

# 第一期允许的接收方渠道白名单：只允许本地渠道（TUI/CLI/本机）。这是**白名单**而非黑名单：
# 任何不在名单里的渠道（包括以后新增的 IM 渠道）一律拒绝，fail closed，避免封闭枚举漏项。
LOCAL_TARGET_CHANNELS = frozenset({"chat", "cli", "local", "tui", "gateway-cli", "http"})

# guidance metadata 里标记来源的结构化键与取值；注入渲染据此把会话消息呈现为宿主事件而非用户原话。
SESSION_MESSAGE_ORIGIN_KIND = "session_message"
SESSION_MESSAGE_ORIGIN_THREAD_KEY = "origin_thread_id"
# 宿主事件标记：渲染会话消息时写在最前面，明确它不是用户原话。
SESSION_MESSAGE_HOST_EVENT_MARKER = "[SESSION_MESSAGE_HOST_EVENT]"
# 唤醒信封 metadata 里记"这次唤醒投递的是哪条消息"的字段：值是那条消息的 guidance 幂等键，
#   领取后的"已消费"判据按它查回执，不再按会话对拼键。
SESSION_MESSAGE_KEY_FIELD = "message_dedupe_key"

# owner_kind 结构化取值；main 是管理员（含 IM 绑定管理员私聊），user/group 是普通用户。
OWNER_KIND_MAIN = "main"


# LLM: 每条消息一个幂等键：发送方、目标之外再带上这次发送自己的结构化身份（模型工具用本次调用的 operation_id，
#   /tell 每次调用新生成）。同一次工具调用重试 operation_id 不变 → 同一个键 → append_once 返回原消息；
#   不同的发送各自入队、各有回执。send_id 为空是调用方缺陷，抛 ValueError，不退回按会话对的旧键。
# 函数用途: 生成一条会话消息在 guidance 队列里的幂等键。
def session_message_dedupe_key(sender_thread_id: str, target_thread_id: str, send_id: str) -> str:
    identity = str(send_id or "").strip()
    if not identity:
        raise ValueError("session message send_id is required")
    return f"session_message:{sender_thread_id}->{target_thread_id}:{identity}"


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


# LLM: 纯函数，无 IO、无副作用；顺序固定：先 kind 合法性，再身份 fail closed，再目标归属，再渠道白名单，
#   再自派任务，最后按 owner_kind 与开关。目标不存在（None）与目标属于别的 owner 都返回同一码
#   SESSION_TARGET_OUT_OF_SCOPE，不泄露存在性；发送方 target 就是自己时任务拒绝、消息允许。
#   身份缺失（provider/owner_kind/owner_id 任一为空）一律 fail closed，绝不默认成 main/local。
# 函数用途: 按结构化字段判定一次会话间消息或派任务是否允许。
def decide_session_messaging(request: SessionMessagingRequest) -> SessionMessagingDecision:
    kind = str(request.kind or "").strip().lower()
    if kind not in SESSION_KINDS:
        return SessionMessagingDecision(False, "SESSION_KIND_INVALID", (f"unknown_kind:{kind}",))

    # 身份 fail closed：结构化身份三元组任一缺失都拒绝，不能把"拿不到"当成管理员。
    if not _identity_complete(request.sender_identity):
        return SessionMessagingDecision(
            False, SESSION_IDENTITY_UNAVAILABLE, ("sender_identity_incomplete",)
        )

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

    # 接收方渠道白名单：只允许本地渠道（TUI/CLI/本机）；空渠道按本地处理（无绑定的本机会话）。
    # 不在白名单里的一律拒绝（fail closed），包括以后新增的 IM 渠道，避免封闭枚举漏项。
    target_channel = str(request.target_channel or "").strip().lower()
    if target_channel and target_channel not in LOCAL_TARGET_CHANNELS:
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


# LLM: 身份三元组任一为空即视为不可用；这是 fail-closed 判据，不做任何默认值补齐。
# 函数用途: 判断结构化 owner 身份是否完整可用。
def _identity_complete(identity: OwnerIdentity) -> bool:
    return all(
        str(getattr(identity, field, "") or "").strip()
        for field in ("provider", "owner_kind", "owner_id")
    )


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
        return bool(config.session_messaging_admin_enabled)
    return bool(config.session_messaging_user_enabled)


# LLM: 派任务第一期只开给管理员，且没有普通用户开关；返回 False 时工具不进 registry。
# 函数用途: 判断当前 owner 是否应该看到"派会话任务"工具。
def session_task_tool_visible(home_paths: object, config: object) -> bool:
    owner_kind = str(getattr(home_paths, "owner_kind", "") or "").strip()
    return owner_kind == OWNER_KIND_MAIN and bool(config.session_task_admin_enabled)


__all__ = [
    "SESSION_MESSAGE_KEY_FIELD",
    "session_message_dedupe_key",
    "OWNER_KIND_MAIN",
    "SESSION_KINDS",
    "SESSION_KIND_MESSAGE",
    "SESSION_KIND_TASK",
    "SESSION_MESSAGING_DISABLED",
    "SESSION_TARGET_OUT_OF_SCOPE",
    "SESSION_TARGET_CHANNEL_UNSUPPORTED",
    "SESSION_IDENTITY_UNAVAILABLE",
    "SESSION_NO_CURRENT_THREAD",
    "SESSION_TASK_CHAIN_LIMIT",
    "SESSION_TASK_ORIGIN_KIND",
    "SESSION_TASK_RATE_LIMIT",
    "LOCAL_TARGET_CHANNELS",
    "SESSION_TASK_NOT_ALLOWED",
    "SESSION_TASK_TARGET_SELF",
    "SessionMessagingDecision",
    "SessionMessagingRequest",
    "decide_session_messaging",
    "session_messaging_tool_visible",
    "session_task_tool_visible",
]
