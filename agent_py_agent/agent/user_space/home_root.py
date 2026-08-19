from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

# LLM: This is the single dependency-light authority for configured home precedence. Full agents
# and lightweight Gateway clients must call it so MY_AGENT_HOME isolation cannot diverge.
# 模块用途: 决定 my-agent 数据根目录，统一处理配置值、环境变量和历史默认值的优先级。


# LLM: Explicit non-default config wins; the historical literal ~/.my-agent is treated as an
# unset default so MY_AGENT_HOME can isolate tests and deployments. Return text, not created paths.
# 函数用途: 解析 my-agent home 根目录；只做选择，不创建目录或写入任何状态。
def configured_home_root(
    config,
    *,
    env: Mapping[str, str] | None = None,
) -> str | None:
    source = os.environ if env is None else env
    raw = str(getattr(config, "my_agent_home", "") or "").strip()
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


__all__ = ["configured_home_root"]
