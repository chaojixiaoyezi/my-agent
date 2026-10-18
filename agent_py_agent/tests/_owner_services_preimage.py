# LLM: 仅测试oracle，函数正文逐字来自X03获批前像；不作为产品兼容实现或运行时权限来源。
# 模块用途: 对照纯owner资源提取的原完整算法，前像摘要由片A冻结证据记录。
from dataclasses import replace

from agent_py_agent.agent.owner_scoped_pool import _config_with_owner
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    home_paths_with_owner,
    owner_identity_from_config,
    resolve_owner_home,
)
from agent_py_agent.agent.user_space.runtime_paths import resolve_runtime_paths_for_agent


def _owner_resource_configuration(transaction):
    control, owner = transaction.control, transaction.owner
    base = home_paths(control.home_root)
    home = home_paths_with_owner(base, resolve_owner_home(control.home_root, owner))
    config = _config_with_owner(control.config, owner)
    if owner != owner_identity_from_config(control.config):
        fields = resolve_runtime_paths_for_agent(control.config, base.owner_home_dir, base).paths
        config = replace(config, **{key: "" for key in fields if hasattr(config, key)})
    paths = resolve_runtime_paths_for_agent(config, home.owner_home_dir, home).paths
    return home, config, paths
