
from __future__ import annotations

# LLM: Agent construction and shared runtime helpers live here; dependency-light command startup
# primitives belong to cli.bootstrap and are re-exported for compatibility.
# 模块用途: 提供智能体构造、工作区解析、能力路由和格式化等公共运行时逻辑；轻量启动能力
# 由 bootstrap 模块统一负责，避免交互入口过早加载整个智能体。

"""provides CLI bootstrap helpers for config loading, agent construction, routing, and formatting.

给人看的解释：
所有命令都会反复做几件事：加载配置、创建 SimpleAgent、创建能力路由、格式化时间。
这些公共动作统一放这里，避免每个命令模块各写一遍。
"""

import sys
import time
from pathlib import Path

from ..agent.capability import CapabilityRouter
from ..agent.core import SimpleAgent
from ..agent.settings import load_config
from ..agent.settings.services.runtime_config_env import apply_runtime_config_environment
from .bootstrap import (
    DEFAULT_CAPABILITY_CONFIG,
    DEFAULT_CONFIG,
    ROOT,
    add_resume_context_switches,
    configure_stdio,
)
from .workspace_resolution import (
    explicit_workspace_root,
    explicit_workspace_roots,
    owner_home_workspace_root,
    resolve_workspace_root,
    resolve_workspace_roots,
    validate_requested_workspace_roots,
)

CHAT_PROMPT = "你> "
PLAIN_CHAT_PROMPT = "user> "


def make_agent(args) -> SimpleAgent:

    config = apply_runtime_config_environment(load_config(args.config))
    # 一次声明多个目录时，整串会被当成一条路径——必须逐项解析后再写回配置。
    explicit_roots = explicit_workspace_roots(args)
    if explicit_roots:
        config.workspace_root = [str(root) for root in explicit_roots]
    roots = resolve_workspace_roots(
        config,
        args.config,
        current_dir=owner_home_workspace_root(config),
    )
    validate_requested_workspace_roots(config, roots)
    return SimpleAgent(config, roots[0], workspace_roots=roots)


def make_capability_router(agent: SimpleAgent, capability_config, skill_dirs: list[str] | None):
    service = getattr(agent, "skills_service", None)
    router = getattr(agent, "capability_router", None)
    if service is None or not isinstance(router, CapabilityRouter):
        raise RuntimeError("SimpleAgent SkillsService 未装配")
    service.set_extra_roots(skill_dirs or ())
    router.config = capability_config
    return router


def format_local_time(timestamp: float) -> str:

    if not timestamp:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def resume_context_override(args) -> bool | None:

    return getattr(args, "resume_context", None)


def _memory_record_count(agent: SimpleAgent) -> int:
    try:
        return len(agent.memory.all())
    except Exception as exc:
        print(
            f"memory count failed error_code={type(exc).__name__} error={exc}",
            file=sys.stderr,
        )
        return 0
