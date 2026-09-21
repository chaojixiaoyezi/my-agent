# LLM: 后台路由沿原线程 binding、owner 路径、owner 身份、普通 binding 顺序读取，不授予执行或投递权限。
# 仅接收三个只读能力；持久读取在调用时发生，不能冻结成另一份路由状态，联测观察、唤醒和冻结重投。
# 模块用途: 为后台工作选择已有投递地址；不执行模型、不消费事件、不发送消息。
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .channels import PROACTIVE_PUSH_CHANNELS
from .models import ConversationThread


# LLM: owner 路径和身份仅在更具体的线程路由缺失时读取；生成器保留路径字段的原始查询顺序。
# 类用途: 提供线程读取、owner 路径及身份读取三个能力，避免依赖整个调度器或 Agent。
@dataclass(frozen=True)
class BackgroundRouteDependencies:
    load_thread: Callable[[str], ConversationThread | None]
    owner_paths: Callable[[], Iterable[str]]
    owner_identity: Callable[[], tuple[str, str]]


# LLM: 保留原绑定顺序和目标回退规则；这里只读已经加载的线程，不查询或改写 owner 权限。
# 函数用途: 显式路由缺少目标时，从已有会话绑定取默认目标。
def default_route_target(thread: ConversationThread, route_channel: str) -> str:
    for binding in thread.channel_bindings:
        if binding.channel == route_channel:
            return binding.channel_conversation_id
    return (
        thread.channel_bindings[-1].channel_conversation_id
        if thread.channel_bindings
        else thread.thread_id
    )


# LLM: 只解析原 canonical owner 路径结构，返回第一个匹配；local 身份是否可外发由上层原通道声明裁决。
# 函数用途: 从调用方按原顺序提供的路径中读取 owner 身份，不检查文件或创建目录。
def owner_from_paths(paths: Iterable[str]) -> tuple[str, str]:
    for text in paths:
        match = re.search(r"owners/providers/([^/]+)/(?:users|groups)/([^/]+)", text)
        if match:
            return match.group(1), match.group(2)
    return "", ""


# LLM: 优先最新可外发 binding，其次原 owner 路径/属性，最后普通 binding；加载异常保持原 internal 回退。
# 函数用途: 按当前线程事实选择后台回复地址；读操作可能失败，但不把地址选择当成发送成功。
def resolve_background_route(
    dependencies: BackgroundRouteDependencies,
    thread_id: str,
) -> tuple[str, str]:
    try:
        thread = dependencies.load_thread(thread_id)
    except Exception:
        thread = None
    bindings = list(getattr(thread, "channel_bindings", ()) or ())
    for binding in reversed(bindings):
        channel = str(getattr(binding, "channel", "") or "")
        target = str(
            getattr(binding, "channel_user_id", "")
            or getattr(binding, "channel_conversation_id", "")
            or ""
        )
        if channel in PROACTIVE_PUSH_CHANNELS and target:
            return channel, target
    provider, open_id = owner_from_paths(dependencies.owner_paths())
    if provider in PROACTIVE_PUSH_CHANNELS and open_id:
        return provider, open_id
    owner_channel, owner_id = dependencies.owner_identity()
    if owner_channel in PROACTIVE_PUSH_CHANNELS and owner_id:
        return owner_channel, owner_id
    if bindings:
        binding = bindings[-1]
        channel = str(getattr(binding, "channel", "") or "internal")
        target = str(
            getattr(binding, "channel_user_id", "")
            or getattr(binding, "channel_conversation_id", "")
            or ""
        )
        return channel, target or default_route_target(thread, channel)
    return "internal", ""
