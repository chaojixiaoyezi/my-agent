from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# LLM: This is the single dependency-light authority for configured home precedence. Full agents
# and lightweight Gateway clients must call it so MY_AGENT_HOME isolation cannot diverge.
# C的内部typed快照只是本入口原解析结果；无快照时配置/环境/历史默认优先级完全保持，快照不签发owner或路径权限。
# 模块用途: 决定 my-agent 数据根目录，统一处理配置值、环境变量和历史默认值的优先级。


# LLM: 仅可信C冷构造保存原解析的规范Path和同份配置值；dataclass replace须保留，改my_agent_home却留旧快照时拒绝，不能由YAML或请求注入。
# 类用途: 承运已选中的home事实，避免延后构造因环境变化重新选源；不读盘、不授执行权。
@dataclass(frozen=True, repr=False)
class ConfiguredHomeSnapshot:
    root: Path
    configured_value: str


# LLM: Explicit non-default config wins; the historical literal ~/.my-agent is treated as an
# unset default so MY_AGENT_HOME can isolate tests and deployments. Return text, not created paths.
#   内部C快照存在时先验证同份配置关联，再返回同一原解析结果，不重读环境；普通配置仍按原规则解析。
# 函数用途: 解析 my-agent home 根目录；只做选择，不创建目录或写入任何状态。
def configured_home_root(
    config,
    *,
    env: Mapping[str, str] | None = None,
) -> str | None:
    raw = str(getattr(config, "my_agent_home", "") or "").strip()
    snapshot = getattr(config, "host_home_snapshot", None)
    if snapshot is not None:
        if (type(snapshot) is not ConfiguredHomeSnapshot or raw != snapshot.configured_value
                or not isinstance(snapshot.root, Path) or not snapshot.root.is_absolute()
                or snapshot.root != snapshot.root.resolve() or raw != str(snapshot.root)):
            raise ValueError("configured_home_snapshot_invalid")
        return str(snapshot.root)
    source = os.environ if env is None else env
    env_home = str(source.get("MY_AGENT_HOME", "") or "").strip()
    if raw:
        try:
            default_home = Path("~/.my-agent").expanduser().resolve()
            configured = Path(raw).expanduser().resolve()
        except OSError:
            configured = None
        if configured is None or configured != default_home:
            return raw
        return env_home or raw
    return env_home or None


__all__ = ["ConfiguredHomeSnapshot", "configured_home_root"]
