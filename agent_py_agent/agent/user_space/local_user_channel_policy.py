# LLM: 通道守卫只检查结构化目标身份，不信任旧绑定或用户JSON里的放宽声明。
# 模块用途: 为绑定、IM入站和飞书认证共用普通本地用户纯Web限制。
from __future__ import annotations

from ..plugin_host.errors import HostAPIError


# LLM: identity由宿主resolver或provider记录提供；普通local/user任何非web入口均403，无IO。
# 函数用途: 在绑定和使用旧绑定之前拒绝受管账号进入IM通道。
def require_local_user_channel(identity, channel: str) -> None:
    if isinstance(identity, str):
        managed = identity.startswith("local/") and identity != "local/main"
    else:
        managed = identity.provider == "local" and identity.owner_kind != "main"
    if managed and channel != "web":
        raise HostAPIError("MANAGED_USER_WEB_ONLY_CHANNEL_RESTRICTED", 403,
                           message="受管本地账号仅允许通过Web访问，不能绑定或使用IM通道。")
