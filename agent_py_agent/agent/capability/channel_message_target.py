# LLM: send_message 的目标解析只消费 owner、能力配置和管理员绑定记录；不接收任意收件人。
# 模块用途: 为本机管理员解析已绑定的外发渠道，普通用户仍只能向自己的渠道发送；同步检查消息工具和绑定测试。
from __future__ import annotations

from pathlib import Path

from ..user_space.admin_channel_identity import (
    AdminIdentityStoreError,
    list_admin_channel_identities,
)


# LLM: 返回 provider/target/owner_root/error；调用方在 availability、业务幂等键和发送前共用此函数，绑定变更不沿用旧缓存。
# 函数用途: 只读可信身份配置，解析当前用户或本机管理员的唯一消息接收人；不发请求、不写文件。
def resolve_message_target(agent: object, channel: str = "") -> tuple[str, str, Path | None, str]:
    home = getattr(agent, "home_paths", None)
    config = getattr(agent, "config", None)
    provider = str(getattr(home, "owner_provider", "") or "").strip().lower()
    requested = str(channel or "").strip().lower()
    root = getattr(home, "owner_home_dir", None)
    try:
        owner_root = Path(root).expanduser().resolve(strict=True) if root else None
    except OSError:
        owner_root = None
    if owner_root is None:
        return "", "", None, "CHANNEL_ADAPTER_UNAVAILABLE"
    if provider != "local":
        if requested and requested != provider:
            return "", "", owner_root, "CHANNEL_TARGET_OUT_OF_SCOPE"
        target = str(getattr(config, "my_agent_owner_id", "") or "").strip()
        return provider, target, owner_root, "" if target else "CHANNEL_ADAPTER_UNAVAILABLE"
    # MyAgentHomePaths.owner_id 是目录层规范键 local/main，不是 OwnerIdentity.owner_id 的 main。
    if (getattr(home, "owner_kind", "") != "main" or getattr(home, "owner_id", "") != "local/main"
            or not bool(getattr(config, "admin_channel_identity_enabled", False))):
        return "", "", owner_root, "CHANNEL_TARGET_OUT_OF_SCOPE"
    from .config import CapabilityConfig
    from .runtime_config_reload import capability_config_for_agent

    policy = capability_config_for_agent(agent) or CapabilityConfig()
    declared = policy.admin_message_channels
    allowed = {str(item).strip().lower() for item in declared} if isinstance(declared, list) else set()
    if requested and requested not in allowed:
        return "", "", owner_root, "CHANNEL_TARGET_OUT_OF_SCOPE"
    home_root = getattr(home, "root", None)
    if not home_root:
        return "", "", owner_root, "CHANNEL_ADAPTER_UNAVAILABLE"
    try:
        candidates = [item for item in list_admin_channel_identities(home_root)
                      if item.channel in allowed and (not requested or item.channel == requested)]
    except AdminIdentityStoreError:
        return "", "", owner_root, "CHANNEL_BINDING_INVALID"
    if len(candidates) != 1:
        return "", "", owner_root, "CHANNEL_TARGET_AMBIGUOUS" if candidates else "CHANNEL_ADAPTER_UNAVAILABLE"
    identity = candidates[0]
    return identity.channel, identity.user_id, owner_root, ""
