
from __future__ import annotations

# LLM: C 冷准备与实际 Agent 共用同一配置/owner 权限/工作区解析，冻结配置不是执行授权；修改须联查 CLI/C 构造与 owner 根测试。
# LLM: Agent construction and shared runtime helpers live here; dependency-light command startup
# primitives belong to cli.bootstrap and are re-exported for compatibility.
# 模块用途: 提供智能体构造、工作区解析、能力路由和格式化等公共运行时逻辑；轻量启动能力
# 由 bootstrap 模块统一负责，避免交互入口过早加载整个智能体。

"""provides CLI bootstrap helpers for config loading, agent construction, routing, and formatting.

给人看的解释：
所有命令都会反复做几件事：加载配置、创建 SimpleAgent、创建能力路由、格式化时间。
这些公共动作统一放这里，避免每个命令模块各写一遍。
"""

import copy
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
    config_with_owner_permission_mode,
    explicit_workspace_root,
    explicit_workspace_roots,
    owner_home_workspace_root,
    resolve_workspace_root,
    resolve_workspace_roots,
    validate_requested_workspace_roots,
)

CHAT_PROMPT = "你> "
PLAIN_CHAT_PROMPT = "user> "


# LLM: 这是 CLI 与 C 唯一配置/constructor roots 解析；内部冻结副本不重读 YAML/环境，但仍沿原 owner 权限校验，不创建 Agent、目录或业务账。
# 函数用途: 解析真实构造参数，让控制面与稍后的 Agent 使用相同配置来源及工作区规则。
def agent_configuration(args):
    frozen = getattr(args, "_gateway_frozen_config", None)
    config = (copy.deepcopy(frozen) if frozen is not None
              else apply_runtime_config_environment(load_config(args.config)))
    config = config_with_owner_permission_mode(config)
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
    return config, roots


# LLM: 普通 CLI 保持原配置/权限/根顺序；C factory 只消费组合根冻结的同源参数，真实 SimpleAgent 仍负责全部业务初始化与权限门。
# 函数用途: 按共享解析结果创建实际 Agent；目录、存储和后端副作用仍只由原构造函数执行。
def make_agent(args) -> SimpleAgent:
    config, roots = agent_configuration(args)
    return SimpleAgent(config, roots[0], workspace_roots=roots)



# LLM: 仅公开Gateway运行入口调用；原YAML/显式工作区解析一次后冻结，外来private Namespace不能覆盖部署数量。
# 函数用途: 给原协调入口运输同源配置与派生数量；不启动Agent、读业务账或授予执行身份。
def frozen_gateway_arguments(args):
    from ..agent.settings.gateway_topology import validate_gateway_worker_process_count

    public = copy.copy(args)
    if hasattr(public, "_gateway_frozen_config"):
        delattr(public, "_gateway_frozen_config")
    config, roots = agent_configuration(public)
    frozen = copy.copy(args)
    frozen._gateway_frozen_config = config
    frozen._gateway_worker_count = validate_gateway_worker_process_count(config.gateway_worker_process_count)
    frozen.workspace_root = [str(root) for root in roots]
    return frozen


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


# LLM: 命令行显式参数优先；没给时用调用方传入的代码默认值。参数减量第 3 批（2026-09-27）起 cli_*/subagent_* 这类
#   命令默认值不再是配置项，各命令模块在读取点旁边定义常量再传进来；本函数不读配置、不写文件。
# 函数用途: 取命令行整数参数，缺省时用代码默认值。
def int_arg_or_default(args, arg_name: str, default: int) -> int:
    value = getattr(args, arg_name, None)
    return int(default if value is None else value)
