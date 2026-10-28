
from __future__ import annotations

# LLM: 管理CLI与实际Agent共用配置/owner/路径解析，导入本模块不加载core；冻结配置不授权执行，管理解析不初始化业务。
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
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from ..agent.capability import CapabilityRouter
from ..agent.gateway_parts.request_client import GatewayClientContext
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

if TYPE_CHECKING:
    from ..agent.core import SimpleAgent


# LLM: CLI与C冻结同源配置和构造根；只做身份/路径检查，允许原构造器稍后创建合法目录，不提前物化Home或借配置授运行权限。
# 函数用途: 解析实际构造参数，保留先初始化私有Home、再创建用户工作目录的原顺序。
def agent_configuration(args):
    frozen = getattr(args, "_gateway_frozen_config", None)
    config = (copy.deepcopy(frozen) if frozen is not None
              else apply_runtime_config_environment(load_config(args.config)))
    config = config_with_owner_permission_mode(config)
    # 一次声明多个目录时，整串会被当成一条路径——必须逐项解析后再写回配置。
    explicit_roots = explicit_workspace_roots(args, initializing=True)
    if explicit_roots:
        config.workspace_root = [str(root) for root in explicit_roots]
    roots = resolve_workspace_roots(
        config,
        args.config,
        current_dir=owner_home_workspace_root(config),
    )
    validate_requested_workspace_roots(config, roots, initializing=True)
    if config.workspace_root:
        config.workspace_root = [str(root) for root in roots]
    return config, roots


# LLM: 仅真实执行才惰性导入core；保留旧构造替换点和单位置args签名，私有标记严格True才延后MCP。
# 函数用途: 按共享解析结果创建实际Agent，管理命令应使用轻上下文，业务构造副作用仍归原构造器。
def make_agent(args) -> SimpleAgent:
    config, roots = agent_configuration(args)
    constructor = globals().get("SimpleAgent")
    if constructor is None:
        from ..agent.core import SimpleAgent as constructor
    if getattr(args, '_defer_mcp_connect', None) is True:
        return constructor(config, roots[0], workspace_roots=roots, defer_mcp_connect=True)
    return constructor(config, roots[0], workspace_roots=roots)


# LLM: 显式读取旧构造类型时才加载core；导入本模块不触发，原构造替换点继续受make_agent尊重。
# 函数用途: 保留原SimpleAgent符号的按需访问，不让普通管理模块冷导入完整运行时。
def __getattr__(name: str):
    if name != "SimpleAgent":
        raise AttributeError(name)
    from ..agent.core import SimpleAgent

    return SimpleAgent


_default_make_agent = make_agent


# LLM: 使用原CLI配置/owner权限/运行路径规则，默认不导入core、建目录或预检凭据；显式替换的原factory作为注入边界保留。
# 函数用途: 为管理命令解析同源配置与服务地址，避免只为查状态或投递请求创建完整Agent。
def make_lightweight_cli_agent_context(args, *, factory=None):
    selected = make_agent if factory is None or factory is _default_make_agent else factory
    if selected is not _default_make_agent:
        return selected(args)
    from ..agent.user_space.home_layout import home_paths
    from ..agent.user_space.home_root import configured_home_root
    from ..agent.user_space.owner_access import resolve_owner_scope_and_access
    from ..agent.user_space.owner_resolver import (
        home_paths_with_owner,
        owner_identity_from_config,
        resolve_owner_home,
    )
    from ..agent.user_space.runtime_paths import (
        apply_runtime_paths_to_config,
        resolve_runtime_paths_for_agent,
    )

    config, roots = agent_configuration(args)
    base = home_paths(configured_home_root(config))
    owner = resolve_owner_home(base.root, owner_identity_from_config(config))
    scoped = home_paths_with_owner(base, owner)
    root = roots[0]
    # owner策略只收紧工具档位；原权限墙裁决独立于策略，不能用进程cwd或TUI共享服务根替代。
    scope, _ = resolve_owner_scope_and_access(scoped, config)
    package_entry = any((root / name).is_file() for name in ("__main__.py", "__main__.pyc"))
    workspace = Path(scope).expanduser().resolve(strict=False) if scope else root.parent if package_entry else root
    resolution = resolve_runtime_paths_for_agent(config, workspace, scoped)
    apply_runtime_paths_to_config(config, resolution)
    return GatewayClientContext(config, root, roots, scoped, workspace, resolution.paths)



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


# LLM: 原技能目录和路由配置保持；仅挂接宿主准备回调，首次OPEN检索才连接，不在此处触发MCP。
# 函数用途: 取得代理能力路由器并为CLI接通按需目录准备。
def make_capability_router(agent: SimpleAgent, capability_config, skill_dirs: list[str] | None):
    service = getattr(agent, "skills_service", None)
    router = getattr(agent, "capability_router", None)
    if service is None or not isinstance(router, CapabilityRouter):
        raise RuntimeError("SimpleAgent SkillsService 未装配")
    service.set_extra_roots(skill_dirs or ())
    router.config = capability_config
    preparer = getattr(agent, 'ensure_mcp_catalog_ready', None)
    if callable(preparer):
        router._ensure_mcp_catalog = partial(preparer, router)
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
