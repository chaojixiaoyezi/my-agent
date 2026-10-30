# LLM: 仅从宿主给定的完整规范home回验只读身份，不扫描目录、不默认管理员、不提供执行或停止授权。
# 模块用途: 为旧进程观察入口建立可证明的owner上下文，拒绝后代目录和含糊布局。
from pathlib import Path

from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home


# LLM: 只匹配home完整布局后缀并经原resolver回验；同名owners组件不改变根推导，拒绝后代目录，不物化目录。
# 函数用途: 只为规范owner home本身取得稳定编号，供观察兼容和显式上下文冲突检查使用。
def canonical_observation_owner_id(home: Path) -> str | None:
    try:
        canonical = home.resolve()
        parts = canonical.parts
        if len(parts) >= 4 and parts[-3:-1] == ("owners", "local"):
            root = canonical.parents[2]
            identity = OwnerIdentity.local_main() if parts[-1] == "main" else OwnerIdentity.local_user(parts[-1])
        elif (len(parts) >= 6 and parts[-5:-3] == ("owners", "providers")
              and parts[-2] in {"users", "groups"}):
            root = canonical.parents[4]
            identity = (OwnerIdentity.provider_user(parts[-3], parts[-1]) if parts[-2] == "users"
                        else OwnerIdentity.provider_group(parts[-3], parts[-1]))
        else:
            return None
        owner = resolve_owner_home(root, identity)
        return owner.owner_id if owner.home_dir == canonical else None
    except (OSError, ValueError, RuntimeError):
        return None
